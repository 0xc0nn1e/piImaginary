from threading import Event

from pi_recorder.audio import AudioCaptureResult
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


def make_recorder(tmp_path, wav_factory, source):
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
        retry_seconds=1,
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
