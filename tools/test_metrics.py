#!/usr/bin/env python3
"""Tests of tools/metrics.py: synthetic trial vs known truth, plus hand-computed tiny databases.

Every derived_metrics key of data_model.yaml must be in metrics.METRICS and must be compared to an
independent expected value (truth or hand arithmetic) somewhere below; the record is written to
tools/.coverage_metrics.json = {"exercised": [...], "waived": {key: reason}}.

Usage: test_metrics.py [data_model.yaml]      exit 1 on any failure
"""
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import metrics as M  # noqa: E402
from build_db import build, load_model  # noqa: E402
from synthetic_trial import make_trial  # noqa: E402

YAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")
COVERAGE_FILE = os.path.join(HERE, ".coverage_metrics.json")
WAIVED = {}            # key -> concrete reason; intentionally empty
FAIL = []
N = [0]
EXERCISED = set()


def check(name, ok, detail="", keys=()):
    """keys = derived_metrics keys whose output this check compares with an expected value."""
    N[0] += 1
    EXERCISED.update(keys)
    if not ok:
        FAIL.append("%s %s" % (name, detail))
        print("FAIL", name, detail)


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def rel(a, b):
    return abs(a - b) / abs(b)


F = M.METRICS          # call through the registry so its entries are what is tested


# ------------------------------------------------------------------ registry
def test_registry():
    keys = set(load_model(YAML)["derived_metrics"])
    missing = sorted(keys - set(F))
    check("METRICS covers every derived_metrics key", not missing, missing)
    check("METRICS has no unknown keys", not (set(F) - keys), sorted(set(F) - keys))
    check("METRICS values callable", all(callable(v) for v in F.values()))


# ------------------------------------------------------------------ synthetic trial vs truth
def test_synthetic():
    con, model, truth = make_trial(YAML)
    M.use_model(con, model)
    rows = {r["unit_id"]: r for r in M.unit_summary(con, "T1")}
    print("%-5s %-28s %-28s %-24s %-22s %-22s %-14s %-16s %-14s" % (
        "unit", "gain kg (metric/truth)", "fcr_bio (metric/truth)", "protein intake g", "intake DM g",
        "sgr %/d", "adc prot", "surv %", "dd cov o+i"))
    for uid, t in truth.items():
        r = rows[uid]
        check(uid + " n_fish_d0", F["n_fish_d0"](con, uid) == t["n_d0"], (F["n_fish_d0"](con, uid), t["n_d0"]), ["n_fish_d0"])
        fin = [o for o in M.occasions(con, uid) if o["occasion"] == "final"][0]
        check(uid + " n_at_risk(final)", F["n_at_risk"](con, uid, fin["date"]) == t["n_final"], keys=["n_at_risk"])
        check(uid + " b_start", rel(F["biomass_kg"](con, uid, "d0"), t["b_start_kg"]) <= 0.001, keys=["biomass_kg"])
        check(uid + " b_end", rel(F["biomass_kg"](con, uid, "final"), t["b_end_kg"]) <= 0.001, keys=["biomass_kg"])
        check(uid + " summary b_start/b_end", rel(r["b_start_kg"], t["b_start_kg"]) <= 0.001
              and rel(r["b_end_kg"], t["b_end_kg"]) <= 0.001)
        check(uid + " w_final", rel(F["unit_weight_g"](con, uid, "final"), t["w_final_g"]) <= 0.001, keys=["unit_weight_g"])
        check(uid + " biomass_gain", rel(r["biomass_gain_kg"], t["gain_kg"]) <= 0.005, (r["biomass_gain_kg"], t["gain_kg"]),
              ["biomass_gain_kg"])
        check(uid + " biomass_gain registry", close(F["biomass_gain_kg"](con, uid, "d0", "final"), r["biomass_gain_kg"]))
        check(uid + " fcr_biological", rel(r["fcr_biological"], t["fcr_bio"]) <= 0.02, (r["fcr_biological"], t["fcr_bio"]),
              ["fcr_biological"])
        check(uid + " intake_dm", rel(r["intake_dm_g"], t["feed_dm_eaten_g"]) <= 0.02, keys=["intake_dm_g"])
        check(uid + " protein_intake", rel(r["protein_intake_g"], t["prot_eaten_g"]) <= 0.02,
              (r["protein_intake_g"], t["prot_eaten_g"]), ["protein_intake_g", "nutrient_intake_g"])
        check(uid + " per", rel(r["per"], t["gain_kg"] * 1000 / t["prot_eaten_g"]) <= 0.02, keys=["per"])
        check(uid + " adc_protein", abs(r["adc_pct"]["protein"] - t["adc_protein_pct"]) <= 0.1,
              (r["adc_pct"]["protein"], t["adc_protein_pct"]), ["adc_pct"])
        check(uid + " adc_dm (generator: 75 % of DM digested)", abs(r["adc_pct"]["dm"] - 75.0) <= 0.1, keys=["adc_pct"])
        check(uid + " survival", r["survival_pct"] == t["survival_pct"], (r["survival_pct"], t["survival_pct"]),
              ["survival_pct"])
        check(uid + " sgr", abs(r["sgr_pct_day"] - t["sgr_true_pct"]) <= 0.05, (r["sgr_pct_day"], t["sgr_true_pct"]),
              ["sgr_pct_day"])
        c = r["degree_days_coverage"]
        check(uid + " dd coverage reported", r["degree_days"] is not None and c["n_eligible"] == 84
              and c["coverage_pct"] is not None, c)
        # generator: liver = 1.2 %, viscera (incl. liver) = 9 % of body weight, rounded to 0.01 g
        check(uid + " hsi", abs(r["hsi_pct"] - 1.2) < 0.01 and abs(r["vsi_pct"] - 9.0) < 0.01, (r["hsi_pct"], r["vsi_pct"]),
              ["hsi_pct", "vsi_pct"])
        check(uid + " density d0 (volume 1 m3)", rel(F["density_kg_m3"](con, uid, fin["date"][:4] + "-03-03"),
                                                    t["b_start_kg"]) <= 0.001, keys=["density_kg_m3"])
        print("%-5s %-28s %-28s %-24s %-22s %-22s %-14s %-16s %-14s" % (
            uid, "%.3f / %.3f" % (r["biomass_gain_kg"], t["gain_kg"]),
            "%.4f / %.4f (%+.2f%%)" % (r["fcr_biological"], t["fcr_bio"], 100 * (r["fcr_biological"] / t["fcr_bio"] - 1)),
            "%.0f / %.0f" % (r["protein_intake_g"], t["prot_eaten_g"]),
            "%.0f / %.0f" % (r["intake_dm_g"], t["feed_dm_eaten_g"]),
            "%.4f / %.4f" % (r["sgr_pct_day"], t["sgr_true_pct"]),
            "%.3f / %.1f" % (r["adc_pct"]["protein"], t["adc_protein_pct"]),
            "%s / %s" % (r["survival_pct"], t["survival_pct"]), "%d+%d" % (c["n_observed"], c["n_imputed"])))
    for uid, t in truth.items():
        check(uid + " expected_count(final) = truth n_final", M.expected_count(con, uid, "final") == t["n_final"],
              (M.expected_count(con, uid, "final"), t["n_final"]))
    before = {uid: (M.expected_count(con, uid, "final"), M.count_at(con, uid, "final")[0]) for uid in truth}
    con.execute("UPDATE weighings SET n_fish = n_fish - 3 WHERE sampling_type = 'harvest'")
    for uid in truth:
        check(uid + " recount -3: expected_count unchanged, count_at -3",
              M.expected_count(con, uid, "final") == before[uid][0] and M.count_at(con, uid, "final")[0] == before[uid][1] - 3)
    con.rollback()
    c = rows["U3D1"]["degree_days_coverage"]
    check("U3D1 sensor outage filled by system reference", c["n_imputed"] >= 14 and c["coverage_pct"] == 100.0, c)
    st = F["unit_analysis_status"](con, "U2D2")
    check("U2D2 status: pending exclusion, still ITT", st["exclusion_pending"] and not st["excluded_decided"] and st["itt"]
          and st["has_d0"] and st["has_final"] and st["follow_up_days"] == 84 and st["n_occasions"] == 4, st,   # SY2: stocking, d0, mid1, final
          ["unit_analysis_status"])
    check("day_since_d0 mid1 = 42", F["day_since_d0"](con, "U1D1", "mid1") == 42, keys=["day_since_d0"])
    w = M.unit_weight(con, "U1D1", "mid1")     # mid1 has no count -> sample; the removed row is dropped
    check("U1D1 mid1 sample n excludes qc removed", w["w_source"] == "sample" and w["n_weight"] == 19, w, ["unit_weight_g"])
    dr = F["unit_day_risk"](con, "U1D1")
    check("unit_day_risk length", len(dr) == 84, len(dr), ["unit_day_risk"])
    check("unit_day_risk deaths", sum(r["n_event"] for r in dr) == truth["U1D1"]["deaths"], keys=["unit_day_risk"])
    check("unit_day_risk censored (5 sampled)", sum(r["n_censored"] for r in dr) == 5, keys=["unit_day_risk"])
    p = M.params(con, "T1")
    check("params defaults", p["min_coverage_pct"] == 90 and p["dead_weight_ratio"] == 1)


# ------------------------------------------------------------------ tiny hand-built databases
def day(k):
    return (dt.date(2025, 1, 1) + dt.timedelta(days=k)).isoformat()


def tiny(stock_n=100, w=100.0, basis="round"):
    """One tank U, Europe/Oslo, d0 on 2025-01-01 with a stocking count."""
    con, model = build(YAML)
    M.use_model(con, model)

    def ins(t, **r):
        con.execute("INSERT INTO %s (%s) VALUES (%s)" % (t, ",".join(r), ",".join("?" * len(r))), list(r.values()))
    ins("trials", trial_id="T", start_date=day(0))
    ins("sites", site_id="S", timezone="Europe/Oslo", latitude=60.0, longitude=10.0)
    ins("systems", system_id="SY", trial_id="T", site_id="S", temp_control="controlled", target_temp_c=10.0)
    ins("diets", diet_id="D", trial_id="T")
    ins("feed_batches", batch_id="B", diet_id="D", pellet_mm=3.0, dm_frac=0.9)
    ins("units", unit_id="U", trial_id="T", system_id="SY", diet_id="D", unit_type="tank")
    ins("unit_occasions", unit_id="U", occasion="d0", seq=1, occasion_date=day(0))
    ins("weighings", record_id="W0", unit_id="U", occasion="d0", date=day(0), sampling_type="stocking",
        n_fish=stock_n, biomass_kg=stock_n * w / 1000, weight_basis=basis)
    return con, ins


def ev(ins, rid, date, etype, n, oc, kg=None, ws=None, rel_=None, unit="U"):
    ins("events", record_id=rid, unit_id=unit, date=date, event_type=etype, n_fish=n, outcome_class=oc,
        biomass_kg=kg, weight_source=ws or "weighed", relative_to_weighing=rel_)


def final(ins, k, n, kg, typ="harvest"):
    ins("unit_occasions", unit_id="U", occasion="final", seq=2, occasion_date=day(k))
    ins("weighings", record_id="WF", unit_id="U", occasion="final", date=day(k), sampling_type=typ,
        n_fish=n, biomass_kg=kg, weight_basis="round")


def test_rich_hand_computed():
    """4-day window, every period metric against hand arithmetic.
    d0: 100 fish 10 kg (100 g); 2 deaths on day 1 (0.2 kg, after weighing: in window);
    final day 4 harvest count 97 fish 11.64 kg (120 g) -> 1 fish unaccounted."""
    con, ins = tiny()
    con.execute("UPDATE units SET volume_m3 = 2.0 WHERE unit_id = 'U'")
    final(ins, 4, 97, 11.64)
    ev(ins, "E1", day(1), "mortality", 2, "death", 0.2)
    # temperature: day0 two instant readings 06:00 / 18:00 local (UTC+1) -> time-weighted 10.0;
    # day1 daily_mean 11; day3 only at system reference 13; day2 nowhere -> interpolated 12.
    for rid, ts, v, agg in (("T0a", day(0) + "T05:00:00Z", 8.0, "instant"), ("T0b", day(0) + "T17:00:00Z", 12.0, "instant"),
                            ("T1", day(1) + "T11:00:00Z", 11.0, "daily_mean")):
        ins("environment", record_id=rid, timestamp=ts, unit_id="U", variable="temp_c", aggregation=agg, value=v)
    ins("environment", record_id="S0", timestamp=day(0) + "T11:00:00Z", system_id="SY", variable="temp_c",
        aggregation="daily_mean", value=99.0)                  # ignored: unit has its own day-0 value
    ins("environment", record_id="S3", timestamp=day(3) + "T11:00:00Z", system_id="SY", variable="temp_c",
        aggregation="daily_mean", value=13.0)
    # recovery tests: only (tank, 3 mm, 10 C) matches unit_type, pellet and the 11.5 +- 2 C band
    for rid, ut, pm, tc, rec in (("R1", "tank", 3.0, 10.0, 80.0), ("R2", "tank", 3.0, 20.0, 50.0),
                                 ("R3", "tank", 6.0, 11.0, 10.0), ("R4", "cage", 3.0, 11.0, 99.0)):
        ins("recovery_tests", record_id=rid, trial_id="T", unit_type=ut, pellet_mm=pm, temp_c=tc,
            feed_in_dm_g=100.0, feed_recovered_dm_g=rec)
    # feed 100 g/day (dm 0.9); uneaten 10 g wet on days 0-2 (dm frac 0.5 measured on day 0 only), day 3 not collected
    for k in range(4):
        ins("feeding", unit_id="U", date=day(k), batch_id="B", diet_id="D", feed_given_g=100.0)
    for k in range(3):
        ins("uneaten_feed", unit_id="U", date=day(k), uneaten_wet_g=10.0, uneaten_dm_frac=0.5 if k == 0 else None)
    ins("composition", record_id="CP", trial_id="T", sample_id="DB", diet_id="D", batch_id="B", sample_type="diet",
        analyte="protein", value=450.0, value_unit="g_kg", basis="as_fed")
    ins("composition", record_id="WI", trial_id="T", sample_id="WBI", sample_type="whole_body_initial",
        analyte="protein", value=160.0, value_unit="g_kg", basis="wet")
    ins("composition", record_id="WFp", trial_id="T", sample_id="WBF", unit_id="U", sample_type="whole_body_final",
        date=day(4), analyte="protein", value=55.0, value_unit="pct", basis="dry_matter")
    ins("composition", record_id="WFd", trial_id="T", sample_id="WBF", unit_id="U", sample_type="whole_body_final",
        date=day(4), analyte="dm", value=320.0, value_unit="g_kg", basis="wet")
    ins("unit_geometry", record_id="G1", unit_id="U", valid_from=day(2), volume_m3=4.0)
    ins("deviations", record_id="DV", trial_id="T", date=day(1), unit_id="U", category="exclusion",
        exclusion_scope="unit", exclusion_reason="technical", description="pending")

    W = lambda k: 100.0 * 1.2 ** (k / 4)            # noqa: E731  geometric interpolation 100 -> 120 g
    n = {0: 100, 1: 100, 2: 98, 3: 98, 4: 97}
    B = {k: n[k] * W(k) / 1000 for k in range(4)}
    gain = 11.64 - 10.0 + 0.2
    intake = 4 * (90.0 - 5.0 / 0.8)                 # 5 g uneaten DM/day, day 3 imputed at 15/270 x 90 = 5
    prot = intake * 500.0 / 1000                    # 450 g/kg as fed / 0.9 = 500 g/kg DM
    c_end = 550.0 * 0.32                            # 55 % of DM x 320 g DM/kg wet = 176 g/kg wet
    c_i = (160.0 + c_end) / 2                       # no unit whole-body sample within 2 days of day 1
    ret = 100 * (11.64 * c_end - 10.0 * 160.0 + 1 * 0.2 * c_i) / prot
    pm = M.period_metrics(con, "U", "d0", "final")
    a = lambda k: F[k](con, "U", "d0", "final")     # noqa: E731

    # strengthened n_at_risk: day 2 (98) equals no count row (100 at d0, 97 at final)
    for k, exp in n.items():
        check("n_at_risk day %d" % k, F["n_at_risk"](con, "U", day(k)) == exp, F["n_at_risk"](con, "U", day(k)), ["n_at_risk"])
    check("expected_count final = 100 - 2 (recount shows 97)", M.expected_count(con, "U", "final") == 98
          and M.count_at(con, "U", "final")[0] == 97, M.expected_count(con, "U", "final"))
    check("expected_count d0 (no earlier count) = None", M.expected_count(con, "U", "d0") is None)
    check("w_interp day 2", close(F["w_interp"](con, "U", day(2)), W(2)), keys=["w_interp"])
    check("biomass_kg day 2", close(F["biomass_kg"](con, "U", date_iso=day(2)), B[2]), keys=["biomass_kg"])
    check("biomass_kg final", close(F["biomass_kg"](con, "U", "final"), 11.64), keys=["biomass_kg"])
    check("biomass_gain", close(a("biomass_gain_kg"), gain), (a("biomass_gain_kg"), gain), ["biomass_gain_kg"])
    check("sgr", close(a("sgr_pct_day"), 100 * math.log(1.2) / 4), keys=["sgr_pct_day"])
    check("degree_days 10+11+12+13", close(a("degree_days"), 46.0), a("degree_days"), ["degree_days"])
    dc = pm["degree_days_coverage"]
    check("degree_days coverage 2 obs + 2 imp", (dc["n_observed"], dc["n_imputed"], dc["coverage_pct"]) == (2, 2, 100.0), dc)
    check("tgc", close(a("tgc"), 1000 * (120 ** (1 / 3) - 100 ** (1 / 3)) / 46.0), keys=["tgc"])
    check("temp_range_ok (11.5 within 10 +- 2)", pm["temp_range_ok"] is True)
    check("recovery", close(a("recovery"), 0.8), a("recovery"), ["recovery"])
    check("intake_dm_g", close(a("intake_dm_g"), intake), (a("intake_dm_g"), intake), ["intake_dm_g"])
    ic = pm["intake_dm_coverage"]
    check("intake coverage 3 obs + 1 imp", (ic["n_eligible"], ic["n_observed"], ic["n_imputed"]) == (4, 3, 1), ic)
    check("fcr_gross", close(a("fcr_gross"), 0.4 / 1.64), keys=["fcr_gross"])
    check("fcr_apparent", close(a("fcr_apparent"), 0.4 / gain), keys=["fcr_apparent"])
    check("fcr_biological", close(a("fcr_biological"), intake / 1000 / gain), keys=["fcr_biological"])
    check("fish_days", close(a("fish_days"), 396.0), a("fish_days"), ["fish_days"])
    check("fi_pct_bw_day", close(a("fi_pct_bw_day"), 100 * intake / (sum(B.values()) * 1000)), keys=["fi_pct_bw_day"])
    check("nutrient_intake protein", close(F["nutrient_intake_g"](con, "U", "d0", "final", "protein"), prot),
          keys=["nutrient_intake_g"])
    check("protein_intake", close(a("protein_intake_g"), prot), keys=["protein_intake_g"])
    check("retention protein", close(F["retention_pct"](con, "U", "d0", "final", "protein"), ret),
          (F["retention_pct"](con, "U", "d0", "final"), ret), ["retention_pct"])
    check("per", close(a("per"), gain * 1000 / prot), keys=["per"])
    check("density day 1 (units.volume_m3 = 2)", close(F["density_kg_m3"](con, "U", day(1)), B[1] / 2), keys=["density_kg_m3"])
    check("density day 3 (unit_geometry 4 m3)", close(F["density_kg_m3"](con, "U", day(3)), B[3] / 4), keys=["density_kg_m3"])
    check("day_since_d0 final", F["day_since_d0"](con, "U", "final") == 4, keys=["day_since_d0"])
    check("survival simple 98 %", close(F["survival_pct"](con, "U"), 98.0), F["survival_pct"](con, "U"), ["survival_pct"])
    st = F["unit_analysis_status"](con, "U")
    check("unit_analysis_status", (st["has_d0"], st["has_final"], st["follow_up_days"], st["n_occasions"],
                                   st["exclusion_pending"], st["excluded_decided"], st["w_source_by_occasion"])
          == (True, True, 4, 2, True, False, {"d0": "count", "final": "count"}), st, ["unit_analysis_status"])
    rk = F["unit_day_risk"](con, "U")
    check("unit_day_risk rows", [(r["n_at_risk"], r["n_event"]) for r in rk] == [(100, 0), (100, 2), (98, 0), (98, 0)],
          rk, ["unit_day_risk"])


def test_bulk_lots_with_mean_weight():
    """Count lots that report n and MEAN weight only (no biomass_kg): B_lot = n * weight_g / 1000."""
    con, ins = tiny()
    ins("unit_occasions", unit_id="U", occasion="mid1", seq=2, occasion_date=day(10))
    ins("weighings", record_id="L1", unit_id="U", occasion="mid1", date=day(10), lot_no=1, sampling_type="bulk",
        n_fish=60, weight_g=150.0)
    ins("weighings", record_id="L2", unit_id="U", occasion="mid1", date=day(10), lot_no=2, sampling_type="bulk",
        n_fish=40, weight_g=200.0, biomass_kg=8.0)
    n, b = M.count_at(con, "U", "mid1")
    check("count lots: n", n == 100, n, ["count_rows"])
    check("count lots: B from mean weight + biomass", close(b, 60 * 150 / 1000 + 8.0), b, ["count_rows", "biomass_kg"])
    w = F["unit_weight_g"](con, "U", "mid1")
    check("count lots: W = 1000 B / n", close(w, 1000 * 17.0 / 100), w, ["unit_weight_g"])


def test_adc_wet_basis_feces():
    """Feces reported on a WET basis with a dm analyte must give the same ADC as on DM basis."""
    con, model, truth = make_trial(YAML)
    M.use_model(con, model)
    uid = "U1D1"
    dm_ref = F["adc_pct"](con, uid, "protein")
    dmadc_ref = F["adc_pct"](con, uid, "dm")     # dry-matter ADC depends on the marker alone
    con.execute("UPDATE composition SET value = value * 0.25, basis = 'wet' WHERE unit_id = ? AND sample_type = 'feces'",
                (uid,))
    con.execute("INSERT INTO composition (record_id, trial_id, sample_id, unit_id, sample_type, feces_method, "
                "collection_start, collection_end, analyte, value, value_unit, basis) SELECT 'CF_DM', trial_id, "
                "sample_id, unit_id, sample_type, feces_method, collection_start, collection_end, 'dm', 250, 'g_kg', "
                "'wet' FROM composition WHERE record_id = 'CF_U1D1_Y'")
    wet = F["adc_pct"](con, uid, "protein")
    check("adc on wet-basis feces equals DM-basis ADC", close(wet, dm_ref, 1e-6), (wet, dm_ref), ["adc_pct"])
    check("adc on wet-basis feces equals truth", close(wet, truth[uid]["adc_protein_pct"], 0.1), wet, ["adc_pct"])
    dmadc = F["adc_pct"](con, uid, "dm")
    check("dry-matter ADC on wet-basis feces unchanged (75 %)", close(dmadc, dmadc_ref, 1e-6) and close(dmadc, 75.0, 0.05),
          (dmadc, dmadc_ref), ["adc_pct"])


def test_dose_realised():
    con, ins = tiny()
    ins("feed_batches", batch_id="B2", diet_id="D", pellet_mm=3.0, dm_frac=0.8)
    ins("units", unit_id="U2", trial_id="T", system_id="SY", diet_id="D", unit_type="tank")
    for k in range(4):
        ins("feeding", unit_id="U", date=day(k), batch_id="B", diet_id="D", feed_given_g=100.0)    # 360 g DM
    ins("feeding", unit_id="U2", date=day(0), batch_id="B2", diet_id="D", feed_given_g=200.0)      # 160 g DM
    ins("feeding", unit_id="U2", date=day(1), batch_id="B2", diet_id="D", feed_given_g=999.0, qc_flag="removed")
    ins("composition", record_id="Z1", trial_id="T", sample_id="DB", diet_id="D", batch_id="B", sample_type="diet",
        analyte="Zn", value=90.0, value_unit="mg_kg", basis="as_fed")          # 100 mg/kg DM
    ins("composition", record_id="Z2", trial_id="T", sample_id="DB2", diet_id="D", batch_id="B2", sample_type="diet",
        analyte="Zn", value=160.0, value_unit="mg_kg", basis="dry_matter")
    for basis, exp in (("total_dm", (100 * 360 + 160 * 160) / 520), ("total_as_fed", (90 * 360 + 128 * 160) / 520),
                       ("added", None)):          # added without a basal diet -> NULL
        con.execute("UPDATE diets SET dose_value = 1, dose_unit = 'mg_kg', dose_analyte = 'Zn', dose_basis = ?", (basis,))
        got = F["dose_realised"](con, "D")
        check("dose_realised %s" % basis, (got is None and exp is None) or close(got, exp), (got, exp), ["dose_realised"])
    con.execute("UPDATE diets SET dose_basis = 'total_dm', dose_unit = 'pct'")
    check("dose_realised in pct", close(F["dose_realised"](con, "D"), (100 * 360 + 160 * 160) / 520 / 10000),
          keys=["dose_realised"])
    # added: C(diet) - C(basal diet with dose_value 0), both on DM: basal Zn 30 mg/kg DM
    ins("diets", diet_id="DB", trial_id="T", dose_value=0, dose_unit="mg_kg", dose_analyte="Zn", dose_basis="added")
    ins("feed_batches", batch_id="BB", diet_id="DB", pellet_mm=3.0, dm_frac=0.9)
    ins("units", unit_id="U3", trial_id="T", system_id="SY", diet_id="DB", unit_type="tank")
    ins("feeding", unit_id="U3", date=day(0), batch_id="BB", diet_id="DB", feed_given_g=100.0)
    ins("composition", record_id="Z3", trial_id="T", sample_id="DBB", diet_id="DB", batch_id="BB", sample_type="diet",
        analyte="Zn", value=30.0, value_unit="mg_kg", basis="dry_matter")
    con.execute("UPDATE diets SET dose_basis = 'added', dose_unit = 'mg_kg' WHERE diet_id = 'D'")
    check("dose_realised added = diet - basal", close(F["dose_realised"](con, "D"), (100 * 360 + 160 * 160) / 520 - 30),
          (F["dose_realised"](con, "D"),), ["dose_realised"])
    check("dose_realised of the basal diet itself is 0", F["dose_realised"](con, "DB") == 0.0, keys=["dose_realised"])
    ins("diets", diet_id="DB2", trial_id="T", dose_value=0)
    check("dose_realised added with two basal diets -> NULL", F["dose_realised"](con, "D") is None, keys=["dose_realised"])


def test_fish_level_indices():
    con, ins = tiny()
    for k, (w, L, basis) in enumerate(((100.0, 20.0, "round"), (120.0, 22.0, "round"), (90.0, 20.0, "gutted"),
                                       (80.0, 20.0, None))):
        ins("weighings", record_id="S%d" % k, unit_id="U", occasion="d0", date=day(0), sampling_type="sample",
            specimen_no=k + 1, weight_g=w, fork_length_cm=L, weight_basis=basis)
    exp_k = (1.25 + 100 * 120 / 22 ** 3 + 1.0) / 3      # gutted row excluded, NULL basis = round
    check("condition_k", close(F["condition_k"](con, "U", "d0"), exp_k), F["condition_k"](con, "U", "d0"), ["condition_k"])
    ins("fish_measurements", record_id="F1", unit_id="U", date=day(0), body_weight_g=200.0, liver_g=3.0, viscera_g=20.0,
        viscera_includes_liver=1, gonad_g=1.0, fillet_g=120.0)
    ins("fish_measurements", record_id="F2", unit_id="U", date=day(0), body_weight_g=100.0, liver_g=1.0, viscera_g=8.0,
        viscera_includes_liver=0, fillet_g=60.0)
    ins("fish_measurements", record_id="F3", unit_id="U", date=day(0), body_weight_g=50.0, liver_g=40.0, qc_flag="removed")
    check("hsi_pct", close(F["hsi_pct"](con, "U"), 1.25), F["hsi_pct"](con, "U"), ["hsi_pct"])
    check("vsi_pct incl / excl liver", close(F["vsi_pct"](con, "U"), (10.0 + 9.0) / 2), F["vsi_pct"](con, "U"), ["vsi_pct"])
    check("gsi_pct", close(F["gsi_pct"](con, "U"), 0.5), keys=["gsi_pct"])
    check("fillet_yield_pct", close(F["fillet_yield_pct"](con, "U"), 60.0), keys=["fillet_yield_pct"])
    for rid, d, nf, af in (("L1", day(1), 10, 3), ("L2", day(1), 20, 1), ("L3", day(3), 10, 2), ("L4", day(9), 10, 9)):
        ins("lice_counts", record_id=rid, unit_id="U", date=d, n_fish_sampled=nf, adult_female=af)
    check("lice per date = ratio of sums", close(F["lice_af_per_fish"](con, "U", day(1)), 4 / 30), keys=["lice_af_per_fish"])
    check("lice period = mean of dates", close(F["lice_af_per_fish"](con, "U", None, day(0), day(5)), (4 / 30 + 0.2) / 2),
          keys=["lice_af_per_fish"])


def test_weight_basis():
    # NULL basis counts as round: weight and B_count returned
    con, ins = tiny(basis=None)
    check("NULL basis count -> B_count", M.count_at(con, "U", "d0") == (100, 10.0), M.count_at(con, "U", "d0"),
          ["count_rows"])
    check("NULL basis count -> weight", close(F["unit_weight_g"](con, "U", "d0"), 100.0), keys=["unit_weight_g"])
    # a gutted lot makes B_count NULL; weight falls back to the (round) sample rows, else NULL
    con, ins = tiny()
    ins("weighings", record_id="W0b", unit_id="U", occasion="d0", date=day(0), sampling_type="stocking", lot_no=2,
        n_fish=10, biomass_kg=0.9, weight_basis="gutted")
    check("gutted lot -> B_count NULL, n kept", M.count_at(con, "U", "d0") == (110, None), M.count_at(con, "U", "d0"),
          ["count_rows"])
    check("gutted lot, no sample -> weight NULL", F["unit_weight_g"](con, "U", "d0") is None, keys=["unit_weight_g"])
    ins("weighings", record_id="S1", unit_id="U", occasion="d0", date=day(0), sampling_type="sample", specimen_no=1,
        weight_g=104.0, weight_basis=None)
    ins("weighings", record_id="S2", unit_id="U", occasion="d0", date=day(0), sampling_type="sample", specimen_no=2,
        weight_g=50.0, weight_basis="bled")
    w = M.unit_weight(con, "U", "d0")
    check("gutted lot -> sample weight (NULL basis kept, bled dropped)", w["w_source"] == "sample" and w["w_g"] == 104.0,
          w, ["unit_weight_g"])
    check("gutted lot -> B(occasion) = n_at_risk x W", close(F["biomass_kg"](con, "U", "d0"), 110 * 104.0 / 1000),
          keys=["biomass_kg"])


def test_period_sum_coverage():
    for n_fed, expect in ((9, 1000.0), (8, None)):
        con, ins = tiny()
        final(ins, 10, 100, 12.0)
        for k in range(n_fed):        # days without a feeding row stay eligible
            ins("feeding", unit_id="U", date=day(k), batch_id="B", diet_id="D", feed_given_g=100.0)
        pm = M.period_metrics(con, "U", "d0", "final")
        got = pm["feed_given_g"]
        check("period_sum %d/10 days" % n_fed, (got is None and expect is None) or close(got, expect), (got, expect),
              ["period_sum"])
        check("period_sum coverage %d" % n_fed, pm["feed_given_coverage"]["coverage_pct"] == 10.0 * n_fed, keys=["period_sum"])
    con, ins = tiny()
    final(ins, 10, 100, 12.0)
    for k in range(9):
        ins("feeding", unit_id="U", date=day(k), batch_id="B", diet_id="D", feed_given_g=100.0)
    ins("feeding", unit_id="U", date=day(9), batch_id="B", diet_id="D", feed_given_g=0.0, feeding_stopped=1)
    pm = M.period_metrics(con, "U", "d0", "final")
    check("feeding_stopped not eligible", pm["feed_given_coverage"]["n_eligible"] == 9 and pm["feed_given_g"] == 900.0,
          pm["feed_given_coverage"], ["period_sum"])
    s, c = F["period_sum"]({1: (2.0, False), 2: (None, False), 3: (4.0, True)}, [1, 2, 3], 60)
    check("period_sum generic rescale", s == 9.0 and c["n_imputed"] == 1 and c["n_observed"] == 1, (s, c), ["period_sum"])
    s, c = F["period_sum"]({1: (2.0, False)}, [1, 2, 3], 60)
    check("period_sum generic NULL", s is None and round(c["coverage_pct"], 3) == 33.333, (s, c), ["period_sum"])


def test_relative_to_weighing():
    for rel_, n_mid, in_window in (("before", 97, True), (None, 100, False)):
        con, ins = tiny()
        ins("unit_occasions", unit_id="U", occasion="mid1", seq=2, occasion_date=day(5))
        ins("weighings", record_id="WM", unit_id="U", occasion="mid1", date=day(5), sampling_type="sample",
            specimen_no=1, weight_g=110.0, weight_basis="round")
        ev(ins, "E1", day(5), "mortality", 3, "death", 0.3, rel_=rel_)
        check("n_at_risk mid1 rel=%s" % rel_, M.n_at_risk(con, "U", day(5)) == n_mid, M.n_at_risk(con, "U", day(5)),
              ["n_at_risk"])
        pm = M.period_metrics(con, "U", "d0", "mid1")
        exp_gain = n_mid * 110.0 / 1000 - 10.0 + (0.3 if in_window else 0.0)
        check("gain d0-mid1 rel=%s" % rel_, close(pm["biomass_gain_kg"], exp_gain), (pm["biomass_gain_kg"], exp_gain),
              ["biomass_gain_kg"])
        u = M._Unit(con, "U")
        check("event slot rel=%s" % rel_, (len(u.events_between(M._d(day(4)), M._d(day(5)))) == 1) == in_window)


def test_recount_reset():
    con, ins = tiny()
    ev(ins, "E1", day(1), "mortality", 5, "death", 0.5)
    ins("unit_occasions", unit_id="U", occasion="mid1", seq=2, occasion_date=day(5))
    ins("weighings", record_id="WM", unit_id="U", occasion="mid1", date=day(5), sampling_type="bulk",
        n_fish=90, biomass_kg=9.9, weight_basis="round")
    ev(ins, "E2", day(6), "mortality", 2, "death", 0.2)
    ev(ins, "E3", day(8), "removal_sampling", 3, "sampling", 0.3)
    check("n_at_risk before recount", M.n_at_risk(con, "U", day(4)) == 95, M.n_at_risk(con, "U", day(4)), ["n_at_risk"])
    check("n_at_risk at recount = count", M.n_at_risk(con, "U", day(5)) == 90, keys=["n_at_risk"])
    check("n_at_risk after recount", M.n_at_risk(con, "U", day(9)) == 85, M.n_at_risk(con, "U", day(9)), ["n_at_risk"])
    check("expected_count at recount = 95 (count shows 90)", M.expected_count(con, "U", "mid1") == 95,
          M.expected_count(con, "U", "mid1"))


def test_transfer_in_and_estimates():
    con, ins = tiny()
    ins("units", unit_id="U2", trial_id="T", system_id="SY", diet_id="D", unit_type="tank")
    final(ins, 10, 110, 14.3)
    ins("events", record_id="E1", unit_id="U", date=day(3), event_type="transfer_in", n_fish=10, outcome_class="transfer",
        biomass_kg=1.1, weight_source="weighed", counterpart_unit_id="U2")
    pm = M.period_metrics(con, "U", "d0", "final")
    check("transfer_in subtracts from gain", close(pm["biomass_gain_kg"], 14.3 - 10.0 - 1.1), pm["biomass_gain_kg"],
          ["biomass_gain_kg"])
    check("n_at_risk with transfer_in", M.n_at_risk(con, "U", day(5)) == 110, keys=["n_at_risk"])
    ins("validation_params", trial_id="T", param="dead_weight_ratio", value=0.5)
    ev(ins, "E2", day(5), "mortality", 2, "death", None, ws="estimated_interpolated")
    ev(ins, "E3", day(6), "cull", 1, "cull_scheduled", None, ws="estimated_last_mean")
    w5 = M.w_interp(con, "U", day(5))
    check("w_interp geometric", close(w5, (100.0 * 130.0) ** 0.5), w5, ["w_interp"])
    exp = 2 * w5 / 1000 * 0.5
    check("event_biomass estimated_interpolated x dead_weight_ratio", close(F["event_biomass_kg"](con, "U", "E2"), exp),
          keys=["event_biomass_kg"])
    check("event_biomass estimated_last_mean (d0 weight, no ratio for cull_scheduled)",
          close(F["event_biomass_kg"](con, "U", "E3"), 0.1), F["event_biomass_kg"](con, "U", "E3"), ["event_biomass_kg"])
    pm = M.period_metrics(con, "U", "d0", "final")
    check("biomass_gain_estimated_share", close(pm["biomass_gain_estimated_share"], (exp + 0.1) / pm["biomass_gain_kg"]))
    check("survival not simple with transfer_in -> KM", M.survival(con, "U")[1].startswith("kaplan_meier"),
          M.survival(con, "U"))


def test_kaplan_meier():
    con, ins = tiny()
    con.execute("UPDATE units SET termination_reason = 'disease'")
    ins("unit_occasions", unit_id="U", occasion="end_early", seq=2, occasion_date=day(5))
    ev(ins, "E1", day(1), "mortality", 2, "death", 0.2)
    ev(ins, "E2", day(2), "removal_sampling", 10, "sampling", 1.0)
    ev(ins, "E3", day(3), "cull", 4, "cull_moribund", 0.4)
    exp = 100 * (1 - 2 / 100) * (1 - 4 / 88)
    got, method = M.survival(con, "U")
    check("Kaplan-Meier with censoring", close(got, exp) and method == "kaplan_meier_day_5", (got, method, exp),
          ["survival_pct"])
    check("survival_pct registry", close(F["survival_pct"](con, "U"), exp), keys=["survival_pct"])


def test_n_fish_d0_from_stocking():
    con, model = build(YAML)
    M.use_model(con, model)

    def ins(t, **r):
        con.execute("INSERT INTO %s (%s) VALUES (%s)" % (t, ",".join(r), ",".join("?" * len(r))), list(r.values()))
    ins("trials", trial_id="T", start_date="2025-01-08")
    ins("sites", site_id="S", timezone="Europe/Oslo")
    ins("systems", system_id="SY", trial_id="T", site_id="S", temp_control="ambient")
    ins("diets", diet_id="D", trial_id="T")
    ins("units", unit_id="U", trial_id="T", system_id="SY", diet_id="D")
    ins("unit_occasions", unit_id="U", occasion="stocking", seq=1, occasion_date="2025-01-01")
    ins("unit_occasions", unit_id="U", occasion="d0", seq=2, occasion_date="2025-01-08")
    for rid, lot, kind in (("W0", 1, "stocking"), ("W0b", 2, "stocking"), ("W0c", 1, "bulk")):
        ins("weighings", record_id=rid, unit_id="U", occasion="stocking", date="2025-01-01", sampling_type=kind,
            lot_no=lot, n_fish=50 if kind == "stocking" else 101, biomass_kg=5.0, weight_basis="round")
    ev(ins, "E1", "2025-01-03", "mortality", 4, "death", 0.4)
    ev(ins, "E2", "2025-01-08", "mortality", 1, "death", 0.1)
    ev(ins, "E3", "2025-01-08", "mortality", 2, "death", 0.2, rel_="before")
    cr = F["count_rows"](con, "U", "stocking")
    check("count_rows: bulk outranks stocking", [r["record_id"] for r in cr] == ["W0c"], cr, ["count_rows"])
    con.execute("DELETE FROM weighings WHERE record_id = 'W0c'")
    cr = F["count_rows"](con, "U", "stocking")
    check("count_rows: all lots", sorted(r["record_id"] for r in cr) == ["W0", "W0b"], keys=["count_rows"])
    check("count_at all lots", M.count_at(con, "U", "stocking") == (100, 10.0), keys=["count_rows"])
    check("n_fish_d0 from stocking + events", F["n_fish_d0"](con, "U") == 94, F["n_fish_d0"](con, "U"), ["n_fish_d0"])


def test_daylength():
    f = F["daylength_h"]
    r = f(69.6, 18.9, "2025-12-21")
    check("69.6N Dec21 polar night", r["polar_night"] and not r["polar_day"] and r["natural_h"] == 0.0, r, ["daylength_h"])
    check("69.6N Dec21 civil twilight > 0", 0 < r["civil_h"] < 8, r, ["daylength_h"])
    r = f(69.6, 18.9, "2025-06-21")
    check("69.6N Jun21 polar day", r["polar_day"] and not r["polar_night"] and r["natural_h"] == 24.0, r, ["daylength_h"])
    for dte, lo, hi in (("2025-06-21", 18.5, 19.2), ("2025-12-21", 5.8, 6.5), ("2025-03-20", 12.0, 12.5)):
        r = f(59.0, 10.7, dte)
        check("59N %s" % dte, not r["polar_day"] and not r["polar_night"] and lo <= r["natural_h"] <= hi, r, ["daylength_h"])
    check("pole never NaN", f(90.0, 0.0, "2025-06-21")["natural_h"] == 24.0, keys=["daylength_h"])
    check("NULL latitude", f(None, None, "2025-06-21")["natural_h"] is None, keys=["daylength_h"])


def test_nh3():
    f = F["nh3_n_mg_l"]
    # Emerson et al. 1975 Table 1: 15 C, pH 8.0 -> 2.73 % unionised (freshwater)
    got = f(1.0, 8.0, 15.0, 0.0)
    check("nh3 Emerson table 15 C pH 8.0 = 2.73 %", abs(100 * got - 2.73) < 0.1, 100 * got, ["nh3_n_mg_l"])
    # Emerson et al. 1975 Table 1: 25 C, pH 7.0 -> 0.565 %; 5 C, pH 9.0 -> 11.1 %
    check("nh3 Emerson 25 C pH 7.0 = 0.565 %", abs(100 * f(1.0, 7.0, 25.0, 0.0) - 0.565) < 0.02, 100 * f(1.0, 7.0, 25.0, 0.0),
          ["nh3_n_mg_l"])
    check("nh3 Emerson 5 C pH 9.0 = 11.1 %", abs(100 * f(1.0, 9.0, 5.0, 0.0) - 11.1) < 0.3, 100 * f(1.0, 9.0, 5.0, 0.0),
          ["nh3_n_mg_l"])
    # hand arithmetic, 34 ppt: I = 19.9273*34/(1000-1.005109*34) = 0.70154;
    # pKa = 0.09018 + 2729.92/288.15 + (0.1552 - 0.0003142*288.15)*0.70154 = 9.56420 + 0.04536 = 9.60956
    exp = 2.0 / (1 + 10 ** (9.60956 - 8.0))
    check("nh3 34 ppt hand value", abs(f(2.0, 8.0, 15.0, 34.0) - exp) < 1e-4, (f(2.0, 8.0, 15.0, 34.0), exp), ["nh3_n_mg_l"])
    check("nh3 NULL salinity -> NULL", f(1.0, 8.0, 15.0, None) is None, keys=["nh3_n_mg_l"])


if __name__ == "__main__":
    for fn in (test_registry, test_synthetic, test_rich_hand_computed, test_dose_realised, test_fish_level_indices,
               test_weight_basis, test_period_sum_coverage, test_relative_to_weighing, test_recount_reset,
               test_transfer_in_and_estimates, test_kaplan_meier, test_n_fish_d0_from_stocking, test_daylength, test_nh3,
               test_bulk_lots_with_mean_weight, test_adc_wet_basis_feces):
        try:
            fn()
        except Exception as e:     # a crash is a failure, keep running the others
            import traceback
            traceback.print_exc()
            FAIL.append("%s crashed: %r" % (fn.__name__, e))
    keys = set(load_model(YAML)["derived_metrics"])
    unexercised = sorted(keys - EXERCISED - set(WAIVED))
    check("every derived_metrics key exercised or waived", not unexercised, unexercised)
    with open(COVERAGE_FILE, "w") as fh:
        json.dump({"exercised": sorted(EXERCISED & keys), "waived": WAIVED}, fh, indent=1)
        fh.write("\n")
    print("\n%d checks, %d failed; metrics exercised %d/%d, waived %d -> %s" % (
        N[0], len(FAIL), len(EXERCISED & keys), len(keys), len(WAIVED), COVERAGE_FILE))
    sys.exit(1 if FAIL else 0)
