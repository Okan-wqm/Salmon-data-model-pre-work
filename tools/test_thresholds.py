#!/usr/bin/env python3
"""Threshold-shift experiment: every numeric validation_params_defaults value must be tested
at its boundary.

For each numeric default a copy of data_model.yaml is written to a temporary directory with
that one value multiplied by 10 and by 0.1 (a default of 0 is shifted to 5 instead, one run).
test_validate.py, test_metrics.py and test_calibrate.py are run against the copy, in parallel;
the first failing script stops the other two. A shift that no test notices means the threshold
is never tested on both sides of its boundary, and this script fails -- unless the parameter
is in WAIVED {param: concrete reason}, which is printed.

Integer-valued parameters are rounded after shifting:
  counts (min_detections, flatline_min_readings)  max(1, round(v x f)): 0 detections or a
      flat line of 0 readings is meaningless (4 x 0.1 -> 1, 30 x 0.1 -> 3)
  day windows (max_interp_days, max_slaughter_lag_days, retention_sample_window_days,
      camera_window_days)  round(v x f), 0 allowed: 0 days = same day only / no
      interpolation is a real setting, and max(1, ...) would leave a 1-day default unshifted
NULL defaults (uneaten_dm_frac) are not numeric and are not shifted; test_metrics.py tests
uneaten_dm_frac when set in validation_params.

The test scripts are copied with the YAML so that their coverage records (tools/.coverage_*.json)
are written into the temporary directory, never over those of the real run.

Usage: test_thresholds.py [data_model.yaml]      exit 1 if a shift goes unnoticed
"""
import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
YAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")
TESTS = ("test_validate.py", "test_metrics.py", "test_calibrate.py")
FACTORS = (("x10", 10.0), ("x0.1", 0.1))
COUNT_PARAMS = {"min_detections", "flatline_min_readings"}
DAY_PARAMS = {"max_interp_days", "max_slaughter_lag_days", "retention_sample_window_days", "camera_window_days"}
WAIVED = {}            # param -> concrete reason why a direction cannot be tested; intentionally empty
TIMEOUT_S = 600


def shifted(name, v, f):
    if v == 0:
        return 5
    x = v * f
    if name in COUNT_PARAMS:
        return max(1, int(round(x)))
    if name in DAY_PARAMS:
        return int(round(x))
    return round(x, 10)


def write_copy(text, name, value, root):
    """YAML text with validation_params_defaults.<name> replaced (line edit keeps comments);
    name None = unchanged copy (baseline)."""
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("validation_params_defaults:"))
    pat = re.compile(r"^(\s+%s:\s*)([^#\s][^#]*?)(\s*(#.*)?)$" % re.escape(name or ""))
    for i in range(start + 1, len(lines)):
        if name is None:
            break
        if lines[i] and not lines[i][0].isspace() and not lines[i].startswith("#"):
            break
        m = pat.match(lines[i])
        if m:
            lines[i] = m.group(1) + repr(value) + m.group(3)
            break
    else:
        if name is not None:
            raise SystemExit("parameter %s not found in validation_params_defaults" % name)
    tools = os.path.join(root, "tools")
    os.makedirs(tools)
    for f in os.listdir(HERE):
        if f.endswith(".py"):
            shutil.copy(os.path.join(HERE, f), tools)
    path = os.path.join(root, "data_model.yaml")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    with open(path) as fh:     # the edit must change exactly this value
        got = yaml.safe_load(fh)["validation_params_defaults"].get(name)
    if name is not None and got != value:
        raise SystemExit("YAML copy for %s holds %r, wanted %r" % (name, got, value))
    return path


def run_tests(root, yaml_path):
    """Run the three test scripts in parallel; the first failure terminates the others.
    Returns (failing script, first FAIL line) or None when all pass."""
    procs = {}

    def one(script):
        p = subprocess.Popen([sys.executable, os.path.join(root, "tools", script), yaml_path], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs[script] = p
        out, _ = p.communicate(timeout=TIMEOUT_S)
        return script, p.returncode, out

    caught = None
    with cf.ThreadPoolExecutor(max_workers=len(TESTS)) as ex:
        pending = {ex.submit(one, s) for s in TESTS}
        while pending:
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fu in done:
                script, rc, out = fu.result()
                if rc != 0 and rc > 0 and caught is None:
                    lines = [ln.strip() for ln in out.splitlines()]
                    first = next((ln for ln in lines if ln.startswith("FAIL")), None) \
                        or next((ln for ln in lines if "Error" in ln or "error" in ln), lines[-1] if lines else "")
                    caught = (script, first)
                    for p in procs.values():
                        if p.poll() is None:
                            p.terminate()
    return caught


def main():
    t0 = time.time()
    with open(YAML) as fh:
        text = fh.read()
    defaults = yaml.safe_load(text)["validation_params_defaults"]
    with tempfile.TemporaryDirectory() as tmp:
        base = run_tests(os.path.join(tmp, "base"), write_copy(text, None, None, os.path.join(tmp, "base")))
        if base:
            print("baseline (unshifted YAML) already fails in %s: %s" % base)
            print("FAILED: baseline must pass before shifts mean anything")
            return 1
        survived, n_shift = [], 0
        for name, v in defaults.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                print("%-30s %-8s not numeric (null): not shifted; test_metrics sets it via validation_params" % (name, v))
                continue
            cells = []
            shifts = [("->5", 5)] if v == 0 else [(lab, shifted(name, v, f)) for lab, f in FACTORS]
            for lab, nv in shifts:
                n_shift += 1
                # the label can hold characters that are not valid in a directory name on
                # Windows ('->5' for parameters whose default is 0), so it is not used verbatim
                root = os.path.join(tmp, "%s_%s" % (name, re.sub(r"[^A-Za-z0-9_.]", "_", lab)))
                caught = run_tests(root, write_copy(text, name, nv, root))
                shutil.rmtree(root, ignore_errors=True)
                if caught:
                    cells.append("%s=%-7g caught by %s [%s]" % (lab, nv, caught[0].replace(".py", ""), caught[1][:70]))
                else:
                    cells.append("%s=%-7g SURVIVED" % (lab, nv))
                    survived.append((name, lab, nv))
            mark = "ok  " if not any(s[0] == name for s in survived) else ("waiv" if name in WAIVED else "FAIL")
            print("%s %-30s %-8g %s" % (mark, name, v, " | ".join(cells)), flush=True)
    for name, why in sorted(WAIVED.items()):
        print("waived %s: %s" % (name, why))
    bad = [s for s in survived if s[0] not in WAIVED]
    if bad:
        print("FAILED: %d of %d threshold shifts unnoticed by any test: %s (%.0f s)" % (
            len(bad), n_shift, ", ".join("%s %s" % (a, b) for a, b, _ in bad), time.time() - t0))
        return 1
    print("%d/%d threshold shifts detected (%d parameters, %d waived) in %.0f s" % (
        n_shift - len(survived), n_shift, sum(1 for v in defaults.values() if isinstance(v, (int, float))),
        len(WAIVED), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
