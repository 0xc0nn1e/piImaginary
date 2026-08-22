import logging
import os
import platform
import socket
from pathlib import Path
from typing import Any, Dict, Optional

from pi_recorder.storage import StorageManager


LOGGER = logging.getLogger(__name__)

PROC_MEMINFO = Path("/proc/meminfo")
PROC_UPTIME = Path("/proc/uptime")
THERMAL_ZONE_TEMPERATURE = Path("/sys/class/thermal/thermal_zone0/temp")

# /proc/meminfo labels mapped to the payload keys they populate.
_MEMINFO_FIELDS = {
    "MemTotal": "memory_total_bytes",
    "MemAvailable": "memory_available_bytes",
    "SwapTotal": "swap_total_bytes",
    "SwapFree": "swap_free_bytes",
}


def read_load_average() -> Dict[str, Optional[float]]:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):
        return {
            "load_average_1m": None,
            "load_average_5m": None,
            "load_average_15m": None,
        }
    return {
        "load_average_1m": round(one, 2),
        "load_average_5m": round(five, 2),
        "load_average_15m": round(fifteen, 2),
    }


def read_memory(meminfo_path: Path = PROC_MEMINFO) -> Dict[str, Optional[int]]:
    memory: Dict[str, Optional[int]] = {key: None for key in _MEMINFO_FIELDS.values()}
    try:
        content = meminfo_path.read_text(encoding="utf-8")
    except OSError:
        # macOS and other non-Linux systems have no /proc; report unknown values.
        return memory

    for line in content.splitlines():
        label, separator, remainder = line.partition(":")
        key = _MEMINFO_FIELDS.get(label.strip())
        if not separator or key is None:
            continue
        fields = remainder.split()
        if fields and fields[0].isdigit():
            # /proc/meminfo reports these entries in kibibytes.
            memory[key] = int(fields[0]) * 1024
    return memory


def read_uptime_seconds(uptime_path: Path = PROC_UPTIME) -> Optional[float]:
    try:
        return round(float(uptime_path.read_text(encoding="utf-8").split()[0]), 1)
    except (IndexError, OSError, ValueError):
        return None


def read_cpu_temperature_celsius(
    thermal_path: Path = THERMAL_ZONE_TEMPERATURE,
) -> Optional[float]:
    try:
        # The kernel exposes this reading in millidegrees Celsius.
        return round(int(thermal_path.read_text(encoding="utf-8").strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def read_hostname() -> Optional[str]:
    try:
        return socket.gethostname()
    except OSError:
        return None


def collect_metrics(storage: StorageManager) -> Dict[str, Any]:
    """Return host metrics for a heartbeat; unavailable readings become None."""
    metrics: Dict[str, Any] = {
        "hostname": read_hostname(),
        "platform": platform.system(),
        "uptime_seconds": read_uptime_seconds(),
        "cpu_count": os.cpu_count(),
        "cpu_temperature_celsius": read_cpu_temperature_celsius(),
        "recording_disk_total_bytes": None,
        "recording_disk_free_bytes": None,
    }
    metrics.update(read_load_average())
    metrics.update(read_memory())

    try:
        usage = storage.disk_usage()
    except OSError as exc:
        LOGGER.warning("Could not read recording disk usage: %s", exc)
    else:
        metrics["recording_disk_total_bytes"] = usage.total
        metrics["recording_disk_free_bytes"] = usage.free
    return metrics
