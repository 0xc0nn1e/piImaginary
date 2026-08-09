from datetime import datetime, timezone

from pi_recorder.models import PENDING, UPLOADING
from pi_recorder.queue import UploadQueue


UTC = timezone.utc


def test_queue_persistence_after_restart(tmp_path, metadata_factory) -> None:
    database_path = tmp_path / "recorder.db"
    recording = metadata_factory()
    first_queue = UploadQueue(database_path)
    first_queue.initialize()
    first_queue.add(recording)

    restarted_queue = UploadQueue(database_path)
    restarted_queue.initialize()
    loaded = restarted_queue.get(recording.recording_id)

    assert restarted_queue.count() == 1
    assert loaded is not None
    assert loaded.checksum_sha256 == recording.checksum_sha256
    assert loaded.upload_status == PENDING


def test_queue_restart_recovers_uploading_item(tmp_path, metadata_factory) -> None:
    database_path = tmp_path / "recorder.db"
    recording = metadata_factory()
    upload_queue = UploadQueue(database_path)
    upload_queue.initialize()
    upload_queue.add(recording)
    claimed = upload_queue.claim_next(datetime(2026, 8, 9, tzinfo=UTC))

    assert claimed is not None
    assert claimed.upload_status == UPLOADING

    restarted_queue = UploadQueue(database_path)
    restarted_queue.initialize()
    assert restarted_queue.recover_uploading(datetime(2026, 8, 10, tzinfo=UTC)) == 1
    recovered = restarted_queue.get(recording.recording_id)
    assert recovered is not None
    assert recovered.upload_status == PENDING
    assert "restart" in recovered.last_error
