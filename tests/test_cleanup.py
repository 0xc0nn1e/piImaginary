from datetime import datetime, timedelta, timezone

from pi_recorder.cleanup import CleanupManager
from pi_recorder.queue import UploadQueue
from pi_recorder.storage import StorageManager


UTC = timezone.utc


def test_cleanup_removes_only_expired_uploaded_recording(tmp_path, metadata_factory) -> None:
    storage = StorageManager(tmp_path / "recordings")
    upload_queue = UploadQueue(tmp_path / "recorder.db")
    upload_queue.initialize()
    uploaded = metadata_factory("uploaded.wav")
    pending = metadata_factory("pending.wav")
    upload_queue.add(uploaded)
    upload_queue.add(pending)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    upload_queue.mark_uploaded(uploaded.recording_id, now - timedelta(days=8))
    cleanup = CleanupManager(storage, upload_queue, retention_days=7, min_free_disk_mb=1)

    assert cleanup.run(now) == 1

    assert not uploaded.file_path.exists()
    assert pending.file_path.exists()
    assert upload_queue.get(uploaded.recording_id).local_deleted_at is not None
    assert upload_queue.get(pending.recording_id).local_deleted_at is None


def test_low_disk_cleanup_never_deletes_pending_recording(tmp_path, metadata_factory) -> None:
    storage = StorageManager(tmp_path / "recordings")
    upload_queue = UploadQueue(tmp_path / "recorder.db")
    upload_queue.initialize()
    uploaded = metadata_factory("recent-upload.wav")
    pending = metadata_factory("must-keep.wav")
    upload_queue.add(uploaded)
    upload_queue.add(pending)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    upload_queue.mark_uploaded(uploaded.recording_id, now)
    cleanup = CleanupManager(
        storage,
        upload_queue,
        retention_days=7,
        min_free_disk_mb=10**9,
    )

    cleanup.run(now)

    assert not uploaded.file_path.exists()
    assert pending.file_path.exists()
    assert upload_queue.get(pending.recording_id).upload_status == "pending"
