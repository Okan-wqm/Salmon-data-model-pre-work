#!/usr/bin/env python3
"""Prove that the constraints in data_model.yaml do what they claim.

1. A minimal VALID dataset must load (guards against over-strict constraints).
2. Every probe is a row that breaks one rule and MUST be rejected (guards against rules that
   exist only as prose). Each probe runs in a savepoint, so one failure cannot hide another.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from build_db import build  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
con, model = build(path)

VALID = [
    "INSERT INTO trials (trial_id, start_date) VALUES ('T1','2025-01-01'), ('T2','2025-01-01')",
    "INSERT INTO sites (site_id, timezone, latitude) VALUES ('S1','Europe/Oslo',63.4)",
    "INSERT INTO systems (system_id, trial_id, site_id, temp_control) VALUES ('SY1','T1','S1','controlled'), ('SY2','T2','S1','ambient')",
    "INSERT INTO diets (diet_id, trial_id, control_role) VALUES ('D1','T1','reference'), ('D2','T1','none'), ('D3','T2','none')",
    "INSERT INTO feed_batches (batch_id, diet_id, dm_frac) VALUES ('B1','D1',0.92), ('B2','D2',0.93), ('B3','D3',0.91)",
    "INSERT INTO units (unit_id, trial_id, system_id, diet_id) VALUES ('U1','T1','SY1','D1'), ('U2','T1','SY1','D2')",
    "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U1','d0',1,'2025-01-01'), ('U1','final',2,'2025-03-01'), ('U1','mid1',3,'2025-02-01'), ('U2','d0',1,'2025-01-01')",
    "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, biomass_kg) VALUES ('W1','U1','d0','2025-01-01','stocking',100,5.0)",
    "INSERT INTO weighings (record_id, unit_id, occasion, date, time, sampling_type, weight_g) VALUES ('W2','U1','mid1','2025-02-01','09:30','sample',55.0), ('W3','U1','mid1','2025-02-01','09:31','sample',60.0)",
    "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g) VALUES ('U1','2025-01-02','B1','D1',100), ('U1','2025-01-02','B1','D1',50)"
    if False else "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g) VALUES ('U1','2025-01-02','B1','D1',100)",
    "INSERT INTO uneaten_feed (unit_id, date, uneaten_wet_g) VALUES ('U1','2025-01-02',5)",
    "INSERT INTO environment (record_id, timestamp, unit_id, variable, value, aggregation) VALUES ('E1','2025-01-02T00:00:00Z','U1','temp_c',8.5,'daily_mean')",
    "INSERT INTO environment (record_id, timestamp, system_id, variable, value) VALUES ('E2','2025-01-02T00:00:00Z','SY1','temp_c',8.4)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EV1','U1','2025-01-03','mortality',2,'death',0.1)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source) VALUES ('EV2','U1','2025-01-04','mortality',0,'death','not_applicable')",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source) VALUES ('EV3','U1','2025-01-05','escape',3,'escape','estimated_last_mean')",
    "INSERT INTO water_samples (record_id, date, unit_id, analyte, value) VALUES ('WS1','2025-01-02','U1','tan_n_mg_l',0.4)",
    "INSERT INTO water_samples (record_id, date, unit_id, analyte, censor) VALUES ('WS2','2025-01-02','U1','no2_n_mg_l','lt_lod')",
    "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value, value_unit, basis) VALUES ('C1','T1','S-1','diet','B1','D1','protein',460,'g_kg','as_fed')",
    "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, analyte, value, value_unit, basis) VALUES ('C2','T1','S-0','whole_body_initial','protein',170,'g_kg','wet')",
    "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled, adult_female) VALUES ('L1','U1','2025-02-01',20,3)",
    "INSERT INTO camera_calibrations (calibration_id, unit_id, camera_id, algorithm_version, valid_from, model) VALUES ('CAL1','U1','CAM1','v1','2025-01-01','bias_only')",
    "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version, calibration_id) VALUES ('U1','CAM1','2025-02-01T00:00:00Z','2025-02-02T00:00:00Z','v1','CAL1')",
    "INSERT INTO fish (fish_id, trial_id, pit_tag, unit_id) VALUES ('F1','T1','PIT1','U1')",
    "INSERT INTO deviations (record_id, trial_id, date, unit_id, category, exclusion_scope, description) VALUES ('DV1','T1','2025-02-01','U1','exclusion','unit','test')",
    "INSERT INTO deviations (record_id, trial_id, date, category, target_table, target_record_id, description) VALUES ('DV2','T1','2025-02-01','data_quality','weighings','W2','bad weight')",
    "INSERT INTO analysis_plans (plan_id, trial_id, version, file_hash) VALUES ('P1','T1','1.0','%s')" % ("a" * 64),
]

# (name, sql) -- every one MUST raise sqlite3.IntegrityError
PROBES = [
    ("unit with system of another trial (R17)",
     "INSERT INTO units (unit_id, trial_id, system_id, diet_id) VALUES ('UX','T1','SY2','D1')"),
    ("unit with diet of another trial (R17)",
     "INSERT INTO units (unit_id, trial_id, system_id, diet_id) VALUES ('UX','T1','SY1','D3')"),
    ("fish with trial different from its unit (R17)",
     "INSERT INTO fish (fish_id, trial_id, pit_tag, unit_id) VALUES ('FX','T2','PITX','U1')"),
    ("feeding with batch of another diet (R17)",
     "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g) VALUES ('U1','2025-01-03','B2','D1',10)"),
    ("feeding diet different from unit diet (R17)",
     "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g) VALUES ('U1','2025-01-03','B2','D2',10)"),
    ("weighing with unknown occasion for the unit (C19)",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g) VALUES ('WX','U2','mid1','2025-02-01','sample',50)"),
    ("environment with neither unit nor system (R13)",
     "INSERT INTO environment (record_id, timestamp, variable, value) VALUES ('EX','2025-01-02T00:00:00Z','temp_c',8)"),
    ("environment with both unit and system (R13)",
     "INSERT INTO environment (record_id, timestamp, unit_id, system_id, variable, value) VALUES ('EX','2025-01-02T00:00:00Z','U1','SY1','temp_c',8)"),
    ("light regime with both unit and system (R13)",
     "INSERT INTO light_regimes (regime_id, unit_id, system_id, start_date) VALUES ('LR','U1','SY1','2025-01-01')"),
    ("second stocking row for a unit",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, biomass_kg) VALUES ('WX','U1','d0','2025-01-01','stocking',90,4)"),
    ("duplicate bulk row for the same unit and occasion",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, biomass_kg) VALUES ('WX','U1','final','2025-03-01','harvest',90,50), ('WY','U1','final','2025-03-01','harvest',90,50)"),
    ("second reference diet in a trial (R21)",
     "INSERT INTO diets (diet_id, trial_id, control_role) VALUES ('DX','T1','reference')"),
    ("duplicate PIT tag in a trial (R27)",
     "INSERT INTO fish (fish_id, trial_id, pit_tag, unit_id) VALUES ('FX','T1','PIT1','U1')"),
    ("duplicate composition replicate (R25)",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value, value_unit, basis) VALUES ('CX','T1','S-1','diet','B1','D1','protein',461,'g_kg','as_fed')"),
    ("duplicate plan version",
     "INSERT INTO analysis_plans (plan_id, trial_id, version) VALUES ('PX','T1','1.0')"),
    ("bad hash length",
     "INSERT INTO analysis_plans (plan_id, trial_id, version, file_hash) VALUES ('PX','T1','2.0','abc')"),
    ("weighing time 25:99",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, time, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','25:99','sample',50)"),
    ("unknown occasion vocabulary",
     "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U2','banana',2,'2025-02-01')"),
    ("individual weighing without weight",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type) VALUES ('WX','U1','mid1','2025-02-01','individual')"),
    ("bulk weighing without n_fish",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, biomass_kg) VALUES ('WX','U1','mid1','2025-02-01','bulk',5)"),
    ("mortality event without n_fish (R16)",
     "INSERT INTO events (record_id, unit_id, date, event_type, outcome_class) VALUES ('EX','U1','2025-01-06','mortality','death')"),
    ("mortality event without outcome_class (R16)",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, biomass_kg) VALUES ('EX','U1','2025-01-06','mortality',1,0.1)"),
    ("dead fish without biomass and without estimate (R16)",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class) VALUES ('EX','U1','2025-01-06','mortality',5,'death')"),
    ("event ends before it starts",
     "INSERT INTO events (record_id, unit_id, date, end_date, event_type) VALUES ('EX','U1','2025-01-06','2025-01-01','algal_bloom')"),
    ("water sample NULL value with censor none",
     "INSERT INTO water_samples (record_id, date, unit_id, analyte) VALUES ('WSX','2025-01-02','U1','tan_n_mg_l')"),
    ("water sample NULL value with censor explicitly NULL",
     "INSERT INTO water_samples (record_id, date, unit_id, analyte, censor) VALUES ('WSY','2025-01-02','U1','tan_n_mg_l',NULL)"),
    ("composition NULL value with censor none",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value_unit, basis) VALUES ('CX','T1','S-9','diet','B1','D1','lipid','g_kg','as_fed')"),
    ("diet composition row without batch",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, analyte, value, value_unit, basis) VALUES ('CX','T1','S-8','diet','lipid',200,'g_kg','as_fed')"),
    ("composition without basis column value",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, analyte, value, value_unit) VALUES ('CX','T1','S-7','fillet','lipid',100,'g_kg')"),
    ("exclusion without scope",
     "INSERT INTO deviations (record_id, trial_id, date, category, description) VALUES ('DVX','T1','2025-02-01','exclusion','x')"),
    ("deviation target_table without record id",
     "INSERT INTO deviations (record_id, trial_id, date, target_table, description) VALUES ('DVX','T1','2025-02-01','weighings','x')"),
    ("lice stage count above fish sampled",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled, adult_female) VALUES ('LX','U1','2025-02-02',5,50)"),
    ("negative fish sampled",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled) VALUES ('LX','U1','2025-02-02',-3)"),
    ("histogram bin upper below lower",
     "INSERT INTO camera_distributions (record_id, unit_id, camera_id, date, bin_lower_g, bin_upper_g) VALUES ('CD','U1','CAM1','2025-02-01',500,400)"),
    ("camera period ends before it starts",
     "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version) VALUES ('U1','CAM2','2025-02-02T00:00:00Z','2025-02-01T00:00:00Z','v1')"),
    ("camera estimate with calibration of another algorithm version (R22)",
     "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version, calibration_id) VALUES ('U1','CAM1','2025-03-01T00:00:00Z','2025-03-02T00:00:00Z','v2','CAL1')"),
    ("trial ends before it starts",
     "INSERT INTO trials (trial_id, start_date, end_date) VALUES ('TX','2025-05-01','2025-01-01')"),
    ("planned outcome alpha 5",
     "INSERT INTO planned_outcomes (plan_id, outcome, role, unit_of_analysis, alpha) VALUES ('P1','sgr_pct_day','primary','unit',5)"),
    ("latitude 999",
     "INSERT INTO sites (site_id, timezone, latitude) VALUES ('SX','Europe/Oslo',999)"),
    ("site without timezone (no default)",
     "INSERT INTO sites (site_id) VALUES ('SX')"),
    ("enum outside vocabulary (sampling_type)",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','banana',50)"),
    ("location history ends before it starts",
     "INSERT INTO unit_location_history (record_id, unit_id, system_id, valid_from, valid_to) VALUES ('UL','U1','SY1','2025-02-01','2025-01-01')"),
    ("delete of a referenced trial",
     "DELETE FROM trials WHERE trial_id = 'T1'"),
]

failed = []
for stmt in VALID:
    try:
        con.execute(stmt)
    except sqlite3.Error as e:
        failed.append(("VALID rows rejected (over-strict)", stmt[:90], str(e)))
con.commit()

for name, stmt in PROBES:
    con.execute("SAVEPOINT p")
    try:
        con.execute(stmt)
        failed.append((name, "ACCEPTED but must be rejected", ""))
    except sqlite3.IntegrityError:
        pass
    except sqlite3.Error as e:
        failed.append((name, "unexpected error type", str(e)))
    finally:
        con.execute("ROLLBACK TO p")
        con.execute("RELEASE p")

if failed:
    for f in failed:
        print("FAIL:", *f)
    print("%d problem(s); %d probes, %d valid statements" % (len(failed), len(PROBES), len(VALID)))
    sys.exit(1)
print("OK: %d valid statements loaded, %d invalid rows rejected" % (len(VALID), len(PROBES)))
