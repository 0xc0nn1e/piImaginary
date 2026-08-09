from datetime import datetime, timezone

from pi_recorder.storage import StorageManager


UTC = timezone.utc


def test_chunk_creation_uses_date_directory_and_partial_file(tmp_path) -> None:
    storage = StorageManager(tmp_path / "recordings")
    started = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)

    target = storage.create_chunk_target(started)

    assert target.final_path.parent.name == "2026-08-09"
    assert target.final_path.name.startswith("20260809T090000Z_")
    assert target.partial_path.name.endswith(".wav.partial")


def test_metadata_creation_and_checksum(tmp_path, wav_factory) -> None:
    storage = StorageManager(tmp_path / "recordings")
    started = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    target = storage.create_chunk_target(started)
    wav_factory(target.partial_path, duration_seconds=0.1)

    metadata = storage.finalize_chunk(
        target,
        end_time=started,
        device_id="test-recorder",
        expected_sample_rate=16000,
        expected_channels=1,
    )

    assert target.final_path.exists()
    assert not target.partial_path.exists()
    assert metadata.duration_seconds == 0.1
    assert metadata.file_size == target.final_path.stat().st_size
    assert len(metadata.checksum_sha256) == 64
    assert StorageManager.sha256(target.final_path) == metadata.checksum_sha256
    assert metadata.filename.startswith("2026-08-09/")


def test_valid_partial_chunk_can_be_recovered(tmp_path, wav_factory) -> None:
    storage = StorageManager(tmp_path / "recordings")
    started = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    target = storage.create_chunk_target(started)
    wav_factory(target.partial_path)

    metadata = storage.recover_file(target.partial_path, "test-recorder", 16000, 1)

    assert metadata is not None
    assert metadata.recording_id == target.recording_id
    assert metadata.file_path == target.final_path
    assert target.final_path.exists()
