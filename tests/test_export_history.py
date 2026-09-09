from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from openaria.bridge.sdk import ExportedSession, SessionInfo, Source, SourceMode
from openaria.bridge.sdk._history import ExportHistory

SOURCE = Source(SourceMode.LAN, "http://192.0.2.1", "device", "Device")
SESSION = SessionInfo("session", "Recording", "2026-09-08T00:00:00Z", 1, 10, "a" * 64)


def test_history_survives_restart_and_transport_change_but_not_content_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state/history.sqlite3"
    output = tmp_path / "output"
    ExportHistory(path).record(
        SOURCE, (SESSION,), (ExportedSession("session", output, 1, 10),)
    )
    history = ExportHistory(path)
    card = dataclasses.replace(SOURCE, mode=SourceMode.CARD, location="/media/card")
    assert history.exported_ids(card, (SESSION,), tmp_path / "other-output") == {
        "session"
    }
    changed = dataclasses.replace(SESSION, manifest_sha256="b" * 64)
    assert history.exported_ids(SOURCE, (changed,), output) == set()
    other = dataclasses.replace(SOURCE, device_id="other-device")
    assert history.exported_ids(other, (SESSION,), output) == set()


def test_history_uses_digest_from_fresh_export_inventory(tmp_path: Path) -> None:
    history = ExportHistory(tmp_path / "history.sqlite3")
    completed = ExportedSession("session", tmp_path, 1, 10, manifest_sha256="b" * 64)
    history.record(SOURCE, (SESSION,), (completed,))
    assert history.exported_ids(SOURCE, (SESSION,), tmp_path) == set()
    changed = dataclasses.replace(SESSION, manifest_sha256="b" * 64)
    assert history.exported_ids(SOURCE, (changed,), tmp_path) == {"session"}


def test_existing_receipts_are_imported_without_hashing_media(tmp_path: Path) -> None:
    directory = tmp_path / "Device/session"
    internal = directory / ".openaria"
    internal.mkdir(parents=True)
    (directory / "recording.mp4").write_bytes(b"media")
    export = {
        "schema": "openaria.bridge-export.v2",
        "session_id": "session",
        "device": {"device_id": "device"},
        "manifest": {"sha256": "a" * 64},
    }
    media = {
        "schema": "openaria.media-export.v1",
        "session_id": "session",
        "source_manifest_sha256": "a" * 64,
        "cleanup": {"status": "complete"},
        "output": {"path": "recording.mp4", "bytes": 5},
    }
    (internal / "export.json").write_text(json.dumps(export))
    (internal / "media.json").write_text(json.dumps(media))
    history = ExportHistory(tmp_path / "state/history.sqlite3")
    assert history.exported_ids(SOURCE, (SESSION,), tmp_path) == {"session"}
    (directory / "recording.mp4").unlink()
    assert ExportHistory(history.path).exported_ids(SOURCE, (SESSION,), tmp_path) == {
        "session"
    }


def test_broken_receipts_and_failed_exports_do_not_create_markers(
    tmp_path: Path,
) -> None:
    internal = tmp_path / "Device/session/.openaria"
    internal.mkdir(parents=True)
    (internal / "export.json").write_text("null")
    history = ExportHistory(tmp_path / "state/history.sqlite3")
    history.record(SOURCE, (SESSION,), ())
    assert history.exported_ids(SOURCE, (SESSION,), tmp_path) == set()
    assert not history.path.exists()
