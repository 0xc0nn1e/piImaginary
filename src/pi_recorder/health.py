import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pi_recorder.audio import resolve_audio_backend
from pi_recorder.config import Config
from pi_recorder.models import FAILED, PENDING, RecordingMetadata, UPLOADED, UPLOADING
from pi_recorder.queue import UploadQueue
from pi_recorder.storage import StorageManager
from pi_recorder.system_metrics import collect_metrics


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc

HEARTBEAT_SCHEMA_VERSION = 1
MAX_ERROR_CHARS = 500


def _utc(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(tz=UTC)).astimezone(UTC)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat(timespec="microseconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Optional[datetime], now: datetime) -> Optional[float]:
    return None if value is None else round((now - value).total_seconds(), 1)


class RecorderHealth:
    """In-memory capture health, written by the recorder and read by the heartbeat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # A monotonic clock keeps the uptime correct across system clock changes.
        self._started_monotonic = time.monotonic()
        self._chunks_completed = 0
        self._last_chunk_completed_at: Optional[datetime] = None
        self._last_chunk_filename: Optional[str] = None
        self._last_chunk_duration_seconds: Optional[float] = None
        self._consecutive_capture_failures = 0
        self._last_capture_error: Optional[str] = None
        self._last_capture_error_at: Optional[datetime] = None

    def record_capture_success(
        self,
        recording: RecordingMetadata,
        now: Optional[datetime] = None,
    ) -> None:
        timestamp = _utc(now)
        with self._lock:
            self._chunks_completed += 1
            self._last_chunk_completed_at = timestamp
            self._last_chunk_filename = recording.filename
            self._last_chunk_duration_seconds = recording.duration_seconds
            self._consecutive_capture_failures = 0
            self._last_capture_error = None
            self._last_capture_error_at = None

    def record_capture_failure(self, detail: str, now: Optional[datetime] = None) -> None:
        timestamp = _utc(now)
        with self._lock:
            self._consecutive_capture_failures += 1
            self._last_capture_error = detail[:MAX_ERROR_CHARS]
            self._last_capture_error_at = timestamp

    def snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        current_time = _utc(now)
        with self._lock:
            last_chunk_at = self._last_chunk_completed_at
            return {
                "process_uptime_seconds": round(
                    time.monotonic() - self._started_monotonic, 1
                ),
                "chunks_completed": self._chunks_completed,
                "last_chunk_completed_at": _iso(last_chunk_at),
                "seconds_since_last_chunk": _age_seconds(last_chunk_at, current_time),
                "last_chunk_filename": self._last_chunk_filename,
                "last_chunk_duration_seconds": self._last_chunk_duration_seconds,
                "consecutive_capture_failures": self._consecutive_capture_failures,
                "last_capture_error": self._last_capture_error,
                "last_capture_error_at": _iso(self._last_capture_error_at),
            }


def _queue_state(upload_queue: UploadQueue, now: datetime) -> Dict[str, Any]:
    try:
        statistics = upload_queue.statistics()
    except Exception as exc:
        # A busy database must not cost us the whole heartbeat; report unknown counts.
        LOGGER.warning("Could not read upload queue statistics: %s", exc)
        statistics = {}

    oldest_pending_at = _parse_iso(statistics.get("oldest_pending_created_at"))
    last_uploaded_at = _parse_iso(statistics.get("last_uploaded_at"))
    return {
        PENDING: statistics.get(PENDING),
        UPLOADING: statistics.get(UPLOADING),
        FAILED: statistics.get(FAILED),
        UPLOADED: statistics.get(UPLOADED),
        "oldest_pending_created_at": statistics.get("oldest_pending_created_at"),
        "oldest_pending_age_seconds": _age_seconds(oldest_pending_at, now),
        "last_uploaded_at": statistics.get("last_uploaded_at"),
        "seconds_since_last_upload": _age_seconds(last_uploaded_at, now),
    }


def build_heartbeat_payload(
    config: Config,
    health: RecorderHealth,
    upload_queue: UploadQueue,
    storage: StorageManager,
    status: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble one heartbeat. It never contains the API token or absolute paths."""
    current_time = _utc(now)

    try:
        audio_backend = resolve_audio_backend(config.audio_backend)
    except ValueError:
        audio_backend = config.audio_backend

    recorder_state: Dict[str, Any] = {
        "audio_backend": audio_backend,
        "audio_device": config.audio_device,
        "chunk_seconds": config.chunk_seconds,
    }
    recorder_state.update(health.snapshot(current_time))

    system_state = collect_metrics(storage)
    system_state["min_free_disk_mb"] = config.min_free_disk_mb

    return {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "device_id": config.device_id,
        "sent_at": _iso(current_time),
        "status": status,
        "recorder": recorder_state,
        "queue": _queue_state(upload_queue, current_time),
        "system": system_state,
    }
