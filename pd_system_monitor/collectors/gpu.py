from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_FIELDS = ("index", "name", "memory.used", "memory.total", "temperature.gpu", "utilization.gpu")
_NVIDIA_SMI: str | None = None


def _resolve_nvidia_smi() -> str | None:
    global _NVIDIA_SMI
    if _NVIDIA_SMI is not None:
        return _NVIDIA_SMI or None
    found = shutil.which("nvidia-smi")
    _NVIDIA_SMI = found or ""
    return found


def collect() -> dict[str, Any]:
    nvidia_smi = _resolve_nvidia_smi()
    if not nvidia_smi:
        return {"available": False, "gpus": []}

    cmd = [
        nvidia_smi,
        f"--query-gpu={','.join(_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("nvidia-smi call failed: %s", e)
        return {"available": False, "gpus": []}

    if proc.returncode != 0:
        logger.warning("nvidia-smi exited %d: %s", proc.returncode, proc.stderr.strip())
        return {"available": False, "gpus": []}

    gpus: list[dict[str, Any]] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_FIELDS):
            continue
        try:
            mem_used_mib = int(parts[2])
            mem_total_mib = int(parts[3])
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "vram_used_bytes": mem_used_mib * 1024 * 1024,
                "vram_total_bytes": mem_total_mib * 1024 * 1024,
                "vram_percent": (mem_used_mib / mem_total_mib * 100.0) if mem_total_mib else 0.0,
                "temp_c": _maybe_int(parts[4]),
                "util_percent": _maybe_int(parts[5]),
            })
        except (ValueError, ZeroDivisionError):
            continue

    return {"available": bool(gpus), "gpus": gpus}


def _maybe_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None
