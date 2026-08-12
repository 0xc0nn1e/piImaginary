import io

import pytest

from pi_recorder import manual_upload
from pi_recorder.config import DEFAULT_MAX_WAV_BYTES
from pi_recorder.manual_upload import ManualUploadError, TerminalProgressBar, metadata_for_wav


def test_manual_metadata_for_valid_wav(tmp_path, wav_factory) -> None:
    path = wav_factory(tmp_path / "meeting.wav")

    metadata = metadata_for_wav(path, "test-device", DEFAULT_MAX_WAV_BYTES)

    assert metadata.filename == "meeting.wav"
    assert metadata.file_size == path.stat().st_size
    assert metadata.sample_rate == 16000
    assert metadata.channels == 1
    assert metadata.device_id == "test-device"
    assert len(metadata.checksum_sha256) == 64


def test_manual_metadata_accepts_exact_size_limit(tmp_path, wav_factory) -> None:
    path = wav_factory(tmp_path / "meeting.wav")
    exact_size = path.stat().st_size

    metadata = metadata_for_wav(path, "test-device", exact_size)

    assert metadata.file_size == exact_size


def test_manual_metadata_rejects_file_over_size_limit(tmp_path, wav_factory) -> None:
    path = wav_factory(tmp_path / "meeting.wav")

    with pytest.raises(ManualUploadError, match="maximum"):
        metadata_for_wav(path, "test-device", path.stat().st_size - 1)


def test_progress_bar_reaches_100_percent() -> None:
    output = io.StringIO()
    progress = TerminalProgressBar(output, width=10)

    progress.update(0, 100)
    progress.update(40, 100)
    progress.update(100, 100)

    assert " 40%" in output.getvalue()
    assert "100%" in output.getvalue()
    assert output.getvalue().endswith("\n")


def test_manual_cli_uploads_without_starting_recorder(tmp_path, wav_factory, monkeypatch) -> None:
    path = wav_factory(tmp_path / "meeting.wav")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SERVER_URL=https://upload.example.test\n"
        "API_TOKEN=test-token\n"
        "DEVICE_ID=test-device\n",
        encoding="utf-8",
    )
    uploaded = []

    class FakeHttpUploadClient:
        def __init__(
            self,
            server_url,
            endpoint,
            api_token,
            timeout_seconds,
            max_wav_bytes,
        ) -> None:
            assert server_url == "https://upload.example.test"
            assert api_token == "test-token"
            assert max_wav_bytes == DEFAULT_MAX_WAV_BYTES

        def upload(self, recording, progress_callback=None) -> None:
            uploaded.append(recording)
            progress_callback(0, recording.file_size)
            progress_callback(recording.file_size, recording.file_size)

    monkeypatch.setattr(manual_upload, "HttpUploadClient", FakeHttpUploadClient)

    assert manual_upload.main([str(path), "--env-file", str(env_file)]) == 0
    assert [recording.filename for recording in uploaded] == ["meeting.wav"]
