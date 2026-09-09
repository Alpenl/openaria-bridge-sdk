from __future__ import annotations

import dataclasses
import io
import json
import urllib.error
from email.message import Message

import pytest

from openaria.bridge.sdk import (
    ContractError,
    DeleteError,
    OpenAriaSDK,
    SessionInfo,
    Source,
    SourceMode,
)
from openaria.bridge.sdk import _lan as lan
from openaria.bridge.sdk import client as client_module

SOURCE = Source(
    SourceMode.LAN,
    "http://192.0.2.1:8080/api/v4",
    "device",
    "Device",
    capabilities={"session_deletion": True},
)
SESSION = SessionInfo(
    "01989f6a-2c00-7a1b-8c2d-3e4f50617283",
    "Recording",
    "2026-09-08T00:00:00Z",
    1,
    1,
    "a" * 64,
)


class Response(io.BytesIO):
    status = 200

    def __init__(self, body):
        payload = json.dumps(body).encode()
        super().__init__(payload)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(payload))

    def getcode(self):
        return self.status


def result():
    return {
        "schema": "ylx.session-delete-result.v1",
        "deleted_session_ids": [SESSION.session_id],
        "failed_sessions": [],
    }


def test_remote_deletion_reuses_identical_idempotency_key_after_disconnect(monkeypatch):
    requests = []
    monkeypatch.setenv("OPENARIA_DEVICE_CSRF_TOKEN", "csrf")

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.URLError("response lost")
            return Response(result())

    client = lan.DeviceApiClient(SOURCE.location, token="secret", opener=Opener())
    assert client.delete_sessions(SOURCE, (SESSION,)).deleted_session_ids == (
        SESSION.session_id,
    )
    assert len(requests) == 2
    assert requests[0].data == requests[1].data
    assert requests[0].headers == requests[1].headers
    assert requests[0].headers["Idempotency-key"]
    assert requests[0].headers["X-csrf-token"] == "csrf"
    assert requests[0].headers["Origin"] == "http://192.0.2.1:8080"
    assert requests[0].headers["Authorization"] == "Bearer secret"
    assert requests[0].method == "POST"
    assert requests[0].full_url.endswith("/sessions/delete")
    assert json.loads(requests[0].data)["sessions"] == [
        {"session_id": SESSION.session_id, "manifest_sha256": SESSION.manifest_sha256}
    ]


@pytest.mark.parametrize("code", [401, 403, 409, 423])
def test_remote_deletion_does_not_retry_rejections(code):
    requests = []

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url, code, "rejected", Message(), io.BytesIO(b"rejected")
            )

    with pytest.raises(lan.DiscoveryError, match=str(code)):
        lan.DeviceApiClient(SOURCE.location, opener=Opener()).delete_sessions(
            SOURCE, (SESSION,)
        )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {**result(), "deleted_session_ids": ["other"]},
        {**result(), "deleted_session_ids": []},
        {**result(), "deleted_session_ids": [SESSION.session_id, SESSION.session_id]},
        {
            **result(),
            "failed_sessions": [{"session_id": SESSION.session_id, "error": "failed"}],
        },
    ],
)
def test_remote_deletion_rejects_unaccounted_or_conflicting_results(payload):
    class Opener:
        def open(self, *args, **kwargs):
            return Response(payload)

    with pytest.raises(ContractError):
        lan.DeviceApiClient(SOURCE.location, opener=Opener()).delete_sessions(
            SOURCE, (SESSION,)
        )


def test_sdk_deletes_with_displayed_digest_then_invalidates_cache(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def list_sessions(self, source):
            calls.append("list")
            return (SESSION,) if calls.count("list") == 1 else ()

        def delete_sessions(self, source, sessions):
            calls.append(sessions)
            raise DeleteError("manifest changed")

    monkeypatch.setattr(client_module, "DeviceApiClient", Client)
    sdk = OpenAriaSDK()
    assert sdk.list_sessions(SOURCE) == (SESSION,)
    with pytest.raises(DeleteError):
        sdk.delete_sessions(source=SOURCE, session_ids=[SESSION.session_id])
    assert calls == ["list", (SESSION,)]
    assert sdk.list_sessions(SOURCE) == ()


def test_old_firmware_is_rejected_without_network_request():
    class Opener:
        def open(self, *args, **kwargs):
            pytest.fail("old firmware must not receive a deletion request")

    old = dataclasses.replace(SOURCE, capabilities={"session_deletion": False})
    with pytest.raises(DeleteError, match="升级固件"):
        lan.DeviceApiClient(SOURCE.location, opener=Opener()).delete_sessions(
            old, (SESSION,)
        )


def test_stale_confirmation_cannot_delete_updated_recording(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def list_sessions(self, source):
            return (dataclasses.replace(SESSION, manifest_sha256="b" * 64),)

        def delete_sessions(self, *args):
            pytest.fail("changed content must require a fresh confirmation")

    monkeypatch.setattr(client_module, "DeviceApiClient", Client)
    with pytest.raises(DeleteError, match="重新确认"):
        OpenAriaSDK().delete_sessions(
            source=SOURCE,
            session_ids=[SESSION.session_id],
            expected_manifests={SESSION.session_id: SESSION.manifest_sha256},
        )
