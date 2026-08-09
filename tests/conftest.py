import hashlib
import uuid
import wave
from pathlib import Path

import pytest

from pi_recorder.models import PENDING, RecordingMetadata


@pytest.fixture
def wav_factory():
    def create(
        path: Path,
        duration_seconds: float = 0.05,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame_count = max(1, int(duration_seconds * sample_rate))
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"\x00\x00" * frame_count * channels)
        return path

    return create


@pytest.fixture
def metadata_factory(tmp_path, wav_factory):
    counter = 0

    def create(name: str = None) -> RecordingMetadata:
        nonlocal counter
        counter += 1
        recording_id = str(uuid.uuid4())
        filename = name or "2026-08-09/chunk-{}.wav".format(counter)
        file_path = wav_factory(tmp_path / "recordings" / filename)
        checksum = hashlib.sha256(file_path.read_bytes()).hexdigest()
        return RecordingMetadata(
            recording_id=recording_id,
            start_time="2026-08-09T00:00:00.000000+00:00",
            end_time="2026-08-09T00:00:00.050000+00:00",
            duration_seconds=0.05,
            filename=filename,
            file_path=file_path,
            file_size=file_path.stat().st_size,
            checksum_sha256=checksum,
            upload_status=PENDING,
            retry_count=0,
            created_at="2026-08-09T00:00:00.050000+00:00",
            device_id="test-recorder",
            audio_format="wav",
            sample_rate=16000,
            channels=1,
        )

    return create
