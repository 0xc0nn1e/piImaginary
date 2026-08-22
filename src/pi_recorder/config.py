import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional
from urllib.parse import urlparse


DEFAULT_MAX_WAV_BYTES = 98_000_000
WAV_HEADER_ALLOWANCE_BYTES = 64 * 1024


class ConfigError(ValueError):
    pass


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError("Invalid .env line {}".format(line_number))
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError("Invalid .env key on line {}".format(line_number))
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError("{} must be an integer".format(name)) from exc
    if value <= 0:
        raise ConfigError("{} must be greater than zero".format(name))
    return value


def _endpoint(
    values: Mapping[str, str],
    name: str,
    default: str,
    allow_blank: bool = False,
) -> str:
    endpoint = values.get(name, default).strip()
    if allow_blank and not endpoint:
        return ""
    if (
        not endpoint.startswith("/")
        or endpoint.startswith("//")
        or "?" in endpoint
        or "#" in endpoint
    ):
        raise ConfigError("{} must start with one slash".format(name))
    return endpoint


def estimated_wav_size_bytes(
    sample_rate: int,
    channels: int,
    duration_seconds: int,
) -> int:
    """Return a conservative size estimate for 16-bit PCM WAV audio."""
    return sample_rate * channels * 2 * duration_seconds + WAV_HEADER_ALLOWANCE_BYTES


@dataclass(frozen=True)
class Config:
    device_id: str
    audio_backend: str
    audio_device: str
    recording_dir: Path
    database_path: Path
    chunk_minutes: int
    sample_rate: int
    audio_channels: int
    audio_sample_format: str
    arecord_binary: str
    ffmpeg_binary: str
    server_url: str
    upload_endpoint: str
    api_token: str = field(repr=False)
    upload_timeout_seconds: int = 60
    upload_poll_seconds: int = 10
    heartbeat_endpoint: str = "/api/v1/heartbeats"
    heartbeat_minutes: int = 10
    heartbeat_timeout_seconds: int = 30
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600
    retention_days: int = 7
    min_free_disk_mb: int = 512
    record_retry_seconds: int = 5
    log_level: str = "INFO"
    max_wav_bytes: int = DEFAULT_MAX_WAV_BYTES

    @property
    def chunk_seconds(self) -> int:
        return self.chunk_minutes * 60

    @property
    def upload_enabled(self) -> bool:
        return bool(self.server_url)

    @property
    def heartbeat_seconds(self) -> int:
        return self.heartbeat_minutes * 60

    @property
    def heartbeat_enabled(self) -> bool:
        return bool(self.server_url and self.heartbeat_endpoint)

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        env_file: Optional[Path] = Path(".env"),
    ) -> "Config":
        values: Dict[str, str] = {}
        if env_file is not None:
            values.update(_read_env_file(env_file))
        values.update(dict(os.environ if environ is None else environ))

        server_url = values.get("SERVER_URL", "").strip().rstrip("/")
        if server_url:
            parsed = urlparse(server_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ConfigError("SERVER_URL must be an absolute HTTPS URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ConfigError("SERVER_URL must not contain credentials, a query, or a fragment")
            try:
                parsed.port
            except ValueError as exc:
                raise ConfigError("SERVER_URL contains an invalid port") from exc

        endpoint = _endpoint(values, "UPLOAD_ENDPOINT", "/api/v1/recordings")
        # An empty HEARTBEAT_ENDPOINT disables reporting, mirroring an empty SERVER_URL.
        heartbeat_endpoint = _endpoint(
            values,
            "HEARTBEAT_ENDPOINT",
            "/api/v1/heartbeats",
            allow_blank=True,
        )

        device_id = values.get("DEVICE_ID", socket.gethostname()).strip()
        if not device_id or not re.fullmatch(r"[A-Za-z0-9._-]+", device_id):
            raise ConfigError("DEVICE_ID may contain only letters, digits, dot, underscore, and dash")

        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL is invalid")

        audio_format = values.get("AUDIO_SAMPLE_FORMAT", "S16_LE").strip().upper()
        if audio_format != "S16_LE":
            raise ConfigError("MVP supports AUDIO_SAMPLE_FORMAT=S16_LE only")

        audio_backend = values.get("AUDIO_BACKEND", "auto").strip().lower()
        if audio_backend not in {"auto", "alsa", "avfoundation"}:
            raise ConfigError("AUDIO_BACKEND must be auto, alsa, or avfoundation")

        chunk_minutes = _positive_int(values, "CHUNK_MINUTES", 10)
        max_wav_bytes = _positive_int(values, "MAX_WAV_BYTES", DEFAULT_MAX_WAV_BYTES)
        if max_wav_bytes > DEFAULT_MAX_WAV_BYTES:
            raise ConfigError(
                "MAX_WAV_BYTES must not exceed {} bytes".format(DEFAULT_MAX_WAV_BYTES)
            )
        sample_rate = _positive_int(values, "SAMPLE_RATE", 16000)
        audio_channels = _positive_int(values, "AUDIO_CHANNELS", 1)
        estimated_size = estimated_wav_size_bytes(
            sample_rate,
            audio_channels,
            chunk_minutes * 60,
        )
        if estimated_size > max_wav_bytes:
            raise ConfigError(
                "CHUNK_MINUTES, SAMPLE_RATE, and AUDIO_CHANNELS may create a WAV "
                "larger than MAX_WAV_BYTES (estimated {} bytes)".format(estimated_size)
            )
        retry_base = _positive_int(values, "RETRY_BASE_SECONDS", 30)
        retry_max = _positive_int(values, "RETRY_MAX_SECONDS", 3600)
        if retry_max < retry_base:
            raise ConfigError("RETRY_MAX_SECONDS must be at least RETRY_BASE_SECONDS")

        return cls(
            device_id=device_id,
            audio_backend=audio_backend,
            audio_device=values.get("AUDIO_DEVICE", "default").strip() or "default",
            recording_dir=Path(values.get("RECORDING_DIR", "./data/recordings")).expanduser(),
            database_path=Path(values.get("DATABASE_PATH", "./data/recorder.db")).expanduser(),
            chunk_minutes=chunk_minutes,
            max_wav_bytes=max_wav_bytes,
            sample_rate=sample_rate,
            audio_channels=audio_channels,
            audio_sample_format=audio_format,
            arecord_binary=values.get("ARECORD_BINARY", "arecord").strip() or "arecord",
            ffmpeg_binary=values.get("FFMPEG_BINARY", "ffmpeg").strip() or "ffmpeg",
            server_url=server_url,
            upload_endpoint=endpoint,
            api_token=values.get("API_TOKEN", ""),
            upload_timeout_seconds=_positive_int(values, "UPLOAD_TIMEOUT_SECONDS", 60),
            upload_poll_seconds=_positive_int(values, "UPLOAD_POLL_SECONDS", 10),
            heartbeat_endpoint=heartbeat_endpoint,
            heartbeat_minutes=_positive_int(values, "HEARTBEAT_MINUTES", 10),
            heartbeat_timeout_seconds=_positive_int(values, "HEARTBEAT_TIMEOUT_SECONDS", 30),
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
            retention_days=_positive_int(values, "RETENTION_DAYS", 7),
            min_free_disk_mb=_positive_int(values, "MIN_FREE_DISK_MB", 512),
            record_retry_seconds=_positive_int(values, "RECORD_RETRY_SECONDS", 5),
            log_level=log_level,
        )
