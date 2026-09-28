#!/usr/bin/env python3
"""sc_guard - policy enforcement and verification gates.

Two jobs:

  policy   Refuse work that would touch something the sidecar must never touch,
           or that exceeds the size limits that keep a change reviewable.

  verify   Run the one command that proves a task is done, and record the raw
           output. A task without a passing verification command did not finish,
           no matter how confident the summary sounds.

Exit codes: 0 allowed/passed, 5 denied, 6 verification failed.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

AUDIT = os.path.join(C.STATE_DIR, "audit.jsonl")

# Commands that are never acceptable in an unattended run, regardless of what a
# task description asks for. Deny by pattern because these are the operations
# that destroy work irreversibly or leak material off the machine.
DENY_COMMAND_PATTERNS = [
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f", "recursive force delete"),
    (r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r", "recursive force delete"),
    (r"\bgit\s+push\b.*--force", "force push"),
    (r"\bgit\s+push\b.*-f\b", "force push"),
    (r"\bgit\s+reset\s+--hard", "hard reset discards work"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fd]", "git clean deletes untracked work"),
    (r"\bgit\s+branch\s+-D\b", "forced branch delete"),
    (r"\bgit\s+checkout\s+--\s+\.", "discards working tree changes"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "destroys stashed work"),
    (r"\bgit\s+filter-branch\b", "history rewrite"),
    (r"\bgit\s+rebase\b.*--root", "history rewrite"),
    (r"\bdrop\s+(table|database)\b", "destructive database op"),
    (r"\btruncate\s+table\b", "destructive database op"),
    (r"\bkubectl\s+delete\b", "cluster deletion"),
    (r"\bterraform\s+(destroy|apply\s+-auto-approve)", "infrastructure destruction"),
    (r"\bdd\s+if=.*of=/dev/", "raw disk write"),
    (r"\bmkfs\b", "filesystem format"),
    (r">\s*/dev/(disk|sd|nvme)", "raw device write"),
    (r"\b(shutdown|reboot|halt)\b", "host power state"),
    (r"\bkillall\s+-9\b", "mass process kill"),
    (r"\bchmod\s+-R\s+777\b", "world-writable permissions"),
    (r"\bcurl\b[^|]*\|\s*(ba)?sh", "piping remote content into a shell"),
    (r"\bwget\b[^|]*\|\s*(ba)?sh", "piping remote content into a shell"),
    (r"\bnpm\s+publish\b", "package publication"),
    (r"\bcargo\s+publish\b", "package publication"),
    (r"\btwine\s+upload\b", "package publication"),
    (r"\bdocker\s+(push|rm\s+-f)", "container publication or forced removal"),
    (r"\bssh-keygen\b", "credential generation"),
    (r"\brm\s+-rf\s+/\s*$", "root delete"),
]

# Anything matching these may not be read, written, or referenced by a task.
def _default_deny(cfg):
    return cfg["guardrails"].get("deny_globs", []) + [
        "*/.git/config", "*/.git/hooks/*", "*/Library/Messages/*",
        "*/Library/Mail/*", "*/Library/Safari/*",
    ]


def path_denied(path, cfg):
    expanded = os.path.expanduser(path)
    real = os.path.realpath(expanded)
    for pattern in _default_deny(cfg):
        pat = os.path.expanduser(pattern)
        if fnmatch.fnmatch(expanded, pat) or fnmatch.fnmatch(real, pat):
            return pattern
        # also test each path suffix, so '*.env' matches '/a/b/.env'
        parts = real.split(os.sep)
        for i in range(len(parts)):
            if fnmatch.fnmatch(os.sep.join(parts[i:]), pat):
                return pattern
    return None


def within_allowed_roots(path, cfg):
    real = os.path.realpath(os.path.expanduser(path))
    for root in cfg["guardrails"].get("allowed_roots", []):
        root_real = os.path.realpath(os.path.expanduser(root))
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    return False


def audit(event, **fields):
    entry = {"at": C.iso(), "event": event}
    entry.update(fields)
    C.append_jsonl(AUDIT, entry)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_policy(args):
    cfg = C.load_config()
    g = cfg["guardrails"]
    denials = []
    warnings = []

    targets = args.target or []
    for t in targets:
        if not within_allowed_roots(t, cfg):
            denials.append("path outside allowed_roots: " + t)
        hit = path_denied(t, cfg)
        if hit:
            denials.append("path matches deny pattern %s: %s" % (hit, t))

    if args.files is not None and args.files > int(g.get("max_files_changed", 25)):
        denials.append("touches %d files, over the %s file limit"
                       % (args.files, g.get("max_files_changed")))
    if args.lines is not None and args.lines > int(g.get("max_lines_changed", 800)):
        warnings.append("changes %d lines, over the %s line soft limit - split it"
                        % (args.lines, g.get("max_lines_changed")))
    if args.push and not g.get("allow_push", False):
        denials.append("push requested but guardrails.allow_push is false")
    if args.commit and not g.get("allow_commit", True):
        denials.append("commit requested but guardrails.allow_commit is false")

    if args.command:
        for pat, why in DENY_COMMAND_PATTERNS:
            if re.search(pat, args.command, re.I):
                denials.append("command blocked (%s): %s" % (why, args.command[:120]))

    allowed = not denials
    audit("policy", allowed=allowed, denials=denials, warnings=warnings,
          target=targets, command=(args.command or "")[:200])
    C.emit({
        "kind": "policy",
        "allowed": allowed,
        "denials": denials,
        "warnings": warnings,
        "limits": {
            "max_files_changed": g.get("max_files_changed"),
            "max_lines_changed": g.get("max_lines_changed"),
            "allow_commit": g.get("allow_commit"),
            "allow_push": g.get("allow_push"),
            "working_branch_prefix": g.get("working_branch_prefix"),
        },
    })
    sys.exit(0 if allowed else 5)


def cmd_verify(args):
    """Run the verification command in the target directory and record everything."""
    cfg = C.load_config()
    cwd = os.path.expanduser(args.cwd) if args.cwd else os.getcwd()
    timeout = int(args.timeout or 900)

    if not os.path.isdir(cwd):
        C.emit({"kind": "verify", "passed": False, "error": "no such directory: " + cwd})
        sys.exit(6)

    if not within_allowed_roots(cwd, cfg):
        audit("verify", passed=False, error="outside allowed roots", cwd=cwd)
        C.emit({"kind": "verify", "passed": False, "error": "target outside allowed_roots"})
        sys.exit(5)

    for pat, why in DENY_COMMAND_PATTERNS:
        if re.search(pat, args.cmd, re.I):
            audit("verify", passed=False, error="blocked command", command=args.cmd[:200])
            C.emit({"kind": "verify", "passed": False, "error": "command blocked: " + why})
            sys.exit(5)

    started = C.now()
    try:
        p = subprocess.run(
            ["/bin/sh", "-lc", args.cmd],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        code = p.returncode
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        timed_out = False
    except subprocess.TimeoutExpired:
        code, out, err, timed_out = 124, "", "timed out", True

    duration = round(C.now() - started, 1)

    # Every run's raw tail is kept, because the point of a gate is the evidence,
    # not the verdict. Truncate from the middle so both the start and the
    # failure at the end survive.
    def tail(text, limit=4000):
        if len(text) <= limit:
            return text
        return text[: limit // 4] + "\n...[truncated]...\n" + text[-(3 * limit // 4):]

    passed = (code == 0) and not timed_out
    audit("verify", passed=passed, code=code, cwd=cwd,
          command=args.cmd[:300], duration_s=duration, task_id=args.task_id)

    C.emit({
        "kind": "verify",
        "passed": passed,
        "exit_code": code,
        "timed_out": timed_out,
        "duration_s": duration,
        "cwd": cwd,
        "command": args.cmd,
        "stdout_tail": tail(out),
        "stderr_tail": tail(err),
        "task_id": args.task_id,
        "audit": AUDIT,
    })
    sys.exit(0 if passed else 6)


def main():
    ap = argparse.ArgumentParser(description="Sidecar policy and verification gates.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("policy", help="may this work proceed?")
    p.add_argument("--target", action="append", default=[])
    p.add_argument("--files", type=int, default=None)
    p.add_argument("--lines", type=int, default=None)
    p.add_argument("--commit", action="store_true")
    p.add_argument("--push", action="store_true")
    p.add_argument("--command", default="")
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("verify", help="run the proof that a task is done")
    p.add_argument("--cmd", required=True)
    p.add_argument("--cwd", default="")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--task-id", default="")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
