"""Mounted recording-card discovery and read-only export adapter."""

from __future__ import annotations

import dataclasses
import os
import string
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import main as legacy

from ._export import ArtifactDescriptor, export_session_tree
from .errors import ContractError, DiscoveryError, ExportError
from .models import ExportedSession, SessionInfo, Source, SourceMode


@dataclasses.dataclass(frozen=True)
class CardInventory:
    source: Source
    sessions: tuple[Any, ...]
    session_infos: tuple[SessionInfo, ...]


def discover_card_inventories(
    *,
    card: Path | None = None,
    search_roots: Iterable[Path] | None = None,
) -> tuple[CardInventory, ...]:
    if card is not None:
        root = _resolved_directory(card, "recording card")
        try:
            return (_read_inventory(root),)
        except (legacy.PipelineError, OSError, ContractError) as error:
            raise DiscoveryError(
                f"{root} is not a usable Open Aria card: {error}"
            ) from error

    roots = tuple(search_roots) if search_roots is not None else _system_mount_roots()
    inventories: list[CardInventory] = []
    seen: set[Path] = set()
    for candidate in _expand_candidates(roots):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen or (os.name != "nt" and resolved == Path("/")):
            continue
        seen.add(resolved)
        try:
            inventories.append(_read_inventory(resolved))
        except (legacy.PipelineError, OSError, ContractError):
            continue
    if not inventories:
        raise DiscoveryError(
            "no mounted Open Aria recording card found; insert the card or pass --card PATH"
        )
    by_location = {inventory.source.location: inventory for inventory in inventories}
    return tuple(
        sorted(by_location.values(), key=lambda inventory: inventory.source.location)
    )


def export_card_session(
    inventory: CardInventory,
    session_info: SessionInfo,
    output_root: Path,
    progress: Callable[[str], None] | None = None,
) -> ExportedSession:
    session = next(
        (
            candidate
            for candidate in inventory.sessions
            if candidate.session_id == session_info.session_id
        ),
        None,
    )
    if session is None:
        raise ExportError(f"card session disappeared: {session_info.session_id}")
    try:
        legacy.verify(session)
        manifest_bytes = legacy._read_regular_file(
            session.directory,
            Path(session.source_manifest_name),
            session.source_manifest_name,
        )
    except legacy.PipelineError as error:
        raise ExportError(f"card verification failed: {error}") from error

    source_artifacts = {
        artifact.display_path: artifact for artifact in session.artifacts
    }

    def write_artifact(artifact: ArtifactDescriptor, destination: Path) -> None:
        declared = source_artifacts.get(artifact.path)
        if declared is None:
            raise ExportError(f"card inventory omitted {artifact.path}")
        if (
            declared.size_bytes != artifact.size_bytes
            or declared.sha256 != artifact.sha256
        ):
            raise ExportError(f"card inventory changed for {artifact.path}")
        try:
            legacy._copy_regular_file(
                session.directory,
                Path(artifact.path),
                destination,
                artifact.path,
            )
        except legacy.PipelineError as error:
            raise ExportError(f"copying {artifact.path} failed: {error}") from error

    return export_session_tree(
        source=inventory.source,
        session=session_info,
        output_root=output_root,
        manifest_name=session.source_manifest_name,
        manifest_bytes=manifest_bytes,
        artifact_writer=write_artifact,
        progress=progress,
    )


def _read_inventory(root: Path) -> CardInventory:
    recordings = legacy.find_recordings_dir(root)
    sessions = tuple(legacy.read_sessions(recordings, allow_unsigned=True))
    marker = legacy.device_id_of(root)
    first = sessions[0] if sessions else None
    device = (
        first.device if first is not None and isinstance(first.device, dict) else {}
    )
    device_id = str(device.get("device_id") or marker or root.name)
    device_label = str(device.get("device_label") or marker or root.name)
    if not device_id or not device_label:
        raise ContractError("recording card has no usable device identity")
    source = Source(
        mode=SourceMode.CARD,
        location=str(root),
        card_root=root,
        device_id=device_id,
        device_label=device_label,
        capabilities={
            "session_list": True,
            "session_detail": True,
            "artifact_download": True,
        },
    )
    infos = tuple(
        SessionInfo(
            session_id=session.session_id,
            display_name=session.name,
            started_at=session.captured_at,
            duration_seconds=session.duration_seconds,
            total_bytes=sum(artifact.size_bytes for artifact in session.artifacts),
            manifest_sha256=session.source_manifest_sha256,
        )
        for session in sessions
    )
    return CardInventory(source=source, sessions=sessions, session_infos=infos)


def _system_mount_roots() -> tuple[Path, ...]:
    roots: set[Path] = set()
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            candidate = Path(f"{letter}:\\")
            if candidate.exists():
                roots.add(candidate)
    elif sys.platform == "darwin":
        volumes = Path("/Volumes")
        if volumes.is_dir():
            roots.update(path for path in volumes.iterdir() if path.is_dir())
    else:
        mountinfo = Path("/proc/self/mountinfo")
        try:
            lines = mountinfo.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        pseudo_filesystems = {
            "autofs",
            "cgroup",
            "cgroup2",
            "debugfs",
            "devpts",
            "devtmpfs",
            "fusectl",
            "mqueue",
            "proc",
            "pstore",
            "securityfs",
            "sysfs",
            "tmpfs",
            "tracefs",
        }
        for line in lines:
            fields = line.split()
            try:
                separator = fields.index("-")
                mountpoint = _decode_mount_path(fields[4])
                filesystem = fields[separator + 1]
            except (ValueError, IndexError):
                continue
            if filesystem not in pseudo_filesystems:
                roots.add(Path(mountpoint))
        for common in (Path("/media"), Path("/run/media"), Path("/mnt")):
            if common.is_dir():
                roots.add(common)
    return tuple(sorted(roots, key=str))


def _expand_candidates(roots: Iterable[Path]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for root in roots:
        candidates.append(root)
        try:
            children = tuple(path for path in root.iterdir() if path.is_dir())
        except OSError:
            continue
        candidates.extend(children)
        if root in {Path("/media"), Path("/run/media")}:
            for child in children:
                try:
                    candidates.extend(path for path in child.iterdir() if path.is_dir())
                except OSError:
                    continue
    return tuple(candidates)


def _resolved_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise DiscoveryError(f"{label} does not exist: {path}") from error
    if not resolved.is_dir():
        raise DiscoveryError(f"{label} is not a directory: {resolved}")
    return resolved


def _decode_mount_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )
