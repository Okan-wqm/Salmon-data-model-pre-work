#!/usr/bin/env python3
"""Build one self-contained HTML dashboard for a trial: design, data quality, growth, feed,
survival, environment, the pre-registered statistics with an explanation of every test, and
the evidence that the numbers are right.

Every number on the page comes from the tools of this repository at build time:
tools/metrics.py (unit metrics), tools/validate.py (rule findings), tools/analysis.py (statistics),
and, when present, the records of the last proof-chain run (tools/.coverage_chain.json and
tools/.coverage_analyses.json). Nothing is typed by hand: numbers are written through one helper
(Page.n) that keeps the unrounded value next to the printed one, and the whole data set behind
the page is embedded as JSON (script id 'payload'). tools/test_dashboard.py checks both.

The page is static HTML and CSS with inline SVG charts (no JavaScript, no network), so it opens
by double click and prints. Requirements beyond the analysis tools: matplotlib.
Usage: build_dashboard.py [data_model.yaml] [database] [--out dashboard.html] [--seed 7]
       Without a database the synthetic trial is built and its known true values are shown.
"""
import datetime as dt
import html
import io
import json
import math
import os
import platform
import re
import sqlite3
import subprocess
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import scipy  # noqa: E402
from scipy import stats  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analysis as A  # noqa: E402
import metrics as M  # noqa: E402
import validate as V  # noqa: E402

DEFAULT_YAML = os.path.join(os.path.dirname(HERE), "data_model.yaml")
N_SYSTEMS_GRID = list(range(2, 9))


# ---------------------------------------------------------------- data behind the page
def _rows(con, sql, *args):
    cur = con.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _json_file(path):
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return None


def _no_paths(text):
    """Absolute paths of the machine that ran the tests are of no use to a reader: file name only.
    The repository and home directories are removed literally (they may hold spaces), any other
    absolute path by pattern."""
    for root in (HERE, os.path.dirname(HERE), os.path.expanduser("~")):
        for r in {root, root.replace("\\", "/")}:
            text = re.sub(re.escape(r) + r"[\\/]*", "", text)
    return re.sub(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s\\/]+[\\/])+([^\s\\/]+)", r"\1", text)


def _git_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10)
        d = subprocess.run(["git", "status", "--porcelain"], cwd=HERE, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return {"commit": r.stdout.strip(), "uncommitted_changes": bool(d.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        pass
    return {"commit": None, "uncommitted_changes": None}


def collect(yaml_path=DEFAULT_YAML, db=None, seed=7):
    """Everything the page shows, as one JSON-ready dictionary."""
    truth = None
    if db:
        from build_db import load_model
        model, con = load_model(yaml_path), sqlite3.connect(db)
    else:
        from synthetic_trial import make_trial
        con, model, truth = make_trial(yaml_path, seed=seed)
    res = A.run_all(con, model)
    tid = res["trial_id"]
    des, units = A.analysis_dataset(con, tid, model)
    d0 = {u["unit_id"]: dt.date.fromisoformat(u["start_date"]) for u in units}
    day = lambda uid, iso: (dt.date.fromisoformat(iso[:10]) - d0[uid]).days  # noqa: E731
    p = {"trial": _rows(con, "SELECT * FROM trials WHERE trial_id = ?", tid)[0], "synthetic": truth is not None,
         "seed": seed if truth is not None else None, "analysis": res}
    counts = {t: con.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0] for t in model["tables"]}
    p["tables"] = {"n_tables": len(counts), "n_core": sum(1 for t in model["tables"].values() if t.get("tier", "core") == "core"),
                   "n_filled": sum(1 for v in counts.values() if v), "n_rows": sum(counts.values()),
                   "rows": {t: n for t, n in counts.items() if n}}
    p["model"] = {"n_rules": len(model["cross_table_rules"]), "n_validator_rules": len(V.validator_rules(model)),
                  "n_metrics": len(model["derived_metrics"]), "n_analyses": len(A.ANALYSES)}
    # design: systems with their units
    p["systems"] = [{"system_id": s, "level": des["system_level"][s],
                     "units": [{"unit_id": u["unit_id"], "diet_id": u["diet_id"], "pending": bool(u.get("exclusion_pending"))}
                               for u in units if u["system_id"] == s]} for s in sorted(des["system_level"])]
    p["plan"] = _rows(con, "SELECT * FROM analysis_plans WHERE plan_id = ?", res["plan_id"])
    p["randomisations"] = _rows(con, "SELECT * FROM randomisations WHERE trial_id = ? ORDER BY randomisation_id", tid)
    p["locked_runs"] = _rows(con, "SELECT * FROM analysis_runs WHERE trial_id = ? ORDER BY run_at", tid)
    p["deviations"] = _rows(con, "SELECT record_id, date, unit_id, category, exclusion_reason, description, decided_by, decided_date"
                                 " FROM deviations WHERE trial_id = ? ORDER BY date", tid)
    # rule findings
    fnd = V.validate(con, model, tid)
    text = {r["id"]: r["rule"] for r in model["cross_table_rules"]}
    by = {}
    for f in fnd:
        g = by.setdefault((f["rule"], f["severity"]), {"rule": f["rule"], "severity": f["severity"], "n": 0, "units": [],
                                                       "example": f["message"], "rule_text": text.get(f["rule"], "")})
        g["n"] += 1
        if f["unit_id"]:
            g["units"].append(f["unit_id"])
    p["findings"] = {"summary": V.summary(fnd)["by_severity"], "groups": sorted(by.values(), key=lambda g: (g["severity"], g["rule"])),
                     "all": [{k: f[k] for k in ("rule", "severity", "unit_id", "message")} for f in fnd]}
    # per unit: coverage, weights per occasion, individual end weights, daily feed, deaths, temperature
    per = {}
    for u in units:
        uid = u["unit_id"]
        occ = [{"occasion": o["occasion"], "day": M.day_since_d0(con, uid, o["occasion"]), "weight_g": M.unit_weight_g(con, uid, o["occasion"]),
                "source": (u.get("w_source_by_occasion") or {}).get(o["occasion"])} for o in M.occasions(con, uid)]
        occ = [o for o in occ if o["day"] is not None and o["day"] >= 0 and o["weight_g"] is not None]
        fish = [r["weight_g"] for r in _rows(con, "SELECT weight_g FROM weighings WHERE unit_id = ? AND occasion = ? AND weight_g IS NOT NULL"
                                                  " AND sampling_type IN ('sample', 'individual') AND (qc_flag IS NULL OR qc_flag != 'removed')"
                                                  " ORDER BY record_id", uid, u["end_occasion"])]
        feed = [(day(uid, r["date"]), r["g"]) for r in _rows(con, "SELECT date, sum(feed_given_g) AS g FROM feeding WHERE unit_id = ?"
                                                                  " GROUP BY date ORDER BY date", uid)]
        deaths = [(day(uid, r["date"]), r["n"]) for r in _rows(
            con, "SELECT date, sum(n_fish) AS n FROM events WHERE unit_id = ? AND outcome_class IN ('death', 'cull_moribund')"
                 " AND n_fish > 0 AND date >= ? AND date < ? GROUP BY date ORDER BY date", uid, u["start_date"], u["end_date"])]
        tz = _rows(con, "SELECT s.timezone FROM sites s JOIN systems y ON y.site_id = s.site_id WHERE y.system_id = ?", u["system_id"])
        temp = _temperature(con, uid, u["system_id"], u["start_date"], u["end_date"], tz[0]["timezone"] if tz else "UTC")
        per[uid] = {"occasions": occ, "fish_end_weights_g": fish, "feed_daily_g": [[d, g] for d, g in feed if 0 <= d],
                    "deaths": [[d, n] for d, n in deaths], "temperature": temp,
                    "coverage": {k: u.get(k) for k in ("degree_days_coverage", "intake_dm_coverage", "feed_given_coverage")},
                    "days": u["days"]}
    p["per_unit"] = per
    # known truth of the synthetic trial: the same contrasts computed from the generator's unit values
    if truth is not None:
        import synthetic_trial as ST
        keys = {"growth_sgr": lambda t: t["sgr_true_pct"], "intake_log": lambda t: math.log(t["feed_dm_eaten_g"]),
                "gain_log": lambda t: math.log(t["gain_kg"]), "feed_per_gain_log": lambda t: math.log(t["fcr_bio"]),
                "survival_logit": lambda t: math.log((t["n_d0"] - t["deaths"] + 0.5) / (t["deaths"] + 0.5))}
        tc = {}
        for lb, f in keys.items():
            if lb in res["outcomes"]:
                tc[lb] = {c["label"]: c["estimate"] for c in A.stratum_contrasts(units, {u_: f(t) for u_, t in truth.items()}, des)}
        g = ST.DIETS
        lv = sorted(g["D1"]["sgr"])
        p["truth"] = {"units": {u_: {k: t[k] for k in ("sgr_true_pct", "fcr_bio", "survival_pct", "gain_kg", "feed_dm_eaten_g", "deaths")}
                                for u_, t in truth.items()},
                      "contrasts": tc,
                      "generator": {"sgr_cell_means": {d_: {("%gC" % t): g[d_]["sgr"][t] for t in lv} for d_ in g},
                                    "sgr_diet_main": float(np.mean([g["D2"]["sgr"][t] - g["D1"]["sgr"][t] for t in lv])),
                                    "sgr_interaction": (g["D2"]["sgr"][lv[1]] - g["D1"]["sgr"][lv[1]]) - (g["D2"]["sgr"][lv[0]] - g["D1"]["sgr"][lv[0]]),
                                    "sgr_temperature": float(np.mean([g[d_]["sgr"][lv[1]] - g[d_]["sgr"][lv[0]] for d_ in g])),
                                    "fcr_dm_feeding_days": dict(ST.TRUE_FCR_DM), "unit_sgr_noise_pct": 100 * ST.SGR_NOISE,
                                    "daily_mortality_probability": ST.DAILY_DEATH_PROB, "fish_weight_cv_pct": 100 * ST.FISH_CV}}
    else:
        p["truth"] = None
    # power for the planned SD and difference of the primary outcome
    prim = [r for r in res["outcomes"].values() if r["plan"].get("role") == "primary" and r["plan_power_check"].get("checked")]
    if prim:
        o = prim[0]["plan"]
        a, b = len(des["whole_levels"]), len(des["diets"])
        grid = []
        for n in N_SYSTEMS_GRID:
            row = {"n_systems_per_level": n, "df": a * (n - 1), "t_crit": float(stats.t.ppf(1 - o["alpha"] / 2, a * (n - 1)))}
            for term in ("diet", "interaction") if a > 1 else ("diet",):
                dp = A.design_power(o["mdd"], term, o["expected_sd"], 0.0, n, a, b, o["alpha"])
                row[term] = {"se": dp["se"], "power_at_mdd": dp["power"], "mdd_80": dp["mdd_80"]}
            grid.append(row)
        p["power"] = {"outcome": o["outcome_label"], "expected_sd": o["expected_sd"], "planned_mdd": o["mdd"], "alpha": o["alpha"],
                      "planned_power": o["power"], "grid": grid, "n_run": min(res["design"]["n_systems_per_level"].values()),
                      "t_crit_normal": float(stats.norm.ppf(1 - o["alpha"] / 2))}
    else:
        p["power"] = None
    # records of the last proof-chain run and the stored independent reference
    p["chain"] = _json_file(os.path.join(HERE, ".coverage_chain.json"))
    for s in (p["chain"] or {}).get("steps", []):       # a test may print where it wrote its record: keep the file name only
        s["message"] = _no_paths(s["message"])
    p["evidence"] = _json_file(os.path.join(HERE, ".coverage_analyses.json"))
    ref = _json_file(os.path.join(HERE, "reference", "analysis_reference.json"))
    p["reference"] = None
    if ref and all(lb in res["outcomes"] and all(abs(res["outcomes"][lb]["y"].get(u_, float("nan")) - v) < 1e-9
                                                 for u_, v in zip(ref["inputs"]["unit"], ref["inputs"][lb])) for lb in ref["aov"]):
        p["reference"] = {"made_with": ref["made_with"],
                          "wald_z_p_diet": {lb: r["wald_z_p_diet"] for lb, r in ref["reml"].items()},
                          "reml_var_system": {lb: r["var_system"] for lb, r in ref["reml"].items()}}
        # lme4 / lmerTest small-sample tests of the same tank values (tools/reference/make_reference_lmer.R)
        lref = _json_file(os.path.join(HERE, "reference", "lmer_reference.json"))
        if lref and all(lb in res["outcomes"] and all(abs(res["outcomes"][lb]["y"].get(u_, float("nan")) - v) < 1e-9
                                                      for u_, v in zip(lref["inputs"]["unit"], lref["inputs"][lb])) for lb in lref["lmer"]):
            p["reference"]["lmer"] = {"made_with": lref["made_with"],
                                      "diet": {lb: {"singular": r["singular"],
                                                    "satterthwaite": {k: r["satterthwaite"]["diet"][k] for k in ("df", "p")},
                                                    "kenward_roger": {k: r["kenward_roger"]["diet"][k] for k in ("df", "p")}}
                                               for lb, r in lref["lmer"].items()}}
    p["derived"] = _derived(p)
    p["versions"] = dict({"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__,
                          "matplotlib": matplotlib.__version__}, **_git_commit())
    return A._canon(p)


def _temperature(con, unit_id, system_id, start, end, tzname):
    """Daily temperature used for the unit over [start, end): its own sensor, else the system
    reference (source 'system'), else missing. Local date of each reading."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tzname)

    def daily(rows):
        out = {}
        for r in rows:
            t = dt.datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).astimezone(tz)
            out.setdefault(t.date().isoformat(), []).append(r["value"])
        return {k: float(np.mean(v)) for k, v in out.items()}

    own = daily(_rows(con, "SELECT timestamp, value FROM environment WHERE unit_id = ? AND variable = 'temp_c'", unit_id))
    ref = daily(_rows(con, "SELECT timestamp, value FROM environment WHERE system_id = ? AND unit_id IS NULL AND variable = 'temp_c'",
                      system_id))
    d, stop, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    i = 0
    while d < stop:
        k = d.isoformat()
        out.append([i, own.get(k, ref.get(k)), "unit" if k in own else ("system" if k in ref else None)])
        d += dt.timedelta(days=1)
        i += 1
    return out


def _derived(p):
    """Numbers shown on the page that are simple functions of the payload (counts, percentages)."""
    res = p["analysis"]
    d = {"n_units": res["design"]["n_units"], "n_systems": len(p["systems"]),
         "n_fish_d0": sum(u["n_fish_d0"] or 0 for u in res["units"]),
         "trial_days": max(v["days"] for v in p["per_unit"].values()),
         "n_findings": sum(p["findings"]["summary"].values()),
         "n_pending": sum(1 for u in res["units"] if u["exclusion_pending"]),
         "outcomes": {}}
    for lb, r in res["outcomes"].items():
        c = [x for x in r["contrasts"] if x["term"] == "diet"][0]
        o = {"df": c["df"], "t_crit": float(stats.t.ppf(1 - r["alpha"] / 2, c["df"])) if c["df"] > 0 else None,
             "significant": (c.get("p_adjusted", c["p"]) < r["alpha"]) if c["p"] is not None else None}
        if r["scale"] in ("log", "logit") and c["ci_low"] is not None:
            o["pct"] = 100 * (c["natural"]["estimate"] - 1)
            o["pct_low"], o["pct_high"] = 100 * (c["natural"]["ci_low"] - 1), 100 * (c["natural"]["ci_high"] - 1)
        d["outcomes"][lb] = o
    if p["chain"]:
        d["chain_ok"] = sum(1 for s in p["chain"]["steps"] if s["ok"])
        d["chain_n"] = len(p["chain"]["steps"])
        d["chain_seconds"] = sum(s["seconds"] for s in p["chain"]["steps"])
    ev = p["evidence"]
    if ev and "growth_diet_effect" in (ev.get("seeds") or {}):
        s = ev["seeds"]
        tv = s["true_growth"]["diet"]
        eff = s["growth_diet_effect"]
        d["seeds"] = {"coverage_n": sum(1 for r in eff if r["ci_low"] <= tv <= r["ci_high"]), "n_effect": len(eff),
                      "mean_estimate": float(np.mean([r["estimate"] for r in eff])), "true": tv,
                      "reject_effect": sum(1 for r in eff if r["p"] < 0.05),
                      "reject_null": sum(1 for r in s["growth_diet_null"] if r["p"] < 0.05), "n_null": len(s["growth_diet_null"]),
                      "naive_null": sum(1 for r in s["fish_null"] if r["naive_p"] < 0.05),
                      "unit_null": sum(1 for r in s["fish_null"] if r["correct_p"] < 0.05)}
        d["seeds"]["naive_null_pct"] = 100.0 * d["seeds"]["naive_null"] / d["seeds"]["n_null"]
        d["seeds"]["coverage_pct"] = 100.0 * d["seeds"]["coverage_n"] / d["seeds"]["n_effect"]
        d["seeds"]["reject_effect_pct"] = 100.0 * d["seeds"]["reject_effect"] / d["seeds"]["n_effect"]
        if s.get("power_formula") is not None:
            d["seeds"]["power_share_se_pct"] = 100.0 * math.sqrt(s["power_formula"] * (1 - s["power_formula"]) / len(eff))
    fl = res.get("fish_level")
    if fl:
        d["fish_df_ratio"] = fl["naive"]["df"] / fl["correct"]["df"]
    prim = [r for r in res["outcomes"].values() if r["plan"].get("role") == "primary"]
    if prim:
        c = [x for x in prim[0]["contrasts"] if x["term"] == "diet"][0]
        if p["truth"] and prim[0]["plan"]["outcome"] == "sgr_pct_day" and c["se"]:
            d["primary_truth_distance_se"] = abs(c["estimate"] - p["truth"]["generator"]["sgr_diet_main"]) / c["se"]
        if c["df"] > 0:
            a = prim[0]["alpha"]
            d["t_crit_normal"] = float(stats.norm.ppf(1 - a / 2))
            d["ci_width_factor"] = float(stats.t.ppf(1 - a / 2, c["df"])) / d["t_crit_normal"]
        rd = prim[0]["randomisation_diet"]
        if rd:
            d["randomisation_floor"] = [{"n_systems": k, "n_allocations": 2 ** k, "min_p": 2.0 / 2 ** k}
                                        for k in sorted({rd["n_systems"], rd["n_systems"] + 2, rd["n_systems"] + 4, rd["n_systems"] + 8})]
    o3 = res["outcomes"]
    if all(k in o3 for k in ("intake_log", "gain_log", "feed_per_gain_log")):
        e = {k: [x for x in o3[k]["contrasts"] if x["term"] == "diet"][0]["estimate"] for k in ("intake_log", "gain_log", "feed_per_gain_log")}
        d["feed_identity"] = dict(e, intake_minus_gain=e["intake_log"] - e["gain_log"])
    return d


# ---------------------------------------------------------------- charts (matplotlib -> inline SVG)
PALETTE = ["#4477AA", "#EE7733", "#228833", "#AA3377"]       # diets, reference first; distinguishable without red/green
MARKERS, DASHES = ["o", "D", "s", "^"], ["-", "--", ":", "-."]   # whole-plot levels
SEVERITY_COLOR = {"error": "#CC3311", "warning": "#CCBB44", "info": "#BBBBBB"}
INK, GREY = "#222222", "#888888"


def _style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 9, "axes.labelsize": 9, "xtick.labelsize": 8,
        "ytick.labelsize": 8, "legend.fontsize": 8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlelocation": "left", "legend.frameon": False, "lines.linewidth": 1.4, "axes.linewidth": 0.8,
        "svg.fonttype": "path", "svg.hashsalt": "feed-trial-dashboard", "figure.dpi": 100, "savefig.dpi": 100})


class Look:
    """One colour per diet and one marker / dash per whole-plot level, everywhere on the page."""

    def __init__(self, p):
        des = p["analysis"]["design"]
        diets = [des["reference_diet"]] + [d for d in des["diets"] if d != des["reference_diet"]]
        self.color = {d: PALETTE[i % len(PALETTE)] for i, d in enumerate(diets)}
        self.marker = {lv: MARKERS[i % 4] for i, lv in enumerate(des["whole_levels"])}
        self.dash = {lv: DASHES[i % 4] for i, lv in enumerate(des["whole_levels"])}
        self.diets, self.levels = diets, des["whole_levels"]
        self.units = {u["unit_id"]: u for u in p["analysis"]["units"]}
        self.order = [u["unit_id"] for s in p["systems"] for u in s["units"]]
        self.wf = des["whole_factor"]

    def pt(self, uid):
        u = self.units[uid]
        return {"color": self.color[u["diet_id"]], "marker": self.marker[u["whole_level"]],
                "markerfacecolor": "white" if u["exclusion_pending"] else self.color[u["diet_id"]]}

    def legend(self, ax, pending=True, **kw):
        from matplotlib.lines import Line2D
        h = [Line2D([], [], color=self.color[d], marker="s", ls="", label="diet %s" % d) for d in self.diets]
        if len(self.levels) > 1:
            h += [Line2D([], [], color=GREY, marker=self.marker[lv], ls=self.dash[lv], label=lv) for lv in self.levels]
        if pending and any(u["exclusion_pending"] for u in self.units.values()):
            h.append(Line2D([], [], color=GREY, marker="o", markerfacecolor="white", ls="", label="exclusion pending"))
        ax.legend(handles=h, **kw)


def fig_growth(p, L):
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    for uid in L.order:
        occ = p["per_unit"][uid]["occasions"]
        u = L.units[uid]
        ax.plot([o["day"] for o in occ], [o["weight_g"] for o in occ], ls=L.dash[u["whole_level"]], ms=5, **L.pt(uid))
    ax.set_yscale("log")
    ws = [o["weight_g"] for v in p["per_unit"].values() for o in v["occasions"]]
    ticks = [t for t in (50, 75, 100, 150, 200, 300, 400, 600, 800, 1200, 1600, 2400) if min(ws) * 0.9 <= t <= max(ws) * 1.1]
    ax.set_yticks(ticks)
    ax.set_yticklabels([str(t) for t in ticks])
    ax.minorticks_off()
    ax.set_xlabel("day since d0")
    ax.set_ylabel("mean fish weight per tank (g, log scale)")
    ax.set_title("Tank mean weight at each weighing; straight lines on the log scale mean a constant growth rate")
    L.legend(ax, loc="upper left")
    ax.margins(0.05)
    return fig


def _cells(L):
    return [(lv, d) for lv in L.levels for d in L.diets]


def fig_sgr(p, L):
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    cells = _cells(L)
    for i, (lv, d) in enumerate(cells):
        us = [u for u in L.units.values() if u["diet_id"] == d and u["whole_level"] == lv and u["sgr_pct_day"] is not None]
        for j, u in enumerate(us):
            ax.plot(i + (j - (len(us) - 1) / 2) * 0.12, u["sgr_pct_day"], ls="", ms=7, **L.pt(u["unit_id"]))
        if us:
            ax.hlines(np.mean([u["sgr_pct_day"] for u in us]), i - 0.28, i + 0.28, color=L.color[d], lw=2)
        if p["truth"]:
            tv = p["truth"]["generator"]["sgr_cell_means"].get(d, {}).get(lv)
            if tv is not None:
                ax.plot(i + 0.36, tv, marker="x", color=INK, ms=7, ls="")
    ax.set_xticks(range(len(cells)))
    ax.set_xticklabels(["%s\ndiet %s" % c for c in cells])
    ax.set_ylabel("specific growth rate (% per day)")
    ax.set_title("Growth rate of each tank (points), cell mean (bar)" + (", generator's true cell mean (x)" if p["truth"] else ""))
    ax.margins(0.08)
    return fig


def fig_fish(p, L):
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    rng = np.random.default_rng(1)
    for i, uid in enumerate(L.order):
        w = p["per_unit"][uid]["fish_end_weights_g"]
        if not w:
            continue
        ax.plot(i + rng.uniform(-0.22, 0.22, len(w)), w, ls="", marker=".", ms=4, alpha=0.55, color=L.color[L.units[uid]["diet_id"]])
        ax.hlines(np.mean(w), i - 0.32, i + 0.32, color=INK, lw=1.6)
    ax.set_xticks(range(len(L.order)))
    ax.set_xticklabels(["%s\n%s" % (uid, L.units[uid]["whole_level"]) for uid in L.order])
    ax.set_ylabel("individual weight at the final weighing (g)")
    ax.set_title("Fish (dots) vary around their tank mean (bar); tanks vary too, and only tanks were randomised")
    ax.margins(0.04)
    return fig


def fig_feed(p, L):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.2, 3.2), gridspec_kw={"width_ratios": [1.25, 1]})
    for uid in L.order:
        f = p["per_unit"][uid]["feed_daily_g"]
        u = L.units[uid]
        a1.plot([d for d, _ in f], [g for _, g in f], color=L.color[u["diet_id"]], ls=L.dash[u["whole_level"]], lw=1)
    a1.set_xlabel("day since d0")
    a1.set_ylabel("feed given per tank (g per day)")
    a1.set_title("Daily feed; drops to 0 on the fasting day before a weighing")
    L.legend(a1, pending=False, loc="upper left")
    for i, uid in enumerate(L.order):
        u = L.units[uid]
        if u["fcr_biological"] is not None:
            a2.plot(i, u["fcr_biological"], ls="", ms=7, **L.pt(uid))
        if p["truth"]:
            a2.plot(i, p["truth"]["units"][uid]["fcr_bio"], marker="x", color=INK, ms=7, ls="")
    a2.set_xticks(range(len(L.order)))
    a2.set_xticklabels(L.order, rotation=90)
    a2.set_ylabel("biological FCR (kg dry feed eaten per kg gain)")
    a2.set_title("FCR per tank" + (" (x = generator's true value)" if p["truth"] else ""))
    a2.margins(0.08, 0.25)
    fig.tight_layout()
    return fig


def fig_survival(p, L):
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    for uid in L.order:
        u, days = L.units[uid], p["per_unit"][uid]["days"]
        x, y, c = [0], [0], 0
        for d, n in p["per_unit"][uid]["deaths"]:
            c += n
            x.append(d)
            y.append(c)
        x.append(days)
        y.append(c)
        ax.step(x, y, where="post", color=L.color[u["diet_id"]], ls=L.dash[u["whole_level"]], lw=1.2)
    ax.set_xlabel("day since d0")
    ax.set_ylabel("cumulative deaths per tank")
    ax.set_title("Cumulative deaths per tank" + (" (the generator gives every tank the same daily risk)" if p["truth"] else ""))
    L.legend(ax, pending=False, loc="upper left")
    ax.margins(0.03)
    return fig


def fig_temperature(p, L):
    fig, axes = plt.subplots(1, len(L.levels), figsize=(9.2, 2.9), squeeze=False)
    for ax, lv in zip(axes[0], L.levels):
        for uid in L.order:
            u = L.units[uid]
            if u["whole_level"] != lv:
                continue
            t = [r for r in p["per_unit"][uid]["temperature"] if r[1] is not None]
            ax.plot([r[0] for r in t], [r[1] for r in t], color=L.color[u["diet_id"]], lw=0.8, alpha=0.8)
            fb = [r for r in t if r[2] == "system"]
            ax.plot([r[0] for r in fb], [r[1] for r in fb], ls="", marker="o", ms=3.5, markerfacecolor="white", color=INK)
        ax.set_title("%s%s" % (("%s " % L.wf) if L.wf else "", lv))
        ax.set_xlabel("day since d0")
        ax.margins(0.03, 0.25)
    axes[0][0].set_ylabel("daily temperature used (°C)")
    fig.suptitle("Tank sensor readings (lines); open circles = days filled from the system reference sensor", x=0.01, ha="left", fontsize=9)
    fig.tight_layout()
    return fig


def fig_coverage(p, L):
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 2.9), sharey=True)
    for ax, key, title in ((axes[0], "degree_days_coverage", "Temperature days (degree-days)"),
                           (axes[1], "intake_dm_coverage", "Feeding days with uneaten-feed collection (intake)")):
        obs = [(p["per_unit"][u]["coverage"][key] or {}).get("n_observed", 0) for u in L.order]
        imp = [(p["per_unit"][u]["coverage"][key] or {}).get("n_imputed", 0) for u in L.order]
        y = np.arange(len(L.order))
        ax.barh(y, obs, color="#99AABB", label="observed")
        ax.barh(y, imp, left=obs, color=INK, label="filled by the period-sum rule")
        for yi, o_, i_ in zip(y, obs, imp):
            ax.text(o_ + i_ + 1, yi, "%d" % i_, va="center", fontsize=7)
        ax.set_yticks(y)
        ax.set_yticklabels(L.order)
        ax.set_xlabel("days")
        ax.set_title(title)
        ax.set_xlim(0, max(o_ + i_ for o_, i_ in zip(obs, imp)) * 1.1)
    axes[0].invert_yaxis()
    fig.suptitle("Days observed and days filled per tank (number = days filled)", x=0.01, ha="left", fontsize=9)
    fig.tight_layout()
    fig.legend(*axes[1].get_legend_handles_labels(), loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=2)
    return fig


def fig_findings(p, L):
    g = p["findings"]["groups"]
    fig, ax = plt.subplots(figsize=(6.4, 0.9 + 0.33 * max(len(g), 1)))
    y = np.arange(len(g))
    ax.barh(y, [x["n"] for x in g], color=[SEVERITY_COLOR[x["severity"]] for x in g])
    for yi, x in zip(y, g):
        ax.text(x["n"] + 0.15, yi, "%d" % x["n"], va="center", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels(["%s (%s)" % (x["rule"], x["severity"]) for x in g])
    ax.invert_yaxis()
    ax.set_xlabel("findings")
    ax.set_xlim(0, max([x["n"] for x in g] + [1]) * 1.15)
    ax.set_title("Rule findings on this trial, by rule")
    return fig


SCALE_LABEL = {"raw": "", "log": " (natural log)", "logit": " (empirical logit)"}


def fig_cells(p, L):
    out = p["analysis"]["outcomes"]
    n = len(out)
    cols = min(3, n)
    rows = int(math.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(3.1 * cols, 2.5 * rows), squeeze=False)
    xs = {lv: i for i, lv in enumerate(L.levels)}
    for ax, (lb, r) in zip(axes.ravel(), out.items()):
        for d_i, d in enumerate(L.diets):
            off = (d_i - (len(L.diets) - 1) / 2) * 0.10
            means = []
            for lv in L.levels:
                ys = [(r["y"][u["unit_id"]], u["unit_id"]) for u in L.units.values() if u["diet_id"] == d and u["whole_level"] == lv
                      and r["y"].get(u["unit_id"]) is not None]
                for v, uid in ys:
                    ax.plot(xs[lv] + off, v, ls="", ms=5, **L.pt(uid))
                means.append(np.mean([v for v, _ in ys]) if ys else np.nan)
            ax.plot([xs[lv] + off for lv in L.levels], means, color=L.color[d], lw=1.2)
        ax.set_xticks(list(xs.values()))
        ax.set_xticklabels(L.levels)
        ax.set_title("%s%s" % (lb, SCALE_LABEL.get(r["scale"], "")))
        ax.margins(0.25, 0.15)
        ax.ticklabel_format(axis="y", useOffset=False)
    for ax in axes.ravel()[n:]:
        ax.axis("off")
    fig.suptitle("Each planned outcome on its analysis scale: tanks (points) and cell means (lines) by %s" % (L.wf or "block"),
                 x=0.01, ha="left", fontsize=9)
    fig.tight_layout()
    L.legend(fig, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=6)
    return fig


def fig_forest(p, L):
    out = [(lb, r) for lb, r in p["analysis"]["outcomes"].items()]
    fig, axes = plt.subplots(len(out), 1, figsize=(6.6, 0.82 * len(out) + 0.6), squeeze=False)
    for ax, (lb, r) in zip(axes[:, 0], out):
        c = [x for x in r["contrasts"] if x["term"] == "diet"][0]
        nat = c["natural"]
        null = 0.0 if nat["kind"] == "difference" else 1.0
        if nat["kind"] != "difference":
            ax.set_xscale("log")
        if nat["ci_low"] is not None:
            ax.plot([nat["ci_low"], nat["ci_high"]], [0, 0], color=INK, lw=1.6)
        ax.plot(nat["estimate"], 0, marker="o", color=INK, ms=6)
        tr = ((p["truth"] or {}).get("contrasts") or {}).get(lb, {}).get(c["label"])
        if tr is not None:
            ax.plot(tr if nat["kind"] == "difference" else math.exp(tr), 0.32, marker="x", color=PALETTE[1], ms=7, ls="")
        ax.axvline(null, color=GREY, lw=0.8, ls=":")
        ax.set_ylim(-0.6, 0.75)
        ax.set_yticks([])
        ax.spines["left"].set_visible(False)
        ax.set_title("%s: %s (%s)%s" % (lb, nat["kind"], c["label"].split(",")[0],
                                        "" if nat["ci_low"] is not None else " - " + (c["note"] or "no interval")), fontsize=8)
        vals = [v for v in (nat["ci_low"], nat["ci_high"], nat["estimate"], null) if v is not None]
        lo, hi = min(vals), max(vals)
        if nat["kind"] == "difference":
            pad = (hi - lo) * 0.15 or 0.5
            ax.set_xlim(lo - pad, hi + pad)
        else:
            ax.set_xlim(lo / (hi / lo) ** 0.15, hi * (hi / lo) ** 0.15)
            ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: "%g" % v))
            ax.xaxis.set_minor_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: "%g" % v))
            ax.tick_params(axis="x", which="both", labelsize=7)
    fig.suptitle("Diet effect per outcome: estimate and 95 % interval; dotted line = no effect"
                 + ("\nx = the same contrast computed from the generator's true tank values" if p["truth"] else ""),
                 x=0.01, ha="left", fontsize=9)
    fig.tight_layout()
    return fig


def fig_random(p, L):
    prim = [(lb, r) for lb, r in p["analysis"]["outcomes"].items() if r["plan"].get("role") == "primary"] or list(p["analysis"]["outcomes"].items())
    lb, r = prim[0]
    rt = r["randomisation_diet"]
    fig, ax = plt.subplots(figsize=(6.4, 1.9))
    if rt and rt.get("distribution"):
        d = rt["distribution"]
        obs = abs(rt["statistic"])
        ext = [v for v in d if abs(v) >= obs - 1e-12]
        ax.plot(d, [0] * len(d), ls="", marker="|", ms=22, color=GREY, mew=1.4)
        ax.plot(ext, [0] * len(ext), ls="", marker="|", ms=22, color=PALETTE[1], mew=2.2)
        ax.plot(rt["statistic"], 0.03, ls="", marker="v", color=INK, ms=7)
        ax.axvline(0, color=GREY, lw=0.8, ls=":")
    ax.set_yticks([])
    ax.set_ylim(-0.05, 0.06)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("diet effect that each possible allocation of diets to tanks would have given (%s)" % lb)
    ax.set_title("All %d equally likely allocations; orange = at least as extreme as the one observed (triangle)" % (rt["n_allocations"] if rt else 0))
    return fig


def fig_power(p, L):
    g = p["power"]["grid"]
    n = [x["n_systems_per_level"] for x in g]
    terms = [t for t in ("diet", "interaction") if t in g[0]]
    name = {"diet": "diet main effect", "interaction": "diet x %s" % (L.wf or "level")}
    col = {"diet": INK, "interaction": GREY}
    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(9.6, 3.0))
    for t in terms:
        a1.plot(n, [x[t]["power_at_mdd"] for x in g], marker="o", ms=4, color=col[t], label=name[t])
        a2.plot(n, [x[t]["mdd_80"] for x in g], marker="o", ms=4, color=col[t], label=name[t])
    a1.axhline(p["power"]["planned_power"], color=GREY, lw=0.8, ls=":")
    a1.set_ylim(0, 1.03)
    a1.set_ylabel("power at a true difference of %g" % p["power"]["planned_mdd"])
    a1.set_title("Power")
    a1.legend(loc="lower right")
    a2.set_ylabel("difference found with %d %% power" % round(100 * p["power"]["planned_power"]))
    a2.set_ylim(0, None)
    a2.set_title("Minimum detectable difference")
    a3.plot(n, [x["t_crit"] for x in g], marker="o", ms=4, color=INK)
    a3.axhline(p["power"]["t_crit_normal"], color=GREY, lw=0.8, ls=":")
    a3.text(n[-1], p["power"]["t_crit_normal"] - 0.12, "large-sample limit %.2f" % p["power"]["t_crit_normal"], ha="right", va="top",
            fontsize=7, color=GREY)
    a3.set_ylim(0, None)
    a3.set_ylabel("estimate / SE needed for p < %g" % p["power"]["alpha"])
    a3.set_title("Critical t value")
    for ax in (a1, a2, a3):
        ax.set_xlabel("systems per %s level" % (L.wf or "block"))
        ax.set_xticks(n)
        ax.axvline(p["power"]["n_run"], color=PALETTE[1], lw=0.9, ls="--")
    a1.text(p["power"]["n_run"] + 0.12, 0.04, "this trial", color=PALETTE[1], fontsize=8)
    fig.suptitle("What more systems would buy, at the planned SD between tanks of %g" % p["power"]["expected_sd"], x=0.01, ha="left", fontsize=9)
    fig.tight_layout()
    return fig


def fig_seeds(p, L):
    s = p["evidence"]["seeds"]
    eff = sorted(s["growth_diet_effect"], key=lambda r: r["estimate"])
    tv = s["true_growth"]["diet"]
    fig, ax = plt.subplots(figsize=(9.2, 3.0))
    for i, r in enumerate(eff):
        hit = r["ci_low"] <= tv <= r["ci_high"]
        ax.plot([i, i], [r["ci_low"], r["ci_high"]], color=GREY if hit else PALETTE[1], lw=0.9 if hit else 1.6)
        ax.plot(i, r["estimate"], marker=".", ms=3, color=INK)
    ax.axhline(tv, color=PALETTE[0], lw=1.2)
    ax.axhline(0, color=GREY, lw=0.8, ls=":")
    ax.set_xlabel("%d synthetic trials generated with different random seeds, sorted by estimate" % len(eff))
    ax.set_ylabel("diet effect on SGR (% per day)")
    ax.set_title("Estimate and 95 % interval of every trial; blue line = true effect; orange = interval misses it")
    ax.margins(0.01, 0.05)
    return fig


def fig_null(p, L):
    f = p["evidence"]["seeds"]["fish_null"]
    fig, ax = plt.subplots(figsize=(4.6, 3.6))
    for key, color, label in (("naive_p", PALETTE[1], "fish as replicates (invalid)"), ("correct_p", INK, "tank as replicate")):
        v = np.sort([r[key] for r in f])
        ax.step(np.concatenate([[0], v, [1]]), np.concatenate([[0], np.arange(1, len(v) + 1) / len(v), [1]]), where="post",
                color=color, label=label)
    ax.plot([0, 1], [0, 1], color=GREY, lw=0.8, ls=":")
    ax.axvline(0.05, color=GREY, lw=0.8, ls=":")
    ax.set_xlabel("p value of the diet test")
    ax.set_ylabel("share of trials with a p value this small or smaller")
    ax.set_title("%d trials with NO diet effect: a valid test follows the diagonal" % len(f))
    ax.legend(loc="lower right")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    return fig


def figures(p):
    _style()
    L = Look(p)
    figs = {"growth": fig_growth, "sgr": fig_sgr, "fish": fig_fish, "feed": fig_feed, "survival": fig_survival,
            "temperature": fig_temperature, "coverage": fig_coverage, "findings": fig_findings, "cells": fig_cells,
            "forest": fig_forest, "random": fig_random}
    if p["power"]:
        figs["power"] = fig_power
    if p["evidence"] and "growth_diet_effect" in (p["evidence"].get("seeds") or {}):
        figs["seeds"], figs["null"] = fig_seeds, fig_null
    return {k: f(p, L) for k, f in figs.items()}


def svg(fig, key=""):
    """Figure -> inline SVG that scales with the page; identical bytes for identical data.
    key prefixes every id of the drawing, so that the charts of one page do not share ids."""
    buf = io.StringIO()
    fig.savefig(buf, format="svg", metadata={"Date": None, "Creator": None}, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    s = buf.getvalue()
    s = s[s.index("<svg"):]
    m = re.search(r'width="([\d.]+)pt" height="([\d.]+)pt"', s)
    w = float(m.group(1)) * 96 / 72
    if key:
        s = re.sub(r'\bid="([^"]+)"', lambda g: 'id="%s-%s"' % (key, g.group(1)), s)
        s = re.sub(r'href="#([^"]+)"', lambda g: 'href="#%s-%s"' % (key, g.group(1)), s)
        s = re.sub(r"url\(#([^)]+)\)", lambda g: "url(#%s-%s)" % (key, g.group(1)), s)
    return re.sub(r'width="[\d.]+pt" height="[\d.]+pt"', 'style="width:100%%;max-width:%dpx;height:auto"' % round(w), s, count=1)


# ---------------------------------------------------------------- page
CSS = """
:root{--ink:#222;--mut:#5f6770;--line:#d9dde2;--bg:#f6f7f9;--card:#fff;--d0:#4477AA;--d1:#EE7733;--d2:#228833;--d3:#AA3377;--warn:#CCBB44;--err:#CC3311}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font:15px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{background:#1f2a36;color:#fff;padding:22px 28px 18px}header h1{margin:0 0 4px;font-size:24px}header p{margin:0;color:#c9d2dc}
nav{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:8px 28px;display:flex;flex-wrap:wrap;gap:4px 18px}
nav a{color:#1f4e79;text-decoration:none;font-weight:600;font-size:14px}nav a:hover{text-decoration:underline}
main{max-width:1180px;margin:0 auto;padding:8px 28px 60px}
section{scroll-margin-top:52px;margin-top:34px}h2{font-size:21px;border-bottom:2px solid var(--line);padding-bottom:6px;margin:0 0 14px}
h3{font-size:16.5px;margin:22px 0 8px}h4{font-size:14.5px;margin:14px 0 6px}
p{margin:8px 0}.lead{font-size:16px}.mut{color:var(--mut)}.small{font-size:13px}
.banner{background:#fff7e0;border:1px solid #e6cf7a;border-radius:6px;padding:10px 14px;margin:16px 0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(205px,1fr));gap:12px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .k{font-size:12.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}.card .v{font-size:25px;font-weight:650;line-height:1.25}
.card .s{font-size:13px;color:var(--mut)}
.box{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:14px 0}
.fig{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:14px 0}.fig svg{display:block}
.fig figcaption,.cap{font-size:13px;color:var(--mut);margin-top:6px}figure{margin:0}
table{border-collapse:collapse;width:100%;font-size:13.5px;background:var(--card)}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{background:#eef1f4;font-weight:600}td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:8px;margin:10px 0}.tw table{border:0}
code{font:12.5px/1.4 ui-monospace,Consolas,Menlo,monospace;background:#eef1f4;padding:1px 4px;border-radius:3px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:0 14px}
details>summary{cursor:pointer;font-weight:600;padding:10px 0}details[open]>summary{border-bottom:1px solid var(--line);margin-bottom:8px}
details>*:last-child{margin-bottom:12px}
.tag{display:inline-block;font-size:12px;font-weight:600;border-radius:10px;padding:1px 9px;border:1px solid var(--line);background:#eef1f4;white-space:nowrap}
.tag.ok{background:#e3f1e6;border-color:#9cc7a5}.tag.no{background:#fbe5e0;border-color:#e2a495}.tag.warn{background:#fbf4cf;border-color:#d9c96a}
.design{display:flex;flex-wrap:wrap;gap:14px}.lvl{border:1px dashed #9aa3ad;border-radius:8px;padding:8px 10px;background:#fff}
.lvl>b{display:block;margin-bottom:6px}.sys{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin:0 6px 6px 0;background:#f6f7f9}
.sys>span{display:block;font-size:12.5px;color:var(--mut);margin-bottom:4px}
.tank{display:inline-block;color:#fff;border-radius:5px;padding:3px 8px;margin-right:4px;font-size:13px;font-weight:600}
.tank.pend{background:#fff!important;border:2px solid}.swatch{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.n{font-variant-numeric:tabular-nums}.na{color:var(--mut)}ul,ol{margin:6px 0 6px 0;padding-left:22px}li{margin:3px 0}
footer{max-width:1180px;margin:0 auto;padding:0 28px 40px;color:var(--mut);font-size:13px}
@media print{nav{position:static}details{break-inside:avoid}.fig{break-inside:avoid}}
"""


def esc(s):
    return html.escape(str(s), quote=True)


class Page:
    """HTML builder. Every computed number goes through n() or p(): the span keeps the unrounded
    value (data-v) and the format (data-f), so the printed text can be checked against the data."""

    def __init__(self):
        self.out = []

    def add(self, s):
        self.out.append(s)

    @staticmethod
    def n(v, fmt="%.3g"):
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return '<span class="na">&ndash;</span>'
        return '<span class="n" data-v="%r" data-f="%s">%s</span>' % (v, esc(fmt), esc(fmt % v))

    @staticmethod
    def p(v):
        """p value: 3 decimals, 4 below 0.01, '< 0.0001' below that."""
        if v is None:
            return '<span class="na">&ndash;</span>'
        if v < 0.0001:
            return '<span class="n" data-v="%r" data-f="p&lt;">&lt; 0.0001</span>' % v
        fmt = "%.4f" if v < 0.01 else "%.3f"
        return '<span class="n" data-v="%r" data-f="%s">%s</span>' % (v, fmt, fmt % v)

    @staticmethod
    def peq(v):
        """'p = 0.054' or 'p < 0.0001' for running text."""
        return ("p " if v is not None and v < 0.0001 else "p = ") + Page.p(v)

    def table(self, head, rows, right=()):
        h = "".join('<th%s>%s</th>' % (' class="r"' if i in right else "", c) for i, c in enumerate(head))
        b = "".join("<tr>%s</tr>" % "".join('<td%s>%s</td>' % (' class="r"' if i in right else "", c) for i, c in enumerate(r)) for r in rows)
        self.add('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (h, b))

    def fig(self, svgs, key, caption):
        if key in svgs:
            self.add('<figure class="fig" id="fig-%s">%s<figcaption>%s</figcaption></figure>' % (key, svgs[key], caption))

    def html(self):
        return "".join(self.out)


def _diet(c):
    return [x for x in c if x["term"] == "diet"][0]


def reading(c, alpha):
    """One plain phrase for a contrast; a Holm-adjusted p value, when there is one, decides."""
    if c["p"] is None:
        return esc(c["note"] or "no test")
    a = Page.n(100 * alpha, "%g")
    if c.get("p_adjust_method") == "holm":
        if c["p_adjusted"] < alpha:
            return "a difference at the %s %% level, also after Holm adjustment" % a
        return ("not shown at the %s %% level after Holm adjustment (unadjusted p is below it)" % a) if c["p"] < alpha else (
            "not shown at the %s %% level" % a)
    return ("a difference at the %s %% level" % a) if c["p"] < alpha else ("not shown at the %s %% level" % a)


UNIT_OF = {"sgr_pct_day": "% per day", "adc_pct": "percentage points", "tgc": "TGC units"}
WHAT = {
    "growth_sgr": "Does the diet change the growth rate of a tank over the trial?",
    "intake_log": "Does the diet change how much dry feed a tank eats?",
    "gain_log": "Does the diet change the biomass a tank gains (fish removed or dead counted in)?",
    "feed_per_gain_log": "Does the diet change the dry feed eaten per kg of gain (biological FCR)?",
    "survival_logit": "Does the diet change the share of fish that survive?",
    "adc_protein": "Does the diet change the apparent digestibility of protein?",
}


class Ctx:
    """What every section needs, computed once."""

    def __init__(self, p, svgs):
        self.p, self.svgs = p, svgs
        self.res, self.des, self.d = p["analysis"], p["analysis"]["design"], p["derived"]
        self.L = Look(p)
        self.out = self.res["outcomes"]
        prim = [lb for lb, r in self.out.items() if r["plan"].get("role") == "primary"]
        self.prim_lb = prim[0] if prim else list(self.out)[0]
        self.prim = self.out[self.prim_lb]
        self.pc = _diet(self.prim["contrasts"])
        self.alpha = self.prim["alpha"]
        self.wf = self.des["whole_factor"]
        self.nlev = len(self.des["whole_levels"])
        self.nper = min(self.des["n_systems_per_level"].values())
        self.tr = p["truth"]
        self.ref, self.test = self.des["reference_diet"], [x for x in self.des["diets"] if x != self.des["reference_diet"]][0]
        self.title = p["trial"].get("title") or p["trial"]["trial_id"]

    def sw(self, dd):
        return '<span class="swatch" style="background:%s"></span>' % self.L.color[dd]


SECTIONS = [("overview", "Overview"), ("design", "Design and plan"), ("quality", "Data quality"), ("growth", "Growth"), ("feed", "Feed"),
            ("survival", "Survival and environment"), ("statistics", "Statistics"), ("evidence", "Evidence"), ("limits", "Limits")]
UNPACK = "p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf"


def sec_head(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    prim_lb, prim, pc, alpha, nlev, nper, title, sw = c.prim_lb, c.prim, c.pc, c.alpha, c.nlev, c.nper, c.title, c.sw
    P.add('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
          '<title>%s - feed trial dashboard</title><style>%s</style></head><body>' % (esc(title), CSS))
    P.add('<header><h1>%s</h1><p>Trial %s &middot; %s &middot; %s to %s &middot; analysis plan %s</p></header>' % (
        esc(title), esc(p["trial"]["trial_id"]), esc((p["trial"].get("system_type") or "").upper()), esc(p["trial"]["start_date"]),
        esc(p["trial"]["end_date"]), esc(res["plan_id"] or "none")))
    P.add("<nav>%s</nav><main>" % "".join('<a href="#%s">%s</a>' % s for s in SECTIONS))



def sec_overview(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    prim_lb, prim, pc, alpha, nlev, nper, title, sw = c.prim_lb, c.prim, c.pc, c.alpha, c.nlev, c.nper, c.title, c.sw
    # ------------------------------------------------------------ overview
    P.add('<section id="overview"><h2>Overview</h2>')
    if p["synthetic"]:
        P.add('<div class="banner"><b>Sample data.</b> This trial is generated by <code>tools/synthetic_trial.py</code> (seed %s) from known '
              'true effects. It is not a real experiment. Because the truth is known, every estimate on this page can be compared with it; '
              'where a result is a property of the simulator rather than of salmon, the page says so.</div>' % n(p["seed"], "%d"))
    P.add('<p class="lead">%s diets were compared in %s tanks%s. The tank is the experimental unit, so every test below uses one number per tank.</p>' % (
        n(len(des["diets"]), "%d"), n(d["n_units"], "%d"),
        (" in %s RAS systems, %s per %s level" % (n(d["n_systems"], "%d"), n(nper, "%d"), esc(wf))) if wf else ""))
    P.add('<div class="cards">')
    card = lambda k, v, s="": P.add('<div class="card"><div class="k">%s</div><div class="v">%s</div><div class="s">%s</div></div>' % (k, v, s))  # noqa: E731
    card("Experimental units", n(d["n_units"], "%d"), "tanks, %s fish at d0" % n(d["n_fish_d0"], "%d"))
    card("Design", "%s &times; %s" % (n(nlev, "%d"), n(len(des["diets"]), "%d")) if wf else n(len(des["diets"]), "%d"),
         ("%s levels &times; diets, split plot" % esc(wf)) if wf else "diets in blocks")
    card("Duration", "%s days" % n(d["trial_days"], "%d"), "%s to %s" % (esc(p["trial"]["start_date"]), esc(p["trial"]["end_date"])))
    fs = p["findings"]["summary"]
    card("Rule findings", "%s errors" % n(fs.get("error", 0), "%d"), "%s warnings, %s info &middot; %s rules checked" % (
        n(fs.get("warning", 0), "%d"), n(fs.get("info", 0), "%d"), n(p["model"]["n_validator_rules"], "%d")))
    card("Data", "%s rows" % n(p["tables"]["n_rows"], "%d"), "in %s of %s tables" % (n(p["tables"]["n_filled"], "%d"), n(p["tables"]["n_tables"], "%d")))
    if p["chain"]:
        card("Proof chain", "%s / %s" % (n(d["chain_ok"], "%d"), n(d["chain_n"], "%d")), "steps passed in the last run")
    if p["evidence"]:
        card("Analysis checks", "%s / %s" % (n(p["evidence"]["n_checks"] - p["evidence"].get("n_failed", 0), "%d"), n(p["evidence"]["n_checks"], "%d")),
             "passed; %s of %s analyses covered" % (n(len(p["evidence"]["exercised"]), "%d"), n(p["model"]["n_analyses"], "%d")))
    P.add("</div>")
    unit = UNIT_OF.get(prim["plan"]["outcome"], "")
    P.add('<div class="box"><h3 style="margin-top:0">Primary outcome: %s</h3><p>%s %s &minus; %s = <b>%s</b> %s (95 %% interval %s to %s), '
          'p = <b>%s</b> with %s error degrees of freedom: %s.</p>' % (
              esc(prim_lb), esc(WHAT.get(prim_lb, "")), esc(c.test), esc(des["reference_diet"]),
              n(pc["estimate"], "%.3f"), unit, n(pc["ci_low"], "%.3f"), n(pc["ci_high"], "%.3f"), pv(pc["p"]), n(pc["df"], "%d"),
              reading(pc, alpha)))
    if tr and prim_lb == "growth_sgr":
        P.add('<p>The generator\'s true effect is %s %% per day; the estimate lies %s standard errors from it and the interval %s it. What the '
              'trial lacks is precision: with %s systems per %s level the standard error rests on %s degrees of freedom, and an estimate must exceed '
              '%s standard errors (not about 2) to count as significant. See <a href="#statistics">Statistics</a> for each test and '
              '<a href="#power">Power and design</a> for what more systems would change.</p>' % (
                  n(tr["generator"]["sgr_diet_main"], "%.3f"), n(d["primary_truth_distance_se"], "%.2f"),
                  "contains" if pc["ci_low"] <= tr["generator"]["sgr_diet_main"] <= pc["ci_high"] else "does not contain",
                  n(nper, "%d"), esc(wf), n(pc["df"], "%d"), n(d["outcomes"][prim_lb]["t_crit"], "%.2f")))
    P.add("</div>")
    P.add("<h3>All pre-registered outcomes</h3>")
    rows = []
    for lb, r in out.items():
        dc = _diet(r["contrasts"])
        nat = dc["natural"]
        rows.append([esc(lb), esc(r["plan"].get("role", "")), "%s %s" % (esc(nat["kind"]), n(nat["estimate"], "%.3f")),
                     "%s to %s" % (n(nat["ci_low"], "%.3f"), n(nat["ci_high"], "%.3f")) if nat["ci_low"] is not None else '<span class="na">&ndash;</span>',
                     pv(dc["p"]), pv(dc.get("p_adjusted")) if dc.get("p_adjust_method") == "holm" else '<span class="na">&ndash;</span>',
                     reading(dc, r["alpha"])])
    P.table(["Outcome", "Role", "Diet effect (%s vs %s)" % (esc(c.test), esc(des["reference_diet"])),
             "95 % interval", "p", "Holm-adjusted p", "Reading"], rows, right=(2, 3, 4, 5))
    P.add('<p class="cap">Difference = on the unit of the outcome; ratio and odds ratio = 1 means no effect. Holm adjustment applies within the '
          'family of secondary outcomes. "Not shown" means the data are compatible with no effect and with the effects inside the interval; '
          'it does not mean the diets are equal.</p></section>')



def sec_design(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    prim_lb, prim, pc, alpha, nlev, nper, title, sw = c.prim_lb, c.prim, c.pc, c.alpha, c.nlev, c.nper, c.title, c.sw
    # ------------------------------------------------------------ design and plan
    P.add('<section id="design"><h2>Design and plan</h2><div class="design">')
    for lv in des["whole_levels"]:
        P.add('<div class="lvl"><b>%s</b>' % (("%s %s" % (esc(wf), esc(lv))) if wf else "systems (blocks)"))
        for s in p["systems"]:
            if s["level"] != lv:
                continue
            P.add('<div class="sys"><span>system %s</span>' % esc(s["system_id"]))
            for u in s["units"]:
                col = L.color[u["diet_id"]]
                P.add('<span class="tank%s" style="background:%s;%s" title="diet %s">%s</span>' % (
                    " pend" if u["pending"] else "", col, ("color:%s;border-color:%s" % (col, col)) if u["pending"] else "", esc(u["diet_id"]), esc(u["unit_id"])))
            P.add("</div>")
        P.add("</div>")
    P.add('</div><p class="cap">%s%s</p>' % (" ".join("%sdiet %s%s" % (sw(dd), esc(dd), " (reference)" if dd == des["reference_diet"] else "") for dd in L.diets),
                                            " &middot; outlined tank = exclusion pending" if d["n_pending"] else ""))
    P.add("<ul>")
    if wf:
        P.add("<li><b>%s</b> was assigned to whole systems: the system is the unit for %s (%s systems, %s per level).</li>" % (
            esc(wf.capitalize()), esc(wf), n(d["n_systems"], "%d"), n(nper, "%d")))
    P.add("<li><b>Diet</b> was assigned to the tanks inside each system: the tank is the unit for diet, and each system is a block that holds every diet once.</li>")
    for r_ in p["randomisations"]:
        P.add("<li>Randomisation <code>%s</code>: %s, method %s%s, on %s, seed %s, allocation concealed: %s.</li>" % (
            esc(r_["randomisation_id"]), esc(r_["scope"].replace("_", " ")), esc(r_["method"]),
            (" within " + esc(r_["strata"])) if r_.get("strata") else "", esc(r_.get("date") or "?"), esc(r_.get("seed")),
            "yes" if r_.get("allocation_concealed") else "no"))
    t = p["trial"]
    P.add("<li>Blinding recorded for feeders: %s, assessors: %s, analysts: %s; unblinded at %s.</li>" % tuple(
        ["yes" if t.get(k) else "no" for k in ("blinding_feeders", "blinding_assessors", "blinding_analysts")] + [esc(t.get("unblinded_at") or "not recorded")]))
    for pl in p["plan"]:
        P.add("<li>Analysis plan <code>%s</code> version %s, frozen %s (sha256 <code>%s&hellip;</code>).</li>" % (
            esc(pl["plan_id"]), esc(pl["version"]), esc(pl.get("frozen_at") or "?"), esc((pl.get("file_hash") or "")[:12])))
    for r_ in p["locked_runs"]:
        if r_["purpose"] == "primary_locked":
            P.add("<li>Primary analysis locked at %s%s (rule R38).</li>" % (esc(r_["run_at"]), (
                ", before unblinding" if t.get("unblinded_at") and r_["run_at"] <= t["unblinded_at"] else ", AFTER unblinding")))
    P.add("</ul><h3>Planned outcomes</h3>")
    P.table(["Label", "Metric", "Scale", "Role", "Multiplicity", "Model (as registered)", "Error strata"],
            [[esc(lb), "<code>%s</code>" % esc(r["plan"]["outcome"]), esc(r["scale"]), esc(r["plan"].get("role", "")),
              esc(r["plan"].get("multiplicity_method") or "none"), "<code>%s</code>" % esc(r["plan"].get("model") or ""),
              esc(r["plan"].get("error_strata") or "")] for lb, r in out.items()])
    if p["deviations"]:
        P.add("<h3>Deviations</h3>")
        P.table(["Record", "Date", "Tank", "Category", "Description", "Decision"],
                [[esc(x["record_id"]), esc(x["date"]), esc(x["unit_id"] or "-"), esc(x["category"]), esc(x["description"] or ""),
                  ("decided by %s on %s" % (esc(x["decided_by"]), esc(x["decided_date"]))) if x["decided_by"] else '<span class="tag warn">pending</span>']
                 for x in p["deviations"]])
        P.add('<p class="cap">A pending exclusion stays in every analysis population; a sensitivity analysis removes it (see Statistics).</p>')
    P.add("</section>")


def sec_quality(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    fs = p["findings"]["summary"]
    P.add('<section id="quality"><h2>Data quality</h2>')
    P.add('<p>The rule engine (<code>tools/validate.py</code>) checked %s cross-table rules and reported %s error, %s warning and %s info finding(s). '
          'An error blocks the analysis data set; %s.</p>' % (
              n(p["model"]["n_validator_rules"], "%d"), n(fs.get("error", 0), "%d"), n(fs.get("warning", 0), "%d"), n(fs.get("info", 0), "%d"),
              "there is none, so the analysis proceeds" if not fs.get("error") else "<b>the results below must not be used until they are resolved</b>"))
    P.fig(c.svgs, "findings", "Findings by rule. Warnings are reported with their consequence and do not stop the analysis.")
    P.table(["Rule", "Severity", "Findings", "Tanks", "What the rule requires", "Example finding"],
            [[esc(g["rule"]), '<span class="tag %s">%s</span>' % ({"error": "no", "warning": "warn"}.get(g["severity"], ""), esc(g["severity"])),
              n(g["n"], "%d"), esc(", ".join(sorted(set(g["units"]))) or "-"), esc(g["rule_text"][:230] + ("..." if len(g["rule_text"]) > 230 else "")),
              esc(g["example"][:200] + ("..." if len(g["example"]) > 200 else ""))] for g in p["findings"]["groups"]], right=(2,))
    P.fig(c.svgs, "coverage", "Period totals follow one rule: a missing day is filled only where that is physically defensible (temperature from the "
          "system reference sensor, uneaten feed from the tank's own ratio), and the number of filled days is always reported.")
    P.add("<h3>Tables with data</h3><p class=\"small\">%s</p></section>" % ", ".join(
        "%s&nbsp;(%s)" % (esc(t), n(k, "%d")) for t, k in sorted(p["tables"]["rows"].items(), key=lambda kv: -kv[1])))


def _unit_rows(c, cols):
    """One table row per tank in design order; cols = [(header, key or function, format)]."""
    L, n = c.L, Page.n
    rows = []
    for uid in L.order:
        u = L.units[uid]
        r = ['%s%s' % (c.sw(u["diet_id"]), esc(uid)), esc(u["system_id"])] + ([esc(u["whole_level"])] if c.wf else []) + [esc(u["diet_id"])]
        for _, key, fmt in cols:
            v = key(u) if callable(key) else u.get(key)
            r.append(n(v, fmt))
        rows.append(r)
    head = ["Tank", "System"] + ([esc(c.wf)] if c.wf else []) + ["Diet"] + [h for h, _, _ in cols]
    return head, rows, tuple(range(len(head) - len(cols), len(head)))


def sec_growth(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    P.add('<section id="growth"><h2>Growth</h2>')
    P.fig(c.svgs, "growth", "One line per tank from the count weighings at d0 and at the end and the sample weighing in between.")
    P.fig(c.svgs, "sgr", "Specific growth rate = 100 &times; (ln W<sub>end</sub> &minus; ln W<sub>start</sub>) / days. "
          "It is compared between diets within a system; the data model warns against comparing SGR across temperatures as a measure of diet quality.")
    cols = [("Fish d0", "n_fish_d0", "%d"), ("Fish end", "n_end", "%d"), ("W start (g)", "w_start_g", "%.1f"), ("W end (g)", "w_end_g", "%.1f"),
            ("SGR (%/day)", "sgr_pct_day", "%.3f")]
    if tr:
        cols.append(("True SGR", lambda u: tr["units"][u["unit_id"]]["sgr_true_pct"], "%.3f"))
    cols += [("Mean temp (&deg;C)", "temp_mean_c", "%.2f"), ("Degree-days", "degree_days", "%.0f"), ("TGC", "tgc", "%.3f"),
             ("Gain (kg)", "biomass_gain_kg", "%.2f")]
    P.table(*_unit_rows(c, cols))
    P.add('<p class="cap">TGC (thermal growth coefficient) is shown as a descriptor only; inference is done with the models in Statistics. '
          'Gain adds back the biomass of fish that died or were sampled.</p>')
    P.fig(c.svgs, "fish", "Individual weights from the final sample. The spread inside a tank is real, but it is not the yardstick for a diet effect: "
          "tanks differ from each other beyond what their fish explain, and the diets were allocated to tanks.")
    P.add("</section>")


def sec_feed(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    P.add('<section id="feed"><h2>Feed</h2>')
    P.fig(c.svgs, "feed", "Left: feed given per tank and day. Right: biological FCR = dry-matter feed eaten / wet gain, per tank.")
    cols = [("Feed given (g)", "feed_given_g", "%.0f"), ("Intake DM (g)", "intake_dm_g", "%.0f"), ("Recovery", "recovery", "%.3f"),
            ("FCR gross", "fcr_gross", "%.3f"), ("FCR biological", "fcr_biological", "%.3f")]
    if tr:
        cols.append(("True FCR", lambda u: tr["units"][u["unit_id"]]["fcr_bio"], "%.3f"))
    cols += [("Protein intake (g)", "protein_intake_g", "%.0f"), ("PER", "per", "%.2f"), ("Protein retention (%)", "retention_protein_pct", "%.1f"),
             ("ADC protein (%)", "adc_protein_pct", "%.2f")]
    P.table(*_unit_rows(c, cols))
    P.add('<p class="cap">Intake = feed given on a dry-matter basis minus uneaten feed corrected for the recovery of the collector (recovery tests per pellet size). '
          'FCR gross uses feed given and the change in standing biomass; FCR biological uses intake and gain including removed fish. '
          'PER = gain per g protein eaten; ADC = apparent digestibility coefficient from the inert marker.</p></section>')


def sec_survival(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    P.add('<section id="survival"><h2>Survival and environment</h2>')
    P.fig(c.svgs, "survival", "Deaths and moribund culls from d0 to the final weighing; fish removed for sampling are censored, not counted as deaths.")
    cols = [("Fish d0", "n_fish_d0", "%d"), ("Deaths", lambda u: sum(k for _, k in p["per_unit"][u["unit_id"]]["deaths"]), "%d"),
            ("Survival (%)", "survival_pct", "%.1f")]
    if tr:
        cols.append(("True survival (%)", lambda u: tr["units"][u["unit_id"]]["survival_pct"], "%.1f"))
    cols += [("Temp days observed", lambda u: (p["per_unit"][u["unit_id"]]["coverage"]["degree_days_coverage"] or {}).get("n_observed"), "%d"),
             ("Temp days filled", lambda u: (p["per_unit"][u["unit_id"]]["coverage"]["degree_days_coverage"] or {}).get("n_imputed"), "%d")]
    P.table(*_unit_rows(c, cols))
    P.fig(c.svgs, "temperature", "Measured temperature is a descriptor of each system; the models use the assigned level%s." % (
        (" of " + esc(wf)) if wf else ""))
    P.add("</section>")
def _explain(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    pc, alpha, nper = c.pc, c.alpha, c.nper
    df = pc["df"]
    tcrit = d["outcomes"][c.prim_lb]["t_crit"]
    det = lambda title, body, open_=False: P.add('<details%s><summary>%s</summary>%s</details>' % (" open" if open_ else "", title, body))  # noqa: E731
    det("1. The experimental unit is the tank, never the fish",
        "<p>Diets were allocated to tanks. Fish of one tank share water, feed and each other, so they are not independent observations of a diet. "
        "Every test therefore uses one number per tank (%s tanks). Weighing more fish makes a tank's number more precise; it does not add "
        "replicates. What happens when this is ignored is shown under <a href=\"#fishunit\">The analysis not to run</a>.</p>" % n(d["n_units"], "%d"), True)
    if wf:
        det("2. Two layers of allocation, two error terms (split plot)",
            "<p>%s was set per system, diet per tank inside a system. The analysis keeps the two layers apart:</p><ul>"
            "<li><b>%s</b> is compared with the variation between systems kept at the same level. There are %s systems and %s levels, so this "
            "yardstick has %s &minus; %s = %s degrees of freedom.</li>"
            "<li><b>Diet</b> and <b>diet &times; %s</b> use the difference between the two diets inside each system. The system itself cancels out of "
            "that difference. The yardstick is how much the difference varies between systems of the same level: again %s degrees of freedom.</li></ul>"
            "<p>Testing %s against tanks would treat the tanks of one system as independent, the same mistake as treating fish as replicates, one "
            "level up. Rule R35 of the data model checks every planned model for this, and the plan of this trial names <code>system</code> as an "
            "error stratum.</p>" % (esc(wf.capitalize()), esc(wf.capitalize()), n(d["n_systems"], "%d"), n(c.nlev, "%d"), n(d["n_systems"], "%d"),
                                    n(c.nlev, "%d"), n(df, "%d"), esc(wf), n(df, "%d"), esc(wf)), True)
    det("3. What each test computes: t, F, p and the 95 % interval",
        "<p>Each test compares an estimated difference with its standard error (SE), the amount by which the estimate would vary between "
        "repetitions of the trial: <b>t = estimate / SE</b>. For a factor with two levels the F ratio of the ANOVA table is t&sup2;.</p><ul>"
        "<li><b>Null hypothesis:</b> the true difference is 0 (the ratio is 1).</li>"
        "<li><b>p value:</b> the probability, if the null hypothesis were true and the assumptions hold, of a t at least as far from 0 as the one "
        "observed. It is not the probability that the null hypothesis is true.</li>"
        "<li><b>Decision rule fixed in the plan:</b> two-sided, alpha = %s. A p below alpha is called significant.</li>"
        "<li><b>95 %% interval:</b> estimate &plusmn; t<sub>crit</sub> &times; SE, the true differences that are compatible with the data. "
        "An interval that contains 0 (or ratio 1) goes with a p above alpha.</li></ul>" % n(alpha, "%g"))
    if df > 0 and "ci_width_factor" in d:
        nxt = ""
        if p["power"]:
            g = {x["n_systems_per_level"]: x for x in p["power"]["grid"]}
            more = [k for k in sorted(g) if k > nper][:2]
            nxt = (" The multiplier falls to " + " and ".join("%s with %s systems per level" % (n(g[k]["t_crit"], "%.2f"), n(k, "%d")) for k in more)
                   + ".") if more else ""
        det("4. Why %s degrees of freedom dominate every result here" % n(df, "%d"),
            "<p>The SE is itself estimated, here from only %s independent pieces of information. The t distribution compensates by asking for more: "
            "an estimate must exceed %s &times; SE to be significant, instead of %s &times; SE with a well-estimated SE. An interval built on these "
            "degrees of freedom is therefore %s times as wide as it would be with the same SE known exactly.%s</p>"
            "<p>This is a property of the design, fixed before any fish was weighed. No analysis method recovers the missing information.</p>" % (
                n(df, "%d"), n(tcrit, "%.2f"), n(d["t_crit_normal"], "%.2f"), n(d["ci_width_factor"], "%.1f"), nxt))
    det("5. Scales: difference, ratio, odds ratio",
        "<ul><li><b>raw</b>: the contrast is a difference in the unit of the metric.</li>"
        "<li><b>log</b>: the natural log of the tank value is analysed; a difference of logs is a ratio, so exp(estimate) is reported: 1.18 means "
        "18 % more, 0.95 means 5 % less. Totals such as feed and gain vary in proportion to their size, which the log scale makes additive.</li>"
        "<li><b>logit</b> (survival): ln((alive + 0.5) / (dead + 0.5)) per tank from the d0 count; the 0.5 keeps a tank without deaths finite. "
        "exp(estimate) is an odds ratio of surviving.</li></ul>")
    if "feed_identity" in d:
        fi = d["feed_identity"]
        det("6. Feed conversion without analysing a ratio",
            "<p>The data model's rule for FCR is: fit feed and gain, do not analyse the ratio. Feed eaten (<code>intake_log</code>) and gain "
            "(<code>gain_log</code>) are analysed separately. On the log scale feed per gain is their difference, so its diet contrast is exactly "
            "the intake contrast minus the gain contrast: %s &minus; %s = %s, and <code>feed_per_gain_log</code> reports %s. Taken per tank, the "
            "difference keeps the link between a tank's feed and its own gain, which two separate tests cannot use. The raw FCR ratio is never "
            "put into a test.</p>" % (n(fi["intake_log"], "%.4f"), n(fi["gain_log"], "%.4f"), n(fi["intake_minus_gain"], "%.4f"), n(fi["feed_per_gain_log"], "%.4f")))
    rd = c.prim["randomisation_diet"]
    if rd:
        det("7. Randomisation test: the same question without assuming a distribution",
            "<p>The diets were allocated at random to the two tanks of each system. If diet changed nothing in any tank, swapping the two labels "
            "inside a system would have been just as likely. With %s systems there are %s equally likely allocations; the p value is the share of "
            "them whose diet effect is at least as large as the one observed. Nothing is assumed about the distribution of the data. The price: "
            "p cannot go below %s here%s. See <a href=\"#randomisation\">Randomisation test</a>.</p>" % (
                n(rd["n_systems"], "%d"), n(rd["n_allocations"], "%d"), n(rd["min_p"], "%.3f"),
                ", so this test can never reach significance in this design, whatever the data" if rd["min_p"] > alpha else ""))
    if any(m["method"] == "holm" for m in res["multiplicity"]):
        k = max(m["n_tests_in_family"] for m in res["multiplicity"] if m["method"] == "holm")
        det("8. Several outcomes: Holm adjustment",
            "<p>Testing %s secondary outcomes at %s %% each would give a false positive somewhere more often than %s %% of the time. The plan "
            "names the Holm method for this family: the smallest p is multiplied by %s, the next by %s, and so on, never letting an adjusted value "
            "fall below the one before it. Calling only adjusted p values below alpha significant keeps the chance of any false positive in the "
            "family at %s %% at most. The primary outcome stands alone and is not adjusted.</p>" % (
                n(k, "%d"), n(100 * alpha, "%g"), n(100 * alpha, "%g"), n(k, "%d"), n(k - 1, "%d"), n(100 * alpha, "%g")))
    if d["n_pending"]:
        det("9. Sensitivity analysis: the pending exclusion",
            "<p>An exclusion that nobody has decided yet stays in the primary analysis (intention to treat). A sensitivity analysis repeats the "
            "contrasts without that tank. Its system then has no within-system diet difference and drops out, which leaves fewer degrees of freedom. "
            "The result is reported for every outcome so that the reader can see whether the conclusion depends on that tank.</p>")
    cov = ""
    if "seeds" in d:
        s = d["seeds"]
        cov = (" In %s synthetic trials whose tank noise is uniform, not normal, the 95 %% interval covered the true effect %s times (%s %%), and "
               "in %s trials with no diet effect the test rejected %s times." % (n(s["n_effect"], "%d"), n(s["coverage_n"], "%d"), n(s["coverage_pct"], "%.0f"),
                                                                                    n(s["n_null"], "%d"), n(s["reject_null"], "%d")))
    det("10. Assumptions, and which of them could be checked",
        "<ul><li><b>Independent units:</b> holds by design when allocation was random; the randomisation runs are recorded in the database "
        "(see Design and plan).</li>"
        "<li><b>Normal errors with equal variance:</b> cannot be assessed with %s error degrees of freedom. No residual plot or normality test "
        "has any power here, so none is shown. Two things stand in for it: the randomisation test (no distribution assumed), and the simulation "
        "evidence.%s</li>"
        "<li><b>No interference between the tanks of a system:</b> tanks on one loop share water, so one diet could reach the neighbouring tank "
        "through water quality. The design assumes it does not; these data cannot show whether it did.</li></ul>" % (n(df, "%d"), cov))
    if p["power"]:
        pw = p["power"]
        g0 = [x for x in pw["grid"] if x["n_systems_per_level"] == pw["n_run"]][0]
        det("11. 'Not significant' is not 'no effect'",
            "<p>The design was planned to find a diet difference of %s with %s %% power, given an SD between tanks of %s. Smaller true differences "
            "will usually not reach significance, and for the diet &times; %s interaction the smallest difference found with that power is %s. "
            "A non-significant result therefore says the trial could not tell; the interval says what remains possible.</p>" % (
                n(pw["planned_mdd"], "%g"), n(100 * pw["planned_power"], "%.0f"), n(pw["expected_sd"], "%g"), esc(wf or "level"),
                n(g0.get("interaction", g0["diet"])["mdd_80"], "%.2f")))


def _outcome(P, c, lb, r):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    plan, dc = r["plan"], _diet(r["contrasts"])
    f = "%.4f" if max(abs(x["estimate"]) for x in r["contrasts"]) < 10 else "%.2f"
    P.add('<details%s id="o-%s"><summary>%s &middot; %s &middot; diet effect %s %s, %s</summary>' % (
        " open" if lb == c.prim_lb else "", esc(lb), esc(lb), esc(plan.get("role", "")), esc(dc["natural"]["kind"]), n(dc["natural"]["estimate"], "%.3f"),
        Page.peq(dc["p"]) if dc["p"] is not None else "no test"))
    P.add("<p><b>Question.</b> %s</p><p><b>Registered model.</b> <code>%s</code> &middot; metric <code>%s</code>%s on the %s scale &middot; error strata: %s "
          "&middot; estimand: %s effect &middot; alpha %s, two-sided.</p>" % (
              esc(WHAT.get(lb, "Does the diet change this outcome?")), esc(plan.get("model") or ""), esc(plan["outcome"]),
              (" (%s)" % esc(plan["analyte"])) if plan.get("analyte") else "", esc(r["scale"]), esc(plan.get("error_strata") or "-"),
              esc(plan.get("estimand") or "-"), n(r["alpha"], "%g")))
    if r["degenerate"]:
        P.add('<p class="mut">No ANOVA table: tanks of the same diet differ only by rounding, so there is no error term to test against.</p>')
    elif r["anova"]:
        P.add("<h4>ANOVA table with its two error terms</h4>")
        P.table(["Term", "df", "Mean square", "F", "p", "Tested against"],
                [[esc(x["term"]), n(x["df"], "%d"), n(x["ms"], "%.4g"), n(x["f"], "%.2f"), pv(x["p"]),
                  ("%s (%s df)" % (esc(x["error_term"]), n(x["error_df"], "%d"))) if x["error_term"] else '<span class="mut">error term</span>']
                 for x in r["anova"]["rows"]], right=(1, 2, 3, 4))
    else:
        P.add('<p class="mut">No ANOVA table: %s</p>' % esc(r["anova_note"]))
    P.add("<h4>Contrasts</h4>")
    tcol = bool(tr and tr["contrasts"].get(lb))
    rows = []
    for x in r["contrasts"]:
        nat = x["natural"]
        row = [esc(x["label"]), n(x["estimate"], f), n(x["se"], f), n(x["df"], "%d"), n(x["t"], "%.2f"), pv(x["p"]),
               ("%s to %s" % (n(x["ci_low"], f), n(x["ci_high"], f))) if x["ci_low"] is not None else '<span class="na">&ndash;</span>',
               ("%s %s (%s to %s)" % (esc(nat["kind"]), n(nat["estimate"], "%.3f"), n(nat["ci_low"], "%.3f"), n(nat["ci_high"], "%.3f")))
               if (r["scale"] != "raw" and nat["ci_low"] is not None) else '<span class="na">&ndash;</span>']
        if tcol:
            row.append(n(tr["contrasts"][lb].get(x["label"]), f))
        rows.append(row)
    P.table(["Contrast", "Estimate", "SE", "df", "t", "p", "95 % interval", "Natural scale"] + (["From true tank values"] if tcol else []), rows,
            right=(1, 2, 3, 4, 5, 6, 7, 8))
    if tcol:
        P.add('<p class="cap">"From true tank values" = the same contrast computed from the generator\'s true value of each tank instead of the measured '
              'one. The gap to the estimate is measurement and calculation error; the gap between that column and the generator\'s constants is the '
              'luck of which tanks grew well.</p>')
    # reading
    P.add("<h4>Reading</h4><ul>")
    if dc["p"] is None:
        P.add("<li><b>Diet:</b> estimate %s; %s.</li>" % (n(dc["estimate"], f), esc(dc["note"] or "")))
    else:
        nat = dc["natural"]
        P.add("<li><b>Diet</b> (%s): %s %s, 95 %% interval %s to %s; t = %s on %s df, %s%s: %s.</li>" % (
            esc(dc["label"]), esc(nat["kind"]), n(nat["estimate"], "%.3f"), n(nat["ci_low"], "%.3f"), n(nat["ci_high"], "%.3f"), n(dc["t"], "%.2f"),
            n(dc["df"], "%d"), Page.peq(dc["p"]), (", Holm-adjusted %s" % Page.peq(dc["p_adjusted"])) if dc.get("p_adjust_method") == "holm" else "",
            reading(dc, r["alpha"])))
    raw = r["scale"] == "raw"

    def est(x):                              # estimate with interval on the scale the reader thinks in
        if raw:
            return "%s (%s to %s)" % (n(x["estimate"], f), n(x["ci_low"], f), n(x["ci_high"], f))
        t_ = x["natural"]
        return "%s %s (%s to %s)" % (esc(t_["kind"]), n(t_["estimate"], "%.3f"), n(t_["ci_low"], "%.3f"), n(t_["ci_high"], "%.3f"))

    for x in r["contrasts"]:
        if x["term"].startswith("diet x") and x["p"] is not None:
            P.add("<li><b>Does the diet effect depend on %s?</b> %s: %s, %s: %s. This is the least precise contrast of the design "
                  "(its SE is twice that of the diet effect).</li>" % (
                      esc(wf), "Diet effect at one level minus at the other" if raw else "Diet effect at one level divided by that at the other",
                      est(x), Page.peq(x["p"]), "the data show a dependence" if x["p"] < r["alpha"] else "no dependence is shown, and none is ruled out"))
        if wf and x["term"] == wf and x["p"] is not None:
            P.add("<li><b>%s</b> (%s), tested against systems: %s, %s.%s</li>" % (
                esc(wf.capitalize()), esc(x["label"]), est(x), Page.peq(x["p"]),
                " The data model warns against reading an SGR difference between temperatures as a difference in growth capacity; "
                "it is reported here because temperature is a factor of the design." if plan["outcome"] == "sgr_pct_day" else ""))
    rd = r["randomisation_diet"]
    if rd:
        P.add("<li><b>Randomisation test of the diet effect:</b> %s, from %s allocations; the smallest value this design allows is %s.</li>" % (
            Page.peq(rd["p"]), n(rd["n_allocations"], "%d"), n(rd["min_p"], "%.3f")))
    sp = r["sensitivity_pending"]
    if sp["removed"]:
        if sp["contrasts"]:
            sc = _diet(sp["contrasts"])
            if sc["p"] is None:
                P.add("<li><b>Without the pending exclusion (%s):</b> diet estimate %s on %s systems; %s.</li>" % (
                    esc(", ".join(sp["removed"])), n(sc["estimate"], f), n(sc["n_systems"], "%d"), esc(sc["note"] or "no test")))
            else:
                P.add("<li><b>Without the pending exclusion (%s):</b> diet %s on %s systems and %s df, %s.</li>" % (
                    esc(", ".join(sp["removed"])), est(sc), n(sc["n_systems"], "%d"), n(sc["df"], "%d"), Page.peq(sc["p"])))
        else:
            P.add("<li>Without the pending exclusion: %s.</li>" % esc(sp["note"]))
    vc = r["variance_components"]
    if vc and not r["degenerate"]:
        P.add("<li><b>Variation:</b> SD between tanks of a system %s (95 %% interval %s to %s, from %s df); between-system SD %s%s.</li>" % (
            n(vc["sd_unit"], "%.3g"), n(vc["sd_unit_ci95"][0], "%.3g"), n(vc["sd_unit_ci95"][1], "%.3g"), n(vc["df_unit"], "%d"), n(vc["sd_system"], "%.3g"),
            " (the estimate was below 0 and is set to 0: systems differ no more than their tanks do)" if vc["system_component_truncated"] else ""))
    pcheck = r["plan_power_check"]
    if pcheck.get("checked"):
        P.add("<li><b>Plan check:</b> the plan declares %s %% power for a difference of %s; recomputed from its SD and this design: %s %% (%s).</li>" % (
            n(100 * pcheck["declared_power"], "%.0f"), n(pcheck["declared_mdd"], "%g"), n(100 * pcheck["computed_power"], "%.1f"),
            "consistent" if pcheck["consistent"] else "NOT consistent"))
    P.add("</ul>")
    if tr:
        g = tr["generator"]
        note = {
            "growth_sgr": "True values in the generator: diet effect %s %% per day (mean over temperatures), interaction %s, temperature effect %s." % (
                n(g["sgr_diet_main"], "%.3f"), n(g["sgr_interaction"], "%.3f"), n(g["sgr_temperature"], "%.3f")),
            "feed_per_gain_log": "The generator gives every tank of a diet the same feed efficiency on feeding days (%s) with no variation between "
                                 "tanks, so the SE here is far smaller than a real trial would give. The size of the effect is meaningful; the "
                                 "tiny p value is a property of the simulator." % " and ".join(
                                     "%s for %s" % (n(v, "%.2f"), esc(k)) for k, v in g["fcr_dm_feeding_days"].items()),
            "survival_logit": "The generator gives every tank the same chance of a death per day (%s), whatever the diet or temperature. "
                              "There is no true effect, so any difference here is chance." % n(g["daily_mortality_probability"], "%g"),
            "adc_protein": "The generator fixes digestibility per diet without measurement noise, so tanks of a diet differ only by rounding. "
                           "An F ratio on rounding noise would be meaningless and is withheld.",
            "intake_log": "Intake follows from growth in the generator: a faster-growing tank is fed more.",
            "gain_log": "Gain follows from the growth rate and the start weight of each tank.",
        }.get(lb)
        if note:
            P.add('<p class="banner"><b>Sample-data note.</b> %s</p>' % note)
    P.add("</details>")


def sec_statistics(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    P.add('<section id="statistics"><h2>Statistics</h2>')
    P.add('<p class="lead">Every analysis below was fixed in the plan before the trial started (table <code>planned_outcomes</code>) and is computed by '
          '<code>tools/analysis.py</code>, which reads the plan and runs exactly the registered models. Each analysis has automated tests; see '
          '<a href="#evidence">Evidence</a>.</p><h3>How to read the tests</h3>')
    _explain(P, c)
    P.fig(c.svgs, "cells", "The data behind every test: one point per tank. Lines that are not parallel suggest that the diet effect differs between levels.")
    P.fig(c.svgs, "forest", "Each row has its own axis and scale. An interval that crosses the dotted line is compatible with no diet effect.")
    P.add("<h3>Results per planned outcome</h3>")
    for lb, r in out.items():
        _outcome(P, c, lb, r)
    # fish as replicates
    fl = res.get("fish_level")
    if fl:
        P.add('<h3 id="fishunit">The analysis not to run: fish as replicates</h3>')
        P.add("<p>The same final weights analysed twice (%s). With every fish as an independent observation (%s fish): t = %s on %s degrees of "
              "freedom, %s. With the tank mean as the observation (%s tanks): t = %s on %s degrees of freedom, %s. The first analysis "
              "claims %s times the degrees of freedom the design has, and its SE is %s times too small.</p>" % (
                  esc(fl["response"]), n(fl["n_fish"], "%d"), n(fl["naive"]["t"], "%.1f"), n(fl["naive"]["df"], "%d"), Page.peq(fl["naive"]["p"]),
                  n(fl["n_units"], "%d"), n(fl["correct"]["t"], "%.2f"), n(fl["correct"]["df"], "%d"), Page.peq(fl["correct"]["p"]),
                  n(d["fish_df_ratio"], "%.0f"), n(fl["se_ratio"], "%.1f")))
        if "seeds" in d:
            s = d["seeds"]
            P.add("<p>That this is wrong and not merely cautious is shown by trials generated with <b>no diet effect at all</b>: treating fish as "
                  "replicates declared a significant diet effect in %s of %s trials (%s %%); the tank analysis in %s of %s. A test at the 5 %% "
                  "level should do so in about 5 %%.</p>" % (n(s["naive_null"], "%d"), n(s["n_null"], "%d"), n(s["naive_null_pct"], "%.0f"),
                                                           n(s["unit_null"], "%d"), n(s["n_null"], "%d")))
            P.fig(c.svgs, "null", "Distribution of p values when the null hypothesis is true. A valid test gives every p value equally often "
                  "(the diagonal); the curve of the fish-level analysis shows how many small p values it produces from nothing.")
    # randomisation
    rd = c.prim["randomisation_diet"]
    if rd:
        P.add('<h3 id="randomisation">Randomisation test</h3>')
        P.fig(c.svgs, "random", "Primary outcome. Each tick is the diet effect one of the possible allocations would have shown if diet changed nothing.")
        P.add("<p>Observed diet effect %s; %s of the %s allocations are at least as extreme, %s. " % (
            n(rd["statistic"], "%.3f"), n(round(rd["p"] * rd["n_allocations"]), "%d"), n(rd["n_allocations"], "%d"), Page.peq(rd["p"])))
        if "randomisation_floor" in d:
            P.add("The smallest p this test can give depends only on the number of systems: " + ", ".join(
                "%s systems &rarr; %s" % (n(x["n_systems"], "%d"), n(x["min_p"], "%.4f")) for x in d["randomisation_floor"]) + ". ")
        rw = c.prim["randomisation_whole"]
        if rw:
            P.add("For %s the systems themselves are permuted: %s allocations, smallest possible p %s, so the %s result of this trial rests "
                  "entirely on the normal-theory test." % (esc(wf), n(rw["n_allocations"], "%d"), n(rw["min_p"], "%.3f"), esc(wf)))
        P.add("</p>")
    # multiplicity
    hm = [m for m in res["multiplicity"] if m["method"] == "holm"]
    if hm:
        P.add("<h3>Multiplicity</h3>")
        P.table(["Family", "Outcome", "Contrast", "p", "Holm-adjusted p", "Tests in family"],
                [[esc(m["family"]), esc(m["outcome"]), esc(m["contrast"]), pv(m["p"]), pv(m["p_adjusted"]), n(m["n_tests_in_family"], "%d")] for m in hm],
                right=(3, 4, 5))
    for note in res["notes"]:
        P.add('<p class="cap">Note from the analysis: %s</p>' % esc(note))
    # power
    if p["power"]:
        pw = p["power"]
        P.add('<h3 id="power">Power and design</h3>')
        P.add("<p>Planned for outcome <code>%s</code>: SD between tanks of a system %s, difference to detect %s, alpha %s, power %s. The curves show "
              "what the same plan gives with more systems per level (one tank per diet in each system).</p>" % (
                  esc(pw["outcome"]), n(pw["expected_sd"], "%g"), n(pw["planned_mdd"], "%g"), n(pw["alpha"], "%g"), n(pw["planned_power"], "%g")))
        P.fig(c.svgs, "power", "Left: chance of a significant result when the true difference equals the planned one. Middle: the smallest true "
              "difference found with the planned power. Right: how far an estimate must lie from 0, in standard errors.")
        terms = [t for t in ("diet", "interaction") if t in pw["grid"][0]]
        head = ["Systems per level", "Error df", "Critical t"]
        for t in terms:
            nm = "diet" if t == "diet" else "diet &times; %s" % esc(wf or "level")
            head += ["SE, %s" % nm, "Power, %s" % nm, "Detectable difference, %s" % nm]
        P.table(head, [[n(x["n_systems_per_level"], "%d") + (' <span class="tag">this trial</span>' if x["n_systems_per_level"] == pw["n_run"] else ""),
                        n(x["df"], "%d"), n(x["t_crit"], "%.2f")]
                       + [v for t in terms for v in (n(x[t]["se"], "%.4f"), n(x[t]["power_at_mdd"], "%.3f"), n(x[t]["mdd_80"], "%.3f"))] for x in pw["grid"]],
                right=tuple(range(1, len(head))))
        vc = c.prim["variance_components"]
        if vc:
            P.add("<p>The SD between tanks actually observed for this outcome is %s, with a 95 %% interval from %s to %s: with %s degrees of freedom "
                  "a single trial says little about its own variability, which is why the plan's SD should come from historical trials "
                  "(<code>tools/calibrate_params.py</code>).</p>" % (
                      n(vc["sd_unit"], "%.3f"), n(vc["sd_unit_ci95"][0], "%.3f"), n(vc["sd_unit_ci95"][1], "%.3f"), n(vc["df_unit"], "%d")))
        if "seeds" in d and p["evidence"]["seeds"].get("power_formula") is not None:
            s = d["seeds"]
            P.add("<p>Check of the power formula against simulation: at the generator's true effect the formula gives %s %%; %s of %s simulated "
                  "trials (%s %%) were significant. The standard error of such a share over %s trials is %s percentage points, and the generator's "
                  "noise is uniform where the formula assumes normal.</p>" % (
                      n(100 * p["evidence"]["seeds"]["power_formula"], "%.1f"), n(s["reject_effect"], "%d"), n(s["n_effect"], "%d"),
                      n(s["reject_effect_pct"], "%.1f"), n(s["n_effect"], "%d"), n(s["power_share_se_pct"], "%.1f")))
    # mixed-model software note
    if p["reference"]:
        rf = p["reference"]
        P.add("<h3>A note on mixed-model software</h3>")
        P.add("<p>The same tank values fitted as a mixed model with a random system effect (REML, statsmodels %s) give the same estimates, standard "
              "errors and variance components wherever the system variance is positive (see Evidence). What differs is the default test: a Wald z "
              "test treats the SE as known exactly and ignores that it rests on %s degrees of freedom.</p>" % (
                  esc(rf["made_with"]["statsmodels"]), n(c.pc["df"], "%d")))
        lm = rf.get("lmer")
        head = ["Outcome", "Stratum t test (this page)", "Wald z test (software default)"]
        if lm:
            head = ["Outcome", "Stratum t test (plan)", "Wald z (default)", "lmerTest Satterthwaite", "lmerTest Kenward-Roger"]

        def lmer_cells(lb):
            if not lm or lb not in lm["diet"]:
                return [] if not lm else ['<span class="na">&ndash;</span>'] * 2
            r = lm["diet"][lb]
            return [pv(r[k]["p"]) + " (df " + n(r[k]["df"], "%.3g") + ")" for k in ("satterthwaite", "kenward_roger")]
        P.table(head + ["REML system variance"],
                [[esc(lb), pv(_diet(out[lb]["contrasts"])["p"]) + " (df " + n(_diet(out[lb]["contrasts"])["df"], "%d") + ")", pv(v)]
                 + lmer_cells(lb)
                 + [n(rf["reml_var_system"][lb], "%.3g") + ((" (at 0)" if lm else " (at 0: REML pools both error terms)")
                                                            if rf["reml_var_system"][lb] < 1e-9 else "")]
                 for lb, v in rf["wald_z_p_diet"].items() if lb in out], right=tuple(range(1, len(head) + 1)))
        P.add('<p class="cap">p value of the diet effect. With 8 tanks the choice of reference distribution decides the conclusion; '
              'the stratum test is the one registered in the plan.</p>')
        if lm:
            sing = [lb for lb, r in lm["diet"].items() if r["singular"] and lb in out]
            P.add("<p>lme4 %s with lmerTest %s, the small-sample tests most mixed-model analyses report, give the stratum test's df and p "
                  "wherever the system variance is positive. Where REML puts it at 0%s the fit is singular: the system and tank error terms "
                  "are pooled, Satterthwaite then counts the degrees of freedom of both, and Kenward-Roger keeps the df but uses the pooled "
                  "variance. For the diet effect both p values are then smaller than that of the planned test, which keeps the design's own "
                  "error term even when the system variance is estimated below 0; the planned test is the one shown under Evidence to "
                  "reject at its nominal 5 %% rate when there is no effect.</p>" % (
                      esc(lm["made_with"]["lme4"]), esc(lm["made_with"]["lmerTest"]),
                      " (" + ", ".join(esc(lb) for lb in sing) + ")" if sing else ""))
    P.add("</section>")
def sec_evidence(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n, pv = Page.n, Page.p
    P.add('<section id="evidence"><h2>Evidence</h2>')
    P.add('<p class="lead">Why the numbers on this page can be trusted: the schema, the metrics, the rules and the statistics are each proved by '
          'tests that run as one chain (<code>python tools/run_all_tests.py</code>).</p>')
    if p["chain"]:
        ch = p["chain"]
        P.add("<h3>Proof chain, last run</h3>")
        P.table(["Step", "Result", "What was shown", "Seconds"],
                [[esc(s["step"]), '<span class="tag %s">%s</span>' % (("ok", "passed") if s["ok"] else ("no", "FAILED")), esc(s["message"]), n(s["seconds"], "%.0f")]
                 for s in ch["steps"]], right=(3,))
        P.add('<p class="cap">Run on %s, Python %s: %s of %s steps passed in %s s. Each step needs the ones before it; the chain stops at the first failure.</p>' % (
            esc(ch["platform"]), esc(ch["python"]), n(d["chain_ok"], "%d"), n(d["chain_n"], "%d"), n(d["chain_seconds"], "%.0f")))
    else:
        P.add('<p class="banner">No record of a proof-chain run was found next to the tools. Run <code>python tools/run_all_tests.py</code> and rebuild this page '
              'to show its result here.</p>')
    ev = p["evidence"]
    if ev:
        P.add("<h3>Tests of the statistical analyses</h3>")
        P.add("<p>%s checks, %s failed; %s of %s analyses are exercised. Each analysis is tested against something it did not compute itself:</p><ul>"
              "<li><b>Known truth.</b> The synthetic trial is generated from known effects. Over repeated trials the estimates must be unbiased and "
              "the 95 %% intervals must cover the truth about 95 %% of the time; per trial, an estimate from the measured data must equal the same "
              "estimate from the generator's true tank values.</li>"
              "<li><b>Independent implementation.</b> The ANOVA against R (<code>aov</code> with an error stratum), estimates and variance components "
              "against a REML mixed model, contrasts with their degrees of freedom and p values against lme4/lmerTest (Satterthwaite and "
              "Kenward-Roger), F tests against least squares, the randomisation tests against brute-force relabelling, the power "
              "function against Monte-Carlo draws.</li>"
              "<li><b>No effect.</b> With nothing to find, a test at the 5 %% level must reject about 5 %% of the time.</li>"
              "<li><b>Known wrong method.</b> Fish as replicates must be shown to fail that requirement.</li></ul>" % (
                  n(ev["n_checks"], "%d"), n(ev.get("n_failed", 0), "%d"), n(len(ev["exercised"]), "%d"), n(p["model"]["n_analyses"], "%d")))
        if "seeds" in d:
            s = d["seeds"]
            P.fig(c.svgs, "seeds", "Primary outcome, diet effect, in %d synthetic trials that differ only in their random seed." % s["n_effect"])
            P.add("<p>Mean estimate over %s trials: %s (true %s). The 95 %% interval covers the truth in %s trials (%s %%). "
                  "The test is significant in %s of them (%s %%); with no diet effect in the generator it is significant in %s of %s.</p>" % (
                      n(s["n_effect"], "%d"), n(s["mean_estimate"], "%.4f"), n(s["true"], "%.3f"), n(s["coverage_n"], "%d"), n(s["coverage_pct"], "%.0f"),
                      n(s["reject_effect"], "%d"), n(s["reject_effect_pct"], "%.0f"), n(s["reject_null"], "%d"), n(s["n_null"], "%d")))
        if ev.get("checks"):
            P.add("<details><summary>All %s checks with their measured values</summary>" % n(len(ev["checks"]), "%d"))
            P.table(["Analysis", "Check", "Measured", "Result"],
                    [[esc(", ".join(k["analyses"])), esc(k["text"]), esc(k["detail"]),
                      '<span class="tag %s">%s</span>' % (("ok", "passed") if k["ok"] else ("no", "FAILED"))] for k in ev["checks"]])
            P.add("</details>")
    else:
        P.add('<p class="banner">No record of the analysis tests was found. Run <code>python tools/test_analysis.py</code> and rebuild this page.</p>')
    if p["reference"]:
        mw = p["reference"]["made_with"]
        lmw = (p["reference"].get("lmer") or {}).get("made_with")
        P.add("<p>Stored reference values (<code>tools/reference/</code>): R %s <code>aov</code>, statsmodels %s REML%s, on the tank values of this trial.</p>" % (
            esc(mw.get("R")), esc(mw.get("statsmodels")),
            ", lme4 %s / lmerTest %s (R %s)" % (esc(lmw["lme4"]), esc(lmw["lmerTest"]), esc(lmw["R"])) if lmw else ""))
    rr = res["run_record"]
    P.add("<h3>Reproducibility</h3>")
    P.table(["Item", "Value"], [
        ["Input snapshot (sha256 of every table)", "<code>%s</code>" % esc(rr["input_snapshot_hash"])],
        ["Parameters (validation thresholds and planned outcomes)", "<code>%s</code>" % esc(rr["params_hash"])],
        ["Results of the analysis", "<code>%s</code>" % esc(rr["output_hash"])],
        ["Code", "commit <code>%s</code>%s" % (esc(p["versions"].get("commit") or "unknown"),
                                              " plus uncommitted changes" if p["versions"].get("uncommitted_changes") else "")],
        ["Software", "Python %s, numpy %s, scipy %s, matplotlib %s" % tuple(esc(p["versions"][k]) for k in ("python", "numpy", "scipy", "matplotlib"))]])
    P.add('<p class="cap">The three hashes are the fields of an <code>analysis_runs</code> row: the same data, parameters and code give the same hashes, '
          'and a change of one weighing changes the first and the third. The complete data behind this page are embedded in it as JSON '
          '(element <code>payload</code>).</p></section>')


def sec_limits(P, c):
    p, res, des, d, L, out, tr, wf = c.p, c.res, c.des, c.d, c.L, c.out, c.tr, c.wf
    n = Page.n
    P.add('<section id="limits"><h2>Limits</h2><ul>')
    if p["synthetic"]:
        g = tr["generator"]
        P.add("<li><b>The data are simulated.</b> The generator is simpler than a fish tank: tanks of a cell differ by a uniform &plusmn;%s %% in growth "
              "rate, there is no variation between systems beyond that of their tanks, none between tanks in feed efficiency or digestibility, and "
              "the same mortality everywhere. Results marked with a sample-data note reflect this.</li>" % n(g["unit_sgr_noise_pct"], "%g"))
    P.add("<li><b>Few replicates.</b> %s systems per level leave %s degrees of freedom for every test. The trial can show large effects only; "
          "see Power and design.</li>" % (n(c.nper, "%d"), n(c.pc["df"], "%d")))
    P.add("<li><b>Scope of the analysis module.</b> One whole-plot factor (or none), one tank per diet in each system (replicate tanks are averaged), "
          "each diet against the reference diet. Not implemented: growth curves over more than two weighings as repeated measures, time-to-event "
          "models of mortality, dose-response on analysed dose, ordinal models for welfare scores, pooling of several trials. The schema stores "
          "what these need; the code for them is not written.</li>")
    P.add("<li><b>Assumptions that cannot be checked here.</b> Normality and equal variance of tank-level errors (see Statistics, point 10).</li>")
    P.add("<li><b>Adjusted analyses.</b> No covariate adjustment is made; the plan lists start weight as a precision covariate, and with %s "
          "tanks a covariate would use up one of the %s error degrees of freedom.</li>" % (n(d["n_units"], "%d"), n(c.pc["df"], "%d")))
    P.add("<li><b>This page.</b> Static HTML with embedded charts; it does not update itself. Rebuild it with "
          "<code>python tools/build_dashboard.py</code> after any change to the data.</li></ul></section>")


def render(p, svgs):
    c, P = Ctx(p, svgs), Page()
    for sec in (sec_head, sec_overview, sec_design, sec_quality, sec_growth, sec_feed, sec_survival, sec_statistics, sec_evidence, sec_limits):
        sec(P, c)
    P.add("</main><footer>Built by tools/build_dashboard.py from the data and tools of this repository. Analysis output %s&hellip;</footer>" % esc(
        p["analysis"]["run_record"]["output_hash"][:16]))
    P.add('<script type="application/json" id="payload">%s</script></body></html>' % json.dumps(p, separators=(",", ":")).replace("</", "<\\/"))
    return P.html()


def build(yaml_path=DEFAULT_YAML, db=None, out="dashboard.html", seed=7):
    p = collect(yaml_path, db, seed)
    _style()
    svgs = {k: svg(f, k) for k, f in figures(p).items()}
    page = render(p, svgs)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
    return p, page


def main(argv):
    opt = {"--out": "dashboard.html", "--seed": "7"}
    args = []
    it = iter(argv[1:])
    for a in it:
        if a in opt:
            opt[a] = next(it)
        else:
            args.append(a)
    p, page = build(args[0] if args else DEFAULT_YAML, args[1] if len(args) > 1 else None, opt["--out"], int(opt["--seed"]))
    print("wrote %s: %d kB, %d figures, %d planned outcomes, findings %s" % (
        opt["--out"], len(page.encode("utf-8")) // 1024, page.count("<figure"), len(p["analysis"]["outcomes"]), p["findings"]["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
