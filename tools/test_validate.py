#!/usr/bin/env python3
"""Tests for tools/validate.py on the synthetic RAS split-plot trial.

1. The clean synthetic trial gives no error finding (warnings / info are printed).
2. For every validator rule, a mutation of a fresh copy of the clean trial (SQL that still
   passes every database constraint) makes that rule produce a NEW finding with the expected
   severity: the rule's YAML severity, or the conditional severity the rule text states.
   Other rules' new findings (collateral) are printed, not failed.
3. With the metrics module unavailable, the metric rules emit an info finding instead of
   crashing.
Writes tools/.coverage_rules.json (validator rules exercised by a mutation) for
tools/run_all_tests.py.

Usage: test_validate.py [data_model.yaml]      exit 1 on failure
"""
import datetime as dt
import json
import math
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate as V  # noqa: E402
from synthetic_trial import make_trial  # noqa: E402

YAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")
D0 = dt.date(2025, 3, 3)
TRIAL = "T1"


def day(n):
    return (D0 + dt.timedelta(days=n)).isoformat()


def ins(con, table, **row):
    con.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (table, ", ".join(row), ", ".join("?" * len(row))),
                list(row.values()))


def plan(con, model_text, error_strata="system", outcome="sgr_pct_day", label="o1", **extra):
    if not con.execute("SELECT 1 FROM analysis_plans WHERE plan_id = 'P1'").fetchone():
        ins(con, "analysis_plans", plan_id="P1", trial_id=TRIAL, version="1")
        # a harmless covariate role, so R28 is checked (not 'no plan rows') in every plan mutation
        ins(con, "covariate_roles", plan_id="P1", variable="w_d0", role="precision", measured="baseline")
    ins(con, "planned_outcomes", plan_id="P1", outcome_label=label, outcome=outcome, role="primary",
        unit_of_analysis="unit", model=model_text, error_strata=error_strata, **extra)


def first_zero_mortality(con, unit, after=5):
    return con.execute("SELECT min(date) FROM events WHERE unit_id = ? AND event_type = 'mortality' AND n_fish = 0"
                       " AND date > ?", (unit, day(after))).fetchone()[0]


def camera(con, unit, start_day, cal=None, version="v1", mean=300.0, n=500):
    ts = dt.datetime.combine(D0 + dt.timedelta(days=start_day), dt.time(10))
    ins(con, "camera_estimates", unit_id=unit, camera_id="C1", period_start=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        period_end=(ts + dt.timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"), n_detections=n,
        mean_weight_g=mean, algorithm_version=version, calibration_id=cal)


def calib(con, cid, unit, a, b, version="v1"):
    ins(con, "camera_calibrations", calibration_id=cid, unit_id=unit, camera_id="C1", algorithm_version=version,
        valid_from=day(a), valid_to=day(b), model="bias_only", bias_g=0.0)


# ------------------------------------------------------------------- mutations
# (rule, expected severity or None = YAML severity, description, function(con))
def m_r40(con):
    calib(con, "CAL2", "U1D1", 30, 50)
    mean = con.execute("SELECT avg(weight_g) FROM weighings WHERE unit_id = 'U1D1' AND occasion = 'mid1'"
                       " AND qc_flag <> 'removed'").fetchone()[0]
    camera(con, "U1D1", 42, cal="CAL2", mean=mean)


def m_r17(con):
    ins(con, "trials", trial_id="T2", start_date=day(0))
    ins(con, "systems", system_id="SYX", trial_id="T2", site_id="S1", temp_control="controlled", target_temp_c=12.0)
    ins(con, "factor_levels", record_id="FL_X", factor_id="F_TEMP", system_id="SYX", level="12C", numeric_value=12.0)


def m_r02_warning(con):
    ins(con, "design_factors", factor_id="F_PHOTO", trial_id=TRIAL, name="photoperiod", role="assigned_treatment",
        applied_at="system", level_type="categorical")
    for s in ("SY1", "SY2", "SY3", "SY4"):
        ins(con, "factor_levels", record_id="FLP_" + s, factor_id="F_PHOTO", system_id=s, level="A" if s == "SY2" else "B")


def m_r02_error(con):
    ins(con, "design_factors", factor_id="F_CLIM", trial_id=TRIAL, name="site_climate", role="observed_condition",
        applied_at="site", level_type="categorical")
    for s in ("SY1", "SY2", "SY3", "SY4"):
        ins(con, "factor_levels", record_id="FLC_" + s, factor_id="F_CLIM", system_id=s, level="mid")
    plan(con, "lmer(sgr ~ diet*site_climate + (1|system))")


def m_fish(con, fate=None, fate_date=None):
    ins(con, "fish", fish_id="F1", trial_id=TRIAL, pit_tag="P1", unit_id="U1D1", fate=fate, fate_date=fate_date)

# ---- threshold-boundary helpers (validation_params_defaults): a value just outside the default
# boundary must fire, one just inside must stay silent (tools/test_thresholds.py shifts each
# default x10 / x0.1 and requires a test to notice)
def recount(con, unit, minus):
    """harvest count of `unit` lower by `minus` fish (miscount; weighed biomass unchanged)"""
    con.execute("UPDATE weighings SET n_fish = n_fish - ? WHERE record_id = ?", (minus, unit + "_HARV"))


def scale_mid1_to_d0(con, ratio):
    """U1D1 mid1 sample weights scaled so that W(mid1) = ratio x W(d0)"""
    w0 = V.M.unit_weight(con, "U1D1", "d0")["w_g"]
    w1 = V.M.unit_weight(con, "U1D1", "mid1")["w_g"]
    con.execute("UPDATE weighings SET weight_g = weight_g * ? WHERE unit_id = 'U1D1' AND occasion = 'mid1'"
                " AND sampling_type = 'sample'", (ratio * w0 / w1,))


def d0_cv(con, cv_pct):
    """U1D1 d0 sample weights spread around their mean to a within-unit CV of cv_pct (mean kept)"""
    rows = con.execute("SELECT record_id, weight_g FROM weighings WHERE unit_id = 'U1D1' AND occasion = 'd0'"
                       " AND sampling_type = 'sample'").fetchall()
    ws = [w for _, w in rows]
    m = sum(ws) / len(ws)
    cv = 100 * math.sqrt(sum((w - m) ** 2 for w in ws) / (len(ws) - 1)) / m
    for rid, w in rows:
        con.execute("UPDATE weighings SET weight_g = ? WHERE record_id = ?", (m + (w - m) * cv_pct / cv, rid))


def d2_mean_diff(con, pct):
    """d0 count biomass of the D2 units scaled so that the D2 d0 mean is pct % above reference D1"""
    w0 = {u: V.M.unit_weight(con, u, "d0")["w_g"] for (u,) in con.execute("SELECT unit_id FROM units")}
    d1 = [w for u, w in w0.items() if u.endswith("D1")]
    d2 = [w for u, w in w0.items() if u.endswith("D2")]
    f = (1 + pct / 100.0) * (sum(d1) / len(d1)) / (sum(d2) / len(d2))
    con.execute("UPDATE weighings SET biomass_kg = biomass_kg * ? WHERE unit_id LIKE '%D2' AND occasion = 'd0'"
                " AND sampling_type IN ('stocking','bulk')", (f,))


def hourly(con, sensor, variable, values, hours, unit="U1D1", start_day=30, position="in_unit"):
    """hourly instant readings (nominal 60 min) of a separate sensor on day start_day, UTC hours"""
    if not con.execute("SELECT 1 FROM sensors WHERE sensor_id = ?", (sensor,)).fetchone():
        ins(con, "sensors", sensor_id=sensor, variable=variable)
    for h, v in zip(hours, values):
        ts = dt.datetime.combine(D0 + dt.timedelta(days=start_day), dt.time(0)) + dt.timedelta(hours=h)
        ins(con, "environment", record_id="%s_%02d" % (sensor, h), timestamp=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            unit_id=unit, variable=variable, aggregation="instant", nominal_interval_min=60, sensor_id=sensor,
            measure_position=position, value=v)


def o2_gap(con, gap_h):
    """hourly o2_sat_pct with readings missing so that (time between readings - 1 h interval) = gap_h"""
    hours = list(range(0, 6)) + list(range(6 + gap_h, 24))
    hourly(con, "O2_U1D1", "o2_sat_pct", [95 + 0.1 * h for h in hours], hours)


def temp_step(con, step_c):
    """hourly inlet temperature with a step of step_c C within one hour (heater switched)"""
    hours = list(range(12))
    hourly(con, "TI_U1D1", "temp_c", [12.0 + 0.02 * h + (step_c if h >= 6 else 0) for h in hours], hours,
           position="inlet")


def flat(con, n_days):
    """U1D1 daily temperature identical (12.005; data have 2 decimals) for n_days days from day 30"""
    con.execute("UPDATE environment SET value = 12.005 WHERE unit_id = 'U1D1' AND variable = 'temp_c'"
                " AND timestamp BETWEEN ? AND ?", (day(30), day(30 + n_days)))


def detections(con, n):
    """calibrated camera estimate on day 20 (no occasion within camera_window_days) with n detections"""
    calib(con, "CALD", "U1D1", 0, 84)
    camera(con, "U1D1", 20, cal="CALD", n=n)


def kill_lag(con, lag_days):
    """U1D1 removal_sampling event lag_days after the mid1 kill (day 42)"""
    con.execute("UPDATE events SET date = ? WHERE unit_id = 'U1D1' AND event_type = 'removal_sampling'",
                (day(42 + lag_days),))


def cages(con, dist_m):
    """U1D1 / U1D2 (different diets) become cages dist_m apart"""
    con.execute("UPDATE units SET unit_type = 'cage', x_m = 0, y_m = 0 WHERE unit_id = 'U1D1'")
    con.execute("UPDATE units SET unit_type = 'cage', x_m = ?, y_m = 0 WHERE unit_id = 'U1D2'", (dist_m,))


MUTATIONS = [
    ("R01", None, "delete U2D1 stocking weighing (d0 bulk count remains)",
     lambda c: c.execute("DELETE FROM weighings WHERE record_id = 'U2D1_STOCK'")),
    ("R02", "warning", "system-level photoperiod with level A on SY2 only (factor not in a plan)", m_r02_warning),
    ("R02", None, "observed site_climate with one site per level, used in a planned model", m_r02_error),
    ("R03", None, "exclusion with scope record naming nothing",
     lambda c: ins(c, "deviations", record_id="DEVX", trial_id=TRIAL, date=day(10), category="exclusion",
                   exclusion_scope="record", description="?", decided_by="x", decided_date=day(10))),
    ("R04", None, "harvest n_fish +20 %",
     lambda c: c.execute("UPDATE weighings SET n_fish = CAST(n_fish * 1.2 AS INTEGER), biomass_kg = biomass_kg * 1.2"
                         " WHERE record_id = 'U1D1_HARV'")),
    ("R05", None, "U1D1 mid1 sample weights halved (below d0)",
     lambda c: c.execute("UPDATE weighings SET weight_g = weight_g * 0.5 WHERE unit_id = 'U1D1' AND occasion = 'mid1'")),
    ("R06", None, "one d0 fish x5 (within-unit CV)",
     lambda c: c.execute("UPDATE weighings SET weight_g = weight_g * 5 WHERE record_id = 'U1D1_d0_S01'")),
    ("R07", None, "delete a week of U1D1 feeding rows",
     lambda c: c.execute("DELETE FROM feeding WHERE unit_id = 'U1D1' AND date BETWEEN ? AND ?", (day(10), day(16)))),
    ("R08", None, "delete a week of U4D2 uneaten_feed rows",
     lambda c: c.execute("DELETE FROM uneaten_feed WHERE unit_id = 'U4D2' AND date BETWEEN ? AND ?", (day(30), day(36)))),
    ("R08", None, "delete recovery tests for 4 mm pellets",
     lambda c: c.execute("DELETE FROM recovery_tests WHERE pellet_mm = 4")),
    ("R09", None, "flat-lined U1D1 temperature for 6 days",
     lambda c: c.execute("UPDATE environment SET value = 12.0 WHERE unit_id = 'U1D1' AND timestamp BETWEEN ? AND ?",
                         (day(30), day(36)))),
    ("R10", None, "camera row with algorithm_version unknown and no calibration",
     lambda c: camera(c, "U1D1", 59, version="unknown")),
    ("R11", None, "light regime of SY1 ends on day 30",
     lambda c: c.execute("UPDATE light_regimes SET end_date = ? WHERE regime_id = 'LR_SY1'", (day(30),))),
    ("R12", None, "diet codes given, U1D1 code equals its diet label",
     lambda c: (c.execute("UPDATE units SET diet_code_blind = 'X' || unit_id"),
                c.execute("UPDATE units SET diet_code_blind = 'D1' WHERE unit_id = 'U1D1'"))),
    ("R14", None, "U1D1 removal_sampling event moved 8 days after the kill",
     lambda c: c.execute("UPDATE events SET date = ? WHERE unit_id = 'U1D1' AND event_type = 'removal_sampling'",
                         (day(50),))),
    ("R14", "warning", "sample row with fish_fate NULL",
     lambda c: c.execute("UPDATE weighings SET fish_fate = NULL WHERE record_id = 'U1D1_final_S01'")),
    ("R15", None, "qc_flag removed without deviation",
     lambda c: c.execute("UPDATE weighings SET qc_flag = 'removed' WHERE record_id = 'U2D1_final_S05'")),
    ("R17", None, "factor_levels row of trial T1's factor on a system of trial T2", m_r17),
    ("R18", None, "weighing of fish F1 (unit U1D1) recorded in U1D2",
     lambda c: (m_fish(c), ins(c, "weighings", record_id="W_F1", unit_id="U1D2", occasion="d0", date=day(0),
                               sampling_type="individual", fish_id="F1", weight_g=100.0, weight_basis="round",
                               fish_fate="returned"))),
    ("R19", None, "delete one day of U1D1 mortality rows",
     lambda c: c.execute("DELETE FROM events WHERE unit_id = 'U1D1' AND event_type = 'mortality' AND date = ?",
                         (first_zero_mortality(c, "U1D1"),))),
    ("R20", None, "U1D2 moved to block BX (incomplete blocks)",
     lambda c: c.execute("UPDATE units SET block = 'BX' WHERE unit_id = 'U1D2'")),
    ("R21", None, "dose_value on D2 only",
     lambda c: c.execute("UPDATE diets SET dose_value = 1.0 WHERE diet_id = 'D2'")),
    ("R21", None, "dose_basis added on both diets, no basal diet with dose 0",
     lambda c: c.execute("UPDATE diets SET dose_value = CASE diet_id WHEN 'D1' THEN 1.0 ELSE 2.0 END,"
                         " dose_unit = 'g_kg', dose_basis = 'added', dose_analyte = 'protein'")),
    ("R22", None, "camera estimate outside its calibration window",
     lambda c: (calib(c, "CAL1", "U1D1", 0, 20), camera(c, "U1D1", 59, cal="CAL1"))),
    ("R23", None, "D1_B1 dm_frac 0.917 vs analysed 0.920: just outside the 0.002 tolerance",
     lambda c: c.execute("UPDATE feed_batches SET dm_frac = 0.917 WHERE batch_id = 'D1_B1'")),
    ("R23", None, "D1_B1 dm_frac 0.85 vs analysed 0.92",
     lambda c: c.execute("UPDATE feed_batches SET dm_frac = 0.85 WHERE batch_id = 'D1_B1'")),
    ("R24", None, "sea_cage trial (SY1 system_kind NULL), temperatures without depth",
     lambda c: (c.execute("UPDATE trials SET system_type = 'sea_cage'"),
                c.execute("UPDATE systems SET system_kind = NULL WHERE system_id = 'SY1'"))),
    ("R25", None, "U1D1 feces analysed in a run of its own",
     lambda c: c.execute("UPDATE composition SET analysis_run_id = 'RUN_X' WHERE sample_id = 'FEC_U1D1'")),
    ("R26", "warning", "U1D1 feces collection spans the batch change",
     lambda c: c.execute("UPDATE composition SET collection_start = ? WHERE sample_id = 'FEC_U1D1'", (day(40),))),
    ("R26", None, "U1D1 feces collected after feeding ended (batch not identifiable)",
     lambda c: c.execute("UPDATE composition SET collection_start = ?, collection_end = ? WHERE sample_id = 'FEC_U1D1'",
                         (day(90), day(91)))),
    ("R27", None, "fish F1 dead on a day without a mortality",
     lambda c: m_fish(c, "dead", first_zero_mortality(c, "U1D1"))),
    ("R28", None, "post-random mediator o2_mg_l in a total-effect model",
     lambda c: (plan(c, "lmer(sgr ~ diet*temperature + o2_mg_l + (1|system))", estimand="total"),
                ins(c, "covariate_roles", plan_id="P1", variable="o2_mg_l", role="mediator", measured="post_random"))),
    ("R29", None, "diet D1 marker none while feces exist",
     lambda c: c.execute("UPDATE diets SET marker = 'none' WHERE diet_id = 'D1'")),
    ("R30", None, "two feces methods, ADC outcome declares single_method",
     # the trial's own plan (P1) declares outcome adc_protein with adc_method_handling single_method
     lambda c: c.execute("UPDATE composition SET feces_method = 'dissection' WHERE sample_id = 'FEC_U1D1'")),
    ("R30", "warning", "feces rows without feces_method (excluded from ADC)",
     lambda c: c.execute("UPDATE composition SET feces_method = NULL WHERE sample_id = 'FEC_U1D1'")),
    ("R31", None, "U1D1 / U1D2 become cages 10 m apart with different diets",
     lambda c: (c.execute("UPDATE units SET unit_type = 'cage', x_m = 0, y_m = 0 WHERE unit_id = 'U1D1'"),
                c.execute("UPDATE units SET unit_type = 'cage', x_m = 10, y_m = 0 WHERE unit_id = 'U1D2'"))),
    ("R32", None, "permit for 850 fish, 800 stocked (94 %)",
     lambda c: (ins(c, "ethics_permits", permit_id="EP1", approved_n_fish=850),
                ins(c, "trial_permits", trial_id=TRIAL, permit_id="EP1"))),
    ("R33", None, "weight_basis NULL on a weighing",
     lambda c: c.execute("UPDATE weighings SET weight_basis = NULL WHERE record_id = 'U2D1_final_S05'")),
    ("R34", None, "occasion mid2 after final (seq 4, earlier date)",
     lambda c: ins(c, "unit_occasions", unit_id="U1D1", occasion="mid2", seq=4, occasion_date=day(60), planned=1)),
    ("R35", None, "system-level temperature in a model without system stratum",
     lambda c: plan(c, "lmer(sgr ~ diet*temperature + (1|unit))", error_strata="unit")),
    ("R36", None, "U1D1 terminated for disease but has a final occasion",
     lambda c: c.execute("UPDATE units SET termination_reason = 'disease' WHERE unit_id = 'U1D1'")),
    ("R37", None, "tank mortality with interpolated weight",
     lambda c: c.execute("UPDATE events SET weight_source = 'estimated_interpolated' WHERE record_id = (SELECT"
                         " min(record_id) FROM events WHERE unit_id = 'U1D1' AND event_type = 'mortality' AND n_fish > 0)")),
    ("R38", None, "primary_locked run after unblinding",
     lambda c: ins(c, "analysis_runs", run_id="RUN1", trial_id=TRIAL, purpose="primary_locked", code_commit="abc",
                   run_at="2025-07-01T00:00:00Z")),
    ("R39", None, "off_protocol feed without deviation",
     lambda c: c.execute("UPDATE feeding SET off_protocol = 1 WHERE unit_id = 'U1D1' AND date = ?", (day(10),))),
    ("R40", None, "U1D1 mid1 weight from a calibrated camera, other units from samples", m_r40),
    ("R41", None, "SY1 reference temperature +5 C for the first 20 days",
     lambda c: c.execute("UPDATE environment SET value = value + 5 WHERE system_id = 'SY1' AND timestamp < ?",
                         (day(20),))),
    # ---- threshold boundaries: just outside the default -> must fire
    ("R04", None, "U1D1 harvest count 2 fish low (89 vs 91 expected, 2.2 % > 2 % tolerance)",
     lambda c: recount(c, "U1D1", 2)),
    ("R05", None, "U1D1 mid1 unit weight 6 % below d0 (limit 5 %)", lambda c: scale_mid1_to_d0(c, 0.94)),
    ("R06", None, "U1D1 d0 within-unit CV 22 % (limit 20 %)", lambda c: d0_cv(c, 22.0)),
    ("R06", None, "D2 d0 mean 6 % above reference D1 (limit 5 %)", lambda c: d2_mean_diff(c, 6.0)),
    ("R09", None, "U1D1 o2_sat_pct hourly sensor with a 7 h gap (max 6 h)", lambda c: o2_gap(c, 7)),
    ("R09", None, "U1D1 inlet temperature step 3.5 C within 1 h (max 3 C/h)", lambda c: temp_step(c, 3.5)),
    ("R09", None, "U1D1 temperature 4 identical daily readings (flat line >= 4)", lambda c: flat(c, 4)),
    ("R10", None, "calibrated camera estimate with 29 detections (min 30)", lambda c: detections(c, 29)),
    ("R14", None, "U1D1 removal_sampling event 2 days after the kill (max lag 1 day)", lambda c: kill_lag(c, 2)),
    ("R31", None, "U1D1 / U1D2 become cages 40 m apart with different diets (<= 50 m)", lambda c: cages(c, 40)),
]


NEGATIVE = [
    ("R23", "D1_B1 dm_frac 0.9185 vs analysed 0.920: inside the 0.002 tolerance",
     lambda c: c.execute("UPDATE feed_batches SET dm_frac = 0.9185 WHERE batch_id = 'D1_B1'")),
    ("R21", "dose_basis added with D1 as basal diet (dose 0) and D2 dosed",
     lambda c: c.execute("UPDATE diets SET dose_value = CASE diet_id WHEN 'D1' THEN 0 ELSE 2.0 END,"
                         " dose_unit = 'g_kg', dose_basis = 'added', dose_analyte = 'protein'")),
    ("R21", "dose_basis total_dm on both diets with analysed protein",
     lambda c: c.execute("UPDATE diets SET dose_value = CASE diet_id WHEN 'D1' THEN 1.0 ELSE 2.0 END,"
                         " dose_unit = 'g_kg', dose_basis = 'total_dm', dose_analyte = 'protein'")),
    # ---- threshold boundaries: just inside the default -> must stay silent (4th item: only
    # findings of that severity count, e.g. R04 reports a difference within tolerance as info)
    ("R04", "U1D1 harvest count 1 fish low (90 vs 91, 1.1 % < 2 % tolerance)", lambda c: recount(c, "U1D1", 1),
     "error"),
    ("R05", "U1D1 mid1 unit weight 4 % below d0 (limit 5 %)", lambda c: scale_mid1_to_d0(c, 0.96)),
    ("R06", "U1D1 d0 within-unit CV 18 % (limit 20 %)", lambda c: d0_cv(c, 18.0)),
    ("R06", "D2 d0 mean 4 % above reference D1 (limit 5 %)", lambda c: d2_mean_diff(c, 4.0)),
    ("R09", "U1D1 o2_sat_pct hourly sensor with a 5 h gap (max 6 h)", lambda c: o2_gap(c, 5)),
    ("R09", "U1D1 inlet temperature step 2.5 C within 1 h (max 3 C/h)", lambda c: temp_step(c, 2.5)),
    ("R09", "U1D1 temperature 3 identical daily readings (flat line needs 4)", lambda c: flat(c, 3)),
    ("R10", "calibrated camera estimate with 31 detections (min 30)", lambda c: detections(c, 31)),
    ("R14", "U1D1 removal_sampling event 1 day after the kill (max lag 1 day)", lambda c: kill_lag(c, 1)),
    ("R31", "U1D1 / U1D2 become cages 60 m apart with different diets (> 50 m)", lambda c: cages(c, 60)),
]


def key(f):
    return (f["rule"], f["severity"], f["unit_id"], f["record"], f["message"])


def copy(src):
    dst = sqlite3.connect(":memory:")
    src.backup(dst)
    dst.execute("PRAGMA foreign_keys = ON")
    return dst


def main():
    fails = []
    con, model, _ = make_trial(YAML)
    sev = {r["id"]: r["severity"] for r in model["cross_table_rules"]}
    vrules = [r["id"] for r in V.validator_rules(model)]
    print("validator rules in the YAML: %d" % len(vrules))

    # 1. clean trial
    clean = V.validate(con, model, TRIAL)
    errs = [f for f in clean if f["severity"] == "error"]
    print("clean trial: %s" % V.summary(clean)["by_severity"])
    for f in clean:
        print("  clean %-7s %s %s %s" % (f["severity"], f["rule"], f["unit_id"] or "-", f["message"][:110]))
    if errs:
        fails.append("clean trial has %d error finding(s): %s" % (len(errs), [key(f) for f in errs]))
    if not any(f["rule"] == "R03" and f["severity"] == "warning" and "pending" in f["message"] for f in clean):
        fails.append("clean trial: R03 pending exclusion (DEV1) not reported")
    s = V.summary(clean)
    if sum(s["by_severity"].values()) != len(clean):
        fails.append("summary() counts do not add up")
    base = {key(f) for f in clean}

    # 2. mutations
    exercised, collateral = set(), {}
    for rid, want, desc, fn in MUTATIONS:
        expected = want or sev[rid]
        c = copy(con)
        try:
            fn(c)
            c.commit()
        except sqlite3.Error as e:
            fails.append("%s mutation '%s' violates a database constraint: %s" % (rid, desc, e))
            continue
        new = [f for f in V.validate(c, model, TRIAL) if key(f) not in base]
        hit = [f for f in new if f["rule"] == rid and f["severity"] == expected]
        other = sorted({"%s/%s" % (f["rule"], f["severity"]) for f in new if f["rule"] != rid})
        ok = bool(hit)
        if ok and want is None and expected != sev[rid]:
            ok = False
        print("%s %s %-7s %s%s" % ("ok  " if ok else "FAIL", rid, expected, desc,
                                    "  [collateral: %s]" % ", ".join(other) if other else ""))
        if ok:
            print("       -> %s" % hit[0]["message"][:140])
            if other:
                collateral.setdefault(rid, other)
            else:
                exercised.add(rid)          # exactly this rule fired, with the expected severity
        else:
            fails.append("%s mutation '%s': no new %s finding; new: %s" % (
                rid, desc, expected, [(f["rule"], f["severity"], f["message"][:80]) for f in new]))
    waived = {r: "every mutation that fires it also fires %s (inherent overlap)" % ", ".join(collateral[r])
              for r in collateral if r not in exercised}
    missing = sorted(set(vrules) - exercised - set(waived))
    if missing:
        fails.append("validator rules without a passing mutation: %s" % ", ".join(missing))
    for r, why in sorted(waived.items()):
        print("waived %s: %s" % (r, why))

    # 2b. negative mutations: legitimate changes that must NOT make the rule fire
    for rid, desc, fn, *only in NEGATIVE:
        c = copy(con)
        try:
            fn(c)
            c.commit()
        except sqlite3.Error as e:
            fails.append("%s negative '%s' violates a database constraint: %s" % (rid, desc, e))
            continue
        new = [f for f in V.validate(c, model, TRIAL) if key(f) not in base and f["rule"] == rid
               and (not only or f["severity"] == only[0])]
        print("%s %s must stay silent: %s" % ("ok  " if not new else "FAIL", rid, desc))
        if new:
            fails.append("%s fired on a legitimate change '%s': %s" % (rid, desc, new[0]["message"][:100]))

    # 3. metrics module unavailable -> info, no crash
    saved = V.M
    V.M = None
    try:
        fs = V.validate(con, model, TRIAL)
        for rid in ("R04", "R05", "R06", "R40"):
            if not any(f["rule"] == rid and f["severity"] == "info" and "metrics module unavailable" in f["message"]
                       for f in fs):
                fails.append("%s: no 'metrics module unavailable' info without metrics" % rid)
        if any("rule raised" in f["message"] for f in fs):
            fails.append("a rule crashed without the metrics module")
    finally:
        V.M = saved

    # 5. the analysis plan: the synthetic trial records one, so the plan-dependent rules are checked
    # on it; on a trial without plan rows they must be reported as not checked, never as passed
    plan_rules = ("R28", "R30", "R35", "R38")
    for rid in plan_rules:
        if any(f["rule"] == rid for f in clean):
            fails.append("%s: finding on the clean trial although its plan is recorded" % rid)
    c = copy(con)
    for t in ("analysis_runs", "covariate_roles", "planned_outcomes", "analysis_plans"):
        c.execute('DELETE FROM "%s"' % t)
    fs = V.validate(c, model, TRIAL)
    for rid in plan_rules:
        if not any(f["rule"] == rid and f["severity"] == "info" and "not checked" in f["message"] for f in fs):
            fails.append("%s: no 'not checked' info on a trial without plan rows" % rid)
    print("ok   plan rules %s: silent on the recorded plan, 'not checked' without plan rows" % ", ".join(plan_rules))

    with open(os.path.join(HERE, ".coverage_rules.json"), "w") as f:
        json.dump({"exercised": sorted(exercised), "waived": waived}, f, indent=1)

    if fails:
        print("\nFAILURES:")
        for x in fails:
            print("  " + x)
        print("FAILED: %d problem(s)" % len(fails))
        return 1
    print("%d/%d validator rules fire alone on a mutation (%d mutations, %d waived); clean trial has no error" % (
        len(exercised), len(vrules), len(MUTATIONS), len(waived)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
