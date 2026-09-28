#!/usr/bin/env python3
"""sc_run - decide whether to work, then account for the work.

This is the only entry point that mutates anything, and it mutates only the
sidecar's own state. It never edits a repository: it decides, hands a directive
to an agent, and records what happened. Keeping the mutating surface that small
is deliberate - the risky part of an unattended system is the part that writes.

  plan       read-only verdict: should we work now, on what, under what limits
  dispatch   record a finished run: gate the result, charge the budget, update status
  reconcile  expire runs that started and never reported back
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_budget  # noqa: E402
import sc_common as C  # noqa: E402
import sc_guard  # noqa: E402
import sc_probe  # noqa: E402
import sc_quota  # noqa: E402

BACKLOG = os.path.join(C.STATE_DIR, "backlog.json")
RUNS = os.path.join(C.STATE_DIR, "runs.json")


def load_backlog():
    return C.read_json(BACKLOG, {}) or {"candidates": []}


def load_runs():
    return C.read_json(RUNS, {}) or {"runs": []}


def save_runs(data):
    C.write_json_atomic(RUNS, data)


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

def reconcile(cfg):
    """Close out runs that started and never reported.

    A crashed agent leaves a run open forever, which would silently pin budget
    and hide the failure. Charging a conservative estimate on recovery means an
    abandoned run costs something, so a repeatedly crashing task trips its
    circuit breaker instead of retrying invisibly.
    """
    data = load_runs()
    max_minutes = float(cfg["guardrails"].get("max_runtime_minutes", 45))
    now = C.now()
    expired = []

    for r in data["runs"]:
        if r.get("status") != "started":
            continue
        started = r.get("started_epoch")
        if not started or (now - started) < max_minutes * 60:
            continue

        r["status"] = "abandoned"
        r["finished_at"] = C.iso(started + max_minutes * 60)
        r["note"] = ("no report within %s minutes; charged the full runtime as a "
                     "conservative estimate" % max_minutes)
        expired.append(r)

        led = sc_budget.load_ledger()
        today = sc_budget.bucket(led, "days", C.day_key(started))
        today["wall_min"] = round(today["wall_min"] + max_minutes, 2)
        today["tasks"] = int(today["tasks"] + 1)
        task = led["tasks"].setdefault(r["task_id"], {"consecutive_failures": 0, "total_runs": 0, "usd": 0.0})
        task["consecutive_failures"] = int(task.get("consecutive_failures", 0)) + 1
        task["last_outcome"] = "abandoned"
        task["last_note"] = r["note"]
        sc_budget.save_ledger(led)
        sc_guard.audit("run_abandoned", task_id=r["task_id"], run_id=r.get("run_id"))

    if expired:
        save_runs(data)
    return expired


def mark_task_status(task_id, status, note=None):
    backlog = load_backlog()
    for c in backlog.get("candidates", []):
        if c["id"] == task_id:
            c["status"] = status
            c["status_changed"] = C.iso()
            if note:
                c["human_note"] = note
            break
    C.write_json_atomic(BACKLOG, backlog)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def pick_candidate(cfg, backlog, budget_view):
    """Choose the next runnable task, or explain why nothing is runnable.

    Ordering is by tier first, then score. Tier dominates on purpose: a
    mechanically verifiable fix beats an appealing idea that nobody can check.
    """
    done = {"done", "rejected", "wontfix"}
    skipped = []
    chosen = None

    candidates = sorted(
        (c for c in backlog.get("candidates", []) if c.get("status") not in done),
        key=lambda c: (c.get("tier", 9), -c.get("score", 0)),
    )

    for c in candidates:
        if not c.get("auto_ok"):
            skipped.append({
                "id": c["id"], "title": c["title"], "tier": c["tier"],
                "reason": "needs human approval (tier %s or flagged manual)"
                          % c.get("tier"),
            })
            continue
        if c.get("bulk"):
            skipped.append({"id": c["id"], "title": c["title"],
                            "reason": "covers %s projects; pick one explicitly" % c.get("count")})
            continue
        if c.get("status") == "needs_interpretation":
            skipped.append({"id": c["id"], "title": c["title"],
                            "reason": "unverified lead, needs interpretation first"})
            continue
        if not c.get("verify_command"):
            # Some findings are real but cannot be proved by a command ("this
            # doc is stale", "this project is finished"). Those stay human work:
            # dispatching them would mean charging budget for an unverifiable
            # claim, which is exactly the failure this system exists to avoid.
            skipped.append({"id": c["id"], "title": c["title"],
                            "reason": "no runnable verification command; this one needs a human"})
            continue
        if c.get("last_run"):
            t = budget_view.get("task_breakers", {}).get(c["id"], 0)
            if t >= int(cfg["approval"].get("require_human_after_failures", 2)):
                skipped.append({"id": c["id"], "title": c["title"],
                                "reason": "failed %d times; a human should look before another attempt" % t})
                continue

        est_usd = float(cfg["approval"].get("auto_task_max_usd", 1.0)) * 0.5
        est_min = float(cfg["approval"].get("auto_task_max_minutes", 45)) * 0.5
        verdict = sc_budget.evaluate(
            cfg, sc_budget.load_ledger(), c["id"], est_usd, est_min, 0, 0, c.get("tier", 3))
        if not verdict["allowed"]:
            skipped.append({"id": c["id"], "title": c["title"],
                            "reason": "; ".join(verdict["denials"])})
            continue

        chosen = c
        break

    return chosen, skipped


def build_directive(cfg, task):
    g = cfg["guardrails"]
    return {
        "task_id": task["id"],
        "title": task["title"],
        "tier": task.get("tier"),
        "target": task.get("target"),
        "why": task.get("why"),
        "evidence": task.get("evidence", []),
        "objective": (
            "Resolve the finding described in evidence, at the target path, in the "
            "smallest change that makes the verification command pass. If the "
            "finding turns out to be wrong or already fixed, stop and report that "
            "instead of inventing work."
        ),
        "verify_with": task.get("verify_with"),
        "verify_command": task.get("verify_command"),
        "constraints": {
            "branch_prefix": g.get("working_branch_prefix"),
            "allow_commit": g.get("allow_commit"),
            "allow_push": g.get("allow_push"),
            "max_files_changed": g.get("max_files_changed"),
            "max_lines_changed": g.get("max_lines_changed"),
            "max_runtime_minutes": g.get("max_runtime_minutes"),
            "never": [
                "force push, hard reset, git clean, branch -D, stash drop",
                "touch deny-listed paths (credentials, keys, .env, shell history)",
                "install or upgrade global dependencies",
                "publish packages or push container images",
                "delete files outside the target repository",
            ],
        },
        "must_report": {
            "files_changed": "count and paths",
            "lines_changed": "added+removed",
            "verify_command": "the exact command run",
            "verify_exit_code": "integer",
            "verify_output_tail": "last ~40 lines",
            "blocked_on": "anything that stopped the work, or null",
            "usd_estimate": "spend if the provider bills per token, else 0",
        },
        "on_failure": ("Stop and report. Do not attempt more than %s corrective "
                       "cycles on the same failure."
                       % cfg["guardrails"].get("max_failures_per_task", 3)),
    }


def cmd_plan(args):
    cfg = C.load_config()
    expired = reconcile(cfg)

    # 1. Is the machine free?
    machine_cfg = cfg["machine"]
    cpu = sc_probe.read_cpu(machine_cfg)
    user = sc_probe.read_user_activity()
    mem = sc_probe.read_memory(machine_cfg)
    gpu = sc_probe.read_gpu()
    power = sc_probe.read_thermal_and_power(machine_cfg)
    endpoints = [sc_probe.probe_endpoint(e) for e in cfg.get("endpoints", [])]
    verdict = sc_probe.decide(machine_cfg, cpu, user, mem, gpu, power, endpoints)

    C.write_json_atomic(os.path.join(C.STATE_DIR, "last_probe.json"), {
        "kind": "probe", "at": C.iso(), "host": os.uname().nodename,
        "cpu": cpu, "user": user, "memory": mem, "gpu": gpu, "power": power,
        "endpoints": endpoints, "verdict": verdict["verdict"],
        "reasons": verdict["reasons"], "allowed_work": verdict["allowed_work"],
    })

    probe_view = {
        "verdict": verdict["verdict"],
        "reasons": verdict["reasons"],
        "allowed_work": verdict["allowed_work"],
        "cpu_load_per_core": cpu["load_per_core"],
        "user_idle_s": user["user_idle_s"],
        "mem_free_pct": mem["mem_free_pct"],
        "gpu_util_pct": gpu.get("util_pct"),
        "endpoints": endpoints,
    }

    # 2. Is there subscription headroom? Only meaningful when a ceiling is set.
    quota_view = {"providers": [], "headroom_available": None}
    for p in cfg.get("quota", {}).get("providers", []):
        root = os.path.expanduser(p.get("transcript_root", ""))
        if not os.path.isdir(root):
            continue
        ceiling = int(p.get("window_token_ceiling") or 0)
        res = sc_quota.analyse(
            root, float(p.get("window_hours") or 5.0), 7,
            p.get("model_exclude_globs", []), ceiling,
            float(p.get("reserve_fraction", cfg["budget"].get("reserve_fraction", 0.25))))
        exact = res.get("exact") or {}
        quota_view["providers"].append({
            "id": p.get("id"),
            "rolling_tokens": res["rolling_window"]["tokens"],
            "resets_in_s": (exact.get("resets_in_s")
                            if exact.get("available") else res["rolling_window"]["resets_in_s"]),
            "headroom_tokens": res["ceiling"]["headroom_tokens"],
            "headroom_pct": res["ceiling"]["headroom_pct"],
            "idle_windows_last_7d": res["windows"]["never_used"],
            "configured": bool(ceiling),
            # When Claude Code reports its own rate limits, that number wins:
            # it counts usage from every machine and the web UI, which the
            # transcript estimate cannot see.
            "exact_used_pct": exact.get("used_percentage"),
            "exact_source": exact.get("source") if exact.get("available") else None,
            "exact_captured_at": exact.get("captured_at"),
        })
    # Prefer an exact reading over the estimate. A stale reading (older than the
    # window it describes) is worse than no reading, so it is ignored.
    for q in quota_view["providers"]:
        if q.get("exact_used_pct") is None:
            continue
        captured = q.get("exact_captured_at")
        captured_ts = C.parse_iso(captured)
        if captured_ts is None:
            # An unreadable capture time cannot be shown to be current, and
            # trusting it would let a stale reading unlock work on a window that
            # is actually spent. Fail closed.
            fresh = False
            q["exact_fresh_reason"] = ("capture time missing or unparseable (%r); "
                                       "treating the reading as stale" % (captured,))
        else:
            age = C.now() - captured_ts
            fresh = age < 6 * 3600
            q["exact_age_s"] = int(age)
            if not fresh:
                q["exact_fresh_reason"] = "reading is %d hours old" % int(age // 3600)
        q["exact_fresh"] = fresh
        if not fresh:
            q["exact_used_pct"] = None
        if fresh:
            q["headroom_pct"] = round(max(0.0, 100.0 - float(q["exact_used_pct"])), 1)

    configured = [q for q in quota_view["providers"]
                  if q["configured"] or q.get("exact_used_pct") is not None]
    if configured:
        quota_view["headroom_available"] = any(
            (q["headroom_pct"] is None or q["headroom_pct"] > 10) for q in configured)

    # 3. Budget
    led = sc_budget.load_ledger()
    budget = cfg["budget"]
    reserve = float(budget.get("reserve_fraction", 0.0))
    today = sc_budget.bucket(led, "days", C.day_key())
    week = sc_budget.bucket(led, "weeks", C.iso_week_key())
    budget_view = {
        "today": today,
        "week": week,
        "usable": {
            "usd": round(budget["daily_total_usd"] * (1 - reserve), 2),
            "wall_min": round(budget["daily_wall_clock_min"] * (1 - reserve), 1),
            "tasks": round(budget["daily_tasks"] * (1 - reserve), 1),
        },
        "task_breakers": {k: v.get("consecutive_failures", 0) for k, v in led["tasks"].items()},
    }

    # 4. What would we work on?
    backlog = load_backlog()
    task, skipped = pick_candidate(cfg, backlog, budget_view)

    gates = {
        "machine_free": verdict["verdict"] in ("green", "yellow"),
        "budget_available": today["tasks"] < budget_view["usable"]["tasks"] and today["usd"] < budget_view["usable"]["usd"],
        "has_candidate": task is not None,
        "quota_ok": quota_view["headroom_available"] is not False,
    }
    go = all(gates.values())

    reasons = []
    if not gates["machine_free"]:
        reasons.append("machine is %s: %s" % (verdict["verdict"], "; ".join(verdict["reasons"])))
    if not gates["budget_available"]:
        reasons.append("daily budget exhausted: %s/%s tasks, $%s/$%s"
                       % (today["tasks"], budget_view["usable"]["tasks"],
                          today["usd"], budget_view["usable"]["usd"]))
    if not gates["has_candidate"]:
        reasons.append("no auto-runnable candidate; %d need a decision" % len(skipped))
    if not gates["quota_ok"]:
        reasons.append("subscription window has no headroom")
    if go:
        reasons.append("all gates open; work is %s" % ("heavy" if verdict["verdict"] == "green" else "light"))

    C.emit({
        "kind": "plan",
        "at": C.iso(),
        "go": go,
        "reasons": reasons,
        "gates": gates,
        "probe": probe_view,
        "quota": quota_view,
        "budget": budget_view,
        "task": task,
        "directive": build_directive(cfg, task) if (go and task) else None,
        "needs_decision": skipped[:12],
        "expired_runs_closed": [
            {"task_id": r["task_id"], "run_id": r.get("run_id"), "note": r.get("note")}
            for r in expired
        ],
        "backlog_counts": backlog.get("counts", {}),
    })


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def cmd_dispatch(args):
    cfg = C.load_config()
    task_id = args.task_id
    outcome = args.outcome

    # The gate runs before the ledger is touched, so a task that reports success
    # without a passing verification is recorded as a failure, not a win.
    verify_result = None
    if args.verify_cmd:
        import subprocess
        denied = next((why for pat, why in sc_guard.DENY_COMMAND_PATTERNS
                       if __import__("re").search(pat, args.verify_cmd, __import__("re").I)), None)
        if denied:
            verify_result = {"passed": False, "error": "verification command blocked: " + denied}
        else:
            cwd = os.path.expanduser(args.cwd) if args.cwd else os.getcwd()
            try:
                p = subprocess.run(["/bin/sh", "-lc", args.verify_cmd], cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=int(args.verify_timeout or 900), check=False)
                verify_result = {
                    "passed": p.returncode == 0,
                    "exit_code": p.returncode,
                    "stdout_tail": p.stdout.decode("utf-8", "replace")[-3000:],
                    "stderr_tail": p.stderr.decode("utf-8", "replace")[-3000:],
                }
            except subprocess.TimeoutExpired:
                verify_result = {"passed": False, "exit_code": 124, "error": "verification timed out"}

    claimed_success = outcome == "ok"
    verified = bool(verify_result and verify_result.get("passed"))
    effective = outcome
    if claimed_success and verify_result is not None and not verified:
        effective = "fail"

    led = sc_budget.load_ledger()
    sc_budget.bucket(led, "days", C.day_key())
    sc_budget.bucket(led, "weeks", C.iso_week_key())

    today = sc_budget.bucket(led, "days", C.day_key())
    week = sc_budget.bucket(led, "weeks", C.iso_week_key())
    today["usd"] = round(today["usd"] + (args.usd or 0.0), 4)
    week["usd"] = round(week["usd"] + (args.usd or 0.0), 4)
    today["wall_min"] = round(today["wall_min"] + (args.minutes or 0.0), 2)
    today["gpu_min"] = round(today["gpu_min"] + (args.gpu_minutes or 0.0), 2)
    today["tokens"] = int(today["tokens"] + (args.tokens or 0))
    today["tasks"] = int(today["tasks"] + 1)

    task = led["tasks"].setdefault(task_id, {"consecutive_failures": 0, "total_runs": 0, "usd": 0.0})
    task["total_runs"] = int(task.get("total_runs", 0)) + 1
    task["usd"] = round(float(task.get("usd", 0.0)) + (args.usd or 0.0), 4)
    task["last_run"] = C.iso()
    task["last_outcome"] = effective
    if args.note:
        task["last_note"] = args.note
    task["consecutive_failures"] = 0 if effective == "ok" else int(task.get("consecutive_failures", 0)) + 1
    sc_budget.save_ledger(led)

    runs = load_runs()
    runs["runs"].append({
        "run_id": args.run_id or ("run-" + C.fingerprint(task_id + C.iso())),
        "task_id": task_id,
        "started_epoch": None,
        "finished_at": C.iso(),
        "status": "finished",
        "claimed_outcome": outcome,
        "effective_outcome": effective,
        "verified": verified,
        "usd": args.usd, "minutes": args.minutes, "tokens": args.tokens,
        "files_changed": args.files, "lines_changed": args.lines,
        "summary": args.summary,
        "note": args.note,
    })
    save_runs(runs)

    if effective == "ok":
        mark_task_status(task_id, "done")
    elif effective == "abort":
        mark_task_status(task_id, "blocked", args.note)

    sc_guard.audit("dispatch", task_id=task_id, claimed=outcome, effective=effective,
                   verified=verified, usd=args.usd, minutes=args.minutes)

    next_status = task["consecutive_failures"]
    warning = None
    if next_status >= int(cfg["guardrails"].get("max_failures_per_task", 3)):
        warning = ("circuit breaker open: this task failed %d times and will not be "
                   "scheduled again until a human clears it" % next_status)

    C.emit({
        "kind": "dispatch",
        "at": C.iso(),
        "task_id": task_id,
        "claimed_outcome": outcome,
        "effective_outcome": effective,
        "verified": verified,
        "verification": verify_result,
        "charged": {"usd": args.usd, "minutes": args.minutes, "tokens": args.tokens},
        "today": today,
        "consecutive_failures": next_status,
        "warning": warning,
    })


def cmd_reconcile(args):
    cfg = C.load_config()
    expired = reconcile(cfg)
    C.emit({"kind": "reconcile", "at": C.iso(), "closed": len(expired),
            "runs": [{"task_id": r["task_id"], "note": r.get("note")} for r in expired]})


def main():
    ap = argparse.ArgumentParser(description="Sidecar scheduler.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="read-only: should we work now, and on what?")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("dispatch", help="record a finished run and settle the budget")
    p.add_argument("--task-id", required=True)
    p.add_argument("--outcome", choices=["ok", "fail", "abort"], required=True)
    p.add_argument("--verify-cmd", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--verify-timeout", type=int, default=900)
    p.add_argument("--usd", type=float, default=0.0)
    p.add_argument("--minutes", type=float, default=0.0)
    p.add_argument("--gpu-minutes", type=float, default=0.0)
    p.add_argument("--tokens", type=int, default=0)
    p.add_argument("--files", type=int, default=None)
    p.add_argument("--lines", type=int, default=None)
    p.add_argument("--summary", default="")
    p.add_argument("--note", default="")
    p.add_argument("--run-id", default="")
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("reconcile", help="close out runs that never reported back")
    p.set_defaults(func=cmd_reconcile)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
