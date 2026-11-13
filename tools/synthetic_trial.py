#!/usr/bin/env python3
"""Synthetic RAS split-plot feed trial with KNOWN true values, loaded through build_db.

Design: temperature 12 / 16 C assigned to RAS systems (2 systems per level = split-plot),
2 diets (D1 reference, D2) x 1 tank per system = 8 tanks, 100 fish per tank, 84 days.
Systems SY1/SY3 stock on d0; SY2/SY4 stock 7 days earlier (acclimation) with a d0 count.
Daily feeding with a batch change (3 mm -> 4 mm) at mid1, uneaten-feed collection with a
few missed days, unit temperature sensors with gaps (system reference as fallback),
weighed daily mortality, a terminal sampling of 5 fish at mid1, harvest at final, diet /
feces / whole-body composition with a Y2O3 marker, one pending and one decided deviation.
The trial also carries its pre-registered analysis plan (PLAN_OUTCOMES: one primary and four
secondary outcomes plus an exploratory one, each with model, error strata, scale and
multiplicity handling), covariate roles, the two randomisation runs, blind diet codes and a
locked primary analysis run, so rules R28, R30, R35 and R38 are checked on it.

The generator returns the connection and a `truth` dict per unit (true counts, biomass,
feed eaten on a dry-matter basis, gain incl. removed fish, protein deposited, ADC), so that
tools/metrics.py can be checked against values that do not depend on the metric code.

Usage: synthetic_trial.py [data_model.yaml] [out.db]
"""
import datetime as dt
import hashlib
import json
import math
import os
import random
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_db import build  # noqa: E402

TZ = ZoneInfo("Europe/Oslo")
D0 = dt.date(2025, 3, 3)
MID, FINAL = 42, 84
SYSTEMS = {"SY1": 12.0, "SY2": 12.0, "SY3": 16.0, "SY4": 16.0}
ACCLIM = {"SY2", "SY4"}            # stocked 7 days before d0
DIETS = {"D1": {"sgr": {12.0: 1.30, 16.0: 1.45}, "dm": (0.92, 0.925), "prot_asfed": 460.0, "y_asfed": 80.0, "adc_prot": 0.88},
         "D2": {"sgr": {12.0: 1.38, 16.0: 1.62}, "dm": (0.93, 0.935), "prot_asfed": 470.0, "y_asfed": 80.0, "adc_prot": 0.90}}
TRUE_FCR_DM = {"D1": 0.80, "D2": 0.76}   # true DM feed eaten per kg wet gain
WASTE_FRAC = 0.06                       # true uneaten share of DM given
RECOVERY = {3.0: 0.86, 4.0: 0.82}       # true recovery of uneaten DM by pellet size
UNEATEN_DM = 0.35
PROT_BODY = (165.0, 175.0)              # g/kg wet, initial and final whole body
SGR_NOISE = 0.03                        # a tank grows at its cell SGR x uniform(1 - SGR_NOISE, 1 + SGR_NOISE)
FISH_CV = 0.04                          # SD of an individual weight relative to the tank mean
DAILY_DEATH_PROB = 0.04                 # chance of one death per tank and day, the same for every diet and temperature
BLIND_CODE = {"D1": "F17", "D2": "F42"}   # units.diet_code_blind: what feeders and assessors see

# Pre-registered analysis plan (analysis_plans P1, frozen before d0). tools/analysis.py reads these
# rows and runs exactly the models named here; tools/test_analysis.py checks the power inputs.
# The tank is the unit; temperature is applied to systems, so every model has system as an error
# stratum (R35). expected_sd = SD between tanks of the same diet in the same system; with 2 systems
# per temperature the diet contrast has 2 error df and mdd is the difference found with 80 % power.
_STRATA = "system, system:unit"
_COMMON = {"occasion": "final", "estimand": "total", "error_strata": _STRATA, "unit_of_analysis": "unit",
           "missing_data_handling": "complete_case", "two_sided": 1, "alpha": 0.05,
           "contrast": "D2 vs D1 (reference), averaged over temperature",
           "planned_n_units_per_level": 4, "planned_n_fish_per_unit": 100}
PLAN_OUTCOMES = [
    {"outcome_label": "growth_sgr", "outcome": "sgr_pct_day", "role": "primary", "family": "primary",
     "hierarchy_rank": 1, "multiplicity_method": "none", "scale": "raw",
     "model": "aov(sgr_pct_day ~ diet*temperature + Error(system))",
     "expected_sd": 0.025, "sd_level": "unit", "mdd": 0.10, "power": 0.80, "n_occasions": 3,
     "dropout_assumed_pct": 0.0},
    {"outcome_label": "intake_log", "outcome": "intake_dm_g", "role": "secondary", "family": "secondary",
     "multiplicity_method": "holm", "scale": "log", "log_definition": "log_of_unit_mean",
     "model": "aov(log(intake_dm_g) ~ diet*temperature + Error(system))"},
    {"outcome_label": "gain_log", "outcome": "biomass_gain_kg", "role": "secondary", "family": "secondary",
     "multiplicity_method": "holm", "scale": "log", "log_definition": "log_of_unit_mean",
     "model": "aov(log(biomass_gain_kg) ~ diet*temperature + Error(system))"},
    {"outcome_label": "feed_per_gain_log", "outcome": "fcr_biological", "role": "secondary", "family": "secondary",
     "multiplicity_method": "holm", "scale": "log", "log_definition": "log_of_unit_mean",
     "model": "aov(log(intake_dm_g) - log(biomass_gain_kg) ~ diet*temperature + Error(system))"},
    {"outcome_label": "survival_logit", "outcome": "survival_pct", "role": "secondary", "family": "secondary",
     "multiplicity_method": "holm", "scale": "logit",
     "model": "aov(empirical_logit(survival_pct) ~ diet*temperature + Error(system))"},
    {"outcome_label": "adc_protein", "outcome": "adc_pct", "analyte": "protein", "role": "exploratory",
     "family": "exploratory", "multiplicity_method": "none", "scale": "raw", "adc_method_handling": "single_method",
     "model": "aov(adc_pct ~ diet*temperature + Error(system))"},
]
PLAN_TEXT = json.dumps([dict(_COMMON, **o) for o in PLAN_OUTCOMES], sort_keys=True)


def day(d):
    return (D0 + dt.timedelta(days=d)).isoformat()


def local_noon_utc(d):
    t = dt.datetime.combine(D0 + dt.timedelta(days=d), dt.time(12), tzinfo=TZ)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_trial(yaml_path="data_model.yaml", db=":memory:", seed=7):
    rng = random.Random(seed)
    con, model = build(yaml_path, db)

    def ins(table, **row):
        con.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(row), ", ".join("?" * len(row))),
                    list(row.values()))

    ins("trials", trial_id="T1", title="Synthetic RAS split-plot", life_stage="post_smolt", system_type="ras",
        start_date=day(0), end_date=day(FINAL), blinding_feeders=1, blinding_assessors=1, blinding_analysts=1,
        unblinded_at="2025-06-01T08:00:00Z", ethics_permit="FOTS-0000")
    ins("sites", site_id="S1", name="Research station", latitude=63.44, longitude=10.4, region="mid", timezone="Europe/Oslo")
    ins("cohorts", cohort_id="C1", strain="AquaGen", ploidy="diploid", smolt_type="zero_plus")
    ins("sensors", sensor_id="SCALE1", variable="weight")
    ins("design_factors", factor_id="F_TEMP", trial_id="T1", name="temperature", role="assigned_treatment",
        applied_at="system", level_type="numeric")
    # the two tanks of a system (one per diet) form a block: units.block = system_id (R20)
    ins("design_factors", factor_id="F_BLOCK", trial_id="T1", name="block", role="blocking",
        applied_at="system", level_type="categorical")
    for sid, temp in SYSTEMS.items():
        ins("sensors", sensor_id="REF_" + sid, variable="temp_c")
        ins("systems", system_id=sid, trial_id="T1", site_id="S1", system_kind="ras", temp_control="controlled",
            target_temp_c=temp, water_source="brackish", reference_temp_sensor_id="REF_" + sid,
            start_date=day(0), acclimation_start=day(-7) if sid in ACCLIM else day(0))
        ins("factor_levels", record_id="FL_" + sid, factor_id="F_TEMP", system_id=sid, level="%dC" % temp,
            numeric_value=temp, level_order=1 if temp == 12 else 2, is_reference=1 if temp == 12 else 0)
        ins("factor_levels", record_id="FB_" + sid, factor_id="F_BLOCK", system_id=sid, level=sid,
            level_order=int(sid[2:]))
        ins("light_regimes", regime_id="LR_" + sid, system_id=sid, start_date=day(-7), light_source="led_white",
            schedule="24L", light_hours_per_day=24)
    batches = {}
    for did, d in DIETS.items():
        ins("diets", diet_id=did, trial_id="T1", label=did, control_role="reference" if did == "D1" else "none",
            marker="Y2O3", marker_analyte="Y")
        for k, (pellet, dm) in enumerate(((3.0, d["dm"][0]), (4.0, d["dm"][1]))):
            bid = "%s_B%d" % (did, k + 1)
            batches[(did, k)] = (bid, pellet, dm)
            ins("feed_batches", batch_id=bid, diet_id=did, pellet_mm=pellet, dm_frac=dm, marker_y2o3_pct_nominal=0.01)
            for an, val, unit in (("protein", d["prot_asfed"] * (1 + 0.01 * k), "g_kg"), ("Y", d["y_asfed"], "mg_kg"),
                                  ("dm", dm * 1000, "g_kg")):
                ins("composition", record_id="CD_%s_%s" % (bid, an), trial_id="T1", sample_id="DIET_" + bid,
                    diet_id=did, batch_id=bid, sample_type="diet", analyte=an, value=round(val, 3),
                    value_unit=unit, basis="as_fed", lab_id="LAB1", analysis_run_id="RUN_DIET")
    for pellet, rec in RECOVERY.items():
        for temp in (12.0, 16.0):
            ins("recovery_tests", record_id="RT_%g_%g" % (pellet, temp), trial_id="T1", unit_type="tank",
                pellet_mm=pellet, temp_c=temp, n_reps=3, feed_in_dm_g=100.0, feed_recovered_dm_g=100.0 * rec)
    ins("composition", record_id="CW_INIT", trial_id="T1", sample_id="WB_INIT", cohort_id="C1",
        sample_type="whole_body_initial", n_fish_pooled=10, analyte="protein", value=PROT_BODY[0],
        value_unit="g_kg", basis="wet", lab_id="LAB1", analysis_run_id="RUN_WB")

    truth = {}
    units = []
    for i, sid in enumerate(SYSTEMS):
        for did in DIETS:
            units.append(("U%d%s" % (i + 1, did), sid, did))
    for uid, sid, did in units:
        temp = SYSTEMS[sid]
        sgr = DIETS[did]["sgr"][temp] / 100.0 * rng.uniform(1 - SGR_NOISE, 1 + SGR_NOISE)
        ins("units", unit_id=uid, trial_id="T1", system_id=sid, diet_id=did, cohort_id="C1", unit_type="tank",
            volume_m3=1.0, block=sid, block_scope="within_system", diet_code_blind=BLIND_CODE[did])
        stock_day = -7 if sid in ACCLIM else 0
        occ = ([("stocking", 0, stock_day)] if stock_day < 0 else []) + [("d0", 1, 0), ("mid1", 2, MID), ("final", 3, FINAL)]
        for name, seq, d in occ:
            ins("unit_occasions", unit_id=uid, occasion=name, seq=seq, occasion_date=day(d), planned=1)

        # --- daily population simulation (weights grow at sgr from the stocking day)
        w0 = rng.uniform(95, 105)
        n = 100
        mean_w = lambda d: w0 * math.exp(sgr * (d - stock_day))  # noqa: E731
        t = {"n_stocked": n, "dead_after_d0": 0, "dead_biomass_kg": 0.0, "removed_biomass_kg": 0.0,
             "feed_dm_eaten_g": 0.0, "prot_eaten_g": 0.0}
        rid = 0

        def ev(d, etype, nf, oc, biomass=None, ws=None, rel=None):
            nonlocal rid
            rid += 1
            ins("events", record_id="%s_EV%03d" % (uid, rid), unit_id=uid, date=day(d), event_type=etype,
                n_fish=nf, n_fish_estimated=0 if nf else None, outcome_class=oc if nf else None, biomass_kg=biomass,
                weight_source=ws if ws else ("not_applicable" if nf == 0 else "weighed"),
                relative_to_weighing=rel, cause_category="unknown" if etype == "mortality" and nf else None)

        ins("weighings", record_id=uid + "_STOCK", unit_id=uid, occasion=occ[0][0], date=day(stock_day),
            sampling_type="stocking", n_fish=n, biomass_kg=round(n * w0 / 1000, 4), weight_basis="round",
            instrument_id="SCALE1")
        for d in range(stock_day, FINAL):
            if d == 0 and stock_day < 0:
                ins("weighings", record_id=uid + "_D0", unit_id=uid, occasion="d0", date=day(0), sampling_type="bulk",
                    n_fish=n, biomass_kg=round(n * mean_w(0) / 1000, 4), weight_basis="round", instrument_id="SCALE1")
            if d in (0, MID):
                occn = "d0" if d == 0 else "mid1"
                for k in range(20):
                    ins("weighings", record_id="%s_%s_S%02d" % (uid, occn, k + 1), unit_id=uid, occasion=occn,
                        date=day(d), sampling_type="sample", specimen_no=k + 1,
                        weight_g=round(mean_w(d) * rng.gauss(1, FISH_CV), 1), weight_basis="round",
                        fish_fate="killed" if (d == MID and k < 5) else "returned", instrument_id="SCALE1")
            if d == MID:   # terminal sampling of 5 fish after the weighing
                b = round(5 * mean_w(d) / 1000, 4)
                ev(d, "removal_sampling", 5, "sampling", b)
                n -= 5
                t["removed_biomass_kg"] += b
                for k in range(5):
                    w = mean_w(d)
                    ins("fish_measurements", record_id="%s_FM%d" % (uid, k + 1), unit_id=uid, specimen_no=k + 1,
                        date=day(d), kill_method="percussion", body_weight_g=round(w, 1),
                        liver_g=round(w * 0.012, 2), viscera_g=round(w * 0.09, 2), viscera_includes_liver=1)
            # mortality (weighed; daily row, zero allowed)
            dead = 1 if rng.random() < DAILY_DEATH_PROB else 0
            if d >= 0:
                if dead:
                    b = round(dead * mean_w(d) * 0.8 / 1000, 4)
                    ev(d, "mortality", dead, "death", b)
                    t["dead_after_d0"] += dead
                    t["dead_biomass_kg"] += b
                else:
                    ev(d, "mortality", 0, None)
            n -= dead if d >= 0 else 0
            if d < 0:
                continue
            # feeding: fasting the day before each weighing
            k = 0 if d < MID else 1
            bid, pellet, dm = batches[(did, k)]
            fasting = d in (MID - 1, FINAL - 1)
            if fasting:
                ins("feeding", unit_id=uid, date=day(d), batch_id=bid, diet_id=did, feed_given_g=0.0,
                    feeding_stopped=1, ration_type="ad_libitum", delivery_basis="metered")
                continue
            gain_g = n * mean_w(d) * sgr
            eaten_dm = gain_g * TRUE_FCR_DM[did]
            given_dm = eaten_dm / (1 - WASTE_FRAC)
            given = given_dm / dm
            uneaten_dm_true = given_dm - eaten_dm
            measured_dm = uneaten_dm_true * RECOVERY[pellet]
            t["feed_dm_eaten_g"] += eaten_dm
            t["prot_eaten_g"] += eaten_dm * (DIETS[did]["prot_asfed"] * (1 + 0.01 * k) / dm) / 1000
            ins("feeding", unit_id=uid, date=day(d), batch_id=bid, diet_id=did, feed_given_g=round(given, 2),
                ration_type="ad_libitum", n_meals=3, feeder_type="automatic", delivery_basis="metered")
            if rng.random() > 0.03:      # ~3 % of collections missed
                ins("uneaten_feed", unit_id=uid, date=day(d), uneaten_wet_g=round(measured_dm / UNEATEN_DM, 3),
                    uneaten_dm_frac=UNEATEN_DM if d % 7 == 0 else None, collection_h_after_meal=1.0)
        # final: harvest count (before harvest event), sample rows, whole-body
        b_end = round(n * mean_w(FINAL) / 1000, 4)
        ins("weighings", record_id=uid + "_HARV", unit_id=uid, occasion="final", date=day(FINAL),
            sampling_type="harvest", n_fish=n, biomass_kg=b_end, weight_basis="round", instrument_id="SCALE1")
        for k in range(20):
            ins("weighings", record_id="%s_final_S%02d" % (uid, k + 1), unit_id=uid, occasion="final", date=day(FINAL),
                sampling_type="sample", specimen_no=k + 1, weight_g=round(mean_w(FINAL) * rng.gauss(1, FISH_CV), 1),
                weight_basis="round", fish_fate="returned", instrument_id="SCALE1")
        ev(FINAL, "harvest", n, "harvest", b_end)
        ins("composition", record_id="CW_" + uid, trial_id="T1", sample_id="WB_" + uid, unit_id=uid,
            sample_type="whole_body_final", date=day(FINAL), n_fish_pooled=5, analyte="protein",
            value=PROT_BODY[1], value_unit="g_kg", basis="wet", lab_id="LAB1", analysis_run_id="RUN_WB")
        # feces (stripping) in the last week: Y and protein on DM
        adc = DIETS[did]["adc_prot"]
        y_dm = DIETS[did]["y_asfed"] / DIETS[did]["dm"][1]
        p_dm = DIETS[did]["prot_asfed"] * 1.01 / DIETS[did]["dm"][1]
        y_fec = y_dm / 0.25                       # 75 % of DM digested
        p_fec = p_dm * (1 - adc) * y_fec / y_dm
        for an, val, unit in (("Y", y_fec, "mg_kg"), ("protein", p_fec, "g_kg")):
            ins("composition", record_id="CF_%s_%s" % (uid, an), trial_id="T1", sample_id="FEC_" + uid, unit_id=uid,
                sample_type="feces", feces_method="stripping", collection_start=day(FINAL - 5),
                collection_end=day(FINAL - 2), analyte=an, value=round(val, 3), value_unit=unit, basis="dry_matter",
                lab_id="LAB1", analysis_run_id="RUN_FEC")
        # temperatures: unit sensor daily means, with a 2-day gap and, in one tank, a 12-day outage
        ins("sensors", sensor_id="T_" + uid, variable="temp_c")
        for d in range(stock_day, FINAL + 1):
            gap = d in (20, 21) or (uid == "U3D1" and 50 <= d < 62)
            if not gap:
                ins("environment", record_id="%s_T%03d" % (uid, d + 10), timestamp=local_noon_utc(d), unit_id=uid,
                    variable="temp_c", aggregation="daily_mean", nominal_interval_min=1440, sensor_id="T_" + uid,
                    measure_position="outlet", value=round(temp + rng.gauss(0, 0.15), 2))
        n_d0 = 100 if stock_day == 0 else 100   # no deaths recorded before d0 in this generator
        truth[uid] = {
            "system": sid, "diet": did, "temp": temp,
            "n_d0": n_d0, "n_final": n, "deaths": t["dead_after_d0"],
            "b_start_kg": round(100 * mean_w(0) / 1000, 4) if stock_day < 0 else round(100 * w0 / 1000, 4),
            "b_end_kg": b_end, "w_final_g": mean_w(FINAL),
            "gain_kg": b_end - (100 * mean_w(0) / 1000) + t["dead_biomass_kg"] + t["removed_biomass_kg"],
            "feed_dm_eaten_g": t["feed_dm_eaten_g"], "prot_eaten_g": t["prot_eaten_g"],
            "adc_protein_pct": 100 * adc, "sgr_true_pct": 100 * sgr,
        }
        truth[uid]["fcr_bio"] = truth[uid]["feed_dm_eaten_g"] / 1000 / truth[uid]["gain_kg"]
        truth[uid]["survival_pct"] = 100 * (1 - truth[uid]["deaths"] / n_d0)
    # system-level reference temperatures (fallback for sensor outages)
    for sid, temp in SYSTEMS.items():
        for d in range(-7, FINAL + 1):
            ins("environment", record_id="%s_T%03d" % (sid, d + 10), timestamp=local_noon_utc(d), system_id=sid,
                variable="temp_c", aggregation="daily_mean", sensor_id="REF_" + sid, measure_position="reference",
                value=round(temp + rng.gauss(0, 0.1), 2))
    ins("deviations", record_id="DEV1", trial_id="T1", date=day(30), unit_id="U2D2", category="exclusion",
        exclusion_scope="unit", exclusion_reason="technical", description="Flow failure for 6 h, exclusion proposed",
        raised_by="human")
    con.execute("UPDATE weighings SET qc_flag = 'removed' WHERE record_id = 'U1D1_mid1_S20'")
    ins("deviations", record_id="DEV2", trial_id="T1", date=day(MID), category="data_quality", target_table="weighings",
        target_record_id="U1D1_mid1_S20", description="Scale error", decided_by="trial manager", decided_date=day(MID),
        raised_by="human")
    # analysis plan, frozen a week before d0; no random numbers are drawn here, so the data above
    # are the same with or without these rows
    ins("analysis_plans", plan_id="P1", trial_id="T1", version="1", file_path="tools/synthetic_trial.py#PLAN_TEXT",
        file_hash=hashlib.sha256(PLAN_TEXT.encode()).hexdigest(), frozen_at="2025-02-24T12:00:00Z")
    for o in PLAN_OUTCOMES:
        ins("planned_outcomes", plan_id="P1", **dict(_COMMON, **o))
    ins("covariate_roles", plan_id="P1", variable="w_d0", role="precision", measured="baseline")
    ins("covariate_roles", plan_id="P1", variable="density_kg_m3", role="mediator", measured="post_random")
    ins("randomisations", randomisation_id="RND_TEMP", trial_id="T1", scope="factor_to_system", method="complete",
        seed=20250210, software="python random (stdlib)", allocation_concealed=1, performed_by="trial statistician",
        date=day(-21))
    ins("randomisations", randomisation_id="RND_DIET", trial_id="T1", scope="diet_to_unit", method="blocked",
        strata="system", seed=20250211, software="python random (stdlib)", allocation_concealed=1,
        performed_by="trial statistician", date=day(-21))
    # primary analysis locked after the last weighing (d84 = 2025-05-26) and before unblinding (R38)
    ins("analysis_runs", run_id="RUN_PRIMARY", trial_id="T1", plan_id="P1", purpose="primary_locked",
        code_commit="synthetic", run_at="2025-05-28T09:00:00Z", run_by="trial statistician")
    con.commit()
    return con, model, truth


if __name__ == "__main__":
    yp = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
    out = sys.argv[2] if len(sys.argv) > 2 else ":memory:"
    con, model, truth = make_trial(yp, out)
    rows = {t: con.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0] for t in model["tables"]}
    print("loaded:", {k: v for k, v in rows.items() if v})
    print(json.dumps({u: {k: round(v, 3) if isinstance(v, float) else v for k, v in t.items()}
                      for u, t in list(truth.items())[:2]}, indent=1))
    print("synthetic trial loaded under all constraints: %d units, %d rows" % (len(truth), sum(rows.values())))
