import http.client
import json
import logging
from threading import Event
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse


LOGGER = logging.getLogger(__name__)

STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"

# Shutdown must fit inside the systemd stop timeout alongside the recorder and uploader.
SHUTDOWN_TIMEOUT_SECONDS = 5


class HeartbeatError(Exception):
    pass


class HttpHeartbeatClient:
    def __init__(
        self,
        server_url: str,
        endpoint: str,
        api_token: str,
        device_id: str,
        timeout_seconds: int,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.endpoint = endpoint
        self.api_token = api_token
        self.device_id = device_id
        self.timeout_seconds = timeout_seconds

    def send(self, payload: Dict[str, Any], timeout_seconds: Optional[int] = None) -> None:
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HeartbeatError("Invalid server URL")

        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HeartbeatError("Heartbeat payload is not serializable: {}".format(exc)) from exc

        connection_class = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
        base_path = parsed.path.rstrip("/")
        request_path = (base_path + self.endpoint) or "/"
        try:
            connection.putrequest("POST", request_path)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
            connection.putheader("Accept", "application/json")
            connection.putheader("X-Device-ID", self.device_id)
            if self.api_token:
                connection.putheader("Authorization", "Bearer {}".format(self.api_token))
            connection.endheaders()
            connection.send(body)

            response = connection.getresponse()
            response_text = response.read(4096).decode("utf-8", errors="replace").strip()
            if not 200 <= response.status < 300:
                detail = response_text[:500] or response.reason
                raise HeartbeatError("Server returned HTTP {}: {}".format(response.status, detail))
        except (OSError, http.client.HTTPException) as exc:
            raise HeartbeatError("Heartbeat request failed: {}".format(exc)) from exc
        finally:
            connection.close()


class HeartbeatWorker:
    def __init__(
        self,
        client: "HttpHeartbeatClient",
        payload_builder: Callable[[str], Dict[str, Any]],
        interval_seconds: int,
        shutdown_timeout_seconds: int = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self.client = client
        self.payload_builder = payload_builder
        self.interval_seconds = interval_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds

    def send_once(self, status: str, timeout_seconds: Optional[int] = None) -> bool:
        """Send one heartbeat. Reporting failures must never reach the caller."""
        try:
            self.client.send(self.payload_builder(status), timeout_seconds)
        except Exception as exc:
            LOGGER.warning("Heartbeat (%s) failed; recording is unaffected: %s", status, exc)
            return False
        LOGGER.debug("Heartbeat (%s) accepted", status)
        return True

    def run(self, stop_event: Event) -> None:
        LOGGER.info("Heartbeat worker started")
        self.send_once(STARTING)
        while not stop_event.wait(self.interval_seconds):
            self.send_once(RUNNING)
        # A final heartbeat lets the server tell a clean shutdown from a crash.
        self.send_once(STOPPING, self.shutdown_timeout_seconds)
        LOGGER.info("Heartbeat worker stopped")
