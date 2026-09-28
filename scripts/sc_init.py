#!/usr/bin/env python3
"""sc_init - write a starter config and probe what this machine can actually do.

Run this first. It inspects the machine, finds local inference endpoints, checks
which transcript roots exist, and writes ~/.sidecar/config.json with values that
match reality rather than defaults from someone else's setup.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402

# Ports where a local OpenAI-compatible server is commonly listening. Probing is
# cheap and non-destructive; a wrong guess costs one refused connection.
COMMON_PORTS = [8000, 8001, 8080, 8888, 8892, 8100, 11434, 1234, 5000, 30000]


def probe_local_endpoints(timeout=2):
    found = []
    for port in COMMON_PORTS:
        base = "http://127.0.0.1:%d" % port
        r = C.run_cmd(["/usr/bin/curl", "-s", "-m", str(timeout), base + "/v1/models"], timeout=timeout + 3)
        if not r["ok"] or not r["out"]:
            continue
        try:
            doc = json.loads(r["out"])
        except ValueError:
            continue
        models = [m.get("id") for m in doc.get("data", []) if isinstance(m, dict)]
        if models:
            found.append({"port": port, "base_url": base + "/v1", "models": models})
    return found


def main():
    ap = argparse.ArgumentParser(description="Initialise the sidecar config.")
    ap.add_argument("--force", action="store_true", help="overwrite an existing config")
    ap.add_argument("--no-probe", action="store_true", help="skip endpoint probing")
    args = ap.parse_args()

    C.ensure_dirs()

    if os.path.exists(C.CONFIG_PATH) and not args.force:
        C.emit({
            "kind": "init",
            "wrote": False,
            "config": C.CONFIG_PATH,
            "reason": "config already exists; pass --force to overwrite",
        })
        return

    cfg = json.loads(json.dumps(C.DEFAULT_CONFIG))

    # Real core count, so load thresholds are normalised correctly.
    r = C.run_cmd(["/usr/sbin/sysctl", "-n", "hw.ncpu"])
    try:
        cfg["machine"]["cpu_cores"] = int(r["out"])
    except ValueError:
        pass

    # Which transcript roots actually exist on this machine.
    roots = [p for p in cfg["mining"]["transcript_roots"] if os.path.isdir(os.path.expanduser(p))]
    if roots:
        cfg["mining"]["transcript_roots"] = roots

    # Local endpoints become gating targets: the sidecar will not pile work onto
    # a GPU that is already serving someone.
    endpoints = []
    if not args.no_probe:
        for e in probe_local_endpoints():
            endpoints.append({
                "name": "local-%d" % e["port"],
                "base_url": e["base_url"],
                "model": e["models"][0],
                "max_waiting": 0,
                "max_running": 4,
            })
    cfg["endpoints"] = endpoints

    C.write_json_atomic(C.CONFIG_PATH, cfg)

    C.emit({
        "kind": "init",
        "wrote": True,
        "config": C.CONFIG_PATH,
        "state_dir": C.STATE_DIR,
        "cpu_cores": cfg["machine"]["cpu_cores"],
        "transcript_roots": cfg["mining"]["transcript_roots"],
        "endpoints_found": endpoints,
        "next": [
            "Review %s - especially budget caps, allowed_roots and deny_globs." % C.CONFIG_PATH,
            "python3 scripts/sc_mine.py   # build the first backlog",
            "python3 scripts/sc_run.py plan   # see the current verdict",
            "python3 scripts/sc_render.py --open   # open the dashboard",
        ],
    })


if __name__ == "__main__":
    main()
