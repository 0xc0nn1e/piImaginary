import argparse
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence, TextIO

from pi_recorder.config import Config, ConfigError
from pi_recorder.logging_config import configure_logging
from pi_recorder.models import PENDING, RecordingMetadata
from pi_recorder.storage import InvalidAudioFile, StorageManager, utc_iso, utc_now
from pi_recorder.uploader import HttpUploadClient, UploadError


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


class ManualUploadError(ValueError):
    pass


class TerminalProgressBar:
    def __init__(self, stream: TextIO = sys.stderr, width: int = 32) -> None:
        self.stream = stream
        self.width = width
        self.finished = False

    def update(self, bytes_sent: int, total_bytes: int) -> None:
        ratio = min(1.0, bytes_sent / float(total_bytes)) if total_bytes else 1.0
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        self.stream.write(
            "\rUploading [{}] {:3d}% {:.1f}/{:.1f} MB".format(
                bar,
                int(ratio * 100),
                bytes_sent / 1_000_000,
                total_bytes / 1_000_000,
            )
        )
        if bytes_sent >= total_bytes and not self.finished:
            self.stream.write("\n")
            self.finished = True
        self.stream.flush()

    def finish_error(self) -> None:
        if not self.finished:
            self.stream.write("\n")
            self.stream.flush()
            self.finished = True


def metadata_for_wav(path: Path, device_id: str, max_wav_bytes: int) -> RecordingMetadata:
    requested_path = path.expanduser()
    if requested_path.suffix.lower() != ".wav":
        raise ManualUploadError("Only .wav files can be uploaded")
    try:
        resolved_path = requested_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ManualUploadError("WAV file does not exist: {}".format(requested_path)) from exc
    if not resolved_path.is_file():
        raise ManualUploadError("WAV path is not a regular file: {}".format(requested_path))

    before = resolved_path.stat()
    if before.st_size > max_wav_bytes:
        raise ManualUploadError(
            "WAV file is {} bytes; maximum is {} bytes".format(
                before.st_size,
                max_wav_bytes,
            )
        )

    duration, channels, sample_rate, _sample_width = StorageManager.inspect_wav(resolved_path)
    checksum = StorageManager.sha256(resolved_path)
    after = resolved_path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ManualUploadError("WAV file changed while metadata was being prepared")

    end_time = datetime.fromtimestamp(after.st_mtime, tz=UTC)
    start_time = end_time - timedelta(seconds=duration)
    return RecordingMetadata(
        recording_id=str(uuid.uuid4()),
        start_time=utc_iso(start_time),
        end_time=utc_iso(end_time),
        duration_seconds=round(duration, 6),
        filename=resolved_path.name,
        file_path=resolved_path,
        file_size=after.st_size,
        checksum_sha256=checksum,
        upload_status=PENDING,
        retry_count=0,
        created_at=utc_iso(utc_now()),
        device_id=device_id,
        audio_format="wav",
        sample_rate=sample_rate,
        channels=channels,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload one existing WAV file without starting the recorder."
    )
    parser.add_argument("wav_file", type=Path, help="WAV file to upload")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="configuration file (default: .env)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.from_env(env_file=args.env_file)
    except ConfigError as exc:
        configure_logging("INFO")
        LOGGER.error("Configuration error: %s", exc)
        return 2

    configure_logging(config.log_level)
    if not config.upload_enabled:
        LOGGER.error("SERVER_URL is empty; set it in %s", args.env_file)
        return 2

    progress = TerminalProgressBar()
    try:
        recording = metadata_for_wav(
            args.wav_file,
            device_id=config.device_id,
            max_wav_bytes=config.max_wav_bytes,
        )
        client = HttpUploadClient(
            config.server_url,
            config.upload_endpoint,
            config.api_token,
            config.upload_timeout_seconds,
            config.max_wav_bytes,
        )
        client.upload(recording, progress_callback=progress.update)
    except (InvalidAudioFile, ManualUploadError, UploadError, OSError) as exc:
        progress.finish_error()
        LOGGER.error("Upload failed: %s", exc)
        return 1

    LOGGER.info(
        "Upload confirmed for %s (%s)",
        recording.filename,
        recording.recording_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
