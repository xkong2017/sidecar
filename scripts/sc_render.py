#!/usr/bin/env python3
"""sc_render - one self-contained HTML page showing what the sidecar knows and did.

Written to ~/.sidecar/status.html. No external assets, no server, no network
requests: open it from disk or leave it open and re-render on a timer.

The point of the page is to make the machine's judgement legible. If the sidecar
declines to work, the reason is on the page - a system that silently does
nothing is indistinguishable from a broken one.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_budget  # noqa: E402
import sc_common as C  # noqa: E402

BACKLOG = os.path.join(C.STATE_DIR, "backlog.json")
STATUS_HTML = os.path.join(C.SIDECAR_HOME, "status.html")

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e9ef;--dim:#8b93a7;
--green:#3fb950;--amber:#d29922;--red:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,-apple-system,"SF Pro Text",Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px;font-weight:600;letter-spacing:-.01em}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);
margin:32px 0 10px;font-weight:600}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--dim);font-size:11.5px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:22px;font-weight:600;margin-top:5px;font-variant-numeric:tabular-nums}
.card .n{color:var(--dim);font-size:12px;margin-top:3px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;
font-weight:600;letter-spacing:.03em}
.green{background:rgba(63,185,80,.14);color:var(--green)}
.amber{background:rgba(210,153,34,.16);color:var(--amber)}
.red{background:rgba(248,81,73,.14);color:var(--red)}
.grey{background:rgba(139,147,167,.14);color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11.5px;
text-transform:uppercase;letter-spacing:.05em;padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid rgba(38,43,54,.55);vertical-align:top}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--dim)}
.tier{display:inline-block;min-width:20px;text-align:center;padding:1px 6px;border-radius:5px;
font-size:11px;font-weight:700;background:rgba(88,166,255,.14);color:var(--accent)}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:7px}
.bar i{display:block;height:100%;background:var(--accent)}
ul{margin:6px 0 0;padding-left:18px;color:var(--dim)}
li{margin:3px 0}
.q{color:var(--fg);background:rgba(255,255,255,.03);border-left:2px solid var(--line);
padding:6px 10px;border-radius:0 6px 6px 0;margin:5px 0;font-size:12.5px}
.foot{color:var(--dim);font-size:11.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:14px}
.empty{color:var(--dim);font-style:italic;padding:10px 0}
"""


def esc(v):
    return html.escape(str(v if v is not None else ""))


def pill(text, tone):
    return '<span class="pill %s">%s</span>' % (tone, esc(text))


def card(key, value, note=""):
    return ('<div class="card"><div class="k">%s</div><div class="v">%s</div>'
            '<div class="n">%s</div></div>' % (esc(key), esc(value), esc(note)))


def read_probe():
    path = os.path.join(C.STATE_DIR, "last_probe.json")
    return C.read_json(path, {}) or {}


def build():
    cfg = C.load_config()
    backlog = C.read_json(BACKLOG, {}) or {}
    profile = C.read_json(os.path.join(C.STATE_DIR, "profile.json"), {}) or {}
    probe = read_probe()
    led = sc_budget.load_ledger()

    budget = cfg["budget"]
    reserve = float(budget.get("reserve_fraction", 0.0))
    today = sc_budget.bucket(led, "days", C.day_key())
    week = sc_budget.bucket(led, "weeks", C.iso_week_key())
    usable_usd = budget["daily_total_usd"] * (1 - reserve)
    usable_min = budget["daily_wall_clock_min"] * (1 - reserve)

    verdict = probe.get("verdict", "unknown")
    tone = {"green": "green", "yellow": "amber", "red": "red"}.get(verdict, "grey")

    counts = backlog.get("counts", {})
    candidates = backlog.get("candidates", [])
    leads = backlog.get("leads", [])

    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append("<title>sidecar status</title><style>%s</style></head><body><div class='wrap'>" % CSS)
    parts.append("<h1>Sidecar</h1>")
    parts.append("<div class='sub'>%s · generated %s</div>" %
                 (esc(probe.get("host", "this machine")), esc(C.iso())))

    # --- top row
    parts.append("<div class='grid'>")
    parts.append('<div class="card"><div class="k">Machine</div>'
                 '<div class="v">%s</div><div class="n">%s</div></div>' %
                 (pill(verdict.upper(), tone),
                  esc("; ".join(probe.get("reasons", [])[:2]) or "not probed yet")))
    parts.append(card("Backlog", counts.get("candidates", len(candidates)),
                      "%s auto-runnable · %s leads" %
                      (counts.get("auto_ok", 0), counts.get("leads", len(leads)))))
    parts.append(card("Today", "%s tasks" % today["tasks"],
                      "$%.2f of $%.2f · %.0f of %.0f min" %
                      (today["usd"], usable_usd, today["wall_min"], usable_min)))
    parts.append(card("Week", "$%.2f" % week["usd"], "of $%.2f usable" %
                      (budget["weekly_total_usd"] * (1 - reserve))))
    parts.append("</div>")

    # --- budget bars
    parts.append("<h2>Budget</h2><div class='grid'>")
    for label, used, cap in (
        ("Daily spend", today["usd"], usable_usd),
        ("Daily runtime", today["wall_min"], usable_min),
        ("Daily tasks", today["tasks"], budget["daily_tasks"] * (1 - reserve)),
    ):
        pct = min(100.0, 100.0 * used / cap) if cap else 0.0
        parts.append("<div class='card'><div class='k'>%s</div>"
                     "<div class='v'>%s <span class='mono'>/ %s</span></div>"
                     "<div class='bar'><i style='width:%.1f%%'></i></div></div>"
                     % (esc(label), esc(round(used, 2)), esc(round(cap, 2)), pct))
    parts.append("</div>")

    # --- candidates
    parts.append("<h2>Backlog &mdash; needs your decision</h2>")
    if not candidates:
        parts.append("<div class='empty'>Nothing mined yet. Run sc_mine.py.</div>")
    else:
        parts.append("<table><tr><th>Tier</th><th>Score</th><th>Proposal</th>"
                     "<th>Target</th><th>Proof of done</th><th>Status</th></tr>")
        for c in candidates:
            tgt = c.get("target") or ("%d projects" % len(c.get("targets", []))
                                      if c.get("targets") else "-")
            tone2 = {"proposed": "grey", "done": "green", "blocked": "red",
                     "rejected": "grey", "needs_interpretation": "amber"}.get(c.get("status"), "grey")
            parts.append(
                "<tr><td><span class='tier'>T%s</span></td><td class='mono'>%s</td>"
                "<td><b>%s</b><div class='mono'>%s</div></td>"
                "<td class='mono'>%s</td><td class='mono'>%s</td><td>%s</td></tr>"
                % (esc(c.get("tier")), esc(c.get("score")), esc(c.get("title")),
                   esc("; ".join(str(e) for e in c.get("evidence", [])[:1])[:150]),
                   esc(str(tgt)[:60]), esc(str(c.get("verify_with"))[:70]),
                   pill(c.get("status", "?"), tone2)))
        parts.append("</table>")

    # --- leads
    parts.append("<h2>Leads &mdash; raw material, not tasks</h2>")
    parts.append("<div class='sub'>Unverified keyword hits. Each one must be read "
                 "with its transcript before it becomes a task.</div>")
    if leads:
        for l in leads[:10]:
            parts.append("<div class='q'>%s<div class='mono'>%s · %s</div></div>"
                         % (esc(l.get("quote", "")[:220]), esc(l.get("kind")),
                            esc(str(l.get("project"))[-50:])))
    else:
        parts.append("<div class='empty'>No leads.</div>")

    # --- recent runs
    runs = (C.read_json(os.path.join(C.STATE_DIR, "runs.json"), {}) or {}).get("runs", [])
    parts.append("<h2>Recent runs</h2>")
    if runs:
        parts.append("<table><tr><th>When</th><th>Task</th><th>Outcome</th>"
                     "<th>Verified</th><th>Cost</th><th>Note</th></tr>")
        for r in runs[-12:][::-1]:
            tone3 = {"ok": "green", "fail": "red", "abort": "amber",
                     "abandoned": "red"}.get(r.get("effective_outcome"), "grey")
            parts.append("<tr><td class='mono'>%s</td><td class='mono'>%s</td><td>%s</td>"
                         "<td>%s</td><td class='mono'>$%.2f / %.0f min</td><td class='mono'>%s</td></tr>"
                         % (esc(str(r.get("finished_at"))[:16]), esc(r.get("task_id")),
                            pill(r.get("effective_outcome", "?"), tone3),
                            esc("yes" if r.get("verified") else "no"),
                            r.get("usd", 0) or 0, r.get("minutes", 0) or 0,
                            esc(str(r.get("note") or r.get("summary") or "")[:80])))
        parts.append("</table>")
    else:
        parts.append("<div class='empty'>No runs recorded.</div>")

    # --- profile
    parts.append("<h2>What it believes about you</h2>")
    prefs = profile.get("preference_signals", [])
    parts.append("<div class='sub'>Redacted excerpts only. Nothing here leaves the machine.</div>")
    if prefs:
        parts.append("<ul>")
        for s in prefs[:8]:
            parts.append("<li>%s</li>" % esc(s.get("quote", "")[:190]))
        parts.append("</ul>")
    else:
        parts.append("<div class='empty'>No preference signals mined yet.</div>")

    parts.append("<div class='foot'>State: %s · transcripts read locally, "
                 "never uploaded · <span class='mono'>%s</span></div>"
                 % (esc(C.SIDECAR_HOME), esc(STATUS_HTML)))
    parts.append("</div></body></html>")

    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Render the sidecar status page.")
    ap.add_argument("--open", action="store_true", help="open in the default browser")
    args = ap.parse_args()

    C.ensure_dirs()
    body = build()
    with open(STATUS_HTML, "w", encoding="utf-8") as fh:
        fh.write(body)

    if args.open:
        C.run_cmd(["/usr/bin/open", STATUS_HTML], timeout=10)

    C.emit({"kind": "render", "path": STATUS_HTML, "bytes": len(body)})


if __name__ == "__main__":
    main()
