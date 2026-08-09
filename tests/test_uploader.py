import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pi_recorder.models import FAILED, UPLOADED
from pi_recorder.queue import UploadQueue
from pi_recorder.uploader import HttpUploadClient, UploadError, UploaderWorker


UTC = timezone.utc


class FakeUploadClient:
    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = []

    def upload(self, recording) -> None:
        self.calls.append(recording.recording_id)
        if self.error is not None:
            raise self.error


def make_worker(tmp_path, metadata_factory, client):
    upload_queue = UploadQueue(tmp_path / "recorder.db")
    upload_queue.initialize()
    recording = metadata_factory()
    upload_queue.add(recording)
    worker = UploaderWorker(upload_queue, client, 1, 30, 3600)
    return worker, upload_queue, recording


def test_upload_success_marks_recording_uploaded(tmp_path, metadata_factory) -> None:
    client = FakeUploadClient()
    worker, upload_queue, recording = make_worker(tmp_path, metadata_factory, client)

    assert worker.process_once(datetime(2026, 8, 9, tzinfo=UTC))

    stored = upload_queue.get(recording.recording_id)
    assert stored is not None
    assert stored.upload_status == UPLOADED
    assert client.calls == [recording.recording_id]


def test_upload_failure_is_persisted_with_backoff(tmp_path, metadata_factory) -> None:
    client = FakeUploadClient(UploadError("server unavailable"))
    worker, upload_queue, recording = make_worker(tmp_path, metadata_factory, client)
    now = datetime(2026, 8, 9, tzinfo=UTC)

    assert worker.process_once(now)

    stored = upload_queue.get(recording.recording_id)
    assert stored is not None
    assert stored.upload_status == FAILED
    assert stored.retry_count == 1
    assert stored.next_attempt_at == (now + timedelta(seconds=30)).isoformat(timespec="microseconds")


def test_upload_retries_when_backoff_expires(tmp_path, metadata_factory) -> None:
    client = FakeUploadClient(UploadError("offline"))
    worker, upload_queue, recording = make_worker(tmp_path, metadata_factory, client)
    now = datetime(2026, 8, 9, tzinfo=UTC)
    worker.process_once(now)

    client.error = None
    assert not worker.process_once(now + timedelta(seconds=29))
    assert worker.process_once(now + timedelta(seconds=30))
    assert upload_queue.get(recording.recording_id).upload_status == UPLOADED


class RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers["Content-Length"])
        self.server.received_path = self.path
        self.server.received_headers = self.headers
        self.server.received_body = self.rfile.read(content_length)
        self.send_response(self.server.response_status)
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')

    def log_message(self, format, *args):
        return


def run_test_server(status=201):
    server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
    server.response_status = status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_http_client_streams_multipart_contract(metadata_factory) -> None:
    recording = metadata_factory()
    server, thread = run_test_server()
    client = HttpUploadClient(
        "http://127.0.0.1:{}".format(server.server_port),
        "/api/v1/recordings",
        "test-token",
        2,
    )
    try:
        client.upload(recording)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert server.received_path == "/api/v1/recordings"
    assert server.received_headers["Idempotency-Key"] == recording.recording_id
    assert server.received_headers["Authorization"] == "Bearer test-token"
    assert b'name="metadata"' in server.received_body
    assert b'name="audio"' in server.received_body
    assert recording.checksum_sha256.encode("ascii") in server.received_body


def test_http_client_rejects_server_failure(metadata_factory) -> None:
    recording = metadata_factory()
    server, thread = run_test_server(status=503)
    client = HttpUploadClient(
        "http://127.0.0.1:{}".format(server.server_port),
        "/api/v1/recordings",
        "",
        2,
    )
    try:
        with pytest.raises(UploadError, match="HTTP 503"):
            client.upload(recording)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
