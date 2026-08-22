"""Card to object store, end to end.

One pass over a recording card: find the sessions, check the bytes against
what the card claims, transcode every session to one HEVC profile, and put
the result in an S3-compatible bucket.

The card is treated as read-only evidence throughout. Nothing here writes to
it, and every byte that leaves it is checked against the SHA-256 the card's
own publication manifest declares before it is used for anything.

    uv run main.py --card /mnt/tfcard --work ./work --bucket recordings

Object storage is configured by environment (S3_ENDPOINT, S3_ACCESS_KEY,
S3_SECRET_KEY, S3_REGION) so credentials never sit in a shell history.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker

# Where a card keeps its recordings, in probe order. The long one is the
# Ubuntu Core layout: there the mount point is the whole `ubuntu-data`
# partition, so the capture snap's directory sits well below the root.
RECORDING_CONTAINERS = (
    "recordings",
    "YLX_RECORDINGS",
    "system-data/var/snap/ylx-capture/common/recordings",
)

# One delivery profile for everything, so a consumer downstream never has to
# ask what a given file is.
#
# H.264 rather than HEVC, which is the more efficient codec and was the first
# choice: Chrome cannot decode HEVC on Linux at all (a `<video>` fed one fails
# with MEDIA_ERR_SRC_NOT_SUPPORTED), and these files are previewed in a
# browser. An archive nobody can open is not an archive. `yuv420p` and the
# High profile are the same combination the existing pipeline output uses.
#
# `+faststart` is the load-bearing flag. The capture card writes fragmented
# MP4 with an empty moov, which carries no sample index, so a browser has to
# crawl the fragments before it can play: measured on one 39 MB clip, that is
# 19 range requests, 406 MB transferred and 40 seconds to first frame. The
# same stream remuxed with the index in front plays in 3 seconds.
VIDEO_PRESET = "slow"
CRF_FOR_MJPEG_SOURCE = 20
CRF_FOR_H264_SOURCE = 18
SUPPORTED_ROTATIONS = (0, 180)
NORMALIZATION_STATE_SCHEMA = "ylx.normalization-state.v1"
NORMALIZATION_STATE_FILENAME = ".normalization-state.json"
FFMPEG_ENCODE_PREFIX = (
    "ffmpeg",
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
)
NORMALIZED_PIXEL_FORMAT = "yuv420p"
NORMALIZED_MOVFLAGS = "+faststart"
PUBLICATION_LEASE_STALE_AFTER_SECONDS = 24 * 60 * 60
PUBLISHED_AUXILIARY_ROLES = frozenset({"imu", "metadata"})
SBS_EXPORT_AUDIO_SUFFIXES = frozenset({".wav", ".m4a"})
SBS_EXPORT_AUDIO_BITRATE = "192k"

READ_CHUNK_BYTES = 1024 * 1024

DEVICE_SESSION_V1_SCHEMA = "ylx.device-session.v1"
DEVICE_SESSION_V2_SCHEMA = "ylx.device-session.v2"
BUCKET_PUBLICATION_V2_SCHEMA = "ylx.bucket-publication.v2"
BUCKET_PUBLICATION_V3_SCHEMA = "ylx.bucket-publication.v3"
LEGACY_PUBLICATION_MANIFEST_SCHEMA = "legacy.publication-manifest.v1"
UUID_V7_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
UUID_V4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")

# Aliyun OSS defaults, overridable by environment. OSS ignores the SigV4
# credential-scope region but still requires a syntactically valid one.
DEFAULT_S3_ENDPOINT = "https://oss-cn-beijing.aliyuncs.com"
DEFAULT_S3_REGION = "us-east-1"

# DRAFT RP-YLX publication-manifest v1 mirror. These are source pins, not an
# authority claim; production registry material is supplied outside the card.
RP_MANIFEST_SCHEMA_SHA256 = (
    "34fdff84002e084ebc1d6cbd7ac51189ea93ceff9a5fab3b24dd4ed07f80129e"
)
RP_SIGNATURE_SCHEMA_SHA256 = (
    "48fb65c73ba47406120aa75b878ddf7dd7a112b14fc5b4b0871a0f60e5946444"
)
RP_KAT_SHA256 = "d14efca3085594e1a1991b1c3662b565171e94751abefeba5d3c88cf7825c431"
RP_ADMISSION_MANIFEST_SHA256 = (
    "bc8de60db9766cf8e5e18d8f63bdabdb3288b896f5cb4e7501551d317e131347"
)
RP_ADMISSION_VECTOR_SHA256 = (
    "7b14d91cce9dfc91b96b30095a1760e6e7d79ed84844a9f75d70280655233c9b"
)
TRUSTED_KEY_REGISTRY_SCHEMA_SHA256 = (
    "16b097254d992dd937cee77e3f8a511900f30cc501d93f09291d069d7861ee64"
)
VENDOR_ROOT = Path(__file__).resolve().parent / "vendor" / "rp-ylx"
YLX_CONTRACT_ROOT = Path(__file__).resolve().parent / "vendor" / "ylx-contracts"
YLX_DEVICE_SESSION_V1_SCHEMA_SHA256 = (
    "9292820ba81b518c17fd580de49bfd1c92a3519242abf4eb29bbe05f96a02b9c"
)
YLX_DEVICE_SESSION_V2_SCHEMA_SHA256 = (
    "8dc6096981f3fc50f9b4418000431955e0ba9424c7c0257cd2e129251a6a715b"
)
YLX_BUCKET_PUBLICATION_V2_SCHEMA_SHA256 = (
    "15fbe4a817ba2937769771abb8cf7ac78384696d539a6f38ecf7f74ebe02f8f1"
)
YLX_BUCKET_PUBLICATION_V3_SCHEMA_SHA256 = (
    "90d6e52f587da3ca4d9f8222db680ff9677e4e4470fc079f92744026dff2d42c"
)
YLX_CONTRACT_SOURCE_REPO = "openaria-contract-snapshot"
YLX_CONTRACT_SOURCE_REF = "snapshot/2026-08-23"
YLX_CONTRACT_SOURCE_COMMIT = "1f026c9d0273186acc35f465014aa25029bd6863"
YLX_CONTRACT_SOURCE_ROOT = f"{YLX_CONTRACT_SOURCE_REPO}@{YLX_CONTRACT_SOURCE_COMMIT}"
YLX_CONTRACT_SCHEMA_SOURCE_PATHS = {
    "ylx-bucket-publication-v2.schema.json": (
        "contracts/schemas/ylx-bucket-publication-v2.schema.json"
    ),
    "ylx-bucket-publication-v3.schema.json": (
        "contracts/schemas/ylx-bucket-publication-v3.schema.json"
    ),
    "ylx-device-session-v1.schema.json": (
        "contracts/schemas/ylx-device-session-v1.schema.json"
    ),
    "ylx-device-session-v2.schema.json": (
        "contracts/schemas/ylx-device-session-v2.schema.json"
    ),
}
AUDIO_DURATION_EPSILON_SECONDS = 1e-9
MAX_AUDIO_BYTE_COUNT = (1 << 63) - 1


class PipelineError(RuntimeError):
    """Anything that should stop this session without a traceback."""


def _reject_duplicate_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite value {value}")


def parse_strict_json(raw: bytes, label: str) -> Any:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PipelineError(f"invalid strict JSON in {label}: {error}") from error

    def check(item: Any) -> None:
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise PipelineError(
                    f"invalid strict JSON in {label}: isolated surrogate"
                )
        elif isinstance(item, float) and not math.isfinite(item):
            raise PipelineError(f"invalid strict JSON in {label}: non-finite number")
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                check(key)
                check(child)

    check(value)
    return value


def canonical_signature_payload(manifest: dict[str, Any]) -> bytes:
    if "publication_signature" not in manifest:
        raise PipelineError("publication manifest has no signature")
    try:
        return json.dumps(
            {
                key: value
                for key, value in manifest.items()
                if key != "publication_signature"
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise PipelineError(f"manifest is not RP canonicalizable: {error}") from error


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_vendored_regular_file(path: Path, relative: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise PipelineError(
            f"vendored YLX contract file is unavailable: {relative}"
        ) from error
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise PipelineError(
            f"vendored YLX contract file must be a regular non-symlink file: {relative}"
        )


def _vendor_path_from_source_path(source_path: str) -> Path:
    prefix = "contracts/fixtures/"
    if not source_path.startswith(prefix):
        raise PipelineError(f"unsupported vendored YLX source path {source_path}")
    return YLX_CONTRACT_ROOT / "fixtures" / source_path.removeprefix(prefix)


@lru_cache
def _ylx_contract_source_metadata() -> dict[str, Any]:
    metadata_path = YLX_CONTRACT_ROOT / "SOURCE.json"
    _require_vendored_regular_file(metadata_path, "SOURCE.json")
    metadata = parse_strict_json(metadata_path.read_bytes(), "YLX vendor metadata")
    if not isinstance(metadata, dict):
        raise PipelineError("YLX vendor metadata must be an object")
    if (
        metadata.get("source_repo") != YLX_CONTRACT_SOURCE_REPO
        or metadata.get("source_ref") != YLX_CONTRACT_SOURCE_REF
        or metadata.get("source_commit") != YLX_CONTRACT_SOURCE_COMMIT
        or metadata.get("source_root") != YLX_CONTRACT_SOURCE_ROOT
    ):
        raise PipelineError("vendored YLX source identity pin mismatch")
    if "requested_authority" in metadata:
        raise PipelineError("vendored YLX metadata must use the actual source pin")

    schemas = metadata.get("schemas")
    corpora = metadata.get("corpora")
    if not isinstance(schemas, dict) or not isinstance(corpora, dict):
        raise PipelineError("vendored YLX metadata lacks schema/corpus pins")
    if metadata.get("source_paths") != YLX_CONTRACT_SCHEMA_SOURCE_PATHS:
        raise PipelineError("vendored YLX schema source path pin mismatch")
    if set(schemas) != set(YLX_CONTRACT_SCHEMA_SOURCE_PATHS):
        raise PipelineError("vendored YLX schema source path set mismatch")

    expected_files: dict[str, str] = {}
    for filename, descriptor in schemas.items():
        if not isinstance(filename, str) or not isinstance(descriptor, dict):
            raise PipelineError("vendored YLX schema metadata is malformed")
        source_path = descriptor.get("source_path")
        sha256 = descriptor.get("sha256")
        if (
            source_path != YLX_CONTRACT_SCHEMA_SOURCE_PATHS.get(filename)
            or not isinstance(sha256, str)
            or SHA256_PATTERN.fullmatch(sha256) is None
        ):
            raise PipelineError(f"vendored YLX schema metadata mismatch for {filename}")
        expected_files[filename] = sha256

    for source_path, sha256 in corpora.items():
        if (
            not isinstance(source_path, str)
            or not isinstance(sha256, str)
            or SHA256_PATTERN.fullmatch(sha256) is None
        ):
            raise PipelineError("vendored YLX corpus metadata is malformed")
        relative = _vendor_path_from_source_path(source_path).relative_to(
            YLX_CONTRACT_ROOT
        )
        expected_files[relative.as_posix()] = sha256

    actual_files: set[str] = set()
    for path in YLX_CONTRACT_ROOT.rglob("*"):
        relative = path.relative_to(YLX_CONTRACT_ROOT).as_posix()
        if relative == "SOURCE.json":
            continue
        try:
            path_mode = path.lstat().st_mode
        except OSError as error:
            raise PipelineError(
                f"vendored YLX contract file is unavailable: {relative}"
            ) from error
        if stat.S_ISDIR(path_mode):
            continue
        actual_files.add(relative)
        if stat.S_ISLNK(path_mode) or not stat.S_ISREG(path_mode):
            raise PipelineError(
                "vendored YLX contract file must be a regular non-symlink file: "
                f"{relative}"
            )
    if actual_files != set(expected_files):
        missing = sorted(set(expected_files) - actual_files)
        extra = sorted(actual_files - set(expected_files))
        raise PipelineError(
            f"vendored YLX contract tree mismatch: missing={missing}; extra={extra}"
        )
    for relative, expected_sha256 in expected_files.items():
        path = YLX_CONTRACT_ROOT / relative
        _require_vendored_regular_file(path, relative)
        if _digest(path) != expected_sha256:
            raise PipelineError(f"vendored YLX contract pin mismatch for {relative}")
    return metadata


def ylx_contract_fixture(relative: str) -> Path:
    _ylx_contract_source_metadata()
    path = YLX_CONTRACT_ROOT / "fixtures" / relative
    _require_vendored_regular_file(path, f"fixtures/{relative}")
    return path


@lru_cache
def _pinned_ylx_schema(filename: str, expected_sha256: str) -> dict[str, Any]:
    _ylx_contract_source_metadata()
    path = YLX_CONTRACT_ROOT / filename
    _require_vendored_regular_file(path, filename)
    if _digest(path) != expected_sha256:
        raise PipelineError(f"vendored YLX schema pin mismatch for {filename}")
    schema = parse_strict_json(path.read_bytes(), filename)
    Draft202012Validator.check_schema(schema)
    return schema


@lru_cache
def _device_session_v1_validator() -> Draft202012Validator:
    return Draft202012Validator(
        _pinned_ylx_schema(
            "ylx-device-session-v1.schema.json",
            YLX_DEVICE_SESSION_V1_SCHEMA_SHA256,
        ),
        format_checker=FormatChecker(),
    )


@lru_cache
def _device_session_v2_validator() -> Draft202012Validator:
    return Draft202012Validator(
        _pinned_ylx_schema(
            "ylx-device-session-v2.schema.json",
            YLX_DEVICE_SESSION_V2_SCHEMA_SHA256,
        ),
        format_checker=FormatChecker(),
    )


@lru_cache
def _bucket_publication_v2_validator() -> Draft202012Validator:
    return Draft202012Validator(
        _pinned_ylx_schema(
            "ylx-bucket-publication-v2.schema.json",
            YLX_BUCKET_PUBLICATION_V2_SCHEMA_SHA256,
        ),
        format_checker=FormatChecker(),
    )


@lru_cache
def _bucket_publication_v3_validator() -> Draft202012Validator:
    return Draft202012Validator(
        _pinned_ylx_schema(
            "ylx-bucket-publication-v3.schema.json",
            YLX_BUCKET_PUBLICATION_V3_SCHEMA_SHA256,
        ),
        format_checker=FormatChecker(),
    )


def _validate_schema(validator: Draft202012Validator, value: Any, label: str) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise PipelineError(f"{label} schema rejection at {path}: {error.message}")


def rp_validators() -> tuple[Draft202012Validator, Draft202012Validator]:
    """Load byte-pinned RP schema mirrors; fail before accepting media on drift."""
    metadata = parse_strict_json(
        (VENDOR_ROOT / "SOURCE.json").read_bytes(), "RP vendor metadata"
    )
    manifest_path = VENDOR_ROOT / "publication-manifest-v1.schema.json"
    signature_path = VENDOR_ROOT / "publication-signature-v1.schema.json"
    if (
        _digest(manifest_path) != RP_MANIFEST_SCHEMA_SHA256
        or _digest(signature_path) != RP_SIGNATURE_SCHEMA_SHA256
        or metadata.get("source_commit") != "2db57ae68e04197397b8ac84f4d71548aa2fcb36"
        or metadata.get("publication_manifest_schema_sha256")
        != RP_MANIFEST_SCHEMA_SHA256
        or metadata.get("publication_signature_schema_sha256")
        != RP_SIGNATURE_SCHEMA_SHA256
        or metadata.get("trusted_key_registry_schema_sha256")
        != TRUSTED_KEY_REGISTRY_SCHEMA_SHA256
    ):
        raise PipelineError("vendored RP schema pin mismatch")
    manifest_schema = parse_strict_json(
        manifest_path.read_bytes(), "RP manifest schema"
    )
    signature_schema = parse_strict_json(
        signature_path.read_bytes(), "RP signature schema"
    )
    Draft202012Validator.check_schema(manifest_schema)
    Draft202012Validator.check_schema(signature_schema)
    return Draft202012Validator(
        manifest_schema, format_checker=FormatChecker()
    ), Draft202012Validator(signature_schema, format_checker=FormatChecker())


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (AttributeError, ValueError) as error:
        raise PipelineError(f"invalid registry {label}") from error
    if parsed.tzinfo is None:
        raise PipelineError(f"invalid registry {label}")
    return parsed.astimezone(UTC)


def validate_registry(registry: Any, verification_time: datetime) -> None:
    schema_path = VENDOR_ROOT / "trusted-key-registry-v1.schema.json"
    if _digest(schema_path) != TRUSTED_KEY_REGISTRY_SCHEMA_SHA256:
        raise PipelineError("trusted-key registry schema pin mismatch")
    schema = parse_strict_json(schema_path.read_bytes(), "registry schema")
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            registry
        )
    )
    if errors:
        raise PipelineError(
            f"trusted-key registry schema rejection: {errors[0].message}"
        )
    issued, expires = (
        _parse_time(registry["issued_at"], "issued_at"),
        _parse_time(registry["expires_at"], "expires_at"),
    )
    if issued > verification_time or expires <= verification_time or expires <= issued:
        raise PipelineError("trusted-key registry is stale or not yet valid")
    for binding in registry["bindings"].values():
        for key in binding.values():
            before, after = (
                _parse_time(key["not_before"], "not_before"),
                _parse_time(key["not_after"], "not_after"),
            )
            if after <= before:
                raise PipelineError("trusted key lifecycle is invalid")
            if key["status"] == "revoked":
                revoked = _parse_time(key["revoked_at"], "revoked_at")
                if not (
                    before <= revoked <= after
                    and revoked <= issued
                    and revoked <= verification_time
                ):
                    raise PipelineError("trusted key revocation lifecycle is invalid")


def verify_source_signature(
    manifest: dict[str, Any],
    registry: Any,
    external_device_identity: str,
    verification_time: datetime | None = None,
) -> dict[str, str]:
    manifest_validator, envelope_validator = rp_validators()
    errors = list(manifest_validator.iter_errors(manifest))
    if errors:
        raise PipelineError(
            f"RP publication manifest schema rejection: {errors[0].message}"
        )
    required = {
        "schema_version",
        "session_id",
        "revision",
        "captured_at",
        "published_at",
        "duration_seconds",
        "total_bytes",
        "video_bytes",
        "integrity_ok",
        "files",
        "publication_signature",
    }
    if (
        not required.issubset(manifest)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise PipelineError("RP publication manifest schema rejection")
    if (
        not isinstance(manifest["session_id"], str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", manifest["session_id"]) is None
        or not isinstance(manifest["revision"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["revision"]) is None
    ):
        raise PipelineError("RP publication manifest schema rejection")
    if (
        isinstance(manifest["duration_seconds"], bool)
        or not isinstance(manifest["duration_seconds"], (int, float))
        or not math.isfinite(manifest["duration_seconds"])
        or any(
            not isinstance(manifest[field], int)
            or isinstance(manifest[field], bool)
            or manifest[field] < 0
            for field in ("total_bytes", "video_bytes")
        )
        or not isinstance(manifest["integrity_ok"], bool)
    ):
        raise PipelineError("RP publication manifest schema rejection")
    for entry in manifest["files"]:
        if (
            not isinstance(entry, dict)
            or not {
                "id",
                "display_path",
                "role",
                "size_bytes",
                "sha256",
                "media_type",
            }.issubset(entry)
            or entry.get("role")
            not in {
                "video_left",
                "video_right",
                "video_mono",
                "imu",
                "metadata",
                "other",
            }
            or not isinstance(entry.get("size_bytes"), int)
            or isinstance(entry.get("size_bytes"), bool)
            or entry["size_bytes"] < 0
            or not isinstance(entry.get("display_path"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256"))) is None
        ):
            raise PipelineError("RP publication manifest schema rejection")
    ids = [entry["id"] for entry in manifest["files"]]
    paths = [entry["display_path"] for entry in manifest["files"]]
    if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
        raise PipelineError(
            "RP publication manifest invariant rejection: duplicate file id or display path"
        )
    total_bytes = sum(entry["size_bytes"] for entry in manifest["files"])
    video_bytes = sum(
        entry["size_bytes"]
        for entry in manifest["files"]
        if entry["role"] in {"video_left", "video_right", "video_mono"}
    )
    if manifest["total_bytes"] != total_bytes or manifest["video_bytes"] != video_bytes:
        raise PipelineError(
            "RP publication manifest invariant rejection: byte totals do not match inventory"
        )
    content_fields = {
        field: manifest[field]
        for field in (
            "schema_version",
            "session_id",
            "captured_at",
            "duration_seconds",
            "total_bytes",
            "video_bytes",
            "integrity_ok",
            "files",
        )
    }
    expected_revision = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                content_fields,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    if manifest["revision"] != expected_revision:
        raise PipelineError(
            "RP publication manifest invariant rejection: revision does not match canonical inventory"
        )
    if _parse_time(manifest["published_at"], "published_at") < _parse_time(
        manifest["captured_at"], "captured_at"
    ):
        raise PipelineError(
            "RP publication manifest invariant rejection: published_at precedes captured_at"
        )
    envelope = manifest["publication_signature"]
    errors = list(envelope_validator.iter_errors(envelope))
    if errors:
        raise PipelineError(
            f"RP publication-signature wire rejection: {errors[0].message}"
        )
    if not isinstance(envelope, dict) or envelope.get("algorithm") != "ed25519":
        raise PipelineError("RP publication-signature wire rejection")
    version = envelope.get("key_version")
    fingerprint = envelope.get("public_key_fingerprint")
    signature_hex = envelope.get("signature")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        or not isinstance(signature_hex, str)
        or re.fullmatch(r"[0-9a-f]{128}", signature_hex) is None
    ):
        raise PipelineError("RP publication-signature wire rejection")
    if not isinstance(registry, dict) or not isinstance(registry.get("bindings"), dict):
        raise PipelineError("trusted-key registry unavailable; refusing downgrade")
    verification_time = verification_time or datetime.now(UTC)
    validate_registry(registry, verification_time)
    binding = registry["bindings"].get(external_device_identity)
    if not isinstance(binding, dict):
        raise PipelineError("unknown external device identity or binding mismatch")
    key = binding.get(str(version))
    if not isinstance(key, dict):
        raise PipelineError("unknown trusted key version")
    if key.get("status") != "active":
        raise PipelineError("trusted key is unavailable or revoked")
    if not (
        _parse_time(key["not_before"], "not_before")
        <= verification_time
        < _parse_time(key["not_after"], "not_after")
    ):
        raise PipelineError("trusted key is not currently valid")
    if fingerprint != key.get("fingerprint"):
        raise PipelineError("registry fingerprint mismatch")
    try:
        public_key = bytes.fromhex(key["public_key_hex"])
        signature = bytes.fromhex(signature_hex)
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("malformed trusted public key or signature") from error
    if (
        len(public_key) != 32
        or len(signature) != 64
        or fingerprint != f"sha256:{hashlib.sha256(public_key).hexdigest()}"
    ):
        raise PipelineError("trusted public key fingerprint mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, canonical_signature_payload(manifest)
        )
    except (InvalidSignature, ValueError) as error:
        raise PipelineError("Ed25519 signature verification failed") from error
    return {
        "status": "sealed",
        "external_device_identity": external_device_identity,
        "key_version": str(version),
        "fingerprint": fingerprint,
    }


def authenticated_binding_receipt(
    raw: bytes,
    verification_time: datetime,
    trusted_public_key_pem: bytes,
    expected_issuer: str,
    expected_identity: str,
    expected_audience: str,
    expected_registry_revision: str,
) -> tuple[str, str]:
    """Verify a caller-owned receipt against an out-of-band trust policy."""
    receipt = parse_strict_json(raw, "authenticated binding receipt")
    required = {
        "schema_version",
        "status",
        "issuer",
        "identity",
        "audience",
        "external_device_identity",
        "inventory_revision",
        "registry_revision",
        "not_before",
        "not_after",
        "nonce",
        "public_key_fingerprint",
        "signature",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required
        or receipt.get("schema_version") != "ylx.authenticated-binding-receipt.v1"
        or receipt.get("status") != "active"
        or receipt.get("issuer") != expected_issuer
        or receipt.get("identity") != expected_identity
        or receipt.get("audience") != expected_audience
        or receipt.get("registry_revision") != expected_registry_revision
        or not isinstance(receipt.get("external_device_identity"), str)
        or not receipt["external_device_identity"]
        or not isinstance(receipt.get("inventory_revision"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["inventory_revision"]) is None
        or not isinstance(receipt.get("nonce"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt["nonce"]) is None
        or not isinstance(receipt.get("public_key_fingerprint"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["public_key_fingerprint"])
        is None
        or not isinstance(receipt.get("signature"), str)
    ):
        raise PipelineError("authenticated binding receipt is invalid")
    if (
        _parse_time(receipt.get("not_before"), "receipt not_before") > verification_time
        or _parse_time(receipt.get("not_after"), "receipt not_after")
        <= verification_time
    ):
        raise PipelineError("authenticated binding receipt is not currently valid")
    try:
        trusted_key = serialization.load_pem_public_key(trusted_public_key_pem)
        signature = base64.b64decode(receipt["signature"], validate=True)
    except (TypeError, ValueError) as error:
        raise PipelineError(
            "authenticated binding receipt signature material is invalid"
        ) from error
    if not isinstance(trusted_key, Ed25519PublicKey) or len(signature) != 64:
        raise PipelineError(
            "authenticated binding receipt signature material is invalid"
        )
    raw_public_key = trusted_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if (
        receipt["public_key_fingerprint"]
        != f"sha256:{hashlib.sha256(raw_public_key).hexdigest()}"
    ):
        raise PipelineError("authenticated binding receipt trust-key mismatch")
    payload = json.dumps(
        {key: value for key, value in receipt.items() if key != "signature"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    try:
        trusted_key.verify(signature, payload)
    except InvalidSignature as error:
        raise PipelineError(
            "authenticated binding receipt signature verification failed"
        ) from error
    return receipt["external_device_identity"], receipt["inventory_revision"]


def load_env_file(path: Path) -> None:
    """Seed the environment from a `.env` beside the script, if there is one.

    A real environment variable always wins, so a shell export or a CI secret
    overrides the committed file without editing it.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip())


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Artifact:
    display_path: str
    role: str
    size_bytes: int
    sha256: str
    media_type: str | None = None
    artifact_id: str | None = None
    segment_index: int | None = None

    def path_under(self, session_dir: Path) -> Path:
        relative = Path(self.display_path)
        if (
            not self.display_path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise PipelineError(f"{self.display_path} escapes the session directory")
        return session_dir / relative


def _natural_path_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Order numeric path components by value without requiring zero padding."""
    tokens: list[tuple[int, int | str]] = []
    for token in re.split(r"(\d+)", value):
        if token.isdigit():
            tokens.append((0, int(token)))
        else:
            tokens.append((1, token))
    # Preserve deterministic ordering when `1` and `01` have equal numeric keys.
    tokens.append((2, value))
    return tuple(tokens)


@dataclasses.dataclass(frozen=True)
class Session:
    directory: Path
    source_directory_name: str
    session_id: str
    captured_at: str
    duration_seconds: float
    artifacts: tuple[Artifact, ...]
    camera: dict
    source_manifest_path: Path
    source_manifest_sha256: str
    source_manifest_revision: str
    source_signature: dict[str, str]
    source_manifest_name: str = "publication_manifest.json"
    source_manifest_schema: str = LEGACY_PUBLICATION_MANIFEST_SCHEMA
    source_manifest_size_bytes: int = 0
    manifest_id: str = ""
    volume_id: str = ""
    source_manifest_sealed_at: str = ""
    device: dict[str, str] = dataclasses.field(default_factory=dict)
    take: dict[str, Any] = dataclasses.field(default_factory=dict)
    source_video_layout: str = ""
    declared_source_codec: str = ""
    source_declarations: dict[str, Any] = dataclasses.field(default_factory=dict)
    source_audio: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.source_directory_name

    def videos(self, role: str) -> list[Artifact]:
        """Video artifacts of one role, in the card's own segment order."""
        artifacts = [a for a in self.artifacts if a.role == role]
        if any(artifact.segment_index is not None for artifact in artifacts):
            return sorted(
                artifacts,
                key=lambda artifact: (
                    artifact.segment_index
                    if artifact.segment_index is not None
                    else sys.maxsize,
                    _natural_path_key(artifact.display_path),
                ),
            )
        return sorted(
            artifacts,
            key=lambda a: _natural_path_key(a.display_path),
        )

    @property
    def source_codec(self) -> str:
        camera_codec = self.camera.get("video_codec") or self.camera.get(
            "source_video_codec"
        )
        return self.declared_source_codec or (
            camera_codec if isinstance(camera_codec, str) else ""
        )


@dataclasses.dataclass(frozen=True)
class SbsExportPlan:
    output: Path
    workdir: Path
    preset: str
    crf: int
    audio_bitrate: str
    mode: str
    left_segments: tuple[Path, ...] = ()
    right_segments: tuple[Path, ...] = ()
    stereo_segments: tuple[Path, ...] = ()
    audio_segments: tuple[Path, ...] = ()

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_segments)


def device_id_of(card: Path) -> str | None:
    """The console keys sessions by device, and the card knows its own id.

    Stored bare (`30D5872D`); the console and the capture fleet both write it
    with the `YLX-` prefix, so it is added here rather than left to the caller
    to remember.
    """
    for container in ("system-data/var/snap/ylx-capture/common", "."):
        marker = card / container / "device-id"
        try:
            if marker.is_file():
                value = marker.read_text(encoding="utf-8").strip()
                if value:
                    return value if value.startswith("YLX-") else f"YLX-{value}"
        except OSError:
            continue
    return None


def _read_session_camera(directory: Path) -> dict:
    try:
        session_json_raw = _read_regular_file(
            directory, Path("session.json"), "session.json"
        )
    except PipelineError:
        return {}
    session_value = parse_strict_json(session_json_raw, "session.json")
    if not isinstance(session_value, dict):
        raise PipelineError("session.json must be an object")
    camera = session_value.get("camera", {})
    if not isinstance(camera, dict):
        raise PipelineError("session.json camera must be an object")
    return camera


def _signature_state_for_manifest(
    manifest: dict[str, Any],
    registry: Any,
    external_device_identity: str | None,
    allow_unsigned: bool,
    check_source_signature: bool,
) -> dict[str, str]:
    if not check_source_signature:
        return {"status": "unchecked_local_export"}
    if "publication_signature" in manifest:
        if not external_device_identity:
            raise PipelineError(
                "external authenticated device identity is required for signed media"
            )
        return verify_source_signature(manifest, registry, external_device_identity)
    if allow_unsigned:
        return {"status": "unsigned_degraded"}
    raise PipelineError(
        "unsigned publication manifest refused; pass --allow-unsigned explicitly"
    )


def _session_from_publication_manifest(
    directory: Path,
    source_directory_name: str,
    manifest_path: Path,
    manifest_bytes: bytes,
    manifest: Any,
    registry: Any,
    external_device_identity: str | None,
    allow_unsigned: bool,
    check_source_signature: bool,
) -> Session:
    if not isinstance(manifest, dict):
        raise PipelineError("publication_manifest.json must be an object")
    if manifest.get("integrity_ok") is not True:
        raise PipelineError("the publication manifest marks this session as not intact")
    session_id = _required_manifest_string(manifest, "session_id")
    captured_at = manifest.get("captured_at", "")
    if not isinstance(captured_at, str):
        raise PipelineError(
            "publication_manifest.json field captured_at must be a string"
        )
    duration_seconds = manifest.get("duration_seconds", 0.0)
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
    ):
        raise PipelineError(
            "publication_manifest.json field duration_seconds must be a finite number"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PipelineError("publication_manifest.json field files must be a list")
    artifacts = tuple(
        _artifact_from_manifest_entry(entry, index)
        for index, entry in enumerate(files)
    )

    camera = _read_session_camera(directory)
    signature_state = _signature_state_for_manifest(
        manifest,
        registry,
        external_device_identity,
        allow_unsigned,
        check_source_signature,
    )

    return Session(
        directory=directory,
        source_directory_name=source_directory_name,
        session_id=_object_identity(session_id, "session_id"),
        captured_at=captured_at,
        duration_seconds=float(duration_seconds),
        artifacts=artifacts,
        camera=camera,
        source_manifest_path=manifest_path,
        source_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_manifest_revision=str(manifest.get("revision") or ""),
        source_signature=signature_state,
        source_manifest_size_bytes=len(manifest_bytes),
    )


def _required_manifest_string(manifest: dict[str, Any], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value:
        raise PipelineError(
            f"publication_manifest.json field {field} must be a non-empty string"
        )
    return value


def _artifact_from_manifest_entry(entry: Any, index: int) -> Artifact:
    label = f"publication_manifest.json files[{index}]"
    if not isinstance(entry, dict):
        raise PipelineError(f"{label} must be an object")
    display_path = _required_manifest_string(entry, "display_path")
    role = _required_manifest_string(entry, "role")
    sha256 = _required_manifest_string(entry, "sha256")
    size_bytes = entry.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise PipelineError(f"{label}.size_bytes must be a non-negative integer")
    media_type = entry.get("media_type")
    if media_type is not None and not isinstance(media_type, str):
        raise PipelineError(f"{label}.media_type must be a string when present")
    return Artifact(
        display_path=display_path,
        role=role,
        size_bytes=size_bytes,
        sha256=sha256,
        media_type=media_type,
    )


def read_publication_session(directory: Path) -> Session:
    """Read one local publication/session directory for offline export.

    This path still verifies file bytes when `verify(session)` is called, but
    it intentionally does not enforce the card-ingest trust policy. SBS export
    is a local packaging operation, not a publication to the object store.
    """
    manifest_path = directory / "publication_manifest.json"
    if manifest_path.is_symlink():
        raise PipelineError("publication manifest is a symlink")
    if not manifest_path.is_file():
        raise PipelineError(f"no publication_manifest.json under {directory}")
    manifest_bytes = _read_regular_file(
        directory, Path("publication_manifest.json"), "publication_manifest.json"
    )
    manifest = parse_strict_json(manifest_bytes, "publication_manifest.json")
    return _session_from_publication_manifest(
        directory=directory,
        source_directory_name=directory.name,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        registry=None,
        external_device_identity=None,
        allow_unsigned=True,
        check_source_signature=False,
    )


def find_recordings_dir(card: Path) -> Path:
    for container in RECORDING_CONTAINERS:
        candidate = card / container
        try:
            if candidate.is_symlink():
                raise PipelineError(
                    f"recordings directory {candidate} must not be a symlink"
                )
            if candidate.is_dir():
                return candidate
        except OSError as error:
            # A card pulled while still mounted leaves a stale mount whose
            # every read fails. That is worth saying plainly, because the
            # fix is to reseat the card, not to look for a missing directory.
            raise PipelineError(
                f"{card} cannot be read ({error.strerror}); is the card still inserted?"
            ) from error
    raise PipelineError(
        f"no recordings directory under {card}; looked for "
        + ", ".join(RECORDING_CONTAINERS)
    )


def _require_device_session_v1(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(f"device-session v1 rejection: {message}")


def _require_string(
    value: Any, field: str, pattern: re.Pattern[str] | None = None
) -> str:
    _require_device_session_v1(isinstance(value, str) and bool(value), field)
    if pattern is not None:
        _require_device_session_v1(pattern.fullmatch(value) is not None, field)
    return value


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    _require_device_session_v1(isinstance(value, dict), field)
    return value


def _validate_relative_artifact_path(path: str) -> None:
    Artifact(path, "validation.only", 0, "0" * 64).path_under(Path("."))


def _device_session_artifact(
    entry: Any,
    *,
    expected_role: str | None,
    expected_media_type: str | None,
    internal_role: str,
    seen_paths: set[str],
    segment_index: int | None = None,
) -> Artifact:
    artifact = _require_dict(entry, "artifact must be an object")
    required = {"artifact_id", "role", "path", "media_type", "bytes", "sha256"}
    _require_device_session_v1(
        required.issubset(artifact) and set(artifact).issubset(required),
        "artifact fields",
    )
    source_role = _require_string(artifact.get("role"), "artifact role")
    if expected_role is not None:
        _require_device_session_v1(source_role == expected_role, "artifact role")
    else:
        _require_device_session_v1(
            ROLE_PATTERN.fullmatch(source_role) is not None, "artifact role"
        )
    media_type = _require_string(artifact.get("media_type"), "artifact media_type")
    if expected_media_type is not None:
        _require_device_session_v1(
            media_type == expected_media_type, "artifact media_type"
        )
    path = _require_string(artifact.get("path"), "artifact path")
    _validate_relative_artifact_path(path)
    if path in seen_paths:
        raise PipelineError(f"duplicate artifact path {path}")
    seen_paths.add(path)
    size_bytes = artifact.get("bytes")
    _require_device_session_v1(
        isinstance(size_bytes, int)
        and not isinstance(size_bytes, bool)
        and size_bytes >= 0,
        "artifact bytes",
    )
    sha256 = _require_string(artifact.get("sha256"), "artifact sha256", SHA256_PATTERN)
    artifact_id = _require_string(
        artifact.get("artifact_id"), "artifact artifact_id", SHA256_PATTERN
    )
    _require_device_session_v1(artifact_id == sha256, "artifact_id must equal sha256")
    return Artifact(
        display_path=path,
        role=internal_role,
        size_bytes=size_bytes,
        sha256=sha256,
        media_type=media_type,
        artifact_id=artifact_id,
        segment_index=segment_index,
    )


def _api_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _manifest_artifact_descriptors(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    video = manifest["video"]
    if video["layout"] == "split-eyes":
        video_artifacts = [
            artifact
            for segment in video["segments"]
            for artifact in segment["artifacts"].values()
        ]
    else:
        video_artifacts = [video["artifact"]]
    audio = manifest.get("audio")
    audio_artifacts = (
        [segment["artifact"] for segment in audio["segments"]]
        if isinstance(audio, dict) and audio.get("state") == "recorded"
        else []
    )
    return [
        *video_artifacts,
        manifest["imu"]["artifact"],
        manifest["frames"]["artifact"],
        *audio_artifacts,
        *manifest["logs"],
    ]


def _validate_device_session_v1_invariants(manifest: dict[str, Any]) -> None:
    take = manifest["take"]
    sequence = take["sequence"]
    continuation = take["continuation_of"]
    if (sequence == 1) != (continuation is None):
        raise PipelineError(
            "device-session v1 invariant rejection: take continuation mismatch"
        )
    if continuation == manifest["session_id"]:
        raise PipelineError(
            "device-session v1 invariant rejection: session cannot continue itself"
        )

    artifacts = _manifest_artifact_descriptors(manifest)
    artifact_ids = [artifact["artifact_id"] for artifact in artifacts]
    paths = [artifact["path"] for artifact in artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise PipelineError(
            "device-session v1 invariant rejection: duplicate artifact_id"
        )
    if len(paths) != len(set(paths)):
        raise PipelineError(
            "device-session v1 invariant rejection: duplicate artifact path"
        )
    for artifact in artifacts:
        if artifact["artifact_id"] != artifact["sha256"]:
            raise PipelineError(
                "device-session v1 invariant rejection: artifact_id must equal sha256"
            )

    camera = manifest["camera"]
    if camera["width"] != camera["eye_width"] * 2:
        raise PipelineError(
            "device-session v1 invariant rejection: camera.width must equal two eye widths"
        )
    nominal_fps = camera["sensor_fps"] / camera["frame_decimation"]
    quality_policy = manifest["integrity"].get("quality_policy")
    measured_semantics = "nominal_fps" in camera and quality_policy is not None
    if ("nominal_fps" in camera) != (quality_policy is not None):
        raise PipelineError(
            "device-session v1 invariant rejection: nominal_fps and quality_policy "
            "must appear together"
        )
    if measured_semantics and abs(camera["nominal_fps"] - nominal_fps) > 1e-9:
        raise PipelineError(
            "device-session v1 invariant rejection: nominal_fps must equal "
            "sensor_fps/frame_decimation"
        )
    if not measured_semantics and abs(camera["effective_fps"] - nominal_fps) > 1e-9:
        raise PipelineError(
            "device-session v1 invariant rejection: legacy effective_fps must equal "
            "sensor_fps/frame_decimation"
        )

    video = manifest["video"]
    split_eye_frame_start: int | None = None
    split_eye_frame_end: int | None = None
    if video["layout"] == "split-eyes":
        previous_frame_end: int | None = None
        previous_time_end: float | None = None
        for expected_index, segment in enumerate(video["segments"]):
            if segment["index"] != expected_index:
                raise PipelineError(
                    "device-session v1 invariant rejection: contiguous segment indices"
                )
            if segment["start_frame"] >= segment["end_frame"]:
                raise PipelineError(
                    "device-session v1 invariant rejection: segment frame interval"
                )
            if segment["start_time_seconds"] >= segment["end_time_seconds"]:
                raise PipelineError(
                    "device-session v1 invariant rejection: segment time interval"
                )
            if (
                previous_frame_end is not None
                and segment["start_frame"] != previous_frame_end
            ):
                raise PipelineError(
                    "device-session v1 invariant rejection: segment frame intervals "
                    "are not contiguous"
                )
            if (
                previous_time_end is not None
                and abs(segment["start_time_seconds"] - previous_time_end) > 1e-9
            ):
                raise PipelineError(
                    "device-session v1 invariant rejection: segment time intervals "
                    "are not contiguous"
                )
            if split_eye_frame_start is None:
                split_eye_frame_start = segment["start_frame"]
            split_eye_frame_end = segment["end_frame"]
            previous_frame_end = segment["end_frame"]
            previous_time_end = float(segment["end_time_seconds"])

    total_dropped = 0
    previous_drop_end: int | None = None
    for drop in manifest["integrity"]["drop_events"]:
        if previous_drop_end is not None and drop["start_frame"] <= previous_drop_end:
            raise PipelineError(
                "device-session v1 invariant rejection: drop events overlap or touch"
            )
        if drop["end_frame"] <= drop["start_frame"]:
            raise PipelineError(
                "device-session v1 invariant rejection: drop event frame interval"
            )
        if drop["dropped"] != drop["end_frame"] - drop["start_frame"]:
            raise PipelineError(
                "device-session v1 invariant rejection: dropped count mismatch"
            )
        total_dropped += drop["dropped"]
        previous_drop_end = drop["end_frame"]
    if total_dropped != manifest["integrity"]["dropped_frames"]:
        raise PipelineError(
            "device-session v1 invariant rejection: dropped_frames does not equal "
            "drop event sum"
        )
    if video["layout"] == "split-eyes":
        assert split_eye_frame_start is not None and split_eye_frame_end is not None
        for drop in manifest["integrity"]["drop_events"]:
            if (
                drop["start_frame"] < split_eye_frame_start
                or drop["end_frame"] > split_eye_frame_end
            ):
                raise PipelineError(
                    "device-session v1 invariant rejection: drop event outside "
                    "segment sequence"
                )
        expected_frame_count = (
            split_eye_frame_end
            - split_eye_frame_start
            - manifest["integrity"]["dropped_frames"]
        )
        if manifest["frames"]["count"] != expected_frame_count:
            raise PipelineError(
                "device-session v1 invariant rejection: frames.count mismatch"
            )

    started_at = _api_datetime(manifest["time"]["started_at"])
    ended_at = _api_datetime(manifest["time"]["ended_at"])
    verified_at = _api_datetime(manifest["integrity"]["verified_at"])
    sealed_at = _api_datetime(manifest["sealed_at"])
    if not started_at <= ended_at <= verified_at <= sealed_at:
        raise PipelineError("device-session v1 invariant rejection: timestamp order")
    actual_duration = (ended_at - started_at).total_seconds()
    if (
        "duration_clock" not in manifest["time"]
        and abs(float(manifest["time"]["duration_seconds"]) - actual_duration) > 0.001
    ):
        raise PipelineError("device-session v1 invariant rejection: duration_seconds")
    if measured_semantics:
        expected_effective = (
            0.0
            if manifest["time"]["duration_seconds"] == 0
            else manifest["frames"]["count"] / manifest["time"]["duration_seconds"]
        )
        if abs(camera["effective_fps"] - expected_effective) > 1e-9:
            raise PipelineError("device-session v1 invariant rejection: effective_fps")
        _validate_device_session_v1_quality_policy(manifest)


def _validate_device_session_v1_quality_policy(manifest: dict[str, Any]) -> None:
    integrity = manifest["integrity"]
    quality_policy = integrity.get("quality_policy")
    if quality_policy is None:
        return
    dropped_frames = integrity["dropped_frames"]
    drop_events = integrity["drop_events"]
    if quality_policy.get("policy_id") == "rdk-x5-lossless-v1" and (
        dropped_frames != 0 or drop_events
    ):
        raise PipelineError(
            "device-session v1 invariant rejection: rdk-x5-lossless-v1 "
            "quality_policy forbids dropped frames"
        )

    max_total = quality_policy["max_total_dropped_frames"]
    if dropped_frames > max_total:
        raise PipelineError(
            "device-session v1 invariant rejection: quality_policy "
            "max_total_dropped_frames"
        )
    max_contiguous = quality_policy["max_contiguous_dropped_frames"]
    if any(drop["dropped"] > max_contiguous for drop in drop_events):
        raise PipelineError(
            "device-session v1 invariant rejection: quality_policy "
            "max_contiguous_dropped_frames"
        )
    total_frames = manifest["frames"]["count"] + dropped_frames
    drop_fraction = 0.0 if total_frames == 0 else dropped_frames / total_frames
    if drop_fraction > quality_policy["max_drop_fraction"]:
        raise PipelineError(
            "device-session v1 invariant rejection: quality_policy max_drop_fraction"
        )
    window_seconds = float(quality_policy["window_seconds"])
    max_window = quality_policy["max_dropped_frames_per_window"]
    for anchor in (float(drop["at_time_seconds"]) for drop in drop_events):
        window_drops = sum(
            drop["dropped"]
            for drop in drop_events
            if anchor <= float(drop["at_time_seconds"]) < anchor + window_seconds
        )
        if window_drops > max_window:
            raise PipelineError(
                "device-session v1 invariant rejection: quality_policy "
                "max_dropped_frames_per_window"
            )


def _validate_device_session_v2_audio_invariants(manifest: dict[str, Any]) -> None:
    imu = manifest["imu"]
    if imu["units"] != "raw_int16" or imu["coordinate_frame"] != "raw_device_axes":
        raise PipelineError(
            "device-session v2 invariant rejection: raw IMU must use raw_int16 in "
            "raw_device_axes"
        )

    audio = manifest["audio"]
    state = audio["state"]
    if state == "not_recorded":
        recorded_fields = {
            "codec",
            "container",
            "sample_format",
            "sample_rate",
            "channels",
            "sample_count",
            "sync",
            "segments",
        }
        if any(field in audio for field in recorded_fields):
            raise PipelineError(
                "device-session v2 invariant rejection: not_recorded audio carries "
                "recorded audio fields"
            )
        return
    if state != "recorded":
        raise PipelineError(
            f"device-session v2 invariant rejection: unsupported audio state {state!r}"
        )
    if (
        audio["codec"] != "pcm_s16le"
        or audio["container"] != "wav"
        or audio["sample_format"] != "S16_LE"
    ):
        raise PipelineError(
            "device-session v2 invariant rejection: recorded audio must declare "
            "pcm_s16le WAV S16_LE"
        )

    sample_count = audio["sample_count"]
    if sample_count <= 0:
        raise PipelineError(
            "device-session v2 invariant rejection: recorded audio sample_count"
        )
    sample_rate = audio["sample_rate"]
    channels = audio["channels"]
    bytes_per_pcm_frame = channels * 2
    duration_tolerance = (1.0 / sample_rate) + AUDIO_DURATION_EPSILON_SECONDS
    previous_sample_end: int | None = None
    previous_time_end: float | None = None
    sample_total = 0
    for expected_index, segment in enumerate(audio["segments"]):
        if segment["index"] != expected_index:
            raise PipelineError(
                "device-session v2 invariant rejection: audio segment indices"
            )
        if segment["start_sample"] >= segment["end_sample"]:
            raise PipelineError(
                "device-session v2 invariant rejection: audio segment sample interval"
            )
        if segment["start_time_seconds"] >= segment["end_time_seconds"]:
            raise PipelineError(
                "device-session v2 invariant rejection: audio segment time interval"
            )
        if (
            previous_sample_end is not None
            and segment["start_sample"] != previous_sample_end
        ):
            raise PipelineError(
                "device-session v2 invariant rejection: audio segment sample "
                "intervals are not contiguous"
            )
        if (
            previous_time_end is not None
            and abs(segment["start_time_seconds"] - previous_time_end) > 1e-9
        ):
            raise PipelineError(
                "device-session v2 invariant rejection: audio segment time "
                "intervals are not contiguous"
            )
        segment_frames = segment["end_sample"] - segment["start_sample"]
        segment_duration = float(segment["end_time_seconds"]) - float(
            segment["start_time_seconds"]
        )
        expected_segment_duration = segment_frames / sample_rate
        if abs(segment_duration - expected_segment_duration) > duration_tolerance:
            raise PipelineError(
                "device-session v2 invariant rejection: audio segment duration "
                "does not match sample_rate"
            )
        if segment_frames > MAX_AUDIO_BYTE_COUNT // bytes_per_pcm_frame:
            raise PipelineError(
                "device-session v2 invariant rejection: audio payload size"
            )
        expected_payload_bytes = segment_frames * bytes_per_pcm_frame
        if segment["pcm_payload_bytes"] != expected_payload_bytes:
            raise PipelineError(
                "device-session v2 invariant rejection: audio pcm_payload_bytes"
            )
        expected_file_bytes = segment["pcm_payload_bytes"] + segment["wav_header_bytes"]
        if expected_file_bytes > MAX_AUDIO_BYTE_COUNT:
            raise PipelineError(
                "device-session v2 invariant rejection: audio artifact byte count"
            )
        artifact = segment["artifact"]
        if artifact["bytes"] != expected_file_bytes:
            raise PipelineError(
                "device-session v2 invariant rejection: audio artifact bytes must "
                "equal pcm_payload_bytes + wav_header_bytes"
            )
        sample_total += segment_frames
        previous_sample_end = segment["end_sample"]
        previous_time_end = float(segment["end_time_seconds"])

    segments = audio["segments"]
    if segments[0]["start_sample"] != 0:
        raise PipelineError(
            "device-session v2 invariant rejection: audio sample domain starts "
            "after zero"
        )
    if sample_total != sample_count:
        raise PipelineError("device-session v2 invariant rejection: audio.sample_count")
    sync = audio["sync"]
    if abs(sync["start_time_seconds"] - segments[0]["start_time_seconds"]) > 1e-9:
        raise PipelineError(
            "device-session v2 invariant rejection: audio sync start_time_seconds"
        )
    if abs(sync["end_time_seconds"] - segments[-1]["end_time_seconds"]) > 1e-9:
        raise PipelineError(
            "device-session v2 invariant rejection: audio sync end_time_seconds"
        )
    sync_duration = float(sync["end_time_seconds"]) - float(sync["start_time_seconds"])
    expected_sync_duration = sample_count / sample_rate
    if abs(sync_duration - expected_sync_duration) > duration_tolerance:
        raise PipelineError(
            "device-session v2 invariant rejection: audio sync duration"
        )
    duration = float(manifest["time"]["duration_seconds"])
    if not (0 <= sync["start_time_seconds"] < sync["end_time_seconds"] <= duration):
        raise PipelineError(
            "device-session v2 invariant rejection: audio sync interval"
        )


def _validate_device_session_v2_invariants(manifest: dict[str, Any]) -> None:
    _validate_device_session_v1_invariants(manifest)
    _validate_device_session_v2_audio_invariants(manifest)


def _read_device_session_v1(
    directory: Path,
    manifest_path: Path,
    manifest_bytes: bytes,
    manifest: dict[str, Any],
) -> Session:
    _validate_schema(_device_session_v1_validator(), manifest, "device-session v1")
    _validate_device_session_v1_invariants(manifest)

    expected_top = {
        "schema",
        "manifest_id",
        "sealed",
        "sealed_at",
        "session_id",
        "volume_id",
        "capture_mode",
        "display_name",
        "device",
        "time",
        "take",
        "camera",
        "video",
        "imu",
        "frames",
        "logs",
        "integrity",
    }
    _require_device_session_v1(set(manifest) == expected_top, "top-level fields")
    _require_device_session_v1(manifest.get("sealed") is True, "sealed must be true")
    manifest_id = _require_string(
        manifest.get("manifest_id"), "manifest_id", UUID_V7_PATTERN
    )
    session_id = _object_identity(
        _require_string(manifest.get("session_id"), "session_id", UUID_V7_PATTERN),
        "session_id",
    )
    volume_id = _require_string(manifest.get("volume_id"), "volume_id", UUID_V4_PATTERN)
    sealed_at = _require_string(manifest.get("sealed_at"), "sealed_at")

    device = _require_dict(manifest.get("device"), "device")
    device_id = _require_string(
        device.get("device_id"), "device.device_id", UUID_V4_PATTERN
    )
    device_label = _require_string(device.get("device_label"), "device.device_label")
    _require_device_session_v1(
        re.fullmatch(r"YLX-[0-9A-F]{8}", device_label) is not None,
        "device.device_label",
    )

    time_fields = _require_dict(manifest.get("time"), "time")
    duration_seconds = time_fields.get("duration_seconds")
    _require_device_session_v1(
        isinstance(duration_seconds, (int, float))
        and not isinstance(duration_seconds, bool)
        and math.isfinite(float(duration_seconds))
        and float(duration_seconds) >= 0,
        "time.duration_seconds",
    )
    started_at = _require_string(time_fields.get("started_at"), "time.started_at")

    take = _require_dict(manifest.get("take"), "take")
    take_id = _require_string(take.get("take_id"), "take.take_id", UUID_V7_PATTERN)
    sequence = take.get("sequence")
    continuation_of = take.get("continuation_of")
    _require_device_session_v1(
        isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 1,
        "take.sequence",
    )
    if sequence == 1:
        _require_device_session_v1(continuation_of is None, "take.continuation_of")
    else:
        _require_string(continuation_of, "take.continuation_of", UUID_V7_PATTERN)

    camera = _require_dict(manifest.get("camera"), "camera")
    _require_device_session_v1(
        camera.get("coordinate_frame") == "opencv_optical",
        "camera.coordinate_frame",
    )
    for field in ("width", "height", "eye_width", "frame_decimation"):
        value = camera.get(field)
        _require_device_session_v1(
            isinstance(value, int) and not isinstance(value, bool) and value > 0,
            f"camera.{field}",
        )
    for field in ("sensor_fps", "effective_fps"):
        value = camera.get(field)
        _require_device_session_v1(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0,
            f"camera.{field}",
        )

    seen_paths: set[str] = set()
    artifacts: list[Artifact] = []
    video = _require_dict(manifest.get("video"), "video")
    layout = _require_string(video.get("layout"), "video.layout")
    video_codec = _require_string(video.get("codec"), "video.codec")
    if layout == "raw-side-by-side":
        _require_device_session_v1(
            video_codec == "mjpeg" and video.get("continuous") is True,
            "raw-side-by-side video fields",
        )
        artifacts.append(
            _device_session_artifact(
                video.get("artifact"),
                expected_role="video.raw-side-by-side",
                expected_media_type="video/x-motion-jpeg",
                internal_role="video_stereo",
                seen_paths=seen_paths,
            )
        )
    elif layout == "split-eyes":
        _require_device_session_v1(
            video_codec == "h264" and video.get("container") == "mp4",
            "split-eyes video fields",
        )
        segments = video.get("segments")
        _require_device_session_v1(
            isinstance(segments, list) and bool(segments), "video.segments"
        )
        seen_segment_indices: set[int] = set()
        for segment in segments:
            segment = _require_dict(segment, "video segment")
            index = segment.get("index")
            _require_device_session_v1(
                isinstance(index, int)
                and not isinstance(index, bool)
                and index >= 0
                and index not in seen_segment_indices,
                "video segment index",
            )
            seen_segment_indices.add(index)
            pair = _require_dict(segment.get("artifacts"), "video segment artifacts")
            artifacts.extend(
                (
                    _device_session_artifact(
                        pair.get("left"),
                        expected_role="video.left",
                        expected_media_type="video/mp4",
                        internal_role="video_left",
                        seen_paths=seen_paths,
                        segment_index=index,
                    ),
                    _device_session_artifact(
                        pair.get("right"),
                        expected_role="video.right",
                        expected_media_type="video/mp4",
                        internal_role="video_right",
                        seen_paths=seen_paths,
                        segment_index=index,
                    ),
                )
            )
    else:
        raise PipelineError(f"unsupported device-session v1 video layout {layout!r}")

    imu = _require_dict(manifest.get("imu"), "imu")
    _require_device_session_v1(
        imu.get("units") == "raw_int16"
        and imu.get("coordinate_frame") == "opencv_optical",
        "imu declaration",
    )
    artifacts.append(
        _device_session_artifact(
            imu.get("artifact"),
            expected_role="imu.samples",
            expected_media_type="application/x-ndjson",
            internal_role="imu.samples",
            seen_paths=seen_paths,
        )
    )
    frames = _require_dict(manifest.get("frames"), "frames")
    artifacts.append(
        _device_session_artifact(
            frames.get("artifact"),
            expected_role="frames.index",
            expected_media_type="application/x-ndjson",
            internal_role="frames.index",
            seen_paths=seen_paths,
        )
    )
    logs = manifest.get("logs")
    _require_device_session_v1(isinstance(logs, list), "logs")
    for log_artifact in logs:
        log = _device_session_artifact(
            log_artifact,
            expected_role=None,
            expected_media_type=None,
            internal_role=_require_dict(log_artifact, "log artifact").get("role"),
            seen_paths=seen_paths,
        )
        _require_device_session_v1(log.role.startswith("log."), "log role")
        artifacts.append(log)

    integrity = _require_dict(manifest.get("integrity"), "integrity")
    _require_device_session_v1(
        integrity.get("fatal_errors") == [],
        "integrity",
    )

    return Session(
        directory=directory,
        source_directory_name=directory.name,
        session_id=session_id,
        captured_at=started_at,
        duration_seconds=float(duration_seconds),
        artifacts=tuple(artifacts),
        camera=dict(camera),
        source_manifest_path=manifest_path,
        source_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_manifest_revision=f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        source_signature={"status": "device_session_v1_sealed"},
        source_manifest_name="manifest.json",
        source_manifest_schema=DEVICE_SESSION_V1_SCHEMA,
        source_manifest_size_bytes=len(manifest_bytes),
        manifest_id=manifest_id,
        volume_id=volume_id,
        source_manifest_sealed_at=sealed_at,
        device={"device_id": device_id, "device_label": device_label},
        take={
            "take_id": take_id,
            "sequence": sequence,
            "continuation_of": continuation_of,
        },
        source_video_layout=layout,
        declared_source_codec=video_codec,
        source_declarations={
            "camera_coordinate_frame": camera["coordinate_frame"],
            "imu_coordinate_frame": imu["coordinate_frame"],
            "imu_units": imu["units"],
            "video_codec": video_codec,
        },
    )


def _device_session_v2_source_audio(audio: dict[str, Any]) -> dict[str, Any]:
    if audio["state"] == "not_recorded":
        return {"state": "not_recorded", "reason": audio["reason"]}
    return {
        "state": "recorded",
        "source_artifact_ids": [
            segment["artifact"]["artifact_id"] for segment in audio["segments"]
        ],
        "role": "audio.wav",
        "codec": audio["codec"],
        "container": audio["container"],
        "sample_format": audio["sample_format"],
        "sample_rate": audio["sample_rate"],
        "channels": audio["channels"],
        "sample_count": audio["sample_count"],
        "sync": dict(audio["sync"]),
        "segments": [
            {
                "index": segment["index"],
                "start_sample": segment["start_sample"],
                "end_sample": segment["end_sample"],
                "start_time_seconds": segment["start_time_seconds"],
                "end_time_seconds": segment["end_time_seconds"],
                "pcm_payload_bytes": segment["pcm_payload_bytes"],
                "wav_header_bytes": segment["wav_header_bytes"],
                "artifact": {
                    "artifact_id": segment["artifact"]["artifact_id"],
                    "role": segment["artifact"]["role"],
                    "path": segment["artifact"]["path"],
                    "media_type": segment["artifact"]["media_type"],
                    "bytes": segment["artifact"]["bytes"],
                    "sha256": segment["artifact"]["sha256"],
                },
            }
            for segment in audio["segments"]
        ],
    }


def _read_device_session_v2(
    directory: Path,
    manifest_path: Path,
    manifest_bytes: bytes,
    manifest: dict[str, Any],
) -> Session:
    _validate_schema(_device_session_v2_validator(), manifest, "device-session v2")
    _validate_device_session_v2_invariants(manifest)

    expected_top = {
        "schema",
        "manifest_id",
        "sealed",
        "sealed_at",
        "session_id",
        "volume_id",
        "capture_mode",
        "display_name",
        "device",
        "time",
        "take",
        "camera",
        "video",
        "imu",
        "frames",
        "audio",
        "logs",
        "integrity",
    }
    _require_device_session_v1(set(manifest) == expected_top, "top-level fields")
    _require_device_session_v1(manifest.get("sealed") is True, "sealed must be true")
    manifest_id = _require_string(
        manifest.get("manifest_id"), "manifest_id", UUID_V7_PATTERN
    )
    session_id = _object_identity(
        _require_string(manifest.get("session_id"), "session_id", UUID_V7_PATTERN),
        "session_id",
    )
    volume_id = _require_string(manifest.get("volume_id"), "volume_id", UUID_V4_PATTERN)
    sealed_at = _require_string(manifest.get("sealed_at"), "sealed_at")

    device = _require_dict(manifest.get("device"), "device")
    device_id = _require_string(
        device.get("device_id"), "device.device_id", UUID_V4_PATTERN
    )
    device_label = _require_string(device.get("device_label"), "device.device_label")
    _require_device_session_v1(
        re.fullmatch(r"YLX-[0-9A-F]{8}", device_label) is not None,
        "device.device_label",
    )

    time_fields = _require_dict(manifest.get("time"), "time")
    duration_seconds = time_fields.get("duration_seconds")
    _require_device_session_v1(
        isinstance(duration_seconds, (int, float))
        and not isinstance(duration_seconds, bool)
        and math.isfinite(float(duration_seconds))
        and float(duration_seconds) >= 0,
        "time.duration_seconds",
    )
    started_at = _require_string(time_fields.get("started_at"), "time.started_at")

    take = _require_dict(manifest.get("take"), "take")
    take_id = _require_string(take.get("take_id"), "take.take_id", UUID_V7_PATTERN)
    sequence = take.get("sequence")
    continuation_of = take.get("continuation_of")
    _require_device_session_v1(
        isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 1,
        "take.sequence",
    )
    if sequence == 1:
        _require_device_session_v1(continuation_of is None, "take.continuation_of")
    else:
        _require_string(continuation_of, "take.continuation_of", UUID_V7_PATTERN)

    camera = _require_dict(manifest.get("camera"), "camera")
    _require_device_session_v1(
        camera.get("coordinate_frame") == "opencv_optical",
        "camera.coordinate_frame",
    )
    for field in ("width", "height", "eye_width", "frame_decimation"):
        value = camera.get(field)
        _require_device_session_v1(
            isinstance(value, int) and not isinstance(value, bool) and value > 0,
            f"camera.{field}",
        )
    for field in ("sensor_fps", "effective_fps"):
        value = camera.get(field)
        _require_device_session_v1(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0,
            f"camera.{field}",
        )

    seen_paths: set[str] = set()
    artifacts: list[Artifact] = []
    video = _require_dict(manifest.get("video"), "video")
    layout = _require_string(video.get("layout"), "video.layout")
    video_codec = _require_string(video.get("codec"), "video.codec")
    if layout == "raw-side-by-side":
        _require_device_session_v1(
            video_codec == "mjpeg" and video.get("continuous") is True,
            "raw-side-by-side video fields",
        )
        artifacts.append(
            _device_session_artifact(
                video.get("artifact"),
                expected_role="video.raw-side-by-side",
                expected_media_type="video/x-motion-jpeg",
                internal_role="video_stereo",
                seen_paths=seen_paths,
            )
        )
    elif layout == "split-eyes":
        _require_device_session_v1(
            video_codec == "h264" and video.get("container") == "mp4",
            "split-eyes video fields",
        )
        segments = video.get("segments")
        _require_device_session_v1(
            isinstance(segments, list) and bool(segments), "video.segments"
        )
        seen_segment_indices: set[int] = set()
        for segment in segments:
            segment = _require_dict(segment, "video segment")
            index = segment.get("index")
            _require_device_session_v1(
                isinstance(index, int)
                and not isinstance(index, bool)
                and index >= 0
                and index not in seen_segment_indices,
                "video segment index",
            )
            seen_segment_indices.add(index)
            pair = _require_dict(segment.get("artifacts"), "video segment artifacts")
            artifacts.extend(
                (
                    _device_session_artifact(
                        pair.get("left"),
                        expected_role="video.left",
                        expected_media_type="video/mp4",
                        internal_role="video_left",
                        seen_paths=seen_paths,
                        segment_index=index,
                    ),
                    _device_session_artifact(
                        pair.get("right"),
                        expected_role="video.right",
                        expected_media_type="video/mp4",
                        internal_role="video_right",
                        seen_paths=seen_paths,
                        segment_index=index,
                    ),
                )
            )
    else:
        raise PipelineError(f"unsupported device-session v2 video layout {layout!r}")

    imu = _require_dict(manifest.get("imu"), "imu")
    _require_device_session_v1(
        imu.get("units") == "raw_int16"
        and imu.get("coordinate_frame") == "raw_device_axes",
        "imu declaration",
    )
    artifacts.append(
        _device_session_artifact(
            imu.get("artifact"),
            expected_role="imu.samples",
            expected_media_type="application/x-ndjson",
            internal_role="imu.samples",
            seen_paths=seen_paths,
        )
    )
    frames = _require_dict(manifest.get("frames"), "frames")
    artifacts.append(
        _device_session_artifact(
            frames.get("artifact"),
            expected_role="frames.index",
            expected_media_type="application/x-ndjson",
            internal_role="frames.index",
            seen_paths=seen_paths,
        )
    )

    audio = _require_dict(manifest.get("audio"), "audio")
    if audio["state"] == "recorded":
        for segment in audio["segments"]:
            index = segment["index"]
            artifacts.append(
                _device_session_artifact(
                    segment.get("artifact"),
                    expected_role="audio.wav",
                    expected_media_type="audio/wav",
                    internal_role="audio.wav",
                    seen_paths=seen_paths,
                    segment_index=index,
                )
            )
    source_audio = _device_session_v2_source_audio(audio)

    logs = manifest.get("logs")
    _require_device_session_v1(isinstance(logs, list), "logs")
    for log_artifact in logs:
        log = _device_session_artifact(
            log_artifact,
            expected_role=None,
            expected_media_type=None,
            internal_role=_require_dict(log_artifact, "log artifact").get("role"),
            seen_paths=seen_paths,
        )
        _require_device_session_v1(log.role.startswith("log."), "log role")
        artifacts.append(log)

    integrity = _require_dict(manifest.get("integrity"), "integrity")
    _require_device_session_v1(
        integrity.get("fatal_errors") == [],
        "integrity",
    )

    return Session(
        directory=directory,
        source_directory_name=directory.name,
        session_id=session_id,
        captured_at=started_at,
        duration_seconds=float(duration_seconds),
        artifacts=tuple(artifacts),
        camera=dict(camera),
        source_manifest_path=manifest_path,
        source_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_manifest_revision=f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        source_signature={"status": "device_session_v2_sealed"},
        source_manifest_name="manifest.json",
        source_manifest_schema=DEVICE_SESSION_V2_SCHEMA,
        source_manifest_size_bytes=len(manifest_bytes),
        manifest_id=manifest_id,
        volume_id=volume_id,
        source_manifest_sealed_at=sealed_at,
        device={"device_id": device_id, "device_label": device_label},
        take={
            "take_id": take_id,
            "sequence": sequence,
            "continuation_of": continuation_of,
        },
        source_video_layout=layout,
        declared_source_codec=video_codec,
        source_declarations={
            "camera_coordinate_frame": camera["coordinate_frame"],
            "imu_coordinate_frame": imu["coordinate_frame"],
            "imu_units": imu["units"],
            "video_codec": video_codec,
            "audio": source_audio,
        },
        source_audio=source_audio,
    )


def _dispatch_root_manifest(
    directory: Path, manifest_path: Path, manifest_bytes: bytes, manifest: Any
) -> Session:
    if not isinstance(manifest, dict):
        raise PipelineError("manifest.json must be an object")
    schema = manifest.get("schema")
    if schema == DEVICE_SESSION_V1_SCHEMA:
        return _read_device_session_v1(
            directory, manifest_path, manifest_bytes, manifest
        )
    if schema == DEVICE_SESSION_V2_SCHEMA:
        return _read_device_session_v2(
            directory, manifest_path, manifest_bytes, manifest
        )
    if isinstance(schema, str) and schema.startswith("ylx.device-session.v"):
        raise PipelineError(f"unsupported device-session schema {schema}")
    raise PipelineError("manifest.json has no supported schema")


def _validate_closed_device_session_take_graph(sessions: list[Session]) -> None:
    device_sessions = [
        session
        for session in sessions
        if session.source_manifest_schema
        in {DEVICE_SESSION_V1_SCHEMA, DEVICE_SESSION_V2_SCHEMA}
    ]
    if not device_sessions:
        return

    by_session_id: dict[str, Session] = {}
    by_manifest_id: dict[str, Session] = {}
    by_take: dict[str, list[Session]] = {}
    for session in device_sessions:
        if session.session_id in by_session_id:
            raise PipelineError(
                "device-session v1 take graph rejection: duplicate session_id "
                f"{session.session_id}"
            )
        by_session_id[session.session_id] = session
        if session.manifest_id in by_manifest_id:
            raise PipelineError(
                "device-session v1 take graph rejection: duplicate manifest_id "
                f"{session.manifest_id}"
            )
        by_manifest_id[session.manifest_id] = session
        by_take.setdefault(session.take["take_id"], []).append(session)

    for start in device_sessions:
        visited: set[str] = set()
        current = start
        while current.take["continuation_of"] is not None:
            predecessor_id = current.take["continuation_of"]
            if predecessor_id in visited:
                raise PipelineError(
                    "device-session v1 take graph rejection: continuation graph "
                    f"contains a cycle at {predecessor_id}"
                )
            visited.add(predecessor_id)
            predecessor = by_session_id.get(predecessor_id)
            if predecessor is None:
                raise PipelineError(
                    "device-session v1 take graph rejection: take predecessor "
                    f"{predecessor_id} is absent from the closed corpus"
                )
            current = predecessor

    successor_by_predecessor: dict[str, Session] = {}
    for take_id, members in by_take.items():
        sequence_counts: dict[int, int] = {}
        for member in members:
            sequence = member.take["sequence"]
            sequence_counts[sequence] = sequence_counts.get(sequence, 0) + 1
        duplicate_sequences = sorted(
            sequence for sequence, count in sequence_counts.items() if count > 1
        )
        if duplicate_sequences:
            raise PipelineError(
                "device-session v1 take graph rejection: duplicate take sequence "
                f"values {duplicate_sequences} for take {take_id}"
            )
        sequences = sorted(sequence_counts)
        if sequences != list(range(1, len(members) + 1)):
            raise PipelineError(
                "device-session v1 take graph rejection: take sequence values are "
                "not contiguous from one in the closed corpus"
            )
        roots = [member for member in members if member.take["sequence"] == 1]
        if len(roots) != 1:
            raise PipelineError(
                "device-session v1 take graph rejection: graph must have exactly "
                "one root in the closed corpus"
            )
        for session in members:
            sequence = session.take["sequence"]
            predecessor_id = session.take["continuation_of"]
            if sequence == 1:
                if predecessor_id is not None:
                    raise PipelineError(
                        "device-session v1 take graph rejection: sequence 1 "
                        "must not name a predecessor"
                    )
                continue
            predecessor = by_session_id.get(predecessor_id)
            if predecessor is None:
                raise PipelineError(
                    "device-session v1 take graph rejection: take predecessor "
                    f"{predecessor_id} is absent from the closed corpus"
                )
            if predecessor.take["take_id"] != take_id:
                raise PipelineError(
                    "device-session v1 take graph rejection: predecessor belongs "
                    "to a different take_id"
                )
            if predecessor.take["sequence"] + 1 != sequence:
                raise PipelineError(
                    "device-session v1 take graph rejection: take sequence is not "
                    "predecessor.sequence + 1"
                )
            if predecessor.device != session.device:
                raise PipelineError(
                    "device-session v1 take graph rejection: take crosses "
                    "canonical device_id/device_label"
                )
            if predecessor_id in successor_by_predecessor:
                first = successor_by_predecessor[predecessor_id]
                raise PipelineError(
                    "device-session v1 take graph rejection: take graph branches "
                    f"from {predecessor_id}; first successor is {first.session_id}"
                )
            successor_by_predecessor[predecessor_id] = session
            if _api_datetime(predecessor.source_manifest_sealed_at) > _api_datetime(
                session.captured_at
            ):
                raise PipelineError(
                    "device-session v1 take graph rejection: predecessor was not "
                    "sealed before continuation started"
                )


def _oldest_first_session_key(session: Session) -> tuple[datetime, str]:
    try:
        captured_at = _api_datetime(session.captured_at)
    except PipelineError:
        captured_at = datetime.max.replace(tzinfo=UTC)
    return captured_at, session.source_directory_name


def read_sessions(
    recordings: Path,
    registry: Any = None,
    external_device_identity: str | None = None,
    allow_unsigned: bool = False,
) -> list[Session]:
    """Every published session on the card, oldest first.

    A directory without a publication manifest is skipped rather than guessed
    at: an interrupted capture leaves a partial tree behind, and inventing an
    inventory for it would mean uploading whatever happens to be on disk.
    """
    if recordings.is_symlink():
        raise PipelineError(f"recordings directory {recordings} must not be a symlink")
    sessions = []
    try:
        directories = sorted(
            p for p in recordings.iterdir() if p.is_dir() and not p.is_symlink()
        )
    except OSError as error:
        raise PipelineError(
            f"{recordings} cannot be listed ({error.strerror}); is the card still inserted?"
        ) from error
    for directory in directories:
        root_manifest_path = directory / "manifest.json"
        if root_manifest_path.is_symlink():
            print(f"  skip {directory.name}: root manifest is a symlink")
            continue
        if root_manifest_path.is_file():
            manifest_bytes = _read_regular_file(
                directory, Path("manifest.json"), "manifest.json"
            )
            manifest = parse_strict_json(manifest_bytes, "manifest.json")
            sessions.append(
                _dispatch_root_manifest(
                    directory, root_manifest_path, manifest_bytes, manifest
                )
            )
            continue

        manifest_path = directory / "publication_manifest.json"
        if manifest_path.is_symlink():
            print(f"  skip {directory.name}: publication manifest is a symlink")
            continue
        if not manifest_path.is_file():
            print(
                f"  skip {directory.name}: no publication manifest (capture never finished)"
            )
            continue
        manifest_bytes = _read_regular_file(
            directory, Path("publication_manifest.json"), "publication_manifest.json"
        )
        manifest = parse_strict_json(manifest_bytes, "publication_manifest.json")
        if not isinstance(manifest, dict):
            raise PipelineError("publication_manifest.json must be an object")
        if not manifest.get("integrity_ok"):
            print(
                f"  skip {directory.name}: the card marks this publication as not intact"
            )
            continue
        sessions.append(
            _session_from_publication_manifest(
                directory=directory,
                source_directory_name=directory.name,
                manifest_path=manifest_path,
                manifest_bytes=manifest_bytes,
                manifest=manifest,
                registry=registry,
                external_device_identity=external_device_identity,
                allow_unsigned=allow_unsigned,
                check_source_signature=True,
            )
        )
    _validate_closed_device_session_take_graph(sessions)
    return sorted(sessions, key=_oldest_first_session_key)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _open_regular_beneath(root: Path, relative: Path, description: str) -> int:
    """Open one regular file through no-follow directory descriptors.

    The root descriptor remains anchored while every component is opened with
    ``openat``. Replacing an intermediate directory with a symlink after a
    preflight check therefore cannot redirect this read outside the session.
    """
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PipelineError(f"{description} escapes the session directory")
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as error:
        raise PipelineError(
            f"cannot open session directory for {description}: {error.strerror or error}"
        ) from error
    try:
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except OSError as error:
                raise PipelineError(
                    f"cannot open {description}: {error.strerror or error}"
                ) from error
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise PipelineError(
                f"cannot open {description}: {error.strerror or error}"
            ) from error
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            os.close(descriptor)
            raise PipelineError(f"{description} is not a regular file")
        if descriptor_stat.st_nlink != 1:
            os.close(descriptor)
            raise PipelineError(f"{description} is hardlinked")
        return descriptor
    finally:
        os.close(directory_fd)


def _copy_regular_file(
    root: Path, relative: Path, destination: Path, description: str
) -> None:
    """Copy a regular source through an already-open descriptor into a snapshot."""
    descriptor = _open_regular_beneath(root, relative, description)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            os.fdopen(descriptor, "rb", closefd=False) as input_handle,
            destination.open("xb") as output_handle,
        ):
            shutil.copyfileobj(input_handle, output_handle, READ_CHUNK_BYTES)
    except OSError as error:
        raise PipelineError(
            f"copying {description} failed: {error.strerror or error}"
        ) from error
    finally:
        os.close(descriptor)


def _read_regular_file(root: Path, relative: Path, description: str) -> bytes:
    """Read a regular file from one no-follow descriptor."""
    descriptor = _open_regular_beneath(root, relative, description)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _wav_u16(raw: bytes, offset: int, label: str) -> int:
    if offset + 2 > len(raw):
        raise PipelineError(f"{label} WAV header is truncated")
    return int.from_bytes(raw[offset : offset + 2], "little")


def _wav_u32(raw: bytes, offset: int, label: str) -> int:
    if offset + 4 > len(raw):
        raise PipelineError(f"{label} WAV header is truncated")
    return int.from_bytes(raw[offset : offset + 4], "little")


def _parse_pcm_s16le_wav(raw: bytes, label: str) -> dict[str, int]:
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise PipelineError(f"{label} is not a RIFF/WAVE audio file")
    riff_size = _wav_u32(raw, 4, label)
    if riff_size + 8 != len(raw):
        raise PipelineError(f"{label} WAV RIFF size does not match artifact bytes")

    offset = 12
    fmt: dict[str, int] | None = None
    data: dict[str, int] | None = None
    while offset + 8 <= len(raw):
        chunk_id = raw[offset : offset + 4]
        chunk_size = _wav_u32(raw, offset + 4, label)
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_size
        if chunk_end > len(raw):
            raise PipelineError(f"{label} WAV chunk size exceeds artifact bytes")
        if chunk_id == b"fmt ":
            if fmt is not None:
                raise PipelineError(f"{label} WAV has multiple fmt chunks")
            if chunk_size < 16:
                raise PipelineError(f"{label} WAV fmt chunk is too small")
            fmt = {
                "audio_format": _wav_u16(raw, chunk_start, label),
                "channels": _wav_u16(raw, chunk_start + 2, label),
                "sample_rate": _wav_u32(raw, chunk_start + 4, label),
                "byte_rate": _wav_u32(raw, chunk_start + 8, label),
                "block_align": _wav_u16(raw, chunk_start + 12, label),
                "bits_per_sample": _wav_u16(raw, chunk_start + 14, label),
            }
        elif chunk_id == b"data":
            if data is not None:
                raise PipelineError(f"{label} WAV has multiple data chunks")
            data = {
                "wav_header_bytes": chunk_start,
                "pcm_payload_bytes": chunk_size,
                "data_end": chunk_end,
            }
        offset = chunk_end + (chunk_size % 2)

    if offset != len(raw):
        raise PipelineError(f"{label} WAV chunk padding is invalid")
    if fmt is None:
        raise PipelineError(f"{label} WAV lacks fmt chunk")
    if data is None:
        raise PipelineError(f"{label} WAV lacks data chunk")
    if data["data_end"] != len(raw):
        raise PipelineError(f"{label} WAV data chunk must be final")
    return {**fmt, **data}


def _validate_device_session_v2_audio_wav_bytes(
    artifact: Artifact,
    source_audio: dict[str, Any],
    segment: dict[str, Any],
    raw: bytes,
) -> None:
    label = artifact.display_path
    parsed = _parse_pcm_s16le_wav(raw, label)
    if source_audio.get("codec") != "pcm_s16le":
        raise PipelineError(f"{label} audio manifest codec is not pcm_s16le")
    if source_audio.get("container") != "wav":
        raise PipelineError(f"{label} audio manifest container is not wav")
    if source_audio.get("sample_format") != "S16_LE":
        raise PipelineError(f"{label} audio manifest sample_format is not S16_LE")
    if parsed["audio_format"] != 1:
        raise PipelineError(f"{label} WAV audio_format is not PCM")
    if parsed["bits_per_sample"] != 16:
        raise PipelineError(f"{label} WAV bits_per_sample is not 16")
    if parsed["channels"] != source_audio["channels"]:
        raise PipelineError(f"{label} WAV channels do not match manifest")
    if parsed["sample_rate"] != source_audio["sample_rate"]:
        raise PipelineError(f"{label} WAV sample_rate does not match manifest")
    expected_block_align = source_audio["channels"] * 2
    if parsed["block_align"] != expected_block_align:
        raise PipelineError(f"{label} WAV block_align does not match manifest")
    expected_byte_rate = source_audio["sample_rate"] * expected_block_align
    if parsed["byte_rate"] != expected_byte_rate:
        raise PipelineError(f"{label} WAV byte_rate does not match manifest")
    if parsed["wav_header_bytes"] != segment["wav_header_bytes"]:
        raise PipelineError(f"{label} WAV wav_header_bytes do not match manifest")
    if parsed["pcm_payload_bytes"] != segment["pcm_payload_bytes"]:
        raise PipelineError(f"{label} WAV data chunk size does not match manifest")
    expected_total = segment["wav_header_bytes"] + segment["pcm_payload_bytes"]
    if len(raw) != artifact.size_bytes or len(raw) != expected_total:
        raise PipelineError(f"{label} WAV total artifact bytes do not match manifest")


def _device_session_v2_audio_segments_by_path(
    session: Session,
) -> dict[str, dict[str, Any]]:
    if (
        session.source_manifest_schema != DEVICE_SESSION_V2_SCHEMA
        or session.source_audio.get("state") != "recorded"
    ):
        return {}
    return {
        segment["artifact"]["path"]: segment
        for segment in session.source_audio.get("segments", [])
    }


def snapshot_session(session: Session, workdir: Path) -> Session:
    """Freeze verified card inputs before ffmpeg reopens any media path.

    The snapshot is verified again before normalization. A card changed while
    this copy is made therefore fails closed rather than changing the bytes
    that ffmpeg or upload subsequently sees.
    """
    snapshots = workdir / ".ylx-source-snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot_directory = Path(
        tempfile.mkdtemp(prefix=f"{session.session_id}-", dir=snapshots)
    )
    manifest_path = snapshot_directory / session.source_manifest_name
    _copy_regular_file(
        session.directory,
        Path(session.source_manifest_name),
        manifest_path,
        session.source_manifest_name,
    )
    if sha256_of(manifest_path) != session.source_manifest_sha256:
        raise PipelineError(
            f"{session.source_manifest_name} changed while creating source snapshot"
        )
    for artifact in session.artifacts:
        destination = snapshot_directory / artifact.display_path
        _copy_regular_file(
            session.directory,
            Path(artifact.display_path),
            destination,
            artifact.display_path,
        )
        if (
            destination.stat().st_size != artifact.size_bytes
            or sha256_of(destination) != artifact.sha256
        ):
            raise PipelineError(
                f"{artifact.display_path} changed while creating source snapshot"
            )
    return dataclasses.replace(
        session,
        directory=snapshot_directory,
        source_manifest_path=manifest_path,
    )


def verify(session: Session) -> None:
    """Check every declared artifact against the bytes on the card.

    Size is checked first because it is free and rules out most damage; the
    digest is what actually decides. A card that fails here is reported and
    skipped, never repaired.
    """
    # Keep the session identity that discovery accepted bound to this run. A
    # removable card can change between discovery and upload; accepting a
    # replacement publication manifest would break the source provenance.
    try:
        current_manifest_sha256 = hashlib.sha256(
            _read_regular_file(
                session.directory,
                Path(session.source_manifest_name),
                session.source_manifest_name,
            )
        ).hexdigest()
    except OSError as error:
        raise PipelineError(
            f"{session.source_manifest_name} disappeared after discovery"
        ) from error
    if current_manifest_sha256 != session.source_manifest_sha256:
        raise PipelineError(f"{session.source_manifest_name} changed after discovery")

    audio_segments_by_path = _device_session_v2_audio_segments_by_path(session)
    for artifact in session.artifacts:
        descriptor = _open_regular_beneath(
            session.directory, Path(artifact.display_path), artifact.display_path
        )
        audio_segment = audio_segments_by_path.get(artifact.display_path)
        audio_bytes = bytearray() if audio_segment is not None else None
        try:
            digest = hashlib.sha256()
            actual_size = 0
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while chunk := handle.read(READ_CHUNK_BYTES):
                    actual_size += len(chunk)
                    digest.update(chunk)
                    if audio_bytes is not None:
                        audio_bytes.extend(chunk)
        finally:
            os.close(descriptor)
        if actual_size != artifact.size_bytes:
            raise PipelineError(
                f"{artifact.display_path} is {actual_size} bytes, "
                f"the manifest claims {artifact.size_bytes}"
            )
        if digest.hexdigest() != artifact.sha256:
            raise PipelineError(
                f"{artifact.display_path} does not match its declared SHA-256"
            )
        if audio_segment is not None and audio_bytes is not None:
            _validate_device_session_v2_audio_wav_bytes(
                artifact, session.source_audio, audio_segment, bytes(audio_bytes)
            )


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _pipeline_version() -> str:
    try:
        return importlib.metadata.version("ylx-card-pipeline")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


@lru_cache(maxsize=1)
def _pipeline_distribution_sha256() -> str:
    root = Path(__file__).resolve().parent
    entries: list[str] = []
    for relative in ("main.py", "provenance.py"):
        path = root / relative
        if path.is_file():
            entries.append(f"{relative}\0{sha256_of(path)}")
    vendor = root / "vendor"
    if vendor.is_dir():
        for path in sorted(item for item in vendor.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            entries.append(f"{relative}\0{sha256_of(path)}")
    if not entries:
        payload = Path(__file__).read_bytes()
        return hashlib.sha256(payload).hexdigest()
    payload = ("\n".join(entries) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pipeline_tool_metadata() -> dict[str, Any]:
    distribution_sha256 = _pipeline_distribution_sha256()
    return {
        "name": "ylx-card-pipeline",
        "version": _pipeline_version(),
        "build": {
            "build_id": f"runtime-distribution-sha256:{distribution_sha256[:16]}",
            "artifact_sha256": distribution_sha256,
        },
    }


@lru_cache(maxsize=1)
def _ffmpeg_version() -> str:
    completed = subprocess.run(
        ["ffmpeg", "-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise PipelineError(
            "ffmpeg version probe failed: "
            f"{detail[-1] if detail else f'exit {completed.returncode}'}"
        )
    first_line = completed.stdout.strip().splitlines()
    if not first_line:
        raise PipelineError("ffmpeg version probe returned no output")
    return first_line[0]


def run_ffmpeg(argv: list[str], description: str) -> dict[str, Any]:
    """Execute one fully materialized ffmpeg argv and record runtime binding."""
    ffmpeg_version = _ffmpeg_version()
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    exit_status = {
        "code": completed.returncode if completed.returncode >= 0 else None,
        "signal": None if completed.returncode >= 0 else -completed.returncode,
    }
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise PipelineError(
            f"{description} failed: {detail[-1] if detail else f'exit {completed.returncode}'}"
        )
    return {
        "argv": argv,
        "ffmpeg_version": ffmpeg_version,
        "exit_status": exit_status,
    }


def _concat_listing_path(workdir: Path, label: str) -> Path:
    return workdir / f"{label.replace(' ', '-')}-segments.txt"


def concat_file_for(segments: list[Path], workdir: Path, label: str) -> Path:
    """Write ffmpeg's concat list. Paths are quoted for its own parser."""
    listing = _concat_listing_path(workdir, label)
    listing.write_text(
        "".join(f"file '{_ffmpeg_concat_path(path.resolve())}'\n" for path in segments),
        encoding="utf-8",
    )
    return listing


def _ffmpeg_concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _ffmpeg_encode_argv(
    *,
    inputs: list[Path],
    output: Path,
    crf: int,
    preset: str,
    workdir: Path,
    label: str,
    crop: str | None,
) -> list[str]:
    if not inputs:
        raise PipelineError(f"encoding {label} needs at least one input")
    arguments: list[str] = []
    if len(inputs) == 1:
        arguments += ["-i", str(inputs[0])]
    else:
        arguments += [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(_concat_listing_path(workdir, label)),
        ]

    if crop:
        arguments += ["-vf", crop]

    arguments += [
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        NORMALIZED_PIXEL_FORMAT,
        # The whole point: index first, samples after, so a player can start
        # on the first range request instead of crawling the file.
        "-movflags",
        NORMALIZED_MOVFLAGS,
        str(output),
    ]
    return [*FFMPEG_ENCODE_PREFIX, *arguments]


def encode(
    inputs: list[Path],
    output: Path,
    crf: int,
    preset: str,
    workdir: Path,
    label: str,
    crop: str | None,
) -> dict[str, Any]:
    """Transcode one eye to the delivery profile.

    Segments are concatenated at the demuxer rather than encoded separately
    and joined, so the result has one continuous timeline and one encoder
    state instead of a visible seam at every segment boundary.
    """
    if len(inputs) > 1:
        concat_file_for(inputs, workdir, label)
    argv = _ffmpeg_encode_argv(
        inputs=inputs,
        output=output,
        crf=crf,
        preset=preset,
        workdir=workdir,
        label=label,
        crop=crop,
    )
    return run_ffmpeg(argv, f"encoding {label}")


def _normalization_cache_key(
    session: Session, preset: str, rotation_degrees: int
) -> dict[str, Any]:
    return {
        "source_manifest_sha256": session.source_manifest_sha256,
        "source_manifest_revision": session.source_manifest_revision,
        "source_codec": session.source_codec,
        "camera_geometry": {
            field: session.camera.get(field)
            for field in ("width", "height", "left_size", "layout")
        },
        "preset": preset,
        "rotation_degrees": rotation_degrees,
        "codec": "h264",
        "encoder": "libx264",
        "profile": "high",
        "pixel_format": "yuv420p",
        "faststart": True,
        "crf": (
            CRF_FOR_H264_SOURCE
            if session.source_codec == "h264"
            else CRF_FOR_MJPEG_SOURCE
        ),
    }


def _normalization_crf_for_session(session: Session) -> int:
    return (
        CRF_FOR_H264_SOURCE if session.source_codec == "h264" else CRF_FOR_MJPEG_SOURCE
    )


def _cached_normalization_outputs(
    session_work: Path, cache_key: dict[str, Any]
) -> list[Path] | None:
    state_path = session_work / NORMALIZATION_STATE_FILENAME
    if state_path.is_symlink() or not state_path.is_file():
        return None
    try:
        state = parse_strict_json(state_path.read_bytes(), NORMALIZATION_STATE_FILENAME)
    except (OSError, PipelineError):
        return None
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != NORMALIZATION_STATE_SCHEMA
        or state.get("cache_key") != cache_key
        or not isinstance(state.get("outputs"), list)
    ):
        return None

    by_name: dict[str, dict[str, Any]] = {}
    for entry in state["outputs"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            return None
        if entry["name"] in by_name:
            return None
        by_name[entry["name"]] = entry
    if set(by_name) != {"left.mp4", "right.mp4"}:
        return None

    outputs = []
    for name in ("left.mp4", "right.mp4"):
        output = session_work / name
        entry = by_name[name]
        if (
            output.is_symlink()
            or not output.is_file()
            or isinstance(entry.get("size_bytes"), bool)
            or not isinstance(entry.get("size_bytes"), int)
            or not isinstance(entry.get("sha256"), str)
        ):
            return None
        try:
            if (
                output.stat().st_size != entry["size_bytes"]
                or sha256_of(output) != entry["sha256"]
            ):
                return None
        except OSError:
            return None
        outputs.append(output)
    return outputs


def _normalization_execution_record(
    *,
    role: str,
    output: Path,
    source_artifact_ids: list[str],
    execution: Any,
) -> dict[str, Any] | None:
    if not isinstance(execution, dict):
        return None
    argv = execution.get("argv")
    ffmpeg_version = execution.get("ffmpeg_version")
    exit_status = execution.get("exit_status")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
        or not isinstance(ffmpeg_version, str)
        or not ffmpeg_version
        or not isinstance(exit_status, dict)
        or exit_status.get("code") != 0
        or exit_status.get("signal") is not None
    ):
        return None
    return {
        "role": role,
        "source_artifact_ids": source_artifact_ids,
        "output": {
            "name": output.name,
            "bytes": output.stat().st_size,
            "sha256": sha256_of(output),
        },
        "argv": list(argv),
        "ffmpeg_version": ffmpeg_version,
        "exit_status": {"code": 0, "signal": None},
    }


def _write_normalization_state(
    session_work: Path,
    cache_key: dict[str, Any],
    outputs: list[Path],
    executions: list[dict[str, Any]] | None = None,
) -> None:
    state = {
        "schema_version": NORMALIZATION_STATE_SCHEMA,
        "cache_key": cache_key,
        "pipeline": {"tool": _pipeline_tool_metadata()},
        "outputs": [
            {
                "name": output.name,
                "size_bytes": output.stat().st_size,
                "sha256": sha256_of(output),
            }
            for output in outputs
        ],
        "executions": list(executions or []),
    }
    body = (
        json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".normalization-state-", dir=session_work
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, session_work / NORMALIZATION_STATE_FILENAME)
        directory_fd = os.open(
            session_work, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise PipelineError(
            f"writing normalization recovery state failed: {error}"
        ) from error
    finally:
        temporary_path.unlink(missing_ok=True)


def normalize(
    session: Session,
    workdir: Path,
    preset: str,
    rotation_degrees: int = 0,
    reuse_completed: bool = True,
) -> list[Path]:
    """Produce exactly two files — left eye and right eye — in one profile.

    The card publishes stereo two different ways: side-by-side MJPEG passed
    straight through from the sensor, and already-split H.264. Both become
    the same pair of outputs here, which is the entire point of this step.
    """
    if rotation_degrees not in SUPPORTED_ROTATIONS:
        raise PipelineError(
            f"unsupported rotation {rotation_degrees}; choose one of {SUPPORTED_ROTATIONS}"
        )

    session_work = workdir / session.name
    session_work.mkdir(parents=True, exist_ok=True)
    cache_key = _normalization_cache_key(session, preset, rotation_degrees)
    if reuse_completed:
        cached = _cached_normalization_outputs(session_work, cache_key)
        if cached is not None:
            return cached

    stereo = session.videos("video_stereo")
    left = session.videos("video_left")
    right = session.videos("video_right")
    outputs: list[Path] = []
    executions: list[dict[str, Any]] = []

    if stereo:
        width = int(session.camera.get("width") or 0)
        height = int(session.camera.get("height") or 0)
        if width <= 0 or width % 2 != 0:
            raise PipelineError(
                f"side-by-side source needs an even frame width, camera reports {width!r}"
            )
        if height <= 0:
            raise PipelineError(
                f"side-by-side source needs a frame height, got {height!r}"
            )
        eye_width = width // 2
        segments = [a.path_under(session.directory) for a in stereo]
        crf = _normalization_crf_for_session(session)
        # The left half is the left eye, as `session.json`'s
        # `left_right_side_by_side` says. A rig mounted upside down is handled
        # by rotating the whole frame before this split, which swaps the halves
        # along with everything else.
        #
        # Getting this backwards is silent — both files play, both look like
        # the scene, and only the depth is inverted — so it is worth measuring
        # rather than assuming. Disparity direction only answers the question
        # on footage with real near-field content: on a far room with repeating
        # blinds and identical chairs the estimate is noise, and reading a
        # verdict out of that noise is how this code briefly had it backwards.
        for eye, x_offset in (("left", 0), ("right", eye_width)):
            output = session_work / f"{eye}.mp4"
            role = f"video.{eye}"
            filters = []
            if rotation_degrees == 180:
                # Rotate the complete stereo canvas before cropping. Besides
                # correcting orientation, this intentionally swaps the two
                # physical eye positions for an upside-down rig.
                filters.extend(("hflip", "vflip"))
            filters.append(f"crop={eye_width}:{height}:{x_offset}:0")
            execution = encode(
                inputs=segments,
                output=output,
                crf=crf,
                preset=preset,
                workdir=session_work,
                label=f"{session.name} {eye}",
                crop=",".join(filters),
            )
            outputs.append(output)
            record = _normalization_execution_record(
                role=role,
                output=output,
                source_artifact_ids=[
                    _artifact_identity(artifact) for artifact in stereo
                ],
                execution=execution,
            )
            if record is not None:
                executions.append(record)
        _write_normalization_state(session_work, cache_key, outputs, executions)
        return outputs

    if not left or len(left) != len(right):
        raise PipelineError(
            "session has neither side-by-side video nor an evenly paired set of eyes"
        )
    crf = _normalization_crf_for_session(session)
    eye_sources = (
        (("left", right), ("right", left))
        if rotation_degrees == 180
        else (("left", left), ("right", right))
    )
    for eye, artifacts in eye_sources:
        output = session_work / f"{eye}.mp4"
        role = f"video.{eye}"
        execution = encode(
            inputs=[a.path_under(session.directory) for a in artifacts],
            output=output,
            crf=crf,
            preset=preset,
            workdir=session_work,
            label=f"{session.name} {eye}",
            crop="hflip,vflip" if rotation_degrees == 180 else None,
        )
        outputs.append(output)
        record = _normalization_execution_record(
            role=role,
            output=output,
            source_artifact_ids=[
                _artifact_identity(artifact) for artifact in artifacts
            ],
            execution=execution,
        )
        if record is not None:
            executions.append(record)
    _write_normalization_state(session_work, cache_key, outputs, executions)
    return outputs


# --------------------------------------------------------------------------
# Offline SBS export
# --------------------------------------------------------------------------


def _default_sbs_export_crf(session: Session) -> int:
    codec = session.source_codec.lower()
    return (
        CRF_FOR_MJPEG_SOURCE
        if codec in {"mjpeg", "mjpg", "motion-jpeg"}
        else CRF_FOR_H264_SOURCE
    )


def _display_path_is_under_audio(path: str) -> bool:
    return bool(Path(path).parts) and Path(path).parts[0] == "audio"


def _is_audio_artifact(artifact: Artifact) -> bool:
    media_type = (artifact.media_type or "").lower()
    suffix = Path(artifact.display_path).suffix.lower()
    return media_type.startswith("audio/") or (
        _display_path_is_under_audio(artifact.display_path)
        and suffix in SBS_EXPORT_AUDIO_SUFFIXES
    )


def discover_sbs_export_audio_segments(session: Session) -> tuple[Path, ...]:
    manifest_audio = sorted(
        (artifact for artifact in session.artifacts if _is_audio_artifact(artifact)),
        key=lambda artifact: _natural_path_key(artifact.display_path),
    )
    if manifest_audio:
        return tuple(artifact.path_under(session.directory) for artifact in manifest_audio)

    audio_dir = session.directory / "audio"
    if audio_dir.is_symlink() or not audio_dir.is_dir():
        return ()
    return tuple(
        sorted(
            (
                path
                for path in audio_dir.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in SBS_EXPORT_AUDIO_SUFFIXES
            ),
            key=lambda path: _natural_path_key(str(path.relative_to(audio_dir))),
        )
    )


def build_sbs_export_plan(
    session: Session,
    output: Path,
    workdir: Path,
    *,
    preset: str = VIDEO_PRESET,
    crf: int | None = None,
    audio_bitrate: str = SBS_EXPORT_AUDIO_BITRATE,
) -> SbsExportPlan:
    left = tuple(
        artifact.path_under(session.directory) for artifact in session.videos("video_left")
    )
    right = tuple(
        artifact.path_under(session.directory)
        for artifact in session.videos("video_right")
    )
    stereo = tuple(
        artifact.path_under(session.directory)
        for artifact in session.videos("video_stereo")
    )
    audio = discover_sbs_export_audio_segments(session)

    if (left or right) and stereo:
        raise PipelineError(
            "SBS export source mixes video_stereo and video_left/video_right artifacts"
        )

    if left or right:
        if not left or len(left) != len(right):
            raise PipelineError(
                "SBS export requires an evenly paired set of video_left/video_right artifacts"
            )
        _validate_sbs_split_pairing(left, right)
        return SbsExportPlan(
            output=output,
            workdir=workdir,
            preset=preset,
            crf=crf if crf is not None else _default_sbs_export_crf(session),
            audio_bitrate=audio_bitrate,
            mode="split",
            left_segments=left,
            right_segments=right,
            audio_segments=audio,
        )

    if stereo:
        return SbsExportPlan(
            output=output,
            workdir=workdir,
            preset=preset,
            crf=crf if crf is not None else _default_sbs_export_crf(session),
            audio_bitrate=audio_bitrate,
            mode="stereo",
            stereo_segments=stereo,
            audio_segments=audio,
        )

    raise PipelineError(
        "SBS export needs video_left/video_right or video_stereo artifacts"
    )


def _sbs_segment_number(path: Path) -> int | None:
    match = re.search(r"(\d+)$", path.stem)
    return int(match.group(1)) if match else None


def _sbs_segment_number_label(value: int | None) -> str:
    return str(value) if value is not None else "no numeric suffix"


def _validate_sbs_split_pairing(left: tuple[Path, ...], right: tuple[Path, ...]) -> None:
    for index, (left_path, right_path) in enumerate(zip(left, right, strict=True)):
        left_number = _sbs_segment_number(left_path)
        right_number = _sbs_segment_number(right_path)
        if left_number != right_number:
            raise PipelineError(
                "SBS export left/right segment numbers differ at pair "
                f"{index}: {_sbs_segment_number_label(left_number)} vs "
                f"{_sbs_segment_number_label(right_number)}"
            )


def _concat_input_arguments(
    segments: tuple[Path, ...], workdir: Path, label: str
) -> list[str]:
    listing = concat_file_for(list(segments), workdir, label)
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


def build_sbs_export_ffmpeg_arguments(plan: SbsExportPlan) -> list[str]:
    arguments: list[str] = []
    plan.workdir.mkdir(parents=True, exist_ok=True)

    if plan.mode == "split":
        arguments += _concat_input_arguments(
            plan.left_segments, plan.workdir, "sbs-left"
        )
        arguments += _concat_input_arguments(
            plan.right_segments, plan.workdir, "sbs-right"
        )
        audio_input_index = 2
        if plan.has_audio:
            arguments += _concat_input_arguments(
                plan.audio_segments, plan.workdir, "sbs-audio"
            )
        arguments += [
            "-filter_complex",
            "[0:v:0]setpts=PTS-STARTPTS[l];"
            "[1:v:0]setpts=PTS-STARTPTS[r];"
            "[l][r]hstack=inputs=2[v]",
            "-map",
            "[v]",
        ]
    elif plan.mode == "stereo":
        arguments += _concat_input_arguments(
            plan.stereo_segments, plan.workdir, "sbs-stereo"
        )
        audio_input_index = 1
        if plan.has_audio:
            arguments += _concat_input_arguments(
                plan.audio_segments, plan.workdir, "sbs-audio"
            )
        arguments += ["-map", "0:v:0"]
    else:
        raise PipelineError(f"unsupported SBS export plan mode {plan.mode!r}")

    if plan.has_audio:
        arguments += ["-map", f"{audio_input_index}:a:0"]
    else:
        arguments += ["-an"]

    arguments += [
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        plan.preset,
        "-crf",
        str(plan.crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if plan.has_audio:
        arguments += ["-c:a", "aac", "-b:a", plan.audio_bitrate, "-shortest"]
    arguments += ["-sn", "-dn", "-movflags", "+faststart", str(plan.output)]
    return arguments


def _commit_staged_sbs_export(staged_output: Path, final_output: Path) -> None:
    if staged_output.is_symlink() or not staged_output.is_file():
        raise PipelineError("ffmpeg did not produce an SBS export output")
    if staged_output.stat().st_size <= 0:
        raise PipelineError("ffmpeg produced an empty SBS export output")
    os.replace(staged_output, final_output)


def _prepare_sbs_export_output(output: Path, overwrite: bool) -> None:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PipelineError(
            f"cannot create SBS export output directory {output.parent}: {error}"
        ) from error
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise PipelineError(
            f"SBS export output path must be a regular file target: {output}"
        )
    if output.exists() and not overwrite:
        raise PipelineError(f"SBS export output already exists: {output}")


def export_sbs(
    session: Session,
    output: Path,
    *,
    workdir: Path | None = None,
    preset: str = VIDEO_PRESET,
    crf: int | None = None,
    audio_bitrate: str = SBS_EXPORT_AUDIO_BITRATE,
    verify_inputs: bool = True,
    overwrite: bool = False,
) -> Path:
    if verify_inputs:
        verify(session)
    _prepare_sbs_export_output(output, overwrite)

    with tempfile.TemporaryDirectory(
        prefix=".ylx-sbs-export-", dir=output.parent
    ) as staging_directory:
        staging_output = Path(staging_directory) / "output.mp4"
        if workdir is None:
            temporary_workdir = Path(staging_directory)
            plan = build_sbs_export_plan(
                session,
                staging_output,
                temporary_workdir,
                preset=preset,
                crf=crf,
                audio_bitrate=audio_bitrate,
            )
        else:
            workdir.mkdir(parents=True, exist_ok=True)
            plan = build_sbs_export_plan(
                session,
                staging_output,
                workdir,
                preset=preset,
                crf=crf,
                audio_bitrate=audio_bitrate,
            )
        run_ffmpeg(
            [*FFMPEG_ENCODE_PREFIX, *build_sbs_export_ffmpeg_arguments(plan)],
            f"exporting {session.name} as SBS MP4",
        )
        _commit_staged_sbs_export(staging_output, output)
    return output


def export_sbs_from_directory(
    directory: Path,
    output: Path,
    *,
    workdir: Path | None = None,
    preset: str = VIDEO_PRESET,
    crf: int | None = None,
    audio_bitrate: str = SBS_EXPORT_AUDIO_BITRATE,
    overwrite: bool = False,
) -> Path:
    return export_sbs(
        read_publication_session(directory),
        output,
        workdir=workdir,
        preset=preset,
        crf=crf,
        audio_bitrate=audio_bitrate,
        overwrite=overwrite,
    )


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


def object_store():
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    access_key = os.environ.get("S3_ACCESS_KEY")
    secret_key = os.environ.get("S3_SECRET_KEY")
    if not (access_key and secret_key):
        raise PipelineError(
            "object storage is not configured; set S3_ACCESS_KEY and S3_SECRET_KEY"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("S3_REGION", DEFAULT_S3_REGION),
        # Aliyun OSS addresses buckets as a hostname prefix. Left to its own
        # devices boto3 would build a path-style URL against a custom
        # endpoint, which OSS answers with a signature mismatch that reads
        # like a bad key rather than a bad URL.
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            retries={"max_attempts": 3, "mode": "standard"},
            # Recent botocore adds a streaming CRC32 trailer to uploads by
            # default. OSS rejects that framing outright
            # ("STREAMING-UNSIGNED-PAYLOAD-TRAILER is not supported"), so
            # checksums are sent only where the protocol requires them.
            # Integrity is not weakened: every object's SHA-256 is recorded
            # in the manifest and can be checked against the stored bytes.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def file_id_for(display_path: str) -> str:
    """The opaque id the console rebuilds object keys from.

    Derived from the path rather than random so re-running a session
    overwrites its own objects instead of littering the bucket with a fresh
    copy every time.
    """
    return "f-" + hashlib.sha256(display_path.encode()).hexdigest()[:32]


def _object_identity(value: str, field: str) -> str:
    """Reject identities that could escape their one object-key component."""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(character in value for character in ("/", "\\", "\x00"))
    ):
        raise PipelineError(f"invalid {field} for object storage key")
    return value


def _media_type_for(role: str) -> str:
    if role.startswith("video_"):
        return "video/mp4"
    if role == "imu":
        return "application/x-ndjson"
    if role == "metadata":
        return "application/json"
    return "application/octet-stream"


def build_provenance(
    session: Session,
    files: list[dict[str, Any]],
    rotation_degrees: int = 0,
) -> dict[str, Any]:
    """Describe local derivation without claiming the card or bucket attests it."""

    has_stereo = any(artifact.role == "video_stereo" for artifact in session.artifacts)

    def inputs_for(file: dict[str, Any]) -> list[dict[str, str]]:
        role = file["role"]
        if role in PUBLISHED_AUXILIARY_ROLES:
            candidates = (
                artifact
                for artifact in session.artifacts
                if artifact.role == role
                and artifact.display_path == file["display_path"]
            )
        elif has_stereo:
            candidates = (
                artifact
                for artifact in session.artifacts
                if artifact.role == "video_stereo"
            )
        else:
            input_role = role
            if rotation_degrees == 180:
                input_role = {
                    "video_left": "video_right",
                    "video_right": "video_left",
                }.get(role, role)
            candidates = (
                artifact
                for artifact in session.artifacts
                if artifact.role == input_role
            )
        return [
            {"display_path": artifact.display_path, "sha256": artifact.sha256}
            for artifact in candidates
        ]

    return {
        "source_publication_manifest": {
            "path": "publication_manifest.json",
            "sha256": session.source_manifest_sha256,
            "revision": session.source_manifest_revision,
            "session_id": session.session_id,
        },
        "derived_relations": [
            {
                "output_display_path": file["display_path"],
                "output_sha256": file["sha256"],
                "inputs": inputs_for(file),
                "operation": (
                    "copy_verified_source"
                    if file["role"] in PUBLISHED_AUXILIARY_ROLES
                    else "normalize_h264_faststart"
                ),
            }
            for file in files
        ],
    }


@dataclasses.dataclass(frozen=True)
class PublicationLease:
    key: str
    etag: str


def _conditional_delete(client: Any, bucket: str, key: str, etag: str) -> None:
    """Delete only the exact lease generation this publisher created."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
    except (BotoCoreError, ClientError, OSError, TypeError) as error:
        raise PipelineError(
            f"conditional lease delete for {key} failed: {error}"
        ) from error


def _best_effort_conditional_delete(
    client: Any, bucket: str, key: str, etag: str | None
) -> None:
    """Try to remove a failed setup object without ever deleting another owner."""
    if not etag:
        return
    try:
        _conditional_delete(client, bucket, key, etag)
    except PipelineError:
        pass


def _precondition_failed(error: Any) -> bool:
    return error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}


def _server_etag(response: Any) -> str | None:
    """Only the object store may define an ETag used for conditional delete."""
    if isinstance(response, dict) and response.get("ETag"):
        return str(response["ETag"])
    return None


def _recover_stale_publication_lease(
    client: Any,
    bucket: str,
    lease_key: str,
    stale_after_seconds: float,
) -> None:
    """Conditionally remove a lease only after its server timestamp expires."""
    from botocore.exceptions import BotoCoreError, ClientError

    if stale_after_seconds <= 0:
        raise PipelineError("publication lease stale threshold must be positive")
    try:
        head = client.head_object(Bucket=bucket, Key=lease_key)
    except (BotoCoreError, ClientError, OSError, TypeError, AttributeError) as error:
        raise PipelineError(
            f"cannot inspect existing publication lease {lease_key}: {error}"
        ) from error
    etag = _server_etag(head)
    last_modified = head.get("LastModified") if isinstance(head, dict) else None
    if (
        not etag
        or not isinstance(last_modified, datetime)
        or last_modified.tzinfo is None
    ):
        raise PipelineError(
            f"existing publication lease {lease_key} lacks an authoritative ETag or timestamp"
        )
    age_seconds = (datetime.now(UTC) - last_modified.astimezone(UTC)).total_seconds()
    if age_seconds < stale_after_seconds:
        raise PipelineError(
            f"existing publication lease {lease_key} is active "
            f"({max(age_seconds, 0):.0f}s old; stale after {stale_after_seconds:.0f}s)"
        )
    _conditional_delete(client, bucket, lease_key, etag)


def _acquire_publication_lease(
    client: Any,
    bucket: str,
    base: str,
    stale_after_seconds: float = PUBLICATION_LEASE_STALE_AFTER_SECONDS,
) -> PublicationLease:
    """Verify conditional-delete support, then create an exclusive lease."""
    from botocore.exceptions import BotoCoreError, ClientError

    capability_key = f"{base}/__ylx_evidence__/lease-capability-{uuid.uuid4()}"
    body = b"ylx-card-pipeline conditional-lease capability probe"
    capability_created = False
    capability_etag: str | None = None
    try:
        capability = client.put_object(
            Bucket=bucket,
            Key=capability_key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
        capability_created = True
        capability_etag = _server_etag(capability)
        if not capability_etag:
            raise PipelineError(
                "object store did not return an ETag for conditional lease probe; "
                "capability object may require manual removal"
            )
        try:
            second = client.put_object(
                Bucket=bucket,
                Key=capability_key,
                Body=body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if not _precondition_failed(error):
                raise PipelineError(
                    "object store rejected conditional lease probe unexpectedly"
                ) from error
        else:
            capability_etag = _server_etag(second)
            raise PipelineError(
                "object store ignored IfNoneMatch for conditional lease probe"
            )
        _conditional_delete(client, bucket, capability_key, capability_etag)
        capability_created = False
    except (BotoCoreError, ClientError, OSError, TypeError) as error:
        raise PipelineError(
            "object store lacks verified conditional lease deletion"
        ) from error
    finally:
        if capability_created:
            _best_effort_conditional_delete(
                client, bucket, capability_key, capability_etag
            )

    lease_key = f"{base}/__ylx_evidence__/publication.lock"
    lease_body = json.dumps(
        {"lease_id": str(uuid.uuid4()), "created_at": time.time()}
    ).encode()
    lease_created = False
    lease_etag: str | None = None
    acquired = False
    try:
        try:
            response = client.put_object(
                Bucket=bucket,
                Key=lease_key,
                Body=lease_body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if not _precondition_failed(error):
                raise
            _recover_stale_publication_lease(
                client, bucket, lease_key, stale_after_seconds
            )
            response = client.put_object(
                Bucket=bucket,
                Key=lease_key,
                Body=lease_body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        lease_created = True
    except (BotoCoreError, ClientError, OSError, TypeError, PipelineError) as error:
        raise PipelineError(
            f"acquiring publication lease {lease_key} failed: {error}"
        ) from error
    try:
        lease_etag = _server_etag(response)
        if not lease_etag:
            raise PipelineError(
                f"object store did not return an ETag for publication lease {lease_key}; "
                "lock may require manual removal"
            )
        acquired = True
        return PublicationLease(key=lease_key, etag=lease_etag)
    finally:
        if lease_created and not acquired:
            _best_effort_conditional_delete(client, bucket, lease_key, lease_etag)


def _remote_object_matches(
    client: Any,
    bucket: str,
    key: str,
    *,
    size_bytes: int,
    sha256: str,
    media_type: str,
) -> bool:
    """Use immutable upload metadata as the resumable object checkpoint."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except AttributeError:
        # Compatibility fallback for small S3 test doubles and older adapters:
        # publication remains correct, but this object will be uploaded again.
        return False
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return False
        raise PipelineError(
            f"checking resumable object {key} failed: {error}"
        ) from error
    except (BotoCoreError, OSError, TypeError) as error:
        raise PipelineError(
            f"checking resumable object {key} failed: {error}"
        ) from error
    if not isinstance(head, dict):
        return False
    metadata = head.get("Metadata")
    content_type = str(head.get("ContentType") or "").split(";", 1)[0].strip().lower()
    return (
        isinstance(metadata, dict)
        and metadata.get("sha256") == sha256
        and head.get("ContentLength") == size_bytes
        and content_type == media_type.lower()
    )


def _publication_revision(session: Session, files: list[dict[str, Any]]) -> str:
    payload = {
        "schema_version": 1,
        "session_id": session.session_id,
        "captured_at": session.captured_at,
        "duration_seconds": session.duration_seconds,
        "files": files,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _content_object_key(base: str, sha256: str) -> str:
    return f"{base}/f-{sha256}"


def _artifact_identity(artifact: Artifact) -> str:
    return artifact.artifact_id or artifact.sha256


def _source_video_artifact_ids(session: Session, output_role: str) -> list[str]:
    if session.source_video_layout == "raw-side-by-side":
        return [
            _artifact_identity(artifact)
            for artifact in session.artifacts
            if artifact.role == "video_stereo"
        ]
    input_role = {
        "video.left": "video_left",
        "video.right": "video_right",
    }.get(output_role)
    return [
        _artifact_identity(artifact)
        for artifact in (session.videos(input_role) if input_role else ())
    ]


def _expected_normalization_publication_binding(
    session: Session,
    output_role: str,
    output: Path,
    preset: str,
    rotation_degrees: int,
) -> dict[str, Any]:
    eye = output_role.removeprefix("video.")
    if eye not in {"left", "right"}:
        raise PipelineError(f"unsupported normalization role {output_role}")
    crf = _normalization_crf_for_session(session)

    if session.source_video_layout == "raw-side-by-side":
        stereo = session.videos("video_stereo")
        width = int(session.camera.get("width") or 0)
        height = int(session.camera.get("height") or 0)
        if width <= 0 or width % 2 != 0 or height <= 0:
            raise PipelineError(
                "normalization state cannot validate side-by-side geometry"
            )
        eye_width = width // 2
        x_offset = 0 if eye == "left" else eye_width
        filters = []
        if rotation_degrees == 180:
            filters.extend(("hflip", "vflip"))
        filters.append(f"crop={eye_width}:{height}:{x_offset}:0")
        inputs = [artifact.path_under(session.directory) for artifact in stereo]
        source_artifact_ids = [_artifact_identity(artifact) for artifact in stereo]
        crop = ",".join(filters)
    else:
        source_role = {
            ("video.left", 0): "video_left",
            ("video.right", 0): "video_right",
            ("video.left", 180): "video_right",
            ("video.right", 180): "video_left",
        }.get((output_role, rotation_degrees))
        if source_role is None:
            raise PipelineError(
                f"unsupported rotation {rotation_degrees}; choose one of {SUPPORTED_ROTATIONS}"
            )
        artifacts = session.videos(source_role)
        inputs = [artifact.path_under(session.directory) for artifact in artifacts]
        source_artifact_ids = [_artifact_identity(artifact) for artifact in artifacts]
        crop = "hflip,vflip" if rotation_degrees == 180 else None

    if not source_artifact_ids:
        raise PipelineError(f"no source artifacts for {output_role}")
    return {
        "source_artifact_ids": source_artifact_ids,
        "argv": _ffmpeg_encode_argv(
            inputs=inputs,
            output=output,
            crf=crf,
            preset=preset,
            workdir=output.parent,
            label=f"{session.name} {eye}",
            crop=crop,
        ),
    }


def _normalization_state_for_publication(
    session: Session, outputs: list[Path], preset: str, rotation_degrees: int
) -> dict[str, Any]:
    if len(outputs) != 2:
        raise PipelineError("normalization state requires exactly two outputs")
    output_by_role: dict[str, Path] = {}
    parents = {output.parent for output in outputs}
    if len(parents) != 1:
        raise PipelineError("normalization state requires outputs from one work dir")
    for output in outputs:
        if output.stem not in {"left", "right"}:
            raise PipelineError(f"unsupported normalized output {output.name}")
        role = f"video.{output.stem}"
        if role in output_by_role:
            raise PipelineError(f"duplicate normalized output {role}")
        output_by_role[role] = output
    if set(output_by_role) != {"video.left", "video.right"}:
        raise PipelineError("normalization state requires left and right outputs")

    state_path = next(iter(parents)) / NORMALIZATION_STATE_FILENAME
    if state_path.is_symlink() or not state_path.is_file():
        raise PipelineError("normalization state is missing for device-session upload")
    state = parse_strict_json(state_path.read_bytes(), NORMALIZATION_STATE_FILENAME)
    cache_key = _normalization_cache_key(session, preset, rotation_degrees)
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != NORMALIZATION_STATE_SCHEMA
        or state.get("cache_key") != cache_key
        or not isinstance(state.get("outputs"), list)
        or not isinstance(state.get("executions"), list)
    ):
        raise PipelineError("normalization state does not match this publication")

    tool = _require_dict(
        _require_dict(state.get("pipeline"), "pipeline").get("tool"), "tool"
    )
    if tool != _pipeline_tool_metadata():
        raise PipelineError(
            "normalization state pipeline.tool does not match current runtime"
        )

    expected_output_names = {"left.mp4", "right.mp4"}
    outputs_by_name: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(state["outputs"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise PipelineError(
                f"normalization state output inventory mismatch at {index}"
            )
        name = entry["name"]
        if name in outputs_by_name:
            raise PipelineError(f"duplicate normalization state output name {name}")
        if name not in expected_output_names:
            raise PipelineError(f"unexpected normalization state output name {name}")
        outputs_by_name[name] = entry
    if set(outputs_by_name) != {"left.mp4", "right.mp4"}:
        raise PipelineError("normalization state output inventory mismatch")

    expected_execution_roles = {"video.left", "video.right"}
    executions_by_role: dict[str, dict[str, Any]] = {}
    execution_outputs_by_name: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(state["executions"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("role"), str):
            raise PipelineError(
                f"normalization state execution inventory mismatch at {index}"
            )
        role = entry["role"]
        if role in executions_by_role:
            raise PipelineError(f"duplicate normalization state execution role {role}")
        if role not in expected_execution_roles:
            raise PipelineError(f"unexpected normalization state execution role {role}")
        output = _require_dict(entry.get("output"), "execution.output")
        output_name = output.get("name")
        if not isinstance(output_name, str):
            raise PipelineError(
                f"normalization state execution output name mismatch at {index}"
            )
        if output_name in execution_outputs_by_name:
            raise PipelineError(
                f"duplicate normalization state execution output name {output_name}"
            )
        if output_name not in expected_output_names:
            raise PipelineError(
                f"unexpected normalization state execution output name {output_name}"
            )
        executions_by_role[role] = entry
        execution_outputs_by_name[output_name] = entry
    if set(executions_by_role) != {"video.left", "video.right"}:
        raise PipelineError("normalization state execution inventory mismatch")

    current_ffmpeg_version = _ffmpeg_version()
    for role, output in output_by_role.items():
        output_digest = sha256_of(output)
        output_size = output.stat().st_size
        state_output = outputs_by_name[output.name]
        execution = executions_by_role[role]
        execution_output = _require_dict(execution.get("output"), "execution.output")
        if (
            state_output.get("size_bytes") != output_size
            or state_output.get("sha256") != output_digest
            or execution_output.get("name") != output.name
            or execution_output.get("bytes") != output_size
            or execution_output.get("sha256") != output_digest
        ):
            raise PipelineError("normalization state output hash/size mismatch")
        source_ids = execution.get("source_artifact_ids")
        expected_binding = _expected_normalization_publication_binding(
            session, role, output, preset, rotation_degrees
        )
        if source_ids != expected_binding["source_artifact_ids"]:
            raise PipelineError("normalization state source ids mismatch")
        argv = execution.get("argv")
        ffmpeg_version = execution.get("ffmpeg_version")
        exit_status = execution.get("exit_status")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
            or not isinstance(ffmpeg_version, str)
            or not ffmpeg_version
            or exit_status != {"code": 0, "signal": None}
        ):
            raise PipelineError("normalization state execution record is incomplete")
        if argv != expected_binding["argv"]:
            raise PipelineError("normalization state ffmpeg argv mismatch")
        if ffmpeg_version != current_ffmpeg_version:
            raise PipelineError("normalization state ffmpeg version mismatch")
    return state


def _normalization_execution_by_role(
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {entry["role"]: entry for entry in state["executions"]}


def _transform_log_parameters_from_state(
    state: dict[str, Any], session: Session, preset: str, rotation_degrees: int
) -> dict[str, Any]:
    executions = [
        _normalization_execution_by_role(state)[role]
        for role in ("video.left", "video.right")
    ]
    first = executions[0]
    return {
        "command": first["argv"],
        "tool": state["pipeline"]["tool"],
        "environment": {
            "FFMPEG_VERSION": first["ffmpeg_version"],
            "NORMALIZATION_PRESET": preset,
            "ROTATION_DEGREES": str(rotation_degrees),
            "SOURCE_MANIFEST_SHA256": session.source_manifest_sha256,
            "EXECUTION_COUNT": str(len(executions)),
        },
        "exit_status": {"code": 0, "signal": None},
    }


def _v2_normalization_provenance(
    session: Session,
    output_role: str,
    preset: str,
    rotation_degrees: int,
    source_artifact_ids: list[str] | None = None,
) -> dict[str, Any]:
    source_artifact_ids = source_artifact_ids or _source_video_artifact_ids(
        session, output_role
    )
    if not source_artifact_ids:
        raise PipelineError(f"no source artifacts for {output_role}")
    return {
        "kind": "normalized-output",
        "source_artifact_ids": source_artifact_ids,
        "transform": {
            "name": "ylx-card-pipeline.normalize",
            "version": "v1",
            "parameters": {
                "preset": preset,
                "rotation_degrees": rotation_degrees,
                "source_video_layout": session.source_video_layout,
            },
        },
    }


def _v2_transform_log_provenance(
    session: Session,
    preset: str,
    rotation_degrees: int,
    normalization_state: dict[str, Any],
) -> dict[str, Any]:
    source_artifact_ids = sorted(
        {
            _artifact_identity(artifact)
            for artifact in session.artifacts
            if artifact.role in {"video_stereo", "video_left", "video_right"}
        }
    )
    if not source_artifact_ids:
        raise PipelineError("no source video artifacts for transform log")
    return {
        "kind": "normalized-output",
        "source_artifact_ids": source_artifact_ids,
        "transform": {
            "name": "ylx-transform-log",
            "version": "v1",
            "parameters": _transform_log_parameters_from_state(
                normalization_state, session, preset, rotation_degrees
            ),
        },
    }


def _normalize_publication_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise PipelineError("invalid publication prefix")
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    segments = normalized.split("/")
    unsafe = (
        len(normalized) > 512
        or "\\" in normalized
        or "__ylx_evidence__" in segments
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in normalized
        )
    )
    if unsafe:
        raise PipelineError(f"invalid publication prefix {prefix!r}")
    return f"{normalized}/"


def _publication_source_audio(session: Session) -> dict[str, Any]:
    if session.source_manifest_schema != DEVICE_SESSION_V2_SCHEMA:
        raise PipelineError("source_audio is only defined for device-session v2")
    if session.source_audio.get("state") == "recorded":
        return {
            "state": "recorded",
            "source_artifact_ids": list(session.source_audio["source_artifact_ids"]),
            "role": "audio.wav",
        }
    if session.source_audio.get("state") == "not_recorded":
        return {
            "state": "not_recorded",
            "reason": session.source_audio["reason"],
        }
    raise PipelineError("device-session v2 source audio state is unavailable")


def _publication_contract_for_session(session: Session) -> tuple[str, str]:
    if session.source_manifest_schema == DEVICE_SESSION_V1_SCHEMA:
        return BUCKET_PUBLICATION_V2_SCHEMA, DEVICE_SESSION_V1_SCHEMA
    if session.source_manifest_schema == DEVICE_SESSION_V2_SCHEMA:
        return BUCKET_PUBLICATION_V3_SCHEMA, DEVICE_SESSION_V2_SCHEMA
    raise PipelineError(
        f"unsupported source manifest schema {session.source_manifest_schema}"
    )


def _validate_bucket_publication(
    publication: dict[str, Any],
    session: Session,
    source_manifest_size: int,
) -> None:
    publication_schema = publication.get("schema")
    if publication_schema == BUCKET_PUBLICATION_V2_SCHEMA:
        validator = _bucket_publication_v2_validator()
        expected_source_schema = DEVICE_SESSION_V1_SCHEMA
        label = "bucket-publication v2"
    elif publication_schema == BUCKET_PUBLICATION_V3_SCHEMA:
        validator = _bucket_publication_v3_validator()
        expected_source_schema = DEVICE_SESSION_V2_SCHEMA
        label = "bucket-publication v3"
    else:
        raise PipelineError(
            f"unsupported bucket-publication schema {publication_schema}"
        )
    _validate_schema(validator, publication, label)
    source = publication["source_manifest"]
    if source["schema"] != expected_source_schema:
        raise PipelineError(
            f"{label} invariant rejection: source manifest schema discriminator"
        )
    if source["schema"] != session.source_manifest_schema:
        raise PipelineError(
            f"{label} invariant rejection: publication/source discriminator mismatch"
        )
    if source["bytes"] != source_manifest_size:
        raise PipelineError(f"{label} invariant rejection: source manifest bytes")
    if source["sha256"] != session.source_manifest_sha256:
        raise PipelineError(f"{label} invariant rejection: source manifest sha256")
    if source["manifest_id"] != session.manifest_id:
        raise PipelineError(f"{label} invariant rejection: source manifest_id")
    if source["session_id"] != session.session_id:
        raise PipelineError(f"{label} invariant rejection: source session_id")
    if source["volume_id"] != session.volume_id:
        raise PipelineError(f"{label} invariant rejection: source volume_id")
    if publication["take"] != session.take:
        raise PipelineError(f"{label} invariant rejection: take")
    if publication["device"] != session.device:
        raise PipelineError(f"{label} invariant rejection: device")

    identity_suffix = f"{session.device['device_id']}/{session.session_id}/"
    source_leaf = f"f-{session.source_manifest_sha256}"
    source_key = source["object_key"]
    if not source_key.endswith(source_leaf):
        raise PipelineError(f"{label} invariant rejection: source object_key leaf")
    authority = source_key[: -len(source_leaf)]
    if not authority.endswith(identity_suffix):
        raise PipelineError(f"{label} invariant rejection: source object_key authority")
    if publication["publication_object_key"] != (
        f"{authority}__ylx_evidence__/publication.json"
    ):
        raise PipelineError(f"{label} invariant rejection: publication object_key")

    source_artifacts = {
        _artifact_identity(artifact): artifact for artifact in session.artifacts
    }
    source_ids = set(source_artifacts)
    referenced_source_ids: set[str] = set()
    roles: set[str] = set()
    content_by_key: dict[str, tuple[Any, ...]] = {}
    normalized_delivery_source_ids: set[str] = set()
    transform_log_source_sets: list[set[str]] = []
    artifacts_by_role: dict[str, dict[str, Any]] = {}
    identity_errors: list[str] = []
    for artifact in publication["artifacts"]:
        artifact_id = artifact["artifact_id"]
        role = artifact["role"]
        if role in roles:
            identity_errors.append(f"duplicate role {role}")
        roles.add(role)
        artifacts_by_role[role] = artifact
        if artifact_id != artifact["sha256"]:
            identity_errors.append("artifact_id != sha256")
        if artifact["object_key"] != f"{authority}f-{artifact_id}":
            identity_errors.append("object_key authority or leaf mismatch")
        provenance = artifact["provenance"]
        provenance_ids = provenance["source_artifact_ids"]
        referenced_source_ids.update(provenance_ids)
        unknown_sources = set(provenance_ids) - source_ids
        if unknown_sources:
            identity_errors.append(
                f"provenance references unknown sources {sorted(unknown_sources)}"
            )
        if provenance["kind"] == "device-artifact":
            if provenance_ids != [artifact_id]:
                identity_errors.append("direct provenance must name its source id")
            source_artifact = source_artifacts.get(artifact_id)
            if source_artifact is None:
                identity_errors.append("direct artifact absent from source manifest")
            elif (
                artifact["role"] != source_artifact.role
                or artifact["media_type"] != source_artifact.media_type
                or artifact["bytes"] != source_artifact.size_bytes
                or artifact["sha256"] != source_artifact.sha256
            ):
                identity_errors.append("direct artifact descriptor changes source")
        if provenance["kind"] == "normalized-output":
            normalized_delivery_source_ids.update(provenance_ids)
        if role == "publication.transform-log":
            transform_log_source_sets.append(set(provenance_ids))
        content_identity = (
            artifact_id,
            artifact["bytes"],
            artifact["media_type"],
            artifact["sha256"],
        )
        previous = content_by_key.setdefault(artifact["object_key"], content_identity)
        if previous != content_identity:
            identity_errors.append("shared object_key has inconsistent descriptor")

    if referenced_source_ids != source_ids:
        identity_errors.append(
            "publication provenance does not cover complete source inventory; "
            f"missing={sorted(source_ids - referenced_source_ids)}"
        )
    for required_role in ("video.left", "video.right", "imu.samples", "frames.index"):
        if required_role not in artifacts_by_role:
            identity_errors.append(f"publication requires {required_role}")
    if normalized_delivery_source_ids and len(transform_log_source_sets) != 1:
        identity_errors.append("normalized publication requires one transform log")
    for transform_sources in transform_log_source_sets:
        if transform_sources != normalized_delivery_source_ids:
            identity_errors.append("transform log source inventory mismatch")

    if publication_schema == BUCKET_PUBLICATION_V3_SCHEMA:
        source_audio = publication["source_audio"]
        expected_audio = _publication_source_audio(session)
        if source_audio != expected_audio:
            identity_errors.append("source_audio differs from source manifest")
        audio_source_ids = {
            _artifact_identity(artifact)
            for artifact in session.artifacts
            if artifact.role == "audio.wav"
        }
        published_audio = artifacts_by_role.get("audio.wav")
        if expected_audio["state"] == "recorded":
            if set(expected_audio["source_artifact_ids"]) != audio_source_ids:
                identity_errors.append("source_audio does not cover audio inventory")
            if published_audio is None:
                identity_errors.append("recorded audio requires audio.wav artifact")
            elif set(published_audio["provenance"]["source_artifact_ids"]) != (
                audio_source_ids
            ):
                identity_errors.append(
                    "audio.wav provenance does not bind audio source"
                )
        elif published_audio is not None:
            identity_errors.append("not_recorded audio must not publish audio.wav")

    if session.source_video_layout == "raw-side-by-side":
        raw_ids = [
            _artifact_identity(artifact)
            for artifact in session.artifacts
            if artifact.role == "video_stereo"
        ]
        for role in ("video.left", "video.right"):
            published = artifacts_by_role.get(role)
            if published and published["provenance"]["source_artifact_ids"] != raw_ids:
                identity_errors.append(f"{role} must bind raw-side-by-side source")
    elif session.source_video_layout == "split-eyes":
        for role, source_role in (
            ("video.left", "video_left"),
            ("video.right", "video_right"),
        ):
            expected = [
                _artifact_identity(artifact) for artifact in session.videos(source_role)
            ]
            published = artifacts_by_role.get(role)
            if published and published["provenance"]["source_artifact_ids"] != expected:
                identity_errors.append(f"{role} split-eye source order mismatch")

    if _api_datetime(publication["published_at"]) < _api_datetime(
        session.source_manifest_sealed_at
    ):
        identity_errors.append("published_at precedes source manifest sealed_at")
    if identity_errors:
        raise PipelineError(
            f"{label} invariant rejection: " + "; ".join(identity_errors)
        )


def _upload_versioned_bucket_publication(
    session: Session,
    outputs: list[Path],
    bucket: str,
    prefix: str,
    preset: str,
    rotation_degrees: int,
    lease_stale_after_seconds: float,
) -> list[str]:
    if not session.device.get("device_id") or not session.device.get("device_label"):
        raise PipelineError("device-session is missing bucket device identity")
    if not session.manifest_id or not session.volume_id or not session.take:
        raise PipelineError("device-session is missing publication identity fields")
    publication_schema, source_manifest_schema = _publication_contract_for_session(
        session
    )

    prefix = _normalize_publication_prefix(prefix)
    base = f"{prefix}{session.device['device_id']}/{session.session_id}"
    normalization_state = _normalization_state_for_publication(
        session, outputs, preset, rotation_degrees
    )
    normalization_executions = _normalization_execution_by_role(normalization_state)

    with tempfile.TemporaryDirectory(prefix="ylx-upload-") as staging_directory:
        staging = Path(staging_directory)
        source_manifest_path = (
            staging / "source-manifest" / session.source_manifest_name
        )
        _copy_regular_file(
            session.directory,
            Path(session.source_manifest_name),
            source_manifest_path,
            session.source_manifest_name,
        )
        source_manifest_sha256 = sha256_of(source_manifest_path)
        if source_manifest_sha256 != session.source_manifest_sha256:
            raise PipelineError(
                f"{session.source_manifest_name} changed while staging publication"
            )

        object_uploads: list[dict[str, Any]] = [
            {
                "path": source_manifest_path,
                "key": _content_object_key(base, source_manifest_sha256),
                "media_type": "application/json",
                "size_bytes": source_manifest_path.stat().st_size,
                "sha256": source_manifest_sha256,
            }
        ]
        artifacts: list[dict[str, Any]] = []
        seen_roles: set[str] = set()

        for output in sorted(outputs, key=lambda path: path.stem != "left"):
            if output.stem not in {"left", "right"}:
                raise PipelineError(f"unsupported normalized output {output.name}")
            role = f"video.{output.stem}"
            if role in seen_roles:
                raise PipelineError(f"duplicate publication role {role}")
            seen_roles.add(role)
            staged = staging / "normalized" / output.name
            _copy_regular_file(output.parent, Path(output.name), staged, output.name)
            size_bytes = staged.stat().st_size
            sha256 = sha256_of(staged)
            object_key = _content_object_key(base, sha256)
            object_uploads.append(
                {
                    "path": staged,
                    "key": object_key,
                    "media_type": "video/mp4",
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                }
            )
            artifacts.append(
                {
                    "artifact_id": sha256,
                    "role": role,
                    "object_key": object_key,
                    "media_type": "video/mp4",
                    "bytes": size_bytes,
                    "sha256": sha256,
                    "provenance": _v2_normalization_provenance(
                        session,
                        role,
                        preset,
                        rotation_degrees,
                        normalization_executions[role]["source_artifact_ids"],
                    ),
                }
            )

        if seen_roles != {"video.left", "video.right"}:
            raise PipelineError(
                "device-session publication requires left and right outputs"
            )

        for artifact in sorted(
            (
                artifact
                for artifact in session.artifacts
                if artifact.role not in {"video_stereo", "video_left", "video_right"}
            ),
            key=lambda artifact: artifact.display_path,
        ):
            if artifact.role in seen_roles:
                raise PipelineError(f"duplicate publication role {artifact.role}")
            seen_roles.add(artifact.role)
            staged = staging / "source" / artifact.display_path
            _copy_regular_file(
                session.directory,
                Path(artifact.display_path),
                staged,
                artifact.display_path,
            )
            size_bytes = staged.stat().st_size
            sha256 = sha256_of(staged)
            if size_bytes != artifact.size_bytes or sha256 != artifact.sha256:
                raise PipelineError(
                    f"{artifact.display_path} changed while staging publication"
                )
            object_key = _content_object_key(base, sha256)
            media_type = artifact.media_type or "application/octet-stream"
            object_uploads.append(
                {
                    "path": staged,
                    "key": object_key,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                }
            )
            artifacts.append(
                {
                    "artifact_id": _artifact_identity(artifact),
                    "role": artifact.role,
                    "object_key": object_key,
                    "media_type": media_type,
                    "bytes": size_bytes,
                    "sha256": sha256,
                    "provenance": {
                        "kind": "device-artifact",
                        "source_artifact_ids": [_artifact_identity(artifact)],
                    },
                }
            )

        transform_log = {
            "schema": "ylx.publication-transform-log.v1",
            "source_manifest_sha256": source_manifest_sha256,
            "source_video_layout": session.source_video_layout,
            "source_declarations": session.source_declarations,
            "source_audio": session.source_audio or {"state": "not_applicable"},
            "pipeline": normalization_state["pipeline"],
            "normalization": {
                "preset": preset,
                "rotation_degrees": rotation_degrees,
                "executions": [
                    normalization_executions[role]
                    for role in ("video.left", "video.right")
                ],
                "outputs": [
                    {
                        "role": artifact["role"],
                        "bytes": artifact["bytes"],
                        "sha256": artifact["sha256"],
                    }
                    for artifact in artifacts
                    if artifact["role"] in {"video.left", "video.right"}
                ],
            },
        }
        transform_log_path = staging / "publication-transform-log.json"
        transform_log_path.write_bytes(_json_bytes(transform_log))
        transform_log_sha256 = sha256_of(transform_log_path)
        transform_log_key = _content_object_key(base, transform_log_sha256)
        object_uploads.append(
            {
                "path": transform_log_path,
                "key": transform_log_key,
                "media_type": "application/json",
                "size_bytes": transform_log_path.stat().st_size,
                "sha256": transform_log_sha256,
            }
        )
        artifacts.append(
            {
                "artifact_id": transform_log_sha256,
                "role": "publication.transform-log",
                "object_key": transform_log_key,
                "media_type": "application/json",
                "bytes": transform_log_path.stat().st_size,
                "sha256": transform_log_sha256,
                "provenance": _v2_transform_log_provenance(
                    session, preset, rotation_degrees, normalization_state
                ),
            }
        )

        publication_key = f"{base}/__ylx_evidence__/publication.json"
        publication = {
            "schema": publication_schema,
            "publication_id": session.manifest_id,
            "sealed": True,
            "published_at": session.source_manifest_sealed_at,
            "device": session.device,
            "source_manifest": {
                "manifest_id": session.manifest_id,
                "schema": source_manifest_schema,
                "session_id": session.session_id,
                "volume_id": session.volume_id,
                "object_key": _content_object_key(base, source_manifest_sha256),
                "bytes": source_manifest_path.stat().st_size,
                "sha256": source_manifest_sha256,
            },
            "take": session.take,
            "publication_object_key": publication_key,
            "artifacts": artifacts,
        }
        if publication_schema == BUCKET_PUBLICATION_V3_SCHEMA:
            publication["source_audio"] = _publication_source_audio(session)
        _validate_bucket_publication(
            publication, session, source_manifest_path.stat().st_size
        )
        manifest_bytes = (
            json.dumps(
                publication,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        keys = [item["key"] for item in object_uploads] + [publication_key]

        client = object_store()
        lease = _acquire_publication_lease(
            client,
            bucket,
            base,
            stale_after_seconds=lease_stale_after_seconds,
        )
        publication_error: Exception | None = None
        try:
            matches = [
                _remote_object_matches(
                    client,
                    bucket,
                    item["key"],
                    size_bytes=item["size_bytes"],
                    sha256=item["sha256"],
                    media_type=item["media_type"],
                )
                for item in object_uploads
            ]
            manifest_matches = _remote_object_matches(
                client,
                bucket,
                publication_key,
                size_bytes=len(manifest_bytes),
                sha256=manifest_sha256,
                media_type="application/json",
            )
            if manifest_matches and all(matches):
                return keys

            client.delete_object(Bucket=bucket, Key=publication_key)
            for item, matches_remote in zip(object_uploads, matches, strict=True):
                if matches_remote:
                    continue
                client.upload_file(
                    str(item["path"]),
                    bucket,
                    item["key"],
                    ExtraArgs={
                        "ContentType": item["media_type"],
                        "Metadata": {"sha256": item["sha256"]},
                    },
                )
            client.put_object(
                Bucket=bucket,
                Key=publication_key,
                Body=manifest_bytes,
                ContentType="application/json",
                IfNoneMatch="*",
                Metadata={"sha256": manifest_sha256},
            )
            return keys
        except Exception as error:
            publication_error = PipelineError(
                f"publishing {session.session_id} failed: {error}"
            )
            raise publication_error from error
        finally:
            try:
                _conditional_delete(client, bucket, lease.key, lease.etag)
            except PipelineError as release_error:
                if publication_error is not None:
                    publication_error.add_note(
                        f"publication lease release also failed: {release_error}"
                    )
                else:
                    raise


def upload(
    session: Session,
    outputs: list[Path],
    bucket: str,
    prefix: str,
    device_id: str,
    preset: str,
    rotation_degrees: int = 0,
    lease_stale_after_seconds: float = PUBLICATION_LEASE_STALE_AFTER_SECONDS,
) -> list[str]:
    """Publish one session in the layout the EgoView console indexes.

        {prefix}{device}/{session}/f-<id>                      session objects
        {prefix}{device}/{session}/__ylx_evidence__/publication.json

    The console rebuilds each object key from the `id` in the manifest, so the
    objects are stored under opaque ids with no extension and the manifest is
    the only thing that says what they are.

    No detached signature is written. The console records evidence trust but
    does not gate on it, so these sessions index and preview as `unsigned`.
    That is the honest state: this pipeline is not the capture device and
    cannot attest for it, and signing a manifest it wrote itself with a key
    found on the card would make a forgery indistinguishable from the real
    thing.

    The manifest goes last, so an interrupted upload never looks complete.
    Every data object carries its SHA-256 as object metadata. On a retry,
    matching objects are retained and only missing or mismatched objects are
    transferred before the completion manifest is restored.
    """
    if rotation_degrees not in SUPPORTED_ROTATIONS:
        raise PipelineError(
            f"unsupported rotation {rotation_degrees}; choose one of {SUPPORTED_ROTATIONS}"
        )

    if session.source_manifest_schema in {
        DEVICE_SESSION_V1_SCHEMA,
        DEVICE_SESSION_V2_SCHEMA,
    }:
        return _upload_versioned_bucket_publication(
            session,
            outputs,
            bucket,
            prefix,
            preset,
            rotation_degrees,
            lease_stale_after_seconds,
        )

    device_id = _object_identity(device_id, "device_id")
    client = object_store()
    prefix = prefix.strip("/")
    prefix = f"{prefix}/" if prefix else ""
    base = f"{prefix}{device_id}/{session.session_id}"

    # Do every failure-prone local operation before acquiring a remote lease.
    # This avoids stranded leases when a work filesystem is full or unavailable.
    with tempfile.TemporaryDirectory(prefix="ylx-upload-") as staging_directory:
        prepared: list[dict[str, Any]] = []
        seen_display_paths: set[str] = set()
        for output in sorted(outputs, key=lambda path: path.stem != "left"):
            if output.stem not in {"left", "right"}:
                raise PipelineError(f"unsupported normalized output {output.name}")
            display_path = f"video/{output.stem}.mp4"
            if display_path in seen_display_paths:
                raise PipelineError(f"duplicate publication path {display_path}")
            seen_display_paths.add(display_path)
            staged = Path(staging_directory) / output.name
            _copy_regular_file(output.parent, Path(output.name), staged, output.name)
            prepared.append(
                {
                    "path": staged,
                    "display_path": display_path,
                    "role": f"video_{output.stem}",
                    "media_type": "video/mp4",
                    "size_bytes": staged.stat().st_size,
                    "sha256": sha256_of(staged),
                }
            )

        for artifact in sorted(
            (
                artifact
                for artifact in session.artifacts
                if artifact.role in PUBLISHED_AUXILIARY_ROLES
            ),
            key=lambda artifact: artifact.display_path,
        ):
            if artifact.display_path in seen_display_paths:
                raise PipelineError(
                    f"duplicate publication path {artifact.display_path}"
                )
            seen_display_paths.add(artifact.display_path)
            staged = Path(staging_directory) / "source" / artifact.display_path
            _copy_regular_file(
                session.directory,
                Path(artifact.display_path),
                staged,
                artifact.display_path,
            )
            size_bytes = staged.stat().st_size
            sha256 = sha256_of(staged)
            if size_bytes != artifact.size_bytes or sha256 != artifact.sha256:
                raise PipelineError(
                    f"{artifact.display_path} changed while staging publication"
                )
            prepared.append(
                {
                    "path": staged,
                    "display_path": artifact.display_path,
                    "role": artifact.role,
                    "media_type": _media_type_for(artifact.role),
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                }
            )

        files: list[dict[str, Any]] = []
        keys: list[str] = []
        total_bytes = 0
        video_bytes = 0
        for prepared_output in prepared:
            identifier = file_id_for(prepared_output["display_path"])
            prepared_output["key"] = f"{base}/{identifier}"
            entry = {
                "id": identifier,
                "display_path": prepared_output["display_path"],
                "role": prepared_output["role"],
                "media_type": prepared_output["media_type"],
                "size_bytes": prepared_output["size_bytes"],
                "sha256": prepared_output["sha256"],
            }
            files.append(entry)
            keys.append(prepared_output["key"])
            total_bytes += prepared_output["size_bytes"]
            if prepared_output["role"].startswith("video_"):
                video_bytes += prepared_output["size_bytes"]

        manifest = {
            "schema_version": 1,
            "session_id": session.session_id,
            "captured_at": session.captured_at,
            "duration_seconds": session.duration_seconds,
            "revision": _publication_revision(session, files),
            "total_bytes": total_bytes,
            "video_bytes": video_bytes,
            "integrity_ok": True,
            "files": files,
            "normalization": {
                "codec": "h264",
                "encoder": "libx264",
                "profile": "high",
                "preset": preset,
                "pixel_format": "yuv420p",
                "container": "mp4",
                "faststart": True,
                "rotation_degrees": rotation_degrees,
                "rotation_policy": "operator_explicit",
                "source_directory": session.name,
            },
            # This is local pipeline provenance. It binds the emitted bytes to
            # the card manifest and input hashes, but is not a device signature
            # or evidence that an S3-compatible service persisted those bytes.
            "provenance": build_provenance(session, files, rotation_degrees),
            "source_signature": session.source_signature,
        }
        manifest_bytes = (
            json.dumps(
                manifest,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_key = f"{base}/__ylx_evidence__/publication.json"
        keys.append(manifest_key)

        lease = _acquire_publication_lease(
            client,
            bucket,
            base,
            stale_after_seconds=lease_stale_after_seconds,
        )
        publication_error: Exception | None = None
        try:
            matches = [
                _remote_object_matches(
                    client,
                    bucket,
                    prepared_output["key"],
                    size_bytes=prepared_output["size_bytes"],
                    sha256=prepared_output["sha256"],
                    media_type=prepared_output["media_type"],
                )
                for prepared_output in prepared
            ]
            manifest_matches = _remote_object_matches(
                client,
                bucket,
                manifest_key,
                size_bytes=len(manifest_bytes),
                sha256=manifest_sha256,
                media_type="application/json",
            )
            if manifest_matches and all(matches):
                return keys

            # Hide any prior completion record before repairing fixed object
            # keys. A crash can leave data objects, never a false completion.
            client.delete_object(Bucket=bucket, Key=manifest_key)
            for prepared_output, matches_remote in zip(prepared, matches, strict=True):
                if matches_remote:
                    continue
                client.upload_file(
                    str(prepared_output["path"]),
                    bucket,
                    prepared_output["key"],
                    ExtraArgs={
                        "ContentType": prepared_output["media_type"],
                        "Metadata": {"sha256": prepared_output["sha256"]},
                    },
                )
            client.put_object(
                Bucket=bucket,
                Key=manifest_key,
                Body=manifest_bytes,
                ContentType="application/json",
                Metadata={"sha256": manifest_sha256},
            )
            return keys
        except Exception as error:
            publication_error = PipelineError(
                f"publishing {session.session_id} failed: {error}"
            )
            raise publication_error from error
        finally:
            try:
                _conditional_delete(client, bucket, lease.key, lease.etag)
            except PipelineError as release_error:
                if publication_error is not None:
                    publication_error.add_note(
                        f"publication lease release also failed: {release_error}"
                    )
                else:
                    raise


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def process(session: Session, args: argparse.Namespace, workdir: Path) -> None:
    print(
        f"\n{session.name}  "
        f"({session.duration_seconds:.1f}s, {session.source_codec or 'unknown codec'})"
    )

    print("  verifying card bytes...", end="", flush=True)
    verify(session)
    print(f" ok ({len(session.artifacts)} artifacts)")

    print("  encoding...", end="", flush=True)
    frozen_session = snapshot_session(session, workdir)
    verify(frozen_session)
    outputs = normalize(
        frozen_session,
        workdir,
        args.preset,
        rotation_degrees=args.rotation,
        reuse_completed=not args.force_reencode,
    )
    total = sum(path.stat().st_size for path in outputs)
    print(f" ok ({', '.join(p.name for p in outputs)}, {total / 1e6:.1f} MB)")

    if args.skip_upload:
        print("  upload skipped")
        return

    print("  uploading...", end="", flush=True)
    keys = upload(
        frozen_session,
        outputs,
        args.bucket,
        args.prefix,
        args.device_id,
        args.preset,
        rotation_degrees=args.rotation,
        lease_stale_after_seconds=args.lease_stale_after,
    )
    print(f" ok ({len(keys)} objects under {keys[0].rsplit('/', 1)[0]}/)")


def export_sbs_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py export-sbs",
        description="Export one local YLX session/publication as a side-by-side MP4."
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="session/publication directory containing publication_manifest.json",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="output side-by-side H.264 MP4"
    )
    parser.add_argument(
        "--work",
        type=Path,
        help="directory for temporary ffmpeg concat lists (default: temp dir)",
    )
    parser.add_argument(
        "--preset", default=VIDEO_PRESET, help=f"x264 preset (default: {VIDEO_PRESET})"
    )
    parser.add_argument(
        "--crf",
        type=int,
        help="x264 CRF; default follows the session source codec",
    )
    parser.add_argument(
        "--audio-bitrate",
        default=SBS_EXPORT_AUDIO_BITRATE,
        help=f"AAC bitrate when audio is present (default: {SBS_EXPORT_AUDIO_BITRATE})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output MP4",
    )
    args = parser.parse_args(argv)

    if args.crf is not None and not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")
    if not args.audio_bitrate:
        parser.error("--audio-bitrate must not be empty")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is not on PATH")

    try:
        session = read_publication_session(args.input)
        export_sbs(
            session,
            args.output,
            workdir=args.work,
            preset=args.preset,
            crf=args.crf,
            audio_bitrate=args.audio_bitrate,
            overwrite=args.force,
        )
    except PipelineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"exported: {args.output}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "export-sbs":
        return export_sbs_cli(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "Offline SBS export: uv run main.py export-sbs "
            "--input SESSION_DIR --output session-sbs.mp4"
        ),
    )
    parser.add_argument("--card", type=Path, required=True, help="mounted card root")
    parser.add_argument(
        "--work", type=Path, help="where to put encoded output (default: temp dir)"
    )
    parser.add_argument(
        "--bucket", default=os.environ.get("S3_BUCKET", ""), help="target bucket"
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get("S3_PREFIX", ""),
        help="key prefix; empty matches the console's raw area",
    )
    parser.add_argument("--device-id", help="override the id read from the card")
    parser.add_argument(
        "--trusted-key-registry",
        type=Path,
        help="external trusted device-key registry JSON",
    )
    parser.add_argument(
        "--external-device-identity",
        help="authenticated external device identity for registry lookup",
    )
    parser.add_argument(
        "--binding-receipt",
        type=Path,
        help="caller-owned authenticated binding and inventory receipt",
    )
    parser.add_argument(
        "--binding-authority-key",
        type=Path,
        help="out-of-band Ed25519 public key for binding receipts",
    )
    parser.add_argument("--binding-issuer", help="trusted binding-receipt issuer")
    parser.add_argument(
        "--binding-identity", help="trusted binding-receipt workflow identity"
    )
    parser.add_argument(
        "--binding-audience",
        default="ylx-card-pipeline",
        help="trusted binding-receipt audience",
    )
    parser.add_argument(
        "--unsafe-dev-identity",
        action="store_true",
        help="allow CLI identity only for unsafe development",
    )
    parser.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="explicitly publish unsigned source manifests as degraded",
    )
    parser.add_argument(
        "--preset", default=VIDEO_PRESET, help=f"x264 preset (default: {VIDEO_PRESET})"
    )
    parser.add_argument(
        "--rotation",
        type=int,
        choices=SUPPORTED_ROTATIONS,
        default=0,
        help="clockwise source rotation in degrees; use 180 for an upside-down rig",
    )
    parser.add_argument(
        "--force-reencode",
        action="store_true",
        help="ignore a matching completed normalization in --work",
    )
    parser.add_argument(
        "--lease-stale-after",
        type=float,
        default=PUBLICATION_LEASE_STALE_AFTER_SECONDS,
        help="seconds after which a crashed publisher lease may be reclaimed",
    )
    parser.add_argument(
        "--session", help="only sessions whose directory name contains this"
    )
    parser.add_argument("--limit", type=int, help="process at most this many sessions")
    parser.add_argument(
        "--skip-upload", action="store_true", help="stop after encoding"
    )
    parser.add_argument(
        "--keep-work", action="store_true", help="keep the temporary work dir"
    )
    args = parser.parse_args()

    if args.lease_stale_after <= 0:
        parser.error("--lease-stale-after must be positive")
    if not args.skip_upload and not args.bucket:
        parser.error("--bucket is required unless --skip-upload is given")
    if not args.device_id:
        args.device_id = device_id_of(args.card)
    if not args.skip_upload and not args.device_id:
        parser.error("the card carries no device-id; pass --device-id")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is not on PATH")

    try:
        recordings = find_recordings_dir(args.card)
        print(f"card:       {args.card}")
        print(f"device:     {args.device_id or '(unknown)'}")
        print(f"recordings: {recordings}")
        registry = None
        if args.trusted_key_registry:
            registry = parse_strict_json(
                args.trusted_key_registry.read_bytes(), "trusted key registry"
            )
        identity = args.external_device_identity
        expected_revision = None
        if args.binding_receipt:
            if (
                not registry
                or not args.binding_authority_key
                or not args.binding_issuer
                or not args.binding_identity
            ):
                parser.error(
                    "--binding-receipt requires registry, authority key, issuer, and identity trust policy"
                )
            identity, expected_revision = authenticated_binding_receipt(
                args.binding_receipt.read_bytes(),
                datetime.now(UTC),
                args.binding_authority_key.read_bytes(),
                args.binding_issuer,
                args.binding_identity,
                args.binding_audience,
                str(registry["revision"]),
            )
        elif identity and not args.unsafe_dev_identity:
            parser.error(
                "CLI identity is not authenticated; pass --binding-receipt or explicit --unsafe-dev-identity"
            )
        if not args.allow_unsigned and (not args.trusted_key_registry or not identity):
            parser.error(
                "--trusted-key-registry and --binding-receipt are required unless --allow-unsigned is explicit"
            )
        sessions = read_sessions(recordings, registry, identity, args.allow_unsigned)
        if expected_revision and any(
            session.source_manifest_revision != expected_revision
            for session in sessions
        ):
            raise PipelineError(
                "authenticated binding receipt inventory revision mismatch"
            )
    except PipelineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.session:
        sessions = [s for s in sessions if args.session in s.name]
    if args.limit:
        sessions = sessions[: args.limit]
    if not sessions:
        print("nothing to do: no published sessions found")
        return 0
    print(f"{len(sessions)} session(s) to process")

    temporary = None
    if args.work:
        workdir = args.work
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.mkdtemp(prefix="ylx-card-")
        workdir = Path(temporary)

    failures = 0
    try:
        for session in sessions:
            try:
                process(session, args, workdir)
            except PipelineError as error:
                # One bad session must not strand the rest of the card.
                failures += 1
                print(f"  failed: {error}", file=sys.stderr)
    finally:
        if temporary and not args.keep_work:
            shutil.rmtree(temporary, ignore_errors=True)
        elif temporary:
            print(f"\nwork kept at {temporary}")

    print(f"\ndone: {len(sessions) - failures} succeeded, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
