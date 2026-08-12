from pathlib import Path

import pytest

from pi_recorder.config import (
    DEFAULT_MAX_WAV_BYTES,
    Config,
    ConfigError,
    estimated_wav_size_bytes,
)


def test_config_defaults() -> None:
    config = Config.from_env({}, env_file=None)

    assert config.chunk_minutes == 10
    assert config.chunk_seconds == 600
    assert config.max_wav_bytes == 98_000_000
    assert config.sample_rate == 16000
    assert config.audio_channels == 1
    assert config.audio_backend == "auto"
    assert config.ffmpeg_binary == "ffmpeg"
    assert not config.upload_enabled


def test_config_loads_env_file_and_environment_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEVICE_ID=from-file\nSERVER_URL=https://upload.example.test\nCHUNK_MINUTES=5\n",
        encoding="utf-8",
    )

    config = Config.from_env({"DEVICE_ID": "from-environment"}, env_file=env_file)

    assert config.device_id == "from-environment"
    assert config.server_url == "https://upload.example.test"
    assert config.chunk_minutes == 5


@pytest.mark.parametrize(
    "values",
    [
        {"SERVER_URL": "http://example.test"},
        {"SERVER_URL": "https://user:password@example.test"},
        {"SERVER_URL": "https://example.test?token=value"},
        {"UPLOAD_ENDPOINT": "/api/v1/recordings?unsafe=true"},
        {"CHUNK_MINUTES": "0"},
        {"RETRY_BASE_SECONDS": "60", "RETRY_MAX_SECONDS": "30"},
        {"DEVICE_ID": "not valid"},
        {"AUDIO_SAMPLE_FORMAT": "FLOAT_LE"},
        {"AUDIO_BACKEND": "unknown"},
        {"MAX_WAV_BYTES": str(DEFAULT_MAX_WAV_BYTES + 1)},
        {"MAX_WAV_BYTES": "1000"},
    ],
)
def test_config_rejects_invalid_values(values) -> None:
    with pytest.raises(ConfigError):
        Config.from_env(values, env_file=None)


def test_config_repr_hides_api_token() -> None:
    config = Config.from_env({"API_TOKEN": "test-secret-value"}, env_file=None)

    assert "test-secret-value" not in repr(config)


def test_default_chunk_is_well_below_wav_size_limit() -> None:
    config = Config.from_env({}, env_file=None)

    assert (
        estimated_wav_size_bytes(
            config.sample_rate,
            config.audio_channels,
            config.chunk_seconds,
        )
        < config.max_wav_bytes
    )
