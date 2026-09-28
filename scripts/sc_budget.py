#!/usr/bin/env python3
"""sc_budget - hard compute budget ledger with a reserve and a failure circuit breaker.

The ledger is the single source of truth for what the sidecar is allowed to spend.
Every reservation (check) and every settlement (charge) is appended to ledger.jsonl,
so the totals can always be rebuilt and audited from the event log.

Exit codes: 0 = allowed, 5 = denied.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

LEDGER = os.path.join(C.STATE_DIR, "ledger.json")
EVENTS = os.path.join(C.STATE_DIR, "ledger.jsonl")


def empty_ledger():
    return {"schemaVersion": 1, "days": {}, "weeks": {}, "tasks": {}}


def load_ledger():
    led = C.read_json(LEDGER, None)
    if not isinstance(led, dict) or "days" not in led:
        return empty_ledger()
    for key in ("days", "weeks", "tasks"):
        led.setdefault(key, {})
    return led


def save_ledger(led):
    C.write_json_atomic(LEDGER, led)


def zero():
    return {"usd": 0.0, "wall_min": 0.0, "gpu_min": 0.0, "tokens": 0, "tasks": 0}


def bucket(led, table, key):
    container = led[table]
    if key not in container:
        container[key] = zero()
    for k, v in zero().items():
        container[key].setdefault(k, v)
    return container[key]


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

def evaluate(cfg, led, task_id, est_usd, est_min, est_gpu_min, est_tokens, tier):
    budget = cfg["budget"]
    approval = cfg["approval"]
    reserve = float(budget.get("reserve_fraction", 0.0))

    today = bucket(led, "days", C.day_key())
    week = bucket(led, "weeks", C.iso_week_key())
    task = led["tasks"].get(task_id or "", {})

    def usable(limit):
        """Leave the reserve untouched; the reserve is what the human gets back."""
        return float(limit) * (1.0 - reserve)

    limits = [
        ("daily spend", today["usd"], usable(budget["daily_total_usd"]), est_usd),
        ("weekly spend", week["usd"], usable(budget["weekly_total_usd"]), est_usd),
        ("daily wall clock", today["wall_min"], usable(budget["daily_wall_clock_min"]), est_min),
        ("daily gpu time", today["gpu_min"], usable(budget["daily_gpu_min"]), est_gpu_min),
        ("daily tokens", today["tokens"], usable(budget["daily_tokens"]), est_tokens),
        ("daily task count", today["tasks"], usable(budget["daily_tasks"]), 1),
    ]

    denials = []
    headroom = {}
    for name, used, limit, want in limits:
        left = max(0.0, limit - used)
        headroom[name] = {"used": round(used, 3), "limit": round(limit, 3), "left": round(left, 3)}
        if want and want > left:
            denials.append("%s: wants %.3f, only %.3f left" % (name, want, left))

    if est_usd and est_usd > float(budget["daily_task_usd"]):
        denials.append("single task estimate $%.2f exceeds per-task cap $%.2f"
                       % (est_usd, budget["daily_task_usd"]))

    # circuit breaker on repeated failure of the same task
    failures = int(task.get("consecutive_failures", 0))
    threshold = int(cfg["guardrails"].get("max_failures_per_task", 3))
    breaker_tripped = failures >= threshold
    if breaker_tripped:
        denials.append("circuit breaker: task failed %d times in a row (limit %d)"
                       % (failures, threshold))

    # approval policy
    requires_approval = False
    approval_reasons = []
    mode = approval.get("mode", "ask")
    if mode == "always":
        requires_approval = True
        approval_reasons.append("approval mode is 'always'")
    elif mode == "ask":
        if tier > int(approval.get("max_auto_tier", 2)):
            requires_approval = True
            approval_reasons.append("tier %d above auto ceiling %d" % (tier, approval.get("max_auto_tier")))
        if (est_usd or 0) > float(approval.get("auto_task_max_usd", 0)):
            requires_approval = True
            approval_reasons.append("estimate $%.2f above auto ceiling $%.2f"
                                    % (est_usd or 0, approval.get("auto_task_max_usd")))
        if (est_min or 0) > float(approval.get("auto_task_max_minutes", 0)):
            requires_approval = True
            approval_reasons.append("estimate %s min above auto ceiling %s min"
                                    % (est_min or 0, approval.get("auto_task_max_minutes")))
        if failures >= int(approval.get("require_human_after_failures", 2)):
            requires_approval = True
            approval_reasons.append("task has %d prior failures" % failures)
    # mode == "never": the proposal still gets recorded, nothing is auto-approved

    return {
        "allowed": not denials,
        "denials": denials,
        "headroom": headroom,
        "requires_approval": requires_approval,
        "approval_reasons": approval_reasons,
        "breaker_tripped": breaker_tripped,
        "consecutive_failures": failures,
        "reserve_fraction": reserve,
    }


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_check(args):
    cfg = C.load_config()
    led = load_ledger()
    verdict = evaluate(
        cfg, led, args.task_id,
        args.est_usd, args.est_min, args.est_gpu_min, args.est_tokens, args.tier,
    )
    C.append_jsonl(EVENTS, {
        "at": C.iso(), "event": "check", "task_id": args.task_id,
        "est_usd": args.est_usd, "est_min": args.est_min, "tier": args.tier,
        "allowed": verdict["allowed"], "denials": verdict["denials"],
    })
    out = {"kind": "budget-check", "at": C.iso(), "task_id": args.task_id}
    out.update(verdict)
    C.emit(out)
    sys.exit(0 if verdict["allowed"] else 5)


def cmd_charge(args):
    cfg = C.load_config()
    led = load_ledger()

    today = bucket(led, "days", C.day_key())
    week = bucket(led, "weeks", C.iso_week_key())

    for b in (today, week):
        b["usd"] = round(b["usd"] + (args.usd or 0.0), 4)
    for b in (today,):
        b["wall_min"] = round(b["wall_min"] + (args.wall_min or 0.0), 2)
        b["gpu_min"] = round(b["gpu_min"] + (args.gpu_min or 0.0), 2)
        b["tokens"] = int(b["tokens"] + (args.tokens or 0))
        b["tasks"] = int(b["tasks"] + (1 if args.count_task else 0))

    if args.task_id:
        t = led["tasks"].setdefault(args.task_id, {
            "consecutive_failures": 0, "total_runs": 0, "usd": 0.0, "first_seen": C.iso(),
        })
        t["total_runs"] = int(t.get("total_runs", 0)) + 1
        t["usd"] = round(float(t.get("usd", 0.0)) + (args.usd or 0.0), 4)
        t["last_run"] = C.iso()
        t["last_outcome"] = args.outcome
        if args.outcome == "ok":
            t["consecutive_failures"] = 0
        else:
            t["consecutive_failures"] = int(t.get("consecutive_failures", 0)) + 1
        if args.note:
            t["last_note"] = args.note

    save_ledger(led)
    C.append_jsonl(EVENTS, {
        "at": C.iso(), "event": "charge", "task_id": args.task_id,
        "usd": args.usd, "wall_min": args.wall_min, "gpu_min": args.gpu_min,
        "tokens": args.tokens, "outcome": args.outcome, "note": args.note,
    })
    C.emit({
        "kind": "budget-charge", "at": C.iso(), "charged": {
            "usd": args.usd, "wall_min": args.wall_min, "gpu_min": args.gpu_min,
            "tokens": args.tokens,
        },
        "today": today, "week": week,
        "task_failures": led["tasks"].get(args.task_id or "", {}).get("consecutive_failures"),
    })


def cmd_status(args):
    cfg = C.load_config()
    led = load_ledger()
    budget = cfg["budget"]
    reserve = float(budget.get("reserve_fraction", 0.0))

    today = bucket(led, "days", C.day_key())
    week = bucket(led, "weeks", C.iso_week_key())

    C.emit({
        "kind": "budget-status",
        "at": C.iso(),
        "day": C.day_key(),
        "week": C.iso_week_key(),
        "today": today,
        "week": week,
        "caps": budget,
        "usable_after_reserve": {
            "daily_usd": round(budget["daily_total_usd"] * (1 - reserve), 2),
            "weekly_usd": round(budget["weekly_total_usd"] * (1 - reserve), 2),
            "daily_wall_min": round(budget["daily_wall_clock_min"] * (1 - reserve), 1),
        },
        "tracked_tasks": len(led["tasks"]),
        "ledger_path": LEDGER,
        "events_path": EVENTS,
    })


def main():
    ap = argparse.ArgumentParser(description="Sidecar compute budget ledger.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="ask whether a task may run now")
    p.add_argument("--task-id", default="")
    p.add_argument("--est-usd", type=float, default=0.0)
    p.add_argument("--est-min", type=float, default=0.0)
    p.add_argument("--est-gpu-min", type=float, default=0.0)
    p.add_argument("--est-tokens", type=int, default=0)
    p.add_argument("--tier", type=int, default=1)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("charge", help="settle actual spend against the ledger")
    p.add_argument("--task-id", default="")
    p.add_argument("--usd", type=float, default=0.0)
    p.add_argument("--wall-min", type=float, default=0.0)
    p.add_argument("--gpu-min", type=float, default=0.0)
    p.add_argument("--tokens", type=int, default=0)
    p.add_argument("--outcome", choices=["ok", "fail", "abort"], default="ok")
    p.add_argument("--note", default="")
    p.add_argument("--count-task", action="store_true", default=True)
    p.set_defaults(func=cmd_charge)

    p = sub.add_parser("status", help="show today and this week")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
