#!/usr/bin/env python3
"""sc_quota - reconstruct subscription quota usage from local agent transcripts.

Neither Anthropic nor OpenAI exposes a supported API for "how much of my rolling
window is left". But every assistant turn a transcript-writing CLI records
carries a token usage block. Summing those per window estimates consumption.

Two things this buys:

  1. Live pressure - how much of the current rolling window is burned, and when
     the window rolls over.
  2. Idle windows - which 5-hour blocks over the past N days were never used.
     On a flat-rate subscription an unused window is capacity that expires
     worthless, which is exactly what a sidecar should be filling.

Everything here is an ESTIMATE derived from local files. It is not billing data,
and it under-counts anything done on another machine or in the web UI.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fnmatch
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402


def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def is_excluded(model, patterns):
    for pat in patterns or []:
        if fnmatch.fnmatch(str(model).lower(), pat.lower()):
            return True
    return False


def collect(transcript_root, since_ts, excludes):
    """Single pass over recent transcripts. Returns a sorted list of (ts, tokens).

    Only files modified after since_ts are opened, and only lines containing the
    literal substring 'usage' are JSON-parsed, so a multi-gigabyte transcript
    directory stays cheap.
    """
    records = []
    models = collections.Counter()
    if not os.path.isdir(transcript_root):
        return records, models

    for path in glob.glob(os.path.join(transcript_root, "*", "*.jsonl")):
        try:
            if os.stat(path).st_mtime < since_ts:
                continue
        except OSError:
            continue
        try:
            fh = open(path, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = parse_ts(rec.get("timestamp"))
                if ts is None or ts < since_ts:
                    continue
                model = msg.get("model", "unknown")
                if is_excluded(model, excludes):
                    continue
                tokens = (int(usage.get("input_tokens", 0) or 0)
                          + int(usage.get("cache_creation_input_tokens", 0) or 0)
                          + int(usage.get("cache_read_input_tokens", 0) or 0)
                          + int(usage.get("output_tokens", 0) or 0))
                records.append((ts, tokens))
                models[model] += tokens

    records.sort(key=lambda r: r[0])
    return records, models


def window_block(ts, window_hours, anchor_hour=0):
    """Start timestamp of the fixed window block containing ts."""
    local = dt.datetime.fromtimestamp(ts)
    day_start = local.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)
    delta_h = (local - day_start).total_seconds() / 3600.0
    if delta_h < 0:
        day_start -= dt.timedelta(days=1)
        delta_h = (local - day_start).total_seconds() / 3600.0
    return int(day_start.timestamp() + int(delta_h // window_hours) * window_hours * 3600)


def read_rate_limits():
    """Prefer the authoritative payload Claude Code writes for the status line.

    Transcript reconstruction is a fallback for when that is unavailable. When
    both exist they should roughly agree, and a large disagreement is itself
    worth reporting - it means the estimate is drifting.
    """
    return C.read_json(os.path.join(C.STATE_DIR, "rate_limits.json"), None)


def reconcile(exact, estimate_tokens, ceiling):
    if not isinstance(exact, dict):
        return None
    five = exact.get("five_hour") or {}
    used_pct = five.get("used_percentage")
    out = {
        "available": used_pct is not None,
        "used_percentage": used_pct,
        "resets_at": five.get("resets_at"),
        "resets_in_s": five.get("resets_in_s"),
        "captured_at": exact.get("captured_at"),
        "seven_day": (exact.get("seven_day") or {}).get("used_percentage"),
        "source": exact.get("source"),
    }
    if used_pct is not None and ceiling:
        out["used_tokens_implied"] = int(ceiling * used_pct / 100.0)
        out["headroom_tokens"] = int(max(0, ceiling * (1.0 - used_pct / 100.0)))
        if estimate_tokens:
            drift = abs(estimate_tokens - out["used_tokens_implied"])
            out["estimate_drift_tokens"] = int(drift)
            out["estimate_agrees"] = bool(drift < 0.35 * max(1, out["used_tokens_implied"]))
    return out


def analyse(root, window_hours, lookback_days, excludes, ceiling=0, reserve=0.25):
    now = C.now()
    since = now - lookback_days * 86400

    records, models = collect(root, since, excludes)
    all_records, all_models = collect(root, since, [])

    # A provider configured for cloud quota that shows zero turns usually means
    # either (a) the user genuinely does not use it, or (b) the exclusion globs
    # are swallowing everything. Say so rather than reporting a silently empty
    # window, because both cases look identical in the numbers.
    warning = None
    if not records and all_records:
        warning = ("no turns matched this provider's model filters, but %d turns "
                   "exist from other models (%s) - check model_exclude_globs"
                   % (len(all_records), ", ".join(list(all_models)[:4])))
    elif not records:
        warning = ("no transcript records found under this root in the last %d days - "
                   "headroom is unknown, not full" % lookback_days)

    per_block = collections.Counter()
    per_block_reqs = collections.Counter()
    per_day = collections.Counter()
    total = 0
    for ts, tokens in records:
        per_block[window_block(ts, window_hours)] += tokens
        per_block_reqs[window_block(ts, window_hours)] += 1
        per_day[C.day_key(ts)] += tokens
        total += tokens

    # --- current rolling window, anchored on the oldest turn still inside it
    horizon = now - window_hours * 3600
    in_window = [r for r in records if r[0] >= horizon]
    rolling_tokens = sum(r[1] for r in in_window)

    resets_in = None
    elapsed = window_hours * 3600
    if in_window:
        resets_in = max(0, int(in_window[0][0] + window_hours * 3600 - now))
        elapsed = max(60.0, now - in_window[0][0])
    rate_per_min = (rolling_tokens / (elapsed / 60.0)) if rolling_tokens else 0.0

    usable = ceiling * (1.0 - reserve) if ceiling else 0

    # "No transcripts found" and "found transcripts, none in this window" are
    # different claims. Only the second one supports a headroom number. Without
    # any data at all, reporting 100% headroom would tell a fresh install it has
    # a full window of budget when the truth is that nothing is known.
    data_available = bool(records)
    headroom_pct = None
    if ceiling and usable and data_available:
        headroom_pct = round(100.0 * max(0.0, usable - rolling_tokens) / usable, 1)

    # --- which recent window blocks went unused
    blocks = []
    b = window_block(now - lookback_days * 86400, window_hours)
    last = window_block(now, window_hours)
    while b <= last:
        blocks.append({
            "block_start": C.iso(b),
            "block_end": C.iso(b + window_hours * 3600),
            "tokens": per_block.get(b, 0),
            "requests": per_block_reqs.get(b, 0),
            "current": b == last,
        })
        b += int(window_hours * 3600)

    completed = [x for x in blocks if not x["current"]]
    idle = [x for x in completed if x["tokens"] == 0]
    light = [x for x in completed if 0 < x["tokens"] < (ceiling * 0.25 if ceiling else 1e18)]

    return {
        "id": None,
        "source_root": root,
        "window_hours": window_hours,
        "lookback_days": lookback_days,
        "totals": {
            "tokens": total,
            "turns": len(records),
            "models": dict(models.most_common(8)),
        },
        "rolling_window": {
            "tokens": rolling_tokens,
            "turns": len(in_window),
            "started_at": C.iso(in_window[0][0]) if in_window else None,
            "resets_in_s": resets_in,
            "tokens_per_min": round(rate_per_min, 1),
            "projected_tokens_at_reset": int(rolling_tokens + rate_per_min * (resets_in or 0) / 60.0),
        },
        "ceiling": {
            "configured_tokens": ceiling,
            "usable_after_reserve": int(usable),
            "reserve_fraction": reserve,
            "headroom_tokens": int(max(0, usable - rolling_tokens)) if ceiling else None,
            "headroom_pct": headroom_pct,
        },
        "windows": {
            "completed": len(completed),
            "never_used": len(idle),
            "lightly_used": len(light),
            "idle_block_starts": [x["block_start"] for x in idle][:24],
            "recent": blocks[-24:],
        },
        "per_day_tokens": dict(sorted(per_day.items())[-14:]),
        "all_models_seen": dict(all_models.most_common(10)),
        "data_available": data_available,
        "warning": warning,
        "exact": reconcile(read_rate_limits(), rolling_tokens, ceiling),
        "confidence": "estimated-from-local-transcripts",
        "caveats": [
            "Counts only turns recorded on this machine by transcript-writing CLIs.",
            "Web UI, mobile, and other machines are invisible to this estimate.",
            "Set quota.providers[].window_token_ceiling to your plan's real cap to get headroom numbers.",
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="Estimate subscription quota burn and idle windows.")
    ap.add_argument("--provider", default="", help="provider id from config (default: all)")
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--ceiling", type=int, default=0, help="override window token ceiling")
    args = ap.parse_args()

    cfg = C.load_config()
    providers = cfg.get("quota", {}).get("providers", [])
    if not providers:
        C.emit({"kind": "quota", "providers": [], "error": "no quota providers configured"})
        return
    if args.provider:
        providers = [p for p in providers if p.get("id") == args.provider] or providers[:1]

    out = []
    for p in providers:
        root = os.path.expanduser(p.get("transcript_root", ""))
        window_hours = float(p.get("window_hours") or cfg["quota"].get("window_hours", 5.0))
        ceiling = args.ceiling or int(p.get("window_token_ceiling") or 0)
        reserve = float(p.get("reserve_fraction", cfg["budget"].get("reserve_fraction", 0.25)))
        if not os.path.isdir(root):
            out.append({"id": p.get("id"), "error": "transcript root not found: " + root})
            continue
        result = analyse(root, window_hours, args.lookback_days,
                         p.get("model_exclude_globs", []), ceiling, reserve)
        result["id"] = p.get("id")
        result["provider_kind"] = p.get("source", "transcripts")
        out.append(result)

    C.emit({"kind": "quota", "at": C.iso(), "providers": out})


if __name__ == "__main__":
    main()