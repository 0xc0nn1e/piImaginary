from threading import Event

from pi_recorder.audio import AudioCaptureResult
from pi_recorder.health import RecorderHealth
from pi_recorder.queue import UploadQueue
from pi_recorder.recorder import Recorder
from pi_recorder.storage import StorageManager


class FakeAudioSource:
    def __init__(self, wav_factory, interrupt: bool = False) -> None:
        self.wav_factory = wav_factory
        self.interrupt = interrupt
        self.calls = 0

    def record(self, output_path, duration_seconds, stop_event):
        self.calls += 1
        self.wav_factory(output_path, duration_seconds=0.05)
        if self.interrupt:
            stop_event.set()
            return AudioCaptureResult(False, True, 130, "interrupted for test")
        return AudioCaptureResult(True, False, 0)


def make_recorder(tmp_path, wav_factory, source, max_wav_bytes=98_000_000, health=None):
    storage = StorageManager(tmp_path / "recordings")
    upload_queue = UploadQueue(tmp_path / "recorder.db")
    upload_queue.initialize()
    recorder = Recorder(
        audio_source=source,
        storage=storage,
        upload_queue=upload_queue,
        device_id="test-recorder",
        sample_rate=16000,
        channels=1,
        chunk_seconds=600,
        max_wav_bytes=max_wav_bytes,
        retry_seconds=1,
        health=health,
    )
    return recorder, upload_queue


def test_recorder_creates_and_queues_chunk(tmp_path, wav_factory) -> None:
    source = FakeAudioSource(wav_factory)
    recorder, upload_queue = make_recorder(tmp_path, wav_factory, source)

    recording = recorder.record_one(Event())

    assert recording is not None
    assert recording.file_path.exists()
    assert upload_queue.get(recording.recording_id) is not None


def test_server_unavailable_does_not_affect_recorder(tmp_path, wav_factory) -> None:
    source = FakeAudioSource(wav_factory)
    recorder, upload_queue = make_recorder(tmp_path, wav_factory, source)

    recording = recorder.record_one(Event())

    assert recording is not None
    assert upload_queue.count() == 1


def test_shutdown_preserves_valid_short_chunk(tmp_path, wav_factory) -> None:
    source = FakeAudioSource(wav_factory, interrupt=True)
    recorder, upload_queue = make_recorder(tmp_path, wav_factory, source)
    stop_event = Event()

    recording = recorder.record_one(stop_event)

    assert stop_event.is_set()
    assert recording is not None
    assert upload_queue.count() == 1


def test_restart_recovers_unqueued_closed_chunk(tmp_path, wav_factory) -> None:
    source = FakeAudioSource(wav_factory)
    recorder, upload_queue = make_recorder(tmp_path, wav_factory, source)
    target = recorder.storage.create_chunk_target()
    wav_factory(target.partial_path)

    assert recorder.recover_orphans() == 1

    recovered = upload_queue.get(target.recording_id)
    assert recovered is not None
    assert recovered.file_path.exists()


def test_oversized_recording_remains_unqueued(tmp_path, wav_factory) -> None:
    source = FakeAudioSource(wav_factory)
    recorder, upload_queue = make_recorder(tmp_path, wav_factory, source, max_wav_bytes=100)

    recording = recorder.record_one(Event())

    assert recording is None
    assert upload_queue.count() == 0
    assert list(recorder.storage.recording_dir.rglob("*.wav.partial"))


def test_restart_does_not_queue_oversized_recording(tmp_path, wav_factory) -> None:
    source = FakeAudioSource(wav_factory)
    recorder, upload_queue = make_recorder(tmp_path, wav_factory, source, max_wav_bytes=100)
    target = recorder.storage.create_chunk_target()
    wav_factory(target.partial_path)

    assert recorder.recover_orphans() == 0
    assert upload_queue.count() == 0
    assert target.partial_path.exists()


class FailingAudioSource:
    def record(self, output_path, duration_seconds, stop_event):
        # Nothing is written, so finalization must reject the chunk.
        return AudioCaptureResult(False, False, 1, "arecord: device busy")


def test_health_tracks_a_successful_chunk(tmp_path, wav_factory) -> None:
    health = RecorderHealth()
    source = FakeAudioSource(wav_factory)
    recorder, _ = make_recorder(tmp_path, wav_factory, source, health=health)

    recording = recorder.record_one(Event())

    snapshot = health.snapshot()
    assert snapshot["chunks_completed"] == 1
    assert snapshot["last_chunk_filename"] == recording.filename
    assert snapshot["consecutive_capture_failures"] == 0
    assert snapshot["seconds_since_last_chunk"] is not None


def test_health_counts_consecutive_capture_failures(tmp_path, wav_factory) -> None:
    health = RecorderHealth()
    recorder, _ = make_recorder(tmp_path, wav_factory, FailingAudioSource(), health=health)

    assert recorder.record_one(Event()) is None
    assert recorder.record_one(Event()) is None

    snapshot = health.snapshot()
    assert snapshot["consecutive_capture_failures"] == 2
    assert "device busy" in snapshot["last_capture_error"]
    assert snapshot["last_chunk_completed_at"] is None


def test_health_records_an_oversized_chunk_as_a_failure(tmp_path, wav_factory) -> None:
    health = RecorderHealth()
    source = FakeAudioSource(wav_factory)
    recorder, _ = make_recorder(tmp_path, wav_factory, source, max_wav_bytes=100, health=health)

    assert recorder.record_one(Event()) is None

    snapshot = health.snapshot()
    assert snapshot["consecutive_capture_failures"] == 1
    assert "100-byte limit" in snapshot["last_capture_error"]


def test_health_recovers_after_a_failure(tmp_path, wav_factory) -> None:
    health = RecorderHealth()
    recorder, _ = make_recorder(tmp_path, wav_factory, FailingAudioSource(), health=health)
    recorder.record_one(Event())
    recorder.audio_source = FakeAudioSource(wav_factory)

    assert recorder.record_one(Event()) is not None

    snapshot = health.snapshot()
    assert snapshot["consecutive_capture_failures"] == 0
    assert snapshot["last_capture_error"] is None
