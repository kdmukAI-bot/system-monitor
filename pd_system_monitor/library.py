from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from personal_dashboard.core.result import ModuleResult, Status

from .collectors import gpu, system
from .collectors.systemd_user import SystemdUserCollector
from .collectors.ups import UpsCollector

logger = logging.getLogger(__name__)

_DATA_DIR = (
    Path.home() / ".local" / "share" / "personal-dashboard" / "modules" / "system-monitor"
)

_SEVERITY_RANK = {
    "ok": 0,
    "info": 1,
    "warning": 2,
    "critical": 3,
}
_RANK_TO_STATUS = {
    0: Status.OK,
    1: Status.INFO,
    2: Status.WARNING,
    3: Status.CRITICAL,
}


def _max_sev(*sevs: str) -> str:
    best = "ok"
    for s in sevs:
        if _SEVERITY_RANK.get(s, 0) > _SEVERITY_RANK.get(best, 0):
            best = s
    return best


def _bytes_to_gib(n: int) -> float:
    return n / (1024 ** 3)


def _format_runtime(secs: float | None) -> str:
    if secs is None:
        return "?"
    s = int(secs)
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


class _MetricHysteresis:
    """Tracks consecutive observations for a single metric.

    Reports its WARN/CRIT *only* once the metric has been continuously bad
    long enough to satisfy `sustained_seconds`. Counted in scheduler ticks; the
    plugin's tick interval (5 s) is passed in at construction.
    """

    def __init__(self, sustained_seconds: float, tick_seconds: float):
        ticks = max(1, int(round(sustained_seconds / max(tick_seconds, 0.001))))
        self._needed_ticks = ticks
        self._consecutive_warn = 0
        self._consecutive_crit = 0

    def observe(self, sev: str) -> str:
        """Return the *reported* severity after applying hysteresis."""
        if sev == "critical":
            self._consecutive_crit += 1
            self._consecutive_warn += 1
        elif sev == "warning":
            self._consecutive_warn += 1
            self._consecutive_crit = 0
        else:
            self._consecutive_warn = 0
            self._consecutive_crit = 0
            return sev

        if self._consecutive_crit >= self._needed_ticks:
            return "critical"
        if self._consecutive_warn >= self._needed_ticks:
            return "warning"
        return "ok"


class Monitor:
    display_name = "System Monitor"

    def __init__(self, config: dict) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._latest: ModuleResult | None = None

        thresholds = config.get("thresholds") or {}
        self._ram_warn = float(thresholds.get("ram_warn_pct", 90))
        self._ram_crit = float(thresholds.get("ram_crit_pct", 95))
        self._swap_warn = float(thresholds.get("swap_warn_pct", 50))
        self._disk_warn = float(thresholds.get("disk_warn_pct", 90))
        self._disk_crit = float(thresholds.get("disk_crit_pct", 95))
        self._vram_warn = float(thresholds.get("vram_warn_pct", 95))
        sustained_seconds = float(thresholds.get("sustained_seconds", 120))

        schedule = config.get("schedule") or {}
        tick_seconds = float(schedule.get("seconds", 5))

        self._hyst_ram = _MetricHysteresis(sustained_seconds, tick_seconds)
        self._hyst_swap = _MetricHysteresis(sustained_seconds, tick_seconds)
        self._hyst_disk = _MetricHysteresis(sustained_seconds, tick_seconds)
        self._hyst_vram = _MetricHysteresis(sustained_seconds, tick_seconds)

        ups_cfg = config.get("ups") or {}
        self._ups = UpsCollector(
            device=ups_cfg.get("device", "cyberpower@localhost"),
            event_log_path=ups_cfg.get("event_log", "/var/log/ups-events.log"),
            state_path=_DATA_DIR / "ups_log_state.json",
        )

        systemd_cfg = config.get("systemd") or {}
        self._systemd = SystemdUserCollector(
            extra_units=list(systemd_cfg.get("extra_units") or [])
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        # Serialize against update(): start_scheduler() launches the tick task
        # *before* awaiting library.startup(), so without the lock the first
        # tick could observe partially-initialized collector state.
        async with self._lock:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._ups.startup)
            await asyncio.to_thread(self._systemd.startup)

    async def shutdown(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._ups.shutdown)

    # ------------------------------------------------------------------
    # Scheduler entry point
    # ------------------------------------------------------------------

    async def update(self) -> ModuleResult:
        async with self._lock:
            ram_warn_pct = self._ram_warn

            sys_metrics_first, gpu_metrics, ups_metrics, systemd_metrics = (
                await asyncio.gather(
                    asyncio.to_thread(system.collect, want_top_processes=False),
                    asyncio.to_thread(gpu.collect),
                    asyncio.to_thread(self._ups.collect),
                    asyncio.to_thread(self._systemd.collect),
                )
            )

            # Re-collect system metrics with top_processes if RAM is hot.
            if sys_metrics_first["ram"]["percent"] >= ram_warn_pct:
                sys_metrics = await asyncio.to_thread(
                    system.collect, want_top_processes=True
                )
            else:
                sys_metrics = sys_metrics_first

            ram_pct = sys_metrics["ram"]["percent"]
            swap_pct = sys_metrics["swap"]["percent"]
            disk_pct = sys_metrics["disk"]["percent"]

            ram_raw_sev = "ok"
            if ram_pct >= self._ram_crit:
                ram_raw_sev = "critical"
            elif ram_pct >= self._ram_warn:
                ram_raw_sev = "warning"
            ram_sev = self._hyst_ram.observe(ram_raw_sev)

            swap_raw_sev = "warning" if swap_pct >= self._swap_warn else "ok"
            swap_sev = self._hyst_swap.observe(swap_raw_sev)

            disk_raw_sev = "ok"
            if disk_pct >= self._disk_crit:
                disk_raw_sev = "critical"
            elif disk_pct >= self._disk_warn:
                disk_raw_sev = "warning"
            disk_sev = self._hyst_disk.observe(disk_raw_sev)

            vram_pct = 0.0
            vram_raw_sev = "ok"
            if gpu_metrics.get("gpus"):
                vram_pct = max(g.get("vram_percent", 0.0) for g in gpu_metrics["gpus"])
                if vram_pct >= self._vram_warn:
                    vram_raw_sev = "warning"
            vram_sev = self._hyst_vram.observe(vram_raw_sev)

            ups_status_sev = ups_metrics["live"].get("status_severity", "ok") \
                if ups_metrics["live"].get("available") else "unknown"
            ups_event_sev = ups_metrics.get("new_event_severity", "ok")
            ups_sev = _max_sev(ups_status_sev if ups_status_sev != "unknown" else "ok",
                               ups_event_sev)

            systemd_sev = "warning" if systemd_metrics["summary"]["failed"] else "ok"

            overall_sev = _max_sev(
                ram_sev, swap_sev, disk_sev, vram_sev, ups_sev, systemd_sev
            )
            overall_status = _RANK_TO_STATUS[_SEVERITY_RANK[overall_sev]]

            now = datetime.now()
            summary = self._build_summary(
                sys_metrics, gpu_metrics, ups_metrics, systemd_metrics
            )
            detail = self._build_detail_text(
                ram_sev, swap_sev, disk_sev, vram_sev,
                ups_status_sev, ups_event_sev,
                ups_metrics, systemd_metrics,
                ram_pct, swap_pct, disk_pct, vram_pct,
            )

            data = {
                "ram": sys_metrics["ram"],
                "swap": sys_metrics["swap"],
                "disk": sys_metrics["disk"],
                "load_avg": sys_metrics["load_avg"],
                "cpu_temp_c": sys_metrics["cpu_temp_c"],
                "top_processes": sys_metrics["top_processes"],
                "gpu": gpu_metrics,
                "ups": {
                    "live": ups_metrics["live"],
                    "recent_events": ups_metrics["recent_events"],
                },
                "systemd": systemd_metrics,
                "severities": {
                    "ram": ram_sev, "swap": swap_sev, "disk": disk_sev,
                    "vram": vram_sev, "ups": ups_sev, "systemd": systemd_sev,
                    "overall": overall_sev,
                },
                "thresholds": {
                    "ram_warn_pct": self._ram_warn,
                    "ram_crit_pct": self._ram_crit,
                    "swap_warn_pct": self._swap_warn,
                    "disk_warn_pct": self._disk_warn,
                    "disk_crit_pct": self._disk_crit,
                    "vram_warn_pct": self._vram_warn,
                },
                "polled_at": now.isoformat(),
            }

            result = ModuleResult(
                status=overall_status,
                summary_text=summary,
                detail_text=detail,
                click_url="/modules/system-monitor/",
                data=data,
                occurred_at=now,
            )
            self._latest = result
            return result

    async def get_data(self) -> dict:
        if self._latest is None:
            await self.update()
        assert self._latest is not None
        return {
            "status": self._latest.status.value,
            "summary_text": self._latest.summary_text,
            "detail_text": self._latest.detail_text,
            "click_url": self._latest.click_url,
            "occurred_at": self._latest.occurred_at.isoformat()
            if self._latest.occurred_at
            else None,
            **self._latest.data,
        }

    @property
    def routes(self) -> list[Any]:
        return []

    # ------------------------------------------------------------------
    # Text builders
    # ------------------------------------------------------------------

    def _build_summary(self, sys_m, gpu_m, ups_m, systemd_m) -> str:
        parts: list[str] = []
        ram = sys_m["ram"]
        parts.append(
            f"RAM {ram['percent']:.0f}% ({_bytes_to_gib(ram['used_bytes']):.1f}/"
            f"{_bytes_to_gib(ram['total_bytes']):.1f} GiB)"
        )
        if gpu_m.get("gpus"):
            g = gpu_m["gpus"][0]
            parts.append(
                f"VRAM {g['vram_percent']:.0f}% "
                f"({g['vram_used_bytes'] // (1024*1024)}/"
                f"{g['vram_total_bytes'] // (1024*1024)} MiB)"
            )
        disk = sys_m["disk"]
        parts.append(
            f"disk free {_bytes_to_gib(disk['free_bytes']):.0f} GiB"
        )
        live = ups_m["live"]
        if live.get("available"):
            parts.append(f"UPS {live.get('status_label', 'unknown')}")
        sd = systemd_m["summary"]
        if sd["failed"]:
            parts.append(f"{sd['failed']} svc FAILED")
        else:
            parts.append(f"{sd['active']}/{sd['total']} svcs OK")
        return " · ".join(parts)

    def _build_detail_text(
        self, ram_sev, swap_sev, disk_sev, vram_sev,
        ups_status_sev, ups_event_sev,
        ups_m, systemd_m,
        ram_pct, swap_pct, disk_pct, vram_pct,
    ) -> str | None:
        problems: list[str] = []
        if ram_sev != "ok":
            problems.append(f"RAM {ram_pct:.0f}% (sustained)")
        if swap_sev != "ok":
            problems.append(f"swap {swap_pct:.0f}%")
        if disk_sev != "ok":
            problems.append(f"disk {disk_pct:.0f}% used")
        if vram_sev != "ok":
            problems.append(f"VRAM {vram_pct:.0f}%")
        if ups_status_sev not in ("ok", "unknown"):
            label = ups_m["live"].get("status_label", ups_m["live"].get("status_raw"))
            problems.append(f"UPS: {label}")
        if ups_event_sev != "ok":
            for ev in ups_m.get("new_events", []):
                problems.append(f"UPS event: {ev.get('event')}")
        failed_units = [
            u["unit"] for u in systemd_m["units"] if u["active"] == "failed"
        ]
        if failed_units:
            problems.append("failed services: " + ", ".join(failed_units))
        return "; ".join(problems) if problems else None
