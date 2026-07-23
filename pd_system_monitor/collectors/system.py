from __future__ import annotations

import os
import time
from typing import Any

import psutil


def _disk_for_root() -> dict[str, Any]:
    usage = psutil.disk_usage("/")
    return {
        "mountpoint": "/",
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent": usage.percent,
    }


def _cpu_temperature_c() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    if not temps:
        return None
    for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        readings = temps.get(key)
        if not readings:
            continue
        package = next(
            (r for r in readings if r.label and "package" in r.label.lower()),
            readings[0],
        )
        if package.current is not None:
            return float(package.current)
    return None


def _memory_pressure() -> dict[str, float] | None:
    """Linux PSI memory-stall averages from ``/proc/pressure/memory``.

    Returns keys like ``some_avg10`` / ``full_avg60`` (percent of the trailing
    window during which, respectively, *some* / *all* runnable tasks were
    stalled waiting on memory). Returns ``None`` when PSI is unavailable
    (``CONFIG_PSI`` disabled or a pre-4.20 kernel) so callers can fall back.
    """
    try:
        with open("/proc/pressure/memory", "r") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    out: dict[str, float] = {}
    for line in lines:
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        kind = parts[0]
        for tok in parts[1:]:
            key, _, val = tok.partition("=")
            if key.startswith("avg"):
                try:
                    out[f"{kind}_{key}"] = float(val)
                except ValueError:
                    pass
    return out or None


def _swap_io_counters() -> dict[str, int] | None:
    """Cumulative pages swapped in/out since boot, from ``/proc/vmstat``.

    These are the raw counters behind ``vmstat``'s ``si``/``so`` columns; the
    caller diffs successive samples to derive a swap-I/O *rate* (the real
    thrashing signal, as opposed to static swap occupancy).
    """
    try:
        with open("/proc/vmstat", "r") as fh:
            data = fh.read()
    except OSError:
        return None
    counters: dict[str, int] = {}
    for line in data.splitlines():
        if line.startswith(("pswpin ", "pswpout ")):
            key, _, val = line.partition(" ")
            try:
                counters[key] = int(val)
            except ValueError:
                pass
    if "pswpin" not in counters or "pswpout" not in counters:
        return None
    return {"in_pages": counters["pswpin"], "out_pages": counters["pswpout"]}


def _top_processes_by_rss(limit: int = 5) -> list[dict[str, Any]]:
    procs: list[tuple[int, int, str]] = []
    for p in psutil.process_iter(attrs=("pid", "name", "memory_info")):
        try:
            info = p.info
            mi = info["memory_info"]
            if mi is None:
                continue
            procs.append((mi.rss, info["pid"], info["name"] or "?"))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(reverse=True)
    return [
        {"pid": pid, "name": name, "rss_bytes": rss}
        for (rss, pid, name) in procs[:limit]
    ]


def collect(*, want_top_processes: bool) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    return {
        "ram": {
            "total_bytes": vm.total,
            "used_bytes": vm.used,
            "available_bytes": vm.available,
            "percent": vm.percent,
        },
        "swap": {
            "total_bytes": sm.total,
            "used_bytes": sm.used,
            "percent": sm.percent,
        },
        "mem_pressure": _memory_pressure(),
        "swap_io": _swap_io_counters(),
        "disk": _disk_for_root(),
        "load_avg": {"1m": load1, "5m": load5, "15m": load15},
        "cpu_temp_c": _cpu_temperature_c(),
        "top_processes": _top_processes_by_rss() if want_top_processes else [],
        "collected_at_monotonic": time.monotonic(),
    }
