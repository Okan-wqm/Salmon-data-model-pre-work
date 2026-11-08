# Salmon feed trial data model

A data model for Atlantic salmon feed trials run in tanks, RAS and sea cages under
different climates. It is designed for **statistical analysis**: it stores not only the
measurements, but also the information needed to decide which conclusions the data can
support.

- `data_model.yaml`: the schema (v0.9). It has 47 tables (19 optional), 69 database
  constraints, 41 cross-table rules and one written definition for every response metric.
- `tools/`: the tooling that turns the schema into a database and tests it (see
  [Validation](#validation)).

## Why a dedicated model

Most errors in feed-trial analysis are not in the statistics software. They come from what
the data do or do not record. The model therefore makes these questions explicit:

| Question | How the model answers it |
|---|---|
| What is the experimental unit? | The tank or cage (`units`), never the fish. Fish-level data are nested in units. |
| At what level was each factor applied? | `design_factors.applied_at` = unit, system or site. A temperature set per RAS loop is a whole-plot factor (split-plot) and must be tested against systems, not tanks (rules R02, R35). |
| Was "climate" assigned or only observed? | `design_factors.role` separates an assigned treatment (a temperature setpoint) from an observed condition (sea-site climate). With one site per climate level the climate effect is confounded with site and has zero degrees of freedom. Rule R02 blocks inference on it. |
| Which analysis population? | ITT, mITT and per-protocol are defined, and exclusions are logged with who decided them and when (`deviations`). |
| Which covariates are allowed? | `covariate_roles` marks post-randomisation variables (O2, density, lice) as mediators. Adjusting for them under a total-effect estimand is an error (rule R28). |
| How is each metric computed? | `derived_metrics` gives one definition each for SGR, TGC, degree-days, gross / apparent / biological FCR, feed intake with uneaten-feed recovery (Helland et al. 1996), Y2O3 digestibility, nutrient retention, survival and organ indices, with units, time windows and missing-data handling. |

## Analyses it supports

- **Diet effects:** linear mixed models with tank or cage as the unit and initial weight as covariate (ANCOVA).
- **Diet × temperature:** split-plot models, with system as the whole-plot stratum.
- **Growth curves:** repeated measures on a common time axis, with time-varying temperature and daylength.
- **Dose-response:** requirement studies (broken-line and quadratic models) on analysed rather than nominal dose.
- **Survival:** binomial GLMM, Cox models with unit frailty, and Kaplan–Meier. Deaths are separated from censoring through event classes.
- **Ordinal welfare and quality scores:** cumulative link models, with defined scales and rater identity.
- **Multi-site and multi-trial pooling:** site and climate covariates, cross-trial formulation IDs, and pre-registered outcomes with power inputs (`planned_outcomes`).

## Validation

```bash
python3 tools/check_schema.py data_model.yaml
```

This single command runs three steps:

1. **Static consistency.**
   - Every column, rule reference, constraint id and threshold parameter named in the text must exist.
   - Enum defaults must be valid values.
   - Foreign-key targets must be keys.
   - Duplicate YAML keys are rejected.
2. **Database build.** `tools/build_db.py` creates SQLite STRICT tables with types, required
   fields, vocabularies, ranges, ISO date formats, composite keys (trial and diet
   consistency) and the constraints generated from the single list of count-changing events.
3. **Constraint proof.** `tools/test_constraints.py` loads a valid mini-trial and shows that
   74 deliberately invalid rows are rejected, each by the intended constraint. Examples:
   - a unit linked to another trial's diet;
   - text in a numeric field;
   - a date like `01.02.2025`;
   - a dead fish with neither a weight nor an estimate.

During development the schema was also run end to end on synthetic trials with known true
effects:

- a tank trial;
- a RAS split-plot dose-response trial (3 temperatures × 4 lysine levels, PIT-tagged fish);
- a three-site sea-cage trial from 59°N to 70°N;
- an ingestion test on realistic exports from farm, feeding-system, camera and lab software.

These runs drove most design decisions. Two examples:

- **Split-plot.** Ignoring the split-plot stratum turned a temperature effect with p = 0.066 into p = 0.0002 on the same data.
- **Early termination.** Treating an early-terminated cage as a normal final weight inflated the estimated diet effect from 0.13 to 0.46.

## Adapting it to an organisation

The model is a starting point. Before it is used on real data it should be fitted to the
organisation's own sources and existing models:

- **Source mapping.** Map the farm production system, feeding system or barge, biomass
  cameras and lab LIMS to the tables. This means site and unit codes, feed article to batch,
  lab analyte names, units, and reporting basis (as-is or dry matter).
- **Time and clocks.** Set source clocks and time zones (`import_batches.source_clock`).
  Some feeding systems run on fixed CET; daylight-saving changes need explicit handling.
- **Thresholds.** Replace the placeholder thresholds (`validation_params_defaults`) with
  values from the organisation's own historical variance.
- **Rule engine.** Implement the rule engine for rules marked `enforced_by: validator`, and
  the metric definitions as tested code.

## Limitations

- **Cross-table rules.** 39 of the 41 rules need a validator or an analysis-plan decision;
  only R13 and R16 are fully enforced by the database.
- **Metric definitions.** Metrics are defined in precise prose; a reference implementation
  in code is the next step.
- **Deferred items.** These are listed in the schema header: RAS operations log, diet
  ingredient matrix and others.
