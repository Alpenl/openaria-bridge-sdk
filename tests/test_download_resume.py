import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openaria.bridge.sdk import _lan
from openaria.bridge.sdk._export import ArtifactDescriptor
from openaria.bridge.sdk.errors import ExportError


@contextmanager
def interrupted_server(mode):
    payload = b"0123456789abcdef" * (192 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(dict(self.headers))
            offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
            resumed = len(requests) > 1 and mode != "ignore"
            start = offset if resumed else 0
            self.send_response(206 if resumed else 200)
            self.send_header("Content-Length", str(len(payload) - start))
            self.send_header("Content-Type", "video/mp4")
            self.send_header("ETag", f'"{digest}"')
            if resumed:
                self.send_header(
                    "Content-Range",
                    f"bytes {start + (mode == 'bad_range')}-{len(payload) - 1}/{len(payload)}",
                )
            self.end_headers()
            try:
                self.wfile.write(
                    payload[: 1024 * 1024] if len(requests) == 1 else payload[start:]
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payload, digest, requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("mode", ["resume", "ignore", "bad_range"])
def test_interrupted_download_resumes_or_safely_restarts(tmp_path, mode):
    with interrupted_server(mode) as (endpoint, payload, digest, requests):
        target = tmp_path / "video.mp4"
        artifact = ArtifactDescriptor(
            digest, "video.left", "video/left.mp4", "video/mp4", len(payload), digest
        )
        if mode == "bad_range":
            with pytest.raises(ExportError, match="resume range"):
                _lan.DeviceApiClient(endpoint)._download_artifact(
                    "session", artifact, target
                )
            assert not target.exists()
        else:
            _lan.DeviceApiClient(endpoint)._download_artifact(
                "session", artifact, target
            )
            assert target.read_bytes() == payload
        assert len(requests) == 2
        assert requests[1]["Range"] == "bytes=1048576-"
        assert requests[1]["If-Range"] == f'"{digest}"'


def test_artifact_has_separate_timeout_budget(tmp_path, monkeypatch):
    with interrupted_server("resume") as (endpoint, payload, digest, _):
        client = _lan.DeviceApiClient(endpoint, timeout=0.01)
        original = client._opener.open
        timeouts = []

        def opened(request, *, timeout):
            timeouts.append(timeout)
            return original(request, timeout=timeout)

        monkeypatch.setattr(client._opener, "open", opened)
        artifact = ArtifactDescriptor(
            digest, "video.left", "video/left.mp4", "video/mp4", len(payload), digest
        )
        client._download_artifact("session", artifact, tmp_path / "video")
        assert timeouts == [_lan.ARTIFACT_TIMEOUT, _lan.ARTIFACT_TIMEOUT]
        assert client.timeout == 0.01
