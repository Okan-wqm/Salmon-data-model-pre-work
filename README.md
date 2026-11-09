# Salmon feed trial data model

A data model for Atlantic salmon feed trials run in tanks, RAS and sea cages under
different climate conditions, built for statistical analysis. It stores the measurements and,
alongside them, what is needed to know which conclusions the data can support. It comes with
tools that build it into a database, compute the metrics, check the rules and test all of it.

The model is a starting point. Column vocabularies and importers have to be fitted to an
organisation's own systems before it is used on real data (see [Adapting it](#adapting-it)).

Requirements: Python 3.12 and `pyyaml`; SQLite is built in. No R, no database server.

## Quick start

```bash
pip install pyyaml
python3 tools/run_all_tests.py data_model.yaml        # the whole proof chain, about 3.5 min
python3 tools/validate.py data_model.yaml trial.db    # rule findings by severity; exit 1 on error
python3 tools/metrics.py  data_model.yaml trial.db    # one analysis row per tank or cage
```

Without a database file, `validate.py` and `metrics.py` build the synthetic trial in memory.

## What the model settles before any statistics

| Question | Answer in the model |
|---|---|
| What is the experimental unit? | The tank or cage (`units`), never the fish. Fish-level data are nested in units. |
| At what level was each factor applied? | `design_factors.applied_at` = unit, system or site. A temperature set per RAS loop is a whole-plot factor; rule R35 requires it to be tested against systems, not tanks. |
| Was "climate" assigned or only observed? | `design_factors.role` separates an assigned treatment from an observed condition. With one site per climate level the climate effect cannot be separated from the site effect; rule R02 reports this as an error when the factor is a term of a planned model, otherwise as a warning. |
| When does a period start and end? | One half-open window per period. Feed, degree-days and count-changing events on a weighing day belong to the next period (fish are fasted before weighing); an event that happened before the weighing is marked as such. |
| How are missing days handled? | One `period_sum` rule for every period total: physically defensible inputs are filled first (temperature from the system sensor or short interpolation, uneaten feed from the unit's own ratio), the total is rescaled by coverage, and the number of observed and imputed days is always reported. Below a coverage threshold the value is NULL; a NULL is never read as zero. |
| How are fish counts kept? | Count-changing event types are defined once (`event_types`) with their direction and survival role; the running count is reconciled at every recount with a tolerance (R04). |
| Which analysis population? | ITT = all randomised units; mITT and per-protocol are defined; pending exclusions stay in; exclusions are logged with who decided them and when. Raw data are never altered: removing a record requires a logged decision (R15). |
| Which covariates are allowed? | `covariate_roles` marks post-randomisation variables (oxygen, density, lice) as mediators; adjusting for them under a total-effect estimand is an error (R28). |
| How is each metric computed? | `derived_metrics` gives one definition each, with units and windows: SGR, TGC and degree-days, gross / apparent / biological FCR, intake corrected for uneaten feed (Helland et al. 1996), digestibility with an inert marker, nutrient retention, survival, organ indices, lice per fish, density, unionised ammonia and daylength (including polar day and night). |

## What it records

- **Design.** Factors and levels, randomisation runs, blocks, factorial diets, cohorts, blinding and unblinding time, a versioned analysis plan with pre-registered outcomes (error strata, estimand, multiplicity, equivalence margin, power inputs).
- **Units and time.** Sampling occasions with a numeric order and one date each, tank or cage geometry over time, moves between systems, individually tagged fish.
- **Growth.** Weighings by type (stocking, bulk in several lots, individual, sample, harvest) with weight basis, and biomass cameras with calibration against manual samples and algorithm-version tracking. The weight of a unit comes from a count, else a calibrated camera, else a sample; mixing sources across diets is flagged (R40).
- **Feed.** Daily feed per unit and batch, batches with composition drift, placeholder batches when a feeding system knows the feed type but not the lot, feed split across cages from a barge, off-protocol feed, uneaten feed and recovery tests, pellet quality.
- **Environment.** Sensor series with a fallback chain, setpoint schedules, light regimes, water samples with detection limits, site latitude for daylength.
- **Events and health.** Mortality, culls, sampling, escapes, transfers, harvest, lice treatments, blooms and storms, with cause categories for competing risks; lice counts in the Norwegian reporting convention.
- **Composition and digestibility.** Open analyte list with units and basis (wet, dry matter, as fed), censoring, lab run and report revision, feces collection method.
- **Terminal quality and welfare.** Organ weights and yields, fillet colour and texture, ordinal scores on defined scales with rater identity, tissue samples and assay results.
- **Provenance.** Import batches with file hashes and source clocks, superseding re-exports, an audit log, analysis runs that make a reported number reproducible, ethics permits.

Tables are tiered: 28 core tables and the error-severity rules are the minimum for valid
inference; 19 optional tables can stay empty in a first trial.

## Analyses it supports

- Diet effects with linear mixed models, the tank or cage as unit and initial weight as covariate.
- Diet × temperature split-plot models with system as the whole-plot stratum.
- Growth curves as repeated measures on a common time axis, with time-varying temperature and daylength.
- Dose-response on analysed rather than nominal dose.
- Survival with binomial GLMM, Cox models with unit frailty or Kaplan–Meier; deaths and moribund culls are separated from censoring through event classes.
- Ordinal welfare and quality scores with cumulative link models.
- Multi-site and multi-trial pooling through site and climate covariates, cross-trial formulation IDs and pre-registered outcomes.

The schema also states what not to do: SGR is not compared across temperatures, TGC is a
descriptor rather than an inferential quantity, and ratios such as FCR are modelled as feed
and gain rather than analysed as ratios.

## Tools

| Tool | What it does |
|---|---|
| `tools/build_db.py` | Builds the schema into SQLite STRICT tables: types, vocabularies, ranges, date formats, composite keys, rule constraints, and the event constraints generated from `event_types` |
| `tools/check_schema.py` | Checks the schema file itself: every column, rule, constraint and threshold named in the text exists |
| `tools/metrics.py` | Reference implementation of all 38 metric definitions, one function per key, and the one-row-per-unit analysis dataset |
| `tools/validate.py` | Rule engine for the 39 rules that need more than a database constraint; reports findings by severity and never writes to the data |
| `tools/calibrate_params.py` | Estimates six rule thresholds (initial CV and mean difference, count tolerance, negative growth, minimum recovery, temperature jump) and the power inputs (within- and between-unit CV, ICC) from historical trials; thresholds that are policy decisions are left as configured |
| `tools/synthetic_trial.py` | A RAS split-plot trial (2 temperatures × 2 diets, 8 tanks, 84 days) with known true values, used by the tests |
| `tools/run_all_tests.py` | Runs the proof chain below in dependency order |

All tools take `[data_model.yaml] [database]` on the command line.

## Proof chain

`tools/run_all_tests.py` runs nine steps and stops at the first failure. The same chain runs
in CI on every push and pull request.

1. **Schema.** Every name used in a rule, metric or threshold exists.
2. **Database.** The schema builds.
3. **Constraints.** 92 invalid rows are rejected. Each is also loaded into a database rebuilt without its own target check and must then be accepted, which proves the rejection comes from that check. Every constraint has a probe.
4. **Synthetic trial.** 3,699 rows load under all constraints.
5. **Metrics.** All 38 metrics are compared with the trial's known true values or with hand calculations (297 checks). The tests require biological FCR, intake and protein intake within 2 % of the truth, biomass gain within 0.5 % and survival exactly; the current run agrees within 0.01 %.
6. **Rules.** The clean trial produces no errors. Each of the 39 validator rules fires, alone and with its declared severity, on a change made to a copy of the trial (56 changes, including ones just outside a threshold that must fire and ones just inside that must stay silent).
7. **Thresholds.** The calibration recovers the variation built into the data, detects an injected 3 % recount error and withholds estimates on too few observations.
8. **Boundaries.** Every numeric threshold is shifted ×10 and ×0.1 in a copy of the schema; a test must notice each shift (38 shifts over 20 parameters).
9. **Coverage.** The chain fails if any metric, validator rule or constraint is not exercised by a test.

## Adapting it

- **Source mapping.** Map the farm production system, the feeding system, biomass cameras and the lab system to the tables: site and unit codes, feed article to batch, analyte names, units and reporting basis. No importer is included; the shape of each organisation's exports decides it.
- **Clocks.** Record each source's clock in `import_batches.source_clock`; feeding systems that run on fixed CET and daylight-saving changes otherwise shift daily feed to the wrong day.
- **Thresholds.** The defaults in `validation_params_defaults` are placeholders. Load historical trials and run `tools/calibrate_params.py` to replace the six estimable ones; set the policy thresholds (coverage, minimum gain) in the plan.
- **Rules and metrics.** Extend `tools/validate.py` and `tools/metrics.py` where the organisation's own rules or definitions differ; the coverage step forces a test for every addition.

## Limitations

- The tests prove that the definitions are implemented as written and that every check works. They cannot prove that the definitions suit a given organisation's data; that comes only from real exports and calibration on historical trials.
- Rules R28, R30, R35 and R38 depend on the analysis plan being recorded (`planned_outcomes`, `covariate_roles`, `analysis_runs`); when it is not, the validator reports them as not checked rather than passing them.
- Model formulas and error strata in the plan are free text and are parsed conservatively.
- The synthetic trial is a tank trial. Sea-cage features (camera weights, split feed, estimated escapes, interference) are covered by small hand-built test cases, not by an end-to-end cage trial.
- Deferred, not modelled: RAS operations log, diet ingredient matrix, maturation census, neighbour-farm exposure, ontology mapping.
