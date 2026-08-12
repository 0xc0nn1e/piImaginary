import signal
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from pi_recorder.audio import (
    ArecordAudioSource,
    FfmpegAvfoundationAudioSource,
    resolve_audio_backend,
)
from pi_recorder.config import Config
from pi_recorder.main import build_audio_source


def test_auto_backend_uses_avfoundation_on_macos() -> None:
    assert resolve_audio_backend("auto", "Darwin") == "avfoundation"


def test_auto_backend_uses_alsa_on_linux() -> None:
    assert resolve_audio_backend("auto", "Linux") == "alsa"


def test_explicit_backend_is_preserved() -> None:
    assert resolve_audio_backend("alsa", "Darwin") == "alsa"


def test_auto_backend_rejects_unsupported_platform() -> None:
    with pytest.raises(ValueError, match="does not support"):
        resolve_audio_backend("auto", "Windows")


def test_arecord_command_uses_selected_device_and_wav_format() -> None:
    source = ArecordAudioSource("arecord", "plughw:CARD=USB,DEV=0", 16000, 1, "S16_LE")

    command = source.build_command(Path("chunk.wav.partial"), 600)

    assert command == [
        "arecord",
        "--quiet",
        "--device",
        "plughw:CARD=USB,DEV=0",
        "--file-type",
        "wav",
        "--format",
        "S16_LE",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--duration",
        "600",
        "chunk.wav.partial",
    ]


def test_avfoundation_command_uses_selected_usb_index_and_pcm_wav() -> None:
    source = FfmpegAvfoundationAudioSource("ffmpeg", "2", 16000, 1)

    command = source.build_command(Path("chunk.wav.partial"), 600)

    assert command[:10] == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "avfoundation",
        "-i",
        ":2",
    ]
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert command[-3:] == ["-f", "wav", "chunk.wav.partial"]


def test_avfoundation_device_may_include_audio_separator() -> None:
    source = FfmpegAvfoundationAudioSource("ffmpeg", ":USB Audio", 16000, 1)

    command = source.build_command(Path("chunk.wav.partial"), 5)

    assert command[command.index("-i") + 1] == ":USB Audio"


def test_missing_audio_executable_returns_clear_error() -> None:
    source = FfmpegAvfoundationAudioSource("missing-ffmpeg", "2", 16000, 1)

    with patch("pi_recorder.audio.subprocess.Popen", side_effect=FileNotFoundError):
        result = source.record(Path("chunk.wav.partial"), 5, Event())

    assert not result.completed
    assert result.return_code is None
    assert result.error == "ffmpeg executable not found: missing-ffmpeg"


class InterruptibleProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.signal = None

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        self.signal = signum
        self.returncode = 255

    def communicate(self):
        return None, ""


def test_avfoundation_shutdown_sends_sigint() -> None:
    source = FfmpegAvfoundationAudioSource("ffmpeg", "2", 16000, 1)
    process = InterruptibleProcess()
    stop_event = Event()
    stop_event.set()

    with patch("pi_recorder.audio.subprocess.Popen", return_value=process):
        result = source.record(Path("chunk.wav.partial"), 600, stop_event)

    assert result.interrupted
    assert process.signal == signal.SIGINT
    assert result.return_code == 255


def test_main_builds_avfoundation_source_from_config() -> None:
    config = Config.from_env(
        {"AUDIO_BACKEND": "avfoundation", "AUDIO_DEVICE": "2"}, env_file=None
    )

    source = build_audio_source(config)

    assert isinstance(source, FfmpegAvfoundationAudioSource)
    assert source.device == "2"


def test_main_rejects_default_macos_input() -> None:
    config = Config.from_env(
        {"AUDIO_BACKEND": "avfoundation", "AUDIO_DEVICE": "default"}, env_file=None
    )

    with pytest.raises(ValueError, match="explicit AUDIO_DEVICE"):
        build_audio_source(config)


def test_main_builds_alsa_source_from_config() -> None:
    config = Config.from_env(
        {"AUDIO_BACKEND": "alsa", "AUDIO_DEVICE": "plughw:CARD=USB,DEV=0"},
        env_file=None,
    )

    source = build_audio_source(config)

    assert isinstance(source, ArecordAudioSource)
    assert source.device == "plughw:CARD=USB,DEV=0"
