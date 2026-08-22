import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pi_recorder.config import Config
from pi_recorder.health import (
    HEARTBEAT_SCHEMA_VERSION,
    MAX_ERROR_CHARS,
    RecorderHealth,
    build_heartbeat_payload,
)
from pi_recorder.models import UPLOADED
from pi_recorder.queue import UploadQueue
from pi_recorder.storage import StorageManager


UTC = timezone.utc
NOW = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)
TOKEN = "super-secret-token-value"


def make_config(tmp_path: Path) -> Config:
    return Config(
        device_id="pi-recorder-01",
        audio_backend="alsa",
        audio_device="plughw:CARD=Device,DEV=0",
        recording_dir=tmp_path / "recordings",
        database_path=tmp_path / "recorder.db",
        chunk_minutes=10,
        sample_rate=16000,
        audio_channels=1,
        audio_sample_format="S16_LE",
        arecord_binary="arecord",
        ffmpeg_binary="ffmpeg",
        server_url="https://example.invalid",
        upload_endpoint="/api/v1/recordings",
        api_token=TOKEN,
    )


def make_payload(tmp_path, metadata_factory, status="running", now=NOW):
    config = make_config(tmp_path)
    config.recording_dir.mkdir(parents=True, exist_ok=True)
    storage = StorageManager(config.recording_dir)
    upload_queue = UploadQueue(config.database_path)
    upload_queue.initialize()
    health = RecorderHealth()
    return config, health, upload_queue, storage, build_heartbeat_payload(
        config, health, upload_queue, storage, status, now
    )


def test_success_clears_failure_state() -> None:
    health = RecorderHealth()
    health.record_capture_failure("arecord exited with 1", NOW)
    health.record_capture_failure("arecord exited with 1", NOW)
    assert health.snapshot(NOW)["consecutive_capture_failures"] == 2

    recording = type("R", (), {"filename": "chunk.wav", "duration_seconds": 600.0})()
    health.record_capture_success(recording, NOW)

    snapshot = health.snapshot(NOW + timedelta(seconds=30))
    assert snapshot["consecutive_capture_failures"] == 0
    assert snapshot["last_capture_error"] is None
    assert snapshot["last_capture_error_at"] is None
    assert snapshot["chunks_completed"] == 1
    assert snapshot["last_chunk_filename"] == "chunk.wav"
    assert snapshot["seconds_since_last_chunk"] == 30.0


def test_failure_detail_is_truncated() -> None:
    health = RecorderHealth()
    health.record_capture_failure("x" * (MAX_ERROR_CHARS + 100), NOW)

    assert len(health.snapshot(NOW)["last_capture_error"]) == MAX_ERROR_CHARS


def test_fresh_health_reports_no_chunk_yet() -> None:
    snapshot = RecorderHealth().snapshot(NOW)

    assert snapshot["chunks_completed"] == 0
    assert snapshot["last_chunk_completed_at"] is None
    assert snapshot["seconds_since_last_chunk"] is None
    assert snapshot["process_uptime_seconds"] >= 0


def test_payload_is_json_serializable_and_complete(tmp_path, metadata_factory) -> None:
    _, _, _, _, payload = make_payload(tmp_path, metadata_factory)

    assert payload["schema_version"] == HEARTBEAT_SCHEMA_VERSION
    assert payload["device_id"] == "pi-recorder-01"
    assert payload["status"] == "running"
    assert payload["sent_at"] == "2026-08-23T04:00:00.000000+00:00"
    assert payload["recorder"]["chunk_seconds"] == 600
    assert payload["recorder"]["audio_backend"] == "alsa"
    assert payload["queue"] == {
        "pending": 0,
        "uploading": 0,
        "failed": 0,
        "uploaded": 0,
        "oldest_pending_created_at": None,
        "oldest_pending_age_seconds": None,
        "last_uploaded_at": None,
        "seconds_since_last_upload": None,
    }
    assert payload["system"]["min_free_disk_mb"] == 512
    json.dumps(payload)


def test_payload_never_leaks_the_api_token(tmp_path, metadata_factory) -> None:
    _, _, _, _, payload = make_payload(tmp_path, metadata_factory)

    assert TOKEN not in json.dumps(payload)


def test_payload_reports_queue_backlog_ages(tmp_path, metadata_factory) -> None:
    config, health, upload_queue, storage, _ = make_payload(tmp_path, metadata_factory)
    pending = metadata_factory()
    upload_queue.add(pending)
    done = metadata_factory()
    upload_queue.add(done)
    upload_queue.mark_uploaded(done.recording_id, NOW - timedelta(seconds=120))

    payload = build_heartbeat_payload(config, health, upload_queue, storage, "running", NOW)

    assert payload["queue"]["pending"] == 1
    assert payload["queue"]["uploaded"] == 1
    assert payload["queue"]["oldest_pending_created_at"] == pending.created_at
    assert payload["queue"]["oldest_pending_age_seconds"] > 0
    assert payload["queue"]["seconds_since_last_upload"] == 120.0


def test_payload_survives_an_unreadable_queue(tmp_path, metadata_factory) -> None:
    config, health, _, storage, _ = make_payload(tmp_path, metadata_factory)

    class BrokenQueue:
        def statistics(self):
            raise RuntimeError("database is locked")

    payload = build_heartbeat_payload(config, health, BrokenQueue(), storage, "running", NOW)

    # Unknown counts stay None so the server never mistakes them for a healthy zero.
    assert payload["queue"]["pending"] is None
    assert payload["queue"]["seconds_since_last_upload"] is None
    assert payload["status"] == "running"
    json.dumps(payload)


def test_cleanup_does_not_hide_the_last_upload(tmp_path, metadata_factory) -> None:
    config, health, upload_queue, storage, _ = make_payload(tmp_path, metadata_factory)
    recording = metadata_factory()
    upload_queue.add(recording)
    upload_queue.mark_uploaded(recording.recording_id, NOW - timedelta(seconds=60))
    upload_queue.mark_local_deleted(recording.recording_id, NOW)

    payload = build_heartbeat_payload(config, health, upload_queue, storage, "running", NOW)

    assert payload["queue"][UPLOADED] == 0
    assert payload["queue"]["seconds_since_last_upload"] == 60.0
