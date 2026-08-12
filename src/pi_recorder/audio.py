import logging
import platform
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Optional, Protocol


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioCaptureResult:
    completed: bool
    interrupted: bool
    return_code: Optional[int]
    error: Optional[str] = None


class AudioSource(Protocol):
    def record(self, output_path: Path, duration_seconds: int, stop_event: Event) -> AudioCaptureResult:
        ...


def resolve_audio_backend(configured_backend: str, system_name: Optional[str] = None) -> str:
    if configured_backend != "auto":
        return configured_backend

    system = system_name or platform.system()
    if system == "Darwin":
        return "avfoundation"
    if system == "Linux":
        return "alsa"
    raise ValueError("AUDIO_BACKEND=auto does not support {}".format(system or "this OS"))


class SubprocessAudioSource:
    program_name = "audio recorder"

    def __init__(self, graceful_stop_seconds: float = 8.0) -> None:
        self.graceful_stop_seconds = graceful_stop_seconds

    def build_command(self, output_path: Path, duration_seconds: int) -> list:
        raise NotImplementedError

    def record(self, output_path: Path, duration_seconds: int, stop_event: Event) -> AudioCaptureResult:
        command = self.build_command(output_path, duration_seconds)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return AudioCaptureResult(
                False,
                False,
                None,
                "{} executable not found: {}".format(self.program_name, command[0]),
            )
        except OSError as exc:
            return AudioCaptureResult(False, False, None, str(exc))

        interrupted = False
        while process.poll() is None:
            if stop_event.wait(0.2):
                interrupted = True
                process.send_signal(signal.SIGINT)
                break

        if interrupted:
            deadline = time.monotonic() + self.graceful_stop_seconds
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if process.poll() is None:
                LOGGER.warning("%s did not stop after SIGINT; sending SIGTERM", self.program_name)
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    LOGGER.error("%s did not stop after SIGTERM; sending SIGKILL", self.program_name)
                    process.kill()

        _, stderr = process.communicate()
        return_code = process.returncode
        error = stderr.strip()[-1000:] if stderr and stderr.strip() else None
        return AudioCaptureResult(return_code == 0, interrupted, return_code, error)


class ArecordAudioSource(SubprocessAudioSource):
    program_name = "arecord"

    def __init__(
        self,
        binary: str,
        device: str,
        sample_rate: int,
        channels: int,
        sample_format: str,
        graceful_stop_seconds: float = 8.0,
    ) -> None:
        super().__init__(graceful_stop_seconds)
        self.binary = binary
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_format = sample_format

    def build_command(self, output_path: Path, duration_seconds: int) -> list:
        return [
            self.binary,
            "--quiet",
            "--device",
            self.device,
            "--file-type",
            "wav",
            "--format",
            self.sample_format,
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(self.channels),
            "--duration",
            str(duration_seconds),
            str(output_path),
        ]


class FfmpegAvfoundationAudioSource(SubprocessAudioSource):
    program_name = "ffmpeg"

    def __init__(
        self,
        binary: str,
        device: str,
        sample_rate: int,
        channels: int,
        graceful_stop_seconds: float = 8.0,
    ) -> None:
        super().__init__(graceful_stop_seconds)
        self.binary = binary
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels

    def build_command(self, output_path: Path, duration_seconds: int) -> list:
        avfoundation_input = self.device if self.device.startswith(":") else ":{}".format(self.device)
        return [
            self.binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            avfoundation_input,
            "-t",
            str(duration_seconds),
            "-ac",
            str(self.channels),
            "-ar",
            str(self.sample_rate),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output_path),
        ]
