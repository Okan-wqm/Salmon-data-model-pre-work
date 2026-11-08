# Salmon feed trial data model

A data model for Atlantic salmon feed trials run in tanks, RAS and sea cages under
different climates. It is designed for **statistical analysis**: it stores not only the
measurements, but also the information needed to decide which conclusions the data can
support.

- `data_model.yaml`: the schema (v0.9). It has 47 tables (19 optional), 69 database
  constraints, 41 cross-table rules and one written definition for every response metric.
- `tools/`: the database builder, the reference implementation of every metric, the rule
  engine, threshold calibration and the tests that prove them (see
  [Tools and validation](#tools-and-validation)).

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

## Tools and validation

| Tool | What it does |
|---|---|
| `tools/build_db.py` | Builds the schema into SQLite STRICT tables with every type, vocabulary, range, date format, composite key and rule constraint |
| `tools/metrics.py` | Reference implementation of every metric in `derived_metrics` (growth, degree-days, FCR, intake, digestibility, retention, survival, organ indices, unionised ammonia, daylength) and the one-row-per-unit ITT analysis dataset |
| `tools/validate.py` | Rule engine for the 39 cross-table rules that need more than a database constraint; findings by severity, thresholds per trial |
| `tools/calibrate_params.py` | Estimates the rule thresholds and power inputs (within- and between-unit CV, ICC) from an organisation's historical trials |
| `tools/synthetic_trial.py` | A RAS split-plot trial (2 temperatures × 2 diets, 8 tanks, 84 days) with known true values, used by the tests |

```bash
python3 tools/run_all_tests.py data_model.yaml
```

This runs the steps below as one chain, in dependency order, and stops at the first failure.

1. **Schema.** Every name used in a rule, metric or threshold must exist.
2. **Database.** The schema builds into a database.
3. **Constraints.** 92 invalid rows must be rejected. Each one is also rebuilt without its own target check and must then be accepted, which proves the rejection comes from that check and not an unrelated one.
4. **Synthetic trial.** 3,699 rows load under all constraints.
5. **Metrics.** All 38 metrics are compared with known true values or hand calculations (277 checks). Biological FCR, intake, protein intake, gain and survival agree with the truth to within 0.01 %.
6. **Rules.** The clean trial produces no errors. For each of the 39 validator rules there is a change to a copy of the trial that makes exactly that rule fire with the declared severity. Legitimate changes are also checked to stay silent.
7. **Thresholds.** The estimates recover the variation built into the data, detect an injected 3 % recount error, and are withheld when there are too few observations.
8. **Coverage gate.** The chain fails if any metric, validator rule or constraint is not exercised by a test. Adding an untested metric, rule or constraint to the schema breaks it.

The same chain runs on every push (`.github/workflows/tests.yml`).

During development the schema was also run end to end on synthetic trials with known true
effects:

- a RAS split-plot dose-response trial with model fitting;
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

- **Synthetic tests.** The tests prove that the definitions are implemented as written and
  that every check works. They cannot prove that the definitions suit a given organisation's
  data; that comes from mapping real exports and calibrating thresholds on historical trials.
- **Plan rules.** Rules R28, R30, R35 and R38 are checked only when the analysis plan is
  recorded in `planned_outcomes` / `covariate_roles` / `analysis_runs`.
- **Free-text parsing.** These rules read the model formula and error strata from text.
- **Deferred items.** These are listed in the schema header: RAS operations log, diet
  ingredient matrix and others.
