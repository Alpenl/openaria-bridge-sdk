"""Shared atomic exporter for LAN and card sources."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._json import load_json
from .errors import ContractError, ExportError
from .models import ExportedSession, SessionInfo, Source

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
EXPORT_RECEIPT = ".openaria-export.json"
COPY_CHUNK_BYTES = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class ArtifactDescriptor:
    artifact_id: str
    role: str
    path: str
    media_type: str
    size_bytes: int
    sha256: str

    @property
    def relative_path(self) -> Path:
        return safe_relative_path(self.path)


def safe_relative_path(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ContractError(f"artifact path escapes the session directory: {value!r}")
    return path


def safe_segment(value: str, label: str) -> str:
    if not SAFE_SEGMENT_RE.fullmatch(value):
        raise ContractError(f"{label} is not a safe path segment: {value!r}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def artifacts_from_manifest(
    raw: bytes, expected_session_id: str
) -> tuple[ArtifactDescriptor, ...]:
    manifest = load_json(raw, "Device Session manifest")
    if not isinstance(manifest, dict):
        raise ContractError("Device Session manifest must be an object")
    if manifest.get("session_id") != expected_session_id:
        raise ContractError("session manifest does not match the requested session")

    artifacts: list[ArtifactDescriptor] = []
    schema = manifest.get("schema")
    if schema in {"ylx.device-session.v1", "ylx.device-session.v2"}:
        _collect_artifacts(manifest, artifacts)
    elif isinstance(manifest.get("files"), list):
        artifacts.extend(
            _legacy_artifact(entry, index)
            for index, entry in enumerate(manifest["files"])
        )
    else:
        raise ContractError(f"unsupported session manifest schema: {schema!r}")
    if not artifacts:
        raise ContractError("Device Session manifest contains no artifacts")

    seen_ids: set[str] = set()
    seen_paths: set[tuple[str, ...]] = set()
    for artifact in artifacts:
        if artifact.artifact_id in seen_ids:
            raise ContractError(
                f"Device Session repeats artifact_id {artifact.artifact_id}"
            )
        portable_path = _portable_path_key(artifact.relative_path)
        if portable_path in seen_paths:
            raise ContractError(
                f"Device Session repeats a portable artifact path: {artifact.path}"
            )
        seen_ids.add(artifact.artifact_id)
        seen_paths.add(portable_path)
    return tuple(
        sorted(artifacts, key=lambda artifact: (artifact.path, artifact.artifact_id))
    )


def _collect_artifacts(value: Any, output: list[ArtifactDescriptor]) -> None:
    if isinstance(value, dict):
        descriptor = _artifact_from_object(value)
        if descriptor is not None:
            output.append(descriptor)
            return
        for child in value.values():
            _collect_artifacts(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_artifacts(child, output)


def _artifact_from_object(value: dict[str, Any]) -> ArtifactDescriptor | None:
    fields = {"artifact_id", "role", "path", "media_type", "bytes", "sha256"}
    if not fields.issubset(value):
        return None
    artifact_id = value["artifact_id"]
    role = value["role"]
    path = value["path"]
    media_type = value["media_type"]
    size_bytes = value["bytes"]
    sha256 = value["sha256"]
    if not all(
        isinstance(item, str) and item for item in (artifact_id, role, path, media_type)
    ):
        raise ContractError("Device Session contains a malformed artifact descriptor")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise ContractError(
            "Device Session artifact bytes must be a non-negative integer"
        )
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise ContractError(
            "Device Session artifact sha256 must be lowercase hexadecimal"
        )
    safe_segment(artifact_id, "artifact_id")
    safe_relative_path(path)
    return ArtifactDescriptor(
        artifact_id=artifact_id,
        role=role,
        path=path,
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )


def _legacy_artifact(value: Any, index: int) -> ArtifactDescriptor:
    if not isinstance(value, dict):
        raise ContractError(f"legacy manifest files[{index}] must be an object")
    path = value.get("display_path")
    role = value.get("role")
    size_bytes = value.get("size_bytes")
    sha256 = value.get("sha256")
    media_type = value.get("media_type") or "application/octet-stream"
    if not all(isinstance(item, str) and item for item in (path, role, media_type)):
        raise ContractError(f"legacy manifest files[{index}] is malformed")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise ContractError(f"legacy manifest files[{index}] has invalid size_bytes")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise ContractError(f"legacy manifest files[{index}] has invalid sha256")
    safe_relative_path(path)
    return ArtifactDescriptor(
        artifact_id=sha256,
        role=role,
        path=path,
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )


def export_session_tree(
    *,
    source: Source,
    session: SessionInfo,
    output_root: Path,
    manifest_name: str,
    manifest_bytes: bytes,
    artifact_writer: Callable[[ArtifactDescriptor, Path], None],
    progress: Callable[[str], None] | None = None,
) -> ExportedSession:
    """Write one verified source tree and publish it with one directory rename."""

    safe_segment(manifest_name, "manifest filename")
    session_id = safe_segment(session.session_id, "session_id")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if session.manifest_sha256 and manifest_sha256 != session.manifest_sha256:
        raise ContractError(
            f"session {session_id} manifest digest changed after discovery"
        )
    artifacts = artifacts_from_manifest(manifest_bytes, session_id)
    reserved_paths = {
        _portable_path_key(Path(manifest_name)),
        _portable_path_key(Path(EXPORT_RECEIPT)),
    }
    for artifact in artifacts:
        if _portable_path_key(artifact.relative_path) in reserved_paths:
            raise ContractError(
                f"artifact path is reserved by the export format: {artifact.path}"
            )
    artifact_bytes = sum(artifact.size_bytes for artifact in artifacts)
    if artifact_bytes != session.total_bytes:
        raise ContractError(
            f"session {session_id} total_bytes does not match its manifest artifacts"
        )
    device_directory = output_root / _filesystem_component(source.display_name)
    final_directory = device_directory / session_id
    device_directory.mkdir(parents=True, exist_ok=True)

    if final_directory.exists():
        if _existing_export_matches(
            final_directory,
            manifest_name,
            len(manifest_bytes),
            manifest_sha256,
            artifacts,
        ):
            _emit(progress, f"{session_id}: already verified, reusing existing export")
            return ExportedSession(
                session_id=session_id,
                path=final_directory,
                artifact_count=len(artifacts),
                total_bytes=artifact_bytes,
                reused=True,
            )
        raise ExportError(
            f"destination already exists but does not match the session: {final_directory}"
        )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{session_id}-", suffix=".part", dir=device_directory)
    )
    try:
        (staging / manifest_name).write_bytes(manifest_bytes)
        for index, artifact in enumerate(artifacts, start=1):
            destination = staging / artifact.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _emit(
                progress,
                f"{session_id}: {index}/{len(artifacts)} {artifact.path}",
            )
            artifact_writer(artifact, destination)
            _verify_artifact_file(destination, artifact)
        receipt = {
            "schema": "openaria.bridge-export.v1",
            "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "mode": source.mode.value,
            "source": source.location,
            "device": {
                "device_id": source.device_id,
                "device_label": source.device_label,
            },
            "session_id": session_id,
            "manifest": {
                "path": manifest_name,
                "bytes": len(manifest_bytes),
                "sha256": manifest_sha256,
            },
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "role": artifact.role,
                    "path": artifact.path,
                    "media_type": artifact.media_type,
                    "bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                }
                for artifact in artifacts
            ],
        }
        (staging / EXPORT_RECEIPT).write_text(
            json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(final_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return ExportedSession(
        session_id=session_id,
        path=final_directory,
        artifact_count=len(artifacts),
        total_bytes=artifact_bytes,
    )


def _existing_export_matches(
    directory: Path,
    manifest_name: str,
    manifest_size_bytes: int,
    manifest_sha256: str,
    artifacts: tuple[ArtifactDescriptor, ...],
) -> bool:
    manifest = directory / manifest_name
    if not _regular_file_matches(manifest, manifest_size_bytes, manifest_sha256):
        return False
    return all(
        _regular_file_matches(
            directory / artifact.relative_path,
            artifact.size_bytes,
            artifact.sha256,
        )
        for artifact in artifacts
    )


def _regular_file_matches(path: Path, size_bytes: int, sha256: str) -> bool:
    try:
        return (
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == size_bytes
            and sha256_file(path) == sha256
        )
    except OSError:
        return False


def _verify_artifact_file(path: Path, artifact: ArtifactDescriptor) -> None:
    if not _regular_file_matches(path, artifact.size_bytes, artifact.sha256):
        raise ExportError(
            f"downloaded artifact failed size/SHA-256 verification: {artifact.path}"
        )


def _filesystem_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned[:120] or "openaria-device"


def _portable_path_key(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)
