#!/usr/bin/env python3
"""sc_backlog - the human side of the loop: review, approve, reject.

The sidecar never widens its own authority. A bulk finding covering 23 projects
is a prompt to look, not a licence to edit 23 repositories; the human picks the
one that matters and that single target becomes runnable.

  list                     show everything awaiting a decision
  show <id>                full detail for one item, including evidence
  approve <id> [--target]  make an item runnable (optionally one target of a bulk item)
  reject <id> [--reason]   never propose this again
  reopen <id>              put a rejected item back in play
  clear-breaker <id>       allow a task that tripped its failure breaker to retry
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_budget  # noqa: E402
import sc_common as C  # noqa: E402

BACKLOG = os.path.join(C.STATE_DIR, "backlog.json")


def load():
    return C.read_json(BACKLOG, {}) or {"candidates": [], "leads": []}


def save(b):
    C.write_json_atomic(BACKLOG, b)


def find(b, item_id):
    # An empty id must never match. Every id starts with "", so a blank argument
    # would prefix-match the whole backlog and, when only one item existed,
    # silently act on it.
    if not item_id or not item_id.strip():
        C.die("an item id is required")
    for c in b.get("candidates", []):
        if c["id"] == item_id:
            return c
    # allow a unique prefix, because hashed ids are tedious to type
    matches = [c for c in b.get("candidates", []) if c["id"].startswith(item_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        C.die("ambiguous id prefix %r matches %d items" % (item_id, len(matches)))
    return None


def cmd_list(args):
    b = load()
    rows = []
    for c in b.get("candidates", []):
        if args.status and c.get("status") != args.status:
            continue
        rows.append({
            "id": c["id"], "tier": c.get("tier"), "score": c.get("score"),
            "status": c.get("status"), "title": c.get("title"),
            "targets": len(c.get("targets", [])), "target": c.get("target"),
            "auto_ok": c.get("auto_ok"),
            "verify_with": c.get("verify_with"),
        })
    C.emit({
        "kind": "backlog-list",
        "counts": b.get("counts", {}),
        "run_ready": sum(1 for r in rows if r["status"] == "approved"),
        "items": rows,
    })


def cmd_show(args):
    b = load()
    c = find(b, args.id)
    if not c:
        C.die("no such item: " + args.id)
    detail = dict(c)
    if len(detail.get("targets", [])) > 20:
        detail["targets_truncated"] = detail["targets"][:20]
        detail["targets"] = detail["targets"][:20] + ["... %d more" % (len(c["targets"]) - 20)]
    C.emit({"kind": "backlog-show", "item": detail})


def cmd_approve(args):
    b = load()
    c = find(b, args.id)
    if not c:
        C.die("no such item: " + args.id)

    targets = c.get("targets") or ([c["target"]] if c.get("target") else [])

    if c.get("bulk") or len(targets) > 1:
        if not args.target:
            C.emit({
                "kind": "backlog-approve",
                "approved": False,
                "id": c["id"],
                "reason": ("this item covers %d targets; name exactly one with --target "
                           "so the sidecar never edits a project you did not choose" % len(targets)),
                "targets": targets[:40],
            })
            return
        # Compare resolved paths. Miners store realpath, but a human types the
        # path they know - on macOS /tmp and /var are symlinks, so a literal
        # comparison would reject a target that is genuinely in the list.
        chosen_raw = os.path.expanduser(args.target)
        chosen = os.path.realpath(chosen_raw)
        resolved = {os.path.realpath(t): t for t in targets}
        if chosen not in resolved:
            near = [t for t in targets
                    if chosen_raw in t or t.endswith(os.path.basename(chosen_raw.rstrip("/")))]
            C.emit({
                "kind": "backlog-approve", "approved": False, "id": c["id"],
                "reason": "target not covered by this item",
                "did_you_mean": near[:5],
            })
            return
        chosen = resolved[chosen]
        # Materialise a single-target child so the bulk item stays a bulk item
        # and the approved work has its own run history and breaker.
        child = dict(c)
        child["id"] = c["id"].replace(":bulk", "") + ":" + C.fingerprint(chosen)
        child["title"] = c["title"].split(" (")[0] + ": " + os.path.basename(chosen.rstrip("/"))
        child["target"] = chosen
        child["targets"] = [chosen]
        child["bulk"] = False
        child["parent_id"] = c["id"]
        child["status"] = "approved"
        child["approved_at"] = C.iso()
        child["auto_ok"] = True
        child["score"] = round(float(c.get("score", 0)) + 0.5, 2)
        child["evidence"] = [e for e in c.get("evidence", [])
                             if os.path.basename(chosen.rstrip("/")) in e][:5] or c.get("evidence", [])[:3]

        existing = find(b, child["id"])
        if existing:
            existing.update({"status": "approved", "auto_ok": True, "approved_at": C.iso()})
            child = existing
        else:
            b["candidates"].append(child)
        save(b)
        C.emit({"kind": "backlog-approve", "approved": True,
                "created_id": child["id"], "target": chosen,
                "note": "single-target item is now runnable"})
        return

    c["status"] = "approved"
    c["auto_ok"] = True
    c["approved_at"] = C.iso()
    save(b)
    C.emit({"kind": "backlog-approve", "approved": True, "id": c["id"],
            "target": c.get("target"), "note": "item is now runnable"})


def cmd_reject(args):
    b = load()
    c = find(b, args.id)
    if not c:
        C.die("no such item: " + args.id)
    c["status"] = "rejected"
    c["auto_ok"] = False
    c["human_note"] = args.reason or "rejected"
    c["status_changed"] = C.iso()
    save(b)
    C.emit({"kind": "backlog-reject", "id": c["id"], "reason": c["human_note"],
            "note": "mining will not resurrect this unless the item disappears and reappears"})
    return


def cmd_reopen(args):
    b = load()
    c = find(b, args.id)
    if not c:
        C.die("no such item: " + args.id)
    c["status"] = "proposed"
    c["auto_ok"] = False
    c["human_note"] = args.reason or "reopened"
    save(b)
    C.emit({"kind": "backlog-reopen", "id": c["id"]})


def cmd_clear_breaker(args):
    led = sc_budget.load_ledger()
    t = led["tasks"].get(args.id)
    if not t:
        C.emit({"kind": "backlog-clear-breaker", "cleared": False,
                "reason": "no run history for id " + args.id})
        return
    previous = t.get("consecutive_failures", 0)
    t["consecutive_failures"] = 0
    t["last_note"] = "breaker cleared by human"
    sc_budget.save_ledger(led)
    C.emit({"kind": "backlog-clear-breaker", "cleared": True,
            "id": args.id, "previous_failures": previous})


def main():
    ap = argparse.ArgumentParser(description="Review and approve sidecar proposals.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--status", default="")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show"); p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve")
    p.add_argument("id"); p.add_argument("--target", default="")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject")
    p.add_argument("id"); p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("reopen")
    p.add_argument("id"); p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("clear-breaker"); p.add_argument("id")
    p.set_defaults(func=cmd_clear_breaker)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()