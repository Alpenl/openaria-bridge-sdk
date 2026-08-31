from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from zeroconf import ServiceInfo

import main as legacy_main
import openaria.bridge.sdk._export as export_module
from openaria.bridge.sdk import (
    ContractError,
    ExportError,
    OpenAriaSDK,
    SessionInfo,
    Source,
    SourceMode,
)
from openaria.bridge.sdk._export import artifacts_from_manifest, export_session_tree
from openaria.bridge.sdk._lan import (
    SERVICE_TYPE,
    endpoints_from_service_info,
    normalize_api_base,
)
from openaria.bridge.sdk._media import FINAL_MEDIA_NAME, RenderedMedia

SESSION_ID = "01989f6c-2c00-7a1b-8c2d-3e4f50617283"
DEVICE_ID = "550e8400-e29b-41d4-a716-446655440000"
DEVICE_LABEL = "YLX-30D5872D"


def _is_media_path(relative: str) -> bool:
    return relative.startswith(("video/", "audio/"))


@pytest.fixture(autouse=True)
def _stub_final_media_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    def render(
        session_root: Path,
        manifest_bytes: bytes,
        output: Path,
        progress=None,
    ) -> RenderedMedia:
        payload = b"synthetic-final-mp4"
        output.write_bytes(payload)
        if progress is not None:
            progress("final recording verified")
        return RenderedMedia(
            path=output,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            has_audio=False,
            video_segment_count=1,
            audio_segment_count=0,
            video_start_time_seconds=0,
            audio_start_time_seconds=None,
            audio_offset_seconds=None,
        )

    monkeypatch.setattr(export_module, "render_session_video", render)


def test_card_mode_discovers_mount_and_exports_same_verified_tree(
    tmp_path: Path,
) -> None:
    card = tmp_path / "mounted-card"
    manifest_bytes, payloads, _ = _build_card(card)
    output = tmp_path / "export"
    sdk = OpenAriaSDK(
        mode="card",
        output=output,
        card_search_roots=[tmp_path],
    )

    sources = sdk.discover()
    assert len(sources) == 1
    assert sources[0].mode is SourceMode.CARD
    assert sources[0].card_root == card.resolve()
    assert sources[0].device_label == DEVICE_LABEL

    sessions = sdk.list_sessions(sources[0])
    assert [session.session_id for session in sessions] == [SESSION_ID]
    result = sdk.export(source=sources[0])

    destination = output.resolve() / DEVICE_LABEL / SESSION_ID
    assert result.sessions[0].path == destination
    assert result.sessions[0].media_path == destination / FINAL_MEDIA_NAME
    assert (destination / FINAL_MEDIA_NAME).read_bytes() == b"synthetic-final-mp4"
    source_tree = destination / ".openaria" / "source"
    assert (source_tree / "manifest.json").read_bytes() == manifest_bytes
    for relative, payload in payloads.items():
        if _is_media_path(relative):
            assert not (source_tree / relative).exists()
        else:
            assert (source_tree / relative).read_bytes() == payload
    receipt = json.loads((destination / ".openaria" / "export.json").read_text())
    assert receipt["schema"] == "openaria.bridge-export.v2"
    assert receipt["mode"] == "card"
    assert receipt["session_id"] == SESSION_ID
    media = json.loads((destination / ".openaria" / "media.json").read_text())
    assert media["schema"] == "openaria.media-export.v1"
    assert media["output"]["path"] == FINAL_MEDIA_NAME
    assert media["timeline"]["verdict"] == "aligned"
    assert media["cleanup"]["status"] == "complete"
    assert media["cleanup"]["removed_paths"] == [
        "video/left_00000.mp4",
        "video/right_00000.mp4",
    ]

    repeated = sdk.export(source=sources[0])
    assert repeated.sessions[0].reused is True


def test_modified_final_video_is_never_reused(tmp_path: Path) -> None:
    card = tmp_path / "mounted-card"
    _build_card(card)
    sdk = OpenAriaSDK(mode="card", card=card, output=tmp_path / "export")

    first = sdk.export()
    media_path = first.sessions[0].media_path
    assert media_path is not None
    media_path.write_bytes(b"modified-final-video")

    with pytest.raises(ExportError, match="destination already exists"):
        sdk.export()


def test_card_mode_explicit_path_and_export_override(tmp_path: Path) -> None:
    card = tmp_path / "card"
    _build_card(card)
    output = tmp_path / "chosen-export"
    sdk = OpenAriaSDK(
        mode="card",
        card=card,
        output=tmp_path / "unused-default",
    )

    result = sdk.export(output=output)

    assert result.output_root == output.resolve()
    assert (output / DEVICE_LABEL / SESSION_ID / FINAL_MEDIA_NAME).is_file()


def test_existing_03_source_tree_is_upgraded_and_then_reused(tmp_path: Path) -> None:
    card = tmp_path / "card"
    manifest_bytes, payloads, _ = _build_card(card)
    output = tmp_path / "export"
    legacy = output / DEVICE_LABEL / SESSION_ID
    legacy.mkdir(parents=True)
    (legacy / "manifest.json").write_bytes(manifest_bytes)
    for relative, payload in payloads.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (legacy / ".openaria-export.json").write_text("{}\n", encoding="utf-8")
    sdk = OpenAriaSDK(mode="card", card=card, output=output)

    upgraded = sdk.export()

    assert upgraded.sessions[0].reused is False
    assert (legacy / FINAL_MEDIA_NAME).is_file()
    assert not (legacy / "manifest.json").exists()
    assert not (legacy / "video").exists()
    source_tree = legacy / ".openaria" / "source"
    assert (source_tree / "manifest.json").read_bytes() == manifest_bytes
    for relative, payload in payloads.items():
        if _is_media_path(relative):
            assert not (source_tree / relative).exists()
        else:
            assert (source_tree / relative).read_bytes() == payload

    repeated = sdk.export()
    assert repeated.sessions[0].reused is True


def test_media_render_failure_leaves_no_complete_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = tmp_path / "card"
    _build_card(card)
    output = tmp_path / "export"

    def fail_render(*args: Any, **kwargs: Any) -> RenderedMedia:
        raise ExportError("synthetic render failure")

    monkeypatch.setattr(export_module, "render_session_video", fail_render)
    sdk = OpenAriaSDK(mode="card", card=card, output=output)

    with pytest.raises(ExportError, match="synthetic render failure"):
        sdk.export()

    destination = output / DEVICE_LABEL / SESSION_ID
    assert not destination.exists()
    device_root = output / DEVICE_LABEL
    assert not device_root.exists() or not tuple(device_root.glob("*.part"))


def test_media_cleanup_failure_leaves_no_complete_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = tmp_path / "card"
    _build_card(card)
    output = tmp_path / "export"

    def fail_cleanup(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        raise ExportError("synthetic cleanup failure")

    monkeypatch.setattr(export_module, "_remove_media_inputs", fail_cleanup)
    sdk = OpenAriaSDK(mode="card", card=card, output=output)

    with pytest.raises(ExportError, match="synthetic cleanup failure"):
        sdk.export()

    destination = output / DEVICE_LABEL / SESSION_ID
    assert not destination.exists()


@pytest.mark.parametrize("arguments", ([], ["--version"]))
def test_legacy_main_dispatches_integrated_cli(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import openaria.bridge.sdk.cli as integrated_cli

    received: list[str] = []

    def fake_main(argv: list[str]) -> int:
        received.extend(argv)
        return 17

    monkeypatch.setattr(integrated_cli, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["main.py", *arguments])

    assert legacy_main.main() == 17
    assert received == arguments


def test_card_mode_rejects_output_on_source_card(tmp_path: Path) -> None:
    card = tmp_path / "card"
    _build_card(card)
    output = card / "exports"
    sdk = OpenAriaSDK(mode="card", card=card, output=output)

    with pytest.raises(ExportError, match="outside the source recording card"):
        sdk.export()

    assert not output.exists()


def test_lan_mode_discovers_probes_and_downloads_without_network_mutation_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    card = tmp_path / "source"
    manifest_bytes, payloads, artifact_ids = _build_card(card)
    with _device_api(manifest_bytes, payloads, artifact_ids) as endpoint:
        sdk = OpenAriaSDK(
            mode="lan",
            output=tmp_path / "lan-export",
            discovery_provider=lambda timeout: [endpoint],
        )
        sources = sdk.discover()
        assert len(sources) == 1
        assert sources[0].device_id == DEVICE_ID
        assert sources[0].capabilities["artifact_download"] is True
        assert "network_mutation" not in sources[0].capabilities

        sessions = sdk.list_sessions(sources[0])
        assert sessions[0].exportable is True
        result = sdk.export(source=sources[0])

    destination = result.sessions[0].path
    assert (
        destination == (tmp_path / "lan-export").resolve() / DEVICE_LABEL / SESSION_ID
    )
    source_tree = destination / ".openaria" / "source"
    assert (destination / FINAL_MEDIA_NAME).is_file()
    assert (source_tree / "manifest.json").read_bytes() == manifest_bytes
    for relative, payload in payloads.items():
        if _is_media_path(relative):
            assert not (source_tree / relative).exists()
        else:
            assert (source_tree / relative).read_bytes() == payload
    receipt = json.loads((destination / ".openaria" / "export.json").read_text())
    assert receipt["mode"] == "lan"
    assert receipt["source"].endswith("/api/v4")


def test_lan_digest_mismatch_leaves_no_partial_session(tmp_path: Path) -> None:
    card = tmp_path / "source"
    manifest_bytes, payloads, artifact_ids = _build_card(card)
    with _device_api(
        manifest_bytes,
        payloads,
        artifact_ids,
        corrupt_first_artifact=True,
    ) as endpoint:
        output = tmp_path / "bad-export"
        sdk = OpenAriaSDK(mode="lan", endpoint=endpoint, output=output)
        source = sdk.discover()[0]
        with pytest.raises(ExportError, match="SHA-256"):
            sdk.export(source=source)

    assert not (output / DEVICE_LABEL / SESSION_ID).exists()
    device_root = output / DEVICE_LABEL
    assert not device_root.exists() or not tuple(device_root.glob("*.part"))


def test_gateway_unusable_session_is_visible_but_not_exported(tmp_path: Path) -> None:
    card = tmp_path / "source"
    manifest_bytes, payloads, artifact_ids = _build_card(card)
    with _device_api(
        manifest_bytes,
        payloads,
        artifact_ids,
        verification_verdict="unusable",
    ) as endpoint:
        sdk = OpenAriaSDK(mode="lan", endpoint=endpoint, output=tmp_path / "output")
        source = sdk.discover()[0]
        sessions = sdk.list_sessions(source)
        assert sessions[0].exportable is False
        assert "unusable" in (sessions[0].unavailable_reason or "")
        result = sdk.export(source=source)

        with pytest.raises(ExportError, match="requested session.*not exportable"):
            sdk.export(source=source, session_ids=[SESSION_ID])

    assert result.sessions == ()
    assert result.unavailable_sessions == sessions


def test_lan_pagination_rejects_catalog_revision_change(tmp_path: Path) -> None:
    card = tmp_path / "source"
    manifest_bytes, payloads, artifact_ids = _build_card(card)
    with _device_api(
        manifest_bytes,
        payloads,
        artifact_ids,
        change_catalog_on_next_page=True,
    ) as endpoint:
        sdk = OpenAriaSDK(mode="lan", endpoint=endpoint)
        source = sdk.discover()[0]
        with pytest.raises(ContractError, match="catalog_revision changed"):
            sdk.list_sessions(source)


@pytest.mark.parametrize("path", ("../outside.mp4", "folder\\outside.mp4"))
def test_manifest_path_traversal_is_rejected(path: str) -> None:
    manifest = {
        "schema": "ylx.device-session.v2",
        "session_id": SESSION_ID,
        "artifact": {
            "artifact_id": "a" * 64,
            "role": "video.left",
            "path": path,
            "media_type": "video/mp4",
            "bytes": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        },
    }
    with pytest.raises(ContractError, match="escapes"):
        artifacts_from_manifest(json.dumps(manifest).encode(), SESSION_ID)


def test_manifest_artifact_cannot_overwrite_source_manifest(tmp_path: Path) -> None:
    payload = b"receipt collision"
    manifest = {
        "schema": "ylx.device-session.v2",
        "session_id": SESSION_ID,
        "artifact": {
            "artifact_id": hashlib.sha256(payload).hexdigest(),
            "role": "metadata",
            "path": "MANIFEST.JSON",
            "media_type": "application/json",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    raw = json.dumps(manifest).encode()
    source = Source(
        mode=SourceMode.LAN,
        location="http://127.0.0.1:8080/api/v4",
        device_id=DEVICE_ID,
        device_label=DEVICE_LABEL,
    )
    session = SessionInfo(
        session_id=SESSION_ID,
        display_name="collision",
        started_at="2026-08-31T00:00:00Z",
        duration_seconds=1,
        total_bytes=len(payload),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )

    with pytest.raises(ContractError, match="reserved by the export format"):
        export_session_tree(
            source=source,
            session=session,
            output_root=tmp_path,
            manifest_name="manifest.json",
            manifest_bytes=raw,
            artifact_writer=lambda artifact, destination: destination.write_bytes(
                payload
            ),
        )


def test_manual_endpoint_defaults_to_http_8080_and_v4_base() -> None:
    assert normalize_api_base("192.168.110.36") == "http://192.168.110.36:8080/api/v4"
    assert (
        normalize_api_base("http://192.168.110.36:8080/api/v4/")
        == "http://192.168.110.36:8080/api/v4"
    )
    assert (
        normalize_api_base("http://[fe80::1%25eth0]:8080")
        == "http://[fe80::1%25eth0]:8080/api/v4"
    )
    with pytest.raises(ValueError, match="path"):
        normalize_api_base("http://192.168.110.36:8080/admin")


def test_mdns_service_info_preserves_advertised_port() -> None:
    info = ServiceInfo(
        SERVICE_TYPE,
        f"OpenAria.{SERVICE_TYPE}",
        addresses=[socket.inet_aton("192.0.2.24")],
        port=18080,
        properties={},
        server="openaria.local.",
    )
    assert endpoints_from_service_info(info) == ("http://192.0.2.24:18080",)


def _build_card(card: Path) -> tuple[bytes, dict[str, bytes], dict[str, str]]:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "vendor/ylx-contracts/fixtures/valid/ylx-device-session-v2.audio-not-recorded.json"
    )
    manifest = json.loads(fixture.read_text(encoding="utf-8"))
    payloads = {
        "video/left_00000.mp4": b"left-video",
        "video/right_00000.mp4": b"right-video",
        "imu/imu.jsonl": b'{"x":1}\n',
        "imu/frames.jsonl": b'{"frame":0}\n',
        "logs/capture.log": b"capture complete\n",
    }
    artifact_ids: dict[str, str] = {}

    def patch_artifacts(value: Any) -> None:
        if isinstance(value, dict):
            descriptor_fields = {
                "artifact_id",
                "role",
                "path",
                "media_type",
                "bytes",
                "sha256",
            }
            if descriptor_fields.issubset(value):
                path = value["path"]
                payload = payloads[path]
                value["bytes"] = len(payload)
                value["sha256"] = hashlib.sha256(payload).hexdigest()
                value["artifact_id"] = value["sha256"]
                artifact_ids[path] = value["artifact_id"]
                return
            for child in value.values():
                patch_artifacts(child)
        elif isinstance(value, list):
            for child in value:
                patch_artifacts(child)

    patch_artifacts(manifest)
    session_directory = card / "recordings" / SESSION_ID
    session_directory.mkdir(parents=True)
    for relative, payload in payloads.items():
        path = session_directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode()
    (session_directory / "manifest.json").write_bytes(manifest_bytes)
    (card / "device-id").write_text("30D5872D\n", encoding="utf-8")
    return manifest_bytes, payloads, artifact_ids


@contextmanager
def _device_api(
    manifest_bytes: bytes,
    payloads: dict[str, bytes],
    artifact_ids: dict[str, str],
    *,
    corrupt_first_artifact: bool = False,
    verification_verdict: str = "usable",
    change_catalog_on_next_page: bool = False,
) -> Iterator[str]:
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    id_to_payload = {artifact_ids[path]: payload for path, payload in payloads.items()}
    artifact_media_types: dict[str, str] = {}

    def collect_media_types(value: Any) -> None:
        if isinstance(value, dict):
            if {"artifact_id", "media_type"}.issubset(value):
                artifact_media_types[value["artifact_id"]] = value["media_type"]
                return
            for child in value.values():
                collect_media_types(child)
        elif isinstance(value, list):
            for child in value:
                collect_media_types(child)

    collect_media_types(json.loads(manifest_bytes))
    if corrupt_first_artifact:
        first = next(iter(id_to_payload))
        original = id_to_payload[first]
        id_to_payload[first] = b"x" * len(original)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/api/v4/device":
                self._json(
                    {
                        "schema": "ylx.device.v4",
                        "api_version": "4.0",
                        "security_profile": "lab",
                        "device": {
                            "device_id": DEVICE_ID,
                            "device_label": DEVICE_LABEL,
                        },
                        "capabilities": {
                            "session_list": True,
                            "session_detail": True,
                            "artifact_download": True,
                        },
                    }
                )
                return
            if parsed.path == "/api/v4/sessions":
                second_page = "cursor" in urllib.parse.parse_qs(parsed.query)
                self._json(
                    {
                        "schema": "ylx.session-list.v3",
                        "catalog_revision": f"sha256:{('d' if second_page and change_catalog_on_next_page else 'e') * 64}",
                        "items": []
                        if second_page
                        else [
                            {
                                "session_id": SESSION_ID,
                                "producer_outcome": "sealed",
                                "take_id": "01989f6c-f000-7c3d-ae4f-5061728394a5",
                                "take_sequence": 1,
                                "continuation_of": None,
                                "display_name": "test session",
                                "device": {
                                    "device_id": DEVICE_ID,
                                    "device_label": DEVICE_LABEL,
                                },
                                "started_at": "2026-08-08T10:31:00+08:00",
                                "ended_at": "2026-08-08T10:31:30+08:00",
                                "duration_seconds": 30,
                                "total_bytes": sum(map(len, payloads.values())),
                                "verification": {
                                    "actor": "gateway",
                                    "validator": {
                                        "name": "rp-ylx-device-session-v2",
                                        "version": "1",
                                        "build_sha256": "f" * 64,
                                    },
                                    "manifest_sha256": manifest_sha256,
                                    "verified_at": "2026-08-08T10:31:32+08:00",
                                    "verdict": verification_verdict,
                                    "diagnostics": [],
                                },
                            }
                        ],
                        "diagnostics": [],
                        "next_cursor": (
                            "next-page"
                            if change_catalog_on_next_page and not second_page
                            else None
                        ),
                    }
                )
                return
            if parsed.path == f"/api/v4/sessions/{SESSION_ID}":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(manifest_bytes)))
                self.send_header("YLX-Manifest-SHA256", manifest_sha256)
                self.send_header("ETag", f'"{manifest_sha256}"')
                self.end_headers()
                self.wfile.write(manifest_bytes)
                return
            prefix = f"/api/v4/sessions/{SESSION_ID}/artifacts/"
            if parsed.path.startswith(prefix):
                artifact_id = urllib.parse.unquote(parsed.path.removeprefix(prefix))
                payload = id_to_payload.get(artifact_id)
                if payload is not None:
                    expected_path = next(
                        path
                        for path, candidate in artifact_ids.items()
                        if candidate == artifact_id
                    )
                    expected_sha = hashlib.sha256(payloads[expected_path]).hexdigest()
                    self.send_response(200)
                    self.send_header("Content-Type", artifact_media_types[artifact_id])
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("ETag", f'"{expected_sha}"')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            self.send_error(404)

        def _json(self, value: Any) -> None:
            raw = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
