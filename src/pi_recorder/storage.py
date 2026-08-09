import hashlib
import os
import shutil
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

from pi_recorder.models import PENDING, RecordingMetadata


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class ChunkTarget:
    recording_id: str
    start_time: datetime
    partial_path: Path
    final_path: Path


class InvalidAudioFile(Exception):
    pass


class StorageManager:
    def __init__(self, recording_dir: Path) -> None:
        self.recording_dir = recording_dir.resolve()
        self.recording_dir.mkdir(parents=True, exist_ok=True)

    def create_chunk_target(self, start_time: Optional[datetime] = None) -> ChunkTarget:
        started = (start_time or utc_now()).astimezone(UTC)
        recording_id = str(uuid.uuid4())
        directory = self.recording_dir / started.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        basename = "{}_{}.wav".format(started.strftime("%Y%m%dT%H%M%SZ"), recording_id)
        final_path = directory / basename
        return ChunkTarget(
            recording_id=recording_id,
            start_time=started,
            partial_path=directory / (basename + ".partial"),
            final_path=final_path,
        )

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as audio_file:
            for block in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def inspect_wav(path: Path) -> Tuple[float, int, int, int]:
        try:
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                sample_width = wav_file.getsampwidth()
                frame_count = wav_file.getnframes()
        except (EOFError, OSError, wave.Error) as exc:
            raise InvalidAudioFile("invalid WAV file: {}".format(exc)) from exc

        if channels <= 0 or sample_rate <= 0 or sample_width <= 0 or frame_count <= 0:
            raise InvalidAudioFile("WAV file contains no audio frames")
        return frame_count / float(sample_rate), channels, sample_rate, sample_width

    def is_valid_wav(self, path: Path) -> bool:
        try:
            self.inspect_wav(path)
            return True
        except (InvalidAudioFile, FileNotFoundError):
            return False

    def finalize_chunk(
        self,
        target: ChunkTarget,
        end_time: datetime,
        device_id: str,
        expected_sample_rate: int,
        expected_channels: int,
    ) -> RecordingMetadata:
        duration, channels, sample_rate, sample_width = self.inspect_wav(target.partial_path)
        if sample_width != 2:
            raise InvalidAudioFile("expected 16-bit WAV audio")
        if sample_rate != expected_sample_rate or channels != expected_channels:
            raise InvalidAudioFile(
                "unexpected WAV parameters: {} Hz, {} channel(s)".format(sample_rate, channels)
            )

        with target.partial_path.open("rb") as audio_file:
            os.fsync(audio_file.fileno())
        os.replace(str(target.partial_path), str(target.final_path))
        return self._metadata_for_file(
            path=target.final_path,
            recording_id=target.recording_id,
            start_time=target.start_time,
            end_time=end_time,
            duration=duration,
            device_id=device_id,
            sample_rate=sample_rate,
            channels=channels,
        )

    def _metadata_for_file(
        self,
        path: Path,
        recording_id: str,
        start_time: datetime,
        end_time: datetime,
        duration: float,
        device_id: str,
        sample_rate: int,
        channels: int,
    ) -> RecordingMetadata:
        stat = path.stat()
        return RecordingMetadata(
            recording_id=recording_id,
            start_time=utc_iso(start_time),
            end_time=utc_iso(end_time),
            duration_seconds=round(duration, 6),
            filename=str(path.relative_to(self.recording_dir)),
            file_path=path.resolve(),
            file_size=stat.st_size,
            checksum_sha256=self.sha256(path),
            upload_status=PENDING,
            retry_count=0,
            created_at=utc_iso(datetime.fromtimestamp(stat.st_mtime, tz=UTC)),
            device_id=device_id,
            audio_format="wav",
            sample_rate=sample_rate,
            channels=channels,
        )

    def recover_file(
        self,
        path: Path,
        device_id: str,
        expected_sample_rate: int,
        expected_channels: int,
    ) -> Optional[RecordingMetadata]:
        candidate = path
        if candidate.name.endswith(".wav.partial"):
            if not self.is_valid_wav(candidate):
                return None
            final_path = candidate.with_name(candidate.name[: -len(".partial")])
            if final_path.exists():
                return None
            with candidate.open("rb") as audio_file:
                os.fsync(audio_file.fileno())
            os.replace(str(candidate), str(final_path))
            candidate = final_path

        try:
            timestamp_text, identifier_with_suffix = candidate.name.split("_", 1)
            identifier = identifier_with_suffix[: -len(".wav")]
            recording_id = str(uuid.UUID(identifier))
            start_time = datetime.strptime(timestamp_text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            duration, channels, sample_rate, sample_width = self.inspect_wav(candidate)
        except (ValueError, InvalidAudioFile):
            return None

        if sample_width != 2 or sample_rate != expected_sample_rate or channels != expected_channels:
            return None
        return self._metadata_for_file(
            path=candidate,
            recording_id=recording_id,
            start_time=start_time,
            end_time=start_time + timedelta(seconds=duration),
            duration=duration,
            device_id=device_id,
            sample_rate=sample_rate,
            channels=channels,
        )

    def recovery_candidates(self) -> Iterator[Path]:
        for pattern in ("*.wav", "*.wav.partial"):
            for candidate in sorted(self.recording_dir.rglob(pattern)):
                if candidate.is_file() and not candidate.is_symlink():
                    yield candidate

    def free_bytes(self) -> int:
        return shutil.disk_usage(str(self.recording_dir)).free

    def unlink_recording(self, path: Path) -> bool:
        if path.is_symlink():
            return False
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.recording_dir)
        except (FileNotFoundError, ValueError):
            return False
        if not resolved.is_file():
            return False
        resolved.unlink()
        self._remove_empty_parents(resolved.parent)
        return True

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self.recording_dir:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
