#!/usr/bin/env python3
"""Estimate rule thresholds (validation_params) and power inputs from HISTORICAL trials.

The defaults in data_model.yaml are placeholders. Once an organisation's past trials are
loaded into this schema, this script derives each threshold from the observed variation, so
rules flag what is unusual for THAT organisation instead of an arbitrary number.

Estimated (each with the number of observations it rests on):
  max_initial_cv_pct          95th percentile of within-unit CV of d0 individual/sample weights
  max_initial_mean_diff_pct   2 x the between-unit CV of d0 unit means within a trial (pooled)
  n_balance_tolerance_pct     95th percentile of |n_at_risk - n_count| / n_count at recounts
  max_negative_growth_pct     3 x the 95th percentile of the relative standard error of
                              sample-based occasion means (a drop beyond it is not sampling noise)
  min_recovery                0.9 x the 5th percentile of recovery-test recoveries
  max_temp_jump_c_h           99.9th percentile of |dT| between consecutive readings <= 1 h apart
Power inputs (planned_outcomes): cv_within_unit_pct, cv_between_unit_pct, icc of final weight.
Not estimated (policy decisions, kept as configured): min_coverage_pct, min_gain_kg, base_temp_c, ...

Usage: calibrate_params.py <historical.db> [data_model.yaml]
"""
import math
import os
import statistics as st
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics as M  # noqa: E402

MIN_OBS = 5   # below this an estimate is reported as not estimable


def pct(values, p):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def cv(values):
    return 100 * st.stdev(values) / st.mean(values) if len(values) > 1 and st.mean(values) else None


def fish_weights(con, unit_id, occasion):
    return [r[0] for r in con.execute(
        "SELECT weight_g FROM weighings WHERE unit_id = ? AND occasion = ? AND sampling_type IN ('sample','individual') "
        "AND weight_g IS NOT NULL AND COALESCE(qc_flag,'raw') <> 'removed' "
        "AND COALESCE(weight_basis,'round') = 'round'", (unit_id, occasion))]


def calibrate(con):
    out, notes = {}, {}
    units = con.execute("SELECT unit_id, trial_id FROM units").fetchall()

    def put(name, value, n, basis):
        if value is None or n < MIN_OBS:
            notes[name] = "not estimable: %d observations (need %d)" % (n, MIN_OBS)
        else:
            out[name] = {"suggested": round(value, 3), "n_obs": n, "basis": basis}

    # within-unit CV at d0 and between-unit CV of d0 means (pooled over trials)
    cvs, by_trial = [], {}
    for uid, tid in units:
        w = fish_weights(con, uid, "d0")
        if len(w) >= 3:
            cvs.append(cv(w))
            by_trial.setdefault(tid, []).append(st.mean(w))
    put("max_initial_cv_pct", pct(cvs, 95), len(cvs), "95th percentile of within-unit CV of d0 weights")
    between = [cv(m) for m in by_trial.values() if len(m) >= 3]
    n_between = sum(len(m) for m in by_trial.values())
    put("max_initial_mean_diff_pct", 2 * st.mean(between) if between else None, n_between,
        "2 x between-unit CV of d0 unit means")

    # count balance at recounts after d0
    gaps = []
    for uid, _ in units:
        for o in M.occasions(con, uid):
            if o["occasion"] in ("stocking", "d0"):
                continue
            c = M.count_at(con, uid, o["occasion"])
            if not c or not c[0]:
                continue
            # running total carried forward from the PREVIOUS count (n_at_risk at a counted
            # date returns that count itself, so it cannot reveal a discrepancy)
            n = M.expected_count(con, uid, o["occasion"])
            if n is not None:
                gaps.append(100 * abs(n - c[0]) / c[0])
    put("n_balance_tolerance_pct", max(pct(gaps, 95) or 0, 0.5) if gaps else None, len(gaps),
        "95th percentile of |n_at_risk - count| / count at recounts (floor 0.5 %)")

    # sampling noise of sample-based means
    rse = []
    for uid, _ in units:
        for o in M.occasions(con, uid):
            w = fish_weights(con, uid, o["occasion"])
            if len(w) >= 5:
                rse.append(100 * st.stdev(w) / math.sqrt(len(w)) / st.mean(w))
    put("max_negative_growth_pct", 3 * pct(rse, 95) if rse else None, len(rse),
        "3 x 95th percentile of relative SE of occasion means")

    rec = [r[0] / r[1] for r in con.execute(
        "SELECT feed_recovered_dm_g, feed_in_dm_g FROM recovery_tests WHERE feed_in_dm_g > 0 AND feed_recovered_dm_g IS NOT NULL")]
    put("min_recovery", 0.9 * pct(rec, 5) if rec else None, len(rec), "0.9 x 5th percentile of observed recoveries")

    jumps = []
    for key in ("unit_id", "system_id"):
        series = {}
        for k, ts, v in con.execute(
                "SELECT %s, timestamp, value FROM environment WHERE variable = 'temp_c' AND %s IS NOT NULL "
                "AND COALESCE(aggregation,'instant') IN ('instant','hourly_mean') AND COALESCE(qc_flag,'raw') <> 'removed' "
                "ORDER BY %s, timestamp" % (key, key, key)):
            series.setdefault(k, []).append((ts, v))
        for rows in series.values():
            for (t1, v1), (t2, v2) in zip(rows, rows[1:]):
                h = (_ts(t2) - _ts(t1)) / 3600
                if 0 < h <= 1:
                    jumps.append(abs(v2 - v1) / h)
    put("max_temp_jump_c_h", pct(jumps, 99.9) if jumps else None, len(jumps),
        "99.9th percentile of |dT| per hour between readings <= 1 h apart")

    # power inputs: final weight
    within, means = [], {}
    for uid, tid in units:
        w = fish_weights(con, uid, "final")
        if len(w) >= 3:
            within.append(cv(w))
            means.setdefault(tid, []).append(st.mean(w))
    power = {}
    if within:
        power["cv_within_unit_pct"] = round(st.mean(within), 2)
    b = [cv(m) for m in means.values() if len(m) >= 3]
    if b:
        power["cv_between_unit_pct"] = round(st.mean(b), 2)
    if within and b:
        # ICC of fish weights with unit as cluster, from the two CVs (same mean)
        sb, sw = power["cv_between_unit_pct"] ** 2, power["cv_within_unit_pct"] ** 2
        power["icc"] = round(sb / (sb + sw), 3)
    power["note"] = ("cv_between_unit is the CV of unit means and still contains sampling error "
                     "(sw^2/n per unit); for designs with few fish weighed per unit subtract it first")
    return {"thresholds": out, "not_estimated": notes, "power_inputs": power}


def _ts(s):
    import datetime as dt
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


if __name__ == "__main__":
    import sqlite3
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    con = sqlite3.connect(sys.argv[1])
    print(json.dumps(calibrate(con), indent=1))
