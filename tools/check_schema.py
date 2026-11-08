#!/usr/bin/env python3
"""Static consistency check of data_model.yaml.

Checks: foreign keys point to existing table.column; primary key columns exist and
are required; enum columns have values; table.column names mentioned in rules and
derived metrics exist; rule ids are unique.
"""
import re
import sys

import yaml

path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
with open(path) as f:
    model = yaml.safe_load(f)

tables = model["tables"]
errors = []

for tname, t in tables.items():
    cols = t.get("columns", {})
    pk = t.get("primary_key")
    if pk is None:
        errors.append(f"{tname}: no primary_key")
    else:
        for c in pk if isinstance(pk, list) else [pk]:
            if c not in cols:
                errors.append(f"{tname}: primary key column {c} missing")
            elif not cols[c].get("required"):
                errors.append(f"{tname}.{c}: primary key column not required")
    for cname, spec in cols.items():
        if spec.get("type") == "enum" and not spec.get("values"):
            errors.append(f"{tname}.{cname}: enum without values")
        fk = spec.get("fk")
        if fk:
            ft, fc = fk.split(".")
            if ft not in tables or fc not in tables[ft].get("columns", {}):
                errors.append(f"{tname}.{cname}: fk {fk} does not exist")

ref = re.compile(r"\b([a-z_]+)\.([a-z_0-9]+)\b")
texts = [(r["id"], r["rule"]) for r in model.get("cross_table_rules", [])]
texts += list(model.get("derived_metrics", {}).items())
for tname, t in tables.items():
    texts += [(f"{tname}.rules", r) for r in t.get("rules", [])]
for rid, text in texts:
    for tn, cn in ref.findall(text):
        if tn in tables and cn not in tables[tn]["columns"]:
            errors.append(f"{rid}: references missing column {tn}.{cn}")

ids = [r["id"] for r in model.get("cross_table_rules", [])]
dups = {i for i in ids if ids.count(i) > 1}
if dups:
    errors.append(f"duplicate rule ids: {sorted(dups)}")

n_cols = sum(len(t.get("columns", {})) for t in tables.values())
if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"OK: {len(tables)} tables, {n_cols} columns, {len(ids)} rules")
