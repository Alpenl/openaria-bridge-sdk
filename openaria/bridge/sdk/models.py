"""Small immutable values returned by the integrated SDK."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path


class SourceMode(StrEnum):
    """How recording bytes reach this computer."""

    LAN = "lan"
    CARD = "card"


@dataclasses.dataclass(frozen=True)
class Source:
    """One probed Device API or one mounted recording card."""

    mode: SourceMode
    location: str
    device_id: str
    device_label: str
    api_base: str | None = None
    card_root: Path | None = None
    capabilities: Mapping[str, bool] = dataclasses.field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.device_label or self.device_id or self.location


@dataclasses.dataclass(frozen=True)
class SessionInfo:
    """A sealed session advertised by a selected source."""

    session_id: str
    display_name: str
    started_at: str
    duration_seconds: float
    total_bytes: int
    manifest_sha256: str
    exportable: bool = True
    unavailable_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ExportedSession:
    """Result of one verified source export and synchronized video render."""

    session_id: str
    path: Path
    artifact_count: int
    total_bytes: int
    reused: bool = False
    # ``path`` remains the session directory for 0.3 callers.
    media_path: Path | None = None
    media_bytes: int = 0


@dataclasses.dataclass(frozen=True)
class ExportResult:
    """Complete result of one high-level SDK export call."""

    source: Source
    output_root: Path
    sessions: tuple[ExportedSession, ...]
    unavailable_sessions: tuple[SessionInfo, ...] = ()

    @property
    def exported_count(self) -> int:
        return len(self.sessions)

    @property
    def total_bytes(self) -> int:
        return sum(session.total_bytes for session in self.sessions)
