from datetime import datetime, timezone

from pi_recorder.models import FAILED, PENDING, UPLOADED, UPLOADING
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


def make_queue(tmp_path) -> UploadQueue:
    upload_queue = UploadQueue(tmp_path / "recorder.db")
    upload_queue.initialize()
    return upload_queue


def test_statistics_on_an_empty_queue(tmp_path) -> None:
    statistics = make_queue(tmp_path).statistics()

    assert statistics[PENDING] == 0
    assert statistics[UPLOADING] == 0
    assert statistics[UPLOADED] == 0
    assert statistics[FAILED] == 0
    assert statistics["oldest_pending_created_at"] is None
    assert statistics["last_uploaded_at"] is None


def test_statistics_counts_each_status(tmp_path, metadata_factory) -> None:
    upload_queue = make_queue(tmp_path)
    now = datetime(2026, 8, 23, tzinfo=UTC)

    waiting = metadata_factory()
    upload_queue.add(waiting)
    upload_queue.add(metadata_factory())

    failing = metadata_factory()
    upload_queue.add(failing)
    upload_queue.mark_failed(failing.recording_id, "offline", 30, 3600, now)

    done = metadata_factory()
    upload_queue.add(done)
    upload_queue.mark_uploaded(done.recording_id, now)

    statistics = upload_queue.statistics()

    assert statistics[PENDING] == 2
    assert statistics[FAILED] == 1
    assert statistics[UPLOADED] == 1
    assert statistics[UPLOADING] == 0
    # A failed upload is still outstanding work, so it counts toward the backlog age.
    assert statistics["oldest_pending_created_at"] == waiting.created_at
    assert statistics["last_uploaded_at"] is not None


def test_statistics_excludes_locally_deleted_files_but_keeps_upload_history(
    tmp_path, metadata_factory
) -> None:
    upload_queue = make_queue(tmp_path)
    now = datetime(2026, 8, 23, tzinfo=UTC)
    recording = metadata_factory()
    upload_queue.add(recording)
    upload_queue.mark_uploaded(recording.recording_id, now)
    upload_queue.mark_local_deleted(recording.recording_id, now)

    statistics = upload_queue.statistics()

    assert statistics[UPLOADED] == 0
    assert statistics["last_uploaded_at"] is not None
