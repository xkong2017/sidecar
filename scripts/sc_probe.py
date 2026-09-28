#!/usr/bin/env python3
"""sc_probe - is this machine (and any remote inference endpoint) free right now?

Reads only. No sudo required for anything it does.

Exit codes:  0 = green (go)   3 = yellow (small jobs only)   4 = red (stop)
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_common as C  # noqa: E402


# --------------------------------------------------------------------------
# macOS signal readers
# --------------------------------------------------------------------------

def read_cpu(machine_cfg):
    """Load average normalised by core count, plus instantaneous CPU busy %.

    Portable across macOS and Linux: os.getloadavg works on both, and the core
    count comes from os.cpu_count with the platform-specific tool only as a
    refinement. Returns load1_available=False when no reading was possible, so
    the caller can tell "idle" apart from "could not measure".
    """
    cores = machine_cfg.get("cpu_cores") or 0
    if not cores:
        cores = os.cpu_count() or 0
    if not cores:
        r = C.run_cmd(["/usr/sbin/sysctl", "-n", "hw.ncpu"])
        try:
            cores = int(r["out"])
        except ValueError:
            cores = 0
    if not cores:
        cores = 1

    load1 = load5 = load15 = 0.0
    load_available = False
    try:
        load1, load5, load15 = os.getloadavg()
        load_available = True
    except (OSError, AttributeError):
        r = C.run_cmd(["/usr/sbin/sysctl", "-n", "vm.loadavg"])
        if r["ok"]:
            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", r["out"])
            if len(nums) >= 3:
                load1, load5, load15 = (float(x) for x in nums[:3])
                load_available = True

    # pressure = load per core; 1.0 means every core has exactly one runnable thread
    pressure = load1 / float(cores)

    # instantaneous busy percentage from ps (cheap, no sudo)
    busy_pct = None
    r = C.run_cmd(["/bin/ps", "-A", "-o", "%cpu"], timeout=10)
    if r["ok"]:
        total = 0.0
        for line in r["out"].splitlines()[1:]:
            try:
                total += float(line.strip())
            except ValueError:
                pass
        busy_pct = round(total / float(cores), 1)

    return {
        "cores": cores,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "load_available": load_available,
        "load_per_core": round(pressure, 3),
        "cpu_busy_pct_of_cores": busy_pct,
    }


def read_user_activity():
    """How long since the human last touched keyboard/mouse, and is the screen locked."""
    idle_s = None
    r = C.run_cmd(["/usr/sbin/ioreg", "-c", "IOHIDSystem"])
    if r["ok"]:
        m = re.search(r'"HIDIdleTime"\s*=\s*([0-9]+)', r["out"])
        if m:
            idle_s = round(int(m.group(1)) / 1e9, 1)

    locked = None
    r = C.run_cmd(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-k", "IOConsoleLocked"])
    if r["ok"]:
        m = re.search(r'"IOConsoleLocked"\s*=\s*(Yes|No)', r["out"])
        if m:
            locked = (m.group(1) == "Yes")

    # None means the platform exposes no HID idle counter (e.g. Linux). That is
    # "not measured", which the gate treats differently from "user is active".
    return {"user_idle_s": idle_s, "console_locked": locked,
            "user_activity_measured": idle_s is not None}


def read_memory(cfg):
    """Memory headroom, as the pessimistic of two independent estimators.

    macOS exposes this through vm_stat and memory_pressure; Linux through
    /proc/meminfo. When neither yields a number the result is reported as
    unknown rather than 0%, because 0% would fail the gate forever on a machine
    that simply keeps its metrics somewhere else.
    """
    page_size = 16384
    r = C.run_cmd(["/usr/sbin/sysctl", "-n", "hw.pagesize"])
    if r["ok"]:
        try:
            page_size = int(r["out"])
        except ValueError:
            pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE") or page_size
    except (ValueError, OSError, AttributeError):
        pass

    total_pages = 0
    r = C.run_cmd(["/usr/sbin/sysctl", "-n", "hw.memsize"])
    if r["ok"]:
        try:
            total_pages = int(r["out"]) // page_size
        except ValueError:
            pass

    free_pct = None
    mp_pct = None
    stats = {}

    vm = C.run_cmd(["/usr/bin/vm_stat"])
    if vm["ok"]:
        for line in vm["out"].splitlines():
            m = re.match(r"([A-Za-z][^:]*):\s+([0-9]+)\.", line)
            if m:
                stats[m.group(1).strip()] = int(m.group(2))
        if not total_pages:
            total_pages = sum(stats.values()) or 1
        available = (stats.get("Pages free", 0)
                     + stats.get("Pages inactive", 0)
                     + stats.get("Pages speculative", 0)
                     + stats.get("Pages purgeable", 0))
        free_pct = round(100.0 * available / total_pages, 1)

        mp = C.run_cmd(["/usr/bin/memory_pressure"], timeout=15)
        if mp["ok"]:
            m = re.search(r"System-wide memory free percentage:\s*([0-9]+)%", mp["out"])
            if m:
                mp_pct = int(m.group(1))
    else:
        # Linux and anything else with /proc
        info = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    parts = line.split(":")
                    if len(parts) == 2:
                        info[parts[0].strip()] = parts[1].strip()
        except OSError:
            info = {}
        if info:
            def kb(key):
                try:
                    return float(info.get(key, "0").split()[0])
                except (ValueError, IndexError):
                    return 0.0
            total_kb = kb("MemTotal")
            avail_kb = kb("MemAvailable")
            if not total_kb:
                avail_kb = kb("MemFree") + kb("Buffers") + kb("Cached")
                total_kb = avail_kb + kb("MemFree")
            if total_kb:
                free_pct = round(100.0 * avail_kb / total_kb, 1)

    # Take the pessimistic estimator: undershooting costs a skipped job,
    # overshooting costs the user a swapping machine.
    candidates = [x for x in (free_pct, mp_pct) if x is not None]
    honest = min(candidates) if candidates else None

    return {
        "mem_free_pct": honest,
        "mem_free_pct_vmstat": free_pct,
        "mem_free_pct_pressure": mp_pct,
        "mem_measured": honest is not None,
        "compressed_pages": stats.get("Pages occupied by compressor", 0),
        "swapouts": stats.get("Swapouts", 0),
    }


def read_gpu():
    """Apple Silicon GPU utilisation percentage, no sudo needed."""
    gpu = {"util_pct": None, "tiler_pct": None, "renderer_pct": None}
    r = C.run_cmd(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
    if not r["ok"]:
        return gpu
    m = re.search(r'"Device Utilization %"\s*=\s*([0-9]+)', r["out"])
    if m:
        gpu["util_pct"] = int(m.group(1))
    m = re.search(r'"Tiler Utilization %"\s*=\s*([0-9]+)', r["out"])
    if m:
        gpu["tiler_pct"] = int(m.group(1))
    m = re.search(r'"Renderer Utilization %"\s*=\s*([0-9]+)', r["out"])
    if m:
        gpu["renderer_pct"] = int(m.group(1))
    return gpu


def read_thermal_and_power(machine_cfg):
    out = {"thermal_limited": None, "cpu_speed_limit": None, "on_ac_power": None,
           "battery_pct": None, "sleep_prevented_by": [],
           "thermal_measured": False, "power_measured": False}

    r = C.run_cmd(["/usr/bin/pmset", "-g", "therm"])
    if r["ok"]:
        out["thermal_measured"] = True
        m = re.search(r"CPU_Speed_Limit\s*=\s*([0-9]+)", r["out"])
        if m:
            out["cpu_speed_limit"] = int(m.group(1))
            out["thermal_limited"] = int(m.group(1)) < 100
        else:
            out["thermal_limited"] = False

    r = C.run_cmd(["/usr/bin/pmset", "-g", "batt"])
    if r["ok"]:
        out["power_measured"] = True
        out["on_ac_power"] = "AC Power" in r["out"]
        m = re.search(r"([0-9]+)%", r["out"])
        if m:
            out["battery_pct"] = int(m.group(1))
    else:
        # Linux: a desktop usually has no battery at all, which is wall power.
        try:
            with open("/sys/class/power_supply/AC0/online", "r") as fh:
                out["on_ac_power"] = fh.read().strip() == "1"
                out["power_measured"] = True
        except OSError:
            if os.path.isdir("/sys/class/power_supply"):
                names = os.listdir("/sys/class/power_supply")
                out["on_ac_power"] = not any(n.startswith("BAT") for n in names)
                out["power_measured"] = True

    r = C.run_cmd(["/usr/bin/pmset", "-g"])
    if r["ok"]:
        m = re.search(r"sleep\s+\d+\s+\(sleep prevented by ([^)]+)\)", r["out"])
        if m:
            out["sleep_prevented_by"] = [s.strip() for s in m.group(1).split(",")]

    return out


# --------------------------------------------------------------------------
# remote inference endpoints
# --------------------------------------------------------------------------

def probe_endpoint(ep):
    """Probe one inference endpoint: TCP reachability + vLLM Prometheus metrics."""
    name = ep.get("name") or ep.get("base_url") or "endpoint"
    base = (ep.get("base_url") or "").rstrip("/")
    result = {"name": name, "base_url": base, "reachable": False, "busy": None,
              "running": None, "waiting": None, "kv_cache_pct": None,
              "model": ep.get("model"), "gate": "unknown"}

    if not base:
        result["error"] = "no base_url"
        return result

    # vLLM serves Prometheus metrics at the server root, not under the OpenAI
    # compatibility prefix, so drop a trailing /v1 before asking for them.
    server_root = base[:-3] if base.endswith("/v1") else base
    r = C.run_cmd(["/usr/bin/curl", "-s", "-m", "5", server_root + "/metrics"], timeout=12)
    if not r["ok"] or not r["out"]:
        result["error"] = "no metrics endpoint"
        return result

    result["reachable"] = True
    text = r["out"]

    def first(pattern):
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    result["running"] = first(r"^vllm:num_requests_running\{[^}]*\}\s+([0-9.]+)")
    result["waiting"] = first(r"^vllm:num_requests_waiting\{[^}]*\}\s+([0-9.]+)")
    kv = first(r"^vllm:kv_cache_usage_perc\{[^}]*\}\s+([0-9.]+)")
    if kv is None:
        kv = first(r"^vllm:gpu_cache_usage_perc\{[^}]*\}\s+([0-9.]+)")
    result["kv_cache_pct"] = round(kv * 100, 1) if kv is not None else None

    max_waiting = float(ep.get("max_waiting", 0))
    max_running = float(ep.get("max_running", 4))
    running = result["running"] or 0.0
    waiting = result["waiting"] or 0.0

    if waiting > max_waiting:
        result["gate"] = "busy"
        result["busy"] = True
    elif running < max_running:
        result["gate"] = "free"
        result["busy"] = False
    else:
        result["gate"] = "saturated"
        result["busy"] = True

    return result


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

def decide(machine_cfg, cpu, user, mem, gpu, power, endpoints):
    """Collapse raw signals into green / yellow / red with an explicit reason list.

    Two principles, both learned from getting them wrong:

      Unmeasurable is not the same as idle. A signal the platform cannot provide
      must not silently pass the gate; it becomes a warning so the operator can
      see the decision rests on fewer signals than they think.

      Unmeasurable is not the same as red either. Reporting 0% memory because
      vm_stat is absent would lock the gate shut forever on a Linux box that is
      actually fine.
    """
    blocking = []
    warnings = []

    # --- CPU
    if not cpu.get("load_available", True):
        warnings.append("cpu load unavailable on this platform; gating on other signals")
    else:
        lp = cpu["load_per_core"]
        if lp >= machine_cfg["load_ceiling_yellow"]:
            blocking.append("cpu load %.2f/core >= %.2f" % (lp, machine_cfg["load_ceiling_yellow"]))
        elif lp >= machine_cfg["load_ceiling_green"]:
            warnings.append("cpu load %.2f/core above green %.2f" % (lp, machine_cfg["load_ceiling_green"]))

    # --- human presence
    idle = user.get("user_idle_s")
    if idle is None:
        warnings.append("user idle time not measurable on this platform; "
                        "cannot confirm the human is away")
    elif idle < machine_cfg["user_idle_yellow_s"]:
        blocking.append("user active %ss ago" % idle)
    elif idle < machine_cfg["user_idle_green_s"]:
        warnings.append("user recently active (%ss idle)" % idle)

    if user.get("console_locked") and machine_cfg["block_when_console_locked"]:
        blocking.append("console locked")

    # --- memory
    if not mem.get("mem_measured"):
        warnings.append("memory headroom not measurable on this platform")
    else:
        free = mem["mem_free_pct"]
        if free < machine_cfg["min_free_mem_pct"]:
            blocking.append("memory headroom %.1f%% < %s%%"
                            % (free, machine_cfg["min_free_mem_pct"]))
        elif free < machine_cfg["min_free_mem_pct"] + 10:
            warnings.append("memory headroom low (%.1f%%)" % free)

    # --- thermal / power
    if power.get("thermal_measured") is not False and power.get("thermal_limited"):
        warnings.append("thermal throttling active (speed limit %s%%)" % power["cpu_speed_limit"])
    if machine_cfg["require_ac_power"]:
        if power.get("on_ac_power") is False:
            blocking.append("on battery")
        elif power.get("on_ac_power") is None:
            warnings.append("power source unknown; cannot confirm wall power")

    # --- remote endpoints
    for ep in endpoints:
        if ep["gate"] == "busy":
            blocking.append("endpoint %s busy (waiting=%s)" % (ep["name"], ep["waiting"]))
        elif ep["gate"] == "saturated":
            warnings.append("endpoint %s saturated (running=%s)" % (ep["name"], ep["running"]))
        elif ep["gate"] == "unknown":
            warnings.append("endpoint %s unreachable, skipping" % ep["name"])

    if blocking:
        verdict = "red"
        reasons = blocking + warnings
    elif warnings:
        verdict = "yellow"
        reasons = warnings
    else:
        verdict = "green"
        reasons = ["all signals within green thresholds"]

    degraded = [w for w in warnings if "not measurable" in w or "unavailable" in w
                or "unknown" in w]

    return {
        "verdict": verdict,
        "reasons": reasons,
        "blocking": blocking,
        "warnings": warnings,
        "degraded": degraded,
        "signals_measured": {
            "cpu_load": bool(cpu.get("load_available", True)),
            "user_activity": bool(user.get("user_activity_measured")),
            "memory": bool(mem.get("mem_measured")),
            "power": power.get("on_ac_power") is not None,
        },
        "allowed_work": {
            "green": ["heavy: builds, test suites, multi-agent fan-out, local GPU jobs"],
            "yellow": ["light: read-only research, planning, single-file edits, note-taking"],
            "red": ["none: record intent only"],
        }[verdict],
    }


def main():
    ap = argparse.ArgumentParser(description="Probe machine and endpoint availability.")
    ap.add_argument("--json", action="store_true", help="(default) emit JSON")
    ap.add_argument("--quiet", action="store_true", help="exit code only, no JSON")
    ap.add_argument("--no-endpoints", action="store_true")
    args = ap.parse_args()

    cfg = C.load_config()
    machine_cfg = cfg["machine"]

    cpu = read_cpu(machine_cfg)
    user = read_user_activity()
    mem = read_memory(machine_cfg)
    gpu = read_gpu()
    power = read_thermal_and_power(machine_cfg)

    endpoints = []
    if not args.no_endpoints:
        for ep in cfg.get("endpoints", []):
            endpoints.append(probe_endpoint(ep))

    verdict = decide(machine_cfg, cpu, user, mem, gpu, power, endpoints)

    snapshot = {
        "kind": "probe",
        "at": C.iso(),
        "host": os.uname().nodename,
        "cpu": cpu,
        "user": user,
        "memory": mem,
        "gpu": gpu,
        "power": power,
        "endpoints": endpoints,
        "verdict": verdict["verdict"],
        "reasons": verdict["reasons"],
        "degraded": verdict["degraded"],
        "signals_measured": verdict["signals_measured"],
        "allowed_work": verdict["allowed_work"],
        "platform": sys.platform,
    }

    if args.quiet:
        sys.exit({"green": 0, "yellow": 3, "red": 4}[verdict["verdict"]])

    C.emit(snapshot)
    sys.exit({"green": 0, "yellow": 3, "red": 4}[verdict["verdict"]])


if __name__ == "__main__":
    main()