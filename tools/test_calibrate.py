#!/usr/bin/env python3
"""Test calibrate_params.py on data whose variation is KNOWN.

The synthetic trial draws fish weights with a 4 % CV around each unit mean, unit means
uniformly in 95-105 g at stocking (CV about 2.9 %), recovery 0.82 / 0.86 and an exact count
balance. Each estimate must recover those values; a recount error injected in a copy must
raise the count tolerance; too little data must be reported as not estimable, never guessed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calibrate_params import calibrate  # noqa: E402
from synthetic_trial import make_trial  # noqa: E402

yaml_path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
fails, checks = [], 0


def check(name, ok, detail):
    global checks
    checks += 1
    if callable(ok):
        try:
            ok = ok()
        except (KeyError, TypeError) as e:
            ok, detail = False, "missing: %s" % e
    if not ok:
        fails.append("%s: %s" % (name, detail))


con, model, truth = make_trial(yaml_path)
r = calibrate(con)
th, pw = r["thresholds"], r["power_inputs"]

# within-unit CV of d0 weights is 4 % by construction; the 95th percentile of 8 sample CVs
check("initial CV threshold", lambda: 4.0 <= th["max_initial_cv_pct"]["suggested"] <= 6.5, th.get("max_initial_cv_pct"))
check("initial CV n", lambda: th["max_initial_cv_pct"]["n_obs"] == 8, th.get("max_initial_cv_pct"))
# power input: mean within-unit CV at final close to 4 %
check("cv_within_unit_pct", lambda: 3.0 <= pw["cv_within_unit_pct"] <= 5.0, pw)
# only 4 recovery tests (< 5): withheld, not guessed
check("min_recovery withheld with 4 tests", "min_recovery" not in th and "min_recovery" in r["not_estimated"], r["not_estimated"])
# 4 more tests at 0.82 / 0.86 -> 0.9 x 5th percentile = 0.9 x 0.82
for k, rec in enumerate((0.82, 0.86, 0.82, 0.86)):
    con.execute("INSERT INTO recovery_tests (record_id, trial_id, unit_type, pellet_mm, temp_c, feed_in_dm_g, feed_recovered_dm_g) "
                "VALUES (?, 'T1', 'tank', 3, 12, 100, ?)", ("RTX%d" % k, 100 * rec))
rr = calibrate(con)["thresholds"].get("min_recovery", {})
check("min_recovery with 8 tests", abs(rr.get("suggested", 0) - 0.9 * 0.82) < 0.005, rr)
# exact count balance -> floor 0.5 %
check("count tolerance (exact data)", lambda: th["n_balance_tolerance_pct"]["suggested"] == 0.5, th.get("n_balance_tolerance_pct"))
# sampling noise: SE of 20 fish at 4 % CV = 0.9 %, x3 at the 95th percentile -> about 2.7-3.9 %
check("negative growth threshold", lambda: 2.0 <= th["max_negative_growth_pct"]["suggested"] <= 4.5, th.get("max_negative_growth_pct"))
# daily temperature only -> no hourly jumps -> not estimable, and said so
check("temp jump not guessed", "max_temp_jump_c_h" not in th and "max_temp_jump_c_h" in r["not_estimated"], r["not_estimated"])
check("icc in (0,1)", 0 < pw.get("icc", -1) < 1, pw)

# injected recount error: harvest counts 3 % too low in all units -> tolerance about 3 %
con.execute("UPDATE weighings SET n_fish = CAST(ROUND(n_fish * 0.97) AS INTEGER) WHERE sampling_type = 'harvest'")
r2 = calibrate(con)
tol = r2["thresholds"].get("n_balance_tolerance_pct", {})
check("count tolerance follows recount error", lambda: 2.0 <= tol["suggested"] <= 4.0, tol)

# too little data: a database with 2 units only -> estimates withheld
con.commit()
con.execute("PRAGMA foreign_keys = OFF")
keep = ("U1D1", "U1D2")
for t in ("weighings", "events", "feeding", "uneaten_feed", "environment", "fish_measurements", "unit_occasions"):
    con.execute("DELETE FROM %s WHERE unit_id NOT IN (?, ?)" % t, keep)
con.execute("DELETE FROM units WHERE unit_id NOT IN (?, ?)", keep)
r3 = calibrate(con)
check("small data withheld", "max_initial_cv_pct" not in r3["thresholds"]
      and "max_initial_cv_pct" in r3["not_estimated"], r3["not_estimated"])

if fails:
    print("\n".join("FAIL " + f for f in fails))
    print("%d/%d calibration checks failed" % (len(fails), checks))
    sys.exit(1)
print("OK: %d calibration checks (thresholds recover known variation; injected recount error detected; "
      "small data withheld)" % checks)
