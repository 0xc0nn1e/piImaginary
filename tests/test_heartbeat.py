import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event

import pytest

from pi_recorder.heartbeat import (
    RUNNING,
    STARTING,
    STOPPING,
    HeartbeatError,
    HeartbeatWorker,
    HttpHeartbeatClient,
)


PAYLOAD = {"schema_version": 1, "device_id": "pi-recorder-01", "status": "running"}


class HeartbeatHandler(BaseHTTPRequestHandler):
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


def run_test_server(status=204):
    server = HTTPServer(("127.0.0.1", 0), HeartbeatHandler)
    server.response_status = status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def make_client(server, api_token="heartbeat-token"):
    return HttpHeartbeatClient(
        "http://127.0.0.1:{}".format(server.server_port),
        "/api/v1/heartbeats",
        api_token,
        "pi-recorder-01",
        5,
    )


class FakeHeartbeatClient:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    def send(self, payload, timeout_seconds=None):
        self.sent.append((payload["status"], timeout_seconds))
        if self.error is not None:
            raise self.error


def test_http_client_posts_json_contract() -> None:
    server, thread = run_test_server()
    try:
        make_client(server).send(PAYLOAD)
        assert server.received_path == "/api/v1/heartbeats"
        assert server.received_headers["Content-Type"] == "application/json"
        assert server.received_headers["X-Device-ID"] == "pi-recorder-01"
        assert server.received_headers["Authorization"] == "Bearer heartbeat-token"
        assert json.loads(server.received_body.decode("utf-8")) == PAYLOAD
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_client_omits_authorization_without_a_token() -> None:
    server, thread = run_test_server()
    try:
        make_client(server, api_token="").send(PAYLOAD)
        assert "Authorization" not in server.received_headers
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_client_rejects_a_non_2xx_response() -> None:
    server, thread = run_test_server(status=503)
    try:
        with pytest.raises(HeartbeatError, match="503"):
            make_client(server).send(PAYLOAD)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_client_rejects_an_invalid_server_url() -> None:
    client = HttpHeartbeatClient("not-a-url", "/api/v1/heartbeats", "", "pi", 5)

    with pytest.raises(HeartbeatError, match="Invalid server URL"):
        client.send(PAYLOAD)


def test_http_client_reports_an_unreachable_server() -> None:
    # Port 1 on the loopback interface refuses connections.
    client = HttpHeartbeatClient("http://127.0.0.1:1", "/api/v1/heartbeats", "", "pi", 1)

    with pytest.raises(HeartbeatError, match="Heartbeat request failed"):
        client.send(PAYLOAD)


def test_worker_sends_starting_then_stopping() -> None:
    client = FakeHeartbeatClient()
    worker = HeartbeatWorker(
        client,
        lambda status: {"status": status},
        interval_seconds=600,
        shutdown_timeout_seconds=5,
    )
    stop_event = Event()
    stop_event.set()

    worker.run(stop_event)

    assert [status for status, _ in client.sent] == [STARTING, STOPPING]
    assert client.sent[-1][1] == 5


def test_worker_sends_running_between_intervals() -> None:
    client = FakeHeartbeatClient()
    worker = HeartbeatWorker(client, lambda status: {"status": status}, interval_seconds=0)
    stop_event = Event()

    def stop_after_two_beats():
        while len([s for s, _ in client.sent if s == RUNNING]) < 2:
            pass
        stop_event.set()

    stopper = threading.Thread(target=stop_after_two_beats, daemon=True)
    stopper.start()
    worker.run(stop_event)
    stopper.join(timeout=5)

    assert client.sent[0][0] == STARTING
    assert client.sent[-1][0] == STOPPING
    assert [s for s, _ in client.sent].count(RUNNING) >= 2


def test_worker_never_raises_when_the_server_fails() -> None:
    client = FakeHeartbeatClient(HeartbeatError("offline"))
    worker = HeartbeatWorker(client, lambda status: {"status": status}, interval_seconds=600)
    stop_event = Event()
    stop_event.set()

    worker.run(stop_event)

    assert not worker.send_once(RUNNING)
    assert len(client.sent) == 3


def test_worker_never_raises_when_the_payload_fails() -> None:
    def broken_builder(status):
        raise RuntimeError("cannot read health")

    worker = HeartbeatWorker(FakeHeartbeatClient(), broken_builder, interval_seconds=600)
    stop_event = Event()
    stop_event.set()

    worker.run(stop_event)

    assert not worker.send_once(RUNNING)
