from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


class SystemdUserCollector:
    """Health of systemd --user services that the user installed (plus optional extras).

    Watchset is built once at startup from ~/.config/systemd/user/*.service file
    stems, unioned with config-provided extra_units. Session plumbing (dbus,
    pulseaudio, etc.) lives under /usr/lib/systemd/user and is therefore excluded.
    """

    def __init__(self, extra_units: list[str] | None = None):
        self._extras = list(extra_units or [])
        self._watchset: set[str] = set()
        self._systemctl: str | None = shutil.which("systemctl")
        self._prev_active: dict[str, str] = {}

    def startup(self) -> None:
        watchset: set[str] = set()
        if USER_UNIT_DIR.is_dir():
            for p in USER_UNIT_DIR.glob("*.service"):
                # Skip masked units (symlink to /dev/null). Masking is a
                # deliberate "never run this" — systemctl won't list it, so
                # tracking it would always report it as missing/not-loaded.
                if p.is_symlink() and p.readlink() == Path("/dev/null"):
                    continue
                watchset.add(p.name)
        for u in self._extras:
            watchset.add(u if u.endswith(".service") else f"{u}.service")
        self._watchset = watchset
        logger.info("systemd-user watchset: %s", sorted(watchset))

    def collect(self) -> dict[str, Any]:
        if not self._systemctl:
            return {
                "available": False,
                "units": [],
                "newly_failed": [],
                "summary": {"total": 0, "active": 0, "failed": 0, "other": 0},
            }
        if not self._watchset:
            return {
                "available": True,
                "units": [],
                "newly_failed": [],
                "summary": {"total": 0, "active": 0, "failed": 0, "other": 0},
            }

        try:
            proc = subprocess.run(
                [self._systemctl, "--user", "list-units", "--type=service",
                 "--all", "--no-legend", "--plain"],
                capture_output=True, text=True, timeout=3.0, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("systemctl --user call failed: %s", e)
            return {
                "available": False,
                "units": [],
                "newly_failed": [],
                "summary": {"total": 0, "active": 0, "failed": 0, "other": 0},
            }

        observed: dict[str, dict[str, Any]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            unit, load, active, sub, *rest = parts
            description = rest[0] if rest else ""
            if unit not in self._watchset:
                continue
            observed[unit] = {
                "unit": unit,
                "load": load,
                "active": active,
                "sub": sub,
                "description": description,
            }

        units: list[dict[str, Any]] = []
        for unit_name in sorted(self._watchset):
            if unit_name in observed:
                units.append(observed[unit_name])
            else:
                # Watched but not in list-units output (unloaded / never started)
                units.append({
                    "unit": unit_name,
                    "load": "not-found",
                    "active": "inactive",
                    "sub": "dead",
                    "description": "(not loaded)",
                })

        newly_failed: list[str] = []
        for u in units:
            prev = self._prev_active.get(u["unit"])
            if u["active"] == "failed" and prev != "failed":
                newly_failed.append(u["unit"])
            self._prev_active[u["unit"]] = u["active"]

        active_count = sum(1 for u in units if u["active"] == "active")
        failed_count = sum(1 for u in units if u["active"] == "failed")
        other_count = len(units) - active_count - failed_count

        return {
            "available": True,
            "units": units,
            "newly_failed": newly_failed,
            "summary": {
                "total": len(units),
                "active": active_count,
                "failed": failed_count,
                "other": other_count,
            },
        }
