#!/usr/bin/env python3
"""Consistency test of the dashboard: the page shows what the tools computed, and nothing else.

  1. the data embedded in the page (script 'payload') equal what collect() returned, and their
     analysis part equals a fresh analysis.run_all() on the same trial
  2. every number printed through Page.n / Page.p carries its unrounded value: the printed text
     must equal that value in the stated format, and the value must occur in the payload (as
     is, or as a percentage of a payload value)
  3. two builds of the same trial give the same file, byte for byte
  4. the HTML is well formed without repeated ids, every section and every link target exists,
     every expected chart is present, nothing is loaded from outside the file, and neither a
     missing value nor a directory of the build machine leaks into the page

Usage: test_dashboard.py [data_model.yaml]        (needs matplotlib, numpy, scipy)
"""
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analysis as A  # noqa: E402
import build_dashboard as B  # noqa: E402
from synthetic_trial import make_trial  # noqa: E402

YAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "data_model.yaml")
VOID = {"meta", "br", "hr", "img", "input", "link", "use", "path", "rect", "circle", "line", "polyline", "polygon", "stop"}
FAILS, N = [], [0]


def check(text, ok, detail=""):
    N[0] += 1
    print("%s %s%s" % ("ok  " if ok else "FAIL", text, (" [%s]" % detail) if detail else ""))
    if not ok:
        FAILS.append("%s [%s]" % (text, detail))


class Reader(HTMLParser):
    """Collects the number spans, ids, link targets and the tag balance of the page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.unbalanced, self.ids, self.hrefs, self.external, self.dup = [], [], set(), [], [], []
        self.spans, self.cur, self.text, self.skip = [], None, [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            if a["id"] in self.ids:
                self.dup.append(a["id"])
            self.ids.add(a["id"])
        for k in ("href", "src", "xlink:href"):
            if k in a:
                (self.external if re.match(r"(https?:)?//", a[k]) else self.hrefs).append(a[k])
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "span" and a.get("class") == "n":
            self.cur = {"v": a["data-v"], "f": a["data-f"], "text": ""}
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            if a["id"] in self.ids:
                self.dup.append(a["id"])
            self.ids.add(a["id"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1
        if tag == "span" and self.cur is not None:
            self.spans.append(self.cur)
            self.cur = None
        if tag in VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.unbalanced.append(tag)

    def handle_data(self, data):
        if self.cur is not None:
            self.cur["text"] += data
        if not self.skip:
            self.text.append(data)


def numbers(x, out):
    if isinstance(x, bool):
        return
    if isinstance(x, (int, float)):
        out.append(float(x))
    elif isinstance(x, dict):
        for v in x.values():
            numbers(v, out)
    elif isinstance(x, list):
        for v in x:
            numbers(v, out)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        f1, f2 = os.path.join(tmp, "a.html"), os.path.join(tmp, "b.html")
        p, page = B.build(YAML, None, f1)
        B.build(YAML, None, f2)
        with open(f1, "rb") as a, open(f2, "rb") as b:
            same = a.read() == b.read()
    check("two builds of the same trial are identical byte for byte", same, "%d kB" % (len(page.encode("utf-8")) // 1024))
    m = re.search(r'<script type="application/json" id="payload">(.*?)</script>', page, re.S)
    emb = json.loads(m.group(1).replace("<\\/", "</")) if m else None
    check("the data embedded in the page equal the data the page was built from", emb == json.loads(json.dumps(p)))
    con, model, _ = make_trial(YAML, seed=p["seed"])
    fresh = A.run_all(con, model)
    check("the analysis part of those data equals a fresh analysis.run_all() on the same trial",
          emb is not None and emb["analysis"] == json.loads(json.dumps(fresh)),
          "output hash %s" % fresh["run_record"]["output_hash"][:12])
    r = Reader()
    r.feed(page)
    r.close()
    check("the HTML is well formed: every tag is closed in order and no id is used twice", not r.unbalanced and not r.stack and not r.dup,
          "%d ids; problems: %s %s %s" % (len(r.ids), r.unbalanced[:5], r.stack[:5], r.dup[:5]))
    bad = []
    for s in r.spans:
        v = float(s["v"])
        want = "< 0.0001" if s["f"] == "p<" else s["f"] % (int(round(v)) if s["f"].endswith("d") else v)
        if s["text"].strip() != want or (s["f"] == "p<" and not v < 0.0001) or (s["f"].endswith("d") and abs(v - round(v)) > 1e-9):
            bad.append((s["text"], s["v"], s["f"]))
    check("every printed number equals its unrounded value in its stated format", not bad and len(r.spans) > 300,
          "%d numbers on the page%s" % (len(r.spans), "; first mismatches %s" % bad[:3] if bad else ""))
    pool = []
    numbers(emb, pool)
    pool = sorted(set(pool))
    import bisect

    def known(v):
        for cand in (v, v / 100.0):
            i = bisect.bisect_left(pool, cand - 1e-9 * max(1.0, abs(cand)))
            if i < len(pool) and abs(pool[i] - cand) <= 1e-9 * max(1.0, abs(cand)):
                return True
        return False

    orphan = sorted({s["v"] for s in r.spans if not known(float(s["v"]))})
    check("every printed number occurs in the embedded data, as it is or as a percentage of a value there", not orphan,
          "%d distinct values checked%s" % (len({s["v"] for s in r.spans}), "; not found: %s" % orphan[:8] if orphan else ""))
    want_ids = {s for s, _ in B.SECTIONS}
    check("all %d sections are present and every link on the page points to an element that exists" % len(want_ids),
          want_ids <= r.ids and all(h[1:] in r.ids for h in r.hrefs if h.startswith("#")),
          "missing: %s" % sorted((want_ids - r.ids) | {h for h in r.hrefs if h.startswith("#") and h[1:] not in r.ids}))
    figs = set(re.findall(r'<figure class="fig" id="fig-(\w+)"><svg', page))
    want = {"growth", "sgr", "fish", "feed", "survival", "temperature", "coverage", "findings", "cells", "forest", "random", "power"}
    if p["evidence"] and "growth_diet_effect" in (p["evidence"].get("seeds") or {}):
        want |= {"seeds", "null"}
    check("every expected chart is on the page as inline SVG", figs == want, "%d charts; difference: %s" % (len(figs), sorted(figs ^ want)))
    check("nothing is loaded from outside the file: no external link, image, style sheet or script", not r.external
          and not re.search(r"<script(?![^>]*application/json)", page) and "<link" not in page, str(r.external[:3]))
    text = " ".join(r.text)
    leak = re.findall(r"\b(nan|None|inf|NaN)\b", text)
    check("no missing value leaks into the text as nan, None or inf", not leak, str(leak[:5]))
    roots = {x for root in (HERE, os.path.dirname(HERE), os.path.expanduser("~")) for x in (root, root.replace("\\", "/"), json.dumps(root)[1:-1])}
    check("no directory of the machine that built the page appears in it (text or embedded data)", not any(x in page for x in roots))
    out = p["analysis"]["outcomes"]
    prim = [lb for lb, o in out.items() if o["plan"].get("role") == "primary"][0]
    c = [x for x in out[prim]["contrasts"] if x["term"] == "diet"][0]
    check("the primary result in the overview is the diet contrast of the primary planned outcome",
          ("<b>%s</b>" % B.Page.n(c["estimate"], "%.3f")) in page and ("<b>%s</b>" % B.Page.p(c["p"])) in page,
          "%s: %.3f, p %.3f" % (prim, c["estimate"], c["p"]))
    if FAILS:
        print("FAILURES:")
        for f in FAILS:
            print("  " + f)
        print("FAILED: %d problem(s)" % len(FAILS))
        return 1
    print("%d checks, 0 failed; %d numbers on the page traced to the data" % (N[0], len(r.spans)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
