#!/usr/bin/env python3
"""Static + executable consistency check of data_model.yaml.

Static: allowed keys, primary keys exist and are required, enum defaults are members,
fk targets exist and are a primary key or covered by a unique constraint, constraint tables
and columns exist, rule ids unique and every cited R-id / table.column / p:param exists,
rules mention only enum values that exist.
Executable: the schema builds into SQLite (tools/build_db.py) and
tools/test_constraints.py proves that bad rows are rejected.
"""
import os
import re
import subprocess
import sys

import yaml

here = os.path.dirname(os.path.abspath(__file__))
path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
with open(path) as f:
    model = yaml.safe_load(f)

tables = model["tables"]
errors = []
TABLE_KEYS = {"description", "primary_key", "columns", "rules", "tier"}
COL_KEYS = {"type", "required", "default", "description", "fk", "values", "min", "max"}
TYPES = {"string", "float", "integer", "boolean", "date", "datetime", "enum"}

constraints = model.get("constraints", [])
cids = [c["id"] for c in constraints]
if len(cids) != len(set(cids)):
    errors.append("duplicate constraint ids")

uniques = {}  # table -> list of column tuples that are unique without a WHERE
for c in constraints:
    if c["type"] == "unique" and not c.get("where"):
        uniques.setdefault(c["table"], []).append(tuple(c["columns"]))

for tname, t in tables.items():
    extra = set(t) - TABLE_KEYS
    if extra:
        errors.append(f"{tname}: unknown table keys {sorted(extra)}")
    if t.get("tier", "core") not in ("core", "optional"):
        errors.append(f"{tname}: tier must be core or optional")
    cols = t.get("columns", {})
    pk = t.get("primary_key")
    pkl = pk if isinstance(pk, list) else [pk]
    for c in pkl:
        if c not in cols:
            errors.append(f"{tname}: primary key column {c} missing")
        elif not cols[c].get("required"):
            errors.append(f"{tname}.{c}: primary key column not required")
    for cname, spec in cols.items():
        bad = set(spec) - COL_KEYS
        if bad:
            errors.append(f"{tname}.{cname}: unknown keys {sorted(bad)}")
        if spec.get("type") not in TYPES:
            errors.append(f"{tname}.{cname}: unknown type {spec.get('type')}")
        if spec.get("type") == "enum":
            vals = spec.get("values")
            if not vals:
                errors.append(f"{tname}.{cname}: enum without values")
            elif "default" in spec and spec["default"] not in vals:
                errors.append(f"{tname}.{cname}: default {spec['default']!r} not in enum")
            if vals and len(vals) != len(set(vals)):
                errors.append(f"{tname}.{cname}: duplicate enum values")
        if "min" in spec and "max" in spec and spec["min"] > spec["max"]:
            errors.append(f"{tname}.{cname}: min > max")
        if ("min" in spec or "max" in spec) and spec.get("type") not in ("float", "integer"):
            errors.append(f"{tname}.{cname}: min/max on non-numeric column")
        fk = spec.get("fk")
        if fk:
            if not re.fullmatch(r"[a-z_0-9]+\.[a-z_0-9]+", fk):
                errors.append(f"{tname}.{cname}: malformed fk {fk!r}")
                continue
            ft, fc = fk.split(".")
            if ft not in tables or fc not in tables[ft].get("columns", {}):
                errors.append(f"{tname}.{cname}: fk {fk} does not exist")
                continue
            tpk = tables[ft]["primary_key"]
            if tpk != fc and (fc,) not in uniques.get(ft, []):
                errors.append(f"{tname}.{cname}: fk target {fk} is not a primary key")
            if tables[ft]["columns"][fc]["type"] != spec["type"]:
                errors.append(f"{tname}.{cname}: fk type differs from {fk}")

for c in constraints:
    t = c.get("table")
    if t not in tables:
        errors.append(f"{c['id']}: unknown table {t}")
        continue
    for col in c.get("columns", []):
        if re.fullmatch(r"[a-z_0-9]+", col) and col not in tables[t]["columns"]:
            errors.append(f"{c['id']}: column {col} not in {t}")
    if c["type"] == "fk":
        rt, rc = c["ref_table"], tuple(c["ref_columns"])
        if rt not in tables:
            errors.append(f"{c['id']}: unknown ref_table {rt}")
        else:
            for col in rc:
                if col not in tables[rt]["columns"]:
                    errors.append(f"{c['id']}: {rt}.{col} missing")
            tpk = tables[rt]["primary_key"]
            tpk = tuple(tpk) if isinstance(tpk, list) else (tpk,)
            if rc != tpk and rc not in uniques.get(rt, []):
                errors.append(f"{c['id']}: parent columns {rt}{list(rc)} lack a unique constraint")
        if len(c["columns"]) != len(c["ref_columns"]):
            errors.append(f"{c['id']}: column count mismatch")

# rule texts
rules = model.get("cross_table_rules", [])
ids = [r["id"] for r in rules]
dups = {i for i in ids if ids.count(i) > 1}
if dups:
    errors.append(f"duplicate rule ids: {sorted(dups)}")
for r in rules:
    if r["severity"] not in ("error", "warning", "info"):
        errors.append(f"{r['id']}: bad severity")

params = set(model.get("validation_params_defaults", {}))
texts = [(r["id"], r["rule"]) for r in rules]
texts += list(model.get("derived_metrics", {}).items())
texts += list(model.get("analysis_populations", {}).items())
for tname, t in tables.items():
    texts += [(f"{tname}.rules", r) for r in t.get("rules", [])]
    for cname, spec in t["columns"].items():
        if "description" in spec:
            texts.append((f"{tname}.{cname}.description", spec["description"]))

ref = re.compile(r"\b([a-z_]+)\.([a-z_0-9]+)\b")
rid_ref = re.compile(r"\bR(\d{2})\b")
for rid, text in texts:
    for tn, cn in ref.findall(text):
        if tn in tables and cn not in tables[tn]["columns"]:
            errors.append(f"{rid}: references missing column {tn}.{cn}")
    for n in rid_ref.findall(text):
        if f"R{n}" not in ids:
            errors.append(f"{rid}: cites unknown rule R{n}")
    for p in re.findall(r"\bp:([a-z_0-9]+)", text):
        if p not in params:
            errors.append(f"{rid}: unknown validation param p:{p}")
    for cid in re.findall(r"\bC(\d{2})\b", text):
        if f"C{cid}" not in cids:
            errors.append(f"{rid}: cites unknown constraint C{cid}")

# enum values named in rules as "column = value"
enum_vals, non_enum = {}, set()
for tname, t in tables.items():
    for cname, spec in t["columns"].items():
        if spec.get("type") == "enum":
            enum_vals.setdefault(cname, set()).update(spec["values"])
        else:
            non_enum.add(cname)   # same name used as free text elsewhere (e.g. analyte)
for cname in non_enum:
    enum_vals.pop(cname, None)
eq = re.compile(r"\b([a-z_]+)\s*(?:=|!=)\s*([a-z_0-9]+)\b")
for rid, text in texts:
    for col, val in eq.findall(text):
        if col in enum_vals and val not in enum_vals[col] and val not in ("null", "true", "false"):
            errors.append(f"{rid}: '{col} = {val}' is not a value of enum {col}")

if errors:
    print("\n".join(sorted(set(errors))))
    sys.exit(1)

n_cols = sum(len(t["columns"]) for t in tables.values())
n_opt = sum(1 for t in tables.values() if t.get("tier") == "optional")
print(f"static OK: {len(tables)} tables ({n_opt} optional), {n_cols} columns, "
      f"{len(rules)} rules, {len(constraints)} constraints")
for script in ("build_db.py", "test_constraints.py"):
    r = subprocess.run([sys.executable, os.path.join(here, script), path],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode:
        sys.exit(r.returncode)
