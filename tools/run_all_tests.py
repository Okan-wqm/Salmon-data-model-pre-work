#!/usr/bin/env python3
"""Run the whole proof chain in dependency order; stop at the first broken link.

  1 schema      tools/check_schema.py      static consistency of data_model.yaml
  2 database    tools/build_db.py          the schema builds into SQLite STRICT tables
  3 constraints tools/test_constraints.py  bad rows rejected, each by its own check; full coverage
  4 trial       tools/synthetic_trial.py   a realistic trial with known truth loads under all constraints
  5 metrics     tools/test_metrics.py      every derived metric matches the known truth
  6 rules       tools/test_validate.py     clean trial has no errors; every validator rule fires on its mutation
  7 thresholds  tools/test_calibrate.py    thresholds estimated from historical data recover known values
  8 coverage    (this file)                every derived metric, every validator rule and every constraint
                                           is exercised by a test -- no untested element

Each step needs the previous ones: a metric test on a schema that does not build proves nothing.
Usage: python3 tools/run_all_tests.py [data_model.yaml]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
YAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")

STEPS = [
    ("schema", ["check_schema.py", YAML, "--static-only"]),
    ("database", ["build_db.py", YAML]),
    ("constraints", ["test_constraints.py", YAML]),
    ("trial", ["synthetic_trial.py", YAML]),
    ("metrics", ["test_metrics.py", YAML]),
    ("rules", ["test_validate.py", YAML]),
    ("thresholds", ["test_calibrate.py", YAML]),
]


def run(script, *args):
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def coverage():
    """Every derived metric / validator rule / constraint must be exercised by a test.
    The test scripts write what they exercised to coverage_*.json (see each test)."""
    sys.path.insert(0, HERE)
    from build_db import all_constraints, load_model
    model = load_model(YAML)
    problems = []
    need = {
        "metrics": set(model["derived_metrics"]),
        "rules": {r["id"] for r in model["cross_table_rules"] if "validator" in r["enforced_by"]},
        "constraints": {c["id"] for c in all_constraints(model)},
    }
    for kind, want in need.items():
        path = os.path.join(HERE, ".coverage_%s.json" % kind)
        if not os.path.exists(path):
            problems.append("%s: no coverage record (%s)" % (kind, path))
            continue
        with open(path) as f:
            got = json.load(f)
        exercised = set(got.get("exercised", []))
        waived = got.get("waived", {})            # id -> reason, reported, never silent
        missing = want - exercised - set(waived)
        if missing:
            problems.append("%s not exercised by any test: %s" % (kind, ", ".join(sorted(missing))))
        for k, why in sorted(waived.items()):
            print("    waived %s %s: %s" % (kind, k, why))
    return problems


def main():
    for name, (script, *args) in STEPS:
        if not os.path.exists(os.path.join(HERE, script)):
            print("FAIL %-11s missing %s" % (name, script))
            sys.exit(1)
        code, out = run(script, *args)
        last = out.splitlines()[-1] if out else ""
        print("%s %-11s %s" % ("ok  " if code == 0 else "FAIL", name, last[:150]))
        if code:
            print(out[-3000:])
            sys.exit(1)
    problems = coverage()
    if problems:
        for p in problems:
            print("FAIL coverage   " + p)
        sys.exit(1)
    print("ok   coverage    every metric, validator rule and constraint is exercised by a test")


if __name__ == "__main__":
    main()
