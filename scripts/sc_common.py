#!/usr/bin/env python3
"""sc_common - shared helpers for the sidecar skill.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

HOME = os.path.expanduser("~")
SIDECAR_HOME = os.environ.get("SIDECAR_HOME", os.path.join(HOME, ".sidecar"))
CONFIG_PATH = os.path.join(SIDECAR_HOME, "config.json")
STATE_DIR = os.path.join(SIDECAR_HOME, "state")
LOG_DIR = os.path.join(SIDECAR_HOME, "logs")
RUNS_DIR = os.path.join(SIDECAR_HOME, "runs")

DEFAULT_CONFIG: Dict[str, Any] = {
    "machine": {
        "cpu_cores": 0,
        "load_ceiling_green": 0.25,
        "load_ceiling_yellow": 0.60,
        "user_idle_green_s": 900,
        "user_idle_yellow_s": 300,
        "min_free_mem_pct": 22,
        "block_when_console_locked": False,
        "require_ac_power": True,
    },
    "endpoints": [],
    "budget": {
        "daily_task_usd": 5.0,
        "daily_total_usd": 15.0,
        "weekly_total_usd": 60.0,
        "daily_wall_clock_min": 240,
        "daily_gpu_min": 240,
        "daily_tokens": 20000000,
        "daily_tasks": 8,
        "reserve_fraction": 0.25,
    },
    "approval": {
        "mode": "ask",
        "max_auto_tier": 2,
        "auto_task_max_usd": 1.0,
        "auto_task_max_minutes": 45,
        "require_human_after_failures": 2,
    },
    "guardrails": {
        "allowed_roots": [HOME],
        "deny_globs": [
            "*/.ssh/*", "*/.aws/*", "*/.gnupg/*", "*/.config/gh/*",
            "*/.dsh/*", "*/.claude/*", "*/Library/Keychains/*",
            "*.pem", "*.key", "id_rsa*", "*.env", ".env.*",
            "*/secrets/*", "credentials*",
        ],
        "allow_commit": True,
        "allow_push": False,
        "working_branch_prefix": "sidecar/",
        "max_files_changed": 25,
        "max_lines_changed": 800,
        "max_runtime_minutes": 45,
        "max_failures_per_task": 3,
    },
    "mining": {
        "transcript_roots": [
            os.path.join(HOME, ".claude", "projects"),
            os.path.join(HOME, ".dsh", "sessions"),
        ],
        "repo_roots": [HOME],
        "repo_scan_depth": 2,
        "stale_days": 30,
        "max_sessions_per_run": 400,
        "redact": True,
    },
    # Mining vocabulary lives in config so it can be tuned against a real corpus
    # without editing code. The values shipped here are a starting point derived
    # from a small sample, not measured optima - see sc_eval.py to calibrate
    # them against your own labelled data.
    "mining_tuning": {
        "parked_words": [
            "later", "defer", "deferred", "postpone", "blocked", "waiting on",
            "waiting for", "for now", "revisit", "come back to", "next session",
            "next time", "after we", "once we", "not now", "skip", "backlog",
            "park", "on hold", "someday", "out of scope", "todo", "fixme",
            "hack", "temporary", "workaround",
        ],
        "friction_words": [
            "still not working", "still broken", "still failing", "keeps failing",
            "again", "third time", "didn't work", "doesn't work", "not what i asked",
            "i already", "why is it", "why does it", "frustrat", "annoying",
            "that's wrong", "still wrong", "regression",
        ],
        "max_candidates": 40,
        "max_leads": 60,
        "lead_promotion_threshold": 2,
    },
    "quota": {
        "window_hours": 5.0,
        "providers": [
            {
                "id": "claude-subscription",
                "source": "transcripts",
                "transcript_root": os.path.join(HOME, ".claude", "projects"),
                "window_hours": 5.0,
                "window_token_ceiling": 0,
                "model_exclude_globs": ["*glm*", "*deepseek*", "*qwen*", "<synthetic>"],
            }
        ],
    },
}


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------

def ensure_dirs() -> None:
    for d in (SIDECAR_HOME, STATE_DIR, LOG_DIR, RUNS_DIR):
        os.makedirs(d, exist_ok=True)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str, obj: Any) -> None:
    """Write JSON through a temp file + rename so a crash never tears the file."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


def fingerprint(text: str, length: int = 12) -> str:
    """Short stable id for a piece of text, used to key backlog items."""
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:length]


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Dict[str, Any]:
    ensure_dirs()
    user = read_json(CONFIG_PATH, {}) or {}
    return deep_merge(DEFAULT_CONFIG, user)


# --------------------------------------------------------------------------
# shell
# --------------------------------------------------------------------------

def run_cmd(cmd: List[str], timeout: int = 20) -> Dict[str, Any]:
    """Run a command and never raise. Returns ok, code, out, err."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": p.returncode == 0,
            "code": p.returncode,
            "out": p.stdout.decode("utf-8", "replace").strip(),
            "err": p.stderr.decode("utf-8", "replace").strip(),
        }
    except FileNotFoundError:
        return {"ok": False, "code": 127, "out": "", "err": "not found: " + cmd[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "out": "", "err": "timeout"}
    except Exception as exc:
        return {"ok": False, "code": 1, "out": "", "err": str(exc)}


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def now() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    """Local ISO-8601 with a colon in the UTC offset.

    strftime %z yields -0400, and datetime.fromisoformat only learned to read
    that form in Python 3.11. Since these strings are written to state files and
    read back on whatever interpreter is around, emit the colon form so they
    parse everywhere.
    """
    raw = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else now()))
    if len(raw) >= 5 and raw[-5] in "+-" and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    return raw


def parse_iso(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp tolerantly, returning epoch seconds or None.

    Accepts Z, +00:00, and the +0000 form, and a missing offset. Returns None
    rather than raising, so callers must decide what an unparseable time means
    instead of inheriting a silent default.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def day_key(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else now()))


def iso_week_key(ts: Optional[float] = None) -> str:
    t = time.localtime(ts if ts is not None else now())
    return "%d-W%02d" % (t.tm_year, int(time.strftime("%W", t)))


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def emit(obj: Any, human: Optional[str] = None) -> None:
    if human:
        sys.stderr.write(human.rstrip() + "\n")
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write("sidecar: " + msg + "\n")
    sys.exit(code)