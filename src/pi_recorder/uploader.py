import http.client
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Optional, Protocol
from urllib.parse import urlparse

from pi_recorder.config import DEFAULT_MAX_WAV_BYTES
from pi_recorder.models import RecordingMetadata
from pi_recorder.queue import UploadQueue


LOGGER = logging.getLogger(__name__)


class UploadError(Exception):
    pass


class UploadClient(Protocol):
    def upload(self, recording: RecordingMetadata) -> None:
        ...


class HttpUploadClient:
    def __init__(
        self,
        server_url: str,
        endpoint: str,
        api_token: str,
        timeout_seconds: int,
        max_wav_bytes: int = DEFAULT_MAX_WAV_BYTES,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.endpoint = endpoint
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self.max_wav_bytes = max_wav_bytes

    def upload(
        self,
        recording: RecordingMetadata,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise UploadError("Invalid server URL")
        if not recording.file_path.is_file():
            raise UploadError("Audio file is missing")
        actual_size = recording.file_path.stat().st_size
        if actual_size > self.max_wav_bytes:
            raise UploadError(
                "WAV file is {} bytes; maximum is {} bytes".format(
                    actual_size,
                    self.max_wav_bytes,
                )
            )
        if actual_size != recording.file_size:
            raise UploadError(
                "Audio file size changed from {} to {} bytes".format(
                    recording.file_size,
                    actual_size,
                )
            )

        boundary = "pi-recorder-{}".format(uuid.uuid4().hex)
        metadata_json = json.dumps(
            recording.as_upload_dict(), separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        checksum = recording.checksum_sha256.encode("ascii")
        filename = Path(recording.filename).name.replace('"', "_")

        metadata_part = self._part(
            boundary,
            'form-data; name="metadata"',
            "application/json",
            metadata_json,
        )
        checksum_part = self._part(
            boundary,
            'form-data; name="checksum"',
            "text/plain",
            checksum,
        )
        audio_header = (
            "--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"audio\"; filename=\"{filename}\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).format(boundary=boundary, filename=filename).encode("ascii")
        closing = "\r\n--{}--\r\n".format(boundary).encode("ascii")
        content_length = (
            len(metadata_part)
            + len(checksum_part)
            + len(audio_header)
            + actual_size
            + len(closing)
        )

        connection_class = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(parsed.hostname, parsed.port, timeout=self.timeout_seconds)
        base_path = parsed.path.rstrip("/")
        request_path = (base_path + self.endpoint) or "/"
        try:
            connection.putrequest("POST", request_path)
            connection.putheader("Content-Type", "multipart/form-data; boundary={}".format(boundary))
            connection.putheader("Content-Length", str(content_length))
            connection.putheader("Accept", "application/json")
            connection.putheader("Idempotency-Key", recording.recording_id)
            connection.putheader("X-Device-ID", recording.device_id)
            connection.putheader("X-Content-SHA256", recording.checksum_sha256)
            if self.api_token:
                connection.putheader("Authorization", "Bearer {}".format(self.api_token))
            connection.endheaders()
            connection.send(metadata_part)
            connection.send(checksum_part)
            connection.send(audio_header)
            bytes_sent = 0
            if progress_callback is not None:
                progress_callback(bytes_sent, actual_size)
            with recording.file_path.open("rb") as audio_file:
                for block in iter(lambda: audio_file.read(64 * 1024), b""):
                    connection.send(block)
                    bytes_sent += len(block)
                    if progress_callback is not None:
                        progress_callback(bytes_sent, actual_size)
            connection.send(closing)

            response = connection.getresponse()
            response_text = response.read(4096).decode("utf-8", errors="replace").strip()
            if not 200 <= response.status < 300:
                detail = response_text[:500] or response.reason
                raise UploadError("Server returned HTTP {}: {}".format(response.status, detail))
        except (OSError, http.client.HTTPException) as exc:
            raise UploadError("Upload request failed: {}".format(exc)) from exc
        finally:
            connection.close()

    @staticmethod
    def _part(boundary: str, disposition: str, content_type: str, body: bytes) -> bytes:
        return (
            "--{boundary}\r\n"
            "Content-Disposition: {disposition}\r\n"
            "Content-Type: {content_type}\r\n\r\n"
        ).format(
            boundary=boundary,
            disposition=disposition,
            content_type=content_type,
        ).encode("ascii") + body + b"\r\n"


class UploaderWorker:
    def __init__(
        self,
        upload_queue: UploadQueue,
        client: UploadClient,
        poll_seconds: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> None:
        self.upload_queue = upload_queue
        self.client = client
        self.poll_seconds = poll_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    def process_once(self, now: Optional[datetime] = None) -> bool:
        recording = self.upload_queue.claim_next(now)
        if recording is None:
            return False
        try:
            current_size = recording.file_path.stat().st_size
            if current_size != recording.file_size:
                raise UploadError(
                    "Audio file size changed from {} to {} bytes".format(
                        recording.file_size, current_size
                    )
                )
            self.client.upload(recording)
            self.upload_queue.mark_uploaded(recording.recording_id, now)
            LOGGER.info("Upload confirmed for %s", recording.filename)
        except Exception as exc:
            delay = self.upload_queue.mark_failed(
                recording.recording_id,
                str(exc),
                self.retry_base_seconds,
                self.retry_max_seconds,
                now,
            )
            LOGGER.warning(
                "Upload failed for %s; retrying in %d seconds: %s",
                recording.filename,
                delay,
                exc,
            )
        return True

    def run(self, stop_event: Event) -> None:
        LOGGER.info("Uploader worker started")
        while not stop_event.is_set():
            try:
                processed = self.process_once()
            except Exception:
                LOGGER.exception("Upload queue cycle failed")
                try:
                    self.upload_queue.recover_uploading()
                except Exception:
                    LOGGER.exception("Could not recover an interrupted queue claim")
                processed = False
            if not processed:
                stop_event.wait(self.poll_seconds)
        LOGGER.info("Uploader worker stopped")
