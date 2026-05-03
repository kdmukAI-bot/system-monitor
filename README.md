# pd-system-monitor

System-monitor plugin for [personal-dashboard](https://github.com/kdmukAI-bot/personal-dashboard).

Live (5 s cadence) telemetry: RAM, VRAM, free disk, UPS state, systemd `--user` service health. Plus load averages, swap, CPU/GPU temperature, and (when RAM is hot) the top processes by RSS.

## Install

```sh
# from inside the dashboard venv
pip install -e ~/dev/tools/personal-dashboard-modules/system-monitor
cp config.toml.example config.toml   # edit if defaults don't suit
systemctl --user restart personal-dashboard
```

## Data sources

- **System metrics** — `psutil` (RAM, swap, disk, load avg, CPU temp, top processes by RSS).
- **GPU** — `nvidia-smi --query-gpu=...` subprocess. NVIDIA-only; gracefully no-ops if not present.
- **UPS** — `upsc <device>` for live state + tail of `/var/log/ups-events.log` (and the rotated `.log.1`) for history and edge events.
- **systemd-user** — `systemctl --user list-units --type=service --all`, filtered to units whose unit-file lives in `~/.config/systemd/user/` plus any `[systemd] extra_units` from config.

## Config

See [`config.toml.example`](config.toml.example). Thresholds, polling cadence, UPS device name, and systemd extra-units are all configurable.

## Notifications

Web push fires on the OK→WARN/CRITICAL transition (per the dashboard's `publish_module_result` contract). Per-metric hysteresis (`sustained_seconds`) avoids notifying on momentary spikes. UPS power events (`ONBATT`, `LOWBATT`, `SELFTEST` non-passed, `REPLBATT`, `COMMBAD`) and any systemd-user service entering `failed` elevate plugin status immediately on the tick where they're detected — no hysteresis, since these events ARE the alert.
