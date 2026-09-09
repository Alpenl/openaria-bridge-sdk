"""Small persistent export history; listing never hashes media files."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from ._export import _filesystem_component, safe_segment
from .models import ExportedSession, SessionInfo, Source


class ExportHistory:
    def __init__(self, path: Path | None = None) -> None:
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.path = path or state / "openaria-bridge" / "exports.sqlite3"

    def exported_ids(
        self, source: Source, sessions: tuple[SessionInfo, ...], output: Path
    ) -> set[str]:
        known: set[tuple[str, str]] = set()
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db:
                known = set(
                    db.execute(
                        "SELECT session_id, manifest_sha256 FROM exports WHERE device_id = ?",
                        (source.device_id,),
                    )
                )
        exported = set()
        recovered = []
        for session in sessions:
            if not session.manifest_sha256:
                continue
            if (session.session_id, session.manifest_sha256) in known:
                exported.add(session.session_id)
                continue
            safe_segment(session.session_id, "session_id")
            directory = (
                output / _filesystem_component(source.display_name) / session.session_id
            )
            if _has_receipt(directory, source, session):
                exported.add(session.session_id)
                recovered.append(
                    (session.session_id, session.manifest_sha256, str(directory))
                )
        if recovered:
            self._store(source, recovered)
        return exported

    def record(
        self,
        source: Source,
        sessions: tuple[SessionInfo, ...],
        completed: tuple[ExportedSession, ...],
    ) -> None:
        by_id = {session.session_id: session for session in sessions}
        self._store(
            source,
            [
                (
                    item.session_id,
                    item.manifest_sha256 or by_id[item.session_id].manifest_sha256,
                    str(item.path),
                )
                for item in completed
                if item.session_id in by_id
            ],
        )

    def _store(self, source: Source, records: list[tuple[str, str, str]]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS exports (
                device_id TEXT NOT NULL, session_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL, output_path TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (device_id, session_id, manifest_sha256)
            )""")
            db.executemany(
                "INSERT OR REPLACE INTO exports (device_id, session_id, manifest_sha256, output_path) VALUES (?, ?, ?, ?)",
                [(source.device_id, *record) for record in records],
            )


def _has_receipt(directory: Path, source: Source, session: SessionInfo) -> bool:
    try:
        receipts = []
        for name in ("export.json", "media.json"):
            path = directory / ".openaria" / name
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                return False
            value = json.loads(raw)
            if not isinstance(value, dict):
                return False
            receipts.append(value)
        export, media = receipts
        return (
            export.get("schema") == "openaria.bridge-export.v2"
            and export.get("session_id") == session.session_id
            and export.get("device", {}).get("device_id") == source.device_id
            and export.get("manifest", {}).get("sha256") == session.manifest_sha256
            and media.get("schema") == "openaria.media-export.v1"
            and media.get("session_id") == session.session_id
            and media.get("source_manifest_sha256") == session.manifest_sha256
            and media.get("cleanup", {}).get("status") == "complete"
            and media.get("output", {}).get("path") == "recording.mp4"
            and (directory / "recording.mp4").is_file()
            and (directory / "recording.mp4").stat().st_size == media["output"]["bytes"]
            and media["output"]["bytes"] > 0
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
