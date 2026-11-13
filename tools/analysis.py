#!/usr/bin/env python3
"""Statistical analysis of a feed trial, exactly as pre-registered in planned_outcomes.

Every analysis treats the tank or cage as the experimental unit and respects the level at which
each factor was applied (design_factors.applied_at). The reference design is a split plot:
a whole-plot factor (e.g. temperature) assigned to systems, diets assigned to the units of a
system, one unit per diet per system. A trial without a whole-plot factor is analysed as a
randomised block design with systems as blocks.

Analyses (ANALYSES registry, one function per key; each is exercised by tools/test_analysis.py):
  response                 outcome on its planned scale (raw, log, empirical logit)
  split_plot_anova         balanced stratum ANOVA: whole-plot factor against systems,
                           diet and diet x whole-plot factor against the unit residual
  stratum_contrasts        estimates with SE, df, t, p and CI from the same two strata; also
                           valid when a unit is missing (incomplete systems drop out)
  variance_components      between-system and between-unit variance from the two error rows
  randomisation_test_diet  exact re-randomisation of diets within systems (no normality needed)
  randomisation_test_whole exact re-randomisation of the whole-plot factor over systems
  holm                     Holm step-down adjustment within a multiplicity family
  power_t / mdd            power and minimum detectable difference of a two-sided t test
  design_power             power of each term for a number of systems per level
  plan_power_check         are expected_sd, mdd, alpha and power of the plan consistent?
  fish_level_naive         the analysis NOT to run (fish as replicates), kept as a demonstration
  sensitivity_pending      primary contrasts without units whose exclusion is pending
  run_record               hashes that make a reported number reproducible (analysis_runs)
  run_all                  everything above for every planned outcome of a trial

Nothing here writes to the database. Requirements beyond the core tools: numpy, scipy.
Usage: analysis.py [data_model.yaml] [database] [--json results.json]
"""
import hashlib
import itertools
import json
import math
import os
import sqlite3
import sys

import numpy as np
from scipy import integrate, optimize, stats

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import metrics as M  # noqa: E402

DEFAULT_YAML = os.path.join(os.path.dirname(HERE), "data_model.yaml")
# An error stratum whose SD is below this share of the size of the data holds rounding noise,
# not biological variation; an F ratio on it is not reported (see split_plot_anova).
DEGENERATE_REL_SD = 1e-6
MAX_EXACT_ALLOCATIONS = 1 << 20


class DesignError(ValueError):
    """The data do not have the structure the requested analysis needs."""


# ---------------------------------------------------------------- design and dataset
def design(con, trial_id):
    """Factors of the trial as declared in the database: reference diet, the whole-plot factor
    (assigned treatment applied to systems or sites) and its level per system."""
    def q(sql, *args):                      # rows as dictionaries, without touching con.row_factory
        cur = con.execute(sql, args)
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    diets = q("SELECT diet_id, control_role FROM diets WHERE trial_id = ? ORDER BY diet_id", trial_id)
    if len(diets) < 2:
        raise DesignError("trial %s has %d diet(s); a diet contrast needs at least 2" % (trial_id, len(diets)))
    refs = [d["diet_id"] for d in diets if d["control_role"] == "reference"]
    if len(refs) > 1:
        raise DesignError("more than one reference diet: %s" % refs)
    ref = refs[0] if refs else diets[0]["diet_id"]
    whole = q("SELECT factor_id, name, applied_at FROM design_factors WHERE trial_id = ? AND"
              " role = 'assigned_treatment' AND applied_at IN ('system', 'site')", trial_id)
    if len(whole) > 1:
        raise DesignError("more than one whole-plot factor (%s): not supported" % [w["name"] for w in whole])
    out = {"trial_id": trial_id, "reference_diet": ref, "diets": [d["diet_id"] for d in diets],
           "whole_factor": None, "whole_levels": ["all"], "reference_level": "all", "system_level": {}}
    systems = [r["system_id"] for r in q("SELECT system_id FROM systems WHERE trial_id = ? ORDER BY system_id", trial_id)]
    if not whole:
        out["system_level"] = {s: "all" for s in systems}
        return out
    w = whole[0]
    rows = q("SELECT system_id, level, level_order, is_reference FROM factor_levels WHERE factor_id = ?"
             " AND system_id IS NOT NULL", w["factor_id"])
    order = {}
    for r in rows:
        out["system_level"][r["system_id"]] = r["level"]
        order[r["level"]] = (r["level_order"] if r["level_order"] is not None else 10 ** 6, r["level"])
        if r["is_reference"]:
            out["reference_level"] = r["level"]
    missing = [s for s in systems if s not in out["system_level"]]
    if missing:
        raise DesignError("systems without a level of %s: %s" % (w["name"], missing))
    out["whole_factor"] = w["name"]
    out["whole_levels"] = sorted(order, key=order.get)
    if out["reference_level"] not in out["whole_levels"]:
        out["reference_level"] = out["whole_levels"][0]
    return out


def analysis_dataset(con, trial_id, model=None):
    """ITT units with their design labels and the unit-level metrics of tools/metrics.py."""
    if model is not None:
        M.use_model(con, model)
    des = design(con, trial_id)
    units = []
    for r in M.unit_summary(con, trial_id):
        if not r.get("itt"):
            continue
        u = dict(r)
        u["whole_level"] = des["system_level"][r["system_id"]]
        units.append(u)
    if not units:
        raise DesignError("trial %s has no ITT unit" % trial_id)
    return des, units


def response(unit, outcome, scale, analyte=None):
    """Value of a planned outcome for one unit on the planned scale; None when it is missing.

    raw    the metric as defined in derived_metrics
    log    natural log of the unit value (planned_outcomes.log_definition = log_of_unit_mean)
    logit  empirical logit of survival, ln((alive + 0.5) / (dead + 0.5)), from the d0 count; the
           0.5 keeps a unit without deaths finite
    """
    v = unit.get(outcome)
    if outcome == "adc_pct":
        v = (v or {}).get(analyte or "protein")
    if v is None:
        return None
    v = float(v)
    if scale in (None, "raw"):
        return v
    if scale == "log":
        if v <= 0:
            raise DesignError("log scale needs a positive value: %s = %r in %s" % (outcome, v, unit["unit_id"]))
        return math.log(v)
    if scale == "logit":
        if outcome != "survival_pct":
            raise DesignError("logit scale is implemented for survival_pct only, not %s" % outcome)
        n = unit.get("n_fish_d0")
        if not n:
            return None
        dead = n * (1.0 - v / 100.0)
        return math.log((n - dead + 0.5) / (dead + 0.5))
    raise DesignError("unknown scale %r" % scale)


def _layout(units, y, des):
    """system -> diet -> mean response of the units of that diet in the system (units with a
    missing response are left out), and system -> whole-plot level."""
    cell = {}
    for u in units:
        v = y.get(u["unit_id"])
        if v is None:
            continue
        cell.setdefault(u["system_id"], {}).setdefault(u["diet_id"], []).append(v)
    table = {s: {d: float(np.mean(vs)) for d, vs in ds.items()} for s, ds in cell.items()}
    reps = max((len(vs) for ds in cell.values() for vs in ds.values()), default=0)
    return table, {s: des["system_level"][s] for s in table}, reps


def _degenerate(error_ms, values):
    scale = max(abs(float(np.mean(values))), float(np.std(values)), 1e-300)
    return math.sqrt(max(error_ms, 0.0)) < DEGENERATE_REL_SD * scale


# ---------------------------------------------------------------- stratum ANOVA
def split_plot_anova(units, y, des):
    """Balanced split-plot ANOVA with two error strata.

    Rows: whole-plot factor A (df a-1) tested against systems within A (df a(n-1)); diet B
    (df b-1) and A x B (df (a-1)(b-1)) tested against the unit residual (df a(n-1)(b-1)).
    Needs every system to hold every diet and the same number of systems n >= 2 per level;
    raises DesignError otherwise (use stratum_contrasts, which tolerates a missing unit).
    With one whole-plot level the table is that of a randomised block design.
    When an error mean square is numerically zero the F ratio is withheld (degenerate = True).
    """
    table, level, _ = _layout(units, y, des)
    diets = des["diets"]
    incomplete = sorted(s for s, ds in table.items() if set(ds) != set(diets))
    if incomplete:
        raise DesignError("systems without every diet: %s" % ", ".join(incomplete))
    levels = [lv for lv in des["whole_levels"] if lv in set(level.values())]
    by_level = {lv: sorted(s for s in table if level[s] == lv) for lv in levels}
    n_set = {len(v) for v in by_level.values()}
    if len(n_set) != 1:
        raise DesignError("unequal number of systems per level: %s" % {k: len(v) for k, v in by_level.items()})
    n = n_set.pop()
    a, b = len(levels), len(diets)
    if n < 2:
        raise DesignError("%d system per level: no degrees of freedom for the error terms" % n)
    Y = np.array([[[table[s][d] for d in diets] for s in by_level[lv]] for lv in levels])   # a x n x b
    g = Y.mean()
    m_a, m_as, m_b, m_ab = Y.mean(axis=(1, 2)), Y.mean(axis=2), Y.mean(axis=(0, 1)), Y.mean(axis=1)
    ss_a = b * n * float(((m_a - g) ** 2).sum())
    ss_s = b * float(((m_as - m_a[:, None]) ** 2).sum())
    ss_b = a * n * float(((m_b - g) ** 2).sum())
    ss_ab = n * float(((m_ab - m_a[:, None] - m_b[None, :] + g) ** 2).sum())
    ss_e = max(float(((Y - g) ** 2).sum()) - ss_a - ss_s - ss_b - ss_ab, 0.0)
    df_a, df_s, df_b, df_ab, df_e = a - 1, a * (n - 1), b - 1, (a - 1) * (b - 1), a * (n - 1) * (b - 1)
    ms_s, ms_e = ss_s / df_s, ss_e / df_e
    wf = des["whole_factor"] or "block"
    deg_s, deg_e = _degenerate(ms_s, Y), _degenerate(ms_e, Y)

    def row(term, ss, df, err_ms, err_df, err_name, deg):
        r = {"term": term, "ss": ss, "df": df, "ms": ss / df if df else None, "error_term": err_name,
             "error_df": err_df, "f": None, "p": None}
        if df and err_name and not deg and err_ms > 0:
            r["f"] = (ss / df) / err_ms
            r["p"] = float(stats.f.sf(r["f"], df, err_df))
        return r

    rows = []
    if a > 1:
        rows.append(row(wf, ss_a, df_a, ms_s, df_s, "system within %s" % wf, deg_s))
    rows.append(row("system within %s" % wf if a > 1 else "system (block)", ss_s, df_s, None, None, None, True))
    rows.append(row("diet", ss_b, df_b, ms_e, df_e, "unit residual", deg_e))
    if a > 1:
        rows.append(row("diet x %s" % wf, ss_ab, df_ab, ms_e, df_e, "unit residual", deg_e))
    rows.append(row("unit residual", ss_e, df_e, None, None, None, True))
    return {"rows": rows, "a": a, "b": b, "n_systems_per_level": n, "grand_mean": float(g),
            "ms_system": ms_s, "df_system": df_s, "ms_unit": ms_e, "df_unit": df_e,
            "degenerate": bool(deg_e), "degenerate_system": bool(deg_s),
            "levels": levels, "diets": list(diets)}


def variance_components(anova):
    """Method-of-moments components from the two error rows of split_plot_anova.

    sigma2_unit = MS(unit residual); sigma2_system = (MS(system) - MS(unit residual)) / b. A
    negative estimate is reported as estimated (sigma2_system_raw) and truncated at 0 for the ICC.
    With few systems these are very imprecise: the chi-square interval for sigma2_unit is given."""
    b = anova["b"]
    raw = (anova["ms_system"] - anova["ms_unit"]) / b
    s2u, s2s = anova["ms_unit"], max(raw, 0.0)
    df = anova["df_unit"]
    lo, hi = (df * s2u / stats.chi2.ppf(0.975, df), df * s2u / stats.chi2.ppf(0.025, df))
    return {"sigma2_unit": s2u, "sd_unit": math.sqrt(s2u), "sd_unit_ci95": [math.sqrt(lo), math.sqrt(hi)],
            "df_unit": df, "sigma2_system_raw": raw, "sigma2_system": s2s, "sd_system": math.sqrt(s2s),
            "df_system": anova["df_system"], "icc_system": s2s / (s2s + s2u) if s2s + s2u > 0 else None,
            "system_component_truncated": raw < 0}


# ---------------------------------------------------------------- contrasts
def _contrast(term, label, est, var, df, stratum, n_sys, alpha, deg=False):
    c = {"term": term, "label": label, "estimate": est, "se": None, "df": df, "t": None, "p": None,
         "ci_low": None, "ci_high": None, "alpha": alpha, "stratum": stratum, "n_systems": n_sys, "note": None}
    if df <= 0:
        c["note"] = "no error degrees of freedom: estimate only"
        return c
    if deg:
        c["note"] = "the error variance is numerically zero (rounding only), so no test is made"
        return c
    se = math.sqrt(var)
    tq = float(stats.t.ppf(1 - alpha / 2, df))
    c.update(se=se, t=est / se, p=float(2 * stats.t.sf(abs(est / se), df)), ci_low=est - tq * se, ci_high=est + tq * se)
    return c


def stratum_contrasts(units, y, des, alpha=0.05):
    """Treatment contrasts with the error term of their own stratum.

    Unit stratum. For each diet k against the reference diet, d_s = y(s, k) - y(s, ref) is taken
    within every system s that holds both; the system effect cancels. The variance of d_s is
    pooled within whole-plot levels (df = systems - levels). Reported: the diet effect at each
    level, the diet main effect (unweighted mean over levels) and, against the reference level,
    the diet x level interaction.
    System stratum. For each level against the reference level, the means of complete systems
    are compared with the variance between systems pooled within levels (df = systems - levels).
    In a balanced design t^2 equals the F ratio of split_plot_anova. A system that lost a unit
    drops out of the contrasts that need it, and the result says how many systems were used.
    """
    table, level, _ = _layout(units, y, des)
    ref, lref = des["reference_diet"], des["reference_level"]
    levels = des["whole_levels"]
    wf = des["whole_factor"]
    allv = [v for ds in table.values() for v in ds.values()]
    out = []
    for k in des["diets"]:
        if k == ref:
            continue
        d = {s: ds[k] - ds[ref] for s, ds in table.items() if k in ds and ref in ds}
        by = {lv: [d[s] for s in sorted(d) if level[s] == lv] for lv in levels}
        empty = [lv for lv in levels if not by[lv]]
        if empty:
            raise DesignError("no system holds both %s and %s at level(s) %s" % (k, ref, ", ".join(empty)))
        n_sys = len(d)
        df = n_sys - len(levels)
        ss = sum(float(((np.array(v) - np.mean(v)) ** 2).sum()) for v in by.values())
        s2 = ss / df if df > 0 else float("nan")
        deg = df > 0 and _degenerate(s2 / 2.0, allv)
        means = {lv: float(np.mean(v)) for lv, v in by.items()}
        a = len(levels)
        out.append(_contrast("diet", "%s - %s" % (k, ref) + (", mean over %s" % wf if a > 1 else ""),
                             sum(means.values()) / a, s2 * sum(1.0 / len(v) for v in by.values()) / a ** 2,
                             df, "unit", n_sys, alpha, deg))
        if a > 1:
            for lv in levels:
                out.append(_contrast("diet at %s" % lv, "%s - %s at %s" % (k, ref, lv), means[lv], s2 / len(by[lv]),
                                     df, "unit", n_sys, alpha, deg))
            for lv in levels:
                if lv != lref:
                    out.append(_contrast("diet x %s" % wf, "(%s - %s) at %s minus at %s" % (k, ref, lv, lref),
                                         means[lv] - means[lref], s2 * (1.0 / len(by[lv]) + 1.0 / len(by[lref])),
                                         df, "unit", n_sys, alpha, deg))
    if wf:
        m = {s: float(np.mean([ds[k] for k in des["diets"]])) for s, ds in table.items() if set(ds) == set(des["diets"])}
        by = {lv: [m[s] for s in sorted(m) if level[s] == lv] for lv in levels}
        if all(by.values()):
            df = len(m) - len(levels)
            ss = sum(float(((np.array(v) - np.mean(v)) ** 2).sum()) for v in by.values())
            s2 = ss / df if df > 0 else float("nan")
            deg = df > 0 and _degenerate(s2 * len(des["diets"]), allv)
            for lv in levels:
                if lv != lref:
                    out.append(_contrast(wf, "%s - %s, mean over diets" % (lv, lref),
                                         float(np.mean(by[lv]) - np.mean(by[lref])),
                                         s2 * (1.0 / len(by[lv]) + 1.0 / len(by[lref])), df, "system", len(m), alpha, deg))
    return out


# ---------------------------------------------------------------- randomisation tests
def _exact_p(obs, stats_all):
    arr = np.abs(np.asarray(stats_all, dtype=float))
    tol = 1e-12 * max(1.0, abs(obs))
    return float((arr >= abs(obs) - tol).sum()) / len(arr)


def randomisation_test_diet(units, y, des, diet=None):
    """Exact randomisation test of the diet main effect (two diets compared).

    The diets were allocated to the units of each system at random, so under the null hypothesis
    'diet changes nothing in any unit' swapping the two labels inside a system is as likely as
    the allocation that was used. With N systems there are 2^N equally likely allocations; the p
    value is the share whose |statistic| is at least the observed one (statistic = diet main
    effect of stratum_contrasts). No distribution is assumed; the price is that p cannot go
    below 2 / 2^N (min_p)."""
    table, level, _ = _layout(units, y, des)
    ref = des["reference_diet"]
    others = [k for k in des["diets"] if k != ref]
    k = diet or others[0]
    d = {s: ds[k] - ds[ref] for s, ds in table.items() if k in ds and ref in ds}
    sys_ids = sorted(d)
    levels = [lv for lv in des["whole_levels"] if any(level[s] == lv for s in sys_ids)]
    n = len(sys_ids)
    if n < 1 or 2 ** n > MAX_EXACT_ALLOCATIONS:
        raise DesignError("%d systems: exact enumeration not available" % n)
    w = np.array([1.0 / (len(levels) * sum(level[t] == level[s] for t in sys_ids)) for s in sys_ids])
    dv = np.array([d[s] for s in sys_ids])
    signs = np.array(list(itertools.product((1.0, -1.0), repeat=n)))
    all_stats = signs @ (w * dv)
    obs = float((w * dv).sum())
    return {"statistic": obs, "n_systems": n, "n_allocations": int(2 ** n), "p": _exact_p(obs, all_stats),
            "min_p": 2.0 / 2 ** n, "diet": k, "reference": ref,
            "distribution": sorted(float(v) for v in all_stats) if 2 ** n <= 4096 else None}


def randomisation_test_whole(units, y, des, level=None):
    """Exact randomisation test of the whole-plot factor (one level against the reference).

    The factor was allocated to systems, so the systems are permuted: every way of giving the
    same number of systems to each of the two levels is equally likely under the null. The
    statistic is the difference between the level means of the system means."""
    table, lev, _ = _layout(units, y, des)
    if not des["whole_factor"]:
        raise DesignError("the trial has no whole-plot factor")
    lref = des["reference_level"]
    lv = level or [x for x in des["whole_levels"] if x != lref][0]
    m = {s: float(np.mean([ds[k] for k in des["diets"]])) for s, ds in table.items()
         if set(ds) == set(des["diets"]) and lev[s] in (lv, lref)}
    ids = sorted(m)
    n1 = sum(lev[s] == lv for s in ids)
    n = len(ids)
    if n1 == 0 or n1 == n:
        raise DesignError("no complete system at one of the levels %s, %s" % (lv, lref))
    n_alloc = math.comb(n, n1)
    if n_alloc > MAX_EXACT_ALLOCATIONS:
        raise DesignError("%d allocations: exact enumeration not available" % n_alloc)
    vals = np.array([m[s] for s in ids])
    total = vals.sum()
    all_stats = []
    for comb in itertools.combinations(range(n), n1):
        s1 = vals[list(comb)].sum()
        all_stats.append(s1 / n1 - (total - s1) / (n - n1))
    obs = float(np.mean([m[s] for s in ids if lev[s] == lv]) - np.mean([m[s] for s in ids if lev[s] == lref]))
    return {"statistic": obs, "n_systems": n, "n_allocations": n_alloc, "p": _exact_p(obs, all_stats),
            "min_p": (2.0 if n1 * 2 == n else 1.0) / n_alloc, "level": lv, "reference": lref}


# ---------------------------------------------------------------- multiplicity
def holm(pvalues):
    """Holm step-down adjusted p values (family-wise error rate), in the input order.
    Sorted ascending p_(1..m): adjusted_(i) = max over j <= i of min(1, (m - j + 1) * p_(j))."""
    p = [float(x) for x in pvalues]
    m = len(p)
    order = sorted(range(m), key=lambda i: p[i])
    adj, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adj[i] = running
    return adj


# ---------------------------------------------------------------- power
def power_t(delta, se, df, alpha=0.05):
    """Power of a two-sided t test with df error degrees of freedom for a true difference delta
    estimated with standard error se.

    T = (Z + delta/se) / sqrt(V/df) with Z standard normal and V chi-square(df), so the power is
    the integral over sqrt(V/df) of the two normal tail probabilities. Computed by one-dimensional
    quadrature rather than through the non-central t and F functions of scipy, which returned
    NaN or a negative probability for some arguments in scipy 1.17 (see tools/test_analysis.py)."""
    if df <= 0 or se <= 0:
        raise DesignError("power needs df > 0 and se > 0")
    tc = float(stats.t.ppf(1 - alpha / 2, df))
    ncp = delta / se

    rdf = math.sqrt(df)

    def f(s):                               # s = sqrt(V/df); sqrt(V) has a chi distribution
        return (stats.norm.cdf(ncp - tc * s) + stats.norm.cdf(-ncp - tc * s)) * stats.chi.pdf(s * rdf, df) * rdf

    return float(integrate.quad(f, 0, np.inf, epsabs=1e-12, epsrel=1e-10, limit=400)[0])


def mdd(se, df, alpha=0.05, power=0.80):
    """Minimum detectable difference: the true difference found with the given power."""
    if not alpha < power < 1:
        raise DesignError("power must lie between alpha and 1")
    return float(optimize.brentq(lambda d: power_t(d, se, df, alpha) - power, 0.0, 60.0 * se, xtol=1e-12 * se))


def design_se(term, sd_unit, sd_system, n_systems_per_level, n_levels=2, n_diets=2):
    """Standard error and error df of a two-level contrast in the split-plot design with one unit
    per diet per system. term: diet (main effect), interaction (difference of the diet effect
    between two levels) or whole (difference between two levels)."""
    n, a, b = n_systems_per_level, n_levels, n_diets
    df = a * (n - 1)                        # as in stratum_contrasts: one diet against the reference
    if term == "diet":
        return math.sqrt(2.0 * sd_unit ** 2 / (a * n)), df
    if term == "interaction":
        return math.sqrt(4.0 * sd_unit ** 2 / n), df
    if term == "whole":
        return math.sqrt(2.0 * (sd_system ** 2 + sd_unit ** 2 / b) / n), df
    raise DesignError("unknown term %r" % term)


def design_power(delta, term, sd_unit, sd_system=0.0, n_systems_per_level=2, n_levels=2, n_diets=2, alpha=0.05):
    """Power of one term for a planned number of systems per level (see design_se)."""
    se, df = design_se(term, sd_unit, sd_system, n_systems_per_level, n_levels, n_diets)
    return {"term": term, "delta": delta, "se": se, "df": df, "alpha": alpha,
            "n_systems_per_level": n_systems_per_level, "power": power_t(delta, se, df, alpha),
            "mdd_80": mdd(se, df, alpha, 0.80)}


def plan_power_check(outcome, des, n_systems_per_level, tol=0.02):
    """Do the power inputs of a planned outcome agree with the design that was run?
    Recomputes the power of the diet main effect at planned mdd from expected_sd (taken as the
    SD between units of the same system) and the number of systems; consistent when it is
    within tol of the declared power."""
    need = ("expected_sd", "mdd", "alpha", "power")
    if any(outcome.get(k) is None for k in need):
        return {"checked": False, "reason": "plan gives no " + ", ".join(k for k in need if outcome.get(k) is None)}
    a, b = len(des["whole_levels"]), len(des["diets"])
    se, df = design_se("diet", outcome["expected_sd"], 0.0, n_systems_per_level, a, b)
    got = power_t(outcome["mdd"], se, df, outcome["alpha"])
    return {"checked": True, "declared_power": outcome["power"], "computed_power": got, "se": se, "df": df,
            "computed_mdd": mdd(se, df, outcome["alpha"], outcome["power"]), "declared_mdd": outcome["mdd"],
            "consistent": abs(got - outcome["power"]) <= tol}


# ---------------------------------------------------------------- the analysis not to run
def fish_level_naive(con, units, des, alpha=0.05):
    """Fish as replicates against the unit as replicate, on the same data (ln individual weight
    at the end occasion; sample and individual weighings, qc_flag removed left out).

    naive    two-way layout diet x whole-plot level with every fish as an independent
             observation: the residual is the variation between fish within a cell and its df is
             (number of fish - number of cells). Fish of one tank share the tank, so this is
             pseudo-replication; the SE is too small and the df far too large.
    correct  the unit mean of ln weight analysed with stratum_contrasts.
    Kept as a demonstration and tested for its inflated false-positive rate; never use naive."""
    fish = {}
    for u in units:
        rows = con.execute("SELECT weight_g FROM weighings WHERE unit_id = ? AND occasion = ? AND weight_g IS NOT NULL"
                           " AND sampling_type IN ('sample', 'individual') AND (qc_flag IS NULL OR qc_flag != 'removed')",
                           (u["unit_id"], u["end_occasion"])).fetchall()
        if rows:
            fish[u["unit_id"]] = [math.log(r[0]) for r in rows]
    used = [u for u in units if u["unit_id"] in fish]
    if not used:
        raise DesignError("no individual weights at the end occasion")
    ref = des["reference_diet"]
    k = [d for d in des["diets"] if d != ref][0]
    levels = des["whole_levels"]
    cells = {}
    for u in used:
        cells.setdefault((u["diet_id"], u["whole_level"]), []).extend(fish[u["unit_id"]])
    need = [(d, lv) for d in (k, ref) for lv in levels]
    if any(c not in cells for c in need):
        raise DesignError("a diet x level cell has no fish")
    n_fish = sum(len(v) for v in cells.values())
    df = n_fish - len(cells)
    s2 = sum(float(((np.array(v) - np.mean(v)) ** 2).sum()) for v in cells.values()) / df
    a = len(levels)
    est = sum(np.mean(cells[(k, lv)]) - np.mean(cells[(ref, lv)]) for lv in levels) / a
    var = s2 * sum(1.0 / len(cells[(k, lv)]) + 1.0 / len(cells[(ref, lv)]) for lv in levels) / a ** 2
    naive = _contrast("diet", "%s - %s, fish as replicates" % (k, ref), float(est), var, df, "fish (invalid)", None, alpha)
    naive["n_fish"] = n_fish
    y = {uid: float(np.mean(v)) for uid, v in fish.items()}
    correct = [c for c in stratum_contrasts(used, y, des, alpha) if c["term"] == "diet"][0]
    return {"response": "ln individual weight (g) at the end occasion", "n_fish": n_fish, "n_units": len(used),
            "naive": naive, "correct": correct,
            "se_ratio": correct["se"] / naive["se"] if correct["se"] and naive["se"] else None}


def sensitivity_pending(units, y, des, alpha=0.05):
    """analysis_populations.pending: a pending exclusion stays in every population and a
    sensitivity analysis removes it. Returns the contrasts without those units."""
    removed = sorted(u["unit_id"] for u in units if u.get("exclusion_pending"))
    if not removed:
        return {"removed": [], "contrasts": None, "note": "no pending exclusion"}
    kept = [u for u in units if not u.get("exclusion_pending")]
    try:
        return {"removed": removed, "contrasts": stratum_contrasts(kept, y, des, alpha), "note": None}
    except DesignError as e:
        return {"removed": removed, "contrasts": None, "note": "not estimable without %s: %s" % (", ".join(removed), e)}


# ---------------------------------------------------------------- reproducibility
def _canon(x):
    """JSON-ready copy with floats rounded to 10 significant digits (hashes must not depend on
    the last bits of a floating-point sum) and non-finite numbers as None."""
    if isinstance(x, dict):
        return {str(k): _canon(v) for k, v in x.items()}        # order kept (plan order); hashes sort keys
    if isinstance(x, (list, tuple)):
        return [_canon(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        x = float(x)
        return float("%.10g" % x) if math.isfinite(x) else None
    return x


def _sha(obj):
    return hashlib.sha256(json.dumps(_canon(obj), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_record(con, model, trial_id, results):
    """The fields of an analysis_runs row for this computation (not written to the database).
    input_snapshot_hash: every table dumped in column order, rows sorted; params_hash: the
    validation parameters and the planned outcomes used; output_hash: the results."""
    snap = {}
    for t in sorted(model["tables"]):
        cols = list(model["tables"][t]["columns"])
        rows = con.execute('SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in cols), t)).fetchall()
        snap[t] = sorted(([None if v is None else str(v) for v in r] for r in rows), key=lambda r: [x or "" for x in r])
    params = {"defaults": model.get("validation_params_defaults"),
              "trial": [list(r) for r in con.execute("SELECT * FROM validation_params WHERE trial_id = ? ORDER BY 1, 2",
                                                     (trial_id,))] if "validation_params" in model["tables"] else [],
              "outcomes": {k: v["plan"] for k, v in results["outcomes"].items()}}
    return {"trial_id": trial_id, "plan_id": results["plan_id"], "input_snapshot_hash": _sha(snap),
            "params_hash": _sha(params), "output_hash": _sha({k: v for k, v in results.items() if k != "run_record"})}


# ---------------------------------------------------------------- everything, per planned outcome
EXPORT = ("w_start_g", "w_end_g", "sgr_pct_day", "tgc", "degree_days", "temp_mean_c", "b_start_kg", "b_end_kg",
          "biomass_gain_kg", "feed_given_g", "intake_dm_g", "recovery", "fcr_gross", "fcr_biological",
          "protein_intake_g", "per", "survival_pct", "n_fish_d0", "n_end", "fi_pct_bw_day")


def _back(c, scale):
    """A contrast on the natural scale: log -> ratio, logit -> odds ratio, raw -> difference."""
    kind = {"log": "ratio", "logit": "odds ratio"}.get(scale, "difference")
    f = math.exp if kind != "difference" else (lambda v: v)
    return {"kind": kind, "estimate": f(c["estimate"]),
            "ci_low": f(c["ci_low"]) if c["ci_low"] is not None else None,
            "ci_high": f(c["ci_high"]) if c["ci_high"] is not None else None}


def planned_outcomes(con, trial_id):
    """Planned outcomes of the trial's plan (the latest frozen plan when there are several)."""
    plans = con.execute("SELECT plan_id FROM analysis_plans WHERE trial_id = ? ORDER BY frozen_at IS NULL, frozen_at DESC,"
                        " version DESC", (trial_id,)).fetchall()
    if not plans:
        return None, []
    cur = con.execute("SELECT * FROM planned_outcomes WHERE plan_id = ? ORDER BY CASE role WHEN 'primary' THEN 0"
                      " WHEN 'secondary' THEN 1 ELSE 2 END, hierarchy_rank IS NULL, hierarchy_rank, rowid", (plans[0][0],))
    names = [d[0] for d in cur.description]
    return plans[0][0], [dict(zip(names, r)) for r in cur.fetchall()]


def run_all(con, model, trial_id=None, outcomes=None):
    """Run every planned outcome of the trial and return one JSON-ready dictionary.
    outcomes: planned_outcomes-like dictionaries to analyse instead of the plan (exploratory)."""
    if trial_id is None:
        ids = [r[0] for r in con.execute("SELECT trial_id FROM trials ORDER BY trial_id")]
        if len(ids) != 1:
            raise DesignError("give a trial_id: the database holds %d trials" % len(ids))
        trial_id = ids[0]
    des, units = analysis_dataset(con, trial_id, model)
    plan_id = None
    if outcomes is None:
        plan_id, outcomes = planned_outcomes(con, trial_id)
        if not outcomes:
            raise DesignError("trial %s has no planned_outcomes: analysis.py runs the pre-registered outcomes; pass"
                              " outcomes= for an exploratory run" % trial_id)
    n_by_level = {lv: len({u["system_id"] for u in units if u["whole_level"] == lv}) for lv in des["whole_levels"]}
    res = {"trial_id": trial_id, "plan_id": plan_id, "design": dict(des, n_units=len(units), n_systems_per_level=n_by_level),
           "units": [dict({"unit_id": u["unit_id"], "system_id": u["system_id"], "diet_id": u["diet_id"],
                           "whole_level": u["whole_level"], "exclusion_pending": bool(u.get("exclusion_pending")),
                           "adc_protein_pct": (u.get("adc_pct") or {}).get("protein"),
                           "retention_protein_pct": (u.get("retention_pct") or {}).get("protein")},
                          **{k: u.get(k) for k in EXPORT}) for u in units],
           "outcomes": {}, "multiplicity": [], "notes": []}
    for o in outcomes:
        label, scale = o["outcome_label"], o.get("scale") or "raw"
        alpha = o.get("alpha") if o.get("alpha") is not None else 0.05
        y = {u["unit_id"]: response(u, o["outcome"], scale, o.get("analyte")) for u in units}
        r = {"plan": {k: v for k, v in o.items() if v is not None}, "scale": scale, "alpha": alpha,
             "y": y, "missing_units": sorted(k for k, v in y.items() if v is None)}
        try:
            r["anova"] = split_plot_anova(units, y, des)
            r["variance_components"] = variance_components(r["anova"])
            r["anova_note"] = None
        except DesignError as e:
            r["anova"], r["variance_components"], r["anova_note"] = None, None, str(e)
        r["contrasts"] = stratum_contrasts(units, y, des, alpha)
        for c in r["contrasts"]:
            c["natural"] = _back(c, scale)
        r["degenerate"] = any(c["note"] and "numerically zero" in c["note"] for c in r["contrasts"])
        r["randomisation_diet"] = randomisation_test_diet(units, y, des) if len(des["diets"]) == 2 else None
        r["randomisation_whole"] = (randomisation_test_whole(units, y, des)
                                    if des["whole_factor"] and len(des["whole_levels"]) == 2 else None)
        r["sensitivity_pending"] = sensitivity_pending(units, y, des, alpha)
        for c in r["sensitivity_pending"]["contrasts"] or []:
            c["natural"] = _back(c, scale)
        r["plan_power_check"] = plan_power_check(o, des, min(n_by_level.values()))
        r["cell_means"] = [{"diet_id": d, "whole_level": lv,
                            "n_units": sum(1 for u in units if u["diet_id"] == d and u["whole_level"] == lv
                                           and y[u["unit_id"]] is not None),
                            "mean_y": float(np.mean([y[u["unit_id"]] for u in units if u["diet_id"] == d
                                                     and u["whole_level"] == lv and y[u["unit_id"]] is not None]))}
                           for d in des["diets"] for lv in des["whole_levels"]]
        res["outcomes"][label] = r
    # multiplicity: Holm over the diet main-effect tests of a family that declares it
    fams = {}
    for label, r in res["outcomes"].items():
        fams.setdefault((r["plan"].get("family") or label, r["plan"].get("multiplicity_method") or "none"), []).append(label)
    for (fam, method), labels in fams.items():
        tests = [(lb, c) for lb in labels for c in res["outcomes"][lb]["contrasts"] if c["term"] == "diet" and c["p"] is not None]
        if method == "holm":
            adj = holm([c["p"] for _, c in tests])
        elif method == "none":
            adj = [c["p"] for _, c in tests]
        else:
            res["notes"].append("multiplicity_method %s (family %s) is not implemented: p values are unadjusted" % (method, fam))
            adj = [c["p"] for _, c in tests]
        for (lb, c), pa in zip(tests, adj):
            c["p_adjusted"], c["p_adjust_method"] = pa, method
            res["multiplicity"].append({"family": fam, "method": method, "outcome": lb, "contrast": c["label"],
                                        "p": c["p"], "p_adjusted": pa, "n_tests_in_family": len(tests)})
        for lb in labels:
            if not any(t[0] == lb for t in tests):
                res["notes"].append("outcome %s: no diet test is reported (%s)%s" % (
                    lb, "; ".join(sorted({c["note"] for c in res["outcomes"][lb]["contrasts"] if c["note"]})) or "no contrast",
                    "" if method == "none" else "; it is left out of the %s adjustment of family %s" % (method, fam)))
    try:
        res["fish_level"] = fish_level_naive(con, units, des)
    except DesignError as e:
        res["fish_level"] = None
        res["notes"].append("fish-level demonstration not available: %s" % e)
    res["run_record"] = run_record(con, model, trial_id, res)
    return _canon(res)


ANALYSES = {
    "response": response, "split_plot_anova": split_plot_anova, "stratum_contrasts": stratum_contrasts,
    "variance_components": variance_components, "randomisation_test_diet": randomisation_test_diet,
    "randomisation_test_whole": randomisation_test_whole, "holm": holm, "power_t": power_t, "mdd": mdd,
    "design_power": design_power, "plan_power_check": plan_power_check, "fish_level_naive": fish_level_naive,
    "sensitivity_pending": sensitivity_pending, "run_record": run_record, "run_all": run_all,
}


# ---------------------------------------------------------------- CLI
def _fmt(v, f="%.4g"):
    return "-" if v is None else f % v


def print_report(res, out=sys.stdout):
    d = res["design"]
    out.write("trial %s, plan %s: %d units, diets %s (reference %s), %s levels %s, systems per level %s\n" % (
        res["trial_id"], res["plan_id"], d["n_units"], d["diets"], d["reference_diet"], d["whole_factor"] or "no whole-plot factor",
        d["whole_levels"], d["n_systems_per_level"]))
    for label, r in res["outcomes"].items():
        p = r["plan"]
        out.write("\n== %s  [%s, outcome %s, scale %s]\n   model: %s\n" % (label, p.get("role"), p.get("outcome"), r["scale"], p.get("model")))
        if r["anova"]:
            for row in r["anova"]["rows"]:
                out.write("   %-28s df %2d  MS %-11s F %-9s p %-8s %s\n" % (
                    row["term"], row["df"], _fmt(row["ms"]), _fmt(row["f"]), _fmt(row["p"]),
                    ("against " + row["error_term"]) if row["error_term"] else ""))
        else:
            out.write("   no ANOVA table: %s\n" % r["anova_note"])
        for c in r["contrasts"]:
            n = c["natural"]
            out.write("   %-44s %10s  SE %-9s df %d  p %-8s %s %s [%s, %s]%s\n" % (
                c["label"][:44], _fmt(c["estimate"]), _fmt(c["se"]), c["df"], _fmt(c["p"]), n["kind"], _fmt(n["estimate"]),
                _fmt(n["ci_low"]), _fmt(n["ci_high"]), ("  " + c["note"]) if c["note"] else ""))
        rd = r["randomisation_diet"]
        if rd:
            out.write("   randomisation test, diet: p = %s (%d allocations, smallest possible p %s)\n" % (
                _fmt(rd["p"]), rd["n_allocations"], _fmt(rd["min_p"])))
    for m in res["multiplicity"]:
        if m["method"] != "none":
            out.write("\n%s in family %s: %-20s p %-8s adjusted %s" % (m["method"], m["family"], m["outcome"], _fmt(m["p"]), _fmt(m["p_adjusted"])))
    fl = res.get("fish_level")
    if fl:
        out.write("\n\nfish as replicates (invalid): p %s with df %d; unit as replicate: p %s with df %d; SE ratio %s\n" % (
            _fmt(fl["naive"]["p"]), fl["naive"]["df"], _fmt(fl["correct"]["p"]), fl["correct"]["df"], _fmt(fl["se_ratio"])))
    for n in res["notes"]:
        out.write("note: %s\n" % n)
    rr = res["run_record"]
    out.write("run record: input %s  params %s  output %s\n" % (rr["input_snapshot_hash"][:12], rr["params_hash"][:12], rr["output_hash"][:12]))


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    json_out = argv[argv.index("--json") + 1] if "--json" in argv else None
    if json_out in args:
        args.remove(json_out)
    yaml_path = args[0] if args else DEFAULT_YAML
    if len(args) > 1:
        from build_db import load_model
        model, con = load_model(yaml_path), sqlite3.connect(args[1])
    else:
        from synthetic_trial import make_trial
        con, model, _ = make_trial(yaml_path)
    res = run_all(con, model)
    print_report(res)
    if json_out:
        with open(json_out, "w") as fh:
            json.dump(res, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
