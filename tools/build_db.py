#!/usr/bin/env python3
"""Build a SQLite database from data_model.yaml.

Column rules (type, required, default, enum values, min/max, single-column fk) and the
`constraints` section (unique / partial unique / check / composite fk) become real DDL, so a
row that breaks the model is rejected by the database instead of being described in prose.

Usage: build_db.py [data_model.yaml] [out.db]     (no out.db -> in-memory, prints DDL size)
"""
import sqlite3
import sys

import yaml

TYPE_MAP = {
    "string": "TEXT", "date": "TEXT", "datetime": "TEXT", "enum": "TEXT",
    "float": "REAL", "integer": "INTEGER", "boolean": "INTEGER",
}


def q(name):
    return '"%s"' % name


def sql_literal(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def column_ddl(cname, spec):
    parts = [q(cname), TYPE_MAP[spec["type"]]]
    if spec.get("required"):
        parts.append("NOT NULL")
    if "default" in spec:
        parts.append("DEFAULT " + sql_literal(spec["default"]))
    if spec["type"] == "enum":
        vals = ", ".join(sql_literal(v) for v in spec["values"])
        parts.append("CHECK (%s IN (%s))" % (q(cname), vals))
    if spec["type"] == "boolean":
        parts.append("CHECK (%s IN (0, 1))" % q(cname))
    if "min" in spec:
        parts.append("CHECK (%s >= %s)" % (q(cname), spec["min"]))
    if "max" in spec:
        parts.append("CHECK (%s <= %s)" % (q(cname), spec["max"]))
    return " ".join(parts)


def build_ddl(model):
    tables = model["tables"]
    cons = model.get("constraints", [])
    stmts, post = [], []
    for tname, t in tables.items():
        lines = [column_ddl(c, s) for c, s in t["columns"].items()]
        pk = t["primary_key"]
        pk = pk if isinstance(pk, list) else [pk]
        lines.append("PRIMARY KEY (%s)" % ", ".join(q(c) for c in pk))
        for c, s in t["columns"].items():
            if s.get("fk"):
                ft, fc = s["fk"].split(".")
                lines.append("FOREIGN KEY (%s) REFERENCES %s (%s)" % (q(c), q(ft), q(fc)))
        for c in cons:
            if c["table"] != tname:
                continue
            if c["type"] == "check":
                lines.append("CONSTRAINT %s CHECK (%s)" % (c["id"], c["expr"]))
            elif c["type"] == "fk":
                lines.append("CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s)" % (
                    c["id"], ", ".join(q(x) for x in c["columns"]), q(c["ref_table"]),
                    ", ".join(q(x) for x in c["ref_columns"])))
            elif c["type"] == "unique":
                cols = ", ".join(c["columns"])
                where = " WHERE " + c["where"] if c.get("where") else ""
                post.append("CREATE UNIQUE INDEX %s ON %s (%s)%s" % (
                    "ux_" + c["id"].lower(), q(tname), cols, where))
        stmts.append("CREATE TABLE %s (\n  %s\n)" % (q(tname), ",\n  ".join(lines)))
    return stmts + post


def build(path="data_model.yaml", db=":memory:"):
    with open(path) as f:
        model = yaml.safe_load(f)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = ON")
    for stmt in build_ddl(model):
        con.execute(stmt)
    return con, model


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data_model.yaml"
    db = sys.argv[2] if len(sys.argv) > 2 else ":memory:"
    con, model = build(path, db)
    n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    print("built %d tables, %d constraints" % (n, len(model.get("constraints", []))))
