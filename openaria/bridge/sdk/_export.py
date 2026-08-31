"""Shared atomic exporter for LAN and card sources."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._json import load_json
from ._media import (
    FINAL_MEDIA_NAME,
    RENDERER_NAME,
    RENDERER_VERSION,
    RenderedMedia,
    render_session_video,
)
from .errors import ContractError, ExportError
from .models import ExportedSession, SessionInfo, Source

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
INTERNAL_DIRECTORY = ".openaria"
SOURCE_DIRECTORY = "source"
EXPORT_RECEIPT = "export.json"
MEDIA_RECEIPT = "media.json"
LEGACY_EXPORT_RECEIPT = ".openaria-export.json"
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
    """Verify source bytes, render final media, and publish with one directory rename."""

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
        _portable_path_key(Path(FINAL_MEDIA_NAME)),
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
        existing_media_bytes = _existing_export_matches(
            final_directory,
            session_id,
            manifest_name,
            len(manifest_bytes),
            manifest_sha256,
            artifacts,
        )
        if existing_media_bytes is not None:
            _remove_legacy_source_tree(final_directory, manifest_name, artifacts)
            _emit(progress, f"{session_id}: 成片已校验，直接复用")
            return ExportedSession(
                session_id=session_id,
                path=final_directory,
                artifact_count=len(artifacts),
                total_bytes=artifact_bytes,
                reused=True,
                media_path=final_directory / FINAL_MEDIA_NAME,
                media_bytes=existing_media_bytes,
            )
        if _legacy_export_matches(
            final_directory,
            manifest_name,
            len(manifest_bytes),
            manifest_sha256,
            artifacts,
        ):
            _emit(progress, f"{session_id}: 正在把旧版源数据导出升级为成片")
            rendered = _upgrade_legacy_export(
                source=source,
                session_id=session_id,
                directory=final_directory,
                manifest_name=manifest_name,
                manifest_bytes=manifest_bytes,
                manifest_sha256=manifest_sha256,
                artifacts=artifacts,
                progress=progress,
            )
            return ExportedSession(
                session_id=session_id,
                path=final_directory,
                artifact_count=len(artifacts),
                total_bytes=artifact_bytes,
                media_path=rendered.path,
                media_bytes=rendered.size_bytes,
            )
        raise ExportError(
            f"destination already exists but does not match the session: {final_directory}"
        )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{session_id}-", suffix=".part", dir=device_directory)
    )
    try:
        internal = staging / INTERNAL_DIRECTORY
        source_tree = internal / SOURCE_DIRECTORY
        source_tree.mkdir(parents=True)
        (source_tree / manifest_name).write_bytes(manifest_bytes)
        for index, artifact in enumerate(artifacts, start=1):
            destination = source_tree / artifact.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _emit(
                progress,
                f"{session_id}: {index}/{len(artifacts)} {artifact.path}",
            )
            artifact_writer(artifact, destination)
            _verify_artifact_file(destination, artifact)
        _write_json(
            internal / EXPORT_RECEIPT,
            _export_receipt(
                source=source,
                session_id=session_id,
                manifest_name=manifest_name,
                manifest_bytes=len(manifest_bytes),
                manifest_sha256=manifest_sha256,
                artifacts=artifacts,
            ),
        )
        rendered = render_session_video(
            source_tree,
            manifest_bytes,
            staging / FINAL_MEDIA_NAME,
            lambda message: _emit(progress, f"{session_id}: {message}"),
        )
        removed_media = _remove_media_inputs(source_tree, artifacts)
        _emit(progress, f"{session_id}: 已清理 {len(removed_media)} 个源媒体分片")
        _write_json(
            internal / MEDIA_RECEIPT,
            _media_receipt(
                session_id,
                manifest_sha256,
                rendered,
                artifacts,
                removed_media,
            ),
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
        media_path=final_directory / FINAL_MEDIA_NAME,
        media_bytes=rendered.size_bytes,
    )


def _existing_export_matches(
    directory: Path,
    session_id: str,
    manifest_name: str,
    manifest_size_bytes: int,
    manifest_sha256: str,
    artifacts: tuple[ArtifactDescriptor, ...],
) -> int | None:
    internal = directory / INTERNAL_DIRECTORY
    source_tree = internal / SOURCE_DIRECTORY
    manifest = source_tree / manifest_name
    if not _regular_file_matches(manifest, manifest_size_bytes, manifest_sha256):
        return None
    retained_artifacts = tuple(
        artifact for artifact in artifacts if not _is_media_artifact(artifact)
    )
    if not all(
        _regular_file_matches(
            source_tree / artifact.relative_path,
            artifact.size_bytes,
            artifact.sha256,
        )
        for artifact in retained_artifacts
    ):
        return None
    media_artifacts = tuple(
        artifact for artifact in artifacts if _is_media_artifact(artifact)
    )
    if any(
        (source_tree / artifact.relative_path).exists()
        or (source_tree / artifact.relative_path).is_symlink()
        for artifact in media_artifacts
    ):
        return None
    export_receipt = _read_json_object(internal / EXPORT_RECEIPT)
    if export_receipt != _receipt_without_completion_time(
        _export_receipt(
            source=None,
            session_id=session_id,
            manifest_name=manifest_name,
            manifest_bytes=manifest_size_bytes,
            manifest_sha256=manifest_sha256,
            artifacts=artifacts,
        )
    ):
        return None
    media_receipt = _read_json_object(internal / MEDIA_RECEIPT)
    if (
        media_receipt is None
        or media_receipt.get("schema") != "openaria.media-export.v1"
        or media_receipt.get("session_id") != session_id
        or media_receipt.get("source_manifest_sha256") != manifest_sha256
        or media_receipt.get("renderer")
        != {"name": RENDERER_NAME, "version": RENDERER_VERSION}
        or media_receipt.get("inputs") != _media_input_records(artifacts)
        or media_receipt.get("cleanup")
        != {
            "status": "complete",
            "removed_paths": [artifact.path for artifact in media_artifacts],
        }
    ):
        return None
    output = media_receipt.get("output")
    if not isinstance(output, dict) or output.get("path") != FINAL_MEDIA_NAME:
        return None
    size_bytes = output.get("bytes")
    sha256 = output.get("sha256")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or not isinstance(sha256, str)
        or not SHA256_RE.fullmatch(sha256)
    ):
        return None
    if not _regular_file_matches(directory / FINAL_MEDIA_NAME, size_bytes, sha256):
        return None
    return size_bytes


def _legacy_export_matches(
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


def _upgrade_legacy_export(
    *,
    source: Source,
    session_id: str,
    directory: Path,
    manifest_name: str,
    manifest_bytes: bytes,
    manifest_sha256: str,
    artifacts: tuple[ArtifactDescriptor, ...],
    progress: Callable[[str], None] | None,
) -> RenderedMedia:
    temporary_internal = directory / f"{INTERNAL_DIRECTORY}.upgrade.part"
    final_internal = directory / INTERNAL_DIRECTORY
    output = directory / FINAL_MEDIA_NAME
    if temporary_internal.exists() or final_internal.exists() or output.exists():
        raise ExportError(f"cannot safely upgrade existing export: {directory}")
    rendered: RenderedMedia | None = None
    published = False
    try:
        rendered = render_session_video(
            directory,
            manifest_bytes,
            output,
            lambda message: _emit(progress, f"{session_id}: {message}"),
        )
        source_tree = temporary_internal / SOURCE_DIRECTORY
        source_tree.mkdir(parents=True)
        _link_or_copy(directory / manifest_name, source_tree / manifest_name)
        for artifact in artifacts:
            destination = source_tree / artifact.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _link_or_copy(directory / artifact.relative_path, destination)
        _write_json(
            temporary_internal / EXPORT_RECEIPT,
            _export_receipt(
                source=source,
                session_id=session_id,
                manifest_name=manifest_name,
                manifest_bytes=len(manifest_bytes),
                manifest_sha256=manifest_sha256,
                artifacts=artifacts,
            ),
        )
        removed_media = _remove_media_inputs(source_tree, artifacts)
        _write_json(
            temporary_internal / MEDIA_RECEIPT,
            _media_receipt(
                session_id,
                manifest_sha256,
                rendered,
                artifacts,
                removed_media,
            ),
        )
        temporary_internal.rename(final_internal)
        published = True
        _remove_legacy_source_tree(directory, manifest_name, artifacts)
        return rendered
    except Exception:
        shutil.rmtree(temporary_internal, ignore_errors=True)
        if not published and rendered is not None:
            output.unlink(missing_ok=True)
        raise


def _remove_legacy_source_tree(
    directory: Path,
    manifest_name: str,
    artifacts: tuple[ArtifactDescriptor, ...],
) -> None:
    paths = [directory / manifest_name]
    paths.extend(directory / artifact.relative_path for artifact in artifacts)
    paths.append(directory / LEGACY_EXPORT_RECEIPT)
    for path in paths:
        try:
            if not path.is_symlink() and path.is_file():
                path.unlink()
        except OSError:
            continue
    parents = {
        parent
        for artifact in artifacts
        for parent in (directory / artifact.relative_path).parents
        if parent != directory and directory in parent.parents
    }
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def _link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _is_media_artifact(artifact: ArtifactDescriptor) -> bool:
    media_type = artifact.media_type.casefold()
    role = artifact.role.casefold()
    return media_type.startswith(("video/", "audio/")) or role.startswith(
        ("video.", "audio.")
    )


def _remove_media_inputs(
    source_tree: Path, artifacts: tuple[ArtifactDescriptor, ...]
) -> tuple[str, ...]:
    media_artifacts = tuple(
        artifact for artifact in artifacts if _is_media_artifact(artifact)
    )
    for artifact in media_artifacts:
        path = source_tree / artifact.relative_path
        try:
            if path.is_symlink() or not path.is_file():
                raise ExportError(
                    f"temporary media input disappeared before cleanup: {artifact.path}"
                )
            path.unlink()
        except ExportError:
            raise
        except OSError as error:
            raise ExportError(
                f"cannot clean temporary media input {artifact.path}: {error}"
            ) from error
    parents = {
        parent
        for artifact in media_artifacts
        for parent in (source_tree / artifact.relative_path).parents
        if parent != source_tree and source_tree in parent.parents
    }
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass
    return tuple(artifact.path for artifact in media_artifacts)


def _media_input_records(
    artifacts: tuple[ArtifactDescriptor, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "artifact_id": artifact.artifact_id,
            "role": artifact.role,
            "path": artifact.path,
            "media_type": artifact.media_type,
            "bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        for artifact in artifacts
        if _is_media_artifact(artifact)
    ]


def _export_receipt(
    *,
    source: Source | None,
    session_id: str,
    manifest_name: str,
    manifest_bytes: int,
    manifest_sha256: str,
    artifacts: tuple[ArtifactDescriptor, ...],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": "openaria.bridge-export.v2",
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "manifest": {
            "path": manifest_name,
            "bytes": manifest_bytes,
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
    if source is not None:
        receipt.update(
            {
                "mode": source.mode.value,
                "source": source.location,
                "device": {
                    "device_id": source.device_id,
                    "device_label": source.device_label,
                },
            }
        )
    return receipt


def _receipt_without_completion_time(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt.pop("completed_at", None)
    return receipt


def _media_receipt(
    session_id: str,
    manifest_sha256: str,
    rendered: RenderedMedia,
    artifacts: tuple[ArtifactDescriptor, ...],
    removed_media: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": "openaria.media-export.v1",
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "source_manifest_sha256": manifest_sha256,
        "renderer": {"name": RENDERER_NAME, "version": RENDERER_VERSION},
        "layout": "side-by-side",
        "inputs": _media_input_records(artifacts),
        "cleanup": {
            "status": "complete",
            "removed_paths": list(removed_media),
        },
        "timeline": {
            "verdict": "aligned",
            "video_start_time_seconds": rendered.video_start_time_seconds,
            "audio_start_time_seconds": rendered.audio_start_time_seconds,
            "audio_offset_seconds": rendered.audio_offset_seconds,
        },
        "segments": {
            "video": rendered.video_segment_count,
            "video_frames": rendered.video_frame_count,
            "output_fps": rendered.output_fps,
            "audio": rendered.audio_segment_count,
        },
        "output": {
            "path": FINAL_MEDIA_NAME,
            "bytes": rendered.size_bytes,
            "sha256": rendered.sha256,
            "container": "mp4",
            "video_codec": "h264",
            "audio_codec": "aac" if rendered.has_audio else None,
        },
    }


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    value.pop("completed_at", None)
    value.pop("mode", None)
    value.pop("source", None)
    value.pop("device", None)
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
