#!/usr/bin/env python3
"""Prove that the constraints in data_model.yaml do what they claim.

1. A minimal VALID dataset must load (guards against over-strict constraints).
2. Every probe is a row that breaks one rule and MUST be rejected (guards against rules that
   exist only as prose). Each probe runs in a savepoint, so one failure cannot hide another.
3. Causality: rebuilt WITHOUT its target check only, each probe must be ACCEPTED, so the
   rejection is proven to come from that check and not from an unrelated one.
4. Coverage: every constraint (incl. those generated from event_types) is the target of a
   probe, or, for unique keys that only serve as composite-FK targets, is shown necessary.
5. Generator proof: every column carries the CHECKs its spec requires (scan of the DDL).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from build_db import all_constraints, build, build_ddl  # noqa: E402
import re  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
con, model = build(path)

VALID = [
    "INSERT INTO trials (trial_id, start_date) VALUES ('T1','2025-01-01'), ('T2','2025-01-01')",
    "INSERT INTO analysis_plans (plan_id, trial_id, version, file_hash) VALUES ('P1','T1','1.0','%s'), ('P2','T2','1.0',NULL)" % ("a" * 64),
    "INSERT INTO sites (site_id, timezone, latitude) VALUES ('S1','Europe/Oslo',63.4)",
    "INSERT INTO systems (system_id, trial_id, site_id, temp_control) VALUES ('SY1','T1','S1','controlled'), ('SY2','T2','S1','ambient')",
    "INSERT INTO diets (diet_id, trial_id, control_role) VALUES ('D1','T1','reference'), ('D2','T1','none'), ('D3','T2','none')",
    "INSERT INTO feed_batches (batch_id, diet_id, dm_frac) VALUES ('B1','D1',0.92), ('B2','D2',0.93), ('B3','D3',0.91)",
    "INSERT INTO units (unit_id, trial_id, system_id, diet_id) VALUES ('U1','T1','SY1','D1'), ('U2','T1','SY1','D2')",
    "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U1','d0',1,'2025-01-01'), ('U1','mid1',2,'2025-02-01'), ('U1','final',3,'2025-03-01'), ('U2','stocking',0,'2024-12-20'), ('U2','d0',1,'2025-01-01')",
    "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, biomass_kg) VALUES ('W1','U1','d0','2025-01-01','stocking',100,5.0)",
    "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, weight_g) VALUES ('W0','U2','stocking','2024-12-20','stocking',120,45.0)",
    "INSERT INTO weighings (record_id, unit_id, occasion, date, time, sampling_type, n_fish, biomass_kg) VALUES ('W4','U1','final','2025-03-01','08:00','harvest',60,30), ('W5','U1','final','2025-03-02','08:00','harvest',30,15)".replace("time, sampling_type", "time, lot_no, sampling_type").replace("'08:00','harvest',60", "'08:00',1,'harvest',60").replace("'08:00','harvest',30", "'08:00',2,'harvest',30"),
    "INSERT INTO weighings (record_id, unit_id, occasion, date, time, sampling_type, weight_g) VALUES ('W2','U1','mid1','2025-02-01','09:30','sample',55.0), ('W3','U1','mid1','2025-02-01','09:31','sample',60.0)",
    "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g) VALUES ('U1','2025-01-02','B1','D1',100)",
    "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g, off_protocol) VALUES ('U1','2025-01-03','B2','D2',40,1)",
    "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U2','end_early',2,'2025-02-10')",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EV8','U1','2025-03-01','harvest',60,'harvest',30)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source, n_fish_estimated, n_fish_low, n_fish_high) VALUES ('EV9','U1','2025-02-01','count_adjustment',4,'unaccounted','estimated_interpolated',1,2,6)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source, relative_to_weighing) VALUES ('EV10','U1','2025-03-01','mortality',1,'death','estimated_farm_system',NULL)".replace("'estimated_farm_system',NULL", "'estimated_interpolated','before'"),
    "INSERT INTO events (record_id, unit_id, date, end_date, event_type, scope, event_group_id) VALUES ('EV11','U1','2025-02-05','2025-02-07','lice_treatment','site','G1')",
    "INSERT INTO design_factors (factor_id, trial_id, name, role, applied_at, level_type) VALUES ('FT','T1','temperature','assigned_treatment','system','numeric')",
    "INSERT INTO factor_levels (record_id, factor_id, system_id, level, numeric_value, level_order, is_reference) VALUES ('FL1','FT','SY1','12C',12,1,1)",
    "INSERT INTO camera_calibrations (calibration_id, unit_id, camera_id, algorithm_version, valid_from, model) VALUES ('CAL2','U1','CAM1','v2','2025-02-01','linear')",
    "INSERT INTO uneaten_feed (unit_id, date, uneaten_wet_g) VALUES ('U1','2025-01-02',5)",
    "INSERT INTO environment (record_id, timestamp, unit_id, variable, value, aggregation) VALUES ('E1','2025-01-02T00:00:00Z','U1','temp_c',8.5,'daily_mean')",
    "INSERT INTO environment (record_id, timestamp, system_id, variable, value) VALUES ('E2','2025-01-02T00:00:00Z','SY1','temp_c',8.4)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EV1','U1','2025-01-03','mortality',2,'death',0.1)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source) VALUES ('EV2','U1','2025-01-04','mortality',0,'death','not_applicable')",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source) VALUES ('EV3','U1','2025-01-05','escape',3,'escape','estimated_last_mean')",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source) VALUES ('EV4','U1','2025-01-06','mortality',1,'death','estimated_interpolated')",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EV5','U1','2025-01-07','restock',10,'restock',0.5)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish) VALUES ('EV6','U1','2025-01-08','mortality',0)",
    "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EV7','U1','2025-01-09','cull',2,'cull_moribund',0.1)",
    "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled, adult_female, other_mobile) VALUES ('L2','U1','2025-02-03',20,25,40)",
    "INSERT INTO feeding (unit_id, date, batch_id, diet_id) VALUES ('U1','2025-01-04','B1','D1')",
    "INSERT INTO water_samples (record_id, date, unit_id, analyte, value) VALUES ('WS1','2025-01-02','U1','tan_n_mg_l',0.4)",
    "INSERT INTO water_samples (record_id, date, unit_id, analyte, censor) VALUES ('WS2','2025-01-02','U1','no2_n_mg_l','lt_lod')",
    "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value, value_unit, basis) VALUES ('C1','T1','S-1','diet','B1','D1','protein',460,'g_kg','as_fed')",
    "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, analyte, value, value_unit, basis) VALUES ('C2','T1','S-0','whole_body_initial','protein',170,'g_kg','wet')",
    "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled, adult_female) VALUES ('L1','U1','2025-02-01',20,3)",
    "INSERT INTO camera_calibrations (calibration_id, unit_id, camera_id, algorithm_version, valid_from, model) VALUES ('CAL1','U1','CAM1','v1','2025-01-01','bias_only')",
    "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version, calibration_id) VALUES ('U1','CAM1','2025-02-01T00:00:00Z','2025-02-02T00:00:00Z','v1','CAL1')",
    "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version, calibration_id, source) VALUES ('U1','CAM1','2025-02-01T00:00:00Z','2025-02-02T00:00:00Z','v2','CAL2','vendor')",
    "INSERT INTO composition (record_id, trial_id, sample_id, unit_id, sample_type, feces_method, analyte, value, value_unit, basis) VALUES ('C3','T1','S-F','U1','feces','stripping','Y',400,'mg_kg','dry_matter')",
    "INSERT INTO fish (fish_id, trial_id, pit_tag, unit_id) VALUES ('F1','T1','PIT1','U1')",
    "INSERT INTO deviations (record_id, trial_id, date, unit_id, category, exclusion_scope, description) VALUES ('DV1','T1','2025-02-01','U1','exclusion','unit','test')",
    "INSERT INTO deviations (record_id, trial_id, date, category, target_table, target_record_id, description) VALUES ('DV2','T1','2025-02-01','data_quality','weighings','W2','bad weight')",
    "INSERT INTO deviations (record_id, trial_id, plan_id, date, category, description) VALUES ('DV3','T1','P1','2025-02-01','analysis_plan','x')",
    "INSERT INTO design_factors (factor_id, trial_id, name, role, applied_at, level_type) VALUES ('FU','T1','photoperiod','assigned_treatment','unit','categorical')",
    "INSERT INTO factor_levels (record_id, factor_id, unit_id, level) VALUES ('FLU1','FU','U1','24L')",
    "INSERT INTO samples (sample_id, trial_id, unit_id, tissue) VALUES ('SM1','T1','U1','plasma')",
]

# (name, target, sql) -- every probe MUST be rejected, and must be ACCEPTED when only its
# target check is removed (proof that the target, not something else, rejects it).
# target: constraint id | col:<table>.<column> (column CHECKs) | required:<table>.<column>
#         | strict | fk_enforcement
PROBES = [
    ("unit with system of another trial", "C11",
     "INSERT INTO units (unit_id, trial_id, system_id, diet_id) VALUES ('UX','T1','SY2','D1')"),
    ("unit with diet of another trial", "C10",
     "INSERT INTO units (unit_id, trial_id, system_id, diet_id) VALUES ('UX','T1','SY1','D3')"),
    ("fish with trial different from its unit", "C12",
     "INSERT INTO fish (fish_id, trial_id, pit_tag, unit_id) VALUES ('FX','T2','PITX','U1')"),
    ("composition of another trial's unit", "C13",
     "INSERT INTO composition (record_id, trial_id, sample_id, unit_id, sample_type, analyte, value, value_unit, basis) VALUES ('CX','T2','S-X','U1','fillet','lipid',1,'g_kg','wet')"),
    ("composition with another trial's diet", "C14",
     "INSERT INTO composition (record_id, trial_id, sample_id, diet_id, sample_type, analyte, value, value_unit, basis) VALUES ('CX','T1','S-X','D3','fillet','lipid',1,'g_kg','wet')"),
    ("deviation on another trial's unit", "C15",
     "INSERT INTO deviations (record_id, trial_id, date, unit_id, description) VALUES ('DVX','T2','2025-02-01','U1','x')"),
    ("feeding with batch of another diet", "C17",
     "INSERT INTO feeding (unit_id, date, batch_id, diet_id, feed_given_g) VALUES ('U1','2025-01-06','B2','D1',10)"),
    ("diet analysis with batch of another diet", "C18",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value, value_unit, basis) VALUES ('CX','T1','S-X','diet','B1','D2','lipid',1,'g_kg','as_fed')"),
    ("weighing with unknown occasion for the unit", "C19",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g) VALUES ('WX','U2','mid1','2025-02-01','sample',50)"),
    ("camera estimate with calibration of another algorithm version", "C20",
     "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version, calibration_id) VALUES ('U1','CAM1','2025-03-01T00:00:00Z','2025-03-02T00:00:00Z','v2','CAL1')"),
    ("deviation with plan of another trial", "C21",
     "INSERT INTO deviations (record_id, trial_id, plan_id, date, category, description) VALUES ('DVX','T1','P2','2025-02-01','analysis_plan','x')"),
    ("tissue sample of another trial's unit", "C22",
     "INSERT INTO samples (sample_id, trial_id, unit_id, tissue) VALUES ('SMX','T2','U1','liver')"),
    ("stocking row at occasion final", "C30",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, lot_no, sampling_type, n_fish, biomass_kg) VALUES ('WX','U1','final','2025-03-01',3,'stocking',10,1)"),
    ("second stocking row for a unit (same lot)", "C31",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, biomass_kg) VALUES ('WX','U1','d0','2025-01-01','stocking',90,4)"),
    ("duplicate lot number for the same unit and occasion", "C31",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, lot_no, sampling_type, n_fish, biomass_kg) VALUES ('WX','U1','final','2025-03-03',2,'harvest',90,50)"),
    ("same fish weighed twice at one occasion", "C32",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, fish_id, weight_g) VALUES ('WX','U1','mid1','2025-02-01','sample','F1',50), ('WY','U1','mid1','2025-02-01','sample','F1',51)"),
    ("second reference diet in a trial", "C33",
     "INSERT INTO diets (diet_id, trial_id, control_role) VALUES ('DX','T1','reference')"),
    ("duplicate PIT tag in a trial", "C34",
     "INSERT INTO fish (fish_id, trial_id, pit_tag, unit_id) VALUES ('FX','T1','PIT1','U1')"),
    ("duplicate composition replicate", "C35",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value, value_unit, basis) VALUES ('CX','T1','S-1','diet','B1','D1','protein',461,'g_kg','as_fed')"),
    ("duplicate plan version", "C36",
     "INSERT INTO analysis_plans (plan_id, trial_id, version) VALUES ('PX','T1','1.0')"),
    ("duplicate design factor name", "C37",
     "INSERT INTO design_factors (factor_id, trial_id, name, role, applied_at, level_type) VALUES ('FX','T1','temperature','blocking','unit','categorical')"),
    ("duplicate occasion sequence number", "C38",
     "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U1','mid3',2,'2025-02-10')"),
    ("second level of the same factor for one system", "C39",
     "INSERT INTO factor_levels (record_id, factor_id, system_id, level) VALUES ('FLX','FT','SY1','8C')"),
    ("second level of the same factor for one unit", "C44",
     "INSERT INTO factor_levels (record_id, factor_id, unit_id, level) VALUES ('FLX','FU','U1','12L')"),
    ("environment with neither unit nor system", "C40",
     "INSERT INTO environment (record_id, timestamp, variable, value) VALUES ('EX','2025-01-02T00:00:00Z','temp_c',8)"),
    ("environment with both unit and system", "C40",
     "INSERT INTO environment (record_id, timestamp, unit_id, system_id, variable, value) VALUES ('EX','2025-01-02T00:00:00Z','U1','SY1','temp_c',8)"),
    ("water sample with both unit and system", "C41",
     "INSERT INTO water_samples (record_id, date, unit_id, system_id, analyte, value) VALUES ('WSX','2025-01-02','U1','SY1','ph',7)"),
    ("light regime with both unit and system", "C42",
     "INSERT INTO light_regimes (regime_id, unit_id, system_id, start_date) VALUES ('LR','U1','SY1','2025-01-01')"),
    ("factor level with neither unit nor system", "C43",
     "INSERT INTO factor_levels (record_id, factor_id, level) VALUES ('FLX','FT','x')"),
    ("same specimen number twice at one occasion", "C45",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g, specimen_no) VALUES ('WX','U1','mid1','2025-02-01','sample',50,1), ('WY','U1','mid1','2025-02-01','sample',51,1)"),
    ("water sample time 25:00", "C46",
     "INSERT INTO water_samples (record_id, date, time, unit_id, analyte, value) VALUES ('WSX','2025-01-02','25:00','U1','ph',7)"),
    ("individual weighing without weight", "C50",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, fish_id) VALUES ('WX','U1','mid1','2025-02-01','individual','F1')"),
    ("bulk row with n_fish = 0", "C51",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, n_fish, biomass_kg) VALUES ('WX','U1','mid1','2025-02-01','bulk',0,0)"),
    ("bulk weighing without n_fish", "C51",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, biomass_kg) VALUES ('WX','U1','mid1','2025-02-01','bulk',5)"),
    ("individual row without fish_id", "C52",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','individual',50)"),
    ("weighing time 24:30 (hour check)", "C53",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, time, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','24:30','sample',50)"),
    ("weighing time 09:60 (minute check)", "C53",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, time, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','09:60','sample',50)"),
    ("occasion mid1banana", "C54",
     "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U2','mid1banana',3,'2025-02-01')"),
    ("unknown occasion vocabulary", "C54",
     "INSERT INTO unit_occasions (unit_id, occasion, seq, occasion_date) VALUES ('U2','banana',3,'2025-02-01')"),
    ("mortality event without n_fish", "C55",
     "INSERT INTO events (record_id, unit_id, date, event_type, outcome_class) VALUES ('EX','U1','2025-01-06','mortality','death')"),
    ("restock without n_fish", "C55",
     "INSERT INTO events (record_id, unit_id, date, event_type, outcome_class, biomass_kg) VALUES ('EX','U1','2025-01-10','restock','restock',1)"),
    ("mortality event without outcome_class", "C56",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, biomass_kg) VALUES ('EX','U1','2025-01-06','mortality',1,0.1)"),
    ("dead fish without biomass and without estimate", "C57",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class) VALUES ('EX','U1','2025-01-06','mortality',5,'death')"),
    ("n_fish = 0 with biomass 5", "C57",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, biomass_kg) VALUES ('EX','U1','2025-01-10','mortality',0,5)"),
    ("n_fish = 0 with weight_source estimated", "C57",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, weight_source) VALUES ('EX','U1','2025-01-10','mortality',0,'estimated_last_mean')"),
    ("water sample NULL value with censor none", "C58",
     "INSERT INTO water_samples (record_id, date, unit_id, analyte) VALUES ('WSX','2025-01-02','U1','tan_n_mg_l')"),
    ("water sample NULL value with censor explicitly NULL", "C58",
     "INSERT INTO water_samples (record_id, date, unit_id, analyte, censor) VALUES ('WSY','2025-01-02','U1','tan_n_mg_l',NULL)"),
    ("composition NULL value with censor none", "C59",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, batch_id, diet_id, analyte, value_unit, basis) VALUES ('CX','T1','S-9','diet','B1','D1','lipid','g_kg','as_fed')"),
    ("assay result NULL value with censor none", "C60",
     "INSERT INTO assay_results (record_id, sample_id, analyte, value_unit) VALUES ('AX','SM1','cortisol','ng_ml')"),
    ("diet composition row without batch", "C61",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, diet_id, analyte, value, value_unit, basis) VALUES ('CX','T1','S-8','diet','D1','lipid',200,'g_kg','as_fed')"),
    ("diet composition row without diet_id", "C61",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, analyte, value, value_unit, basis) VALUES ('CX','T1','S-6','diet','lipid',200,'g_kg','as_fed')"),
    ("exclusion without scope", "C62",
     "INSERT INTO deviations (record_id, trial_id, date, category, description) VALUES ('DVX','T1','2025-02-01','exclusion','x')"),
    ("deviation target_table without record id", "C63",
     "INSERT INTO deviations (record_id, trial_id, date, target_table, description) VALUES ('DVX','T1','2025-02-01','weighings','x')"),
    ("bad hash length", "C64",
     "INSERT INTO analysis_plans (plan_id, trial_id, version, file_hash) VALUES ('PX','T1','2.0','abc')"),
    ("feces row with batch but no diet", "C65",
     "INSERT INTO composition (record_id, trial_id, sample_id, unit_id, sample_type, batch_id, feces_method, analyte, value, value_unit, basis) VALUES ('CX','T1','S-G','U1','feces','B1','stripping','Y',300,'mg_kg','dry_matter')"),
    ("fish_fate on a harvest row", "C66",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, lot_no, sampling_type, n_fish, biomass_kg, fish_fate) VALUES ('WX','U1','final','2025-03-03',3,'harvest',5,2,'killed')"),
    ("recovery test with zero feed in", "C67",
     "INSERT INTO recovery_tests (record_id, trial_id, feed_in_dm_g, feed_recovered_dm_g) VALUES ('RX','T1',0,0)"),
    ("estimated range not containing the count", "C68",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, weight_source, n_fish_low) VALUES ('EX','U1','2025-01-10','escape',3,'escape','estimated_interpolated',5)"),
    ("feces_method on a non-feces row", "C69",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, feces_method, analyte, value, value_unit, basis) VALUES ('CX','T1','S-H','fillet','stripping','lipid',100,'g_kg','wet')"),
    ("histogram bin upper below lower", "C70",
     "INSERT INTO camera_distributions (record_id, unit_id, camera_id, date, bin_lower_g, bin_upper_g) VALUES ('CD','U1','CAM1','2025-02-01',500,400)"),
    ("camera period ends before it starts", "C71",
     "INSERT INTO camera_estimates (unit_id, camera_id, period_start, period_end, algorithm_version) VALUES ('U1','CAM2','2025-02-02T00:00:00Z','2025-02-01T00:00:00Z','v1')"),
    ("lice stage counts with zero fish sampled", "C72",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled, adult_female) VALUES ('LX','U1','2025-02-04',0,3)"),
    ("trial ends before it starts", "C73",
     "INSERT INTO trials (trial_id, start_date, end_date) VALUES ('TX','2025-05-01','2025-01-01')"),
    ("location history ends before it starts", "C75",
     "INSERT INTO unit_location_history (record_id, unit_id, system_id, valid_from, valid_to) VALUES ('UL','U1','SY1','2025-02-01','2025-01-01')"),
    ("fish history with empty interval (valid_to exclusive)", "C76",
     "INSERT INTO fish_unit_history (record_id, fish_id, unit_id, valid_from, valid_to) VALUES ('FH','F1','U1','2025-02-01','2025-02-01')"),
    ("calibration valid_to before valid_from", "C77",
     "INSERT INTO camera_calibrations (calibration_id, unit_id, camera_id, algorithm_version, valid_from, valid_to, model) VALUES ('CALX','U1','CAM9','v1','2025-02-01','2025-01-01','linear')"),
    ("geometry valid_to before valid_from", "C78",
     "INSERT INTO unit_geometry (record_id, unit_id, valid_from, valid_to, volume_m3) VALUES ('UG','U1','2025-02-01','2025-01-01',1)"),
    ("feces collection ends before it starts", "C79",
     "INSERT INTO composition (record_id, trial_id, sample_id, unit_id, sample_type, feces_method, collection_start, collection_end, analyte, value, value_unit, basis) VALUES ('CX','T1','S-J','U1','feces','stripping','2025-02-10','2025-02-01','Y',1,'mg_kg','dry_matter')"),
    ("light regime ends before it starts", "C80",
     "INSERT INTO light_regimes (regime_id, unit_id, start_date, end_date) VALUES ('LRX','U1','2025-02-01','2025-01-01')"),
    ("event ends before it starts", "C81",
     "INSERT INTO events (record_id, unit_id, date, end_date, event_type) VALUES ('EX','U1','2025-01-06','2025-01-01','algal_bloom')"),
    ("harvest event with outcome_class death", "C83",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EX','U1','2025-03-02','harvest',30,'death',15)"),
    ("outcome_class contradicting event_type", "C83",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EX','U1','2025-01-10','mortality',1,'restock',0.1)"),
    ("transfer_in with outcome_class death", "C83",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EX','U1','2025-01-10','transfer_in',1,'death',0.1)"),
    ("handling event carrying n_fish", "C84",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish) VALUES ('EX','U1','2025-01-10','handling',7)"),
    ("setpoint interval empty", "C85",
     "INSERT INTO system_setpoints (record_id, system_id, variable, valid_from, valid_to, setpoint) VALUES ('SPX','SY1','temp_c','2025-02-01','2025-02-01',12)"),
    # --- column-level checks generated by build_db (one probe per kind; DDL scan proves every column got it)
    ("empty string in a text column", "col:units.block",
     "UPDATE units SET block = '' WHERE unit_id = 'U1'"),
    ("site timezone empty string", "col:sites.timezone",
     "INSERT INTO sites (site_id, timezone) VALUES ('SX','')"),
    ("date in Norwegian format 01.02.2025", "col:lice_counts.date",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled) VALUES ('LX','U1','01.02.2025',20)"),
    ("date not zero-padded 2025-1-15", "col:lice_counts.date",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled) VALUES ('LX','U1','2025-1-15',20)"),
    ("impossible date 2025-02-30", "col:lice_counts.date",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled) VALUES ('LX','U1','2025-02-30',20)"),
    ("timestamp without UTC marker", "col:environment.timestamp",
     "INSERT INTO environment (record_id, timestamp, unit_id, variable, value) VALUES ('EX','2025-01-02 08:00','U1','temp_c',8)"),
    ("enum outside vocabulary", "col:weighings.sampling_type",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','banana',50)"),
    ("boolean other than 0/1", "col:feeding.off_protocol",
     "UPDATE feeding SET off_protocol = 2 WHERE unit_id = 'U1'"),
    ("negative fish sampled (min)", "col:lice_counts.n_fish_sampled",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled) VALUES ('LX','U1','2025-02-02',-3)"),
    ("latitude 999 (max)", "col:sites.latitude",
     "INSERT INTO sites (site_id, timezone, latitude) VALUES ('SX','Europe/Oslo',999)"),
    ("planned outcome alpha 5 (max)", "col:planned_outcomes.alpha",
     "INSERT INTO planned_outcomes (plan_id, outcome_label, outcome, role, unit_of_analysis, alpha) VALUES ('P1','sgr','sgr_pct_day','primary','unit',5)"),
    ("site without timezone (no default)", "required:sites.timezone",
     "INSERT INTO sites (site_id) VALUES ('SX')"),
    ("composition without basis", "required:composition.basis",
     "INSERT INTO composition (record_id, trial_id, sample_id, sample_type, analyte, value, value_unit) VALUES ('CX','T1','S-7','fillet','lipid',100,'g_kg')"),
    ("text in an integer column", "strict",
     "INSERT INTO events (record_id, unit_id, date, event_type, n_fish, outcome_class, biomass_kg) VALUES ('EX','U1','2025-01-10','mortality','12 fish','death',0.1)"),
    ("empty string in a numeric column", "strict",
     "INSERT INTO weighings (record_id, unit_id, occasion, date, sampling_type, weight_g) VALUES ('WX','U1','mid1','2025-02-01','sample','')"),
    ("lice count linked to a non-existent unit (column fk)", "fk:lice_counts.unit_id",
     "INSERT INTO lice_counts (record_id, unit_id, date, n_fish_sampled) VALUES ('LX','UZ','2025-02-02',20)"),
    ("delete of a referenced trial", "fk_enforcement",
     "DELETE FROM trials WHERE trial_id = 'T1'"),
]


def load_valid(con):
    errors = []
    for stmt in VALID:
        try:
            con.execute(stmt)
        except sqlite3.Error as e:
            errors.append(("VALID rows rejected (over-strict)", stmt[:90], str(e)))
    con.commit()
    return errors


def try_probe(con, stmt):
    con.execute("SAVEPOINT p")
    try:
        con.execute(stmt)
        return "accepted"
    except sqlite3.IntegrityError as e:
        return "rejected: " + str(e)
    except sqlite3.Error as e:
        return "error: " + str(e)
    finally:
        con.execute("ROLLBACK TO p")
        con.execute("RELEASE p")


failed = load_valid(con)

# 1. every probe is rejected by the full schema
for name, target, stmt in PROBES:
    r = try_probe(con, stmt)
    if not r.startswith("rejected"):
        failed.append((name, "must be rejected", r))

# 2. causality: with ONLY the target removed, the probe is accepted
for name, target, stmt in PROBES:
    c2, _ = build(path, drop=frozenset([target]), model=model)
    errs = load_valid(c2)
    if errs:
        failed.append((name, "valid data fail without " + target, errs[0][2]))
        continue
    r = try_probe(c2, stmt)
    if r != "accepted":
        failed.append((name, "still rejected without its target %s (other check is responsible)" % target, r))

# 3. coverage: every constraint is the target of a probe, except unique keys that exist
#    only as composite-FK targets -- those must be proven NECESSARY (dropping one breaks its FK)
targets = {t for _, t, _ in PROBES}
cons = all_constraints(model)
fk_targets = {}
for c in cons:
    if c["type"] == "fk":
        fk_targets.setdefault((c["ref_table"], tuple(c["ref_columns"])), []).append(c["id"])
for c in cons:
    if c["id"] in targets:
        continue
    key = (c["table"], tuple(c.get("columns", [])))
    if c["type"] == "unique" and key in fk_targets:
        c2, _ = build(path, drop=frozenset([c["id"]]), model=model)
        errs = load_valid(c2)
        if not any("foreign key mismatch" in e[2] for e in errs):
            failed.append((c["id"], "FK-target unique key not shown to be necessary", str(errs[:1])))
        continue
    failed.append((c["id"], "constraint has no probe", c.get("expr") or c.get("columns")))

# 4. generator proof: every column of a checked kind carries its CHECK in the DDL
ddl = {re.search(r'CREATE TABLE "(\w+)"', s).group(1): s for s in build_ddl(model) if s.startswith("CREATE TABLE")}
for tname, t in model["tables"].items():
    if not ddl[tname].rstrip().endswith("STRICT"):
        failed.append((tname, "table not STRICT", ""))
    for cname, spec in t["columns"].items():
        line = next(l for l in ddl[tname].splitlines() if l.strip().startswith('"%s" ' % cname))
        need = []
        if spec.get("required"):
            need.append("NOT NULL")
        need += {"enum": [" IN ("], "boolean": ["IN (0, 1)"], "string": ["trim("],
                 "date": ["date("], "datetime": ["GLOB", "datetime("]}.get(spec["type"], [])
        need += ([">= %s" % spec["min"]] if "min" in spec else []) + (["<= %s" % spec["max"]] if "max" in spec else [])
        for n in need:
            if n not in line:
                failed.append(("%s.%s" % (tname, cname), "DDL lacks check " + n, line.strip()[:80]))
        if spec.get("fk") and 'FOREIGN KEY ("%s") REFERENCES' % cname not in ddl[tname]:
            failed.append(("%s.%s" % (tname, cname), "DDL lacks foreign key", ""))

# 5. relabelling an occasion must cascade to its weighings (importers renumber occasions)
try:
    con.execute("UPDATE unit_occasions SET occasion = 'mid2' WHERE unit_id = 'U1' AND occasion = 'mid1'")
    n = con.execute("SELECT count(*) FROM weighings WHERE unit_id = 'U1' AND occasion = 'mid2'").fetchone()[0]
    if n != 2:
        failed.append(("occasion relabel cascade", "weighings not relabelled", str(n)))
except sqlite3.Error as e:
    failed.append(("occasion relabel cascade", "rejected", str(e)))

import json  # noqa: E402
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".coverage_constraints.json"), "w") as f:
    covered = sorted(targets & {c["id"] for c in cons}) + sorted(
        c["id"] for c in cons if c["type"] == "unique" and (c["table"], tuple(c["columns"])) in fk_targets
        and c["id"] not in targets)
    json.dump({"exercised": covered, "waived": {}}, f)

if failed:
    for f in failed:
        print("FAIL:", *f)
    print("%d problem(s); %d probes, %d valid statements" % (len(failed), len(PROBES), len(VALID)))
    sys.exit(1)
n_cons = len(cons)
print("OK: %d valid statements loaded; %d invalid rows rejected, each proven to be rejected by its own "
      "target check; all %d constraints covered; every column's generated checks present"
      % (len(VALID), len(PROBES), n_cons))
