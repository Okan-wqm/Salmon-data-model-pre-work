#!/usr/bin/env python3
"""Build a SQLite database from data_model.yaml.

Column rules (type, required, default, enum values, min/max, single-column fk), the
`constraints` section (unique / partial unique / check / composite fk) and the constraints
generated from `event_types` become real DDL, so a row that breaks the model is rejected by
the database instead of being described in prose.

Tables are STRICT (no text in numeric columns), text columns reject empty strings, dates must
be real ISO dates (YYYY-MM-DD) and timestamps ISO UTC (YYYY-MM-DDTHH:MM:SSZ).

Usage: build_db.py [data_model.yaml] [out.db]     (no out.db -> in-memory)
"""
import sqlite3
import sys

import yaml

TYPE_MAP = {
    "string": "TEXT", "date": "TEXT", "datetime": "TEXT", "enum": "TEXT",
    "float": "REAL", "integer": "INTEGER", "boolean": "INTEGER",
}
TS_GLOB = "[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z"


class UniqueKeyLoader(yaml.SafeLoader):
    """safe_load silently keeps the last of two equal keys; refuse instead."""


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError("duplicate key %r at line %d" % (key, key_node.start_mark.line + 1))
        seen.add(key)
    return loader.construct_mapping(node, deep)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load_model(path):
    with open(path) as f:
        return yaml.load(f, Loader=UniqueKeyLoader)


def q(name):
    return '"%s"' % name


def lit(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def in_list(values):
    return "(" + ", ".join(lit(v) for v in values) + ")"


def generated_constraints(model):
    """Constraints derived from event_types (single source of the count-changing list)."""
    et = model.get("event_types") or {}
    if not et:
        return []
    count = in_list(et)
    per_type = " OR ".join(
        "(event_type = %s AND outcome_class IN %s)" % (lit(t), in_list(v["outcome_classes"]))
        for t, v in et.items())
    return [
        {"id": "C55", "table": "events", "type": "check", "generated": True,
         "expr": "event_type NOT IN %s OR n_fish IS NOT NULL" % count},
        {"id": "C56", "table": "events", "type": "check", "generated": True,
         "expr": "event_type NOT IN %s OR n_fish = 0 OR outcome_class IS NOT NULL" % count},
        {"id": "C57", "table": "events", "type": "check", "generated": True,
         "expr": ("event_type NOT IN %s"
                  " OR (n_fish = 0 AND COALESCE(weight_source,'not_applicable') = 'not_applicable'"
                  " AND COALESCE(biomass_kg,0) = 0)"
                  " OR (n_fish > 0 AND COALESCE(weight_source,'') <> 'not_applicable'"
                  " AND (biomass_kg IS NOT NULL OR COALESCE(weight_source,'') GLOB 'estimated_*'))") % count},
        {"id": "C83", "table": "events", "type": "check", "generated": True,
         "expr": "outcome_class IS NULL OR " + per_type},
        {"id": "C84", "table": "events", "type": "check", "generated": True,
         "expr": ("event_type IN %s OR (n_fish IS NULL AND biomass_kg IS NULL"
                  " AND weight_source IS NULL AND outcome_class IS NULL)") % count},
    ]


def all_constraints(model):
    return list(model.get("constraints", [])) + generated_constraints(model)


def column_ddl(cname, spec, tname="", drop=frozenset()):
    """drop: set of switches used by the tests to prove that a rejection is caused by one
    specific check: 'col:<table>.<column>' (all column CHECKs), 'required:<table>.<column>',
    'fk:<table>.<column>', 'strict', constraint ids, 'fk_enforcement'."""
    c = q(cname)
    key = "%s.%s" % (tname, cname)
    parts = [c, TYPE_MAP[spec["type"]]]
    if spec.get("required") and "required:" + key not in drop:
        parts.append("NOT NULL")
    if "default" in spec:
        parts.append("DEFAULT " + lit(spec["default"]))
    if "col:" + key in drop:
        return " ".join(parts)
    t = spec["type"]
    if t == "enum":
        parts.append("CHECK (%s IN %s)" % (c, in_list(spec["values"])))
    elif t == "boolean":
        parts.append("CHECK (%s IN (0, 1))" % c)
    elif t == "string":
        parts.append("CHECK (%s IS NULL OR trim(%s) <> '')" % (c, c))
    elif t == "date":
        parts.append("CHECK (%s IS NULL OR date(%s) IS %s)" % (c, c, c))
    elif t == "datetime":
        parts.append("CHECK (%s IS NULL OR (%s GLOB '%s' AND datetime(%s) IS NOT NULL))" % (c, c, TS_GLOB, c))
    if "min" in spec:
        parts.append("CHECK (%s >= %s)" % (c, spec["min"]))
    if "max" in spec:
        parts.append("CHECK (%s <= %s)" % (c, spec["max"]))
    return " ".join(parts)


def build_ddl(model, drop=frozenset()):
    tables = model["tables"]
    cons = [c for c in all_constraints(model) if c["id"] not in drop]
    stmts, post = [], []
    for tname, t in tables.items():
        lines = [column_ddl(c, s, tname, drop) for c, s in t["columns"].items()]
        pk = t["primary_key"]
        pk = pk if isinstance(pk, list) else [pk]
        lines.append("PRIMARY KEY (%s)" % ", ".join(q(c) for c in pk))
        for c, s in t["columns"].items():
            if s.get("fk") and "fk:%s.%s" % (tname, c) not in drop:
                ft, fc = s["fk"].split(".")
                lines.append("FOREIGN KEY (%s) REFERENCES %s (%s)" % (q(c), q(ft), q(fc)))
        for c in cons:
            if c["table"] != tname:
                continue
            if c["type"] == "check":
                lines.append("CONSTRAINT %s CHECK (%s)" % (c["id"], c["expr"]))
            elif c["type"] == "fk":
                on_update = " ON UPDATE CASCADE" if c.get("on_update") == "cascade" else ""
                lines.append("CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s)%s" % (
                    c["id"], ", ".join(q(x) for x in c["columns"]), q(c["ref_table"]),
                    ", ".join(q(x) for x in c["ref_columns"]), on_update))
            elif c["type"] == "unique":
                where = " WHERE " + c["where"] if c.get("where") else ""
                post.append("CREATE UNIQUE INDEX %s ON %s (%s)%s" % (
                    "ux_" + c["id"].lower(), q(tname), ", ".join(c["columns"]), where))
        strict = "" if "strict" in drop else " STRICT"
        stmts.append("CREATE TABLE %s (\n  %s\n)%s" % (q(tname), ",\n  ".join(lines), strict))
    return stmts + post


def build(path="data_model.yaml", db=":memory:", drop=frozenset(), model=None):
    model = model or load_model(path)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = %s" % ("OFF" if "fk_enforcement" in drop else "ON"))
    for stmt in build_ddl(model, drop):
        con.execute(stmt)
    return con, model


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
    db = sys.argv[2] if len(sys.argv) > 2 else ":memory:"
    con, model = build(path, db)
    n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    print("built %d STRICT tables, %d constraints (%d generated from event_types)" % (
        n, len(all_constraints(model)), len(generated_constraints(model))))
