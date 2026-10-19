from __future__ import annotations

import hashlib
import io
import threading
import time
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openaria.bridge.sdk import _lan
from openaria.bridge.sdk._export import ArtifactDescriptor
from openaria.bridge.sdk.errors import ContractError, DiscoveryError


class Response(io.BytesIO):
    def __init__(self, status=200, headers=None):
        super().__init__(b'{"ok":true}')
        self.status = status
        self.headers = headers or Message()

    def getcode(self):
        return self.status


class Opener:
    def __init__(self, replies):
        self.replies = replies
        self.calls = 0

    def open(self, *args, **kwargs):
        reply = self.replies[self.calls]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply


def error(headers=(), code=503):
    message = Message()
    for value in headers:
        message.add_header("Retry-After", value)
    body = io.BytesIO(b"temporarily busy")
    return urllib.error.HTTPError(
        "http://127.0.0.1/api/v4/device", code, "busy", message, body
    ), body


def read(opener):
    return _lan.DeviceApiClient("http://127.0.0.1", opener=opener)._json_get(
        "device", label="test"
    )


@pytest.mark.parametrize(
    "value,expected", [("1", 1.0), ("0", 0.15), ("0002", 2.0), (" 1 ", 1.0)]
)
def test_integer_retry_after_delays_next_request(monkeypatch, value, expected):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    first, body = error([value])
    opener = Opener([first, Response()])
    assert read(opener) == {"ok": True}
    assert waits == [expected] and opener.calls == 2 and body.closed


def test_http_date_retry_after_uses_response_time(monkeypatch):
    now = 1792400000.0
    monkeypatch.setattr(_lan.time, "time", lambda: now)
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    date = format_datetime(datetime.fromtimestamp(now + 2, UTC), usegmt=True)
    first, _ = error([date])
    assert read(Opener([first, Response()])) == {"ok": True}
    assert waits == [2.0]


@pytest.mark.parametrize(
    "value", [None, "", "-1", "1.5", "NaN", "inf", "nonsense", "１"]
)
def test_missing_or_invalid_delay_keeps_existing_backoff(monkeypatch, value):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    first, _ = error([] if value is None else [value])
    assert read(Opener([first, Response()])) == {"ok": True}
    assert waits == [0.15]


def test_duplicate_retry_after_fails_without_retry_and_closes_error(monkeypatch):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    first, body = error(["1", "2"])
    opener = Opener([first, Response()])
    with pytest.raises(ContractError, match="repeats Retry-After"):
        read(opener)
    assert opener.calls == 1 and waits == [] and body.closed


@pytest.mark.parametrize(
    "value", ["31", "9" * 5000], ids=["over_budget", "huge_decimal"]
)
def test_excessive_server_delay_never_retries_early(monkeypatch, value):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    first, body = error([value])
    opener = Opener([first, Response()])
    with pytest.raises(DiscoveryError):
        read(opener)
    assert opener.calls == 1 and waits == [] and body.closed


def test_cumulative_wait_budget_is_bounded(monkeypatch):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    first, _ = error(["20"])
    second, _ = error(["20"])
    opener = Opener([first, second, Response()])
    with pytest.raises(DiscoveryError):
        read(opener)
    assert waits == [20.0] and opener.calls == 2


def test_retryable_status_returned_by_custom_opener_preserves_header(monkeypatch):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    headers = Message()
    headers["Retry-After"] = "1"
    first = Response(503, headers)
    assert read(Opener([first, Response()])) == {"ok": True}
    assert waits == [1.0] and first.closed


def test_attempt_limit_is_unchanged(monkeypatch):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    replies = [error(["1"])[0] for _ in range(3)]
    opener = Opener(replies)
    with pytest.raises(DiscoveryError):
        read(opener)
    assert opener.calls == 3 and waits == [1.0, 1.0]


def test_nonretryable_status_does_not_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(_lan.time, "sleep", waits.append)
    first, _ = error(["1"], 404)
    opener = Opener([first, Response()])
    with pytest.raises(DiscoveryError):
        read(opener)
    assert opener.calls == 1 and waits == []


def test_real_server_recovers_after_requested_one_second():
    times = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            times.append(time.monotonic())
            status = 503 if times[-1] - times[0] < 0.75 else 200
            payload = b"temporarily busy" if status == 503 else b'{"ok":true}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            if status == 503:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = _lan.DeviceApiClient(
            f"http://127.0.0.1:{server.server_port}"
        )._json_get("device", label="test")
        assert result == {"ok": True}
        assert len(times) == 2 and times[1] - times[0] >= 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_interrupted_download_preserves_offset_across_retry_after(tmp_path):
    payload = b"0123456789abcdef" * (192 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((time.monotonic(), dict(self.headers)))
            attempt = len(requests)
            if attempt == 2:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start = 0 if attempt == 1 else 1024 * 1024
            self.send_response(200 if attempt == 1 else 206)
            self.send_header("Content-Length", str(len(payload) - start))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("ETag", f'"{digest}"')
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                )
            self.end_headers()
            self.wfile.write(
                payload[: 1024 * 1024] if attempt == 1 else payload[start:]
            )
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        artifact = ArtifactDescriptor(
            digest,
            "diagnostic.synthetic",
            "synthetic.bin",
            "application/octet-stream",
            len(payload),
            digest,
        )
        target = tmp_path / "synthetic.bin"
        _lan.DeviceApiClient(
            f"http://127.0.0.1:{server.server_port}"
        )._download_artifact("session", artifact, target)
        assert target.read_bytes() == payload
        assert len(requests) == 3 and requests[2][0] - requests[1][0] >= 1
        assert requests[1][1]["Range"] == requests[2][1]["Range"] == "bytes=1048576-"
        assert requests[2][1]["If-Range"] == f'"{digest}"'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
