import logging
import signal
import threading
from threading import Event
from types import FrameType
from typing import Optional

from pi_recorder.audio import (
    ArecordAudioSource,
    AudioSource,
    FfmpegAvfoundationAudioSource,
    resolve_audio_backend,
)
from pi_recorder.cleanup import CleanupManager
from pi_recorder.config import Config, ConfigError
from pi_recorder.logging_config import configure_logging
from pi_recorder.queue import UploadQueue
from pi_recorder.recorder import Recorder
from pi_recorder.storage import StorageManager
from pi_recorder.uploader import HttpUploadClient, UploaderWorker


LOGGER = logging.getLogger(__name__)


def build_audio_source(config: Config) -> AudioSource:
    backend = resolve_audio_backend(config.audio_backend)
    if backend == "alsa":
        source: AudioSource = ArecordAudioSource(
            binary=config.arecord_binary,
            device=config.audio_device,
            sample_rate=config.sample_rate,
            channels=config.audio_channels,
            sample_format=config.audio_sample_format,
        )
    elif backend == "avfoundation":
        if config.audio_device.lower() == "default":
            raise ValueError(
                "macOS requires an explicit AUDIO_DEVICE index to avoid the internal microphone"
            )
        source = FfmpegAvfoundationAudioSource(
            binary=config.ffmpeg_binary,
            device=config.audio_device,
            sample_rate=config.sample_rate,
            channels=config.audio_channels,
        )
    else:
        raise ValueError("Unsupported audio backend: {}".format(backend))
    LOGGER.info("Using %s audio backend with device %s", backend, config.audio_device)
    return source


def run(config: Config) -> int:
    stop_event = Event()

    def request_stop(signum: int, _frame: Optional[FrameType]) -> None:
        LOGGER.info("Received signal %s; finishing the current audio chunk", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    storage = StorageManager(config.recording_dir)
    upload_queue = UploadQueue(config.database_path)
    upload_queue.initialize()
    recovered_uploads = upload_queue.recover_uploading()
    if recovered_uploads:
        LOGGER.warning("Recovered %d interrupted upload(s)", recovered_uploads)

    cleanup = CleanupManager(
        storage,
        upload_queue,
        retention_days=config.retention_days,
        min_free_disk_mb=config.min_free_disk_mb,
    )
    cleanup.run()

    audio_source = build_audio_source(config)
    recorder = Recorder(
        audio_source=audio_source,
        storage=storage,
        upload_queue=upload_queue,
        device_id=config.device_id,
        sample_rate=config.sample_rate,
        channels=config.audio_channels,
        chunk_seconds=config.chunk_seconds,
        max_wav_bytes=config.max_wav_bytes,
        retry_seconds=config.record_retry_seconds,
        maintenance=cleanup.run,
    )

    uploader_thread = None
    if config.upload_enabled:
        client = HttpUploadClient(
            config.server_url,
            config.upload_endpoint,
            config.api_token,
            config.upload_timeout_seconds,
            config.max_wav_bytes,
        )
        uploader = UploaderWorker(
            upload_queue,
            client,
            poll_seconds=config.upload_poll_seconds,
            retry_base_seconds=config.retry_base_seconds,
            retry_max_seconds=config.retry_max_seconds,
        )
        uploader_thread = threading.Thread(
            target=uploader.run,
            args=(stop_event,),
            name="pi-recorder-uploader",
            daemon=True,
        )
        uploader_thread.start()
    else:
        LOGGER.warning("SERVER_URL is empty; recordings will remain queued locally")

    try:
        recorder.run(stop_event)
    finally:
        stop_event.set()
        if uploader_thread is not None:
            uploader_thread.join(timeout=10.0)
            if uploader_thread.is_alive():
                LOGGER.warning("Uploader did not stop before the shutdown deadline")
    return 0


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        LOGGER.error("Configuration error: %s", exc)
        return 2

    configure_logging(config.log_level)
    try:
        return run(config)
    except Exception:
        LOGGER.exception("Fatal recorder error")
        return 1
