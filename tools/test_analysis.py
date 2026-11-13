#!/usr/bin/env python3
"""Tests of tools/analysis.py: every analysis is checked against something it did not compute.

  truth        the synthetic trial is generated from known effects (synthetic_trial.DIETS); over
               repeated seeds the estimates must be unbiased and the confidence intervals must
               cover the truth as often as they claim; per seed, an estimate from the measured
               data must equal the same estimate from the generator's truth values
  independent  the same quantity by another route: base R aov with Error(system) and a REML mixed
               model (stored in tools/reference/analysis_reference.json, see make_reference.py),
               lme4/lmerTest with Satterthwaite and Kenward-Roger df (lmer_reference.json, see
               make_reference_lmer.R), a least-squares fit, brute-force relabelling, a paired t
               test, Monte-Carlo draws
  null         with no effect in the data a test must reject at its nominal rate
  invalid      fish as replicates must be shown to reject far too often on the same data

Writes .coverage_analyses.json: tools/run_all_tests.py fails if an analysis has no test.
Usage: test_analysis.py [data_model.yaml] [--seeds N]   (default 120 seeds per scenario)
"""
import copy
import itertools
import json
import math
import os
import sys

import numpy as np
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analysis as A  # noqa: E402
import synthetic_trial as ST  # noqa: E402

YAML = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else os.path.join(os.path.dirname(HERE), "data_model.yaml")
N_SEEDS = int(sys.argv[sys.argv.index("--seeds") + 1]) if "--seeds" in sys.argv else 120
REFERENCE = os.path.join(HERE, "reference", "analysis_reference.json")
REFERENCE_LMER = os.path.join(HERE, "reference", "lmer_reference.json")
TRUTH_KEYS = {"growth_sgr": lambda t: t["sgr_true_pct"], "intake_log": lambda t: math.log(t["feed_dm_eaten_g"]),
              "gain_log": lambda t: math.log(t["gain_kg"]), "feed_per_gain_log": lambda t: math.log(t["fcr_bio"]),
              "survival_logit": lambda t: math.log((t["n_d0"] - t["deaths"] + 0.5) / (t["deaths"] + 0.5))}

GENERATOR = (copy.deepcopy(ST.DIETS), dict(ST.TRUE_FCR_DM))      # the generator's effects as shipped
FAILS, EXERCISED, N_CHECKS, CHECKS = [], set(), [0], []


def check(names, text, ok, detail=""):
    """One line per check; names = the ANALYSES keys this check exercises."""
    N_CHECKS[0] += 1
    EXERCISED.update([names] if isinstance(names, str) else names)
    print("%s %-24s %s%s" % ("ok  " if ok else "FAIL", names if isinstance(names, str) else names[0], text,
                              (" [%s]" % detail) if detail else ""))
    CHECKS.append({"analyses": [names] if isinstance(names, str) else list(names), "text": text, "ok": bool(ok), "detail": detail})
    if not ok:
        FAILS.append("%s: %s [%s]" % (names, text, detail))


def close(a, b, rel=1e-9, abs_=0.0):
    return a is not None and b is not None and abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


# ---------------------------------------------------------------- hand-built designs
def make_design(n_sys=2, levels=("L1", "L2"), diets=("D1", "D2")):
    """A design dictionary and unit list without a database: n_sys systems per level."""
    whole = len(levels) > 1
    des = {"trial_id": "X", "reference_diet": diets[0], "diets": list(diets), "whole_factor": "temperature" if whole else None,
           "whole_levels": list(levels) if whole else ["all"], "reference_level": levels[0] if whole else "all",
           "system_level": {}}
    units = []
    for i, lv in enumerate(des["whole_levels"]):
        for j in range(n_sys):
            s = "S%d_%d" % (i, j)
            des["system_level"][s] = lv
            for d in diets:
                units.append({"unit_id": "%s_%s" % (s, d), "system_id": s, "diet_id": d, "whole_level": lv})
    return des, units


def draw(rng, des, units, sd_unit=1.0, sd_sys=0.0, effects=None):
    """Normal responses: system effect + unit error + optional {(diet, level): mean}."""
    se = {s: rng.normal(0, sd_sys) if sd_sys else 0.0 for s in des["system_level"]}
    return {u["unit_id"]: se[u["system_id"]] + rng.normal(0, sd_unit) + (effects or {}).get((u["diet_id"], u["whole_level"]), 0.0)
            for u in units}


def by_term(contrasts, term):
    return [c for c in contrasts if c["term"] == term][0]


def ols_route(units, y, des):
    """Independent route to the stratum F tests by least squares (no sums-of-squares formulas).
    Unit stratum: y ~ system dummies + diet + diet x level with +-1 coding, F from the increase
    of the residual sum of squares when a column is dropped. System stratum: system means ~ level."""
    lv0, d0 = des["whole_levels"][0], des["diets"][0]
    sys_ids = sorted(des["system_level"])
    yv = np.array([y[u["unit_id"]] for u in units])
    S = np.array([[1.0 if u["system_id"] == s else 0.0 for s in sys_ids] for u in units])
    dz = np.array([1.0 if u["diet_id"] != d0 else -1.0 for u in units])
    tz = np.array([1.0 if u["whole_level"] != lv0 else -1.0 for u in units])

    def rss(X):
        beta = np.linalg.lstsq(X, yv, rcond=None)[0]
        return float(((yv - X @ beta) ** 2).sum()), X.shape[0] - np.linalg.matrix_rank(X)

    full, df = rss(np.column_stack([S, dz, dz * tz]))
    f_diet = (rss(np.column_stack([S, dz * tz]))[0] - full) / (full / df)
    f_int = (rss(np.column_stack([S, dz]))[0] - full) / (full / df)
    m = np.array([np.mean([y[u["unit_id"]] for u in units if u["system_id"] == s]) for s in sys_ids])
    g = np.array([1.0 if des["system_level"][s] != lv0 else 0.0 for s in sys_ids])
    slope, _, _, p_whole, _ = stats.linregress(g, m)
    return {"f_diet": f_diet, "f_int": f_int, "df": df, "p_diet": float(stats.f.sf(f_diet, 1, df)),
            "p_int": float(stats.f.sf(f_int, 1, df)), "p_whole": float(p_whole), "whole_est": float(slope)}


# ---------------------------------------------------------------- one synthetic trial per seed
def seed_job(job):
    """(seed, null) -> estimates from the measured data and from the generator's truth.
    null = True: both diets get the growth and feed efficiency of D1 (no diet effect at all)."""
    seed, null = job
    ST.DIETS, ST.TRUE_FCR_DM = copy.deepcopy(GENERATOR[0]), dict(GENERATOR[1])     # worker processes are reused
    if null:
        ST.DIETS["D2"]["sgr"] = dict(ST.DIETS["D1"]["sgr"])
        ST.TRUE_FCR_DM["D2"] = ST.TRUE_FCR_DM["D1"]
    con, model, truth = ST.make_trial(YAML, seed=seed)
    res = A.run_all(con, model)
    des, units = A.analysis_dataset(con, "T1", model)
    out = {"seed": seed, "null": null, "outcomes": {}, "truth": {}}
    for label, r in res["outcomes"].items():
        out["outcomes"][label] = {c["term"]: {k: c[k] for k in ("estimate", "se", "p", "ci_low", "ci_high")} for c in r["contrasts"]}
        out["outcomes"][label]["_vc"] = r["variance_components"]
        out["outcomes"][label]["_rand_p"] = r["randomisation_diet"]["p"]
        if label in TRUTH_KEYS:
            yt = {u: TRUTH_KEYS[label](t) for u, t in truth.items()}
            out["truth"][label] = {c["term"]: c["estimate"] for c in A.stratum_contrasts(units, yt, des)}
    fl = res["fish_level"]
    out["fish"] = {"naive_p": fl["naive"]["p"], "correct_p": fl["correct"]["p"], "se_ratio": fl["se_ratio"]}
    return out


def run_seeds(jobs):
    """Run the jobs in worker processes of this script (as tools/test_thresholds.py does), each
    writing its results to a file; a failing worker fails the test run."""
    import subprocess
    import tempfile
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    chunks = [jobs[i::workers] for i in range(workers)]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        procs = []
        for i, ch in enumerate(c for c in chunks if c):
            path = os.path.join(tmp, "w%d.json" % i)
            procs.append((subprocess.Popen([sys.executable, os.path.abspath(__file__), YAML, "--worker", json.dumps(ch), path],
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True), path))
        for p, path in procs:
            log, _ = p.communicate()
            if p.returncode:
                raise SystemExit("seed worker failed:\n" + log[-3000:])
            with open(path) as fh:
                out.extend(json.load(fh))
    return sorted(out, key=lambda r: (r["null"], r["seed"]))


def worker(jobs_json, path):
    with open(path, "w") as fh:
        json.dump([seed_job(tuple(j)) for j in json.loads(jobs_json)], fh)
    return 0


def binom_hi(n, p, q=0.999):
    return int(stats.binom.ppf(q, n, p))


def binom_lo(n, p, q=0.001):
    return int(stats.binom.ppf(q, n, p))


# ---------------------------------------------------------------- checks
def test_algebra(rng):
    des, units = make_design(2)
    y = draw(rng, des, units, 1.0, 0.7, {("D2", "L1"): 0.8, ("D2", "L2"): 1.5, ("D1", "L2"): 2.0})
    an = A.split_plot_anova(units, y, des)
    rows = {r["term"]: r for r in an["rows"]}
    ols = ols_route(units, y, des)
    check("split_plot_anova", "2 x 2 design: F of diet and interaction equal the least-squares route",
          close(rows["diet"]["f"], ols["f_diet"], 1e-9) and close(rows["diet x temperature"]["f"], ols["f_int"], 1e-9)
          and rows["diet"]["error_df"] == ols["df"] == 2, "F %.6f vs %.6f" % (rows["diet"]["f"], ols["f_diet"]))
    check("split_plot_anova", "whole-plot factor is tested against systems: p equals a regression of the 4 system means",
          close(rows["temperature"]["p"], ols["p_whole"], 1e-9) and rows["temperature"]["error_df"] == 2,
          "p %.6f vs %.6f" % (rows["temperature"]["p"], ols["p_whole"]))
    check("split_plot_anova", "sums of squares add up to the total and degrees of freedom to N - 1",
          close(sum(r["ss"] for r in an["rows"]), float(((np.array(list(y.values())) - np.mean(list(y.values()))) ** 2).sum()), 1e-9)
          and sum(r["df"] for r in an["rows"]) == len(units) - 1)
    cs = A.stratum_contrasts(units, y, des)
    ok = all(close(by_term(cs, t)["t"] ** 2, rows[t]["f"], 1e-9) and close(by_term(cs, t)["p"], rows[t]["p"], 1e-9)
             for t in ("diet", "diet x temperature", "temperature"))
    check("stratum_contrasts", "balanced design: t^2 and p of each contrast equal the ANOVA F and p", ok)
    cell = lambda d, lv: np.mean([y[u["unit_id"]] for u in units if u["diet_id"] == d and u["whole_level"] == lv])  # noqa: E731
    main = ((cell("D2", "L1") - cell("D1", "L1")) + (cell("D2", "L2") - cell("D1", "L2"))) / 2
    inter = (cell("D2", "L2") - cell("D1", "L2")) - (cell("D2", "L1") - cell("D1", "L1"))
    whole = (cell("D1", "L2") + cell("D2", "L2")) / 2 - (cell("D1", "L1") + cell("D2", "L1")) / 2
    check("stratum_contrasts", "estimates equal the differences of cell means computed directly",
          close(by_term(cs, "diet")["estimate"], main, 1e-12) and close(by_term(cs, "diet x temperature")["estimate"], inter, 1e-12)
          and close(by_term(cs, "temperature")["estimate"], whole, 1e-12))
    c = by_term(cs, "diet")
    check("stratum_contrasts", "the confidence interval is estimate +- t(0.975, df) x SE",
          close(c["ci_high"] - c["estimate"], stats.t.ppf(0.975, c["df"]) * c["se"], 1e-12))
    # 3 levels x 3 diets, 3 systems per level: general formulas against least squares on dummies
    des3, units3 = make_design(3, ("L1", "L2", "L3"), ("D1", "D2", "D3"))
    y3 = draw(rng, des3, units3, 1.0, 0.5, {("D3", "L3"): 2.0, ("D2", "L1"): 1.0})
    an3 = {r["term"]: r for r in A.split_plot_anova(units3, y3, des3)["rows"]}
    yv = np.array([y3[u["unit_id"]] for u in units3])
    dummies = lambda key, vals: np.array([[1.0 if u[key] == v else 0.0 for v in vals] for u in units3])  # noqa: E731
    S, D = dummies("system_id", sorted(des3["system_level"])), dummies("diet_id", des3["diets"][1:])
    L = dummies("whole_level", des3["whole_levels"][1:])
    DL = np.column_stack([D[:, i] * L[:, j] for i in range(2) for j in range(2)])
    rss = lambda X: float(((yv - X @ np.linalg.lstsq(X, yv, rcond=None)[0]) ** 2).sum())  # noqa: E731
    full = rss(np.column_stack([S, D, DL]))
    f_int = ((rss(np.column_stack([S, D])) - full) / 4) / (full / 12)
    check("split_plot_anova", "3 levels x 3 diets x 3 systems: interaction F (4, 12 df) equals the least-squares route",
          close(an3["diet x temperature"]["f"], f_int, 1e-9) and an3["diet x temperature"]["error_df"] == 12
          and an3["temperature"]["error_df"] == 6, "F %.6f vs %.6f" % (an3["diet x temperature"]["f"], f_int))
    # no whole-plot factor: randomised blocks; two diets -> paired t test
    desb, unitsb = make_design(6, ("only",))
    yb = draw(rng, desb, unitsb, 1.0, 2.0, {("D2", "all"): 1.0})
    cb = by_term(A.stratum_contrasts(unitsb, yb, desb), "diet")
    sysb = sorted(desb["system_level"])
    tt = stats.ttest_rel([yb[s + "_D2"] for s in sysb], [yb[s + "_D1"] for s in sysb])
    fb = [r for r in A.split_plot_anova(unitsb, yb, desb)["rows"] if r["term"] == "diet"][0]
    check("stratum_contrasts", "no whole-plot factor (blocks only): the diet contrast is the paired t test, df 5",
          close(cb["t"], float(tt.statistic), 1e-10) and close(cb["p"], float(tt.pvalue), 1e-10) and cb["df"] == 5
          and close(fb["f"], cb["t"] ** 2, 1e-9))
    # design errors are raised, not silently analysed
    des1, units1 = make_design(1)
    y1 = draw(rng, des1, units1)
    try:
        A.split_plot_anova(units1, y1, des1)
        ok = False
    except A.DesignError as e:
        ok = "no degrees of freedom" in str(e)
    c1 = by_term(A.stratum_contrasts(units1, y1, des1), "diet")
    check(["split_plot_anova", "stratum_contrasts"], "one system per level: ANOVA refused; contrast gives an estimate without SE or p",
          ok and c1["p"] is None and c1["se"] is None and c1["estimate"] is not None and "no error degrees" in c1["note"])
    ymiss = dict(y)
    ymiss[units[0]["unit_id"]] = None
    try:
        A.split_plot_anova(units, ymiss, des)
        ok = False
    except A.DesignError as e:
        ok = "without every diet" in str(e)
    cm = A.stratum_contrasts(units, ymiss, des)
    kept = [s for s in sorted(des["system_level"]) if s != units[0]["system_id"]]
    d = {s: ymiss[s + "_D2"] - ymiss[s + "_D1"] for s in kept}
    l1 = [d[s] for s in kept if des["system_level"][s] == "L1"]
    l2 = [d[s] for s in kept if des["system_level"][s] == "L2"]
    check(["split_plot_anova", "stratum_contrasts"], "a missing unit: ANOVA refused; contrasts use the 3 complete systems with df 1",
          ok and by_term(cm, "diet")["df"] == 1 and by_term(cm, "diet")["n_systems"] == 3
          and close(by_term(cm, "diet")["estimate"], (np.mean(l1) + np.mean(l2)) / 2, 1e-12))
    # degenerate error variance
    yc = {u["unit_id"]: 88.0 + (2.0 if u["diet_id"] == "D2" else 0.0) + 1e-8 * rng.normal() for u in units}
    anc = A.split_plot_anova(units, yc, des)
    cc = by_term(A.stratum_contrasts(units, yc, des), "diet")
    check(["split_plot_anova", "stratum_contrasts"], "residual of rounding size only (1e-8 on 88): F and p withheld, estimate kept",
          anc["degenerate"] and all(r["f"] is None for r in anc["rows"]) and cc["p"] is None and close(cc["estimate"], 2.0, 1e-6)
          and "numerically zero" in cc["note"])


def test_null_and_power(rng):
    des, units = make_design(2)
    n = 10000
    rej = {"diet": 0, "diet x temperature": 0, "temperature": 0}
    rand_small, est = 0, []
    for _ in range(n):
        y = draw(rng, des, units, 1.0, 0.8)
        cs = A.stratum_contrasts(units, y, des)
        for t in rej:
            rej[t] += by_term(cs, t)["p"] < 0.05
        est.append(by_term(cs, "diet")["estimate"])
        rand_small += A.randomisation_test_diet(units, y, des)["p"] <= 0.125 + 1e-12
    tol = 0.008
    check("stratum_contrasts", "no effect, normal data with a system variance (10,000 trials): each test rejects at 5 %",
          all(abs(v / n - 0.05) <= tol for v in rej.values()), ", ".join("%s %.4f" % (k, v / n) for k, v in rej.items()))
    check("randomisation_test_diet", "no effect (10,000 trials): P(p <= 0.125) = 0.125, the only level below 0.25 that 4 systems allow",
          abs(rand_small / n - 0.125) <= 0.012, "%.4f" % (rand_small / n))
    se, df = A.design_se("diet", 1.0, 0.8, 2)
    check("design_power", "SE formula of the diet main effect equals the SD of 10,000 simulated estimates",
          abs(np.std(est) / se - 1) < 0.03 and df == 2, "formula %.4f, simulated %.4f" % (se, np.std(est)))
    # power: delta = 0 gives alpha; other routes; Monte Carlo
    check("power_t", "power at no difference equals alpha for df 1, 2, 6, 30",
          all(abs(A.power_t(0.0, 1.0, d, 0.05) - 0.05) < 1e-9 for d in (1, 2, 6, 30)))
    worst, n_cmp = 0.0, 0
    for d in (1, 2, 4, 10):
        tc = stats.t.ppf(0.975, d)
        for ncp in (0.5, 1, 2, 3, 5, 8, 12):
            ref = stats.nct.sf(tc, d, ncp) + stats.nct.sf(tc, d, -ncp)
            if np.isfinite(ref):
                n_cmp += 1
                worst = max(worst, abs(A.power_t(ncp, 1.0, d) - ref))
    check("power_t", "equals the non-central t tail probabilities of scipy on %d (df, ncp) points" % n_cmp, worst < 1e-8 and n_cmp >= 20,
          "largest difference %.1e" % worst)
    z, v = rng.normal(size=400000), rng.chisquare(2, size=400000)
    mc = float(np.mean(np.abs((z + 3.0) / np.sqrt(v / 2)) > stats.t.ppf(0.975, 2)))
    check("power_t", "equals a Monte-Carlo estimate (400,000 draws, df 2, ncp 3)", abs(A.power_t(3.0, 1.0, 2) - mc) < 0.004,
          "%.4f vs %.4f" % (A.power_t(3.0, 1.0, 2), mc))
    pw = [A.power_t(x, 1.0, 2) for x in np.linspace(0, 12, 25)]
    check("power_t", "increases with the true difference and with the error df",
          all(b > a for a, b in zip(pw, pw[1:])) and A.power_t(3, 1, 2) < A.power_t(3, 1, 4) < A.power_t(3, 1, 20))
    m = A.mdd(0.5, 2, 0.05, 0.80)
    check("mdd", "the power at the minimum detectable difference is the requested 0.80; scales with SE",
          abs(A.power_t(m, 0.5, 2) - 0.80) < 1e-8 and close(A.mdd(1.0, 2), 2 * m, 1e-8), "mdd/SE = %.4f at df 2" % (m / 0.5))
    # design_power against simulation of the design with a true effect
    eff = {("D2", "L1"): 2.0, ("D2", "L2"): 2.0}
    desk, unitsk = make_design(3)
    hits, n2 = 0, 4000
    for _ in range(n2):
        hits += by_term(A.stratum_contrasts(unitsk, draw(rng, desk, unitsk, 1.0, 0.5, eff), desk), "diet")["p"] < 0.05
    dp = A.design_power(2.0, "diet", 1.0, 0.5, 3)
    check("design_power", "diet effect 2 SD, 3 systems per level: formula power equals the rejection rate of 4,000 simulated trials",
          abs(dp["power"] - hits / n2) < 0.025 and dp["df"] == 4, "formula %.3f, simulated %.3f" % (dp["power"], hits / n2))
    dw = A.design_power(1.0, "whole", 1.0, 0.5, 4)
    check("design_power", "whole-plot SE uses the system variance plus the unit variance over the diets; more systems give more power",
          close(dw["se"], math.sqrt(2 * (0.25 + 0.5) / 4), 1e-12) and dw["df"] == 6
          and A.design_power(1.0, "whole", 1.0, 0.5, 8)["power"] > dw["power"]
          and close(A.design_se("interaction", 1.0, 0.0, 2)[0], 2 * A.design_se("diet", 1.0, 0.0, 2)[0], 1e-12))
    # variance components
    des4, units4 = make_design(4)
    s2u, s2s, trunc = [], [], 0
    for _ in range(3000):
        vc = A.variance_components(A.split_plot_anova(units4, draw(rng, des4, units4, 1.0, 1.5), des4))
        s2u.append(vc["sigma2_unit"])
        s2s.append(vc["sigma2_system_raw"])
        trunc += vc["system_component_truncated"]
    check("variance_components", "simulated sigma2_unit = 1 and sigma2_system = 2.25 are recovered without bias (3,000 trials)",
          abs(np.mean(s2u) - 1.0) < 0.05 and abs(np.mean(s2s) - 2.25) < 0.15, "means %.3f and %.3f" % (np.mean(s2u), np.mean(s2s)))
    vc0 = A.variance_components({"ms_system": 1.0, "ms_unit": 3.0, "b": 2, "df_unit": 2, "df_system": 2})
    check("variance_components", "MS(system) below MS(unit): raw estimate -1 reported, truncated to 0 for the ICC and flagged",
          vc0["sigma2_system_raw"] == -1.0 and vc0["sigma2_system"] == 0.0 and vc0["icc_system"] == 0.0
          and vc0["system_component_truncated"] and vc0["sd_unit_ci95"][0] < math.sqrt(3) < vc0["sd_unit_ci95"][1])


def test_randomisation_and_holm(rng):
    des, units = make_design(2)
    y = draw(rng, des, units, 1.0, 0.5, {("D2", "L1"): 1.0, ("D2", "L2"): 1.0})
    rt = A.randomisation_test_diet(units, y, des)
    obs = by_term(A.stratum_contrasts(units, y, des), "diet")["estimate"]
    sys_ids = sorted(des["system_level"])
    count = 0
    for flips in itertools.product((False, True), repeat=4):          # relabel the units and re-estimate
        y2 = dict(y)
        for s, fl in zip(sys_ids, flips):
            if fl:
                y2[s + "_D1"], y2[s + "_D2"] = y[s + "_D2"], y[s + "_D1"]
        count += abs(by_term(A.stratum_contrasts(units, y2, des), "diet")["estimate"]) >= abs(obs) - 1e-12
    check("randomisation_test_diet", "p equals a brute-force count over the 16 relabellings re-estimated with stratum_contrasts",
          rt["n_allocations"] == 16 and close(rt["p"], count / 16, 1e-12) and close(rt["statistic"], obs, 1e-12)
          and rt["min_p"] == 0.125, "p = %d/16" % count)
    yall = {u["unit_id"]: (1.0 if u["diet_id"] == "D2" else 0.0) + 0.01 * i for i, u in enumerate(units)}
    check("randomisation_test_diet", "every system favours the same diet: p is the smallest possible value 2/16",
          A.randomisation_test_diet(units, yall, des)["p"] == 0.125)
    des6, units6 = make_design(3)
    check("randomisation_test_diet", "6 systems give 64 allocations and a smallest p of 2/64",
          A.randomisation_test_diet(units6, draw(rng, des6, units6), des6)["min_p"] == 2 / 64)
    rw = A.randomisation_test_whole(units, y, des)
    obs_w = by_term(A.stratum_contrasts(units, y, des), "temperature")["estimate"]
    count, n_alloc = 0, 0
    for comb in itertools.combinations(sys_ids, 2):                      # which 2 systems get level L2
        des2 = dict(des, system_level={s: ("L2" if s in comb else "L1") for s in sys_ids})
        units2 = [dict(u, whole_level=des2["system_level"][u["system_id"]]) for u in units]
        n_alloc += 1
        count += abs(by_term(A.stratum_contrasts(units2, y, des2), "temperature")["estimate"]) >= abs(obs_w) - 1e-12
    check("randomisation_test_whole", "p equals a brute-force count over the 6 ways to give 2 of 4 systems the level",
          rw["n_allocations"] == n_alloc == 6 and close(rw["p"], count / 6, 1e-12) and close(rw["min_p"], 2 / 6, 1e-12),
          "p = %d/6: with 2 systems per level this test can never reach 0.05" % count)
    # Holm
    p = [0.01, 0.04, 0.03, 0.005]
    check("holm", "worked example: 0.01, 0.04, 0.03, 0.005 -> 0.03, 0.06, 0.06, 0.02",
          all(close(a, b, 1e-12) for a, b in zip(A.holm(p), [0.03, 0.06, 0.06, 0.02])))
    ok = True
    for _ in range(200):
        pv = list(rng.uniform(0, 1, size=int(rng.integers(1, 8))))
        adj = A.holm(pv)
        srt = sorted(pv)
        ref = [min(1.0, max((len(pv) - j) * srt[j] for j in range(srt.index(x) + 1))) for x in pv]   # definition, O(m^2)
        ok &= all(close(a, b, 1e-12) for a, b in zip(adj, ref)) and all(a >= x - 1e-15 for a, x in zip(adj, pv)) and max(adj) <= 1
        ok &= [i for i, _ in sorted(enumerate(pv), key=lambda t: t[1])] == [i for i, _ in sorted(enumerate(adj), key=lambda t: (t[1], pv[t[0]]))]
    check("holm", "200 random families: equals the definition, never below the raw p, never above 1, order kept; one test is unchanged",
          ok and A.holm([0.2]) == [0.2])


def test_trial():
    con, model, truth = ST.make_trial(YAML, seed=7)
    res = A.run_all(con, model)
    des, units = A.analysis_dataset(con, "T1", model)
    u1 = [u for u in units if u["unit_id"] == "U1D1"][0]
    dead = u1["n_fish_d0"] * (1 - u1["survival_pct"] / 100)
    check("response", "raw = the metric, log = ln(metric), logit = ln((alive + 0.5) / (dead + 0.5)) from the d0 count",
          A.response(u1, "sgr_pct_day", "raw") == u1["sgr_pct_day"]
          and close(A.response(u1, "intake_dm_g", "log"), math.log(u1["intake_dm_g"]), 1e-15)
          and close(A.response(u1, "survival_pct", "logit"), math.log((u1["n_fish_d0"] - dead + 0.5) / (dead + 0.5)), 1e-12)
          and A.response(u1, "adc_pct", "raw", "protein") == u1["adc_pct"]["protein"])
    full = dict(u1, survival_pct=100.0)
    ok = close(A.response(full, "survival_pct", "logit"), math.log((u1["n_fish_d0"] + 0.5) / 0.5), 1e-12)
    ok &= A.response(dict(u1, intake_dm_g=None), "intake_dm_g", "log") is None
    for bad in (lambda: A.response(dict(u1, intake_dm_g=0.0), "intake_dm_g", "log"), lambda: A.response(u1, "sgr_pct_day", "sqrt"),
                lambda: A.response(u1, "sgr_pct_day", "logit")):
        try:
            bad()
            ok = False
        except A.DesignError:
            pass
    check("response", "a unit without deaths stays finite; a missing value stays missing; log of 0, unknown scale and logit of a non-survival metric are refused", ok)

    # ---- independent implementations: R aov with Error strata and a REML mixed model, stored reference
    if not os.path.exists(REFERENCE):
        check(["split_plot_anova", "variance_components"], "reference file tools/reference/analysis_reference.json exists", False, REFERENCE)
    else:
        with open(REFERENCE) as fh:
            ref = json.load(fh)
        order = ref["inputs"]["unit"]
        stale = [lb for lb in ref["aov"] if not all(close(res["outcomes"][lb]["y"][u], v, 0, 1e-9)
                                                    for u, v in zip(order, ref["inputs"][lb]))]
        check("run_all", "the reference was computed from the same unit values as this trial (seed 7)", not stale,
              "stale for %s: rerun tools/reference/make_reference.R" % stale if stale else "%d outcomes x 8 units" % len(ref["aov"]))
        worst, n_cmp = 0.0, 0
        for lb, rr in ref["aov"].items():
            rows = {r["term"]: r for r in res["outcomes"][lb]["anova"]["rows"]}
            pairs = [("temperature", "temperature"), ("diet", "diet"), ("diet x temperature", "interaction")]
            for mine, theirs in pairs:
                for k in ("f", "p", "ms"):
                    n_cmp += 1
                    worst = max(worst, abs(rows[mine][k] / rr[theirs][k] - 1))
            for mine, theirs in (("system within temperature", "system"), ("unit residual", "residual")):
                n_cmp += 1
                worst = max(worst, abs(rows[mine]["ms"] / rr[theirs]["ms"] - 1))
                worst = max(worst, abs(rows[mine]["df"] - rr[theirs]["df"]))
        check("split_plot_anova", "equals R aov(y ~ diet*temperature + Error(system)): F, p and mean squares of %d outcomes" % len(ref["aov"]),
              worst < 1e-7, "%d values, largest relative difference %.1e (R %s)" % (n_cmp, worst, ref["made_with"]["R"]))
        agree, boundary, worst = [], [], 0.0
        for lb, rr in ref["reml"].items():
            r = res["outcomes"][lb]
            if rr["var_system"] < 1e-6 * rr["var_residual"]:       # REML estimate on the boundary: system variance 0
                boundary.append(lb)
                continue
            agree.append(lb)
            cs = {c["term"]: c for c in r["contrasts"]}
            for mine, theirs in (("temperature", "temperature"), ("diet", "diet"), ("diet x temperature", "interaction")):
                worst = max(worst, abs(cs[mine]["se"] / rr[theirs]["se"] - 1),
                            abs(cs[mine]["estimate"] - rr[theirs]["estimate"]) / cs[mine]["se"])
            worst = max(worst, abs(r["variance_components"]["sigma2_unit"] / rr["var_residual"] - 1),
                        abs(r["variance_components"]["sigma2_system"] / rr["var_system"] - 1))
        check(["stratum_contrasts", "variance_components"],
              "equals a REML mixed model (statsmodels) where its system variance is positive: estimates, SEs, both variance components",
              len(agree) >= 1 and worst < 1e-3, "%s; largest relative difference %.1e (statsmodels %s)" % (
                  ", ".join(agree), worst, ref["made_with"]["statsmodels"]))
        flagged = [lb for lb in ref["reml"] if res["outcomes"][lb]["variance_components"]["system_component_truncated"]]
        check("variance_components", "the outcomes where REML puts the system variance at 0 are exactly those flagged as truncated here",
              sorted(flagged) == sorted(boundary), "on the boundary: %s" % (", ".join(boundary) or "none"))

    # ---- independent implementation of the small-sample tests: lme4 / lmerTest (make_reference_lmer.R)
    if not os.path.exists(REFERENCE_LMER):
        check(["stratum_contrasts", "variance_components"], "reference file tools/reference/lmer_reference.json exists", False, REFERENCE_LMER)
    else:
        with open(REFERENCE_LMER) as fh:
            lref = json.load(fh)
        order = lref["inputs"]["unit"]
        stale = [lb for lb in lref["lmer"] if not all(close(res["outcomes"][lb]["y"][u], v, 0, 1e-9)
                                                      for u, v in zip(order, lref["inputs"][lb]))]
        agree, singular, worst, n_cmp = [], [], {"estimate": 0.0, "se": 0.0, "df": 0.0, "p": 0.0}, 0
        for lb, rr in lref["lmer"].items():
            if rr["singular"]:
                singular.append(lb)
                continue
            agree.append(lb)
            cs = {c["term"]: c for c in res["outcomes"][lb]["contrasts"]}
            for ddf in ("satterthwaite", "kenward_roger"):
                for mine, theirs in (("diet", "diet"), ("temperature", "temperature"), ("diet x temperature", "interaction")):
                    a, b = cs[mine], rr[ddf][theirs]
                    n_cmp += 4
                    worst["estimate"] = max(worst["estimate"], abs(a["estimate"] - b["estimate"]) / a["se"])
                    worst["se"] = max(worst["se"], abs(a["se"] / b["se"] - 1))
                    worst["df"] = max(worst["df"], abs(a["df"] - b["df"]))
                    worst["p"] = max(worst["p"], abs(a["p"] / b["p"] - 1))
        check("stratum_contrasts",
              "equals lme4/lmerTest with Satterthwaite and with Kenward-Roger df where the system variance is positive: "
              "estimate, SE, df and p of the diet, temperature and interaction contrasts",
              not stale and len(agree) >= 1 and worst["estimate"] < 1e-6 and worst["se"] < 1e-4 and worst["df"] < 1e-3 and worst["p"] < 1e-3,
              ("stale for %s: rerun tools/reference/make_reference_lmer.R" % stale) if stale else
              "%s; %d values, largest difference: estimate %.1e SE, SE %.1e, df %.1e, p %.1e relative (lme4 %s, lmerTest %s)" % (
                  ", ".join(agree), n_cmp, worst["estimate"], worst["se"], worst["df"], worst["p"],
                  lref["made_with"]["lme4"], lref["made_with"]["lmerTest"]))
        flagged = [lb for lb in lref["lmer"] if res["outcomes"][lb]["variance_components"]["system_component_truncated"]]
        pooled = [(lb, lref["lmer"][lb]["satterthwaite"]["diet"], {c["term"]: c for c in res["outcomes"][lb]["contrasts"]}["diet"])
                  for lb in singular]
        check("variance_components",
              "lmer fits are singular (system variance 0) exactly where it is flagged as truncated here; there lmerTest pools "
              "both error strata (more df than the stratum test), so its tests are not the planned stratum tests",
              sorted(flagged) == sorted(singular) and all(theirs["df"] > mine["df"] + 0.5 for _, theirs, mine in pooled),
              "; ".join("%s: lmerTest Satterthwaite df %.3g, p %.3g; stratum test df %d, p %.3g" % (
                  lb, theirs["df"], theirs["p"], mine["df"], mine["p"]) for lb, theirs, mine in pooled) or "none singular")

    # ---- run_all on the trial
    labels = list(res["outcomes"])
    check("run_all", "all six planned outcomes are analysed in plan order, the primary first; the result survives a JSON round trip",
          labels == [o["outcome_label"] for o in ST.PLAN_OUTCOMES] and labels[0] == "growth_sgr"
          and json.loads(json.dumps(res)) == res and res["plan_id"] == "P1")
    ok = all(c["natural"]["kind"] == {"log": "ratio", "logit": "odds ratio"}.get(r["scale"], "difference")
             and (r["scale"] == "raw" or close(c["natural"]["estimate"], math.exp(c["estimate"]), 1e-9))
             for r in res["outcomes"].values() for c in r["contrasts"])
    check("run_all", "every contrast is also given on the natural scale: difference (raw), ratio (log), odds ratio (logit)", ok)
    fam = [m for m in res["multiplicity"] if m["family"] == "secondary"]
    check(["run_all", "holm"], "the four secondary outcomes form one Holm family; adjusted p values equal holm() of the raw ones",
          len(fam) == 4 and all(close(a, m["p_adjusted"], 1e-9) for a, m in zip(A.holm([m["p"] for m in fam]), fam))
          and all(m["n_tests_in_family"] == 4 for m in fam))
    adc = res["outcomes"]["adc_protein"]
    check("run_all", "ADC protein: no variation between units beyond rounding, so no p value is reported and a note says why",
          adc["degenerate"] and all(c["p"] is None for c in adc["contrasts"]) and any("adc_protein" in n for n in res["notes"])
          and close(by_term(adc["contrasts"], "diet")["estimate"], 2.0, 0, 1e-4))     # generator: ADC 90 against 88
    pc = res["outcomes"]["growth_sgr"]["plan_power_check"]
    check("plan_power_check", "primary outcome: expected_sd 0.025, mdd 0.10, alpha 0.05 and 2 systems per level give the declared power 0.80",
          pc["checked"] and pc["consistent"] and pc["df"] == 2, "computed power %.4f, computed mdd %.4f" % (pc["computed_power"], pc["computed_mdd"]))
    o = dict(ST._COMMON, **ST.PLAN_OUTCOMES[0])
    bad = A.plan_power_check(dict(o, mdd=0.05), des, 2)
    check("plan_power_check", "a plan that claims 80 % power for half that difference is flagged; a plan without power inputs is 'not checked'",
          bad["checked"] and not bad["consistent"] and bad["computed_power"] < 0.5
          and not res["outcomes"]["intake_log"]["plan_power_check"]["checked"], "power at mdd 0.05 is %.3f" % bad["computed_power"])
    # ---- sensitivity analysis without the pending exclusion
    sp = res["outcomes"]["growth_sgr"]["sensitivity_pending"]
    y = res["outcomes"]["growth_sgr"]["y"]
    hand = ((y["U1D2"] - y["U1D1"]) + ((y["U3D2"] - y["U3D1"]) + (y["U4D2"] - y["U4D1"])) / 2) / 2
    cd = by_term(sp["contrasts"], "diet")
    check("sensitivity_pending", "the pending exclusion U2D2 is removed: 3 complete systems, 1 error df, estimate equals the hand calculation",
          sp["removed"] == ["U2D2"] and cd["df"] == 1 and cd["n_systems"] == 3 and close(cd["estimate"], hand, 0, 1e-8),
          "diet effect %.4f, 95 %% CI %.3f to %.3f" % (cd["estimate"], cd["ci_low"], cd["ci_high"]))
    none = A.sensitivity_pending([dict(u, exclusion_pending=False) for u in units], y, des)
    check("sensitivity_pending", "no pending exclusion: nothing is removed and the result says so", none["removed"] == [] and none["contrasts"] is None)
    # ---- fish as replicates
    fl = res["fish_level"]
    check("fish_level_naive", "seed 7: fish as replicates uses 160 fish and 156 df; the unit analysis of the same weights has 2 df and a larger SE",
          fl["n_fish"] == 160 and fl["naive"]["df"] == 156 and fl["correct"]["df"] == 2 and fl["n_units"] == 8 and fl["se_ratio"] > 1,
          "p %.1e against %.3f; SE ratio %.2f" % (fl["naive"]["p"], fl["correct"]["p"], fl["se_ratio"]))
    # ---- run record
    con_b, model_b, _ = ST.make_trial(YAML, seed=7)
    res_b = A.run_all(con_b, model_b)
    same = res_b["run_record"] == res["run_record"]
    con_b.execute("UPDATE weighings SET biomass_kg = biomass_kg + 0.001 WHERE record_id = 'U1D1_HARV'")
    r2 = A.run_all(con_b, model_b)["run_record"]
    con_b.execute("UPDATE planned_outcomes SET alpha = 0.10 WHERE outcome_label = 'intake_log'")
    r3 = A.run_all(con_b, model_b)["run_record"]
    rr = res["run_record"]
    check("run_record", "same data give the same three hashes; 1 g on one harvest weight changes the input and output hash only;"
          " a changed alpha changes the parameter hash", same and r2["input_snapshot_hash"] != rr["input_snapshot_hash"]
          and r2["output_hash"] != rr["output_hash"] and r2["params_hash"] == rr["params_hash"] and r3["params_hash"] != r2["params_hash"]
          and all(len(rr[k]) == 64 for k in ("input_snapshot_hash", "params_hash", "output_hash")))
    # ---- no plan
    con_c, model_c, _ = ST.make_trial(YAML, seed=7)
    for t in ("analysis_runs", "covariate_roles", "planned_outcomes", "analysis_plans"):
        con_c.execute('DELETE FROM "%s"' % t)
    try:
        A.run_all(con_c, model_c)
        ok = False
    except A.DesignError as e:
        ok = "no planned_outcomes" in str(e)
    ex = A.run_all(con_c, model_c, outcomes=[{"outcome_label": "tgc_explore", "outcome": "tgc", "scale": "raw", "role": "exploratory"}])
    check("run_all", "without a plan the pre-registered run is refused; an explicit exploratory outcome runs and carries no plan id",
          ok and ex["plan_id"] is None and list(ex["outcomes"]) == ["tgc_explore"])
    return res


def test_seeds():
    jobs = [(s, False) for s in range(1, N_SEEDS + 1)] + [(s, True) for s in range(1, N_SEEDS + 1)]
    runs = run_seeds(jobs)
    eff = [r for r in runs if not r["null"]]
    nul = [r for r in runs if r["null"]]
    n = len(eff)
    # per seed: measured data against the generator's truth values
    tol = {"growth_sgr": 2e-4, "intake_log": 3e-4, "gain_log": 3e-4, "feed_per_gain_log": 3e-4, "survival_logit": 1e-9}
    for lb, tl in tol.items():
        worst = max(abs(r["outcomes"][lb][t]["estimate"] - r["truth"][lb][t]) for r in runs for t in r["truth"][lb])
        check("stratum_contrasts", "%s: every contrast from the measured data equals the one from the generator's truth values (%d trials)" % (lb, len(runs)),
              worst <= tl, "largest difference %.1e (tolerance %.0e)" % (worst, tl))
    # SGR: the generator's cell means are known constants
    g = ST.DIETS
    d12, d16 = g["D2"]["sgr"][12.0] - g["D1"]["sgr"][12.0], g["D2"]["sgr"][16.0] - g["D1"]["sgr"][16.0]
    true = {"diet": (d12 + d16) / 2, "diet x temperature": d16 - d12,
            "temperature": (g["D1"]["sgr"][16.0] + g["D2"]["sgr"][16.0]) / 2 - (g["D1"]["sgr"][12.0] + g["D2"]["sgr"][12.0]) / 2}
    for term, tv in true.items():
        est = np.array([r["outcomes"]["growth_sgr"][term]["estimate"] for r in eff])
        cover = sum(r["outcomes"]["growth_sgr"][term]["ci_low"] <= tv <= r["outcomes"]["growth_sgr"][term]["ci_high"] for r in eff)
        check("stratum_contrasts", "growth_sgr %s: unbiased for the true %.3f %%/day and the 95 %% interval covers it (%d trials)" % (term, tv, n),
              abs(est.mean() - tv) <= 4 * est.std(ddof=1) / math.sqrt(n) and cover >= binom_lo(n, 0.95),
              "mean estimate %.4f, coverage %d/%d (at least %d expected)" % (est.mean(), cover, n, binom_lo(n, 0.95)))
    fe = np.array([r["outcomes"]["feed_per_gain_log"]["diet"]["estimate"] for r in eff])

    def fcr_expected(sgr_pct, f):
        """Feed per gain of the generator without noise or deaths: f applies to the growth of the
        feeding days, and the fish also grow on the two fasting days before the weighings."""
        s = sgr_pct / 100.0
        fed = sum(math.exp(s * d) * s * f for d in range(ST.FINAL) if d not in (ST.MID - 1, ST.FINAL - 1))
        return fed / (math.exp(s * ST.FINAL) - 1.0)

    tv = float(np.mean([math.log(fcr_expected(g["D2"]["sgr"][t], ST.TRUE_FCR_DM["D2"]) / fcr_expected(g["D1"]["sgr"][t], ST.TRUE_FCR_DM["D1"]))
                        for t in (12.0, 16.0)]))
    check("stratum_contrasts", "feed per gain: the diet ratio recovers the generator's %.2f / %.2f on feeding days = %.4f over the period" % (
        ST.TRUE_FCR_DM["D2"], ST.TRUE_FCR_DM["D1"], math.exp(tv)), abs(fe.mean() - tv) < 0.001,
        "mean ratio %.4f over %d trials" % (math.exp(fe.mean()), n))
    # survival: the generator has the same mortality in every tank, so every test is a null test
    hi = binom_hi(len(runs), 0.05)
    rej = {t: sum(r["outcomes"]["survival_logit"][t]["p"] < 0.05 for r in runs) for t in ("diet", "diet x temperature", "temperature")}
    check("stratum_contrasts", "survival (same mortality in all tanks): each test rejects at about 5 %% (%d trials)" % len(runs),
          all(v <= hi for v in rej.values()), ", ".join("%s %d" % kv for kv in rej.items()) + "; at most %d expected" % hi)
    # null scenario: both diets identical
    hi = binom_hi(len(nul), 0.05)
    for lb in ("growth_sgr", "intake_log", "gain_log", "feed_per_gain_log"):
        k = sum(r["outcomes"][lb]["diet"]["p"] < 0.05 for r in nul)
        est = np.array([r["outcomes"][lb]["diet"]["estimate"] for r in nul])
        check("stratum_contrasts", "%s, both diets identical in the generator: no bias and the diet test rejects at about 5 %% (%d trials)" % (lb, len(nul)),
              k <= hi and abs(est.mean()) <= 4 * est.std(ddof=1) / math.sqrt(len(nul)), "%d rejections (at most %d expected), mean estimate %.1e" % (k, hi, est.mean()))
    naive = sum(r["fish"]["naive_p"] < 0.05 for r in nul)
    correct = sum(r["fish"]["correct_p"] < 0.05 for r in nul)
    check("fish_level_naive", "both diets identical: fish as replicates 'finds' a diet effect far too often, the unit analysis of the same weights does not",
          naive / len(nul) >= 0.20 and correct <= hi, "fish as replicates %d/%d = %.0f %%, unit as replicate %d/%d" % (
              naive, len(nul), 100.0 * naive / len(nul), correct, len(nul)))
    rs = sum(r["outcomes"]["growth_sgr"]["_rand_p"] <= 0.125 + 1e-12 for r in nul)
    check("randomisation_test_diet", "both diets identical: the randomisation test reaches its smallest p (0.125) in about 12.5 %% of the trials",
          binom_lo(len(nul), 0.125) <= rs <= binom_hi(len(nul), 0.125), "%d/%d" % (rs, len(nul)))
    tr = sum(r["outcomes"]["growth_sgr"]["_vc"]["system_component_truncated"] for r in runs)
    check("variance_components", "the generator has no system variance: the system component of growth is estimated below 0 in about half the trials",
          0.3 <= tr / len(runs) <= 0.7, "%d/%d" % (tr, len(runs)))
    # power as planned against the rejection rate actually obtained
    rej = sum(r["outcomes"]["growth_sgr"]["diet"]["p"] < 0.05 for r in eff)
    sd = math.sqrt(np.mean([(m * ST.SGR_NOISE / math.sqrt(3)) ** 2 for d in g.values() for m in d["sgr"].values()]))   # SD of a uniform
    pw = A.design_power(true["diet"], "diet", sd, 0.0, 2)["power"]
    check("design_power", "growth_sgr diet effect: the rejection rate over %d trials agrees with the power formula at the generator's SD" % n,
          binom_lo(n, pw, 0.0005) - 0.04 * n <= rej <= binom_hi(n, pw, 0.9995) + 0.04 * n,
          "formula %.3f at SD %.4f, obtained %d/%d = %.3f" % (pw, sd, rej, n, rej / n))
    def col(rs, lb, term):
        return [{"seed": r["seed"], **r["outcomes"][lb][term]} for r in rs]

    return {"n_effect": n, "n_null": len(nul), "naive_rejections_null": naive, "correct_rejections_null": correct,
            "growth_diet_rejections_effect": rej, "power_formula": pw, "sd_unit_generator": sd, "true_growth": true,
            "growth_diet_effect": col(eff, "growth_sgr", "diet"), "growth_diet_null": col(nul, "growth_sgr", "diet"),
            "fish_null": [dict(r["fish"], seed=r["seed"]) for r in nul]}


def scipy_note():
    """Not a test of this code: records whether the installed scipy shows the two defects that
    made power_t avoid the non-central t and F functions."""
    import scipy
    tc = stats.t.ppf(0.975, 2)
    with np.errstate(all="ignore"):
        a, b = float(stats.nct.cdf(-tc, 2, 15.8)), float(stats.ncf.sf(tc ** 2, 1, 2, 0.0))
    print("info scipy %s: nct.cdf(-4.303, 2, 15.8) = %r, ncf.sf(18.51, 1, 2, 0) = %r (a probability near 0 and 0.05 are correct)" % (
        scipy.__version__, a, b))


def main():
    rng = np.random.default_rng(20250303)
    test_algebra(rng)
    test_null_and_power(rng)
    test_randomisation_and_holm(rng)
    test_trial()
    summary = test_seeds()
    scipy_note()
    missing = sorted(set(A.ANALYSES) - EXERCISED)
    if missing:
        FAILS.append("analyses without a test: %s" % ", ".join(missing))
    with open(os.path.join(HERE, ".coverage_analyses.json"), "w") as fh:
        json.dump({"exercised": sorted(EXERCISED & set(A.ANALYSES)), "waived": {}, "n_checks": N_CHECKS[0], "n_failed": len(FAILS),
                   "checks": CHECKS, "seeds": summary}, fh)       # also read by tools/build_dashboard.py (evidence section)
    if FAILS:
        print("FAILURES:")
        for f in FAILS:
            print("  " + f)
        print("FAILED: %d problem(s)" % len(FAILS))
        return 1
    print("%d checks, 0 failed; analyses exercised %d/%d; %d + %d synthetic trials (effect, no effect)" % (
        N_CHECKS[0], len(EXERCISED & set(A.ANALYSES)), len(A.ANALYSES), summary["n_effect"], summary["n_null"]))
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        i = sys.argv.index("--worker")
        sys.exit(worker(sys.argv[i + 1], sys.argv[i + 2]))
    sys.exit(main())
