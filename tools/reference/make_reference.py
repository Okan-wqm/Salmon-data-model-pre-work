#!/usr/bin/env python3
"""Reference values for tools/test_analysis.py from two independent implementations.

  1. python3 tools/reference/make_reference.py export
        writes unit_responses_seed7.csv: the planned outcomes of the synthetic trial (seed 7) on
        their analysis scale, one row per unit
  2. Rscript tools/reference/make_reference.R            (base R only)
        aov(y ~ diet*temperature + Error(system)) per outcome -> aov_reference.json
  3. python3 tools/reference/make_reference.py merge     (needs pandas and statsmodels)
        REML mixed model per outcome (statsmodels MixedLM, random intercept per system, Powell
        optimiser: the default L-BFGS stops with a singular matrix on these 8-unit data sets) and
        the aov values -> analysis_reference.json
  4. Rscript tools/reference/make_reference_lmer.R       (needs lme4, lmerTest, pbkrtest)
        lmer(y ~ diet*temperature + (1 | system)) per outcome with Satterthwaite and Kenward-Roger
        tests of the diet, temperature and interaction contrasts -> lmer_reference.json

The tests only read analysis_reference.json and lmer_reference.json; R and statsmodels are
needed to remake them, not to run the tests. Remake it whenever the synthetic trial or the planned outcomes change: the test
compares the stored unit values with the current trial and fails with 'stale' otherwise.
The REML part also stores the p value of the default Wald z test of the diet effect, to show
what a mixed model without small-sample degrees of freedom would report.
"""
import csv
import json
import os
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
YAML = os.path.join(os.path.dirname(TOOLS), "data_model.yaml")
CSV = os.path.join(HERE, "unit_responses_seed7.csv")
SEED = 7


def export():
    import analysis as A
    from synthetic_trial import make_trial
    con, model, _ = make_trial(YAML, seed=SEED)
    res = A.run_all(con, model)
    labels = [lb for lb, r in res["outcomes"].items() if not r["degenerate"]]
    with open(CSV, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["unit", "system", "diet", "temperature"] + labels)
        for u in res["units"]:
            w.writerow([u["unit_id"], u["system_id"], u["diet_id"], u["whole_level"]]
                       + [repr(res["outcomes"][lb]["y"][u["unit_id"]]) for lb in labels])
    print("wrote %s: %d units, outcomes %s" % (CSV, len(res["units"]), ", ".join(labels)))


def merge():
    import numpy as np
    import pandas as pd
    import scipy
    import statsmodels
    import statsmodels.formula.api as smf
    from scipy import stats
    d = pd.read_csv(CSV)
    labels = [c for c in d.columns if c not in ("unit", "system", "diet", "temperature")]
    with open(os.path.join(HERE, "aov_reference.json")) as fh:
        aov = json.load(fh)
    reml = {}
    for lb in labels:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = smf.mixedlm("%s ~ C(diet, Treatment('D1'))*C(temperature, Treatment('12C'))" % lb, d,
                              groups=d["system"]).fit(reml=True, method="powell")
        names, b, V = list(fit.fe_params.index), fit.fe_params.values, fit.cov_params().iloc[:4, :4].values
        i_d = [i for i, n in enumerate(names) if "diet" in n and ":" not in n][0]
        i_t = [i for i, n in enumerate(names) if "temperature" in n and ":" not in n][0]
        i_x = [i for i, n in enumerate(names) if ":" in n][0]

        def lin(main, half_interaction):
            L = np.zeros(4)
            if main is not None:
                L[main] = 1.0
            L[i_x] = half_interaction
            return {"estimate": float(L @ b), "se": float(np.sqrt(L @ V @ L))}

        r = {"var_system": float(fit.cov_re.iloc[0, 0]), "var_residual": float(fit.scale), "converged": bool(fit.converged),
             "diet": lin(i_d, 0.5), "temperature": lin(i_t, 0.5), "interaction": lin(None, 1.0)}
        r["wald_z_p_diet"] = float(2 * stats.norm.sf(abs(r["diet"]["estimate"] / r["diet"]["se"])))
        reml[lb] = r
    out = {"seed": SEED, "made_with": dict(aov["made_with"], statsmodels=statsmodels.__version__, scipy=scipy.__version__),
           "inputs": {c: d[c].tolist() for c in d.columns}, "aov": aov["aov"], "reml": reml}
    with open(os.path.join(HERE, "analysis_reference.json"), "w", newline="\n") as fh:
        json.dump(out, fh, indent=1)
    print("wrote analysis_reference.json: aov (R %s) and REML (statsmodels %s) for %s" % (
        aov["made_with"]["R"], statsmodels.__version__, ", ".join(labels)))


if __name__ == "__main__":
    {"export": export, "merge": merge}[sys.argv[1] if len(sys.argv) > 1 else "export"]()
