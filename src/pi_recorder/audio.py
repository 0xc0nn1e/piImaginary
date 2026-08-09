import logging
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


class ArecordAudioSource:
    def __init__(
        self,
        binary: str,
        device: str,
        sample_rate: int,
        channels: int,
        sample_format: str,
        graceful_stop_seconds: float = 8.0,
    ) -> None:
        self.binary = binary
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_format = sample_format
        self.graceful_stop_seconds = graceful_stop_seconds

    def record(self, output_path: Path, duration_seconds: int, stop_event: Event) -> AudioCaptureResult:
        command = [
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
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
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
                LOGGER.warning("arecord did not stop after SIGINT; sending SIGTERM")
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    LOGGER.error("arecord did not stop after SIGTERM; sending SIGKILL")
                    process.kill()

        _, stderr = process.communicate()
        return_code = process.returncode
        error = stderr.strip()[-1000:] if stderr and stderr.strip() else None
        return AudioCaptureResult(return_code == 0, interrupted, return_code, error)
