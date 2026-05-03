from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Events that should elevate plugin status when newly observed during incremental tail.
NOTABLE_EVENTS = {
    "ONBATT": "warning",
    "LOWBATT": "critical",
    "FSD": "critical",
    "SHUTDOWN": "critical",
    "REPLBATT": "warning",
    "COMMBAD": "warning",
    "NOCOMM": "warning",
}

UPS_STATUS_LABELS = {
    "OL": "On line",
    "OB": "On battery",
    "LB": "Low battery",
    "HB": "High battery",
    "RB": "Replace battery",
    "CHRG": "Charging",
    "DISCHRG": "Discharging",
    "BYPASS": "Bypass",
    "CAL": "Calibrating",
    "OFF": "Off",
    "OVER": "Overloaded",
    "TRIM": "Trimming voltage",
    "BOOST": "Boosting voltage",
    "FSD": "Forced shutdown",
}


class UpsCollector:
    """Live state via `upsc` + tail of /var/log/ups-events.log (handling rotation)."""

    def __init__(
        self,
        device: str,
        event_log_path: str,
        state_path: Path,
        max_events: int = 50,
    ):
        self._device = device
        self._event_log_path = Path(event_log_path)
        self._state_path = state_path
        self._max_events = max_events
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._st_ino: int | None = None
        self._offset: int = 0
        self._missing_log_warned = False
        self._upsc: str | None = shutil.which("upsc")

    # ------------------------------------------------------------------
    # Lifecycle: backfill from rotated log on startup, persist offset on shutdown.
    # ------------------------------------------------------------------

    def startup(self) -> None:
        backfill: list[dict[str, Any]] = []
        rotated = self._event_log_path.with_suffix(self._event_log_path.suffix + ".1")
        if rotated.is_file():
            backfill.extend(_read_jsonl(rotated))
        if self._event_log_path.is_file():
            backfill.extend(_read_jsonl(self._event_log_path))

        # Dedupe and sort
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for ev in backfill:
            key = (ev.get("ts", ""), ev.get("event", ""))
            if key in seen:
                continue
            seen.add(key)
            unique.append(ev)
        unique.sort(key=lambda e: e.get("ts", ""))

        for ev in unique[-self._max_events :]:
            self._events.append(ev)

        # Initialize tail position to end-of-current-file so backfilled events
        # don't trigger notifications.
        if self._event_log_path.is_file():
            try:
                stat = self._event_log_path.stat()
                self._st_ino = stat.st_ino
                self._offset = stat.st_size
            except OSError:
                pass

    def shutdown(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"st_ino": self._st_ino, "offset": self._offset})
            )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Tick-time collection
    # ------------------------------------------------------------------

    def collect(self) -> dict[str, Any]:
        live = self._collect_live()
        new_events = self._tail_log()
        for ev in new_events:
            self._events.append(ev)

        # Determine the worst event severity among the *new* events.
        new_event_severity = "ok"
        for ev in new_events:
            sev = NOTABLE_EVENTS.get(ev.get("event", ""))
            if sev == "critical":
                new_event_severity = "critical"
                break
            if sev == "warning" and new_event_severity != "critical":
                new_event_severity = "warning"
            # SELFTEST with non-passed result is also notable
            if ev.get("event") == "SELFTEST":
                result = (ev.get("result") or "").lower()
                if "passed" not in result and result:
                    if new_event_severity == "ok":
                        new_event_severity = "warning"

        return {
            "live": live,
            "recent_events": list(self._events),
            "new_event_severity": new_event_severity,
            "new_events": new_events,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_live(self) -> dict[str, Any]:
        if not self._upsc:
            return {"available": False, "error": "upsc binary not found"}

        try:
            proc = subprocess.run(
                [self._upsc, self._device],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"available": False, "error": str(e)}

        if proc.returncode != 0:
            return {"available": False, "error": proc.stderr.strip()}

        kv: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                kv[k.strip()] = v.strip()

        status_raw = kv.get("ups.status", "")
        status_tokens = status_raw.split()
        status_label = " · ".join(
            UPS_STATUS_LABELS.get(t, t) for t in status_tokens
        ) or "unknown"

        primary = status_tokens[0] if status_tokens else ""
        status_severity = "ok"
        if primary == "OL":
            status_severity = "ok"
        elif primary == "OB":
            status_severity = "warning"
        elif primary in {"LB", "FSD", "SHUTDOWN"}:
            status_severity = "critical"
        elif primary in {"RB", "OVER", "BYPASS"}:
            status_severity = "warning"
        elif primary == "":
            status_severity = "unknown"

        return {
            "available": True,
            "device_model": kv.get("device.model") or kv.get("ups.model"),
            "device_mfr": kv.get("device.mfr") or kv.get("ups.mfr"),
            "status_raw": status_raw,
            "status_tokens": status_tokens,
            "status_label": status_label,
            "status_severity": status_severity,
            "charge_pct": _maybe_float(kv.get("battery.charge")),
            "charge_low_pct": _maybe_float(kv.get("battery.charge.low")),
            "charge_warning_pct": _maybe_float(kv.get("battery.charge.warning")),
            "runtime_seconds": _maybe_float(kv.get("battery.runtime")),
            "runtime_low_seconds": _maybe_float(kv.get("battery.runtime.low")),
            "load_pct": _maybe_float(kv.get("ups.load")),
            "input_volts": _maybe_float(kv.get("input.voltage")),
            "output_volts": _maybe_float(kv.get("output.voltage")),
            "realpower_watts": _maybe_float(kv.get("ups.realpower")),
            "realpower_nominal_watts": _maybe_float(kv.get("ups.realpower.nominal")),
            "test_result": kv.get("ups.test.result"),
            "battery_voltage": _maybe_float(kv.get("battery.voltage")),
            "battery_voltage_nominal": _maybe_float(kv.get("battery.voltage.nominal")),
            "battery_type": kv.get("battery.type"),
            "raw": kv,
        }

    def _tail_log(self) -> list[dict[str, Any]]:
        path = self._event_log_path
        if not path.is_file():
            if not self._missing_log_warned:
                logger.warning("UPS event log %s not found; skipping tail", path)
                self._missing_log_warned = True
            return []
        self._missing_log_warned = False

        try:
            stat = path.stat()
        except OSError as e:
            logger.warning("UPS event log stat failed: %s", e)
            return []

        if self._st_ino is None:
            # First read after construction (e.g. startup wasn't called yet)
            self._st_ino = stat.st_ino
            self._offset = stat.st_size
            return []

        if stat.st_ino != self._st_ino:
            # File rotated. Read the new file from the beginning; skip whatever
            # tail of the old file was not yet read (rare given size 1M + sparse events).
            logger.info("UPS event log rotated (inode changed); resuming from new file")
            self._st_ino = stat.st_ino
            self._offset = 0

        if stat.st_size < self._offset:
            # File was truncated unexpectedly; reset.
            logger.info("UPS event log shrank; resetting offset to 0")
            self._offset = 0

        if stat.st_size == self._offset:
            return []

        try:
            with path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError as e:
            logger.warning("UPS event log read failed: %s", e)
            return []

        events: list[dict[str, Any]] = []
        for raw in chunk.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("malformed UPS log line: %r", raw)
        return events


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
    return out


def _maybe_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None
