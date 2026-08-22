from pathlib import Path

from pi_recorder.storage import StorageManager
from pi_recorder.system_metrics import (
    collect_metrics,
    read_cpu_temperature_celsius,
    read_memory,
    read_uptime_seconds,
)


MEMINFO_SAMPLE = """MemTotal:         433624 kB
MemFree:           81120 kB
MemAvailable:     294912 kB
Buffers:           12000 kB
SwapTotal:        102400 kB
SwapFree:         102400 kB
"""


def test_read_memory_parses_kibibytes(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO_SAMPLE, encoding="utf-8")

    memory = read_memory(meminfo)

    assert memory["memory_total_bytes"] == 433624 * 1024
    assert memory["memory_available_bytes"] == 294912 * 1024
    assert memory["swap_total_bytes"] == 102400 * 1024
    assert memory["swap_free_bytes"] == 102400 * 1024


def test_readers_return_none_without_proc(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    assert read_memory(missing) == {
        "memory_total_bytes": None,
        "memory_available_bytes": None,
        "swap_total_bytes": None,
        "swap_free_bytes": None,
    }
    assert read_uptime_seconds(missing) is None
    assert read_cpu_temperature_celsius(missing) is None


def test_readers_tolerate_unparsable_content(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage"
    garbage.write_text("not a number\n", encoding="utf-8")

    assert read_uptime_seconds(garbage) is None
    assert read_cpu_temperature_celsius(garbage) is None


def test_read_cpu_temperature_converts_millidegrees(tmp_path: Path) -> None:
    thermal = tmp_path / "temp"
    thermal.write_text("48312\n", encoding="utf-8")

    assert read_cpu_temperature_celsius(thermal) == 48.3


def test_collect_metrics_reports_recording_disk(tmp_path: Path) -> None:
    recording_dir = tmp_path / "recordings"
    recording_dir.mkdir()

    metrics = collect_metrics(StorageManager(recording_dir))

    assert metrics["recording_disk_total_bytes"] > 0
    assert metrics["recording_disk_free_bytes"] >= 0
    assert metrics["platform"]
    # Every reading is optional so a non-Linux host still produces a payload.
    for key in ("uptime_seconds", "cpu_temperature_celsius", "memory_total_bytes"):
        assert key in metrics
