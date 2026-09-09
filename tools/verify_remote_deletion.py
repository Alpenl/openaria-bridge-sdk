"""Exercise Bridge against a firmware gateway using temporary test recordings only."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.firmware_root.resolve()
    sys.path[:0] = [
        str(root / "src"),
        str(root / "tests"),
        str(Path(__file__).resolve().parents[1]),
    ]
    from rp_ylx.api import Principal, SecurityPolicy, create_gateway_server
    from test_capture_coordinator import CaptureCoordinatorTest

    from openaria.bridge.sdk import OpenAriaSDK
    from openaria.bridge.sdk._lan import DeviceApiClient

    fixture = CaptureCoordinatorTest()
    fixture.setUp()
    coordinator = fixture.coordinator()
    server = None
    try:
        first = fixture.seal_one(coordinator, prefix="bridge-delete")
        second = fixture.seal_one(coordinator, prefix="bridge-keep")
        replays = []

        class Provider:
            def __getattr__(self, name):
                return getattr(coordinator, name)

            def delete_sessions(self, command):
                result = coordinator.delete_sessions(command)
                replays.append(result.replayed)
                return result

        server = create_gateway_server(
            "127.0.0.1",
            0,
            Provider(),
            security=SecurityPolicy.customer(
                tokens={
                    "test-token": Principal(
                        "bridge-test",
                        permissions={
                            "getDevice": None,
                            "listSessions": None,
                            "deleteSessions": None,
                        },
                    )
                },
                csrf_token="test-csrf",
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sdk = OpenAriaSDK(
            endpoint=f"http://127.0.0.1:{server.server_port}", token="test-token"
        )
        source = sdk.discover()[0]
        assert source.capabilities["session_deletion"]
        sessions = sdk.list_sessions(source)
        assert {item.session_id for item in sessions} == {first, second}

        class LostResponse:
            lost = False

            def open(self, request, **kwargs):
                response = urllib.request.urlopen(request, **kwargs)
                if request.method == "POST" and not self.lost:
                    self.lost = True
                    response.read()
                    response.close()
                    raise urllib.error.URLError("simulated lost deletion response")
                return response

        client = DeviceApiClient(
            source.location, token="test-token", opener=LostResponse()
        )
        with patch.dict(os.environ, {"OPENARIA_DEVICE_CSRF_TOKEN": "test-csrf"}):
            result = client.delete_sessions(
                source, tuple(item for item in sessions if item.session_id == first)
            )
        assert result.deleted_session_ids == (first,)
        assert replays == [False, True]
        assert not (fixture.mountpoint / "recordings" / first).exists()
        assert (fixture.mountpoint / "recordings" / second).is_dir()
        assert [
            item.session_id for item in sdk.list_sessions(source, refresh=True)
        ] == [second]
        print(
            json.dumps(
                {
                    "capability": True,
                    "deleted": len(result.deleted_session_ids),
                    "remaining": 1,
                    "response_loss_replayed": replays,
                    "storage": "temporary test fixture only",
                }
            )
        )
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        coordinator.close()
        fixture.tearDown()


if __name__ == "__main__":
    main()
