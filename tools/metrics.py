#!/usr/bin/env python3
"""Reference implementation of data_model.yaml `derived_metrics` (v0.9).

The YAML text is the specification; this module implements it literally. Where the text is
ambiguous the chosen reading is stated in a comment marked READING.

Conventions used throughout (YAML header):
  - ONE window [start occasion_date, end occasion_date) for every period quantity.
  - A count-changing event dated on a weighing day happens AFTER that weighing unless
    relative_to_weighing = before. Internally every event gets a position (date, 0) for
    "before" and (date, 1) otherwise; a weighing on date t sits at (t, 0.5). An event belongs
    to the period between weighings s and e iff (s, .5) < pos < (e, .5).
  - Rows with qc_flag = removed and decided record-level exclusions (deviations category
    exclusion, exclusion_scope record, decided_by NOT NULL) are dropped from every input.
  - Local dates in sites.timezone (environment / camera timestamps are UTC).
  - Period sums follow period_sum(); elsewhere a NULL input gives a NULL result.

Public interface: params, occasions, count_at, unit_weight, n_fish_d0, n_at_risk, w_interp,
event_biomass_kg, period_metrics, unit_summary, unit_day_risk, adc_pct, retention_pct,
daylength_h, period_sum.

Usage: metrics.py [data_model.yaml] [db]   (no db -> synthetic trial built in memory)
python3 stdlib only (+ build_db.load_model, which reads the YAML).
"""
import datetime as dt
import math
import os
import sqlite3
import statistics
import sys
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DEFAULT_YAML = os.path.join(os.path.dirname(HERE), "data_model.yaml")

COUNT_PRECEDENCE = ("harvest", "bulk", "stocking")
_MODELS = {}
_DEFAULT_MODEL = []
_EXCL = {}
_COLS = {}


# ---------------------------------------------------------------- model / helpers
def use_model(con, model):
    """Bind a loaded data model (event_types, outcome_class_roles, defaults) to a connection.
    Without it the repository's data_model.yaml is used."""
    _MODELS[id(con)] = model


def _model(con):
    m = _MODELS.get(id(con))
    if m is not None:
        return m
    if not _DEFAULT_MODEL:
        from build_db import load_model
        _DEFAULT_MODEL.append(load_model(DEFAULT_YAML))
    return _DEFAULT_MODEL[0]


def _deltas(con):
    return {t: v["delta"] for t, v in (_model(con).get("event_types") or {}).items()}


def _roles(con):
    return dict(_model(con).get("outcome_class_roles") or {})


def _d(x):
    if x is None or isinstance(x, dt.date):
        return x
    return dt.date.fromisoformat(x)


def _days(start, end):
    """Dates of the window [start, end)."""
    return [start + dt.timedelta(days=i) for i in range((end - start).days)]


def _local_date(ts, tz):
    t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return t.astimezone(tz).date()


def _local_dt(ts, tz):
    t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return t.astimezone(tz)


def _cols(con, table):
    key = (id(con), table)
    if key not in _COLS:
        _COLS[key] = [r[1] for r in con.execute('PRAGMA table_info("%s")' % table)]
    return _COLS[key]


def _excluded(con, table):
    """rowids of `table` hit by a decided record-level exclusion (target_record_id or
    target_filter 'table: <SQL condition>')."""
    hit = _EXCL.get(id(con))
    if hit is None or hit[0] != con.total_changes:
        tables = set(_model(con).get("tables") or {})
        out = {}
        rows = con.execute(
            "SELECT target_table, target_record_id, target_filter FROM deviations WHERE category = 'exclusion'"
            " AND exclusion_scope = 'record' AND decided_by IS NOT NULL").fetchall()
        for tt, tid, tf in rows:
            if tt in tables and tid is not None and "record_id" in _cols(con, tt):
                for (rid,) in con.execute('SELECT rowid FROM "%s" WHERE record_id = ?' % tt, (tid,)):
                    out.setdefault(tt, set()).add(rid)
            if tf and ":" in tf:
                ft, cond = tf.split(":", 1)
                ft = ft.strip()
                if ft in tables:
                    try:
                        for (rid,) in con.execute('SELECT rowid FROM "%s" WHERE %s' % (ft, cond)):
                            out.setdefault(ft, set()).add(rid)
                    except sqlite3.Error:
                        pass   # an unparsable filter is a validator finding (R03 / R15)
        hit = (con.total_changes, out)
        _EXCL[id(con)] = hit
    return hit[1].get(table, set())


def _rows(con, table, where="1", args=()):
    """Rows as dicts, qc_flag removed and decided record-level exclusions dropped."""
    qc = " AND COALESCE(qc_flag, 'raw') <> 'removed'" if "qc_flag" in _cols(con, table) else ""
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    ex = _excluded(con, table)
    return [dict(r) for r in cur.execute('SELECT rowid AS _rowid, * FROM "%s" WHERE (%s)%s' % (table, where, qc), args)
            if r["_rowid"] not in ex]


def _mean(xs):
    xs = [x for x in xs]
    if not xs or any(x is None for x in xs):
        return None
    return sum(xs) / len(xs)


def _to_g_kg(value, unit):
    """g per kg: g_kg as is, pct x10, mg_kg /1000; mj_kg / pct_of_fa / unknown are not mass."""
    if value is None or unit not in ("g_kg", "pct", "mg_kg"):
        return None
    return {"g_kg": value, "pct": value * 10.0, "mg_kg": value / 1000.0}[unit]


def _latest_revision(rows):
    """Keep the highest report_revision per (sample_id, analyte, replicate_no)."""
    best = {}
    for r in rows:
        k = (r["sample_id"], r["analyte"], r.get("replicate_no") or 1)
        rev = r.get("report_revision") or 1
        if k not in best or rev > best[k][0]:
            best[k] = (rev, r)
    return [v[1] for v in best.values()]


# ---------------------------------------------------------------- period_sum
def period_sum(values, eligible, min_coverage_pct):
    """derived_metrics.period_sum step 2. values: {day: (value or None, imputed bool)} after
    step-1 filling; eligible: list of eligible days. Returns (sum or None, coverage dict)."""
    n_el = len(eligible)
    valid = [values[d] for d in eligible if d in values and values[d][0] is not None]
    n_imp = sum(1 for v in valid if v[1])
    cov = {"n_eligible": n_el, "n_observed": len(valid) - n_imp, "n_imputed": n_imp,
           "coverage_pct": 100.0 * len(valid) / n_el if n_el else None}
    if not n_el or not valid or cov["coverage_pct"] < min_coverage_pct:
        return None, cov
    return sum(v[0] for v in valid) * n_el / len(valid), cov


# ---------------------------------------------------------------- parameters / occasions
def params(con, trial_id):
    p = dict(_model(con).get("validation_params_defaults") or {})
    for k, v in con.execute("SELECT param, value FROM validation_params WHERE trial_id = ?", (trial_id,)):
        p[k] = v
    return p


def occasions(con, unit_id):
    return [{"occasion": o, "seq": s, "date": d} for o, s, d in con.execute(
        "SELECT occasion, seq, occasion_date FROM unit_occasions WHERE unit_id = ? ORDER BY seq", (unit_id,))]


def _count_rows(con, unit_id, occasion):
    rows = _rows(con, "weighings", "unit_id = ? AND occasion = ? AND sampling_type IN ('harvest','bulk','stocking')"
                 " AND n_fish IS NOT NULL", (unit_id, occasion))
    for t in COUNT_PRECEDENCE:
        sel = [r for r in rows if r["sampling_type"] == t]
        if sel:
            return sel
    return []


def count_at(con, unit_id, occasion):
    """(n_count, B_count_kg) of the occasion's count rows, or None. B_count is None when a lot
    has no weight or weight_basis bled / gutted (NULL basis counts as round; R33 warns)."""
    sel = _count_rows(con, unit_id, occasion)
    if not sel:
        return None
    n = sum(r["n_fish"] for r in sel)
    b = 0.0
    for r in sel:
        bb = r["biomass_kg"] if r["biomass_kg"] is not None else (
            r["n_fish"] * r["weight_g"] / 1000.0 if r["weight_g"] is not None else None)
        if bb is None or r["weight_basis"] in ("bled", "gutted"):
            b = None
            break
        b += bb
    return n, b


def _calibrated(con, r, local_day):
    if r.get("calibration_id"):
        cals = _rows(con, "camera_calibrations", "calibration_id = ?", (r["calibration_id"],))
    else:
        cals = _rows(con, "camera_calibrations", "unit_id = ? AND camera_id = ? AND algorithm_version = ?",
                     (r["unit_id"], r["camera_id"], r["algorithm_version"]))
    iso = local_day.isoformat()
    cals = [c for c in cals if c["valid_from"] <= iso and (c["valid_to"] is None or iso <= c["valid_to"])]
    if not cals:
        return None
    c = max(cals, key=lambda c: c["valid_from"])
    m = r["mean_weight_g"]
    if c["model"] == "bias_only" and c["bias_g"] is not None:
        return m - c["bias_g"]
    if c["model"] == "linear" and c["slope"] is not None and c["intercept_g"] is not None:
        return c["intercept_g"] + c["slope"] * m
    return None


def _camera_weight(con, unit_id, occ_date, p, tz):
    win = p.get("camera_window_days") or 0
    mind = p.get("min_detections") or 0
    sel = []
    for r in _rows(con, "camera_estimates", "unit_id = ?", (unit_id,)):
        ld = _local_date(r["period_start"], tz)
        if abs((ld - occ_date).days) > win or r["mean_weight_g"] is None or (r["n_detections"] or 0) < mind:
            continue
        m = _calibrated(con, r, ld)
        if m is not None:
            sel.append((r["camera_id"], r["n_detections"], m))
    if any(c != "ALL" for c, _, _ in sel):      # READING: per-camera rows win over the vendor 'ALL' row
        sel = [s for s in sel if s[0] != "ALL"]
    n = sum(s[1] for s in sel)
    if not sel or n <= 0:
        return None
    return sum(s[1] * s[2] for s in sel) / n, n


def unit_weight(con, unit_id, occasion):
    """derived_metrics.unit_weight_g: count > calibrated camera > sample; round basis only."""
    u = _Unit(con, unit_id)
    return u.weight(occasion)


def n_fish_d0(con, unit_id):
    return _Unit(con, unit_id).n_d0()


def n_at_risk(con, unit_id, date_iso):
    return _Unit(con, unit_id).n_at(_d(date_iso))


def w_interp(con, unit_id, date_iso):
    return _Unit(con, unit_id).w_interp(_d(date_iso))


def event_biomass_kg(con, unit_id, record_id):
    u = _Unit(con, unit_id)
    for e in u.events:
        if e["record_id"] == record_id:
            return u.event_biomass(e)[0]
    return None


# ---------------------------------------------------------------- per-unit context
class _Unit:
    def __init__(self, con, unit_id):
        self.con = con
        self.id = unit_id
        cur = con.cursor()
        cur.row_factory = sqlite3.Row
        r = cur.execute(
            "SELECT u.unit_id, u.trial_id, u.system_id, u.diet_id, u.cohort_id, u.unit_type, u.block,"
            " u.growth_temp_sensor_id, u.termination_reason, u.end_date, s.site_id, s.reference_temp_sensor_id,"
            " s.reference_depth_m, s.target_temp_c, si.timezone, si.latitude, si.longitude"
            " FROM units u JOIN systems s ON s.system_id = u.system_id JOIN sites si ON si.site_id = s.site_id"
            " WHERE u.unit_id = ?", (unit_id,)).fetchone()
        if r is None:
            raise KeyError(unit_id)
        self.info = dict(r)
        self.tz = ZoneInfo(self.info["timezone"])
        self.p = params(con, self.info["trial_id"])
        self.deltas = _deltas(con)
        self.roles = _roles(con)
        self.occs = [dict(o, date=_d(o["date"])) for o in occasions(con, unit_id)]
        self._weights = {}
        self._counts = None
        self._events = None
        self._temp_cache = {}

    # -- occasions
    def occ(self, name):
        for o in self.occs:
            if o["occasion"] == name:
                return o
        return None

    @property
    def counts(self):
        if self._counts is None:
            self._counts = []
            for o in self.occs:
                c = count_at(self.con, self.id, o["occasion"])
                if c is not None:
                    self._counts.append(dict(o, n=c[0], b=c[1]))
        return self._counts

    @property
    def events(self):
        """Count-changing events with n_fish, position and delta."""
        if self._events is None:
            ts = list(self.deltas)
            rows = _rows(self.con, "events", "unit_id = ? AND n_fish IS NOT NULL AND event_type IN (%s)"
                         % ",".join("?" * len(ts)), [self.id] + ts) if ts else []
            for e in rows:
                e["date_"] = _d(e["date"])
                e["pos"] = (e["date_"], 0 if e["relative_to_weighing"] == "before" else 1)
                e["delta"] = self.deltas[e["event_type"]]
            self._events = sorted(rows, key=lambda e: e["pos"])
        return self._events

    def events_between(self, a, b):
        """Events strictly after the weighing on date a and before the weighing on date b."""
        lo, hi = (a, 0.5), (b, 0.5)
        return [e for e in self.events if lo < e["pos"] < hi]

    # -- counts
    def n_at(self, t):
        """n_at_risk at the weighing of date t (= start of day t on a day without weighing)."""
        cands = [c for c in self.counts if c["date"] <= t]
        if not cands:
            return None
        c = max(cands, key=lambda c: (c["date"], c["seq"]))
        if c["date"] == t:
            return c["n"]
        return c["n"] + sum(e["delta"] * e["n_fish"] for e in self.events_between(c["date"], t))

    def n_d0(self):
        d0 = self.occ("d0")
        if d0 is None:
            return None
        c = count_at(self.con, self.id, "d0")
        if c is not None:
            return c[0]
        st = self.occ("stocking")
        c = count_at(self.con, self.id, "stocking") if st else None
        if c is None:
            return None
        return c[0] + sum(e["delta"] * e["n_fish"] for e in self.events_between(st["date"], d0["date"]))

    # -- weights
    def weight(self, occasion):
        if occasion in self._weights:
            return self._weights[occasion]
        o = self.occ(occasion)
        res = None
        if o is not None:
            ind = [r["weight_g"] for r in _rows(
                self.con, "weighings", "unit_id = ? AND occasion = ? AND sampling_type IN ('individual','sample')"
                " AND weight_g IS NOT NULL AND COALESCE(weight_basis, 'round') = 'round'", (self.id, occasion))]
            sd = statistics.stdev(ind) if len(ind) >= 2 else None
            c = count_at(self.con, self.id, occasion)
            cam = None
            if c is not None and c[1] is not None and c[0] > 0:
                res = {"w_g": 1000.0 * c[1] / c[0], "sd_g": sd, "n_weight": c[0], "w_source": "count"}
            else:
                cam = _camera_weight(self.con, self.id, o["date"], self.p, self.tz)
            if res is None and cam is not None:
                res = {"w_g": cam[0], "sd_g": sd, "n_weight": cam[1], "w_source": "camera"}
            if res is None and ind:
                res = {"w_g": statistics.fmean(ind), "sd_g": sd, "n_weight": len(ind), "w_source": "sample"}
        self._weights[occasion] = res
        return res

    def _weight_points(self):
        pts = {}
        for o in self.occs:              # later seq on the same date wins
            w = self.weight(o["occasion"])
            if w is not None and w["w_g"] > 0:
                pts[o["date"]] = w["w_g"]
        return sorted(pts.items())

    def w_interp(self, d):
        pts = self._weight_points()
        for i, (od, w) in enumerate(pts):
            if od == d:
                return w
            if i and pts[i - 1][0] < d < od:
                d0, w0 = pts[i - 1]
                f = (d - d0).days / (od - d0).days
                return math.exp(math.log(w0) + f * (math.log(w) - math.log(w0)))
        return None

    def biomass_occ(self, occasion):
        """B(unit, occasion) = B_count, else n_at_risk(occasion_date) * W / 1000."""
        o = self.occ(occasion)
        if o is None:
            return None
        c = count_at(self.con, self.id, occasion)
        if c is not None and c[1] is not None:
            return c[1]
        w = self.weight(occasion)
        n = self.n_at(o["date"])
        return None if w is None or n is None else n * w["w_g"] / 1000.0

    def biomass_day(self, d):
        n, w = self.n_at(d), self.w_interp(d)
        return None if n is None or w is None else n * w / 1000.0

    def event_biomass(self, e):
        """(kg, estimated?) per derived_metrics.event_biomass_kg."""
        ws = e["weight_source"] or ""
        est = ws.startswith("estimated_")
        if e["biomass_kg"] is not None:
            return e["biomass_kg"], est
        if e["n_fish"] == 0:
            return 0.0, False
        w = None
        if ws == "estimated_interpolated":
            w = self.w_interp(e["date_"])
        elif ws == "estimated_last_mean":
            prev = [o for o in self.occs if (o["date"], 0.5) < e["pos"] and self.weight(o["occasion"])]
            if prev:
                w = self.weight(max(prev, key=lambda o: (o["date"], o["seq"]))["occasion"])["w_g"]
        if w is None:
            return None, est
        kg = e["n_fish"] * w / 1000.0
        if e["outcome_class"] in ("death", "cull_moribund"):
            kg *= self.p.get("dead_weight_ratio", 1) or 1
        return kg, True

    # -- temperature (degree_days chain)
    def _daily_values(self, rows):
        """Daily value per local day: daily_mean rows if present, else time-weighted mean."""
        byday = {}
        for r in rows:
            if r["aggregation"] in ("daily_min", "daily_max"):
                continue
            t = _local_dt(r["timestamp"], self.tz)
            byday.setdefault(t.date(), []).append((t, r["value"], r["aggregation"]))
        out = {}
        for d, rs in byday.items():
            dm = [v for _, v, a in rs if a == "daily_mean"]
            if dm:
                out[d] = sum(dm) / len(dm)
                continue
            rs.sort()
            if len(rs) == 1:
                out[d] = rs[0][1]
                continue
            day0 = rs[0][0].replace(hour=0, minute=0, second=0)
            secs = [(t - day0).total_seconds() for t, _, _ in rs]
            bounds = [0.0] + [(secs[i] + secs[i + 1]) / 2 for i in range(len(secs) - 1)] + [86400.0]
            wts = [max(bounds[i + 1] - bounds[i], 0.0) for i in range(len(rs))]
            tot = sum(wts)
            out[d] = sum(w * v for w, (_, v, _) in zip(wts, rs)) / tot if tot > 0 else statistics.fmean(v for _, v, _ in rs)
        return out

    def _system_on(self, d):
        rows = _rows(self.con, "unit_location_history", "unit_id = ? AND valid_from <= ?", (self.id, d.isoformat()))
        # READING: unit_location_history.valid_to exclusive (as fish_unit_history / system_setpoints)
        rows = [r for r in rows if r["valid_to"] is None or d.isoformat() < r["valid_to"] or r["valid_to"] == r["valid_from"]]
        if rows:
            return max(rows, key=lambda r: r["valid_from"])["system_id"]
        return self.info["system_id"]

    def temperature(self, start, end):
        """{day: (T_day, imputed)} over [start, end) via unit -> system reference -> interpolation."""
        key = (start, end)
        if key in self._temp_cache:
            return self._temp_cache[key]
        k = int(self.p.get("max_interp_days") or 0)
        lo, hi = start - dt.timedelta(days=k + 2), end + dt.timedelta(days=k + 2)
        tlo, thi = (lo - dt.timedelta(days=1)).isoformat(), (hi + dt.timedelta(days=1)).isoformat()
        urows = _rows(self.con, "environment", "unit_id = ? AND variable = 'temp_c' AND timestamp >= ? AND timestamp < ?",
                      (self.id, tlo, thi))
        gs = self.info["growth_temp_sensor_id"]
        if gs:
            urows = [r for r in urows if r["sensor_id"] == gs]
        unit_t = self._daily_values(urows)
        sys_t = {}
        cache = {}
        for d in _days(lo, hi):
            if d in unit_t:
                continue
            sid = self._system_on(d)
            if sid not in cache:
                cur = self.con.cursor()
                cur.row_factory = sqlite3.Row
                srow = cur.execute("SELECT reference_temp_sensor_id, reference_depth_m FROM systems WHERE system_id = ?",
                                   (sid,)).fetchone()
                rows = _rows(self.con, "environment", "system_id = ? AND variable = 'temp_c' AND timestamp >= ? AND timestamp < ?",
                             (sid, tlo, thi))
                if srow["reference_temp_sensor_id"] and any(r["sensor_id"] == srow["reference_temp_sensor_id"] for r in rows):
                    rows = [r for r in rows if r["sensor_id"] == srow["reference_temp_sensor_id"]]
                depths = {}
                for r in rows:
                    depths.setdefault(r["depth_m"], []).append(r)
                per_depth = {dep: self._daily_values(rs) for dep, rs in depths.items()}
                cache[sid] = (srow["reference_depth_m"], per_depth)
            ref, per_depth = cache[sid]
            avail = {dep: v[d] for dep, v in per_depth.items() if d in v}
            if not avail:
                continue
            if ref is None:      # all depths averaged
                sys_t[d] = statistics.fmean(avail.values())
            else:
                known = [dep for dep in avail if dep is not None]
                if known:
                    best = min(abs(dep - ref) for dep in known)
                    sys_t[d] = statistics.fmean(avail[dep] for dep in known if abs(dep - ref) == best)
                else:
                    sys_t[d] = statistics.fmean(avail.values())
        series = {}
        for d in _days(lo, hi):
            if d in unit_t:
                series[d] = (unit_t[d], False)
            elif d in sys_t:
                series[d] = (sys_t[d], True)
        out = {}
        known = sorted(series)
        for d in _days(start, end):
            if d in series:
                out[d] = series[d]
                continue
            prev = [x for x in known if x < d]
            nxt = [x for x in known if x > d]
            if prev and nxt:
                a, b = prev[-1], nxt[0]
                if (b - a).days - 1 <= k:
                    f = (d - a).days / (b - a).days
                    out[d] = (series[a][0] + f * (series[b][0] - series[a][0]), True)
                    continue
            out[d] = (None, False)
        self._temp_cache[key] = out
        return out

    # -- feed
    def feed(self, start, end):
        """Per-day feed data over [start, end) (feed metrics, intake, nutrient intake)."""
        con, uid = self.con, self.id
        s, e = start.isoformat(), end.isoformat()
        frows = _rows(con, "feeding", "unit_id = ? AND date >= ? AND date < ?", (uid, s, e))
        urows = _rows(con, "uneaten_feed", "unit_id = ? AND date >= ? AND date < ?", (uid, s, e))
        stops = set()
        for r in frows:
            if r["feeding_stopped"] == 1:
                stops.add(_d(r["date"]))
        for r in _rows(con, "events", "unit_id = ? AND event_type = 'feeding_stop' AND date < ? AND COALESCE(end_date, date) >= ?",
                       (uid, e, s)):
            a, b = _d(r["date"]), _d(r["end_date"] or r["date"])
            for d in _days(a, b + dt.timedelta(days=1)):
                stops.add(d)
        days = _days(start, end)
        eligible = [d for d in days if d not in stops]
        flags = set()
        if any(r["delivery_basis"] == "split_estimate" for r in frows):
            flags.add("split_estimate")
        if any(r["off_protocol"] == 1 for r in frows):
            flags.add("off_protocol")
        batches = _batches(con)
        byday = {}
        for r in frows:
            byday.setdefault(_d(r["date"]), []).append(r)
        dmcache = {}

        def dmf(bid):
            if bid not in dmcache:
                dmcache[bid] = _batch_dm(con, batches, bid, self.info["trial_id"], s, e, flags)
            return dmcache[bid]

        day = {}
        for d in eligible:
            rows = byday.get(d, [])
            fg = sum(r["feed_given_g"] for r in rows) if rows and all(r["feed_given_g"] is not None for r in rows) else None
            per_b = {}
            fed_dm = None
            if fg is not None:
                fed_dm = 0.0
                for r in rows:
                    f = dmf(r["batch_id"])
                    if f is None:
                        fed_dm = None
                        break
                    per_b[r["batch_id"]] = per_b.get(r["batch_id"], 0.0) + r["feed_given_g"] * f
                    fed_dm += r["feed_given_g"] * f
            day[d] = {"feed_given_g": fg, "fed_dm": fed_dm, "per_batch": per_b if fed_dm is not None else None}
        # uneaten feed
        elig = set(eligible)
        ur = {_d(r["date"]): r for r in urows if _d(r["date"]) in elig}
        fracs = [r["uneaten_dm_frac"] for r in ur.values() if r["uneaten_dm_frac"] is not None]
        mean_frac = statistics.fmean(fracs) if fracs else self.p.get("uneaten_dm_frac")
        for d, r in ur.items():
            f = r["uneaten_dm_frac"] if r["uneaten_dm_frac"] is not None else mean_frac
            day[d]["uneaten_dm"] = r["uneaten_wet_g"] * f if f is not None else None
        coll = [(day[d]["uneaten_dm"], day[d]["fed_dm"]) for d in ur
                if day[d].get("uneaten_dm") is not None and day[d]["fed_dm"]]
        # READING: "mean uneaten fraction" = ratio of sums over collected days (fed_dm-weighted)
        ratio = sum(a for a, _ in coll) / sum(b for _, b in coll) if coll else None
        is_tank = self.info["unit_type"] == "tank"
        for d in eligible:
            x = day[d]
            x["uneaten_imputed"] = False
            if d in ur:
                continue
            if x["fed_dm"] is None:
                x["uneaten_dm"] = None
            elif x["fed_dm"] == 0:
                x["uneaten_dm"] = 0.0
            elif is_tank and ratio is not None:
                x["uneaten_dm"] = x["fed_dm"] * ratio
                x["uneaten_imputed"] = True
            else:
                x["uneaten_dm"] = None
        return {"days": days, "eligible": eligible, "day": day, "flags": flags, "batches": batches,
                "dm_frac": dmf, "uneaten_ratio": ratio}

    def recovery(self, fd, t_mean):
        """derived_metrics.recovery for the period; returns (value or None, detail)."""
        tests = _rows(self.con, "recovery_tests", "trial_id = ? AND unit_type IS ?", (self.info["trial_id"], self.info["unit_type"]))
        tests = [t for t in tests if t["feed_in_dm_g"] and t["feed_recovered_dm_g"] is not None]
        if not tests:
            return None, "no recovery test for unit_type"
        w = {}
        for d in fd["eligible"]:
            pb = fd["day"][d]["per_batch"] or {}
            for bid, v in pb.items():
                pm = fd["batches"][bid]["pellet_mm"]
                w[pm] = w.get(pm, 0.0) + v
        if not w or sum(w.values()) <= 0:
            return None, "no feed"
        recs = {}
        for pm in w:
            cand = [t for t in tests if t["pellet_mm"] is not None]
            if pm is not None and cand:
                best = min(abs(t["pellet_mm"] - pm) for t in cand)
                cand = [t for t in cand if abs(t["pellet_mm"] - pm) == best]
            elif pm is None:
                cand = tests
            tc = [t for t in cand if t["temp_c"] is not None]
            if t_mean is not None and tc:
                band = [t for t in tc if abs(t["temp_c"] - t_mean) <= self.p["temp_band_width_c"]]
                if not band:
                    best = min(abs(t["temp_c"] - t_mean) for t in tc)
                    band = [t for t in tc if abs(t["temp_c"] - t_mean) == best]
                cand = band
            recs[pm] = statistics.fmean(t["feed_recovered_dm_g"] / t["feed_in_dm_g"] for t in cand)
        tot = sum(w.values())
        # READING: several pellet sizes in one period -> fed_dm-weighted mean of the matched recoveries
        return sum(recs[pm] * w[pm] for pm in w) / tot, recs


def _batches(con):
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return {r["batch_id"]: dict(r) for r in cur.execute("SELECT * FROM feed_batches")}


def _real_batches_like(con, batches, bid, trial_id, s, e, flags):
    """Real (analysed) batches of the placeholder's diet with the same pellet_mm fed in [s, e)."""
    b = batches[bid]
    same = [x for x in batches.values() if x["diet_id"] == b["diet_id"] and x["is_placeholder"] != 1
            and x["pellet_mm"] == b["pellet_mm"]]
    fed = {r[0] for r in con.execute(
        "SELECT DISTINCT f.batch_id FROM feeding f JOIN units u ON u.unit_id = f.unit_id WHERE u.trial_id = ?"
        " AND f.date >= ? AND f.date < ? AND COALESCE(f.qc_flag,'raw') <> 'removed'", (trial_id, s, e))}
    sel = [x for x in same if x["batch_id"] in fed]
    if not sel and same:
        # READING: no analysed batch of that pellet size fed in the period -> all analysed batches of it
        flags.add("placeholder_from_unfed_batches")
        sel = same
    return sel


def _batch_dm(con, batches, bid, trial_id, s, e, flags):
    b = batches.get(bid)
    if b is None:
        return None
    if b["is_placeholder"] == 1:
        sel = _real_batches_like(con, batches, bid, trial_id, s, e, flags)
        return _mean([x["dm_frac"] for x in sel]) if sel else None
    return b["dm_frac"]


def _c_dm(con, batches, bid, analyte, trial_id, s, e, flags):
    """C_dm(batch, analyte) in g per kg dry matter (nutrient_intake_g)."""
    b = batches.get(bid)
    if b is None:
        return None
    if b["is_placeholder"] == 1:
        sel = _real_batches_like(con, batches, bid, trial_id, s, e, flags)
        return _mean([_c_dm(con, batches, x["batch_id"], analyte, trial_id, s, e, flags) for x in sel]) if sel else None
    rows = _latest_revision(_rows(con, "composition", "sample_type = 'diet' AND batch_id = ? AND analyte = ?", (bid, analyte)))
    vals = []
    for r in rows:
        if r["value"] is None or (r["censor"] or "none") != "none":
            return None
        v = _to_g_kg(r["value"], r["value_unit"])
        if v is None:
            return None
        if r["basis"] == "dry_matter":
            vals.append(v)
        elif r["basis"] == "as_fed" and b["dm_frac"]:
            vals.append(v / b["dm_frac"])
        else:
            return None
    if not vals:
        return None
    # mean over replicate_no per sample, then over samples
    return statistics.fmean(vals)


# ---------------------------------------------------------------- period metrics
def period_metrics(con, unit_id, start_occasion, end_occasion, _u=None, analytes=("protein",)):
    u = _u or _Unit(con, unit_id)
    p = u.p
    mincov = p.get("min_coverage_pct", 90)
    so, eo = u.occ(start_occasion), u.occ(end_occasion)
    out = {"unit_id": unit_id, "start_occasion": start_occasion, "end_occasion": end_occasion, "flags": []}
    flags = set()
    if so is None or eo is None:
        out["flags"] = ["occasion_missing"]
        return out
    sd, ed = so["date"], eo["date"]
    days = _days(sd, ed)
    out.update(start_date=sd.isoformat(), end_date=ed.isoformat(), days=(ed - sd).days)
    ws, we = u.weight(start_occasion), u.weight(end_occasion)
    out["w_start_g"] = ws and ws["w_g"]
    out["w_end_g"] = we and we["w_g"]
    out["w_start_source"] = ws and ws["w_source"]
    out["w_end_source"] = we and we["w_source"]
    out["n_start"] = u.n_at(sd)
    out["n_end"] = u.n_at(ed)
    bs, be = u.biomass_occ(start_occasion), u.biomass_occ(end_occasion)
    out["b_start_kg"], out["b_end_kg"] = bs, be

    # biomass gain
    evs = [e for e in u.events_between(sd, ed) if e["n_fish"] > 0]
    gain, est, missing = None, 0.0, False
    adj = 0.0
    for e in evs:
        kg, is_est = u.event_biomass(e)
        if kg is None:
            missing = True
            continue
        adj += e["delta"] * kg
        if is_est:
            est += kg
    if bs is not None and be is not None and not missing:
        gain = be - bs - adj
    if missing:
        flags.add("event_biomass_missing")
    out["biomass_gain_kg"] = gain
    out["biomass_gain_estimated_share"] = (est / gain) if gain else None

    # SGR / temperature / TGC
    W0, W1 = out["w_start_g"], out["w_end_g"]
    out["sgr_pct_day"] = (100.0 * (math.log(W1) - math.log(W0)) / out["days"]
                          if W0 and W1 and out["days"] > 0 else None)
    temp = u.temperature(sd, ed)
    base = p.get("base_temp_c", 0) or 0
    dd_vals = {d: (None if v is None else max(0.0, v - base), imp) for d, (v, imp) in temp.items()}
    out["degree_days"], out["degree_days_coverage"] = period_sum(dd_vals, days, mincov)
    tvals = [v for v, _ in temp.values() if v is not None]
    t_mean = statistics.fmean(tvals) if tvals else None
    out["temp_mean_c"] = t_mean
    out["tgc"] = (1000.0 * (W1 ** (1 / 3) - W0 ** (1 / 3)) / out["degree_days"]
                  if W0 and W1 and out["degree_days"] else None)
    target = u.info["target_temp_c"]
    # READING: planned temperature window = system target_temp_c +- p:temp_band_width_c
    out["temp_range_ok"] = (None if target is None or t_mean is None
                            else abs(t_mean - target) <= p["temp_band_width_c"])
    if out["temp_range_ok"] is False:
        flags.add("tgc_temp_range")

    # feed
    fd = u.feed(sd, ed)
    flags |= fd["flags"]
    dayd = fd["day"]
    fg_vals = {d: (dayd[d]["feed_given_g"], False) for d in fd["eligible"]}
    out["feed_given_g"], out["feed_given_coverage"] = period_sum(fg_vals, fd["eligible"], mincov)
    bdiff = (be - bs) if (bs is not None and be is not None) else None
    mg = p.get("min_gain_kg", 0) or 0
    out["fcr_gross"] = (out["feed_given_g"] / 1000.0 / bdiff
                        if out["feed_given_g"] is not None and bdiff is not None and bdiff > mg else None)
    out["fcr_apparent"] = (out["feed_given_g"] / 1000.0 / gain
                           if out["feed_given_g"] is not None and gain else None)
    if out["fcr_apparent"] is not None and gain <= mg:
        out["fcr_apparent"] = None

    rec, rec_detail = u.recovery(fd, t_mean)
    out["recovery"] = rec
    if rec is not None and not (p.get("min_recovery", 0) < rec <= 1):
        flags.add("recovery_out_of_range")
    intake_vals = {}
    for d in fd["eligible"]:
        x = dayd[d]
        if x["fed_dm"] is None or x["uneaten_dm"] is None:
            x["intake"] = None
        elif x["uneaten_dm"] == 0:
            x["intake"] = x["fed_dm"]
        else:
            x["intake"] = None if rec is None else x["fed_dm"] - x["uneaten_dm"] / rec
        intake_vals[d] = (x["intake"], x["uneaten_imputed"])
        if x["intake"] is not None and x["intake"] < 0:
            flags.add("intake_day_negative")
    out["intake_dm_g"], cov = period_sum(intake_vals, fd["eligible"], mincov)
    out["intake_dm_coverage"] = cov
    out["fcr_biological"] = (out["intake_dm_g"] / 1000.0 / gain
                             if out["intake_dm_g"] is not None and gain is not None and gain > mg else None)

    # fish-days, feed intake % body weight
    fdays = {d: (u.n_at(d), False) for d in days}
    out["fish_days"], out["fish_days_coverage"] = period_sum(fdays, days, mincov)
    bsum = [u.biomass_day(d) for d in days]
    out["fi_pct_bw_day"] = (100.0 * out["intake_dm_g"] / (sum(bsum) * 1000.0)
                            if out["intake_dm_g"] is not None and bsum and None not in bsum and sum(bsum) > 0 else None)

    # nutrient intake (protein) and retention / PER
    out["nutrient_intake_g"], out["nutrient_intake_coverage"], out["retention_pct"] = {}, {}, {}
    for an in sorted(set(analytes) | {"protein"}):
        ni, ncov = _nutrient_intake(u, fd, intake_vals, an, sd, ed, mincov, flags)
        out["nutrient_intake_g"][an] = ni
        out["nutrient_intake_coverage"][an] = ncov
        out["retention_pct"][an] = _retention(u, sd, ed, bs, be, evs, ni, an)
    out["protein_intake_g"] = out["nutrient_intake_g"]["protein"]
    out["per"] = (gain * 1000.0 / out["protein_intake_g"]
                  if gain is not None and out["protein_intake_g"] else None)
    for k in ("degree_days", "feed_given_g", "intake_dm_g", "biomass_gain_kg"):
        if out[k] is None:
            flags.add(k + "_null")
    out["flags"] = sorted(flags)
    return out


def _nutrient_intake(u, fd, intake_vals, analyte, sd, ed, mincov, flags):
    con = u.con
    s, e = sd.isoformat(), ed.isoformat()
    cache = {}
    vals = {}
    for d in fd["eligible"]:
        x = fd["day"][d]
        it, imp = intake_vals.get(d, (None, False))
        if it is None:
            vals[d] = (None, False)
            continue
        if not x["fed_dm"]:
            vals[d] = (0.0, imp)
            continue
        tot = 0.0
        for bid, fdm in x["per_batch"].items():
            if fdm == 0:
                continue
            if bid not in cache:
                cache[bid] = _c_dm(con, fd["batches"], bid, analyte, u.info["trial_id"], s, e, flags)
            c = cache[bid]
            if c is None:
                # READING: a batch without convertible composition makes the whole sum NULL (not a missing day)
                flags.add("nutrient_composition_missing")
                return None, None
            tot += it * fdm / x["fed_dm"] * c / 1000.0
        vals[d] = (tot, imp)
    return period_sum(vals, fd["eligible"], mincov)


def _wet_g_kg(con, rows):
    """Composition rows (one analyte) -> g per kg WET, mean over replicates then samples."""
    rows = _latest_revision(rows)
    persample = {}
    for r in rows:
        if r["value"] is None or (r["censor"] or "none") != "none":
            return None
        v = _to_g_kg(r["value"], r["value_unit"])
        if v is None:
            return None
        if r["basis"] in ("wet", "as_fed"):     # READING: as_fed = wet for fish tissue
            pass
        elif r["basis"] == "dry_matter":
            dm = _latest_revision(_rows(con, "composition", "sample_id = ? AND trial_id = ? AND analyte = 'dm'",
                                        (r["sample_id"], r["trial_id"])))
            dmv = _mean([_to_g_kg(x["value"], x["value_unit"]) for x in dm]) if dm else None
            if dmv is None:
                return None
            v = v * dmv / 1000.0
        else:
            return None
        persample.setdefault(r["sample_id"], []).append(v)
    if not persample:
        return None
    return statistics.fmean(statistics.fmean(v) for v in persample.values())


def _retention(u, sd, ed, bs, be, evs, intake, analyte):
    con, p = u.con, u.p
    win = p.get("retention_sample_window_days", 0) or 0
    tid = u.info["trial_id"]
    rows = []
    if u.info["cohort_id"]:
        rows = _rows(con, "composition", "trial_id = ? AND sample_type = 'whole_body_initial' AND cohort_id = ? AND analyte = ?",
                     (tid, u.info["cohort_id"], analyte))
    if not rows:
        rows = _rows(con, "composition", "trial_id = ? AND sample_type = 'whole_body_initial' AND cohort_id IS NULL"
                     " AND unit_id IS NULL AND analyte = ?", (tid, analyte))
    c_start = _wet_g_kg(con, rows) if rows else None

    def unit_wb(types, d):
        rs = _rows(con, "composition", "unit_id = ? AND analyte = ? AND sample_type IN (%s) AND date IS NOT NULL"
                   % ",".join("?" * len(types)), [u.id, analyte] + list(types))
        rs = [r for r in rs if abs((_d(r["date"]) - d).days) <= win]
        return _wet_g_kg(con, rs) if rs else None

    c_end = unit_wb(("whole_body_final",), ed)
    if None in (c_start, c_end, bs, be) or not intake:
        return None
    num = be * c_end - bs * c_start
    for e in evs:
        kg, _ = u.event_biomass(e)
        if kg is None:
            return None
        ci = unit_wb(("whole_body_mid", "whole_body_final"), e["date_"])
        if ci is None:
            ci = (c_start + c_end) / 2
        num -= e["delta"] * kg * ci
    return 100.0 * num / intake


def retention_pct(con, unit_id, start_occasion, end_occasion, analyte="protein"):
    pm = period_metrics(con, unit_id, start_occasion, end_occasion, analytes=(analyte,))
    return (pm.get("retention_pct") or {}).get(analyte)


# ---------------------------------------------------------------- digestibility
def adc_pct(con, unit_id, analyte="protein"):
    """derived_metrics.adc_pct; analyte 'dm' gives dry-matter ADC. Mean over feces samples."""
    u = _Unit(con, unit_id)
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    diet = cur.execute("SELECT marker, marker_analyte FROM diets WHERE diet_id = ?", (u.info["diet_id"],)).fetchone()
    if diet is None or (diet["marker"] or "none") == "none" or not diet["marker_analyte"]:
        return None
    mk = diet["marker_analyte"]
    rows = _rows(con, "composition", "unit_id = ? AND sample_type = 'feces' AND feces_method IS NOT NULL"
                 " AND basis <> 'unknown'", (unit_id,))
    samples = {}
    for r in rows:
        samples.setdefault(r["sample_id"], []).append(r)
    res = []
    for sid, rs in samples.items():
        def fval(an):
            sel = _latest_revision([r for r in rs if r["analyte"] == an])
            vals = []
            for r in sel:
                v = _to_g_kg(r["value"], r["value_unit"]) if (r["censor"] or "none") == "none" else None
                if v is None:
                    return None
                if r["basis"] != "dry_matter":
                    dm = [x for x in rs if x["analyte"] == "dm"]
                    dmv = _mean([_to_g_kg(x["value"], x["value_unit"]) for x in dm]) if dm else None
                    if not dmv:
                        return None
                    v = v / (dmv / 1000.0)
                vals.append(v)
            return statistics.fmean(vals) if vals else None
        Mf = fval(mk)
        Nf = fval(analyte) if analyte != "dm" else 1.0
        if not Mf or Mf <= 0 or Nf is None:
            continue
        r0 = rs[0]
        cs = _d(r0["collection_start"] or r0["date"])
        ce = _d(r0["collection_end"] or r0["date"] or r0["collection_start"])
        if cs is None or ce is None:
            continue
        # READING: no gut-transit parameter exists; the collection window itself is used
        fd = u.feed(cs, ce + dt.timedelta(days=1))
        w = {}
        for d in fd["eligible"]:
            x = fd["day"][d]
            if not x["per_batch"] or not x["fed_dm"]:
                continue
            for bid, fdm in x["per_batch"].items():   # eaten ~ fed_dm share of the day
                w[bid] = w.get(bid, 0.0) + fdm
        if not w or sum(w.values()) <= 0:
            continue
        s, e = cs.isoformat(), (ce + dt.timedelta(days=1)).isoformat()
        flags = set()
        Md = [(_c_dm(con, fd["batches"], b, mk, u.info["trial_id"], s, e, flags), wt) for b, wt in w.items()]
        Nd = [(_c_dm(con, fd["batches"], b, analyte, u.info["trial_id"], s, e, flags), wt) for b, wt in w.items()] \
            if analyte != "dm" else [(1.0, 1.0)]
        if any(v is None for v, _ in Md + Nd):
            continue
        md = sum(v * wt for v, wt in Md) / sum(wt for _, wt in Md)
        nd = sum(v * wt for v, wt in Nd) / sum(wt for _, wt in Nd)
        if md <= 0 or nd <= 0:
            continue
        res.append(100.0 * (1 - (md / Mf) * (Nf / nd)) if analyte != "dm" else 100.0 * (1 - md / Mf))
    return statistics.fmean(res) if res else None


# ---------------------------------------------------------------- survival
def unit_day_risk(con, unit_id, _u=None):
    """Per unit-day from d0 to the end occasion (exclusive): n_at_risk at start of day, n_event,
    n_censored, n_added. Day d holds the events between the weighing slot of d and that of d+1."""
    u = _u or _Unit(con, unit_id)
    d0 = u.occ("d0")
    end = u.occ("final") or u.occ("end_early") or (u.occs[-1] if u.occs else None)
    if d0 is None or end is None:
        return []
    out = []
    for d in _days(d0["date"], end["date"]):
        row = {"date": d.isoformat(), "n_at_risk": u.n_at(d), "n_event": 0, "n_censored": 0, "n_added": 0}
        for e in u.events_between(d, d + dt.timedelta(days=1)):
            if e["delta"] > 0:
                row["n_added"] += e["n_fish"]
            else:
                role = u.roles.get(e["outcome_class"])
                if role == "event":
                    row["n_event"] += e["n_fish"]
                elif role == "censored":
                    row["n_censored"] += e["n_fish"]
                elif role == "addition":
                    row["n_added"] += e["n_fish"]
        out.append(row)
    return out


def km_survival_pct(con, unit_id, n_days, _u=None):
    s = 1.0
    for r in unit_day_risk(con, unit_id, _u)[:n_days]:
        if r["n_event"] and r["n_at_risk"]:
            s *= 1 - r["n_event"] / r["n_at_risk"]
    return 100.0 * s


# ---------------------------------------------------------------- daylength
def daylength_h(lat, lon, date):
    """Natural (-0.833 deg) and civil (-6 deg) daylength; NOAA solar declination at local solar noon."""
    if lat is None:
        return {"natural_h": None, "civil_h": None, "polar_day": None, "polar_night": None}
    d = _d(date)
    lon = lon or 0.0
    jd = d.toordinal() + 1721424.5 + 0.5 - lon / 360.0     # Julian day at local solar noon
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
    M = math.radians(357.52911 + T * (35999.05029 - 0.0001537 * T))
    C = (math.sin(M) * (1.914602 - T * (0.004817 + 0.000014 * T)) + math.sin(2 * M) * (0.019993 - 0.000101 * T)
         + math.sin(3 * M) * 0.000289)
    om = math.radians(125.04 - 1934.136 * T)
    lam = math.radians(L0 + C - 0.00569 - 0.00478 * math.sin(om))
    eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(om))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    phi = math.radians(lat)

    def length(alt):
        num = math.sin(math.radians(alt)) - math.sin(phi) * math.sin(dec)
        den = math.cos(phi) * math.cos(dec)
        if abs(den) < 1e-12:
            return (24.0, "day") if num < 0 else (0.0, "night")
        c = num / den
        if c <= -1:
            return 24.0, "day"
        if c >= 1:
            return 0.0, "night"
        return 2 * math.degrees(math.acos(c)) / 15.0, None

    nat, flag = length(-0.833)
    civ, _ = length(-6.0)
    return {"natural_h": nat, "civil_h": civ, "polar_day": flag == "day", "polar_night": flag == "night"}


# ---------------------------------------------------------------- unit summary (ITT dataset)
def unit_analysis_status(con, unit_id, _u=None):
    """derived_metrics.unit_analysis_status for one unit."""
    u = _u or _Unit(con, unit_id)
    d0 = u.occ("d0")
    endo = u.occ("final") or u.occ("end_early")
    dev = con.execute("SELECT decided_by FROM deviations WHERE unit_id = ? AND category = 'exclusion'"
                      " AND exclusion_scope = 'unit'", (unit_id,)).fetchall()
    return {"has_d0": d0 is not None, "has_final": u.occ("final") is not None, "itt": d0 is not None,
            "termination_reason": u.info["termination_reason"], "n_occasions": len(u.occs),
            "end_occasion": endo and endo["occasion"],
            "follow_up_days": (endo["date"] - d0["date"]).days if d0 and endo else None,
            "w_source_by_occasion": {o["occasion"]: (u.weight(o["occasion"]) or {}).get("w_source") for o in u.occs},
            "excluded_decided": any(x[0] is not None for x in dev),
            "exclusion_pending": any(x[0] is None for x in dev)}


def _common_day(con, trial_id):
    fu = []
    for (uid,) in con.execute("SELECT unit_id FROM units WHERE trial_id = ?", (trial_id,)):
        st = unit_analysis_status(con, uid)
        if st["itt"] and st["follow_up_days"] is not None:
            fu.append(st["follow_up_days"])
    return min(fu) if fu else None


def survival(con, unit_id, common_day=None, _u=None):
    """(survival_pct, method). Simple formula for planned_end units without additions/transfers,
    else Kaplan-Meier from unit_day_risk at the common day (min follow-up over the trial's ITT units)."""
    u = _u or _Unit(con, unit_id)
    st = unit_analysis_status(con, unit_id, u)
    if not st["itt"] or st["follow_up_days"] is None:
        return None, None
    # READING (accepted): termination_reason NULL with a final occasion counts as planned_end (R36)
    planned = st["termination_reason"] == "planned_end" or (st["termination_reason"] is None and st["has_final"])
    d0, fin = u.occ("d0"), u.occ("final")
    simple = planned and fin is not None and not any(
        e["n_fish"] > 0 and (e["delta"] > 0 or e["outcome_class"] == "transfer")
        for e in u.events_between(d0["date"], fin["date"]))
    n0 = u.n_d0()
    if simple and n0:
        n_ev = sum(e["n_fish"] for e in u.events_between(d0["date"], fin["date"])
                   if u.roles.get(e["outcome_class"]) == "event" and e["delta"] < 0)
        return 100.0 * (1 - n_ev / n0), "simple"
    if common_day is None:
        common_day = _common_day(con, u.info["trial_id"])
    if not common_day:
        return None, None
    return km_survival_pct(con, unit_id, common_day, u), "kaplan_meier_day_%d" % common_day


def survival_pct(con, unit_id, common_day=None):
    return survival(con, unit_id, common_day)[0]


def unit_summary(con, trial_id=None):
    """One row per unit: ITT analysis dataset d0 -> final (or end_early)."""
    q = "SELECT unit_id FROM units" + (" WHERE trial_id = ?" if trial_id else "") + " ORDER BY unit_id"
    uids = [r[0] for r in con.execute(q, (trial_id,) if trial_id else ())]
    rows = []
    ctx = {}
    for uid in uids:
        u = _Unit(con, uid)
        ctx[uid] = u
        r = {"trial_id": u.info["trial_id"], "unit_id": uid, "system_id": u.info["system_id"],
             "diet_id": u.info["diet_id"], "unit_type": u.info["unit_type"], "block": u.info["block"]}
        for name, level in con.execute(
                "SELECT f.name, l.level FROM factor_levels l JOIN design_factors f ON f.factor_id = l.factor_id"
                " WHERE l.unit_id = ? OR l.system_id = ?", (uid, u.info["system_id"])):
            r["factor_" + name] = level
        r.update(unit_analysis_status(con, uid, u))
        r["n_fish_d0"] = u.n_d0()
        if r["has_d0"] and r["end_occasion"]:
            pm = period_metrics(con, uid, "d0", r["end_occasion"], _u=u)
            r.update({k: v for k, v in pm.items() if k not in ("unit_id",)})
        r["adc_pct"] = {"protein": adc_pct(con, uid, "protein"), "dm": adc_pct(con, uid, "dm")}
        r["hsi_pct"], r["vsi_pct"] = hsi_pct(con, uid), vsi_pct(con, uid)
        r["gsi_pct"], r["fillet_yield_pct"] = gsi_pct(con, uid), fillet_yield_pct(con, uid)
        rows.append(r)
    commons = {}
    for r in rows:
        t = r["trial_id"]
        if t not in commons:
            fu = [x["follow_up_days"] for x in rows if x["trial_id"] == t and x["itt"] and x["follow_up_days"] is not None]
            commons[t] = min(fu) if fu else None
        r["survival_pct"], r["survival_method"] = survival(con, r["unit_id"], commons[t], ctx[r["unit_id"]])
    return rows


# ---------------------------------------------------------------- remaining derived metrics
def day_since_d0(con, unit_id, occasion):
    os_ = {o["occasion"]: _d(o["date"]) for o in occasions(con, unit_id)}
    return (os_[occasion] - os_["d0"]).days if occasion in os_ and "d0" in os_ else None


def count_rows(con, unit_id, occasion):
    """Rows of the highest-precedence count type at the occasion (all lots)."""
    return [{k: v for k, v in r.items() if k != "_rowid"} for r in _count_rows(con, unit_id, occasion)]


def biomass_kg(con, unit_id, occasion=None, date_iso=None):
    """B(unit, occasion) if occasion is given, else B(unit, d) = n_at_risk(d) * w_interp(d) / 1000."""
    u = _Unit(con, unit_id)
    return u.biomass_occ(occasion) if occasion is not None else u.biomass_day(_d(date_iso))


def _pm(key, sub=None):
    def f(con, unit_id, start_occasion, end_occasion):
        v = period_metrics(con, unit_id, start_occasion, end_occasion,
                           analytes=(sub,) if sub else ("protein",)).get(key)
        return v.get(sub) if sub and isinstance(v, dict) else v
    f.__name__ = key
    f.__doc__ = "period_metrics(...)['%s'] between two occasions" % key
    return f


biomass_gain_kg = _pm("biomass_gain_kg")
sgr_pct_day = _pm("sgr_pct_day")
degree_days = _pm("degree_days")
tgc = _pm("tgc")
recovery = _pm("recovery")
intake_dm_g = _pm("intake_dm_g")
fcr_gross = _pm("fcr_gross")
fcr_apparent = _pm("fcr_apparent")
fcr_biological = _pm("fcr_biological")
fish_days = _pm("fish_days")
fi_pct_bw_day = _pm("fi_pct_bw_day")
protein_intake_g = _pm("protein_intake_g")
per = _pm("per")


def nutrient_intake_g(con, unit_id, start_occasion, end_occasion, analyte="protein"):
    return _pm("nutrient_intake_g", analyte)(con, unit_id, start_occasion, end_occasion)


def condition_k(con, unit_id, occasion):
    """Mean of 100 * weight_g / fork_length_cm^3 over per-fish rows (READING: 'individual rows' =
    sampling_type individual or sample, i.e. one row per fish), round basis (NULL counts as round)."""
    rows = _rows(con, "weighings", "unit_id = ? AND occasion = ? AND sampling_type IN ('individual','sample')"
                 " AND weight_g IS NOT NULL AND fork_length_cm > 0 AND COALESCE(weight_basis, 'round') = 'round'",
                 (unit_id, occasion))
    ks = [100.0 * r["weight_g"] / r["fork_length_cm"] ** 3 for r in rows]
    return statistics.fmean(ks) if ks else None


def density_kg_m3(con, unit_id, date_iso):
    """B(unit, d) / volume at d (unit_geometry row valid at d, valid_to exclusive, else units.volume_m3)."""
    u = _Unit(con, unit_id)
    d = _d(date_iso).isoformat()
    g = [r for r in _rows(con, "unit_geometry", "unit_id = ? AND valid_from <= ?", (unit_id, d))
         if r["valid_to"] is None or d < r["valid_to"]]
    vol = max(g, key=lambda r: r["valid_from"])["volume_m3"] if g else \
        con.execute("SELECT volume_m3 FROM units WHERE unit_id = ?", (unit_id,)).fetchone()[0]
    b = u.biomass_day(_d(d))
    return None if b is None or not vol else b / vol


def lice_af_per_fish(con, unit_id, date_iso=None, start_iso=None, end_iso=None):
    """Per date: sum(adult_female) / sum(n_fish_sampled). With start/end: mean over the dates in
    [start, end). A date with any NULL adult_female among sampled rows is NULL (NULL input rule)."""
    def one(d):
        rs = [r for r in _rows(con, "lice_counts", "unit_id = ? AND date = ?", (unit_id, d)) if r["n_fish_sampled"] > 0]
        if not rs or any(r["adult_female"] is None for r in rs):
            return None
        return sum(r["adult_female"] for r in rs) / sum(r["n_fish_sampled"] for r in rs)
    if date_iso is not None:
        return one(date_iso)
    dates = sorted({r["date"] for r in _rows(con, "lice_counts", "unit_id = ? AND date >= ? AND date < ?",
                                              (unit_id, start_iso, end_iso))})
    vals = [one(d) for d in dates]
    vals = [v for v in vals if v is not None]
    return statistics.fmean(vals) if vals else None


def _organ(con, unit_id, f):
    rows = _rows(con, "fish_measurements", "unit_id = ? AND body_weight_g > 0", (unit_id,))
    vals = [v for v in (f(r) for r in rows) if v is not None]
    return statistics.fmean(vals) if vals else None


def hsi_pct(con, unit_id):
    """Unit mean of per-fish 100 * liver_g / body_weight_g."""
    return _organ(con, unit_id, lambda r: None if r["liver_g"] is None else 100.0 * r["liver_g"] / r["body_weight_g"])


def _vsi(r):
    if r["viscera_g"] is None or r["viscera_includes_liver"] is None:
        return None
    if r["viscera_includes_liver"] == 1:
        return 100.0 * r["viscera_g"] / r["body_weight_g"]
    return None if r["liver_g"] is None else 100.0 * (r["viscera_g"] + r["liver_g"]) / r["body_weight_g"]


def vsi_pct(con, unit_id):
    """Unit mean of per-fish 100 * (viscera_g + (viscera_includes_liver ? 0 : liver_g)) / body_weight_g."""
    return _organ(con, unit_id, _vsi)


def gsi_pct(con, unit_id):
    return _organ(con, unit_id, lambda r: None if r["gonad_g"] is None else 100.0 * r["gonad_g"] / r["body_weight_g"])


def fillet_yield_pct(con, unit_id):
    return _organ(con, unit_id, lambda r: None if r["fillet_g"] is None else 100.0 * r["fillet_g"] / r["body_weight_g"])


_DOSE_UNIT_FROM_G_KG = {"g_kg": 1.0, "mg_kg": 1000.0, "pct": 0.1}


def dose_realised(con, diet_id):
    """C_dm(batch, dose_analyte) weighted by fed_dm over all feeding rows of the diet in the trial,
    converted to dose_basis (total_dm as is; total_as_fed x dm_frac(batch), batch by batch before
    weighting) and to dose_unit (g_kg, mg_kg or pct). dose_basis 'added' is not derivable from
    composition -> NULL (R21)."""
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    dt_ = cur.execute("SELECT trial_id, dose_analyte, dose_basis, dose_unit FROM diets WHERE diet_id = ?",
                      (diet_id,)).fetchone()
    if dt_ is None or not dt_["dose_analyte"] or dt_["dose_basis"] not in ("total_dm", "total_as_fed") \
            or dt_["dose_unit"] not in _DOSE_UNIT_FROM_G_KG:
        return None
    frows = _rows(con, "feeding", "diet_id = ? AND feed_given_g > 0", (diet_id,))
    if not frows:
        return None
    s = min(r["date"] for r in frows)
    e = (_d(max(r["date"] for r in frows)) + dt.timedelta(days=1)).isoformat()
    batches = _batches(con)
    flags = set()
    num = den = 0.0
    for bid in sorted({r["batch_id"] for r in frows}):
        dm = _batch_dm(con, batches, bid, dt_["trial_id"], s, e, flags)
        c = _c_dm(con, batches, bid, dt_["dose_analyte"], dt_["trial_id"], s, e, flags)
        if dm is None or c is None:
            return None
        fed_dm = sum(r["feed_given_g"] for r in frows if r["batch_id"] == bid) * dm
        v = c if dt_["dose_basis"] == "total_dm" else c * dm
        num += v * fed_dm
        den += fed_dm
    return num / den * _DOSE_UNIT_FROM_G_KG[dt_["dose_unit"]] if den > 0 else None


def nh3_n_mg_l(tan_n_mg_l, ph, temp_c, salinity_ppt=0.0):
    """Unionised ammonia-N (mg/L) = TAN-N * f, f = 1 / (1 + 10^(pKa - pH)).
    Freshwater pKa: Emerson, Russo, Lund & Thurston (1975) J. Fish. Res. Board Can. 32:2379,
        pKa = 0.09018 + 2729.92 / T,  T in K.
    Salinity: ionic-strength correction of Khoo, Culberson & Bates (1977) J. Solution Chem. 6:281
    (the form used to reproduce Bower & Bidwell 1978 seawater tables),
        pKa_s = pKa + (0.1552 - 0.0003142 * T) * I,  I = 19.9273 * S / (1000 - 1.005109 * S)  (S in ppt).
    NULL input -> NULL (salinity NULL is NOT read as 0)."""
    if None in (tan_n_mg_l, ph, temp_c, salinity_ppt):
        return None
    T = temp_c + 273.15
    pka = 0.09018 + 2729.92 / T
    ionic = 19.9273 * salinity_ppt / (1000.0 - 1.005109 * salinity_ppt)
    pka += (0.1552 - 0.0003142 * T) * ionic
    return tan_n_mg_l / (1.0 + 10.0 ** (pka - ph))


def unit_weight_g(con, unit_id, occasion):
    w = unit_weight(con, unit_id, occasion)
    return w and w["w_g"]


METRICS = {
    "period_sum": period_sum, "day_since_d0": day_since_d0, "count_rows": count_rows,
    "unit_weight_g": unit_weight_g, "w_interp": w_interp, "n_fish_d0": n_fish_d0, "n_at_risk": n_at_risk,
    "unit_day_risk": unit_day_risk, "biomass_kg": biomass_kg, "event_biomass_kg": event_biomass_kg,
    "biomass_gain_kg": biomass_gain_kg, "sgr_pct_day": sgr_pct_day, "degree_days": degree_days, "tgc": tgc,
    "condition_k": condition_k, "density_kg_m3": density_kg_m3, "recovery": recovery, "intake_dm_g": intake_dm_g,
    "fcr_gross": fcr_gross, "fcr_apparent": fcr_apparent, "fcr_biological": fcr_biological, "fish_days": fish_days,
    "fi_pct_bw_day": fi_pct_bw_day, "nutrient_intake_g": nutrient_intake_g, "protein_intake_g": protein_intake_g,
    "dose_realised": dose_realised, "retention_pct": retention_pct, "per": per, "adc_pct": adc_pct,
    "hsi_pct": hsi_pct, "vsi_pct": vsi_pct, "gsi_pct": gsi_pct, "fillet_yield_pct": fillet_yield_pct,
    "lice_af_per_fish": lice_af_per_fish, "survival_pct": survival_pct,
    "unit_analysis_status": unit_analysis_status, "daylength_h": daylength_h, "nh3_n_mg_l": nh3_n_mg_l,
}


# ---------------------------------------------------------------- CLI
COLUMNS = [("unit_id", "%-6s"), ("diet_id", "%-4s"), ("factor_temperature", "%-5s"), ("n_fish_d0", "%4s"),
           ("n_end", "%4s"), ("w_start_g", "%7.1f"), ("w_end_g", "%7.1f"), ("w_end_source", "%-6s"),
           ("b_start_kg", "%7.3f"), ("b_end_kg", "%7.3f"), ("biomass_gain_kg", "%7.3f"), ("sgr_pct_day", "%6.3f"),
           ("degree_days", "%7.1f"), ("tgc", "%6.3f"), ("feed_given_g", "%8.0f"), ("intake_dm_g", "%8.0f"),
           ("recovery", "%5.3f"), ("fcr_gross", "%5.3f"), ("fcr_biological", "%5.3f"), ("protein_intake_g", "%7.0f"),
           ("per", "%5.2f"), ("survival_pct", "%5.1f")]


def print_table(rows):
    print("  ".join(c for c, _ in COLUMNS) + "  ret_prot  adc_prot  adc_dm  dd_cov  intake_cov  flags")
    for r in rows:
        cells = []
        for c, f in COLUMNS:
            v = r.get(c)
            cells.append("NA" if v is None else (f % v))
        ddc = (r.get("degree_days_coverage") or {})
        ic = (r.get("intake_dm_coverage") or {})
        ret = (r.get("retention_pct") or {}).get("protein")
        cells += ["NA" if ret is None else "%.1f" % ret,
                  "NA" if r["adc_pct"]["protein"] is None else "%.2f" % r["adc_pct"]["protein"],
                  "NA" if r["adc_pct"]["dm"] is None else "%.2f" % r["adc_pct"]["dm"],
                  "%s/%s+%s" % (ddc.get("n_eligible"), ddc.get("n_observed"), ddc.get("n_imputed")),
                  "%s/%s+%s" % (ic.get("n_eligible"), ic.get("n_observed"), ic.get("n_imputed")),
                  ",".join(r.get("flags") or []) + (" PENDING_EXCLUSION" if r["exclusion_pending"] else "")]
        print("  ".join(cells))


def main(argv):
    yp = argv[1] if len(argv) > 1 else DEFAULT_YAML
    if len(argv) > 2:
        from build_db import load_model
        con = sqlite3.connect(argv[2])
        use_model(con, load_model(yp))
    else:
        from synthetic_trial import make_trial
        con, model, _ = make_trial(yp)
        use_model(con, model)
    print_table(unit_summary(con))


if __name__ == "__main__":
    main(sys.argv)
