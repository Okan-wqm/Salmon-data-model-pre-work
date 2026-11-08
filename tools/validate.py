#!/usr/bin/env python3
"""Rule engine for data_model.yaml: every cross_table_rules entry whose enforced_by names
'validator' is a function here that returns findings.

A finding is a dict {rule, severity, unit_id, record, message}; record = 'table:record_id'
(or a natural key for tables without record_id) or None. Severity is the rule's severity in
the YAML, except where the rule text itself makes it conditional (R02, R03 pending, R04
listing, R14 fish_fate NULL, R26 several batches, R30 excluded rows) -- see each function.
Thresholds p:name come from validation_params_defaults, overridden per trial by the
validation_params table. The validator only REPORTS: it never writes to the database.

Rules that need a derived metric (R01 d0 count, R04, R05, R06, R40) take it from
tools/metrics.py; when that module cannot be imported they emit one info finding each.

Usage: validate.py [data_model.yaml] [trial.db]     (no db -> builds the synthetic trial)
Exit status 1 if any finding has severity error.
"""
import datetime as dt
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import metrics as M   # noqa: N812
    METRICS_ERROR = None
except Exception as e:     # pragma: no cover - depends on the parallel module
    M = None
    METRICS_ERROR = "%s: %s" % (type(e).__name__, e)

UTC = dt.timezone.utc
SEVERITIES = ("error", "warning", "info")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
LIGHT_VARS = {"light_lux", "par_umol_m2_s"}
SPATIAL_TOKENS = {"x_m", "y_m", "upstream_rank", "cage_group_id", "spatial", "corExp", "corGaus",
                  "corSpher", "exp_cov", "gp", "matern", "s", "te"}


# ---------------------------------------------------------------------------- helpers
def d(s):
    return dt.date.fromisoformat(s) if isinstance(s, str) else s


def iso(x):
    return x.isoformat()


def days(a, b):
    """dates in [a, b)"""
    x = a
    while x < b:
        yield x
        x += dt.timedelta(days=1)


def ranges(dates):
    """consecutive date runs -> [(first, last)]"""
    out = []
    for x in sorted(dates):
        if out and x - out[-1][1] == dt.timedelta(days=1):
            out[-1][1] = x
        else:
            out.append([x, x])
    return [(a, b) for a, b in out]


def fmt_ranges(rs, limit=6):
    s = ", ".join(iso(a) if a == b else "%s..%s" % (iso(a), iso(b)) for a, b in rs[:limit])
    return s + (" (+%d more)" % (len(rs) - limit) if len(rs) > limit else "")


def local_date(ts, tz):
    t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return t.astimezone(tz).date() if tz else t.date()


def tokens(text):
    return set(IDENT.findall(text or ""))


def rhs_terms(model_text):
    """Identifier tokens of the right-hand side of a model formula ('~'); the whole text when
    there is no '~'. Conservative: any identifier on the RHS counts as a term."""
    if not model_text:
        return set()
    rhs = model_text.split("~", 1)[1] if "~" in model_text else model_text
    return tokens(rhs)


def strata_tokens(text):
    """'system, system:unit' -> {'system', 'unit'}; splits on every non-identifier char."""
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or ""))


class Ctx:
    def __init__(self, con, model, trial_id):
        self.con, self.model, self.trial_id = con, model, trial_id
        self.sev = {r["id"]: r["severity"] for r in model.get("cross_table_rules", [])}
        self.findings = []
        self.p = dict(model.get("validation_params_defaults") or {})
        for r in self.rows("SELECT param, value FROM validation_params WHERE trial_id = ?", (trial_id,)):
            self.p[r["param"]] = r["value"]
        self.trial = self.rows("SELECT * FROM trials WHERE trial_id = ?", (trial_id,))[0]
        self.units = {r["unit_id"]: r for r in self.rows(
            "SELECT u.*, s.system_kind, s.site_id, s.target_temp_c, s.temp_control, s.reference_depth_m,"
            " st.timezone FROM units u JOIN systems s ON s.system_id = u.system_id"
            " JOIN sites st ON st.site_id = s.site_id WHERE u.trial_id = ? ORDER BY u.unit_id", (trial_id,))}
        self.systems = {r["system_id"]: r for r in self.rows(
            "SELECT s.*, st.timezone FROM systems s JOIN sites st ON st.site_id = s.site_id"
            " WHERE s.trial_id = ? ORDER BY s.system_id", (trial_id,))}
        self.diets = {r["diet_id"]: r for r in self.rows("SELECT * FROM diets WHERE trial_id = ?", (trial_id,))}
        self.occ = defaultdict(list)
        for r in self.rows("SELECT o.* FROM unit_occasions o JOIN units u ON u.unit_id = o.unit_id"
                           " WHERE u.trial_id = ? ORDER BY o.unit_id, o.seq", (trial_id,)):
            r["date"] = d(r["occasion_date"])
            self.occ[r["unit_id"]].append(r)
        self.factors = {r["factor_id"]: r for r in self.rows(
            "SELECT * FROM design_factors WHERE trial_id = ? ORDER BY factor_id", (trial_id,))}
        self.plans = [r["plan_id"] for r in self.rows("SELECT plan_id FROM analysis_plans WHERE trial_id = ?", (trial_id,))]
        self.outcomes = self.rows(
            "SELECT po.* FROM planned_outcomes po JOIN analysis_plans ap ON ap.plan_id = po.plan_id"
            " WHERE ap.trial_id = ? ORDER BY po.plan_id, po.outcome_label", (trial_id,))
        self._tz = {}

    # -- db
    def rows(self, sql, args=()):
        cur = self.con.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def val(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return r[0] if r else None

    UNITS_SQL = "(SELECT unit_id FROM units WHERE trial_id = ?)"
    SYSTEMS_SQL = "(SELECT system_id FROM systems WHERE trial_id = ?)"

    # -- findings
    def f(self, rule, message, unit_id=None, record=None, severity=None):
        self.findings.append({"rule": rule, "severity": severity or self.sev.get(rule, "error"),
                              "unit_id": unit_id, "record": record, "message": message})

    # -- trial helpers
    def tz(self, name):
        if name not in self._tz:
            try:
                self._tz[name] = ZoneInfo(name) if name else None
            except Exception:
                self._tz[name] = None
        return self._tz[name]

    def d0(self, u):
        for o in self.occ.get(u, []):
            if o["occasion"] == "d0":
                return o["date"]
        return None

    def first_date(self, u):
        o = self.occ.get(u)
        return min(x["date"] for x in o) if o else None

    def last_date(self, u):
        o = self.occ.get(u)
        return max(x["date"] for x in o) if o else None

    def plan_terms(self):
        t = set()
        for o in self.outcomes:
            t |= rhs_terms(o["model"])
        return t

    def factor_level_map(self, roles=("assigned_treatment",)):
        """unit_id -> {factor name: level} for factors with the given roles (unit rows or the
        unit's system rows)."""
        out = defaultdict(dict)
        for fid, fac in self.factors.items():
            if fac["role"] not in roles:
                continue
            for r in self.rows("SELECT * FROM factor_levels WHERE factor_id = ?", (fid,)):
                for u, ur in self.units.items():
                    if r["unit_id"] == u or (r["system_id"] is not None and r["system_id"] == ur["system_id"]):
                        out[u][fac["name"]] = r["level"]
        return out

    def daily_temps(self, unit_id=None, system_id=None):
        """{local date: mean temp_c} from environment rows of a unit or a system (system rows
        restricted to the depth nearest systems.reference_depth_m). Plain mean of the day's
        rows (approximation of the time-weighted mean of derived_metrics.degree_days)."""
        if unit_id:
            rows = self.rows("SELECT timestamp, value, depth_m FROM environment WHERE unit_id = ? AND variable = 'temp_c'"
                             " AND COALESCE(qc_flag, 'raw') <> 'removed'", (unit_id,))
            tz = self.tz(self.units[unit_id]["timezone"]) if unit_id in self.units else None
            ref = None
        else:
            rows = self.rows("SELECT timestamp, value, depth_m FROM environment WHERE system_id = ? AND variable = 'temp_c'"
                             " AND COALESCE(qc_flag, 'raw') <> 'removed'", (system_id,))
            sysr = self.systems.get(system_id) or {}
            tz = self.tz(sysr.get("timezone"))
            ref = sysr.get("reference_depth_m")
        if ref is not None:
            depths = {r["depth_m"] for r in rows if r["depth_m"] is not None}
            if depths:
                best = min(depths, key=lambda x: abs(x - ref))
                rows = [r for r in rows if r["depth_m"] == best]
        by = defaultdict(list)
        for r in rows:
            by[local_date(r["timestamp"], tz)].append(r["value"])
        return {k: sum(v) / len(v) for k, v in by.items()}

    def filter_rowids(self, table, target_filter):
        """rowids of `table` matched by a deviations.target_filter 'table: <SQL condition>'.
        Returns (set, None) or (None, reason)."""
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)\Z", target_filter or "", re.S)
        if not m:
            return None, "target_filter has no 'table:' prefix"
        if m.group(1) != table:
            return set(), None
        expr = m.group(2)
        if ";" in expr:
            return None, "target_filter contains ';'"
        try:
            return {r[0] for r in self.con.execute('SELECT rowid FROM "%s" WHERE (%s)' % (table, expr))}, None
        except sqlite3.Error as e:
            return None, "target_filter not evaluable (%s)" % e


RULES = {}


def rule(rid):
    def deco(fn):
        RULES[rid] = fn
        return fn
    return deco


def need_metrics(ctx, rid):
    if M is None:
        ctx.f(rid, "not checked: metrics module unavailable (%s)" % METRICS_ERROR, severity="info")
        return False
    return True


def is_tank(ur):
    return ur["unit_type"] == "tank" or (ur["unit_type"] is None and ur["system_kind"] in ("ras", "flowthrough"))


def is_cage(ur):
    return ur["unit_type"] == "cage" or ur["system_kind"] == "sea_cage"


# ---------------------------------------------------------------------------- rules
@rule("R01")
def r01(ctx):
    for u, ur in ctx.units.items():
        names = {o["occasion"] for o in ctx.occ.get(u, [])}
        n_stock = ctx.val("SELECT count(*) FROM weighings WHERE unit_id = ? AND sampling_type = 'stocking'"
                          " AND COALESCE(qc_flag, 'raw') <> 'removed'", (u,))
        if not n_stock:
            ctx.f("R01", "no stocking weighing (unit_analysis_status: has_stocking = false)", u)
        if "d0" not in names:
            ctx.f("R01", "no d0 occasion: unit is not in ITT (unit_analysis_status: has_d0 = false)", u)
        if ur["termination_reason"] == "planned_end" and "final" not in names:
            ctx.f("R01", "termination_reason planned_end but no final occasion", u)
        elif ur["termination_reason"] is None and not names & {"final", "end_early"}:
            ctx.f("R01", "no final / end_early occasion and termination_reason NULL: final outcome missing"
                  " (listed in unit_analysis_status)", u, severity="warning")
        if n_stock and "d0" in names and M is not None and M.n_fish_d0(ctx.con, u) is None:
            ctx.f("R01", "n_fish_d0 not derivable (no count at d0 and stocking count not usable)", u)
    if M is None:
        need_metrics(ctx, "R01")


@rule("R02")
def r02(ctx):
    terms = ctx.plan_terms()
    sys_site = {s: r["site_id"] for s, r in ctx.systems.items()}
    for fid, fac in ctx.factors.items():
        if fac["role"] == "blocking":
            continue
        in_plan = fac["name"] in terms or fid in terms
        sev = ctx.sev["R02"] if in_plan else "warning"
        why = "term of a planned_outcomes model" if in_plan else "not a term of any planned_outcomes model"
        rows = ctx.rows("SELECT * FROM factor_levels WHERE factor_id = ? ORDER BY record_id", (fid,))
        rows = [r for r in rows if (r["system_id"] in ctx.systems) or (r["unit_id"] in ctx.units)]
        want_unit = fac["applied_at"] == "unit"
        for r in rows:
            if want_unit != (r["unit_id"] is not None):
                ctx.f("R02", "factor %s applied_at %s but level stored on %s" % (
                    fac["name"], fac["applied_at"], "unit" if r["unit_id"] else "system"),
                    r["unit_id"], "factor_levels:" + r["record_id"], severity=sev)
        if fac["applied_at"] == "site":
            per_site = defaultdict(set)
            for r in rows:
                if r["system_id"]:
                    per_site[sys_site[r["system_id"]]].add(r["level"])
            for site, lv in sorted(per_site.items()):
                if len(lv) > 1:
                    ctx.f("R02", "site-level factor %s has several levels %s on systems of site %s" % (
                        fac["name"], sorted(lv), site), severity=sev)
        # every unit has a level
        has = set()
        for r in rows:
            for u, ur in ctx.units.items():
                if r["unit_id"] == u or (r["system_id"] is not None and r["system_id"] == ur["system_id"]):
                    has.add(u)
        missing = sorted(set(ctx.units) - has)
        if missing:
            ctx.f("R02", "factor %s has no level for units %s" % (fac["name"], ", ".join(missing)), severity=sev)
        # replication of the level
        if fac["role"] == "assigned_treatment" and fac["applied_at"] == "system":
            what, key = "systems", lambda r: r["system_id"]
        elif fac["role"] == "assigned_treatment" and fac["applied_at"] == "site" or fac["role"] == "observed_condition":
            what = "sites"
            key = lambda r: sys_site[r["system_id"]] if r["system_id"] else sys_site[ctx.units[r["unit_id"]]["system_id"]]  # noqa: E731
        else:
            continue
        per_level = defaultdict(set)
        for r in rows:
            per_level[r["level"]].add(key(r))
        n_rep = len(set().union(*per_level.values())) if per_level else 0
        df = n_rep - len(per_level)
        for level, members in sorted(per_level.items()):
            if len(members) < 2:
                ctx.f("R02", "%s %s level %s has %d %s (need >= 2); df for the term = %d %s - %d levels = %d%s; %s" % (
                    fac["role"], fac["name"], level, len(members), what, n_rep, what, len(per_level), df,
                    " (descriptive only)" if df <= 0 else "", why), severity=sev)


@rule("R03")
def r03(ctx):
    devs = ctx.rows("SELECT * FROM deviations WHERE trial_id = ? AND category = 'exclusion' ORDER BY record_id",
                    (ctx.trial_id,))
    has_rid = {t for t, s in ctx.model["tables"].items() if "record_id" in s["columns"]}
    per_diet = defaultdict(Counter)
    unbl = ctx.trial["unblinded_at"]
    for x in devs:
        rec = "deviations:" + x["record_id"]
        named = x["unit_id"] or x["fish_id"] or x["target_table"] or x["target_filter"]
        if not named:
            ctx.f("R03", "exclusion names no unit_id, fish_id, target record or target_filter", record=rec)
            continue
        scope = x["exclusion_scope"]
        if scope == "unit" and not x["unit_id"]:
            ctx.f("R03", "exclusion_scope unit without unit_id", record=rec)
        if scope == "fish" and not x["fish_id"]:
            ctx.f("R03", "exclusion_scope fish without fish_id", record=rec)
        if scope == "record" and not (x["target_record_id"] or x["target_filter"]):
            ctx.f("R03", "exclusion_scope record without target record or target_filter", record=rec)
        unit = x["unit_id"]
        if x["target_table"]:
            t = x["target_table"]
            if t not in has_rid:
                ctx.f("R03", "target_table %s has no record_id: use target_filter" % t, record=rec)
            else:
                cols = ctx.model["tables"][t]["columns"]
                r = ctx.rows('SELECT %s FROM "%s" WHERE record_id = ?' % (
                    "unit_id" if "unit_id" in cols else "NULL AS unit_id", t), (x["target_record_id"],))
                if not r:
                    ctx.f("R03", "target %s:%s does not exist" % (t, x["target_record_id"]), record=rec)
                else:
                    unit = unit or r[0]["unit_id"]
        if x["target_filter"]:
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", x["target_filter"])
            if not m or m.group(1) not in ctx.model["tables"]:
                ctx.f("R03", "target_filter without a known 'table:' prefix", record=rec)
            else:
                ids, why = ctx.filter_rowids(m.group(1), x["target_filter"])
                if ids is None:
                    ctx.f("R03", why, record=rec)
        diet = ctx.units[unit]["diet_id"] if unit in ctx.units else "(no unit)"
        kind = "units" if scope == "unit" else "fish" if scope == "fish" else "records"
        if x["decided_by"] is None:
            per_diet[diet]["pending " + kind] += 1
            ctx.f("R03", "pending exclusion (decided_by NULL): stays in every population, flagged; a sensitivity"
                  " analysis removes it (%s, reason %s)" % (scope, x["exclusion_reason"]), unit, rec, severity="warning")
        else:
            per_diet[diet]["decided " + kind] += 1
            if x["decided_date"] is None:
                ctx.f("R03", "decided exclusion without decided_date: mITT membership not decidable", unit, rec,
                      severity="warning")
            if x["exclusion_reason"] is None:
                ctx.f("R03", "decided exclusion without exclusion_reason: mITT membership not decidable", unit, rec,
                      severity="warning")
    itt = [u for u in ctx.units if ctx.d0(u)]
    excl_pp, excl_mitt = set(), set()
    for x in devs:
        if x["decided_by"] and x["exclusion_scope"] == "unit" and x["unit_id"]:
            excl_pp.add(x["unit_id"])
            if (x["exclusion_reason"] == "prespecified_rule" and x["decided_date"] and unbl
                    and x["decided_date"] < unbl[:10]):
                excl_mitt.add(x["unit_id"])
    counts = "; ".join("%s: %s" % (k, ", ".join("%d %s" % (n, w) for w, n in sorted(c.items())))
                       for k, c in sorted(per_diet.items())) or "none"
    ctx.f("R03", "populations: ITT %d units, mITT %d, PP %d; exclusions per diet: %s" % (
        len(itt), len([u for u in itt if u not in excl_mitt]), len([u for u in itt if u not in excl_pp]), counts),
        severity="info")


def _events_window(ctx, u, a, b):
    """count-changing events after the weighing on date a and before the weighing on date b
    (header convention: an event on a weighing day is after it unless relative_to_weighing =
    before)."""
    et = ctx.model.get("event_types") or {}
    out = []
    for e in ctx.rows("SELECT * FROM events WHERE unit_id = ? AND date >= ? AND date <= ?", (u, iso(a), iso(b))):
        if e["event_type"] not in et or not e["n_fish"]:
            continue
        ed, before = d(e["date"]), e["relative_to_weighing"] == "before"
        if ed == a and before and a != b:
            continue
        if ed == b and not before:
            continue
        out.append((et[e["event_type"]]["delta"], e))
    return out


@rule("R04")
def r04(ctx):
    if not need_metrics(ctx, "R04"):
        return
    tol = ctx.p["n_balance_tolerance_pct"]
    for u in ctx.units:
        occs = M.occasions(ctx.con, u)
        d0 = ctx.d0(u)
        counts = []
        for o in occs:
            c = M.count_at(ctx.con, u, o["occasion"])
            if c is not None:
                counts.append((o, c[0]))
        if d0 is not None and not any(d(o["date"]) > d0 for o, _ in counts):
            ctx.f("R04", "no recount after d0: count balance cannot be checked", u, severity="warning")
        for (po, pn), (co, cn) in zip(counts, counts[1:]):
            # expected = previous count carried forward over the count-changing events between the
            # two weighings. NOT n_at_risk(occasion_date): that returns the recount itself.
            evs = _events_window(ctx, u, d(po["date"]), d(co["date"]))
            exp_n = pn + sum(delta * e["n_fish"] for delta, e in evs)
            if hasattr(M, "expected_count"):
                m_exp = M.expected_count(ctx.con, u, co["occasion"])
                if m_exp is not None:
                    exp_n = m_exp
            lo = hi = float(exp_n)
            for delta, e in evs:     # widen by the n_fish_low..n_fish_high range of estimated events
                vals = [delta * e["n_fish"]]
                if e["n_fish_low"] is not None:
                    vals.append(delta * e["n_fish_low"])
                if e["n_fish_high"] is not None:
                    vals.append(delta * e["n_fish_high"])
                lo += min(vals) - delta * e["n_fish"]
                hi += max(vals) - delta * e["n_fish"]
            lo_t, hi_t = lo * (1 - tol / 100.0), hi * (1 + tol / 100.0)
            exp = "%g" % lo if lo == hi else "%g..%g" % (lo, hi)
            if not lo_t <= cn <= hi_t:
                ctx.f("R04", "count balance %s -> %s: expected %s from %s count %d and events, counted %d"
                      " (outside %g %% tolerance); log in deviations / count_adjustment" % (
                          po["occasion"], co["occasion"], exp, po["occasion"], pn, cn, tol), u,
                      "unit_occasions:%s/%s" % (u, co["occasion"]))
            elif not lo <= cn <= hi:
                ctx.f("R04", "count difference %s -> %s: expected %s, counted %d (within tolerance); log in"
                      " deviations" % (po["occasion"], co["occasion"], exp, cn), u,
                      "unit_occasions:%s/%s" % (u, co["occasion"]), severity="info")


@rule("R05")
def r05(ctx):
    if not need_metrics(ctx, "R05"):
        return
    lim = ctx.p["max_negative_growth_pct"]
    for u in ctx.units:
        prev = None
        for o in M.occasions(ctx.con, u):
            w = M.unit_weight(ctx.con, u, o["occasion"])
            if not w or w.get("w_g") is None:
                continue
            if prev and w["w_g"] < prev[1] * (1 - lim / 100.0):
                ctx.f("R05", "unit weight %s %.1f g (%s) is %.1f %% below %s %.1f g (limit %g %%)" % (
                    o["occasion"], w["w_g"], w.get("w_source"), 100 * (1 - w["w_g"] / prev[1]), prev[0], prev[1], lim),
                    u, "unit_occasions:%s/%s" % (u, o["occasion"]))
            prev = (o["occasion"], w["w_g"])


@rule("R06")
def r06(ctx):
    lim_mean, lim_cv = ctx.p["max_initial_mean_diff_pct"], ctx.p["max_initial_cv_pct"]
    # within-unit CV of individual d0 weights (raw rows, round or NULL basis as in unit_weight_g, not removed)
    for u in ctx.units:
        ws = [r[0] for r in ctx.con.execute(
            "SELECT weight_g FROM weighings WHERE unit_id = ? AND occasion = 'd0' AND sampling_type IN"
            " ('individual','sample') AND COALESCE(weight_basis, 'round') = 'round' AND weight_g IS NOT NULL"
            " AND COALESCE(qc_flag,'raw') <> 'removed'", (u,))]
        if len(ws) >= 2:
            m = sum(ws) / len(ws)
            cv = 100 * math.sqrt(sum((w - m) ** 2 for w in ws) / (len(ws) - 1)) / m if m else float("inf")
            if cv > lim_cv:
                ctx.f("R06", "d0 within-unit CV %.1f %% > %g %% (n = %d)" % (cv, lim_cv, len(ws)), u)
    if not need_metrics(ctx, "R06"):
        return
    w0 = {}
    for u in ctx.units:
        w = M.unit_weight(ctx.con, u, "d0")
        if w and w.get("w_g") is not None:
            w0[u] = w["w_g"]

    def means(key):
        g = defaultdict(list)
        for u, w in w0.items():
            k = key(u)
            if k is not None:
                g[k].append(w)
        return {k: sum(v) / len(v) for k, v in g.items()}

    dm = means(lambda u: ctx.units[u]["diet_id"])
    ref = [k for k, r in ctx.diets.items() if r["control_role"] == "reference" and k in dm]
    if ref:
        for k, v in sorted(dm.items()):
            if k != ref[0] and abs(v - dm[ref[0]]) / dm[ref[0]] * 100 > lim_mean:
                ctx.f("R06", "d0 mean of diet %s %.1f g differs from reference %s %.1f g by %.1f %% (> %g %%)" % (
                    k, v, ref[0], dm[ref[0]], abs(v - dm[ref[0]]) / dm[ref[0]] * 100, lim_mean))
    elif len(dm) > 1:
        lo, hi = min(dm.values()), max(dm.values())
        if (hi - lo) / lo * 100 > lim_mean:
            ctx.f("R06", "no reference diet; d0 diet means range %.1f..%.1f g (%.1f %% > %g %%)" % (
                lo, hi, (hi - lo) / lo * 100, lim_mean))
    lm = ctx.factor_level_map()
    for fid, fac in ctx.factors.items():
        if fac["role"] != "assigned_treatment" or fac["applied_at"] not in ("system", "site"):
            continue
        lv = means(lambda u: lm[u].get(fac["name"]))
        keys = sorted(lv)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                diff = abs(lv[a] - lv[b]) / min(lv[a], lv[b]) * 100
                if diff > lim_mean:
                    ctx.f("R06", "d0 mean of %s %s %.1f g vs %s %.1f g differ by %.1f %% (> %g %%)" % (
                        fac["name"], a, lv[a], b, lv[b], diff, lim_mean))


@rule("R07")
def r07(ctx):
    for u in ctx.units:
        d0, last = ctx.d0(u), ctx.last_date(u)
        if d0 is None or last is None:
            continue
        fed = {d(r[0]) for r in ctx.con.execute("SELECT DISTINCT date FROM feeding WHERE unit_id = ?", (u,))}
        stop = set()
        for a, b in ctx.con.execute("SELECT date, COALESCE(end_date, date) FROM events WHERE unit_id = ?"
                                    " AND event_type = 'feeding_stop'", (u,)):
            stop |= set(days(d(a), d(b) + dt.timedelta(days=1)))
        miss = [x for x in days(d0, last) if x not in fed and x not in stop]
        for a, b in ranges(miss):
            ctx.f("R07", "no feeding row and no feeding_stop on %s (%d day(s)): feed not recorded" % (
                iso(a) if a == b else "%s..%s" % (iso(a), iso(b)), (b - a).days + 1), u)


def _period_temp(ctx, u, a, b, cache):
    if u not in cache:
        cache[u] = (ctx.daily_temps(unit_id=u), ctx.daily_temps(system_id=ctx.units[u]["system_id"]))
    ut, st = cache[u]
    vals = [ut.get(x, st.get(x)) for x in days(a, b)]
    vals = [v for v in vals if v is not None]
    if vals:
        return sum(vals) / len(vals)
    return ctx.units[u]["target_temp_c"]


@rule("R08")
def r08(ctx):
    band = ctx.p["temp_band_width_c"]
    tests = ctx.rows("SELECT * FROM recovery_tests WHERE trial_id = ?", (ctx.trial_id,))
    cache, missing_tests = {}, defaultdict(set)
    for u, ur in ctx.units.items():
        if not is_tank(ur):
            continue
        fdays = {d(r[0]) for r in ctx.con.execute(
            "SELECT date FROM feeding WHERE unit_id = ? GROUP BY date"
            " HAVING sum(feed_given_g) > 0 AND max(COALESCE(feeding_stopped, 0)) = 0", (u,))}
        coll = {d(r[0]) for r in ctx.con.execute(
            "SELECT date FROM uneaten_feed WHERE unit_id = ? AND COALESCE(qc_flag,'raw') <> 'removed'", (u,))}
        miss = sorted(fdays - coll)
        if miss:
            ctx.f("R08", "%d tank feeding day(s) without uneaten_feed row (not collected; filled by period_sum):"
                  " %s" % (len(miss), fmt_ranges(ranges(miss))), u)
        occs = [o for o in ctx.occ.get(u, []) if ctx.d0(u) and o["date"] >= ctx.d0(u)]
        for po, co in zip(occs, occs[1:]):
            a, b = po["date"], co["date"]
            pellets = {r[0] for r in ctx.con.execute(
                "SELECT DISTINCT fb.pellet_mm FROM feeding f JOIN feed_batches fb ON fb.batch_id = f.batch_id"
                " WHERE f.unit_id = ? AND f.date >= ? AND f.date < ? AND f.feed_given_g > 0", (u, iso(a), iso(b)))}
            t = _period_temp(ctx, u, a, b, cache)
            for pm in pellets:
                ok = [x for x in tests if x["unit_type"] == ur["unit_type"] and pm is not None
                      and x["pellet_mm"] == pm and t is not None and x["temp_c"] is not None
                      and abs(x["temp_c"] - t) <= band]
                if not ok:
                    missing_tests[(ur["unit_type"], pm, None if t is None else round(t, 1))].add(
                        "%s %s-%s" % (u, po["occasion"], co["occasion"]))
    for (ut, pm, t), where in sorted(missing_tests.items(), key=str):
        ctx.f("R08", "no recovery_tests row for unit_type %s, pellet %s mm, temperature %s +- %g C (period mean"
              " temperature approximated from unit / system daily means); used by %s" % (
                  ut, pm, t, band, ", ".join(sorted(where))))


@rule("R09")
def r09(ctx):
    gap_h, jump = ctx.p["max_gap_h"], ctx.p["max_temp_jump_c_h"]
    rows = ctx.rows(
        "SELECT rowid, record_id, timestamp, unit_id, system_id, variable, sensor_id, depth_m, measure_position,"
        " aggregation, nominal_interval_min, value FROM environment WHERE COALESCE(qc_flag,'raw') <> 'removed'"
        " AND (unit_id IN %s OR system_id IN %s) ORDER BY timestamp" % (Ctx.UNITS_SQL, Ctx.SYSTEMS_SQL),
        (ctx.trial_id, ctx.trial_id))
    series = defaultdict(list)
    for r in rows:
        series[(r["unit_id"] or "", r["system_id"] or "", r["variable"], r["sensor_id"] or "",
                r["depth_m"], r["measure_position"] or "", r["aggregation"] or "")].append(r)
    for key, rs in sorted(series.items(), key=lambda kv: str(kv[0])):
        u, s, var = key[0] or None, key[1] or None, key[2]
        name = "%s %s%s%s" % (u or s, var, " sensor " + key[3] if key[3] else "",
                              " depth %g" % key[4] if key[4] is not None else "")
        ts = [dt.datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ") for r in rs]
        nom = Counter(r["nominal_interval_min"] for r in rs if r["nominal_interval_min"])
        if nom:
            interval_h = nom.most_common(1)[0][0] / 60.0
        elif len(ts) > 2:
            dif = sorted((b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:]))
            interval_h = dif[len(dif) // 2]
        else:
            interval_h = None
        for i in range(1, len(rs)):
            dh = (ts[i] - ts[i - 1]).total_seconds() / 3600
            if interval_h is not None and dh - interval_h > gap_h:
                ctx.f("R09", "%s: gap of %.1f h between %s and %s (nominal interval %.1f h, max gap %g h)" % (
                    name, dh, rs[i - 1]["timestamp"], rs[i]["timestamp"], interval_h, gap_h), u,
                    "environment:" + rs[i]["record_id"])
            if var == "temp_c" and dh > 0 and abs(rs[i]["value"] - rs[i - 1]["value"]) / dh > jump:
                ctx.f("R09", "%s: temperature jump %.2f C in %.2f h (> %g C/h) at %s" % (
                    name, rs[i]["value"] - rs[i - 1]["value"], dh, jump, rs[i]["timestamp"]), u,
                    "environment:" + rs[i]["record_id"])
        if var == "o2_sat_pct":
            for r in rs:
                if r["value"] > 150 or r["value"] < 0:
                    ctx.f("R09", "%s: o2_sat_pct %g outside 0..150" % (name, r["value"]), u,
                          "environment:" + r["record_id"])
        if var not in LIGHT_VARS and interval_h:
            min_run = max(int(ctx.p["flatline_min_readings"]), int(math.ceil(gap_h / interval_h)) + 1)
            run = 1
            for i in range(1, len(rs) + 1):
                if i < len(rs) and rs[i]["value"] == rs[i - 1]["value"]:
                    run += 1
                    continue
                if run >= min_run:
                    ctx.f("R09", "%s: flat-lined, %d identical readings (%g) %s..%s (>= %d)" % (
                        name, run, rs[i - 1]["value"], rs[i - run]["timestamp"], rs[i - 1]["timestamp"], min_run),
                        u, "environment:" + rs[i - run]["record_id"])
                run = 1


def _camera(ctx):
    rows = ctx.rows("SELECT * FROM camera_estimates WHERE unit_id IN %s ORDER BY unit_id, camera_id, period_start"
                    % Ctx.UNITS_SQL, (ctx.trial_id,))
    cal = ctx.rows("SELECT * FROM camera_calibrations WHERE unit_id IN %s" % Ctx.UNITS_SQL, (ctx.trial_id,))
    for r in rows:
        r["local"] = local_date(r["period_start"], ctx.tz(ctx.units[r["unit_id"]]["timezone"]))
    return rows, cal


def _cal_valid(c, day):
    return d(c["valid_from"]) <= day and (c["valid_to"] is None or day <= d(c["valid_to"]))


@rule("R10")
def r10(ctx):
    rows, cal = _camera(ctx)
    mind = ctx.p["min_detections"]
    seen_version = {}
    for r in rows:
        rec = "camera_estimates:%s/%s/%s/%s" % (r["unit_id"], r["camera_id"], r["period_start"], r["algorithm_version"])
        u, key = r["unit_id"], (r["unit_id"], r["camera_id"])
        if r["algorithm_version"] == "unknown":
            ctx.f("R10", "algorithm_version unknown", u, rec)
        if r["n_detections"] is not None and r["n_detections"] < mind:
            ctx.f("R10", "n_detections %d < %g" % (r["n_detections"], mind), u, rec)
        mine = [c for c in cal if (c["unit_id"], c["camera_id"], c["algorithm_version"]) ==
                (u, r["camera_id"], r["algorithm_version"])]
        if r["calibration_id"] is None and not any(_cal_valid(c, r["local"]) for c in mine):
            ctx.f("R10", "no camera_calibrations row valid on local date %s" % iso(r["local"]), u, rec)
        prev = seen_version.get(key)
        if prev is not None and prev != r["algorithm_version"] and not mine:
            ctx.f("R10", "algorithm_version changed %s -> %s without a new camera_calibrations row" % (
                prev, r["algorithm_version"]), u, rec)
        seen_version[key] = r["algorithm_version"]


@rule("R11")
def r11(ctx):
    regs = ctx.rows("SELECT * FROM light_regimes WHERE unit_id IN %s OR system_id IN %s" % (
        Ctx.UNITS_SQL, Ctx.SYSTEMS_SQL), (ctx.trial_id, ctx.trial_id))
    scopes = defaultdict(list)     # (kind, id) -> units
    for u, ur in ctx.units.items():
        if any(r["unit_id"] == u for r in regs):
            scopes[("unit", u)].append(u)
        else:
            scopes[("system", ur["system_id"])].append(u)
    for (kind, sid), us in sorted(scopes.items()):
        firsts = [ctx.first_date(u) for u in us if ctx.first_date(u)]
        lasts = [ctx.last_date(u) for u in us if ctx.last_date(u)]
        if not firsts:
            continue
        a, b = min(firsts), max(lasts)
        mine = [r for r in regs if r[kind + "_id"] == sid]
        unit = sid if kind == "unit" else None
        if not mine:
            ctx.f("R11", "no light_regimes row for %s %s (period %s..%s)" % (kind, sid, iso(a), iso(b)), unit)
            continue
        cover = Counter()
        for r in mine:
            s, e = d(r["start_date"]), d(r["end_date"]) if r["end_date"] else b
            for x in days(max(a, s), min(b, e) + dt.timedelta(days=1)):
                cover[x] += 1
        period = list(days(a, b + dt.timedelta(days=1)))
        gaps = [x for x in period if cover[x] == 0]
        over = [x for x in period if cover[x] > 1]
        if gaps:
            ctx.f("R11", "light_regimes gap for %s %s: %s" % (kind, sid, fmt_ranges(ranges(gaps))), unit)
        if over:
            ctx.f("R11", "light_regimes overlap for %s %s: %s" % (kind, sid, fmt_ranges(ranges(over))), unit)


@rule("R12")
def r12(ctx):
    if ctx.trial["blinding_feeders"] != 1:
        return
    no_code = [u for u, ur in ctx.units.items() if ur["diet_code_blind"] is None]
    if no_code:
        ctx.f("R12", "blinding_feeders = true but %d unit(s) have no diet_code_blind, so unit-level exports can"
              " only show the diet label (%s); exports themselves are not in the database" % (
                  len(no_code), ", ".join(no_code)))
    for u, ur in ctx.units.items():
        c = ur["diet_code_blind"]
        dr = ctx.diets.get(ur["diet_id"]) or {}
        if c is not None and c.lower() in {str(dr.get("label") or "").lower(), str(ur["diet_id"]).lower()}:
            ctx.f("R12", "blinding_feeders = true but diet_code_blind '%s' equals the diet label / id" % c, u)


@rule("R14")
def r14(ctx):
    lag = ctx.p["max_slaughter_lag_days"]
    by = defaultdict(lambda: {"keys": set(), "w_free": 0, "fm_free": 0, "recs": []})
    for r in ctx.rows("SELECT record_id, unit_id, date, fish_id, specimen_no FROM weighings WHERE unit_id IN %s"
                      " AND sampling_type IN ('sample','individual') AND fish_fate = 'killed'" % Ctx.UNITS_SQL,
                      (ctx.trial_id,)):
        g = by[(r["unit_id"], r["date"])]
        k = ("f", r["fish_id"]) if r["fish_id"] else ("s", r["specimen_no"]) if r["specimen_no"] else None
        if k:
            g["keys"].add(k)
        else:
            g["w_free"] += 1
        g["recs"].append("weighings:" + r["record_id"])
    for r in ctx.rows("SELECT record_id, unit_id, date, fish_id, specimen_no FROM fish_measurements"
                      " WHERE unit_id IN %s" % Ctx.UNITS_SQL, (ctx.trial_id,)):
        g = by[(r["unit_id"], r["date"])]
        k = ("f", r["fish_id"]) if r["fish_id"] else ("s", r["specimen_no"]) if r["specimen_no"] else None
        if k:
            g["keys"].add(k)
        else:
            g["fm_free"] += 1
        g["recs"].append("fish_measurements:" + r["record_id"])
    for (u, day_s), g in sorted(by.items()):
        need = len(g["keys"]) + max(g["w_free"], g["fm_free"])
        x = d(day_s)
        have = ctx.val("SELECT COALESCE(sum(n_fish), 0) FROM events WHERE unit_id = ? AND event_type IN"
                       " ('removal_sampling','harvest') AND date BETWEEN ? AND ?",
                       (u, iso(x - dt.timedelta(days=int(lag))), iso(x + dt.timedelta(days=int(lag)))))
        if have < need:
            ctx.f("R14", "%d killed / measured fish on %s but removal_sampling (or harvest) events within %g day(s)"
                  " remove %d" % (need, day_s, lag, have), u, g["recs"][0])
    for r in ctx.rows("SELECT unit_id, occasion, count(*) n, min(record_id) rid FROM weighings WHERE unit_id IN %s"
                      " AND sampling_type IN ('sample','individual') AND fish_fate IS NULL GROUP BY unit_id, occasion"
                      % Ctx.UNITS_SQL, (ctx.trial_id,)):
        ctx.f("R14", "%d sample/individual row(s) at %s with fish_fate NULL (killed or returned unknown)" % (
            r["n"], r["occasion"]), r["unit_id"], "weighings:" + r["rid"], severity="warning")


def _qc_tables(ctx):
    for t, spec in ctx.model["tables"].items():
        cols = spec["columns"]
        if "qc_flag" not in cols:
            continue
        if "trial_id" in cols:
            scope, args = "trial_id = ?", (ctx.trial_id,)
        elif "unit_id" in cols and "system_id" in cols:
            scope, args = "(unit_id IN %s OR system_id IN %s)" % (Ctx.UNITS_SQL, Ctx.SYSTEMS_SQL), (ctx.trial_id,) * 2
        elif "unit_id" in cols:
            scope, args = "unit_id IN %s" % Ctx.UNITS_SQL, (ctx.trial_id,)
        elif "sample_id" in cols:
            scope, args = "sample_id IN (SELECT sample_id FROM samples WHERE trial_id = ?)", (ctx.trial_id,)
        else:
            continue
        yield t, cols, scope, args


@rule("R15")
def r15(ctx):
    devs = ctx.rows("SELECT * FROM deviations WHERE trial_id = ?", (ctx.trial_id,))
    told = set()
    for t, cols, scope, args in _qc_tables(ctx):
        pk = ctx.model["tables"][t]["primary_key"]
        pk = pk if isinstance(pk, list) else [pk]
        rows = ctx.rows('SELECT rowid AS _rowid, %s%s FROM "%s" WHERE qc_flag = \'removed\' AND %s' % (
            ", ".join(pk), ", unit_id" if "unit_id" in cols and "unit_id" not in pk else "", t, scope), args)
        if not rows:
            continue
        named = {x["target_record_id"] for x in devs if x["target_table"] == t}
        covered = set()
        for x in devs:
            if x["target_filter"]:
                ids, why = ctx.filter_rowids(t, x["target_filter"])
                if ids is None:
                    if x["record_id"] not in told:
                        told.add(x["record_id"])
                        ctx.f("R15", "deviation %s: %s (cannot document removed rows)" % (x["record_id"], why),
                              record="deviations:" + x["record_id"], severity="warning")
                else:
                    covered |= ids
        for r in rows:
            if "record_id" in pk and r["record_id"] in named:
                continue
            if r["_rowid"] in covered:
                continue
            ctx.f("R15", "qc_flag removed without a deviations row naming the record or a matching target_filter",
                  r.get("unit_id"), "%s:%s" % (t, "/".join(str(r[k]) for k in pk)))


@rule("R17")
def r17(ctx):
    for r in ctx.rows("SELECT f.unit_id, f.date, f.batch_id, f.diet_id, dt.trial_id dtrial, u.trial_id utrial"
                      " FROM feeding f JOIN units u ON u.unit_id = f.unit_id JOIN diets dt ON dt.diet_id = f.diet_id"
                      " WHERE (u.trial_id = ? OR dt.trial_id = ?) AND dt.trial_id <> u.trial_id",
                      (ctx.trial_id, ctx.trial_id)):
        ctx.f("R17", "feeding diet %s belongs to trial %s, unit to trial %s" % (r["diet_id"], r["dtrial"], r["utrial"]),
              r["unit_id"], "feeding:%s/%s/%s" % (r["unit_id"], r["date"], r["batch_id"]))
    for r in ctx.rows(
            "SELECT fl.record_id, fl.unit_id, df.trial_id ftrial, COALESCE(s.trial_id, u.trial_id) otrial"
            " FROM factor_levels fl JOIN design_factors df ON df.factor_id = fl.factor_id"
            " LEFT JOIN systems s ON s.system_id = fl.system_id LEFT JOIN units u ON u.unit_id = fl.unit_id"
            " WHERE (df.trial_id = ? OR COALESCE(s.trial_id, u.trial_id) = ?)"
            " AND df.trial_id <> COALESCE(s.trial_id, u.trial_id)", (ctx.trial_id, ctx.trial_id)):
        ctx.f("R17", "factor_levels row: factor of trial %s, system/unit of trial %s" % (r["ftrial"], r["otrial"]),
              r["unit_id"], "factor_levels:" + r["record_id"])


def _fish_unit_at(ctx, fish_id, day, cache):
    if fish_id not in cache:
        f = ctx.rows("SELECT unit_id FROM fish WHERE fish_id = ?", (fish_id,))
        h = ctx.rows("SELECT unit_id, valid_from, valid_to FROM fish_unit_history WHERE fish_id = ?", (fish_id,))
        cache[fish_id] = (f[0]["unit_id"] if f else None, h)
    home, hist = cache[fish_id]
    for h in hist:
        if d(h["valid_from"]) <= day and (h["valid_to"] is None or day < d(h["valid_to"])):
            return h["unit_id"]
    return home


@rule("R18")
def r18(ctx):
    cache = {}
    for t, dcol in (("weighings", "date"), ("events", "date"), ("fish_measurements", "date"),
                    ("fish_scores", "date"), ("samples", "substr(collected_at, 1, 10)")):
        key = "sample_id" if t == "samples" else "record_id"
        for r in ctx.rows('SELECT %s AS k, unit_id, fish_id, %s AS day FROM "%s" WHERE fish_id IS NOT NULL'
                          " AND unit_id IN %s" % (key, dcol, t, Ctx.UNITS_SQL), (ctx.trial_id,)):
            if r["day"] is None:
                continue
            at = _fish_unit_at(ctx, r["fish_id"], d(r["day"]), cache)
            if at != r["unit_id"]:
                ctx.f("R18", "fish %s is in unit %s on %s, row says %s" % (r["fish_id"], at, r["day"], r["unit_id"]),
                      r["unit_id"], "%s:%s" % (t, r["k"]))


@rule("R19")
def r19(ctx):
    for u, ur in ctx.units.items():
        d0, last = ctx.d0(u), ctx.last_date(u)
        if d0 is None or last is None:
            continue
        if ur["end_date"]:
            last = min(last, d(ur["end_date"]) + dt.timedelta(days=1))
        seen = {d(r[0]) for r in ctx.con.execute(
            "SELECT DISTINCT date FROM events WHERE unit_id = ? AND event_type = 'mortality'", (u,))}
        miss = [x for x in days(d0, last) if x not in seen]
        if miss:
            ctx.f("R19", "no mortality row on %d day(s) (not checked, never 0): %s" % (
                len(miss), fmt_ranges(ranges(miss))), u)


@rule("R20")
def r20(ctx):
    blocked = {u: ur for u, ur in ctx.units.items() if ur["block"] is not None}
    if not blocked:
        return
    if not any(f["role"] == "blocking" for f in ctx.factors.values()):
        ctx.f("R20", "units.block is used (%d units) but no design_factors row has role blocking" % len(blocked))
    by = defaultdict(list)
    for u, ur in blocked.items():
        by[ur["block"]].append(ur)
    all_diets = set(ctx.diets)
    for b, urs in sorted(by.items()):
        scopes = {x["block_scope"] for x in urs}
        if "within_system" in scopes and len({x["system_id"] for x in urs}) > 1:
            ctx.f("R20", "block %s has block_scope within_system but spans systems %s: one label must be one"
                  " physical block" % (b, sorted({x["system_id"] for x in urs})))
        if "site" in scopes and len({x["site_id"] for x in urs}) > 1:
            ctx.f("R20", "block %s has block_scope site but spans sites %s" % (b, sorted({x["site_id"] for x in urs})))
        if len(scopes) > 1:
            ctx.f("R20", "block %s has mixed block_scope %s" % (b, sorted(map(str, scopes))))
        present = {x["diet_id"] for x in urs}
        if present != all_diets:
            ctx.f("R20", "incomplete block %s: diets %s missing" % (b, ", ".join(sorted(all_diets - present))))
    lm = ctx.factor_level_map()
    for fid, fac in ctx.factors.items():
        if fac["role"] != "assigned_treatment" or fac["applied_at"] not in ("system", "site"):
            continue
        b2l, l2b = defaultdict(set), defaultdict(set)
        for u, ur in blocked.items():
            lv = lm[u].get(fac["name"])
            b2l[ur["block"]].add(lv)
            l2b[lv].add(ur["block"])
        if b2l and all(len(v) == 1 for v in b2l.values()) and all(len(v) == 1 for v in l2b.values()):
            ctx.f("R20", "blocks coincide with the levels of whole-plot factor %s: block and treatment are aliased"
                  % fac["name"])


DOSE_COLS = ("dose_value", "dose_unit", "dose_basis", "dose_analyte")


@rule("R21")
def r21(ctx):
    if not any(r["dose_value"] is not None for r in ctx.diets.values()):
        return
    n_ref = sum(1 for r in ctx.diets.values() if r["control_role"] == "reference")
    if n_ref > 1:
        ctx.f("R21", "%d reference diets (max one)" % n_ref)
    for did, r in sorted(ctx.diets.items()):
        miss = [c for c in DOSE_COLS if r[c] is None]
        if miss:
            ctx.f("R21", "dose-response trial: diet %s has no %s" % (did, ", ".join(miss)), record="diets:" + did)
            continue
        if r["dose_basis"] == "added" and r["dose_value"] != 0:
            # dose_realised = C(diet) - C(the basal diet with dose_value 0 of the same trial)
            basal = [d for d, x in ctx.diets.items() if d != did and x["dose_value"] == 0]
            if len(basal) != 1:
                ctx.f("R21", "diet %s: dose_basis added needs exactly one basal diet with dose_value 0 in the trial"
                      " (found %d); dose_realised not computable" % (did, len(basal)), record="diets:" + did)
                continue
        comp = ctx.rows(
            "SELECT c.batch_id, c.value_unit, c.basis, fb.dm_frac FROM composition c JOIN feed_batches fb"
            " ON fb.batch_id = c.batch_id WHERE c.sample_type = 'diet' AND c.diet_id = ? AND c.analyte = ?"
            " AND COALESCE(c.qc_flag,'raw') <> 'removed'", (did, r["dose_analyte"]))
        if not comp:
            ctx.f("R21", "diet %s: no composition(sample_type diet) row with analyte '%s' (case-sensitive):"
                  " dose_realised not computable" % (did, r["dose_analyte"]), record="diets:" + did)
            continue
        bad = []
        for c in comp:
            if c["value_unit"] not in ("g_kg", "mg_kg", "pct"):
                bad.append("%s value_unit %s" % (c["batch_id"], c["value_unit"]))
            elif c["basis"] in ("wet", "unknown"):
                bad.append("%s basis %s" % (c["batch_id"], c["basis"]))
            elif ((r["dose_basis"] == "total_dm") != (c["basis"] == "dry_matter")) and c["dm_frac"] is None:
                bad.append("%s basis %s needs dm_frac" % (c["batch_id"], c["basis"]))
        if bad:
            ctx.f("R21", "diet %s: dose_realised not convertible to %s: %s" % (did, r["dose_basis"], "; ".join(bad)),
                  record="diets:" + did)


@rule("R22")
def r22(ctx):
    rows, cal = _camera(ctx)
    calid = {c["calibration_id"]: c for c in cal}
    for r in rows:
        if r["calibration_id"] is None:
            continue
        rec = "camera_estimates:%s/%s/%s/%s" % (r["unit_id"], r["camera_id"], r["period_start"], r["algorithm_version"])
        c = calid.get(r["calibration_id"])
        if c is None:
            ctx.f("R22", "calibration %s not found for this unit" % r["calibration_id"], r["unit_id"], rec)
        elif not _cal_valid(c, r["local"]):
            ctx.f("R22", "local date %s of period_start outside calibration %s valid %s..%s" % (
                iso(r["local"]), c["calibration_id"], c["valid_from"], c["valid_to"] or ""), r["unit_id"], rec)


def _conv_dm(value, unit, basis):
    if value is None:
        return None
    if unit == "g_kg":
        f = value / 1000.0
    elif unit == "pct":
        f = value / 100.0
    elif unit == "mg_kg":
        f = value / 1e6
    else:
        return None
    return f if basis in ("as_fed", "wet") else None


@rule("R23")
def r23(ctx):
    tol = ctx.p["dm_frac_tolerance"]
    for b in ctx.rows("SELECT fb.batch_id, fb.dm_frac FROM feed_batches fb JOIN diets dt ON dt.diet_id = fb.diet_id"
                      " WHERE dt.trial_id = ? AND fb.dm_frac IS NOT NULL", (ctx.trial_id,)):
        rows = ctx.rows("SELECT value, value_unit, basis, replicate_no, COALESCE(report_revision, 1) rev FROM"
                        " composition WHERE sample_type = 'diet' AND batch_id = ? AND analyte = 'dm'"
                        " AND COALESCE(qc_flag,'raw') <> 'removed'", (b["batch_id"],))
        if not rows:
            continue
        top = max(r["rev"] for r in rows)
        vals = [_conv_dm(r["value"], r["value_unit"], r["basis"]) for r in rows if r["rev"] == top]
        if any(v is None for v in vals):
            ctx.f("R23", "batch %s: composition dm not convertible to a fraction (unit/basis)" % b["batch_id"],
                  record="feed_batches:" + b["batch_id"])
            continue
        m = sum(vals) / len(vals)
        if abs(m - b["dm_frac"]) > tol:
            ctx.f("R23", "batch %s: dm_frac %.4f differs from analysed dm %.4f (tolerance %g)" % (
                b["batch_id"], b["dm_frac"], m, tol), record="feed_batches:" + b["batch_id"])


@rule("R24")
def r24(ctx):
    sea = {s for s, r in ctx.systems.items() if r["system_kind"] == "sea_cage"
           or (r["system_kind"] is None and ctx.trial["system_type"] == "sea_cage")}
    if not sea:
        return
    units = [u for u, ur in ctx.units.items() if ur["system_id"] in sea]
    for r in ctx.rows(
            "SELECT unit_id, system_id, variable, count(*) n, min(record_id) rid FROM environment"
            " WHERE variable IN ('temp_c','o2_mg_l','o2_sat_pct') AND depth_m IS NULL AND (unit_id IN (%s)"
            " OR system_id IN (%s)) GROUP BY unit_id, system_id, variable" % (
                ",".join("?" * len(units)) or "NULL", ",".join("?" * len(sea))), units + sorted(sea)):
        ctx.f("R24", "sea-cage %s: %d %s row(s) without depth_m" % (r["unit_id"] or r["system_id"], r["n"], r["variable"]),
              r["unit_id"], "environment:" + r["rid"])


@rule("R25")
def r25(ctx):
    lm = ctx.factor_level_map()
    runs = defaultdict(lambda: defaultdict(set))
    for r in ctx.rows("SELECT analysis_run_id, unit_id, diet_id FROM composition WHERE trial_id = ?"
                      " AND analysis_run_id IS NOT NULL AND COALESCE(qc_flag,'raw') <> 'removed'", (ctx.trial_id,)):
        g = runs[r["analysis_run_id"]]
        u = r["unit_id"]
        diet = r["diet_id"] or (ctx.units[u]["diet_id"] if u in ctx.units else None)
        if diet:
            g["diet"].add(diet)
        if u in ctx.units:
            g["system"].add(ctx.units[u]["system_id"])
            for fname, lv in lm[u].items():
                g[fname].add(lv)
    levels = {"diet": set(ctx.diets), "system": set(ctx.systems)}
    for u in ctx.units:
        for fname, lv in lm[u].items():
            levels.setdefault(fname, set()).add(lv)
    for run, g in sorted(runs.items()):
        bad = []
        for dim, vals in sorted(g.items()):
            if len(vals) == 1 and len(levels.get(dim, ())) > 1:
                bad.append("%s = %s" % (dim, next(iter(vals))))
        if bad:
            ctx.f("R25", "analysis_run_id %s holds a single level of: %s (lab batch aliased with treatment)" % (
                run, "; ".join(bad)))


def _feces(ctx):
    g = defaultdict(list)
    for r in ctx.rows("SELECT * FROM composition WHERE trial_id = ? AND sample_type = 'feces'"
                      " AND COALESCE(qc_flag,'raw') <> 'removed' ORDER BY record_id", (ctx.trial_id,)):
        g[(r["unit_id"], r["sample_id"])].append(r)
    return g


@rule("R26")
def r26(ctx):
    for (u, sid), rs in sorted(_feces(ctx).items(), key=str):
        rec = "composition:" + rs[0]["record_id"]
        starts = [r["collection_start"] or r["date"] for r in rs if r["collection_start"] or r["date"]]
        ends = [r["collection_end"] or r["collection_start"] or r["date"] for r in rs
                if r["collection_end"] or r["collection_start"] or r["date"]]
        if u is None or not starts:
            ctx.f("R26", "feces sample %s: %s, batch fed not identifiable" % (
                sid, "no unit_id" if u is None else "no collection dates"), u, rec)
            continue
        a, b = min(starts), max(ends)
        batches = sorted(r[0] for r in ctx.con.execute(
            "SELECT DISTINCT batch_id FROM feeding WHERE unit_id = ? AND date BETWEEN ? AND ? AND feed_given_g > 0",
            (u, a, b)))
        if not batches:
            ctx.f("R26", "feces sample %s: no feeding in %s..%s, batch fed not identifiable" % (sid, a, b), u, rec)
        elif len(batches) > 1:
            ctx.f("R26", "feces sample %s: %d batches fed in %s..%s (%s): flag for ADC" % (
                sid, len(batches), a, b, ", ".join(batches)), u, rec, severity="warning")


FATE_EVENT = {"dead": "mortality", "sampled": "removal_sampling", "culled": "cull"}


@rule("R27")
def r27(ctx):
    cache = {}
    for f in ctx.rows("SELECT * FROM fish WHERE trial_id = ? AND fate IN ('dead','sampled','culled')", (ctx.trial_id,)):
        rec = "fish:" + f["fish_id"]
        if f["fate_date"] is None:
            ctx.f("R27", "fate %s without fate_date" % f["fate"], f["unit_id"], rec)
            continue
        u = _fish_unit_at(ctx, f["fish_id"], d(f["fate_date"]), cache)
        n = ctx.val("SELECT count(*) FROM events WHERE event_type = ? AND date = ? AND (fish_id = ? OR"
                    " (fish_id IS NULL AND unit_id = ? AND n_fish > 0))",
                    (FATE_EVENT[f["fate"]], f["fate_date"], f["fish_id"], u))
        if not n:
            ctx.f("R27", "fate %s on %s but no %s event for the fish / its unit %s that day" % (
                f["fate"], f["fate_date"], FATE_EVENT[f["fate"]], u), u, rec)


def _no_plan(ctx, rid, what="planned_outcomes"):
    ctx.f(rid, "not checked: no plan rows (%s empty for this trial)" % what, severity="info")


@rule("R28")
def r28(ctx):
    cov = ctx.rows("SELECT cr.* FROM covariate_roles cr JOIN analysis_plans ap ON ap.plan_id = cr.plan_id"
                   " WHERE ap.trial_id = ?", (ctx.trial_id,))
    if not ctx.outcomes or not cov:
        return _no_plan(ctx, "R28", "planned_outcomes" if not ctx.outcomes else "covariate_roles")
    for o in ctx.outcomes:
        terms = rhs_terms(o["model"])
        rec = "planned_outcomes:%s/%s" % (o["plan_id"], o["outcome_label"])
        for c in cov:
            if c["plan_id"] != o["plan_id"] or c["role"] != "mediator" or c["measured"] != "post_random":
                continue
            if c["variable"] not in terms:
                continue
            if o["estimand"] == "total":
                ctx.f("R28", "post-randomisation mediator %s in the model of total-effect outcome %s (model tokenised"
                      " conservatively: any identifier on the right of '~')" % (c["variable"], o["outcome_label"]),
                      record=rec)
            elif o["estimand"] == "direct":
                ctx.f("R28", "mediator %s in direct-effect outcome %s: the plan must state the no-unmeasured-"
                      "mediator-outcome-confounding assumption" % (c["variable"], o["outcome_label"]),
                      record=rec, severity="info")
            else:
                ctx.f("R28", "mediator %s in the model of outcome %s whose estimand is not declared" % (
                    c["variable"], o["outcome_label"]), record=rec, severity="warning")


@rule("R29")
def r29(ctx):
    for (u, sid), rs in sorted(_feces(ctx).items(), key=str):
        rec = "composition:" + rs[0]["record_id"]
        did = rs[0]["diet_id"] or (ctx.units[u]["diet_id"] if u in ctx.units else None)
        dr = ctx.diets.get(did)
        if dr is None:
            ctx.f("R29", "feces sample %s: diet unknown" % sid, u, rec)
            continue
        if dr["marker"] in (None, "none"):
            ctx.f("R29", "feces sample %s: diet %s has marker none, ADC not computable" % (sid, did), u, rec)
            continue
        ma = dr["marker_analyte"]
        if ma is None:
            ctx.f("R29", "diet %s: marker_analyte not filled" % did, u, rec)
            continue
        mk = [r for r in rs if r["analyte"] == ma]
        if not mk or not all((r["value"] or 0) > 0 for r in mk):
            ctx.f("R29", "feces sample %s: no marker %s value > 0" % (sid, ma), u, rec)
        analytes = {r["analyte"] for r in rs}
        for r in rs:
            if r["basis"] == "unknown":
                ctx.f("R29", "feces %s basis unknown" % r["analyte"], u, "composition:" + r["record_id"])
        diet_rows = ctx.rows("SELECT record_id, analyte, basis FROM composition WHERE sample_type = 'diet'"
                             " AND diet_id = ? AND COALESCE(qc_flag,'raw') <> 'removed'", (did,))
        for an in sorted(analytes | {ma}):
            dr_an = [r for r in diet_rows if r["analyte"] == an]
            if not dr_an:
                ctx.f("R29", "diet %s: no composition value for %s (needed for ADC)" % (did, an), u, rec)
            for r in dr_an:
                if r["basis"] == "unknown":
                    ctx.f("R29", "diet %s %s basis unknown" % (did, an), u, "composition:" + r["record_id"])


@rule("R30")
def r30(ctx):
    fec = _feces(ctx)
    for (u, sid), rs in sorted(fec.items(), key=str):
        if any(r["feces_method"] is None for r in rs):
            ctx.f("R30", "feces sample %s has rows without feces_method: excluded from ADC" % sid, u,
                  "composition:" + rs[0]["record_id"], severity="warning")
    if not ctx.outcomes:
        return _no_plan(ctx, "R30")
    adc = [o for o in ctx.outcomes if str(o["outcome"]).startswith("adc")]
    method_of = defaultdict(set)
    for (u, sid), rs in fec.items():
        for r in rs:
            if r["feces_method"] and u:
                method_of[r["feces_method"]].add(u)
    lm = ctx.factor_level_map(("assigned_treatment", "observed_condition", "blocking"))
    for o in adc:
        rec = "planned_outcomes:%s/%s" % (o["plan_id"], o["outcome_label"])
        h = o["adc_method_handling"]
        if h is None:
            ctx.f("R30", "ADC outcome %s does not declare adc_method_handling" % o["outcome_label"], record=rec)
        elif h == "single_method" and len(method_of) > 1:
            ctx.f("R30", "ADC outcome %s declares single_method but units used %s" % (
                o["outcome_label"], ", ".join("%s (%d units)" % (m, len(v)) for m, v in sorted(method_of.items()))),
                record=rec)
        elif h == "method_adjusted":
            terms = rhs_terms(o["model"])
            if not terms & {"feces_method", "method"}:
                ctx.f("R30", "ADC outcome %s is method_adjusted but feces_method is not a model term (tokenised:"
                      " looked for 'feces_method' or 'method')" % o["outcome_label"], record=rec)
            contrasted = ["diet"] + sorted({f["name"] for f in ctx.factors.values() if f["name"] in terms})
            for m, us in sorted(method_of.items()):
                for fac in contrasted:
                    lv = {ctx.units[u]["diet_id"] if fac == "diet" else lm[u].get(fac) for u in us if u in ctx.units}
                    if len(lv) < 2:
                        ctx.f("R30", "ADC outcome %s: method %s occurs in %d level(s) of %s (need >= 2, else aliased)"
                              % (o["outcome_label"], m, len(lv), fac), record=rec)


@rule("R31")
def r31(ctx):
    cages = {u: ur for u, ur in ctx.units.items() if is_cage(ur)}
    if not cages:
        return
    dist = ctx.p["interference_distance_m"]
    pairs = []
    us = sorted(cages)
    nopos = [u for u in us if cages[u]["x_m"] is None or cages[u]["y_m"] is None]
    if nopos:
        ctx.f("R31", "cage units without position x_m / y_m (interference not checkable): %s" % ", ".join(nopos),
              severity="info")
    for i, a in enumerate(us):
        for b in us[i + 1:]:
            ra, rb = cages[a], cages[b]
            if ra["diet_id"] == rb["diet_id"] or ra["site_id"] != rb["site_id"]:
                continue
            why = None
            if None not in (ra["x_m"], ra["y_m"], rb["x_m"], rb["y_m"]):
                dd = math.hypot(ra["x_m"] - rb["x_m"], ra["y_m"] - rb["y_m"])
                if dd <= dist:
                    why = "%.0f m apart (<= %g m)" % (dd, dist)
            if why is None and ra["upstream_rank"] is not None and rb["upstream_rank"] is not None \
                    and abs(ra["upstream_rank"] - rb["upstream_rank"]) == 1:
                why = "adjacent upstream_rank"
            if why:
                pairs.append((a, b, why))
    for a, b, why in pairs:
        if not ctx.outcomes:
            ctx.f("R31", "cages %s and %s (different diets) %s: need cage_group_id in error_strata or a spatial term;"
                  " no plan rows to check" % (a, b, why), a)
            continue
        bad = [o["outcome_label"] for o in ctx.outcomes
               if "cage_group_id" not in strata_tokens(o["error_strata"]) and not rhs_terms(o["model"]) & SPATIAL_TOKENS]
        if bad:
            ctx.f("R31", "cages %s and %s (different diets) %s: outcomes without cage_group_id in error_strata or"
                  " a spatial term (tokenised): %s" % (a, b, why, ", ".join(bad)), a)
    for r in ctx.rows("SELECT DISTINCT unit_id, counterpart_unit_id FROM events WHERE event_type IN"
                      " ('transfer_in','transfer_out') AND counterpart_unit_id IS NOT NULL AND unit_id IN %s"
                      % Ctx.UNITS_SQL, (ctx.trial_id,)):
        if r["unit_id"] in cages and r["counterpart_unit_id"] in cages and r["unit_id"] < r["counterpart_unit_id"]:
            ctx.f("R31", "cages %s and %s exchanged fish: not independent" % (r["unit_id"], r["counterpart_unit_id"]),
                  r["unit_id"])


@rule("R32")
def r32(ctx):
    for p in ctx.rows("SELECT tp.permit_id, tp.site_id, ep.approved_n_fish FROM trial_permits tp JOIN ethics_permits"
                      " ep ON ep.permit_id = tp.permit_id WHERE tp.trial_id = ?", (ctx.trial_id,)):
        if p["approved_n_fish"] is None:
            continue
        us = [u for u, ur in ctx.units.items() if p["site_id"] is None or ur["site_id"] == p["site_id"]]
        if not us:
            continue
        ph = ",".join("?" * len(us))
        stocked = ctx.val("SELECT COALESCE(sum(n_fish),0) FROM weighings WHERE sampling_type = 'stocking'"
                          " AND COALESCE(qc_flag,'raw') <> 'removed' AND unit_id IN (%s)" % ph, us)
        restock = ctx.val("SELECT COALESCE(sum(n_fish),0) FROM events WHERE event_type = 'restock' AND unit_id IN (%s)"
                          % ph, us)
        tot, appr = stocked + restock, p["approved_n_fish"]
        if tot > appr:
            ctx.f("R32", "permit %s: %d fish stocked + restocked exceed approved %d" % (p["permit_id"], tot, appr))
        elif tot >= 0.9 * appr:
            ctx.f("R32", "permit %s: %d fish stocked + restocked = %.0f %% of approved %d (>= 90 %%)" % (
                p["permit_id"], tot, 100.0 * tot / appr, appr))


@rule("R33")
def r33(ctx):
    T = ctx.trial_id
    count_types = tuple(ctx.model.get("event_types") or {})
    checks = [
        ("sites.timezone", "SELECT count(*) FROM sites WHERE timezone IS NULL AND site_id IN"
         " (SELECT site_id FROM systems WHERE trial_id = ?)", (T,), "local days cannot be formed"),
        ("trials.blinding_feeders", "SELECT count(*) FROM trials WHERE trial_id = ? AND blinding_feeders IS NULL", (T,),
         "blinding unknown (ARRIVE item; risk of performance bias not assessable)"),
        ("trials.blinding_assessors", "SELECT count(*) FROM trials WHERE trial_id = ? AND blinding_assessors IS NULL",
         (T,), "blinding unknown (detection bias not assessable)"),
        ("trials.blinding_analysts", "SELECT count(*) FROM trials WHERE trial_id = ? AND blinding_analysts IS NULL",
         (T,), "blinding unknown (analysis bias not assessable)"),
        ("weighings.weight_basis", "SELECT count(*) FROM weighings WHERE weight_basis IS NULL AND unit_id IN %s"
         % Ctx.UNITS_SQL, (T,), "basis unknown, treated as round in derived weights"),
        ("events.n_fish_estimated", "SELECT count(*) FROM events WHERE n_fish_estimated IS NULL AND n_fish > 0"
         " AND event_type IN (%s) AND unit_id IN %s" % (",".join("?" * len(count_types)), Ctx.UNITS_SQL),
         count_types + (T,), "counts treated as exact; no R04 tolerance widening"),
        ("composition.basis unknown", "SELECT count(*) FROM composition WHERE basis = 'unknown' AND trial_id = ?", (T,),
         "rows excluded from ADC and retention"),
        ("composition.feces_method", "SELECT count(*) FROM composition WHERE sample_type = 'feces' AND feces_method"
         " IS NULL AND trial_id = ?", (T,), "rows excluded from ADC (R30)"),
        ("fish_scores.assessor_id", "SELECT count(*) FROM fish_scores WHERE assessor_id IS NULL AND unit_id IN %s"
         % Ctx.UNITS_SQL, (T,), "no rater random effect possible"),
    ]
    for name, sql, args, cons in checks:
        n = ctx.val(sql, args)
        if n:
            ctx.f("R33", "%s NULL on %d row(s): %s" % (name, n, cons))


@rule("R34")
def r34(ctx):
    for u in ctx.units:
        occ = ctx.occ.get(u, [])
        if not occ:
            continue
        for a, b in zip(occ, occ[1:]):
            if b["date"] < a["date"]:
                ctx.f("R34", "seq order differs from date order: %s (seq %d, %s) before %s (seq %d, %s)" % (
                    a["occasion"], a["seq"], a["occasion_date"], b["occasion"], b["seq"], b["occasion_date"]), u)
        names = [o["occasion"] for o in occ]
        rest = [n for n in names if n != "stocking"]
        if "stocking" in names and names[0] != "stocking":
            ctx.f("R34", "stocking is not the lowest seq", u)
        if "d0" in rest and rest[0] != "d0":
            ctx.f("R34", "d0 is not the lowest seq after stocking", u)
        ends = [n for n in names if n in ("final", "end_early")]
        if ends and names[-1] not in ("final", "end_early"):
            ctx.f("R34", "%s is not the highest seq (last is %s)" % (ends[0], names[-1]), u)
        if len(ends) > 1:
            ctx.f("R34", "unit has both final and end_early", u)


@rule("R35")
def r35(ctx):
    if not ctx.outcomes:
        return _no_plan(ctx, "R35")
    for o in ctx.outcomes:
        terms, strata = rhs_terms(o["model"]), strata_tokens(o["error_strata"])
        for fac in ctx.factors.values():
            if fac["role"] != "assigned_treatment" or fac["applied_at"] not in ("system", "site"):
                continue
            if fac["name"] not in terms and fac["factor_id"] not in terms:
                continue
            lvl = fac["applied_at"]
            if strata & {lvl, lvl + "_id"} or o["unit_of_analysis"] == lvl:
                continue
            ctx.f("R35", "outcome %s: %s factor %s is a model term but %s is not in error_strata '%s' (tokenised"
                  " on non-identifier characters)" % (o["outcome_label"], lvl, fac["name"], lvl, o["error_strata"] or ""),
                  record="planned_outcomes:%s/%s" % (o["plan_id"], o["outcome_label"]))


@rule("R36")
def r36(ctx):
    for u, ur in ctx.units.items():
        tr = ur["termination_reason"]
        if tr in (None, "planned_end"):
            continue
        names = {o["occasion"] for o in ctx.occ.get(u, [])}
        if "final" in names:
            ctx.f("R36", "termination_reason %s but occasion final: early-terminated units end with end_early" % tr, u,
                  "unit_occasions:%s/final" % u)
        elif "end_early" not in names:
            ctx.f("R36", "termination_reason %s but no end_early occasion" % tr, u)


@rule("R37")
def r37(ctx):
    for e in ctx.rows("SELECT record_id, unit_id, date, event_type, weight_source FROM events WHERE n_fish > 0 AND"
                      " (event_type = 'mortality' OR (event_type = 'cull' AND outcome_class = 'cull_moribund'))"
                      " AND COALESCE(weight_source, '') <> 'weighed' AND unit_id IN %s" % Ctx.UNITS_SQL,
                      (ctx.trial_id,)):
        if is_tank(ctx.units[e["unit_id"]]):
            ctx.f("R37", "%s on %s in a tank / RAS unit with weight_source %s (should be weighed)" % (
                e["event_type"], e["date"], e["weight_source"]), e["unit_id"], "events:" + e["record_id"])


@rule("R38")
def r38(ctx):
    runs = ctx.rows("SELECT * FROM analysis_runs WHERE trial_id = ?", (ctx.trial_id,))
    if not runs:
        return _no_plan(ctx, "R38", "analysis_runs")
    unbl = ctx.trial["unblinded_at"]
    for r in runs:
        if r["purpose"] != "primary_locked":
            continue
        rec = "analysis_runs:" + r["run_id"]
        if unbl is None:
            ctx.f("R38", "primary_locked run %s but trials.unblinded_at NULL: blinding of the primary analysis not"
                  " decidable" % r["run_id"], record=rec, severity="warning")
        elif r["run_at"] > unbl:
            ctx.f("R38", "primary_locked run %s at %s after unblinding %s: primary analysis is unblinded" % (
                r["run_id"], r["run_at"], unbl), record=rec)


@rule("R39")
def r39(ctx):
    rows = ctx.rows("SELECT f.rowid AS rid, f.unit_id, f.date, f.batch_id, f.diet_id, f.off_protocol, u.diet_id udiet"
                    " FROM feeding f JOIN units u ON u.unit_id = f.unit_id WHERE u.trial_id = ?"
                    " AND (f.off_protocol = 1 OR f.diet_id <> u.diet_id)", (ctx.trial_id,))
    if not rows:
        return
    devs = ctx.rows("SELECT * FROM deviations WHERE trial_id = ? AND category = 'protocol'", (ctx.trial_id,))
    named = {(x["unit_id"], x["date"]) for x in devs if x["unit_id"]}
    covered = set()
    for x in devs:
        if x["target_filter"]:
            ids, _ = ctx.filter_rowids("feeding", x["target_filter"])
            covered |= ids or set()
    for r in rows:
        if (r["unit_id"], r["date"]) in named or r["rid"] in covered:
            continue
        extra = "" if r["off_protocol"] == 1 else " (and off_protocol is not set)"
        ctx.f("R39", "feeding %s on %s: diet %s (unit diet %s) off protocol without a protocol deviation%s" % (
            r["batch_id"], r["date"], r["diet_id"], r["udiet"], extra), r["unit_id"],
            "feeding:%s/%s/%s" % (r["unit_id"], r["date"], r["batch_id"]))


@rule("R40")
def r40(ctx):
    if not need_metrics(ctx, "R40"):
        return
    lm = ctx.factor_level_map(("assigned_treatment", "observed_condition"))
    by = defaultdict(dict)
    for u in ctx.units:
        for o in ctx.occ.get(u, []):
            w = M.unit_weight(ctx.con, u, o["occasion"])
            if w and w.get("w_g") is not None:
                by[o["occasion"]][u] = w.get("w_source")
    for occ, src in sorted(by.items()):
        if len(set(src.values())) < 2:
            continue
        groups = defaultdict(set)
        for u, s in src.items():
            groups[s].add(u)
        alias = []
        for name, key in [("diet", lambda u: ctx.units[u]["diet_id"]), ("site", lambda u: ctx.units[u]["site_id"]),
                          ("system", lambda u: ctx.units[u]["system_id"])] + \
                [(f["name"], (lambda n: lambda u: lm[u].get(n))(f["name"])) for f in ctx.factors.values()]:
            lv = defaultdict(set)
            for u, s in src.items():
                lv[key(u)].add(s)
            # confounded: every level measured by one method, and the methods differ between levels
            if len(lv) > 1 and all(len(v) == 1 for v in lv.values()) and len(set().union(*lv.values())) > 1:
                alias.append(name)
        ctx.f("R40", "occasion %s: w_source differs between units of the diet contrast (%s)%s" % (
            occ, "; ".join("%s: %s" % (s, ", ".join(sorted(us))) for s, us in sorted(groups.items(), key=str)),
            "; method confounded with " + ", ".join(alias) if alias else ""))


@rule("R41")
def r41(ctx):
    band = ctx.p["temp_band_width_c"]
    temp_f = [f for f in ctx.factors.values() if f["role"] == "assigned_treatment" and "temp" in f["name"].lower()]
    for s, sr in ctx.systems.items():
        level = None
        for f in temp_f:
            for r in ctx.rows("SELECT numeric_value FROM factor_levels WHERE factor_id = ? AND system_id = ?",
                              (f["factor_id"], s)):
                level = r["numeric_value"]
        if level is None and sr["temp_control"] == "controlled":
            level = sr["target_temp_c"]
        sp = ctx.rows("SELECT * FROM system_setpoints WHERE system_id = ? AND variable = 'temp_c'", (s,))
        us = [u for u, ur in ctx.units.items() if ur["system_id"] == s]
        firsts = [ctx.first_date(u) for u in us if ctx.first_date(u)]
        if (level is None and not sp) or not firsts:
            continue
        a, b = min(firsts), max(ctx.last_date(u) for u in us if ctx.last_date(u))
        meas = ctx.daily_temps(system_id=s)
        if not meas:
            per = defaultdict(list)
            for u in us:
                for k, v in ctx.daily_temps(unit_id=u).items():
                    per[k].append(v)
            meas = {k: sum(v) / len(v) for k, v in per.items()}
        n = off = 0
        for x in days(a, b):
            if x not in meas:
                continue
            target = level
            for r in sp:
                if d(r["valid_from"]) <= x and (r["valid_to"] is None or x < d(r["valid_to"])):
                    target = None if (r["ramp_per_day"] is not None or r["reason"] in ("acclimation", "ramp")) \
                        else r["setpoint"]
            if target is None:
                continue
            n += 1
            off += abs(meas[x] - target) > band
        if n and off > 0.1 * n:
            ctx.f("R41", "system %s: measured temperature departs from the assigned level by > %g C on %d of %d days"
                  " (%.0f %% > 10 %%)" % (s, band, off, n, 100.0 * off / n))


# ---------------------------------------------------------------------------- API
def validator_rules(model):
    return [r for r in model.get("cross_table_rules", []) if "validator" in (r.get("enforced_by") or [])]


def validate(con, model, trial_id=None):
    trials = [trial_id] if trial_id else [r[0] for r in con.execute("SELECT trial_id FROM trials ORDER BY trial_id")]
    out = []
    if M is not None and hasattr(M, "use_model"):
        M.use_model(con, model)      # metrics reads event_types / defaults from the same model
    for t in trials:
        ctx = Ctx(con, model, t)
        for r in validator_rules(model):
            fn = RULES.get(r["id"])
            if fn is None:
                ctx.f(r["id"], "not implemented in tools/validate.py", severity="info")
                continue
            try:
                fn(ctx)
            except Exception as e:  # a crashing rule must never pass silently
                ctx.f(r["id"], "rule raised %s: %s" % (type(e).__name__, e), severity="error")
        if len(trials) > 1:
            for f in ctx.findings:
                f["message"] = "[%s] %s" % (t, f["message"])
        out += ctx.findings
    return out


def summary(findings):
    by_sev = Counter(f["severity"] for f in findings)
    by_rule = defaultdict(Counter)
    for f in findings:
        by_rule[f["rule"]][f["severity"]] += 1
    return {"by_severity": {s: by_sev.get(s, 0) for s in SEVERITIES},
            "by_rule": {r: dict(c) for r, c in sorted(by_rule.items())}}


def report(findings, out=sys.stdout):
    for sev in SEVERITIES:
        fs = [f for f in findings if f["severity"] == sev]
        print("== %s (%d)" % (sev.upper(), len(fs)), file=out)
        for f in sorted(fs, key=lambda f: (f["rule"], f["unit_id"] or "", f["record"] or "")):
            where = " ".join(x for x in (f["unit_id"], f["record"]) if x)
            print("  %s %s%s" % (f["rule"], f["message"], "  [%s]" % where if where else ""), file=out)
    s = summary(findings)["by_severity"]
    print("summary: %d error, %d warning, %d info" % (s["error"], s["warning"], s["info"]), file=out)


def main(argv):
    yp = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")
    if len(argv) > 2:
        from build_db import load_model
        con, model = sqlite3.connect(argv[2]), load_model(yp)
        con.execute("PRAGMA foreign_keys = ON")
    else:
        from synthetic_trial import make_trial
        con, model, _ = make_trial(yp)
    findings = validate(con, model)
    report(findings)
    return 1 if any(f["severity"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
