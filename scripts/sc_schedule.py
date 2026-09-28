#!/usr/bin/env python3
"""sc_schedule - run the decide step on a timer.

Installs a launchd agent (macOS) or prints a cron line (Linux). The timer runs
'sc_run.py plan' and nothing else.

That restraint is the design. A timer that also executed work would need to pick
a model, a budget, and a repository with nobody watching - which is exactly the
set of decisions this system exists to keep with the human. The timer keeps the
verdict fresh so that when you sit down, the answer is already computed. Wire
sc_cycle.sh to an agent if you want the machine to act on its own.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

LABEL = "com.sidecar.plan"
AGENTS_DIR = os.path.join(C.HOME, "Library", "LaunchAgents")
PLIST = os.path.join(AGENTS_DIR, LABEL + ".plist")
HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_LOG = os.path.join(C.LOG_DIR, "plan.log")


def is_macos():
    return sys.platform == "darwin"


def python_bin():
    return sys.executable or shutil.which("python3") or "/usr/bin/python3"


def write_plist(interval_minutes):
    C.ensure_dirs()
    os.makedirs(AGENTS_DIR, exist_ok=True)
    program = [
        "/bin/sh", "-lc",
        "%s %s plan >> %s 2>&1"
        % (python_bin(), os.path.join(HERE, "sc_run.py"), PLAN_LOG),
    ]
    payload = {
        "Label": LABEL,
        "ProgramArguments": program,
        "StartInterval": int(interval_minutes) * 60,
        "RunAtLoad": True,
        # A laptop sleeps; without this the job is skipped and never caught up,
        # so a closed lid would silently stop the sidecar.
        "StartCalendarInterval": None,
        "StandardOutPath": PLAN_LOG,
        "StandardErrorPath": PLAN_LOG,
        "EnvironmentVariables": {
            "HOME": C.HOME,
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "SIDECAR_HOME": C.SIDECAR_HOME,
        },
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    payload.pop("StartCalendarInterval")
    with open(PLIST, "wb") as fh:
        plistlib.dump(payload, fh)
    return program


def cmd_install(args):
    if not is_macos():
        # Cron is the portable fallback; print rather than edit the user's
        # crontab, because silently rewriting it is not ours to do.
        line = ("*/%d * * * * %s %s plan >> %s 2>&1"
                % (args.every, python_bin(), os.path.join(HERE, "sc_run.py"), PLAN_LOG))
        C.emit({"kind": "schedule-install", "installed": False, "platform": sys.platform,
                "reason": "not macOS; add this line with 'crontab -e'",
                "crontab_line": line})
        return

    program = write_plist(args.every)
    C.run_cmd(["/bin/launchctl", "unload", PLIST], timeout=15)
    load = C.run_cmd(["/bin/launchctl", "load", "-w", PLIST], timeout=15)
    C.emit({
        "kind": "schedule-install",
        "installed": load["ok"],
        "platform": "darwin",
        "plist": PLIST,
        "every_minutes": args.every,
        "command": program,
        "log": PLAN_LOG,
        "error": None if load["ok"] else load["err"],
        "note": ("the timer runs the read-only plan step only; it never executes work"),
    })


def cmd_uninstall(args):
    if not is_macos():
        C.emit({"kind": "schedule-uninstall", "removed": False, "platform": sys.platform,
                "reason": "remove the crontab line manually"})
        return
    C.run_cmd(["/bin/launchctl", "unload", "-w", PLIST], timeout=15)
    removed = False
    if os.path.exists(PLIST):
        os.unlink(PLIST)
        removed = True
    C.emit({"kind": "schedule-uninstall", "removed": removed, "plist": PLIST})


def cmd_status(args):
    exists = os.path.exists(PLIST)
    listed = C.run_cmd(["/bin/launchctl", "list"], timeout=15)
    loaded = LABEL in (listed["out"] or "")
    last = None
    if os.path.exists(PLAN_LOG):
        try:
            with open(PLAN_LOG, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().strip().splitlines()
                last = lines[-1][:200] if lines else None
        except OSError:
            pass
    C.emit({
        "kind": "schedule-status",
        "platform": sys.platform,
        "plist_exists": exists,
        "loaded": loaded,
        "plist": PLIST,
        "log": PLAN_LOG,
        "log_tail": last,
    })


def main():
    ap = argparse.ArgumentParser(description="Install the sidecar plan timer.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install")
    p.add_argument("--every", type=int, default=30, help="minutes between plan runs")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("uninstall"); p.set_defaults(func=cmd_uninstall)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
