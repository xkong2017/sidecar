#!/usr/bin/env python3
"""sc_statusline - capture authoritative quota state from Claude Code.

Claude Code hands its status-line command a JSON blob on stdin that includes
rate_limits.five_hour and rate_limits.seven_day, each with used_percentage and
resets_at. That is a supported, first-party surface, and it is strictly better
than reconstructing quota from transcripts.

Install once:

    claude config set statusLine.command "python3 /path/to/sc_statusline.py"

or add it to ~/.claude/settings.json:

    "statusLine": {"type": "command",
                   "command": "python3 ~/.agents/skills/sidecar/scripts/sc_statusline.py"}

It records what it sees to $SIDECAR_HOME/state/rate_limits.json and prints a
one-line status. sc_quota.py prefers that file over its own estimate.

Reads stdin, writes state, prints one line. Never fails loudly: a status line
that crashes makes the editor unusable, so every error path prints something
harmless instead.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

STATE = os.path.join(C.STATE_DIR, "rate_limits.json")


def pct(value):
    """Accept 0-1 or 0-100 and normalise to 0-100."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v * 100, 1) if v <= 1.0 else round(v, 1)


def resets_in_text(epoch):
    if not epoch:
        return ""
    try:
        left = int(float(epoch) - C.now())
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "reset now"
    h, m = divmod(left // 60, 60)
    return "resets in %dh%02dm" % (h, m) if h else "resets in %dm" % m


def main():
    raw = ""
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        pass

    doc = {}
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except ValueError:
        doc = {}

    limits = (doc.get("rate_limits") or {}) if isinstance(doc, dict) else {}
    captured = {
        "captured_at": C.iso(),
        "source": "claude-code-statusline",
        "five_hour": None,
        "seven_day": None,
        "model": (doc.get("model") or {}).get("display_name") if isinstance(doc.get("model"), dict) else None,
        "raw_keys": sorted(doc.keys()) if isinstance(doc, dict) else [],
    }

    for key, out_key in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
        block = limits.get(key)
        if isinstance(block, dict):
            captured[out_key] = {
                "used_percentage": pct(block.get("used_percentage")),
                "resets_at": block.get("resets_at"),
                "resets_in_s": (int(float(block["resets_at"]) - C.now())
                                if block.get("resets_at") else None),
            }
        elif block is not None:
            captured[out_key] = {"used_percentage": pct(block)}

    # Only persist when the payload actually contained rate limits, so a Claude
    # Code version that stops sending them cannot overwrite good data with nulls.
    have_data = bool(captured["five_hour"] or captured["seven_day"])
    if have_data:
        try:
            C.write_json_atomic(STATE, captured)
        except OSError:
            pass

    # --- status line
    bits = []
    model = captured.get("model")
    if model:
        bits.append(str(model))
    five = captured.get("five_hour") or {}
    seven = captured.get("seven_day") or {}
    if five.get("used_percentage") is not None:
        bits.append("5h %s%%" % five["used_percentage"])
        r = resets_in_text(five.get("resets_at"))
        if r:
            bits.append(r)
    if seven.get("used_percentage") is not None:
        bits.append("7d %s%%" % seven["used_percentage"])

    if not bits:
        bits.append("sidecar: no rate limits in payload")

    # A quiet marker when this window is nearly spent, since the whole point is
    # to keep work off a window that is about to refuse it.
    if five.get("used_percentage") is not None and five["used_percentage"] >= 85:
        bits.append("LOW")

    sys.stdout.write(" | ".join(bits))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
