#!/usr/bin/env python3
"""Tests of tools/metrics.py: synthetic trial vs known truth, plus edge cases on tiny databases.

Usage: test_metrics.py [data_model.yaml]      exit 1 on any failure
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import metrics as M  # noqa: E402
from build_db import build  # noqa: E402
from synthetic_trial import make_trial  # noqa: E402

YAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")
FAIL = []
N = [0]


def check(name, ok, detail=""):
    N[0] += 1
    if not ok:
        FAIL.append("%s %s" % (name, detail))
        print("FAIL", name, detail)


def rel(a, b):
    return abs(a - b) / abs(b)


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
        check(uid + " n_fish_d0", M.n_fish_d0(con, uid) == t["n_d0"], (M.n_fish_d0(con, uid), t["n_d0"]))
        fin = [o for o in M.occasions(con, uid) if o["occasion"] == "final"][0]
        check(uid + " n_at_risk(final)", M.n_at_risk(con, uid, fin["date"]) == t["n_final"],
              (M.n_at_risk(con, uid, fin["date"]), t["n_final"]))
        check(uid + " b_start", rel(r["b_start_kg"], t["b_start_kg"]) <= 0.001, (r["b_start_kg"], t["b_start_kg"]))
        check(uid + " b_end", rel(r["b_end_kg"], t["b_end_kg"]) <= 0.001, (r["b_end_kg"], t["b_end_kg"]))
        check(uid + " biomass_gain", rel(r["biomass_gain_kg"], t["gain_kg"]) <= 0.005, (r["biomass_gain_kg"], t["gain_kg"]))
        check(uid + " fcr_biological", rel(r["fcr_biological"], t["fcr_bio"]) <= 0.02, (r["fcr_biological"], t["fcr_bio"]))
        check(uid + " protein_intake", rel(r["protein_intake_g"], t["prot_eaten_g"]) <= 0.02,
              (r["protein_intake_g"], t["prot_eaten_g"]))
        check(uid + " adc_protein", abs(r["adc_pct"]["protein"] - t["adc_protein_pct"]) <= 0.1,
              (r["adc_pct"]["protein"], t["adc_protein_pct"]))
        check(uid + " survival", r["survival_pct"] == t["survival_pct"], (r["survival_pct"], t["survival_pct"]))
        check(uid + " sgr", abs(r["sgr_pct_day"] - t["sgr_true_pct"]) <= 0.05, (r["sgr_pct_day"], t["sgr_true_pct"]))
        c = r["degree_days_coverage"]
        check(uid + " dd coverage reported", r["degree_days"] is not None and c["n_eligible"] == 84
              and c["coverage_pct"] is not None, c)
        print("%-5s %-28s %-28s %-24s %-22s %-22s %-14s %-16s %-14s" % (
            uid, "%.3f / %.3f" % (r["biomass_gain_kg"], t["gain_kg"]),
            "%.4f / %.4f (%+.2f%%)" % (r["fcr_biological"], t["fcr_bio"], 100 * (r["fcr_biological"] / t["fcr_bio"] - 1)),
            "%.0f / %.0f" % (r["protein_intake_g"], t["prot_eaten_g"]),
            "%.0f / %.0f" % (r["intake_dm_g"], t["feed_dm_eaten_g"]),
            "%.4f / %.4f" % (r["sgr_pct_day"], t["sgr_true_pct"]),
            "%.3f / %.1f" % (r["adc_pct"]["protein"], t["adc_protein_pct"]),
            "%s / %s" % (r["survival_pct"], t["survival_pct"]), "%d+%d" % (c["n_observed"], c["n_imputed"])))
    c = rows["U3D1"]["degree_days_coverage"]
    check("U3D1 sensor outage filled by system reference", c["n_imputed"] >= 14 and c["coverage_pct"] == 100.0, c)
    check("U2D2 pending exclusion flagged, still in ITT", rows["U2D2"]["exclusion_pending"] and rows["U2D2"]["itt"])
    # mid1 has no count -> sample source; the qc_flag removed row must not be counted
    w = M.unit_weight(con, "U1D1", "mid1")
    check("U1D1 mid1 sample n excludes qc removed", w["w_source"] == "sample" and w["n_weight"] == 19, w)
    dr = M.unit_day_risk(con, "U1D1")
    check("unit_day_risk length", len(dr) == 84, len(dr))
    check("unit_day_risk deaths", sum(r["n_event"] for r in dr) == truth["U1D1"]["deaths"])
    check("unit_day_risk censored (5 sampled)", sum(r["n_censored"] for r in dr) == 5)
    p = M.params(con, "T1")
    check("params defaults", p["min_coverage_pct"] == 90 and p["dead_weight_ratio"] == 1)


# ------------------------------------------------------------------ tiny hand-built databases
def tiny(n_days=10, stock_n=100, w=100.0):
    """One tank, d0 on 2025-01-01 (count), final on day n_days (harvest count set by caller)."""
    con, model = build(YAML)
    M.use_model(con, model)
    ins = lambda t, **r: con.execute("INSERT INTO %s (%s) VALUES (%s)" % (t, ",".join(r), ",".join("?" * len(r))),  # noqa: E731
                                     list(r.values()))
    ins("trials", trial_id="T", start_date="2025-01-01")
    ins("sites", site_id="S", timezone="Europe/Oslo", latitude=60.0, longitude=10.0)
    ins("systems", system_id="SY", trial_id="T", site_id="S", temp_control="controlled", target_temp_c=10.0)
    ins("diets", diet_id="D", trial_id="T")
    ins("feed_batches", batch_id="B", diet_id="D", pellet_mm=3.0, dm_frac=0.9)
    ins("units", unit_id="U", trial_id="T", system_id="SY", diet_id="D", unit_type="tank")
    import datetime as dt
    day = lambda k: (dt.date(2025, 1, 1) + dt.timedelta(days=k)).isoformat()  # noqa: E731
    ins("unit_occasions", unit_id="U", occasion="d0", seq=1, occasion_date=day(0))
    ins("weighings", record_id="W0", unit_id="U", occasion="d0", date=day(0), sampling_type="stocking",
        n_fish=stock_n, biomass_kg=stock_n * w / 1000, weight_basis="round")
    return con, ins, day


def ev(ins, rid, date, etype, n, oc, kg=None, ws=None, rel=None):
    ins("events", record_id=rid, unit_id="U", date=date, event_type=etype, n_fish=n, outcome_class=oc,
        biomass_kg=kg, weight_source=ws or "weighed", relative_to_weighing=rel)


def test_period_sum_coverage():
    for n_fed, expect in ((9, 1000.0), (8, None)):
        con, ins, day = tiny()
        ins("unit_occasions", unit_id="U", occasion="final", seq=2, occasion_date=day(10))
        ins("weighings", record_id="WF", unit_id="U", occasion="final", date=day(10), sampling_type="harvest",
            n_fish=100, biomass_kg=12.0, weight_basis="round")
        for k in range(n_fed):        # days without a feeding row stay eligible
            ins("feeding", unit_id="U", date=day(k), batch_id="B", diet_id="D", feed_given_g=100.0)
        pm = M.period_metrics(con, "U", "d0", "final")
        got = pm["feed_given_g"]
        check("period_sum %d/10 days" % n_fed, (got is None and expect is None) or
              (got is not None and expect is not None and abs(got - expect) < 1e-9), (got, expect))
        check("period_sum coverage %d" % n_fed, pm["feed_given_coverage"]["coverage_pct"] == 10.0 * n_fed)
    # feeding_stopped days leave the eligible set
    con, ins, day = tiny()
    ins("unit_occasions", unit_id="U", occasion="final", seq=2, occasion_date=day(10))
    ins("weighings", record_id="WF", unit_id="U", occasion="final", date=day(10), sampling_type="harvest",
        n_fish=100, biomass_kg=12.0, weight_basis="round")
    for k in range(9):
        ins("feeding", unit_id="U", date=day(k), batch_id="B", diet_id="D", feed_given_g=100.0)
    ins("feeding", unit_id="U", date=day(9), batch_id="B", diet_id="D", feed_given_g=0.0, feeding_stopped=1)
    pm = M.period_metrics(con, "U", "d0", "final")
    check("feeding_stopped not eligible", pm["feed_given_coverage"]["n_eligible"] == 9 and pm["feed_given_g"] == 900.0,
          pm["feed_given_coverage"])
    # generic function
    s, c = M.period_sum({1: (2.0, False), 2: (None, False), 3: (4.0, True)}, [1, 2, 3], 60)
    check("period_sum generic rescale", s == 9.0 and c["n_imputed"] == 1 and c["n_observed"] == 1, (s, c))
    s, c = M.period_sum({1: (2.0, False)}, [1, 2, 3], 60)
    check("period_sum generic NULL", s is None and round(c["coverage_pct"], 3) == 33.333, (s, c))


def test_relative_to_weighing():
    for rel_, n_mid, in_window in (("before", 97, True), (None, 100, False)):
        con, ins, day = tiny()
        ins("unit_occasions", unit_id="U", occasion="mid1", seq=2, occasion_date=day(5))
        ins("weighings", record_id="WM", unit_id="U", occasion="mid1", date=day(5), sampling_type="sample",
            specimen_no=1, weight_g=110.0, weight_basis="round")
        ev(ins, "E1", day(5), "mortality", 3, "death", 0.3, rel=rel_)
        check("n_at_risk mid1 rel=%s" % rel_, M.n_at_risk(con, "U", day(5)) == n_mid, M.n_at_risk(con, "U", day(5)))
        pm = M.period_metrics(con, "U", "d0", "mid1")
        exp_b_end = n_mid * 110.0 / 1000
        exp_gain = exp_b_end - 10.0 + (0.3 if in_window else 0.0)
        check("gain d0-mid1 rel=%s" % rel_, abs(pm["biomass_gain_kg"] - exp_gain) < 1e-9, (pm["biomass_gain_kg"], exp_gain))
        # a 'before' event belongs to the interval before the weighing
        u = M._Unit(con, "U")
        on4 = u.events_between(M._d(day(4)), M._d(day(5)))
        check("event slot rel=%s" % rel_, (len(on4) == 1) == in_window)


def test_recount_reset():
    con, ins, day = tiny()
    ev(ins, "E1", day(1), "mortality", 5, "death", 0.5)
    ins("unit_occasions", unit_id="U", occasion="mid1", seq=2, occasion_date=day(5))
    ins("weighings", record_id="WM", unit_id="U", occasion="mid1", date=day(5), sampling_type="bulk",
        n_fish=90, biomass_kg=9.9, weight_basis="round")
    ev(ins, "E2", day(6), "mortality", 2, "death", 0.2)
    ev(ins, "E3", day(8), "removal_sampling", 3, "sampling", 0.3)
    check("n_at_risk before recount", M.n_at_risk(con, "U", day(4)) == 95, M.n_at_risk(con, "U", day(4)))
    check("n_at_risk at recount = count", M.n_at_risk(con, "U", day(5)) == 90)
    check("n_at_risk after recount", M.n_at_risk(con, "U", day(9)) == 85, M.n_at_risk(con, "U", day(9)))


def test_transfer_in_and_estimates():
    con, ins, day = tiny()
    ins("units", unit_id="U2", trial_id="T", system_id="SY", diet_id="D", unit_type="tank")
    ins("unit_occasions", unit_id="U", occasion="final", seq=2, occasion_date=day(10))
    ins("weighings", record_id="WF", unit_id="U", occasion="final", date=day(10), sampling_type="harvest",
        n_fish=110, biomass_kg=14.3, weight_basis="round")
    ins("events", record_id="E1", unit_id="U", date=day(3), event_type="transfer_in", n_fish=10, outcome_class="transfer",
        biomass_kg=1.1, weight_source="weighed", counterpart_unit_id="U2")
    pm = M.period_metrics(con, "U", "d0", "final")
    check("transfer_in subtracts from gain", abs(pm["biomass_gain_kg"] - (14.3 - 10.0 - 1.1)) < 1e-9, pm["biomass_gain_kg"])
    check("n_at_risk with transfer_in", M.n_at_risk(con, "U", day(5)) == 110)
    # estimated_interpolated with dead_weight_ratio; NULL when no estimate possible
    ins("validation_params", trial_id="T", param="dead_weight_ratio", value=0.5)
    ev(ins, "E2", day(5), "mortality", 2, "death", None, ws="estimated_interpolated")
    w5 = M.w_interp(con, "U", day(5))
    exp = 2 * w5 / 1000 * 0.5
    check("event_biomass estimated_interpolated x dead_weight_ratio", abs(M.event_biomass_kg(con, "U", "E2") - exp) < 1e-12)
    pm = M.period_metrics(con, "U", "d0", "final")
    check("biomass_gain_estimated_share", abs(pm["biomass_gain_estimated_share"] - exp / pm["biomass_gain_kg"]) < 1e-12)
    check("w_interp geometric", abs(w5 - (100.0 * 130.0) ** 0.5) < 1e-9, w5)


def test_n_fish_d0_from_stocking():
    con, model = build(YAML)
    M.use_model(con, model)
    ins = lambda t, **r: con.execute("INSERT INTO %s (%s) VALUES (%s)" % (t, ",".join(r), ",".join("?" * len(r))),  # noqa: E731
                                     list(r.values()))
    ins("trials", trial_id="T", start_date="2025-01-08")
    ins("sites", site_id="S", timezone="Europe/Oslo")
    ins("systems", system_id="SY", trial_id="T", site_id="S", temp_control="ambient")
    ins("diets", diet_id="D", trial_id="T")
    ins("units", unit_id="U", trial_id="T", system_id="SY", diet_id="D")
    ins("unit_occasions", unit_id="U", occasion="stocking", seq=1, occasion_date="2025-01-01")
    ins("unit_occasions", unit_id="U", occasion="d0", seq=2, occasion_date="2025-01-08")
    ins("weighings", record_id="W0", unit_id="U", occasion="stocking", date="2025-01-01", sampling_type="stocking",
        n_fish=50, biomass_kg=5.0, weight_basis="round")
    ins("weighings", record_id="W0b", unit_id="U", occasion="stocking", date="2025-01-01", sampling_type="stocking",
        lot_no=2, n_fish=50, biomass_kg=5.0, weight_basis="round")
    ev(ins, "E1", "2025-01-03", "mortality", 4, "death", 0.4)
    ev(ins, "E2", "2025-01-08", "mortality", 1, "death", 0.1)                    # after d0 weighing
    ev(ins, "E3", "2025-01-08", "mortality", 2, "death", 0.2, rel="before")      # before d0 weighing
    check("count_at all lots", M.count_at(con, "U", "stocking") == (100, 10.0))
    check("n_fish_d0 from stocking + events", M.n_fish_d0(con, "U") == 94, M.n_fish_d0(con, "U"))


def test_daylength():
    r = M.daylength_h(69.6, 18.9, "2025-12-21")
    check("69.6N Dec21 polar night", r["polar_night"] and not r["polar_day"] and r["natural_h"] == 0.0, r)
    check("69.6N Dec21 civil twilight > 0", 0 < r["civil_h"] < 8, r)
    r = M.daylength_h(69.6, 18.9, "2025-06-21")
    check("69.6N Jun21 polar day", r["polar_day"] and not r["polar_night"] and r["natural_h"] == 24.0, r)
    for dte, lo, hi in (("2025-06-21", 18.5, 19.2), ("2025-12-21", 5.8, 6.5), ("2025-03-20", 12.0, 12.5)):
        r = M.daylength_h(59.0, 10.7, dte)
        check("59N %s" % dte, not r["polar_day"] and not r["polar_night"] and lo <= r["natural_h"] <= hi, r)
    r = M.daylength_h(90.0, 0.0, "2025-06-21")
    check("pole never NaN", r["natural_h"] == 24.0, r)
    check("NULL latitude", M.daylength_h(None, None, "2025-06-21")["natural_h"] is None)


if __name__ == "__main__":
    for f in (test_synthetic, test_period_sum_coverage, test_relative_to_weighing, test_recount_reset,
              test_transfer_in_and_estimates, test_n_fish_d0_from_stocking, test_daylength):
        try:
            f()
        except Exception as e:     # a crash is a failure, keep running the others
            import traceback
            traceback.print_exc()
            FAIL.append("%s crashed: %r" % (f.__name__, e))
    print("\n%d checks, %d failed" % (N[0], len(FAIL)))
    sys.exit(1 if FAIL else 0)
