"""Best-effort local system-resource snapshot (CPU load, memory,
temperature, disk) for the admin device fleet dashboard. Every field is
independently optional and simply absent when this platform/process can't
read it -- never a fabricated 0, consistent with the rest of the agent
(see docs/CODE_STANDARDS.md and README.md)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")


def _cpu_load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:  # not available on this platform (see os.getloadavg docs)
        return None


def _memory_stats(meminfo_path: str = "/proc/meminfo") -> dict:
    fields = {}
    try:
        with open(meminfo_path) as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    fields[key] = int(rest.strip().split()[0])  # kB
    except (OSError, ValueError):
        return {}
    total_kb = fields.get("MemTotal")
    if not total_kb:
        return {}
    result = {"memory_total_mb": round(total_kb / 1024, 1)}
    available_kb = fields.get("MemAvailable")
    if available_kb is not None:
        result["memory_used_percent"] = round((1 - available_kb / total_kb) * 100, 1)
    return result


def _temperature_c() -> float | None:
    try:
        millidegrees = int(THERMAL_ZONE.read_text().strip())
    except (OSError, ValueError):
        return None
    return round(millidegrees / 1000, 1)


def _disk_used_percent(path: str = "/") -> float | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if not usage.total:
        return None
    return round(usage.used / usage.total * 100, 1)


def collect() -> dict:
    """Never raises. Returns only the fields this platform/process could
    actually read -- an empty dict is a valid (if uninformative) result."""
    result = {}
    cpu = _cpu_load_1m()
    if cpu is not None:
        result["cpu_load_1m"] = cpu
    result.update(_memory_stats())
    temperature = _temperature_c()
    if temperature is not None:
        result["temperature_c"] = temperature
    disk = _disk_used_percent()
    if disk is not None:
        result["disk_used_percent"] = disk
    return result
