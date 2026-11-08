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

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
from build_db import all_constraints, load_model  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
STATIC_ONLY = "--static-only" in sys.argv
path = args[0] if args else "data_model.yaml"
try:
    model = load_model(path)          # rejects duplicate keys (safe_load keeps the last silently)
except ValueError as e:
    print(f"YAML: {e}")
    sys.exit(1)

tables = model["tables"]
errors = []
TABLE_KEYS = {"description", "primary_key", "columns", "rules", "tier"}
COL_KEYS = {"type", "required", "default", "description", "fk", "values", "min", "max"}
TYPES = {"string", "float", "integer", "boolean", "date", "datetime", "enum"}

constraints = all_constraints(model)
cids = [c["id"] for c in constraints]
if len(cids) != len(set(cids)):
    errors.append("duplicate constraint ids")

OCCASIONS = {"stocking", "d0", "final", "end_early"}   # + mid1..mid99 (constraint C54)
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
    for kind, a, b in re.findall(r"\b([CR])(\d{2})-[CR]?(\d{2})\b", text):
        pool = cids if kind == "C" else ids
        for n in range(int(a), int(b) + 1):
            if f"{kind}{n:02d}" not in pool:
                errors.append(f"{rid}: range cites unknown id {kind}{n:02d}")
    for cid in re.findall(r"\bC(\d{2})\b", text):
        if f"C{cid}" not in cids:
            errors.append(f"{rid}: cites unknown constraint C{cid}")

# quoted literals in constraint expressions must be values of an enum column of that table
# (or a known non-enum literal such as a GLOB pattern or empty string)
for c in constraints:
    txt = (c.get("expr") or "") + " " + (c.get("where") or "")
    cols = tables[c["table"]]["columns"].values()
    enums = {v for s in cols if s.get("type") == "enum" for v in s["values"]}
    # controlled vocabularies of string columns are listed in their description ('a | b | c')
    vocab = {w.strip() for s in cols if s.get("type") == "string" and "|" in s.get("description", "")
             for w in re.findall(r"[a-z0-9_]+", s["description"].split(":")[-1])}
    for lit in re.findall(r"'([^']*)'", txt):
        if lit == "" or any(ch in lit for ch in "[]*?:%") or lit in enums or lit in vocab or lit in OCCASIONS:
            continue
        errors.append(f"{c['id']}: literal '{lit}' is not an enum value of {c['table']}")
for c in constraints:
    if '"' in (c.get("expr") or "") + (c.get("where") or ""):
        errors.append(f"{c['id']}: double quote in expression (SQLite would read a misspelt column as a string)")

# rules: enforced_by
for r in rules:
    for e in r.get("enforced_by", []) or []:
        if e not in ("validator", "plan") and e not in cids:
            errors.append(f"{r['id']}: enforced_by {e} is not a constraint id, validator or plan")
    if "enforced_by" not in r:
        errors.append(f"{r['id']}: enforced_by missing")

# event_types: single source must agree with the events enums
ev = tables.get("events", {}).get("columns", {})
et = model.get("event_types") or {}
for t, v in et.items():
    if t not in ev.get("event_type", {}).get("values", []):
        errors.append(f"event_types.{t}: not an events.event_type value")
    if v.get("delta") not in (-1, 1):
        errors.append(f"event_types.{t}: delta must be -1 or 1")
    for oc in v.get("outcome_classes", []):
        if oc not in ev.get("outcome_class", {}).get("values", []):
            errors.append(f"event_types.{t}: outcome class {oc} not an events.outcome_class value")
roles = model.get("outcome_class_roles") or {}
if set(roles) != set(ev.get("outcome_class", {}).get("values", [])):
    errors.append("outcome_class_roles must list exactly the events.outcome_class values")
for k, v in roles.items():
    if v not in ("event", "censored", "addition"):
        errors.append(f"outcome_class_roles.{k}: role {v} unknown")

# bare identifiers in metric / population / rule texts must name something that exists
known = set(tables) | set(model.get("derived_metrics", {})) | set(params) | set(et) | set(roles)
known |= set(model.get("analysis_populations", {})) | set(model)
known |= OCCASIONS
for t in tables.values():
    for cn, sp in t["columns"].items():
        known.add(cn)
        known |= set(sp.get("values", []) or [])
ALLOW = {"temp_range_ok", "w_source", "n_count", "b_count", "n_event", "n_censored", "n_added",
         "n_eligible", "n_observed", "n_imputed", "coverage_pct", "valid_days", "eligible_days",
         "fed_dm", "uneaten_dm", "intake_day", "c_dm", "w_start", "w_end", "b_start", "b_end",
         "c_start", "c_end", "c_i", "t_day", "m_diet", "m_feces", "n_diet", "n_feces", "end_date",
         "start_date", "d0_date", "unit_day", "event_biomass_kg_i", "has_d0", "has_final",
         "follow_up_days", "n_occasions", "polar_day", "polar_night", "biomass_gain_estimated_share",
         "fcr_gross_qc", "split_estimate", "per_type", "role", "day_n", "n_sites", "n_levels",
         "delta_i", "whole_body", "placeholder_from_unfed_batches", "nutrient_composition_missing"}
formula_texts = [(r["id"], r["rule"]) for r in rules]
formula_texts += list(model.get("derived_metrics", {}).items())
formula_texts += list(model.get("analysis_populations", {}).items())
for rid, text in formula_texts:
    for tok in re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", text):
        if tok not in known and tok.lower() not in ALLOW:
            errors.append(f"{rid}: unknown identifier '{tok}'")

# event-type / outcome-class lists in metric and rule texts
all_enum_values = {v for t in tables.values() for s in t["columns"].values() if s.get("type") == "enum" for v in s["values"]}
for rid, text in texts:
    for m in re.finditer(r"(?:event_type|outcome_class)\s+(?:in|of)\s+\(([a-z_, ]+)\)", text):
        for v in re.split(r",\s*", m.group(1).strip()):
            if v and v not in all_enum_values and v not in ("transfer", "death"):
                errors.append(f"{rid}: '{v}' is not an enum value")
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
        if col in enum_vals and val not in enum_vals[col] and val not in ("null", "true", "false") \
                and not (col == "role" and val in (model.get("outcome_class_roles") or {}).values()):
            errors.append(f"{rid}: '{col} = {val}' is not a value of enum {col}")

if errors:
    print("\n".join(sorted(set(errors))))
    sys.exit(1)

n_cols = sum(len(t["columns"]) for t in tables.values())
n_opt = sum(1 for t in tables.values() if t.get("tier") == "optional")
n_db = sum(1 for r in rules if r.get("enforced_by") and all(e not in ("validator", "plan") for e in r["enforced_by"]))
print(f"static OK: {len(tables)} tables ({n_opt} optional), {n_cols} columns, "
      f"{len(rules)} rules ({n_db} fully enforced by the database), {len(constraints)} constraints")
if STATIC_ONLY:
    sys.exit(0)
for script in ("build_db.py", "test_constraints.py"):
    r = subprocess.run([sys.executable, os.path.join(here, script), path],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode:
        sys.exit(r.returncode)
