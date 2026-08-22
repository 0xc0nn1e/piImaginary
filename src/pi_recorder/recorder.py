import logging
from datetime import datetime, timezone
from threading import Event
from typing import Callable, Optional

from pi_recorder.audio import AudioSource
from pi_recorder.config import DEFAULT_MAX_WAV_BYTES
from pi_recorder.health import RecorderHealth
from pi_recorder.models import RecordingMetadata
from pi_recorder.queue import UploadQueue
from pi_recorder.storage import InvalidAudioFile, StorageManager


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


class Recorder:
    def __init__(
        self,
        audio_source: AudioSource,
        storage: StorageManager,
        upload_queue: UploadQueue,
        device_id: str,
        sample_rate: int,
        channels: int,
        chunk_seconds: int,
        retry_seconds: int,
        maintenance: Optional[Callable[[], None]] = None,
        max_wav_bytes: int = DEFAULT_MAX_WAV_BYTES,
        health: Optional[RecorderHealth] = None,
    ) -> None:
        self.audio_source = audio_source
        self.storage = storage
        self.upload_queue = upload_queue
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_seconds = chunk_seconds
        self.max_wav_bytes = max_wav_bytes
        self.retry_seconds = retry_seconds
        self.maintenance = maintenance
        self.health = health or RecorderHealth()

    def recover_orphans(self) -> int:
        recovered = 0
        for candidate in self.storage.recovery_candidates():
            if candidate.name.endswith(".wav") and self.upload_queue.contains_file(candidate):
                continue
            try:
                file_size = candidate.stat().st_size
            except OSError as exc:
                LOGGER.error("Could not inspect recovery candidate %s: %s", candidate, exc)
                continue
            if file_size > self.max_wav_bytes:
                LOGGER.error(
                    "Oversized audio remains unqueued for manual recovery: %s (%d bytes; limit %d)",
                    candidate,
                    file_size,
                    self.max_wav_bytes,
                )
                continue
            metadata = self.storage.recover_file(
                candidate,
                device_id=self.device_id,
                expected_sample_rate=self.sample_rate,
                expected_channels=self.channels,
            )
            if metadata is None or self.upload_queue.contains_file(metadata.file_path):
                continue
            self.upload_queue.add(metadata)
            recovered += 1
            LOGGER.warning("Recovered unqueued audio chunk %s", metadata.filename)
        return recovered

    def record_one(self, stop_event: Event) -> Optional[RecordingMetadata]:
        started = datetime.now(tz=UTC)
        target = self.storage.create_chunk_target(started)
        LOGGER.info("Starting audio chunk %s", target.final_path.name)
        result = self.audio_source.record(target.partial_path, self.chunk_seconds, stop_event)
        ended = datetime.now(tz=UTC)

        if not self.storage.is_valid_wav(target.partial_path):
            detail = result.error or "no valid audio frames were written"
            LOGGER.error("Audio chunk failed and remains unqueued: %s", detail)
            self.health.record_capture_failure(detail)
            return None

        try:
            file_size = target.partial_path.stat().st_size
        except OSError as exc:
            LOGGER.error("Could not inspect audio chunk; it remains unqueued: %s", exc)
            self.health.record_capture_failure("Could not inspect audio chunk: {}".format(exc))
            return None
        if file_size > self.max_wav_bytes:
            LOGGER.error(
                "Audio chunk exceeds %d-byte limit and remains unqueued: %s (%d bytes)",
                self.max_wav_bytes,
                target.partial_path,
                file_size,
            )
            self.health.record_capture_failure(
                "Audio chunk exceeds the {}-byte limit ({} bytes)".format(
                    self.max_wav_bytes,
                    file_size,
                )
            )
            return None

        try:
            metadata = self.storage.finalize_chunk(
                target,
                end_time=ended,
                device_id=self.device_id,
                expected_sample_rate=self.sample_rate,
                expected_channels=self.channels,
            )
            self.upload_queue.add(metadata)
        except InvalidAudioFile as exc:
            LOGGER.error("Audio chunk validation failed: %s", exc)
            self.health.record_capture_failure("Audio chunk validation failed: {}".format(exc))
            return None

        if not result.completed:
            LOGGER.warning(
                "Preserved partial audio after arecord exit code %s: %s",
                result.return_code,
                metadata.filename,
            )
        self.health.record_capture_success(metadata)
        LOGGER.info(
            "Queued audio chunk %s (%.2fs, %d bytes)",
            metadata.filename,
            metadata.duration_seconds,
            metadata.file_size,
        )
        return metadata

    def run(self, stop_event: Event) -> None:
        LOGGER.info("Recorder started")
        self.recover_orphans()
        while not stop_event.is_set():
            try:
                recording = self.record_one(stop_event)
                if recording is None and not stop_event.is_set():
                    stop_event.wait(self.retry_seconds)
            except Exception as exc:
                LOGGER.exception("Recorder cycle failed; audio files are left in place")
                self.health.record_capture_failure("Recorder cycle failed: {}".format(exc))
                if not stop_event.is_set():
                    stop_event.wait(self.retry_seconds)
                try:
                    self.recover_orphans()
                except Exception:
                    LOGGER.exception("Could not recover orphaned audio chunks")
            if self.maintenance is not None:
                try:
                    self.maintenance()
                except Exception:
                    LOGGER.exception("Recorder maintenance failed")
        LOGGER.info("Recorder stopped")
