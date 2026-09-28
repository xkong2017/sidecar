#!/usr/bin/env python3
"""sc_mine - turn local history and repo state into a reviewable backlog.

Two independent sources, deliberately kept separate:

  HISTORY  What the human said and did across agent transcripts. Good for
           preferences and for noticing parked work. Poor for verification.
  REPO     Mechanical facts about the working tree. Every signal here comes with
           a command that can prove it, which is what makes it safe to act on.

The output is a backlog file plus a profile file. Nothing is started here. The
backlog is a proposal, and every item carries the evidence and the verification
command that would prove it done - so the human reviews claims, not vibes.

Privacy: transcripts are read locally and never uploaded. The profile is built
by redacting anything that smells like a credential before it is written.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fnmatch
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

BACKLOG = os.path.join(C.STATE_DIR, "backlog.json")
PROFILE = os.path.join(C.STATE_DIR, "profile.json")

# --------------------------------------------------------------------------
# redaction - applied to every string that leaves a transcript
# --------------------------------------------------------------------------

SECRET_PATTERNS = [
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{16,})"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{20,})"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-\.]{20,})"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*[\x27\x22]?([^\s\x27\x22,}]{8,})"),
     r"\1=[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED_PRIVATE_KEY]"),
]


def redact(text, limit=0):
    if not isinstance(text, str):
        return text
    out = text
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    out = out.replace("\x00", "")
    if limit and len(out) > limit:
        out = out[:limit] + "..."
    return out


def fingerprint(text):
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


# --------------------------------------------------------------------------
# history mining
# --------------------------------------------------------------------------

# Vocabulary-level signals. Deliberately loose: these mark passages worth a
# human or model reading again, not conclusions. A phrase match is raw material.
# Defaults. These are overridable from config.mining_tuning so the vocabulary
# can be tuned against a real corpus without editing this file. sc_eval.py
# measures how well they do on data you have labelled yourself.
PARKED_WORDS = [
    "later", "defer", "deferred", "postpone", "blocked", "waiting on", "waiting for",
    "for now", "revisit", "come back to", "next session", "next time", "after we",
    "once we", "not now", "skip", "backlog", "park", "on hold", "someday",
    "out of scope", "todo", "fixme", "hack", "temporary", "workaround",
]

FRICTION_WORDS = [
    "still not working", "still broken", "still failing", "keeps failing",
    "again", "third time", "didn't work", "doesn't work", "not what i asked",
    "i already", "why is it", "why does it", "frustrat", "annoying",
    "that's wrong", "still wrong", "regression",
]


def tuning(cfg):
    """Merged mining vocabulary: shipped defaults, overridden by config."""
    import sc_common as _C
    base = {
        "parked_words": PARKED_WORDS,
        "friction_words": FRICTION_WORDS,
        "max_candidates": 40,
        "max_leads": 60,
        "lead_promotion_threshold": 2,
    }
    base.update(_C.DEFAULT_CONFIG.get("mining_tuning", {}))
    base.update((cfg or {}).get("mining_tuning", {}))
    return base


PREFERENCE_MARKERS = [
    r"\bI (?:prefer|like|want|always|never|hate|dislike)\b",
    r"\bdon'?t (?:ever|use|add|do)\b",
    r"\balways (?:use|do|run|check)\b",
    r"\bnever (?:use|do|add|commit|push)\b",
    r"\bplease (?:always|never|don'?t)\b",
    r"\bwe (?:use|don'?t use)\b",
    r"\bmy (?:preference|style|rule|convention)\b",
    r"\bremember (?:that|to)\b",
]

# Sentinel models that are transcript artifacts, never real inference.
SENTINEL_MODELS = {"<synthetic>", "synthetic", "unknown", ""}


def classify_project(cwd):
    """Turn a working directory into a stable project key."""
    if not cwd:
        return "unknown"
    parts = [p for p in cwd.split(os.sep) if p]
    for marker in ("Documents", "PycharmProjects", "Desktop", "Projects", "src"):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                return os.sep.join(parts[:i + 2])
    return os.sep.join(parts[:4]) if len(parts) > 4 else cwd


# Text that arrives in a user-role turn but was not written by the human:
# skill bodies, system notices, pasted tables, tool output. Left unfiltered these
# dominate every keyword statistic, because they are far longer and more
# keyword-dense than anything a person types.
MACHINE_TURN_MARKERS = [
    r"^Base directory for this skill",
    r"^A session-scoped .* hook is now active",
    r"^The hook will block",
    r"^Note: ",
    r"^System: ",
    r"^<[a-zA-Z_-]+>",
    r"^\|.*\|\s*$",
    r"^\s*[-*]\s+\*\*",
    r"^(Continue from where you left off|Caveat: )",
]


def is_likely_human(text):
    """Heuristic gate for whether a person actually typed this.

    Deliberately conservative. Dropping a real human turn costs one data point;
    keeping a skill body corrupts the whole vocabulary distribution.
    """
    if not text or len(text) > 3000:
        return False
    stripped = text.strip()
    if len(stripped) < 12:
        return False
    for pat in MACHINE_TURN_MARKERS:
        if re.search(pat, stripped, re.I | re.M):
            return False
    # Mostly-structural content (tables, code fences, long bullet runs) is
    # pasted material rather than speech.
    lines = [l for l in stripped.splitlines() if l.strip()]
    if lines:
        structural = sum(1 for l in lines if l.lstrip()[:1] in ("|", "#", ">") or l.startswith("```"))
        if structural / float(len(lines)) > 0.5:
            return False
    if stripped.count("```") >= 2:
        return False
    return True


def sentence_around(text, pos, width=200):
    """Grab a readable window around a match, snapped to sentence-ish bounds."""
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width // 2)
    window = text[start:end]
    for sep in (". ", "\n", "? ", "! "):
        if sep in window[:width // 3]:
            window = window.split(sep, 1)[1] if window.index(sep) < width // 3 else window
    return window.strip()


def read_transcript_lines(root, since_ts, max_files):
    """Yield (path, record) for recent Claude Code transcript records."""
    if not os.path.isdir(root):
        return
    paths = glob.glob(os.path.join(root, "*", "*.jsonl"))
    paths = [p for p in paths if os.stat(p).st_mtime >= since_ts]
    paths.sort(key=lambda p: os.stat(p).st_mtime, reverse=True)
    for path in paths[:max_files]:
        try:
            fh = open(path, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    yield path, json.loads(line)
                except ValueError:
                    continue


def extract_text(rec):
    """Pull human-readable text out of a transcript record."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None, None
    role = msg.get("role") or rec.get("type")
    content = msg.get("content")
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
    text = "\n".join(p for p in parts if p).strip()
    return (role, text) if text else (role, None)


def mine_history(cfg, since_ts):
    """Read transcripts and return raw material, not conclusions.

    This stage is deliberately factual. It reports what was said, where, and how
    often - never what it means. Interpretation belongs to a model that can read
    the surrounding session, which is why each signal carries a pointer back to
    its transcript so a later stage can pull real context for it.
    """
    mining = cfg["mining"]
    tune = tuning(cfg)
    limit = int(mining.get("max_sessions_per_run", 400))
    roots = [os.path.expanduser(r) for r in mining.get("transcript_roots", [])]

    session_meta = {}
    parked = collections.Counter()
    parked_context = {}
    friction = collections.Counter()
    friction_context = {}
    preferences = collections.Counter()
    pref_context = {}
    error_signatures = collections.Counter()
    files_touched = collections.Counter()
    tool_usage = collections.Counter()
    model_mix = collections.Counter()

    for root in roots:
        for path, rec in read_transcript_lines(root, since_ts, limit):
            ts = rec.get("timestamp")
            sid = rec.get("sessionId") or rec.get("session_id") or os.path.basename(path)
            cwd = rec.get("cwd")
            meta = session_meta.setdefault(sid, {
                "session": sid, "cwd": cwd, "turns": 0, "user_turns": 0,
                "last": ts, "first": ts, "models": set(),
                "source": os.path.basename(root), "transcript": path,
            })
            if cwd and not meta.get("cwd"):
                meta["cwd"] = cwd
            meta["turns"] += 1
            if ts:
                meta["last"] = max(meta["last"] or ts, ts)
                meta["first"] = min(meta["first"] or ts, ts)

            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("model"):
                model = msg["model"]
                meta["models"].add(model)
                if model.lower() not in SENTINEL_MODELS:
                    model_mix[model] += 1
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tool_usage[block.get("name") or "?"] += 1
                            inp = block.get("input") or {}
                            fp = inp.get("file_path") or inp.get("path")
                            if isinstance(fp, str):
                                files_touched[fp] += 1

            role, text = extract_text(rec)
            if role != "user" or not text:
                continue
            if not is_likely_human(text):
                continue

            meta["user_turns"] += 1
            low = text.lower()
            ctx = {"session": sid, "cwd": cwd, "when": ts,
                   "transcript": os.path.relpath(path, os.path.expanduser("~"))}

            for word in tune["parked_words"]:
                pos = low.find(word)
                if pos >= 0:
                    quote = redact(sentence_around(text, pos), 200)
                    if quote:
                        parked[quote] += 1
                        parked_context.setdefault(quote, ctx)

            for word in tune["friction_words"]:
                pos = low.find(word)
                if pos >= 0:
                    quote = redact(sentence_around(text, pos), 200)
                    if quote:
                        friction[quote] += 1
                        friction_context.setdefault(quote, ctx)

            for pat in PREFERENCE_MARKERS:
                m = re.search(pat, text, re.I)
                if m:
                    quote = redact(sentence_around(text, m.start()), 240)
                    if quote:
                        preferences[quote] += 1
                        pref_context.setdefault(quote, ctx)

            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_\.]*(?:Error|Exception))", text):
                error_signatures[m.group(1)] += 1

    sessions = []
    for meta in session_meta.values():
        meta["models"] = sorted(meta["models"])[:4]
        sessions.append(meta)
    sessions.sort(key=lambda s: s.get("last") or "", reverse=True)

    projects = collections.defaultdict(lambda: {
        "sessions": 0, "turns": 0, "user_turns": 0, "last": None, "transcripts": set(),
        "parked": 0, "friction": 0,
    })
    for s in sessions:
        key = classify_project(s.get("cwd"))
        p = projects[key]
        p["sessions"] += 1
        p["turns"] += s["turns"]
        p["user_turns"] += s["user_turns"]
        if s.get("transcript"):
            p["transcripts"].add(s["transcript"])
        if s.get("last") and (p["last"] is None or s["last"] > p["last"]):
            p["last"] = s["last"]

    for quote, ctx in list(parked_context.items()) + list(friction_context.items()):
        key = classify_project(ctx.get("cwd"))
        if key in projects:
            projects[key]["parked" if quote in parked_context else "friction"] += 1

    def pack(counter, ctxmap, cap):
        out = []
        for quote, count in counter.most_common(cap):
            entry = {"quote": quote, "count": count}
            entry.update(ctxmap.get(quote, {}))
            out.append(entry)
        return out

    return {
        "sessions_analysed": len(sessions),
        "sessions": sessions[:60],
        "projects": [
            {"name": k, "path": k, "sessions": v["sessions"], "turns": v["turns"],
             "user_turns": v["user_turns"], "last_active": v["last"],
             "parked_hits": v["parked"], "friction_hits": v["friction"],
             "transcripts": sorted(v["transcripts"])[:5]}
            for k, v in sorted(projects.items(), key=lambda kv: kv[1]["turns"], reverse=True)[:40]
        ],
        "parked_signals": pack(parked, parked_context, 40),
        "friction_signals": pack(friction, friction_context, 30),
        "preference_signals": pack(preferences, pref_context, 40),
        "error_signatures": [{"error": k, "count": v} for k, v in error_signatures.most_common(20)],
        "top_edited_files": [{"file": k, "edits": v} for k, v in files_touched.most_common(25)],
        "tool_usage": [{"tool": k, "calls": v} for k, v in tool_usage.most_common(15)],
        "model_mix": dict(model_mix.most_common(10)),
    }


# --------------------------------------------------------------------------
# repo mining
# --------------------------------------------------------------------------

SIGNAL_RULES = [
    {
        "id": "red-tests",
        "tier": 1,
        "title": "Test suite is failing",
        "why": "A failing suite is unambiguous, and the fix is verifiable.",
        "detect": "test command exits non-zero",
        "verify_with": "the project test command exits zero",
        "verify_command": None,   # supplied per-project from the project's own runner
        "marketable": True,
    },
    {
        "id": "plan-open-items",
        "tier": 2,
        "title": "Unchecked items in a committed plan",
        "why": "The human already decided this work matters by writing it down.",
        "detect": "PLAN.md / TODO.md / ROADMAP.md contain unchecked boxes",
        "verify_with": "the referenced change is committed and the box is checked",
        "verify_command": None,   # depends on which item; a human picks first
        "marketable": True,
    },
    {
        "id": "dirty-tree-stale",
        "tier": 2,
        "title": "Uncommitted work left to rot",
        "why": "Parked work is the clearest evidence of an interrupted intention.",
        "detect": "git status is dirty and the newest change is older than stale_days",
        "verify_with": "the tree is clean (work committed or deliberately discarded)",
        "verify_command": "test -z \"$(git status --porcelain)\"",
        "marketable": False,
    },
    {
        "id": "ci-missing",
        "tier": 3,
        "title": "No CI configuration",
        "why": "Without CI there is no automatic gate for anything else the sidecar does.",
        "detect": "no .github/workflows, .gitlab-ci.yml, or equivalent",
        "verify_with": "a workflow file exists in the repository",
        "verify_command": "ls .github/workflows/*.yml .github/workflows/*.yaml .gitlab-ci.yml 2>/dev/null | head -1 | grep -q .",
        "marketable": True,
    },
    {
        "id": "no-tests",
        "tier": 3,
        "title": "No tests detected",
        "why": "Tests are the gate that makes every other task safe to automate.",
        "detect": "no test directory or test-runner config",
        "verify_with": "a test directory or runner config exists",
        "verify_command": "test -d tests -o -d test -o -d spec -o -d __tests__ -o -f pytest.ini",
        "marketable": True,
    },
    {
        "id": "docs-drift",
        "tier": 4,
        "title": "Documentation predates the code",
        "why": "Cheap to fix, low risk, but rarely urgent.",
        "detect": "README.md older than the newest source file by a wide margin",
        "verify_with": "a human reads it and agrees it is now accurate",
        "verify_command": None,
        "marketable": False,
    },
    {
        "id": "interest-drift",
        "tier": 4,
        "title": "Project has gone cold",
        "why": "An untouched repo may mean abandoned, or may mean finished. Only the human knows.",
        "detect": "no commits and no agent sessions for stale_days",
        "verify_with": "a human decides: revive or archive",
        "verify_command": None,
        "marketable": False,
    },
]


def sh(args, cwd=None, timeout=20):
    r = C.run_cmd(["/usr/bin/env"] + args, timeout=timeout)
    return r


def git(args, cwd, timeout=20):
    try:
        import subprocess
        p = subprocess.run(["/usr/bin/git"] + args, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, check=False)
        return {
            "ok": p.returncode == 0,
            "out": p.stdout.decode("utf-8", "replace").strip(),
            "err": p.stderr.decode("utf-8", "replace").strip(),
        }
    except Exception as exc:
        return {"ok": False, "out": "", "err": str(exc)}


def find_repos(cfg):
    roots = [os.path.expanduser(r) for r in cfg["mining"].get("repo_roots", [])]
    depth = int(cfg["mining"].get("repo_scan_depth", 2))
    seen = set()
    repos = []

    def walk(base, level):
        if level > depth or len(repos) > 120:
            return
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except (OSError, PermissionError):
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            name = e.name
            # A .git directory is the marker we are looking for, so test it before
            # the hidden-directory filter below would swallow it.
            if name == ".git":
                real = os.path.realpath(base)
                if real not in seen:
                    seen.add(real)
                    repos.append(real)
                continue
            if name.startswith(".") or name in ("node_modules", "Library", "Applications",
                                                "Pictures", "Music", "Movies", "Public",
                                                "Downloads", "Desktop", "Documents"):
                continue
            walk(e.path, level + 1)

    for root in roots:
        walk(root, 0)
    return repos


TEST_HINTS = [
    ("pytest", ["pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"]),
    ("package.json test", ["package.json"]),
    ("xcodebuild", ["*.xcodeproj", "*.xcworkspace"]),
    ("cargo test", ["Cargo.toml"]),
    ("go test", ["go.mod"]),
    ("make test", ["Makefile"]),
]


def analyse_repo(path, cfg):
    stale_days = int(cfg["mining"].get("stale_days", 30))
    info = {
        "path": path,
        "name": os.path.basename(path),
        "signals": [],
        "facts": {},
    }

    g = git(["rev-parse", "--is-inside-work-tree"], path)
    if not g["ok"]:
        return None

    head = git(["log", "-1", "--format=%cI%x09%s"], path)
    info["facts"]["last_commit"] = head["out"] if head["ok"] else None
    last_commit_age_days = None
    if head["ok"] and head["out"]:
        try:
            iso = head["out"].split("\t")[0]
            when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
            last_commit_age_days = round((C.now() - when) / 86400, 1)
            info["facts"]["last_commit_age_days"] = last_commit_age_days
            info["facts"]["last_commit_subject"] = head["out"].split("\t")[-1]
        except (ValueError, IndexError):
            pass

    status = git(["status", "--porcelain"], path)
    dirty_files = [l for l in status["out"].splitlines() if l.strip()] if status["ok"] else []
    info["facts"]["dirty_files"] = len(dirty_files)
    if dirty_files:
        newest = 0
        for rel in dirty_files[:200]:
            rel = rel[3:].strip().strip('"')
            full = os.path.join(path, rel)
            try:
                newest = max(newest, os.path.getmtime(full))
            except OSError:
                pass
        dirty_age_days = round((C.now() - newest) / 86400, 1) if newest else None
        info["facts"]["dirty_age_days"] = dirty_age_days
        if dirty_age_days is not None and dirty_age_days > stale_days:
            info["signals"].append({
                "id": "dirty-tree-stale",
                "evidence": "%d uncommitted files, untouched for %.0f days"
                            % (len(dirty_files), dirty_age_days),
                "samples": dirty_files[:8],
            })

    branches = git(["for-each-ref", "--sort=-committerdate",
                    "--format=%(refname:short)\t%(committerdate:short)", "refs/heads/"], path)
    info["facts"]["branches"] = branches["out"].splitlines()[:10] if branches["ok"] else []

    # plan files with unchecked boxes
    for plan_name in ("PLAN.md", "TODO.md", "ROADMAP.md", "plan.md", "todo.md"):
        fp = os.path.join(path, plan_name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                body = fh.read()
        except OSError:
            continue
        open_items = re.findall(r"^\s*[-*]\s*\[\s*\]\s*(.+)$", body, re.M)
        done_items = re.findall(r"^\s*[-*]\s*\[[xX]\]\s*(.+)$", body, re.M)
        if open_items:
            info["signals"].append({
                "id": "plan-open-items",
                "evidence": "%s has %d unchecked of %d items"
                            % (plan_name, len(open_items), len(open_items) + len(done_items)),
                "samples": [redact(x, 160) for x in open_items[:8]],
            })
        info["facts"]["plan_files"] = info["facts"].get("plan_files", []) + [plan_name]

    # test and CI presence
    has_tests = any(
        os.path.isdir(os.path.join(path, d))
        for d in ("tests", "test", "spec", "__tests__", "Tests")
    ) or any(
        os.path.isfile(os.path.join(path, f))
        for f in ("pytest.ini", "tox.ini", "Cargo.toml", "go.mod", "vitest.config.ts",
                  "jest.config.js", "jest.config.ts")
    )
    has_ci = any(os.path.exists(os.path.join(path, p)) for p in (
        ".github/workflows", ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml",
        ".buildkite", "Jenkinsfile",
    ))
    info["facts"]["has_tests"] = has_tests
    info["facts"]["has_ci"] = has_ci

    if not has_ci and (has_tests or os.path.isfile(os.path.join(path, "package.json"))):
        info["signals"].append({
            "id": "ci-missing",
            "evidence": "no CI configuration found",
            "samples": [],
        })
    if not has_tests and any(os.path.isfile(os.path.join(path, f)) for f in
                             ("package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod")):
        info["signals"].append({
            "id": "no-tests",
            "evidence": "no test directory or runner configuration found",
            "samples": [],
        })

    readme = os.path.join(path, "README.md")
    newest_src = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
        for f in filenames:
            if f.endswith((".py", ".ts", ".tsx", ".js", ".swift", ".go", ".rs", ".java")):
                try:
                    newest_src = max(newest_src, os.path.getmtime(os.path.join(dirpath, f)))
                except OSError:
                    pass
        if dirpath.count(os.sep) - path.count(os.sep) > 3:
            dirnames[:] = []

    if os.path.isfile(readme) and newest_src:
        try:
            drift_days = (newest_src - os.path.getmtime(readme)) / 86400
            info["facts"]["readme_drift_days"] = round(drift_days, 1)
            if drift_days > 60:
                info["signals"].append({
                    "id": "docs-drift",
                    "evidence": "README is %.0f days older than the newest source file" % drift_days,
                    "samples": [],
                })
        except OSError:
            pass

    # Materiality gate. "No commits for 90 days" is true of every scratch repo,
    # every archived experiment and every template clone. Before calling one of
    # them cold, require evidence that work was ever actually invested here:
    # real history, or a dirty tree, or a plan file. Otherwise the signal fires
    # on dozens of throwaways and drowns the backlog.
    commits = git(["rev-list", "--count", "HEAD"], path)
    try:
        commit_count = int(commits["out"]) if commits["ok"] else 0
    except ValueError:
        commit_count = 0
    info["facts"]["commit_count"] = commit_count

    substantive = (
        commit_count >= 10
        or bool(dirty_files)
        or bool(info["facts"].get("plan_files"))
    )
    info["facts"]["substantive"] = substantive

    if (last_commit_age_days is not None and last_commit_age_days > stale_days
            and substantive):
        info["signals"].append({
            "id": "interest-drift",
            "evidence": ("no commits for %.0f days, but %d commits of real history"
                         % (last_commit_age_days, commit_count)),
            "samples": [info["facts"].get("last_commit_subject") or ""],
        })
    elif last_commit_age_days is not None and last_commit_age_days > stale_days:
        info.setdefault("leads", []).append({
            "id": "interest-drift",
            "evidence": ("quiet for %.0f days, only %d commits - probably scratch"
                         % (last_commit_age_days, commit_count)),
        })

    return info


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_candidate(tier, history_hits, repo_signal):
    """Confidence x impact, damped hard by tier.

    The tier is the dominant term on purpose. A tier-4 idea with lots of
    historical support is still a tier-4 idea, and should never outrank a
    failing test suite.
    """
    impact = {1: 9.0, 2: 6.5, 3: 4.0, 4: 2.0}.get(tier, 1.0)
    confidence = 0.4
    if repo_signal:
        confidence += 0.45          # a mechanical fact we can point at
    if history_hits >= 3:
        confidence += 0.15
    elif history_hits >= 1:
        confidence += 0.07
    confidence = min(confidence, 1.0)

    wsjj = (impact * confidence) / max(0.5, {1: 1.0, 2: 1.5, 3: 2.0, 4: 2.5}.get(tier, 3.0))
    return {
        "impact": round(impact, 2),
        "confidence": round(confidence, 2),
        "score": round(wsjj, 2),
    }


def build_candidates(cfg, history, repos):
    """Turn facts into proposals.

    Two rules keep this from degenerating into busywork:

      Volume cap. Signals that fire on dozens of repos are collapsed into ONE
      candidate with the full list attached. Thirty-odd 'this repo is cold'
      entries is not a backlog, it is a directory listing.

      Verification required. Every candidate names the command that would prove
      it done. A candidate that cannot name one is recorded as material for a
      human, never as work for the sidecar.
    """
    rules = {r["id"]: r for r in SIGNAL_RULES}
    max_auto_tier = int(cfg["approval"].get("max_auto_tier", 2))
    candidates = []

    # --- repo-derived candidates, grouped by signal so noisy signals collapse
    per_signal = collections.defaultdict(list)
    for repo in repos:
        for sig in repo["signals"]:
            per_signal[sig["id"]].append((repo, sig))

    for signal_id, hits in per_signal.items():
        rule = rules.get(signal_id)
        if not rule:
            continue

        if len(hits) == 1:
            repo, sig = hits[0]
            sc = score_candidate(rule["tier"], 0, True)
            candidates.append({
                "id": "repo:%s:%s" % (signal_id, fingerprint(repo["path"])),
                "source": "repo",
                "tier": rule["tier"],
                "title": "%s: %s" % (rule["title"], repo["name"]),
                "why": rule["why"],
                "target": repo["path"],
                "targets": [repo["path"]],
                "evidence": [sig["evidence"]] + [str(s) for s in sig.get("samples", []) if s],
                "detect": rule["detect"],
                "verify_with": rule["verify_with"],
                "verify_command": rule.get("verify_command"),
                "marketable": rule["marketable"],
                "auto_ok": rule["tier"] <= max_auto_tier,
                "status": "proposed",
                **sc,
            })
        else:
            # Collapsed candidate. Confidence rises with the count, but impact
            # does not: the work is the same work, done N times.
            sc = score_candidate(rule["tier"], min(len(hits), 6), True)
            listing = sorted(hits, key=lambda h: h[0]["name"])
            candidates.append({
                "id": "repo:%s:bulk" % signal_id,
                "source": "repo",
                "tier": rule["tier"],
                "title": "%s (%d projects)" % (rule["title"], len(hits)),
                "why": rule["why"] + " Collapsed into one item so a single noisy signal cannot flood the backlog.",
                "target": None,
                "targets": [h[0]["path"] for h in listing],
                "evidence": ["%s - %s" % (h[0]["name"], h[1]["evidence"]) for h in listing][:30],
                "detect": rule["detect"],
                "verify_with": rule["verify_with"],
                "verify_command": rule.get("verify_command"),
                "marketable": rule["marketable"],
                # A bulk item is never auto-runnable: each project needs its own
                # decision before anything touches it.
                "auto_ok": False,
                "status": "proposed",
                "bulk": True,
                "count": len(hits),
                **sc,
            })

    cap = int(tuning(cfg).get("max_candidates", 40))
    candidates.sort(key=lambda c: (c["tier"], -c["score"], c["title"]))
    return candidates[:cap]


def build_leads(cfg, history, repos):
    """Raw material for the interpretation stage. Never auto-executed.

    Keyword matching over transcripts is far too blunt to produce work on its
    own - a single 'later' is not a parked project. Rather than pad the backlog
    with guesses, these go to a separate list that an LLM pass reads *with the
    surrounding session attached*, and that can be promoted to a real candidate
    only once it cites something concrete.
    """
    leads = []

    tune = tuning(cfg)
    min_hits = int(tune.get("lead_promotion_threshold", 1))
    max_leads = int(tune.get("max_leads", 60))
    park_cap = max(1, max_leads * 2 // 5)
    for item in [s for s in history.get("parked_signals", [])
                 if s.get("count", 1) >= min_hits][:park_cap]:
        leads.append({
            "id": "lead:parked:" + fingerprint(item["quote"]),
            "kind": "possibly_parked",
            "quote": item["quote"],
            "count": item.get("count", 1),
            "project": item.get("cwd"),
            "session": item.get("session"),
            "when": item.get("when"),
            "transcript": item.get("transcript"),
            "promote_if": ("the surrounding session shows a specific unfinished change, "
                           "and names a file or command that would close it"),
        })

    for item in [s for s in history.get("friction_signals", [])
                 if s.get("count", 1) >= min_hits][:max(1, max_leads // 3)]:
        leads.append({
            "id": "lead:friction:" + fingerprint(item["quote"]),
            "kind": "possible_friction",
            "quote": item["quote"],
            "count": item.get("count", 1),
            "project": item.get("cwd"),
            "session": item.get("session"),
            "when": item.get("when"),
            "transcript": item.get("transcript"),
            "promote_if": ("the same friction appears in three or more separate sessions "
                           "and a concrete rule, default, or tool change would remove it"),
        })

    repo_leads = []
    for repo in repos:
        for lead in repo.get("leads", []):
            repo_leads.append({
                "id": "lead:%s:%s" % (lead["id"], fingerprint(repo["path"])),
                "kind": "low_materiality",
                "quote": lead["evidence"],
                "count": 1,
                "project": repo["path"],
                "session": None,
                "when": repo["facts"].get("last_commit"),
                "transcript": None,
                "promote_if": "the human confirms this project still matters",
            })

    # max_leads caps the whole list, not just the history half. Capping only one
    # source would let the other grow without bound as the repo scan widens.
    return (leads + repo_leads)[:max_leads]


def dedupe(candidates, previous):
    """Carry the human's decisions forward across mining runs.

    Three things must survive a re-mine, and each needs different handling:

      A decision (approved / rejected) sticks to an id, so a finding that is
      still present does not come back asking the same question.

      A tombstone for rejected ids is kept even when the finding disappears,
      because otherwise a rejection is forgotten the moment the signal blinks
      off and the same proposal returns next week.

      A human-created item (an approved single target split out of a bulk
      finding) is not regenerated by mining at all, so it has to be carried
      over wholesale or the approval is silently lost.
    """
    previous = previous or {}
    prior = {p["id"]: p for p in previous.get("candidates", [])}
    tombstones = dict(previous.get("decisions", {}))

    merged = []
    seen = set()
    for c in candidates:
        old = prior.get(c["id"])
        if old:
            c["status"] = old.get("status", "proposed")
            c["first_seen"] = old.get("first_seen") or C.iso()
            c["human_note"] = old.get("human_note")
            c["auto_ok"] = old.get("auto_ok", c.get("auto_ok"))
            c["runs"] = old.get("runs", 0) + 1
        else:
            # A tombstoned id stays rejected even after the finding vanishes
            # and returns.
            if tombstones.get(c["id"]) == "rejected":
                c["status"] = "rejected"
                c["auto_ok"] = False
                c["human_note"] = "previously rejected"
            c["first_seen"] = C.iso()
            c["runs"] = 1
        c["last_seen"] = C.iso()
        seen.add(c["id"])
        merged.append(c)

    # Carry forward anything the human created or decided that mining did not
    # regenerate. Without this, approving a single target and then re-mining
    # would quietly delete the approved work.
    for pid, old in prior.items():
        if pid in seen:
            continue
        if old.get("status") in ("approved", "done", "blocked") or old.get("parent_id"):
            old = dict(old)
            old["last_seen"] = C.iso()
            merged.append(old)
            seen.add(pid)

    # Refresh the tombstone table from the merged result.
    for c in merged:
        if c.get("status") in ("rejected", "approved"):
            tombstones[c["id"]] = c["status"]

    return merged, tombstones


def main():
    ap = argparse.ArgumentParser(description="Mine history and repos into a backlog.")
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--no-repos", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = C.load_config()
    since = C.now() - args.lookback_days * 86400

    history = {"sessions_analysed": 0, "parked_moments": [], "friction_moments": [],
               "preference_signals": [], "projects": [], "error_signatures": [],
               "top_edited_files": []}
    if not args.no_history:
        history = mine_history(cfg, since)

    repos = []
    if not args.no_repos:
        for path in find_repos(cfg):
            info = analyse_repo(path, cfg)
            if info:
                repos.append(info)
    repos.sort(key=lambda r: r["facts"].get("last_commit_age_days") or 0)

    candidates = build_candidates(cfg, history, repos)
    leads = build_leads(cfg, history, repos)

    previous = C.read_json(BACKLOG, {}) or {}
    candidates, tombstones = dedupe(candidates, previous)

    profile = {
        "generated_at": C.iso(),
        "lookback_days": args.lookback_days,
        "sessions_analysed": history["sessions_analysed"],
        "projects": history.get("projects", []),
        "preference_signals": history.get("preference_signals", []),
        "top_edited_files": history.get("top_edited_files", []),
        "error_signatures": history.get("error_signatures", []),
        "repos": [
            {"path": r["path"], "name": r["name"], "facts": r["facts"],
             "signal_count": len(r["signals"])}
            for r in repos
        ],
        "note": "Built locally from transcripts and git state. Secrets are redacted.",
    }

    backlog = {
        "schemaVersion": 1,
        "generated_at": C.iso(),
        "counts": {
            "candidates": len(candidates),
            "by_tier": dict(collections.Counter(c["tier"] for c in candidates)),
            "auto_ok": sum(1 for c in candidates if c["auto_ok"]),
            "leads": len(leads),
            "repos_scanned": len(repos),
            "sessions_scanned": history["sessions_analysed"],
        },
        "candidates": candidates,
        "decisions": tombstones,
        "leads": leads,
        "leads_note": ("Unverified raw material. A lead is promoted to a candidate only "
                       "after an interpretation pass reads its transcript and cites a "
                       "specific unfinished change."),
    }

    C.write_json_atomic(BACKLOG, backlog)
    C.write_json_atomic(PROFILE, profile)

    if args.quiet:
        print(json.dumps(backlog["counts"]))
    else:
        C.emit({"kind": "mine", "backlog": BACKLOG, "profile": PROFILE,
                "counts": backlog["counts"],
                "top": [{k: c[k] for k in ("id", "tier", "score", "title", "target")}
                        for c in candidates[:10]]})


if __name__ == "__main__":
    main()