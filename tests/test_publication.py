"""Local contract tests for publication semantics.

The in-memory client below records API payloads only. It is not an S3/OSS
server, does not provide durable storage evidence, and cannot prove that an
object was written in the same physical session as a recording card.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, FormatChecker

import main

RP_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rp-publication-signature-v1"
CONTRACT_FIXTURE_ROOT = main.YLX_CONTRACT_ROOT / "fixtures"
DEVICE_SESSION_ID = "018fe2d2-79b0-7cc0-b7b8-111111111111"
DEVICE_MANIFEST_ID = "018fe2d2-79b0-7cc0-b7b8-222222222222"
DEVICE_TAKE_ID = "018fe2d2-79b0-7cc0-b7b8-333333333333"
DEVICE_VOLUME_ID = "11111111-1111-4111-8111-111111111111"
DEVICE_UUID = "22222222-2222-4222-8222-222222222222"
DEVICE_LABEL = "YLX-1234ABCD"
FAKE_FFMPEG_VERSION = "ffmpeg version 6.1-test"


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.modified_at: dict[tuple[str, str], datetime] = {}
        self.uploads: list[tuple[str, str]] = []
        self.puts: list[tuple[str, str]] = []

    def upload_file(
        self, filename: str, bucket: str, key: str, ExtraArgs: dict
    ) -> None:
        self.uploads.append((bucket, key))
        self.objects[(bucket, key)] = (
            Path(filename).read_bytes(),
            ExtraArgs["ContentType"],
        )
        self.metadata[(bucket, key)] = dict(ExtraArgs.get("Metadata", {}))
        self.modified_at[(bucket, key)] = datetime.now(UTC)

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        IfNoneMatch: str | None = None,
        Metadata: dict[str, str] | None = None,
    ) -> dict:
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.puts.append((Bucket, Key))
        self.objects[(Bucket, Key)] = (Body, ContentType)
        self.metadata[(Bucket, Key)] = dict(Metadata or {})
        self.modified_at[(Bucket, Key)] = datetime.now(UTC)
        return {"ETag": self.etag_for(Body)}

    @staticmethod
    def etag_for(body: bytes) -> str:
        return f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'

    def delete_object(
        self, *, Bucket: str, Key: str, IfMatch: str | None = None
    ) -> None:
        existing = self.objects.get((Bucket, Key))
        if IfMatch is not None and (
            existing is None or self.etag_for(existing[0]) != IfMatch
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "DeleteObject")
        self.objects.pop((Bucket, Key), None)
        self.metadata.pop((Bucket, Key), None)
        self.modified_at.pop((Bucket, Key), None)

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        existing = self.objects.get((Bucket, Key))
        if existing is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        body, content_type = existing
        etags = getattr(self, "etags", {})
        return {
            "ContentLength": len(body),
            "ContentType": content_type,
            "Metadata": self.metadata.get((Bucket, Key), {}),
            "ETag": etags.get((Bucket, Key), self.etag_for(body)),
            "LastModified": self.modified_at.get((Bucket, Key), datetime.now(UTC)),
        }


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def pcm_s16le_wav_bytes(
    *, sample_rate: int, channels: int, sample_count: int, payload_byte: bytes = b"\0"
) -> bytes:
    assert len(payload_byte) == 1
    bits_per_sample = 16
    block_align = channels * bits_per_sample // 8
    payload = payload_byte * (sample_count * block_align)
    byte_rate = sample_rate * block_align
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(payload))
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            1,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
        )
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )


def clear_ylx_contract_caches() -> None:
    main._ylx_contract_source_metadata.cache_clear()
    main._pinned_ylx_schema.cache_clear()
    main._device_session_v1_validator.cache_clear()
    main._device_session_v2_validator.cache_clear()
    main._bucket_publication_v2_validator.cache_clear()
    main._bucket_publication_v3_validator.cache_clear()


def copy_ylx_contract_root(tmp_path: Path) -> Path:
    copied = tmp_path / "ylx-contracts"
    shutil.copytree(main.YLX_CONTRACT_ROOT, copied)
    return copied


def ffmpeg_option(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


def load_contract_fixture(relative: str) -> dict:
    return json.loads((CONTRACT_FIXTURE_ROOT / relative).read_text(encoding="utf-8"))


def write_contract_manifest(recordings: Path, name: str, manifest: dict) -> None:
    session_dir = recordings / name
    session_dir.mkdir(parents=True)
    (session_dir / "manifest.json").write_bytes(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def normalized_outputs_with_state(
    session: main.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    preset: str = "ultrafast",
    rotation_degrees: int = 0,
) -> tuple[list[Path], list[dict]]:
    executions = []

    def fake_run_ffmpeg(argv: list[str], description: str) -> dict:
        output = Path(argv[-1])
        body = f"normalized-{output.stem}".encode("ascii")
        output.write_bytes(body)
        execution = {
            "argv": list(argv),
            "ffmpeg_version": FAKE_FFMPEG_VERSION,
            "exit_status": {"code": 0, "signal": None},
        }
        executions.append(execution)
        return execution

    monkeypatch.setattr(main, "_ffmpeg_version", lambda: FAKE_FFMPEG_VERSION)
    monkeypatch.setattr(main, "run_ffmpeg", fake_run_ffmpeg)
    outputs = main.normalize(
        session,
        tmp_path / "work",
        preset,
        rotation_degrees=rotation_degrees,
        reuse_completed=False,
    )
    return outputs, executions


def tamper_normalization_state(outputs: list[Path], mutate) -> dict:
    state_path = outputs[0].parent / main.NORMALIZATION_STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    mutate(state)
    state_path.write_text(
        json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return state


def forbid_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_object_store():
        raise AssertionError("object_store should not be created for invalid state")

    monkeypatch.setattr(main, "object_store", forbidden_object_store)


def resign_admission_manifest(manifest: dict) -> dict:
    vector = json.loads(
        (RP_FIXTURE_ROOT / "admission-vector.json").read_text(encoding="utf-8")
    )
    candidate = json.loads(json.dumps(manifest))
    key = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(vector["test_only_private_seed_hex"])
    )
    candidate["publication_signature"]["signature"] = key.sign(
        main.canonical_signature_payload(candidate)
    ).hex()
    return candidate


def write_card_session(
    tmp_path: Path,
    session_id: str = "capture-001",
    *,
    with_auxiliary: bool = False,
) -> main.Session:
    session_dir = tmp_path / "recordings" / "card-directory-name"
    source = session_dir / "spool" / "source_00000.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"recorded-card-video")
    files = [
        {
            "display_path": "spool/source_00000.mp4",
            "role": "video_stereo",
            "size_bytes": source.stat().st_size,
            "sha256": digest(source.read_bytes()),
        }
    ]
    if with_auxiliary:
        auxiliary = (
            ("session.json", "metadata", "application/json", b'{"camera":{}}'),
            (
                "capture.commit.json",
                "metadata",
                "application/json",
                b'{"schema_version":3}',
            ),
            (
                "raw/imu.jsonl",
                "imu",
                "application/x-ndjson",
                b'{"t":1,"gx":0.1}\n',
            ),
        )
        for display_path, role, media_type, body in auxiliary:
            path = session_dir / display_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            files.append(
                {
                    "display_path": display_path,
                    "role": role,
                    "media_type": media_type,
                    "size_bytes": len(body),
                    "sha256": digest(body),
                }
            )
    manifest = {
        "session_id": session_id,
        "revision": "sha256:card-revision",
        "captured_at": "2026-08-11T00:00:00Z",
        "duration_seconds": 12.5,
        "integrity_ok": True,
        "files": files,
    }
    manifest_path = session_dir / "publication_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sessions = main.read_sessions(tmp_path / "recordings", allow_unsigned=True)
    assert len(sessions) == 1
    return sessions[0]


def device_session_artifact(
    session_dir: Path,
    path: str,
    role: str,
    media_type: str,
    body: bytes,
) -> dict:
    artifact_path = session_dir / path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(body)
    sha256 = digest(body)
    return {
        "artifact_id": sha256,
        "role": role,
        "path": path,
        "media_type": media_type,
        "bytes": len(body),
        "sha256": sha256,
    }


def write_device_session_v1(
    tmp_path: Path,
    *,
    layout: str = "raw-side-by-side",
    mutate=None,
) -> tuple[main.Session, bytes, dict[str, dict]]:
    session_dir = tmp_path / "recordings" / "device-session-v1"
    if layout == "raw-side-by-side":
        raw_video = device_session_artifact(
            session_dir,
            "video/raw-sbs.mjpeg",
            "video.raw-side-by-side",
            "video/x-motion-jpeg",
            b"raw-sbs-video",
        )
        video = {
            "layout": "raw-side-by-side",
            "codec": "mjpeg",
            "continuous": True,
            "artifact": raw_video,
        }
        artifacts = {"raw_video": raw_video}
    elif layout == "split-eyes":
        left_video = device_session_artifact(
            session_dir,
            "video/left-00000.mp4",
            "video.left",
            "video/mp4",
            b"split-left-video",
        )
        right_video = device_session_artifact(
            session_dir,
            "video/right-00000.mp4",
            "video.right",
            "video/mp4",
            b"split-right-video",
        )
        video = {
            "layout": "split-eyes",
            "codec": "h264",
            "container": "mp4",
            "segments": [
                {
                    "index": 0,
                    "start_frame": 0,
                    "end_frame": 12,
                    "start_time_seconds": 0,
                    "end_time_seconds": 0.4,
                    "artifacts": {"left": left_video, "right": right_video},
                }
            ],
        }
        artifacts = {"left_video": left_video, "right_video": right_video}
    else:
        video = {"layout": layout}
        artifacts = {}

    imu = device_session_artifact(
        session_dir,
        "raw/imu.jsonl",
        "imu.samples",
        "application/x-ndjson",
        b'{"t":1,"gx":10}\n',
    )
    frames = device_session_artifact(
        session_dir,
        "frames/index.jsonl",
        "frames.index",
        "application/x-ndjson",
        b'{"frame":1}\n',
    )
    artifacts.update({"imu": imu, "frames": frames})
    manifest = {
        "schema": "ylx.device-session.v1",
        "manifest_id": DEVICE_MANIFEST_ID,
        "sealed": True,
        "sealed_at": "2026-08-21T04:00:01Z",
        "session_id": DEVICE_SESSION_ID,
        "volume_id": DEVICE_VOLUME_ID,
        "capture_mode": "production",
        "display_name": "Device session v1 fixture",
        "device": {
            "device_id": DEVICE_UUID,
            "device_label": DEVICE_LABEL,
            "hardware_fingerprint": "sha256:" + "a" * 64,
            "platform": "rdk-x5",
            "software_version": "fixture",
            "commit": "b" * 40,
        },
        "time": {
            "started_at": "2026-08-21T04:00:00Z",
            "ended_at": "2026-08-21T04:00:00.400000Z",
            "timezone": "Asia/Shanghai",
            "duration_seconds": 0.4,
        },
        "take": {
            "take_id": DEVICE_TAKE_ID,
            "sequence": 1,
            "continuation_of": None,
        },
        "camera": {
            "width": 3840,
            "height": 1080,
            "eye_width": 1920,
            "sensor_fps": 60,
            "frame_decimation": 2,
            "nominal_fps": 30,
            "effective_fps": 30,
            "coordinate_frame": "opencv_optical",
        },
        "video": video,
        "imu": {
            "artifact": imu,
            "sample_count": 1,
            "units": "raw_int16",
            "coordinate_frame": "opencv_optical",
        },
        "frames": {"artifact": frames, "count": 12},
        "logs": [],
        "integrity": {
            "verified_at": "2026-08-21T04:00:00.500000Z",
            "dropped_frames": 0,
            "drop_events": [],
            "quality_policy": {
                "policy_id": "rdk-x5-lossless-v1",
                "max_contiguous_dropped_frames": 0,
                "max_total_dropped_frames": 0,
                "max_drop_fraction": 0,
                "window_seconds": 1,
                "max_dropped_frames_per_window": 0,
            },
            "fatal_errors": [],
        },
    }
    if mutate is not None:
        mutate(manifest)
    manifest_bytes = (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    (session_dir / "manifest.json").write_bytes(manifest_bytes)
    sessions = main.read_sessions(tmp_path / "recordings", allow_unsigned=True)
    assert len(sessions) == 1
    return sessions[0], manifest_bytes, artifacts


def write_device_session_v2(
    tmp_path: Path,
    *,
    audio_state: str = "recorded",
    audio_body: bytes | None = None,
    mutate=None,
) -> tuple[main.Session, bytes, dict[str, dict]]:
    session_dir = tmp_path / "recordings" / f"device-session-v2-{audio_state}"
    left_video = device_session_artifact(
        session_dir,
        "video/left-00000.mp4",
        "video.left",
        "video/mp4",
        b"v2-left-video",
    )
    right_video = device_session_artifact(
        session_dir,
        "video/right-00000.mp4",
        "video.right",
        "video/mp4",
        b"v2-right-video",
    )
    imu = device_session_artifact(
        session_dir,
        "imu/imu.jsonl",
        "imu.samples",
        "application/x-ndjson",
        b'{"t":1,"gx":10}\n',
    )
    frames = device_session_artifact(
        session_dir,
        "imu/frames.jsonl",
        "frames.index",
        "application/x-ndjson",
        b'{"frame":1}\n',
    )
    artifacts = {
        "left_video": left_video,
        "right_video": right_video,
        "imu": imu,
        "frames": frames,
    }
    if audio_state == "recorded":
        audio_sample_count = 8000 * 29
        if audio_body is None:
            audio_body = pcm_s16le_wav_bytes(
                sample_rate=8000,
                channels=1,
                sample_count=audio_sample_count,
            )
        audio_artifact = device_session_artifact(
            session_dir,
            "audio/audio.wav",
            "audio.wav",
            "audio/wav",
            audio_body,
        )
        audio = {
            "state": "recorded",
            "requested_mode": "enabled",
            "resolved_mode": "enabled",
            "codec": "pcm_s16le",
            "container": "wav",
            "sample_format": "S16_LE",
            "sample_rate": 8000,
            "channels": 1,
            "sample_count": audio_sample_count,
            "sync": {
                "time_base": "host_monotonic",
                "start_time_seconds": 0,
                "end_time_seconds": 29,
                "video_time_reference": "session_time_seconds",
            },
            "segments": [
                {
                    "index": 0,
                    "start_sample": 0,
                    "end_sample": audio_sample_count,
                    "start_time_seconds": 0,
                    "end_time_seconds": 29,
                    "pcm_payload_bytes": audio_sample_count * 2,
                    "wav_header_bytes": 44,
                    "artifact": audio_artifact,
                }
            ],
        }
        artifacts["audio"] = audio_artifact
    elif audio_state == "not_recorded":
        audio = {
            "state": "not_recorded",
            "requested_mode": "disabled",
            "resolved_mode": "disabled",
            "reason": "user_disabled",
        }
    else:
        raise AssertionError(f"unsupported audio_state {audio_state}")

    manifest = {
        "schema": "ylx.device-session.v2",
        "manifest_id": DEVICE_MANIFEST_ID,
        "sealed": True,
        "sealed_at": "2026-08-21T04:00:29.200000Z",
        "session_id": DEVICE_SESSION_ID,
        "volume_id": DEVICE_VOLUME_ID,
        "capture_mode": "production",
        "display_name": "Device session v2 fixture",
        "device": {
            "device_id": DEVICE_UUID,
            "device_label": DEVICE_LABEL,
            "hardware_fingerprint": "sha256:" + "a" * 64,
            "platform": "rdk-x5",
            "software_version": "fixture",
            "commit": "b" * 40,
        },
        "time": {
            "started_at": "2026-08-21T04:00:00Z",
            "ended_at": "2026-08-21T04:00:29Z",
            "timezone": "Asia/Shanghai",
            "duration_seconds": 29,
        },
        "take": {
            "take_id": DEVICE_TAKE_ID,
            "sequence": 1,
            "continuation_of": None,
        },
        "camera": {
            "width": 3840,
            "height": 1080,
            "eye_width": 1920,
            "sensor_fps": 30,
            "frame_decimation": 1,
            "nominal_fps": 30,
            "effective_fps": 30,
            "coordinate_frame": "opencv_optical",
        },
        "video": {
            "layout": "split-eyes",
            "codec": "h264",
            "container": "mp4",
            "segments": [
                {
                    "index": 0,
                    "start_frame": 0,
                    "end_frame": 870,
                    "start_time_seconds": 0,
                    "end_time_seconds": 29,
                    "artifacts": {"left": left_video, "right": right_video},
                }
            ],
        },
        "imu": {
            "artifact": imu,
            "sample_count": 1,
            "units": "raw_int16",
            "coordinate_frame": "raw_device_axes",
        },
        "frames": {"artifact": frames, "count": 870},
        "audio": audio,
        "logs": [],
        "integrity": {
            "verified_at": "2026-08-21T04:00:29.100000Z",
            "dropped_frames": 0,
            "drop_events": [],
            "quality_policy": {
                "policy_id": "rdk-x5-lossless-v1",
                "max_contiguous_dropped_frames": 0,
                "max_total_dropped_frames": 0,
                "max_drop_fraction": 0,
                "window_seconds": 1,
                "max_dropped_frames_per_window": 0,
            },
            "fatal_errors": [],
        },
    }
    if mutate is not None:
        mutate(manifest)
    manifest_bytes = (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    (session_dir / "manifest.json").write_bytes(manifest_bytes)
    sessions = main.read_sessions(tmp_path / "recordings", allow_unsigned=True)
    assert len(sessions) == 1
    return sessions[0], manifest_bytes, artifacts


def test_device_session_v1_publishes_bucket_publication_v2_with_exact_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, manifest_bytes, artifacts = write_device_session_v1(tmp_path)
    main.verify(session)
    outputs, executions = normalized_outputs_with_state(
        session, tmp_path, monkeypatch, preset="ultrafast"
    )
    left, right = outputs
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(
        session, [right, left], "bucket", "raw", "ignored-legacy-device", "ultrafast"
    )

    base = f"raw/{DEVICE_UUID}/{DEVICE_SESSION_ID}"
    publication_key = f"{base}/__ylx_evidence__/publication.json"
    manifest_sha256 = digest(manifest_bytes)
    assert keys[-1] == publication_key
    assert store.puts[-1] == ("bucket", publication_key)
    publication = json.loads(store.objects[("bucket", publication_key)][0])
    assert publication["schema"] == "ylx.bucket-publication.v2"
    assert publication["sealed"] is True
    assert publication["device"] == {
        "device_id": DEVICE_UUID,
        "device_label": DEVICE_LABEL,
    }
    assert publication["source_manifest"] == {
        "manifest_id": DEVICE_MANIFEST_ID,
        "schema": "ylx.device-session.v1",
        "session_id": DEVICE_SESSION_ID,
        "volume_id": DEVICE_VOLUME_ID,
        "object_key": f"{base}/f-{manifest_sha256}",
        "bytes": len(manifest_bytes),
        "sha256": manifest_sha256,
    }
    assert (
        store.objects[("bucket", publication["source_manifest"]["object_key"])][0]
        == manifest_bytes
    )

    by_role = {entry["role"]: entry for entry in publication["artifacts"]}
    assert {"video.left", "video.right", "publication.transform-log"} <= set(by_role)
    assert (
        by_role["video.left"]["object_key"] == f"{base}/f-{digest(left.read_bytes())}"
    )
    assert (
        by_role["video.right"]["object_key"] == f"{base}/f-{digest(right.read_bytes())}"
    )
    assert by_role["video.left"]["provenance"] == {
        "kind": "normalized-output",
        "source_artifact_ids": [artifacts["raw_video"]["sha256"]],
        "transform": {
            "name": "ylx-card-pipeline.normalize",
            "version": "v1",
            "parameters": {
                "preset": "ultrafast",
                "rotation_degrees": 0,
                "source_video_layout": "raw-side-by-side",
            },
        },
    }
    transform_log_entry = by_role["publication.transform-log"]
    transform_log = json.loads(
        store.objects[("bucket", transform_log_entry["object_key"])][0]
    )
    assert transform_log["schema"] == "ylx.publication-transform-log.v1"
    assert transform_log["source_manifest_sha256"] == manifest_sha256
    assert transform_log["source_declarations"] == {
        "camera_coordinate_frame": "opencv_optical",
        "imu_coordinate_frame": "opencv_optical",
        "imu_units": "raw_int16",
        "video_codec": "mjpeg",
    }
    assert transform_log["pipeline"]["tool"]["build"]["build_id"] != "local"
    assert transform_log["pipeline"]["tool"]["build"]["artifact_sha256"] != "0" * 64
    assert [
        entry["argv"] for entry in transform_log["normalization"]["executions"]
    ] == [execution["argv"] for execution in executions]
    assert {
        entry["role"]: entry["source_artifact_ids"]
        for entry in transform_log["normalization"]["executions"]
    } == {
        "video.left": [artifacts["raw_video"]["sha256"]],
        "video.right": [artifacts["raw_video"]["sha256"]],
    }
    assert transform_log["normalization"]["executions"][0]["ffmpeg_version"] == (
        "ffmpeg version 6.1-test"
    )
    left_argv = transform_log["normalization"]["executions"][0]["argv"]
    assert left_argv[:6] == [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    assert left_argv[left_argv.index("-i") + 1] == str(
        session.directory / "video" / "raw-sbs.mjpeg"
    )
    assert left_argv[left_argv.index("-vf") + 1] == "crop=1920:1080:0:0"
    assert left_argv[left_argv.index("-preset") + 1] == "ultrafast"
    assert ffmpeg_option(left_argv, "-crf") == str(main.CRF_FOR_MJPEG_SOURCE)
    assert left_argv[left_argv.index("-pix_fmt") + 1] == "yuv420p"
    assert left_argv[left_argv.index("-movflags") + 1] == "+faststart"
    assert left_argv[-1] == str(left)
    assert transform_log["normalization"]["executions"][0]["exit_status"] == {
        "code": 0,
        "signal": None,
    }
    assert (
        transform_log_entry["provenance"]["transform"]["parameters"]["command"]
        == (executions[0]["argv"])
    )
    assert (
        transform_log_entry["provenance"]["transform"]["parameters"]["tool"]
        == (transform_log["pipeline"]["tool"])
    )


def test_device_session_v1_split_eyes_are_discovered_and_verified(
    tmp_path: Path,
) -> None:
    session, _, artifacts = write_device_session_v1(tmp_path, layout="split-eyes")

    assert session.source_codec == "h264"
    assert [artifact.display_path for artifact in session.videos("video_left")] == [
        artifacts["left_video"]["path"]
    ]
    assert [artifact.display_path for artifact in session.videos("video_right")] == [
        artifacts["right_video"]["path"]
    ]
    main.verify(session)


def test_device_session_v1_split_eyes_use_h264_crf18_in_state_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v1(tmp_path, layout="split-eyes")
    main.verify(session)

    outputs, executions = normalized_outputs_with_state(
        session, tmp_path, monkeypatch, preset="ultrafast"
    )
    state = json.loads(
        (outputs[0].parent / main.NORMALIZATION_STATE_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert state["cache_key"]["source_codec"] == "h264"
    assert [ffmpeg_option(execution["argv"], "-crf") for execution in executions] == [
        str(main.CRF_FOR_H264_SOURCE),
        str(main.CRF_FOR_H264_SOURCE),
    ]

    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(
        session, outputs, "bucket", "raw", "ignored-legacy-device", "ultrafast"
    )

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    transform_log_key = next(
        artifact["object_key"]
        for artifact in publication["artifacts"]
        if artifact["role"] == "publication.transform-log"
    )
    transform_log = json.loads(store.objects[("bucket", transform_log_key)][0])
    assert transform_log["source_declarations"]["video_codec"] == "h264"
    assert [
        ffmpeg_option(execution["argv"], "-crf")
        for execution in transform_log["normalization"]["executions"]
    ] == [
        str(main.CRF_FOR_H264_SOURCE),
        str(main.CRF_FOR_H264_SOURCE),
    ]


def test_device_session_v2_recorded_audio_publishes_v3_with_audio_pass_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, manifest_bytes, artifacts = write_device_session_v2(tmp_path)
    main.verify(session)
    outputs, _ = normalized_outputs_with_state(
        session, tmp_path, monkeypatch, preset="ultrafast"
    )
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(
        session, outputs, "bucket", "raw", "ignored-legacy-device", "ultrafast"
    )

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    base = f"raw/{DEVICE_UUID}/{DEVICE_SESSION_ID}"
    assert publication["schema"] == "ylx.bucket-publication.v3"
    assert publication["source_manifest"]["schema"] == "ylx.device-session.v2"
    assert publication["source_manifest"]["sha256"] == digest(manifest_bytes)
    assert publication["source_audio"] == {
        "state": "recorded",
        "source_artifact_ids": [artifacts["audio"]["sha256"]],
        "role": "audio.wav",
    }
    by_role = {entry["role"]: entry for entry in publication["artifacts"]}
    assert by_role["audio.wav"] == {
        "artifact_id": artifacts["audio"]["sha256"],
        "role": "audio.wav",
        "object_key": f"{base}/f-{artifacts['audio']['sha256']}",
        "media_type": "audio/wav",
        "bytes": artifacts["audio"]["bytes"],
        "sha256": artifacts["audio"]["sha256"],
        "provenance": {
            "kind": "device-artifact",
            "source_artifact_ids": [artifacts["audio"]["sha256"]],
        },
    }
    assert (
        store.objects[("bucket", by_role["audio.wav"]["object_key"])][0]
        == (session.directory / "audio" / "audio.wav").read_bytes()
    )
    transform_log_key = by_role["publication.transform-log"]["object_key"]
    transform_log = json.loads(store.objects[("bucket", transform_log_key)][0])
    assert transform_log["source_declarations"]["imu_coordinate_frame"] == (
        "raw_device_axes"
    )
    assert transform_log["source_audio"]["sample_count"] == 8000 * 29
    assert transform_log["source_audio"]["segments"][0]["artifact"] == {
        "artifact_id": artifacts["audio"]["sha256"],
        "role": "audio.wav",
        "path": "audio/audio.wav",
        "media_type": "audio/wav",
        "bytes": artifacts["audio"]["bytes"],
        "sha256": artifacts["audio"]["sha256"],
    }


def test_device_session_v2_not_recorded_audio_publishes_v3_without_audio_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v2(tmp_path, audio_state="not_recorded")
    main.verify(session)
    outputs, _ = normalized_outputs_with_state(
        session, tmp_path, monkeypatch, preset="ultrafast"
    )
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(
        session, outputs, "bucket", "raw", "ignored-legacy-device", "ultrafast"
    )

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    assert publication["schema"] == "ylx.bucket-publication.v3"
    assert publication["source_audio"] == {
        "state": "not_recorded",
        "reason": "user_disabled",
    }
    assert "audio.wav" not in {entry["role"] for entry in publication["artifacts"]}


def test_device_session_v2_rejects_hash_correct_non_wav_audio(tmp_path: Path) -> None:
    sample_count = 8000 * 29
    fake_wav_length_body = b"R" * (44 + sample_count * 2)
    session, _, _ = write_device_session_v2(tmp_path, audio_body=fake_wav_length_body)

    with pytest.raises(main.PipelineError, match="RIFF/WAVE|WAV"):
        main.verify(session)


def test_device_session_v2_rejects_wav_header_sample_rate_mismatch(
    tmp_path: Path,
) -> None:
    sample_count = 8000 * 29
    wrong_rate_wav = pcm_s16le_wav_bytes(
        sample_rate=16000,
        channels=1,
        sample_count=sample_count,
    )
    session, _, _ = write_device_session_v2(tmp_path, audio_body=wrong_rate_wav)

    with pytest.raises(main.PipelineError, match="sample_rate|sample rate"):
        main.verify(session)


def test_device_session_v2_keeps_raw_imu_axes_out_of_camera_fields(
    tmp_path: Path,
) -> None:
    session, _, _ = write_device_session_v2(tmp_path)

    assert session.source_manifest_schema == "ylx.device-session.v2"
    assert session.source_declarations["imu_coordinate_frame"] == "raw_device_axes"
    assert session.source_declarations["imu_units"] == "raw_int16"
    assert "video_codec" not in session.camera
    assert session.source_codec == "h264"


@pytest.mark.parametrize(
    ("fixture_name", "match"),
    (
        ("invalid/ylx-device-session-v2.missing-audio.json", "device-session v2"),
        (
            "invalid/ylx-device-session-v2.raw-imu-opencv-frame.json",
            "device-session v2",
        ),
        (
            "invalid/ylx-device-session-v2.audio-header-file-inconsistency.json",
            "audio artifact bytes",
        ),
        (
            "invalid/ylx-device-session-v2.audio-file-bytes-mismatch.json",
            "audio artifact bytes",
        ),
        (
            "invalid/ylx-device-session-v2.audio-sample-count-mismatch.json",
            "audio.sample_count",
        ),
        (
            "invalid/ylx-device-session-v2.audio-sample-rate-time-mismatch.json",
            "audio sync duration|audio segment duration",
        ),
        (
            "invalid/ylx-device-session-v2.audio-channel-payload-mismatch.json",
            "pcm_payload_bytes",
        ),
        (
            "invalid/ylx-device-session-v2.audio-not-recorded-extra-artifact.json",
            "device-session v2",
        ),
        (
            "invalid/ylx-device-session-v2.duplicate-audio-path.json",
            "duplicate artifact path",
        ),
    ),
)
def test_device_session_v2_rejects_current_central_hostile_corpus(
    tmp_path: Path, fixture_name: str, match: str
) -> None:
    recordings = tmp_path / "recordings"
    write_contract_manifest(
        recordings,
        Path(fixture_name).stem,
        load_contract_fixture(fixture_name),
    )

    with pytest.raises(main.PipelineError, match=match):
        main.read_sessions(recordings, allow_unsigned=True)


def test_bucket_publication_v3_rejects_wrong_major_and_bad_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v2(tmp_path)
    outputs, _ = normalized_outputs_with_state(session, tmp_path, monkeypatch)
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)
    keys = main.upload(session, outputs, "bucket", "raw", "ignored", "ultrafast")
    publication = json.loads(store.objects[("bucket", keys[-1])][0])

    wrong_major = json.loads(json.dumps(publication))
    wrong_major["schema"] = "ylx.bucket-publication.v4"
    with pytest.raises(main.PipelineError, match="unsupported bucket-publication"):
        main._validate_bucket_publication(
            wrong_major, session, session.source_manifest_size_bytes
        )

    wrong_source = json.loads(json.dumps(publication))
    wrong_source["source_manifest"]["schema"] = "ylx.device-session.v1"
    with pytest.raises(main.PipelineError, match="bucket-publication v3"):
        main._validate_bucket_publication(
            wrong_source, session, session.source_manifest_size_bytes
        )

    bad_provenance = json.loads(json.dumps(publication))
    bad_provenance["artifacts"][0]["provenance"]["source_artifact_ids"] = ["f" * 64]
    with pytest.raises(main.PipelineError, match="unknown sources|role/source"):
        main._validate_bucket_publication(
            bad_provenance, session, session.source_manifest_size_bytes
        )


def test_device_session_v2_upload_rejects_hardlinked_source_before_object_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v2(tmp_path)
    outputs, _ = normalized_outputs_with_state(session, tmp_path, monkeypatch)
    os.link(
        session.directory / "audio" / "audio.wav",
        session.directory / "audio" / "alias.wav",
    )
    forbid_object_store(monkeypatch)

    with pytest.raises(main.PipelineError, match="hardlinked"):
        main.upload(session, outputs, "bucket", "raw", "ignored", "ultrafast")


def test_legacy_publication_manifest_remains_explicitly_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"normalized-left")
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(session, [output], "bucket", "", "YLX-device", "slow")

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    assert publication["schema_version"] == 1
    assert publication["provenance"]["source_publication_manifest"]["path"] == (
        "publication_manifest.json"
    )


def test_read_sessions_returns_publications_oldest_first(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"

    def write_legacy_manifest(name: str, session_id: str, captured_at: str) -> None:
        session_dir = recordings / name
        session_dir.mkdir(parents=True)
        manifest = {
            "session_id": session_id,
            "revision": f"sha256:{session_id}",
            "captured_at": captured_at,
            "duration_seconds": 1.0,
            "integrity_ok": True,
            "files": [],
        }
        (session_dir / "publication_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    write_legacy_manifest("a-late", "legacy-late", "2026-08-21T04:00:02Z")
    write_legacy_manifest("z-early", "legacy-early", "2026-08-21T04:00:01Z")

    sessions = main.read_sessions(recordings, allow_unsigned=True)

    assert [session.session_id for session in sessions] == [
        "legacy-early",
        "legacy-late",
    ]


def test_device_session_unknown_major_schema_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["schema"] = "ylx.device-session.v3"

    with pytest.raises(main.PipelineError, match="unsupported device-session schema"):
        write_device_session_v1(tmp_path, mutate=mutate)


def test_device_session_v1_invalid_shape_fails_closed(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["sealed"] = False

    with pytest.raises(main.PipelineError, match="device-session v1"):
        write_device_session_v1(tmp_path, mutate=mutate)


def test_device_session_v1_rejects_path_traversal(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        manifest["video"]["artifact"]["path"] = "../raw-sbs.mjpeg"

    with pytest.raises(main.PipelineError, match="schema rejection"):
        write_device_session_v1(tmp_path, mutate=mutate)


def test_device_session_v1_rejects_duplicate_artifact_paths(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        left = manifest["video"]["segments"][0]["artifacts"]["left"]
        right = manifest["video"]["segments"][0]["artifacts"]["right"]
        right["path"] = left["path"]

    with pytest.raises(main.PipelineError, match="duplicate artifact path"):
        write_device_session_v1(tmp_path, layout="split-eyes", mutate=mutate)


def test_device_session_v1_verify_rejects_hash_mismatch(tmp_path: Path) -> None:
    session, _, _ = write_device_session_v1(tmp_path)
    (session.directory / "video" / "raw-sbs.mjpeg").write_bytes(b"tampered-data")

    with pytest.raises(main.PipelineError, match="declared SHA-256"):
        main.verify(session)


@pytest.mark.parametrize(
    ("case_name", "mutate"),
    (
        (
            "missing ended_at",
            lambda manifest: manifest["time"].pop("ended_at"),
        ),
        (
            "bad datetime",
            lambda manifest: manifest.__setitem__("sealed_at", "not-a-date"),
        ),
        (
            "nested extra",
            lambda manifest: manifest["camera"].__setitem__("extra", True),
        ),
        (
            "zero fps",
            lambda manifest: manifest["camera"].__setitem__("sensor_fps", 0),
        ),
        (
            "backslash path",
            lambda manifest: manifest["video"]["artifact"].__setitem__(
                "path", r"video\raw-sbs.mjpeg"
            ),
        ),
        (
            "reserved tmp path",
            lambda manifest: manifest["video"]["artifact"].__setitem__(
                "path", "video/raw.tmp"
            ),
        ),
    ),
)
def test_device_session_v1_official_schema_rejects_hostile_shapes(
    tmp_path: Path, case_name: str, mutate
) -> None:
    with pytest.raises(main.PipelineError, match="device-session v1 schema rejection"):
        write_device_session_v1(tmp_path, mutate=mutate)


def test_device_session_v1_rejects_duplicate_artifact_ids(tmp_path: Path) -> None:
    def mutate(manifest: dict) -> None:
        left = manifest["video"]["segments"][0]["artifacts"]["left"]
        right = manifest["video"]["segments"][0]["artifacts"]["right"]
        right["artifact_id"] = left["artifact_id"]
        right["sha256"] = left["sha256"]

    with pytest.raises(main.PipelineError, match="duplicate artifact_id"):
        write_device_session_v1(tmp_path, layout="split-eyes", mutate=mutate)


@pytest.mark.parametrize(
    ("case_name", "mutate", "match"),
    (
        (
            "non-contiguous segment index",
            lambda manifest: manifest["video"]["segments"][0].__setitem__("index", 2),
            "contiguous segment indices",
        ),
        (
            "segment frame interval",
            lambda manifest: (
                manifest["video"]["segments"][0].__setitem__("start_frame", 10),
                manifest["video"]["segments"][0].__setitem__("end_frame", 5),
            ),
            "segment frame interval",
        ),
        (
            "segment time interval",
            lambda manifest: (
                manifest["video"]["segments"][0].__setitem__("start_time_seconds", 0.4),
                manifest["video"]["segments"][0].__setitem__("end_time_seconds", 0.1),
            ),
            "segment time interval",
        ),
        (
            "effective fps mismatch",
            lambda manifest: manifest["camera"].__setitem__("effective_fps", 99),
            "effective_fps",
        ),
        (
            "drop count mismatch",
            lambda manifest: (
                manifest["integrity"].__setitem__("dropped_frames", 0),
                manifest["integrity"].__setitem__(
                    "drop_events",
                    [
                        {
                            "start_frame": 4,
                            "end_frame": 6,
                            "at_time_seconds": 0.2,
                            "reason": "write_backpressure",
                            "dropped": 2,
                        }
                    ],
                ),
            ),
            "dropped_frames",
        ),
    ),
)
def test_device_session_v1_procedural_invariants_reject_hostile_values(
    tmp_path: Path, case_name: str, mutate, match: str
) -> None:
    with pytest.raises(main.PipelineError, match=match):
        write_device_session_v1(tmp_path, layout="split-eyes", mutate=mutate)


def test_device_session_v1_accepts_consistent_dropped_frame_session(
    tmp_path: Path,
) -> None:
    def mutate(manifest: dict) -> None:
        manifest["camera"].pop("nominal_fps")
        manifest["integrity"].pop("quality_policy")
        manifest["integrity"]["dropped_frames"] = 2
        manifest["integrity"]["drop_events"] = [
            {
                "start_frame": 4,
                "end_frame": 6,
                "at_time_seconds": 0.2,
                "reason": "write_backpressure",
                "dropped": 2,
            }
        ]
        manifest["frames"]["count"] = 10

    session, _, _ = write_device_session_v1(
        tmp_path, layout="split-eyes", mutate=mutate
    )

    assert session.source_manifest_schema == main.DEVICE_SESSION_V1_SCHEMA
    assert [artifact.display_path for artifact in session.videos("video_left")] == [
        "video/left-00000.mp4"
    ]


def test_device_session_v1_measured_lossless_policy_rejects_dropped_frames(
    tmp_path: Path,
) -> None:
    def mutate(manifest: dict) -> None:
        manifest["integrity"]["dropped_frames"] = 2
        manifest["integrity"]["drop_events"] = [
            {
                "start_frame": 4,
                "end_frame": 6,
                "at_time_seconds": 0.2,
                "reason": "write_backpressure",
                "dropped": 2,
            }
        ]
        manifest["frames"]["count"] = 10
        manifest["camera"]["effective_fps"] = 25

    with pytest.raises(main.PipelineError, match="rdk-x5-lossless-v1|quality_policy"):
        write_device_session_v1(tmp_path, layout="split-eyes", mutate=mutate)


@pytest.mark.parametrize(
    ("fixture_name", "match"),
    (
        (
            "invalid/ylx-device-session-v1.adjacent-drop-events.json",
            "drop events",
        ),
        (
            "invalid/ylx-device-session-v1.effective-fps-mismatch.json",
            "effective_fps",
        ),
        (
            "invalid/ylx-device-session-v1.frame-count-mismatch.json",
            "frames.count",
        ),
        (
            "invalid/ylx-device-session-v1.successor-without-predecessor.json",
            "predecessor.*closed corpus",
        ),
    ),
)
def test_device_session_v1_rejects_current_central_hostile_corpus(
    tmp_path: Path, fixture_name: str, match: str
) -> None:
    recordings = tmp_path / "recordings"
    write_contract_manifest(
        recordings,
        Path(fixture_name).stem,
        load_contract_fixture(fixture_name),
    )

    with pytest.raises(main.PipelineError, match=match):
        main.read_sessions(recordings, allow_unsigned=True)


def test_read_sessions_accepts_current_central_closed_take_corpus(
    tmp_path: Path,
) -> None:
    recordings = tmp_path / "recordings"
    write_contract_manifest(
        recordings,
        "root",
        load_contract_fixture("valid/ylx-device-session-v1.json"),
    )
    write_contract_manifest(
        recordings,
        "continuation",
        load_contract_fixture("valid/ylx-device-session-v1.continuation.json"),
    )

    sessions = main.read_sessions(recordings, allow_unsigned=True)

    assert sorted(session.take["sequence"] for session in sessions) == [1, 2]


@pytest.mark.parametrize(
    ("case_name", "mutate", "match"),
    (
        (
            "sequence gap",
            lambda predecessor, successor: successor["take"].__setitem__("sequence", 3),
            "sequence is not predecessor.sequence \\+ 1|contiguous",
        ),
        (
            "different take",
            lambda predecessor, successor: successor["take"].__setitem__(
                "take_id", "01989f70-0000-7000-8000-000000000001"
            ),
            "different take_id|not contiguous|exactly one root",
        ),
        (
            "different device",
            lambda predecessor, successor: successor["device"].__setitem__(
                "device_id", "11111111-1111-4111-8111-111111111111"
            ),
            "canonical device_id|device",
        ),
        (
            "time discontinuity",
            lambda predecessor, successor: predecessor.__setitem__(
                "sealed_at", "2026-08-08T10:26:00+08:00"
            ),
            "not sealed before continuation started|time",
        ),
    ),
)
def test_read_sessions_rejects_closed_take_graph_hostile_shapes(
    tmp_path: Path, case_name: str, mutate, match: str
) -> None:
    predecessor = load_contract_fixture("valid/ylx-device-session-v1.json")
    successor = load_contract_fixture("valid/ylx-device-session-v1.continuation.json")
    mutate(predecessor, successor)
    recordings = tmp_path / "recordings"
    write_contract_manifest(recordings, "root", predecessor)
    write_contract_manifest(recordings, "continuation", successor)

    with pytest.raises(main.PipelineError, match=match):
        main.read_sessions(recordings, allow_unsigned=True)


def test_read_sessions_rejects_closed_take_graph_duplicate_session_id(
    tmp_path: Path,
) -> None:
    first = load_contract_fixture("valid/ylx-device-session-v1.json")
    second = json.loads(json.dumps(first))
    second["manifest_id"] = "01989f6a-2c02-7b2c-9d3e-4f5061728395"
    second["take"]["take_id"] = "01989f70-0000-7000-8000-000000000001"
    recordings = tmp_path / "recordings"
    write_contract_manifest(recordings, "first", first)
    write_contract_manifest(recordings, "second", second)

    with pytest.raises(main.PipelineError, match="duplicate session_id"):
        main.read_sessions(recordings, allow_unsigned=True)


def test_read_sessions_rejects_closed_take_graph_branch(
    tmp_path: Path,
) -> None:
    predecessor = load_contract_fixture("valid/ylx-device-session-v1.json")
    first = load_contract_fixture("valid/ylx-device-session-v1.continuation.json")
    second = json.loads(json.dumps(first))
    second["manifest_id"] = "01989f6b-0003-7003-8000-000000000003"
    second["session_id"] = "01989f6b-0004-7004-8000-000000000004"
    recordings = tmp_path / "recordings"
    write_contract_manifest(recordings, "root", predecessor)
    write_contract_manifest(recordings, "first", first)
    write_contract_manifest(recordings, "second", second)

    with pytest.raises(main.PipelineError, match="branches|duplicate take sequence"):
        main.read_sessions(recordings, allow_unsigned=True)


def test_split_eye_segments_keep_manifest_order_instead_of_path_order(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "recordings" / "device-session-v1"

    def mutate(manifest: dict) -> None:
        first = manifest["video"]["segments"][0]
        first["artifacts"] = {
            "left": device_session_artifact(
                session_dir,
                "video/z-left.mp4",
                "video.left",
                "video/mp4",
                b"first-left",
            ),
            "right": device_session_artifact(
                session_dir,
                "video/z-right.mp4",
                "video.right",
                "video/mp4",
                b"first-right",
            ),
        }
        manifest["video"]["segments"].append(
            {
                "index": 1,
                "start_frame": 12,
                "end_frame": 24,
                "start_time_seconds": 0.4,
                "end_time_seconds": 0.8,
                "artifacts": {
                    "left": device_session_artifact(
                        session_dir,
                        "video/a-left.mp4",
                        "video.left",
                        "video/mp4",
                        b"second-left",
                    ),
                    "right": device_session_artifact(
                        session_dir,
                        "video/a-right.mp4",
                        "video.right",
                        "video/mp4",
                        b"second-right",
                    ),
                },
            }
        )
        manifest["time"]["ended_at"] = "2026-08-21T04:00:00.800000Z"
        manifest["time"]["duration_seconds"] = 0.8
        manifest["integrity"]["verified_at"] = "2026-08-21T04:00:00.900000Z"
        manifest["frames"]["count"] = 24

    session, _, _ = write_device_session_v1(
        tmp_path, layout="split-eyes", mutate=mutate
    )

    assert [artifact.display_path for artifact in session.videos("video_left")] == [
        "video/z-left.mp4",
        "video/a-left.mp4",
    ]


@pytest.mark.parametrize(
    "prefix", ("../escape", r"raw\bad", "raw/__ylx_evidence__/bad")
)
def test_device_session_v1_upload_rejects_bad_prefix_before_object_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    session, _, _ = write_device_session_v1(tmp_path)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"normalized-left")
    right.write_bytes(b"normalized-right")

    def forbidden_object_store():
        raise AssertionError("object_store should not be created for invalid prefix")

    monkeypatch.setattr(main, "object_store", forbidden_object_store)
    with pytest.raises(main.PipelineError, match="invalid publication prefix"):
        main.upload(session, [left, right], "bucket", prefix, "ignored", "ultrafast")


def test_device_session_v1_upload_requires_normalization_state_before_object_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v1(tmp_path)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"normalized-left")
    right.write_bytes(b"normalized-right")

    def forbidden_object_store():
        raise AssertionError("object_store should not be created without state")

    monkeypatch.setattr(main, "object_store", forbidden_object_store)
    with pytest.raises(main.PipelineError, match="normalization state"):
        main.upload(session, [left, right], "bucket", "raw", "ignored", "ultrafast")


@pytest.mark.parametrize(
    ("case_name", "mutate", "match"),
    (
        (
            "runtime digest",
            lambda state: state["pipeline"]["tool"]["build"].__setitem__(
                "artifact_sha256", "f" * 64
            ),
            "pipeline\\.tool|tool metadata",
        ),
        (
            "ffmpeg argv",
            lambda state: state["executions"][0]["argv"].__setitem__(
                state["executions"][0]["argv"].index("-preset") + 1,
                "medium",
            ),
            "argv",
        ),
        (
            "ffmpeg version",
            lambda state: state["executions"][0].__setitem__(
                "ffmpeg_version", "ffmpeg version tampered"
            ),
            "ffmpeg version",
        ),
        (
            "duplicate output name",
            lambda state: state["outputs"].append(dict(state["outputs"][0])),
            "duplicate.*output",
        ),
        (
            "extra output name",
            lambda state: state["outputs"].append(
                {"name": "extra.mp4", "size_bytes": 1, "sha256": "a" * 64}
            ),
            "unexpected.*output|output inventory",
        ),
        (
            "duplicate execution role",
            lambda state: state["executions"].append(
                json.loads(json.dumps(state["executions"][0]))
            ),
            "duplicate.*execution|duplicate.*role",
        ),
        (
            "extra execution role",
            lambda state: (
                state["executions"].append(
                    json.loads(json.dumps(state["executions"][0]))
                ),
                state["executions"][-1].__setitem__("role", "video.depth"),
                state["executions"][-1]["output"].__setitem__("name", "depth.mp4"),
            ),
            "unexpected.*execution|execution inventory",
        ),
    ),
)
def test_device_session_v1_upload_rejects_tampered_normalization_state_before_object_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    mutate,
    match: str,
) -> None:
    session, _, _ = write_device_session_v1(tmp_path)
    outputs, _ = normalized_outputs_with_state(session, tmp_path, monkeypatch)
    tamper_normalization_state(outputs, mutate)
    forbid_object_store(monkeypatch)

    with pytest.raises(main.PipelineError, match=match):
        main.upload(session, outputs, "bucket", "raw", "ignored", "ultrafast")


def test_device_session_v1_emitted_publication_validates_against_official_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v1(tmp_path)
    main.verify(session)
    outputs, _ = normalized_outputs_with_state(session, tmp_path, monkeypatch)
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(session, outputs, "bucket", "raw", "ignored", "ultrafast")

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    schema_path = (
        Path(main.__file__).resolve().parent
        / "vendor"
        / "ylx-contracts"
        / "ylx-bucket-publication-v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(publication)


def test_device_session_v2_emitted_publication_validates_against_official_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _, _ = write_device_session_v2(tmp_path)
    main.verify(session)
    outputs, _ = normalized_outputs_with_state(session, tmp_path, monkeypatch)
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(session, outputs, "bucket", "raw", "ignored", "ultrafast")

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    schema_path = (
        Path(main.__file__).resolve().parent
        / "vendor"
        / "ylx-contracts"
        / "ylx-bucket-publication-v3.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(publication)


def test_vendored_contract_source_metadata_matches_runtime_pins() -> None:
    contract_root = Path(main.__file__).resolve().parent / "vendor" / "ylx-contracts"
    source = json.loads((contract_root / "SOURCE.json").read_text(encoding="utf-8"))
    expected = {
        "ylx-device-session-v1.schema.json": (
            "contracts/schemas/ylx-device-session-v1.schema.json",
            main.YLX_DEVICE_SESSION_V1_SCHEMA_SHA256,
        ),
        "ylx-device-session-v2.schema.json": (
            "contracts/schemas/ylx-device-session-v2.schema.json",
            main.YLX_DEVICE_SESSION_V2_SCHEMA_SHA256,
        ),
        "ylx-bucket-publication-v2.schema.json": (
            "contracts/schemas/ylx-bucket-publication-v2.schema.json",
            main.YLX_BUCKET_PUBLICATION_V2_SCHEMA_SHA256,
        ),
        "ylx-bucket-publication-v3.schema.json": (
            "contracts/schemas/ylx-bucket-publication-v3.schema.json",
            main.YLX_BUCKET_PUBLICATION_V3_SCHEMA_SHA256,
        ),
    }

    assert source["source_repo"] == main.YLX_CONTRACT_SOURCE_REPO
    assert source["source_ref"] == main.YLX_CONTRACT_SOURCE_REF
    assert source["source_commit"] == main.YLX_CONTRACT_SOURCE_COMMIT
    assert source["source_root"] == main.YLX_CONTRACT_SOURCE_ROOT
    assert "requested_authority" not in source
    assert source["source_paths"] == {
        name: source_path for name, (source_path, _) in expected.items()
    }
    assert set(source["schemas"]) == set(expected)
    for name, (source_path, expected_sha256) in expected.items():
        schema_metadata = source["schemas"][name]
        assert schema_metadata["source_path"] == source_path
        assert schema_metadata["sha256"] == expected_sha256
        assert digest((contract_root / name).read_bytes()) == expected_sha256
    assert source["validator"]["sha256"] == (
        "da72760fcd4d766cf8591fd571ac5c440b6654b19835c86c3e24ca8323808151"
    )
    assert source["docs"]["contracts/README.md"] == (
        "4c80eab908bb0b7167f81cb4d27c9e6d2fe5cb63857cefbf4db966e4bec912b8"
    )
    assert {
        "contracts/fixtures/valid/ylx-device-session-v2.audio-recorded.json",
        "contracts/fixtures/valid/ylx-bucket-publication-v3.audio-recorded.json",
        "contracts/fixtures/invalid/ylx-device-session-v2.audio-header-file-inconsistency.json",
        "contracts/fixtures/invalid/ylx-bucket-publication-v3.wrong-source-major.json",
    } <= set(source["corpora"])
    for source_path, expected_sha256 in source["corpora"].items():
        relative = source_path.removeprefix("contracts/fixtures/")
        assert digest(main.ylx_contract_fixture(relative).read_bytes()) == (
            expected_sha256
        )
    main._ylx_contract_source_metadata.cache_clear()
    assert main._ylx_contract_source_metadata()["source_commit"] == (
        main.YLX_CONTRACT_SOURCE_COMMIT
    )


def test_vendored_contract_gate_rejects_source_path_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_root = copy_ylx_contract_root(tmp_path)
    source_path = contract_root / "SOURCE.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["source_paths"]["ylx-device-session-v1.schema.json"] = (
        "contracts/schemas/tampered-device-session-v1.schema.json"
    )
    source["schemas"]["ylx-device-session-v1.schema.json"]["source_path"] = (
        "contracts/schemas/tampered-device-session-v1.schema.json"
    )
    source_path.write_text(
        json.dumps(source, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(main, "YLX_CONTRACT_ROOT", contract_root)
    clear_ylx_contract_caches()
    try:
        with pytest.raises(main.PipelineError, match="source path"):
            main._device_session_v1_validator()
    finally:
        clear_ylx_contract_caches()


def test_vendored_contract_gate_rejects_schema_symlink_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_root = copy_ylx_contract_root(tmp_path)
    schema_path = contract_root / "ylx-device-session-v1.schema.json"
    symlink_target = tmp_path / "same-bytes-device-session-v1.schema.json"
    symlink_target.write_bytes(schema_path.read_bytes())
    schema_path.unlink()
    schema_path.symlink_to(symlink_target)

    monkeypatch.setattr(main, "YLX_CONTRACT_ROOT", contract_root)
    clear_ylx_contract_caches()
    try:
        with pytest.raises(main.PipelineError, match="regular|symlink"):
            main._device_session_v1_validator()
    finally:
        clear_ylx_contract_caches()


def test_cli_help_never_loads_repository_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_env_load(_path: Path) -> None:
        raise AssertionError("CLI touched a repository env file")

    monkeypatch.setattr(main, "load_env_file", reject_env_load)
    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])
    with pytest.raises(SystemExit) as exited:
        main.main()
    assert exited.value.code == 0


def test_upload_preserves_sealed_identity_keys_hashes_and_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    main.verify(session)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"normalized-left")
    right.write_bytes(b"normalized-right")
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(
        session, [right, left], "bucket", "raw/", "YLX-device", "ultrafast"
    )

    base = "raw/YLX-device/capture-001"
    assert keys == [
        f"{base}/{main.file_id_for('video/left.mp4')}",
        f"{base}/{main.file_id_for('video/right.mp4')}",
        f"{base}/__ylx_evidence__/publication.json",
    ]
    publication_bytes, content_type = store.objects[("bucket", keys[-1])]
    assert content_type == "application/json"
    publication = json.loads(publication_bytes)
    assert publication["session_id"] == session.session_id
    assert [entry["sha256"] for entry in publication["files"]] == [
        digest(left.read_bytes()),
        digest(right.read_bytes()),
    ]
    assert publication["provenance"]["source_publication_manifest"] == {
        "path": "publication_manifest.json",
        "sha256": session.source_manifest_sha256,
        "revision": "sha256:card-revision",
        "session_id": "capture-001",
    }
    relations = publication["provenance"]["derived_relations"]
    assert [relation["output_display_path"] for relation in relations] == [
        "video/left.mp4",
        "video/right.mp4",
    ]
    assert relations[0]["inputs"] == [
        {
            "display_path": "spool/source_00000.mp4",
            "sha256": digest(b"recorded-card-video"),
        }
    ]


def test_upload_publishes_verified_imu_and_metadata_with_correct_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path, with_auxiliary=True)
    main.verify(session)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"normalized-left")
    right.write_bytes(b"normalized-right")
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(
        session,
        [left, right],
        "bucket",
        "",
        "YLX-device",
        "slow",
        rotation_degrees=180,
    )

    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    files_by_path = {entry["display_path"]: entry for entry in publication["files"]}
    assert set(files_by_path) == {
        "video/left.mp4",
        "video/right.mp4",
        "session.json",
        "capture.commit.json",
        "raw/imu.jsonl",
    }
    assert files_by_path["raw/imu.jsonl"]["role"] == "imu"
    assert files_by_path["raw/imu.jsonl"]["media_type"] == "application/x-ndjson"
    assert files_by_path["session.json"]["role"] == "metadata"
    assert publication["video_bytes"] == len(left.read_bytes()) + len(
        right.read_bytes()
    )
    assert publication["total_bytes"] == sum(
        entry["size_bytes"] for entry in publication["files"]
    )
    assert publication["normalization"]["rotation_degrees"] == 180
    assert publication["revision"].startswith("sha256:")

    for display_path in ("session.json", "capture.commit.json", "raw/imu.jsonl"):
        entry = files_by_path[display_path]
        key = f"YLX-device/{session.session_id}/{entry['id']}"
        assert (
            store.objects[("bucket", key)][0]
            == (session.directory / display_path).read_bytes()
        )
        assert store.metadata[("bucket", key)]["sha256"] == entry["sha256"]
    imu_relation = next(
        relation
        for relation in publication["provenance"]["derived_relations"]
        if relation["output_display_path"] == "raw/imu.jsonl"
    )
    assert imu_relation == {
        "output_display_path": "raw/imu.jsonl",
        "output_sha256": files_by_path["raw/imu.jsonl"]["sha256"],
        "inputs": [
            {
                "display_path": "raw/imu.jsonl",
                "sha256": files_by_path["raw/imu.jsonl"]["sha256"],
            }
        ],
        "operation": "copy_verified_source",
    }


def test_interrupted_upload_resumes_matching_objects_and_manifest_remains_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailRightOnceStore(MemoryObjectStore):
        failed = False

        def upload_file(
            self, filename: str, bucket: str, key: str, ExtraArgs: dict
        ) -> None:
            if not self.failed and Path(filename).name == "right.mp4":
                self.failed = True
                raise OSError("simulated disconnect")
            super().upload_file(filename, bucket, key, ExtraArgs)

    session = write_card_session(tmp_path)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"normalized-left")
    right.write_bytes(b"normalized-right")
    store = FailRightOnceStore()
    monkeypatch.setattr(main, "object_store", lambda: store)
    manifest_key = "YLX-device/capture-001/__ylx_evidence__/publication.json"

    with pytest.raises(main.PipelineError, match="simulated disconnect"):
        main.upload(session, [left, right], "bucket", "", "YLX-device", "slow")
    left_key = f"YLX-device/capture-001/{main.file_id_for('video/left.mp4')}"
    assert store.uploads == [("bucket", left_key)]
    assert ("bucket", manifest_key) not in store.objects

    keys = main.upload(session, [left, right], "bucket", "", "YLX-device", "slow")
    right_key = f"YLX-device/capture-001/{main.file_id_for('video/right.mp4')}"
    assert store.uploads.count(("bucket", left_key)) == 1
    assert store.uploads.count(("bucket", right_key)) == 1
    assert keys[-1] == manifest_key
    assert ("bucket", manifest_key) in store.objects


def test_completed_upload_is_idempotent_when_every_digest_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path, with_auxiliary=True)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"normalized-left")
    right.write_bytes(b"normalized-right")
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    first_keys = main.upload(
        session, [left, right], "bucket", "raw", "YLX-device", "slow"
    )
    first_uploads = list(store.uploads)
    manifest_key = first_keys[-1]
    manifest_put_count = store.puts.count(("bucket", manifest_key))

    second_keys = main.upload(
        session, [right, left], "bucket", "raw", "YLX-device", "slow"
    )
    assert second_keys == first_keys
    assert store.uploads == first_uploads
    assert store.puts.count(("bucket", manifest_key)) == manifest_put_count


def test_verify_rejects_manifest_replaced_after_discovery(tmp_path: Path) -> None:
    session = write_card_session(tmp_path)
    session.source_manifest_path.write_text('{"replacement": true}', encoding="utf-8")

    with pytest.raises(main.PipelineError, match="changed after discovery"):
        main.verify(session)


def test_draft_rp_signature_kat_and_full_admission_inputs() -> None:
    frozen_raw = (RP_FIXTURE_ROOT / "publication_manifest.json").read_bytes()
    raw = (RP_FIXTURE_ROOT / "admission_manifest.json").read_bytes()
    registry_raw = (
        RP_FIXTURE_ROOT / "synthetic-trusted-key-registry.json"
    ).read_bytes()
    registry = main.parse_strict_json(registry_raw, "test registry")
    manifest = main.parse_strict_json(raw, "test manifest")
    state = main.verify_source_signature(manifest, registry, "paired-device-001")
    assert state["status"] == "sealed"
    assert (
        main._digest(main.VENDOR_ROOT / "publication-manifest-v1.schema.json")
        == main.RP_MANIFEST_SCHEMA_SHA256
    )
    assert main._digest(RP_FIXTURE_ROOT / "golden-vector.json") == main.RP_KAT_SHA256
    assert (
        main._digest(RP_FIXTURE_ROOT / "admission_manifest.json")
        == main.RP_ADMISSION_MANIFEST_SHA256
    )
    assert (
        main._digest(RP_FIXTURE_ROOT / "admission-vector.json")
        == main.RP_ADMISSION_VECTOR_SHA256
    )
    frozen = main.parse_strict_json(frozen_raw, "frozen crypto KAT")
    assert (
        hashlib.sha256(main.canonical_signature_payload(frozen)).hexdigest()
        == "f8f890d883cf7c204d1549a125967a4e1a70e60e1c66e8c8f66045301dd0b2c8"
    )
    for bad in (
        raw.replace(b'"signature":"66', b'"signature":"76'),
        raw.replace(
            b'"session_id":"sess-0001"',
            b'"session_id":"sess-0001","session_id":"other"',
        ),
        raw.replace(b'"duration_seconds":121.4', b'"duration_seconds":NaN'),
        raw.replace(b'"duration_seconds":121.4', b'"duration_seconds":Infinity'),
        raw.replace(b'"duration_seconds":121.4', b'"duration_seconds":-Infinity'),
        raw.replace(b'"duration_seconds":121.4', b'"duration_seconds":1e400'),
        raw.replace(b'"session_id":"sess-0001"', b'"session_id":"\\ud800"'),
        raw.replace(
            b'"session_id":"sess-0001"', b'"\\udfff":"value","session_id":"sess-0001"'
        ),
        b'{"x":"\xff"}',
    ):
        with pytest.raises(main.PipelineError):
            parsed = main.parse_strict_json(bad, "negative")
            main.verify_source_signature(parsed, registry, "paired-device-001")
    with pytest.raises(main.PipelineError, match="unknown external device"):
        main.verify_source_signature(manifest, registry, "wrong-device")
    with pytest.raises(main.PipelineError, match="refusing downgrade"):
        main.verify_source_signature(manifest, None, "paired-device-001")
    bad_total = dict(manifest)
    bad_total["total_bytes"] += 1
    with pytest.raises(main.PipelineError, match="byte totals"):
        main.verify_source_signature(bad_total, registry, "paired-device-001")
    bad_duration = dict(manifest)
    bad_duration["duration_seconds"] = float("inf")
    with pytest.raises(main.PipelineError, match="schema rejection"):
        main.verify_source_signature(bad_duration, registry, "paired-device-001")
    bad_revision = resign_admission_manifest(
        {**manifest, "revision": "sha256:" + "d" * 64}
    )
    with pytest.raises(main.PipelineError, match="revision does not match"):
        main.verify_source_signature(bad_revision, registry, "paired-device-001")
    bad_time = resign_admission_manifest(
        {**manifest, "published_at": "2026-07-31T23:59:59Z"}
    )
    with pytest.raises(main.PipelineError, match="published_at precedes"):
        main.verify_source_signature(bad_time, registry, "paired-device-001")
    expired = dict(registry)
    expired["expires_at"] = "2026-01-02T00:00:00Z"
    with pytest.raises(main.PipelineError, match="stale"):
        main.verify_source_signature(
            manifest, expired, "paired-device-001", datetime(2026, 8, 1, tzinfo=UTC)
        )
    revoked = json.loads(json.dumps(registry))
    revoked["bindings"]["paired-device-001"]["1"]["status"] = "revoked"
    revoked["bindings"]["paired-device-001"]["1"]["revoked_at"] = "2026-01-01T00:00:00Z"
    with pytest.raises(main.PipelineError, match="unavailable or revoked"):
        main.verify_source_signature(
            manifest, revoked, "paired-device-001", datetime(2026, 8, 1, tzinfo=UTC)
        )


def test_binding_receipt_requires_out_of_band_signature_and_policy() -> None:
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key()
    public_pem = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_raw = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    receipt = {
        "schema_version": "ylx.authenticated-binding-receipt.v1",
        "status": "active",
        "issuer": "https://ci.example",
        "identity": "repo:ylx:pairing",
        "audience": "ylx-card-pipeline",
        "external_device_identity": "paired-device-001",
        "inventory_revision": "sha256:" + "a" * 64,
        "registry_revision": "synthetic-r1",
        "not_before": "2026-08-01T00:00:00Z",
        "not_after": "2026-08-02T00:00:00Z",
        "nonce": "binding_nonce_001",
        "public_key_fingerprint": f"sha256:{hashlib.sha256(public_raw).hexdigest()}",
    }
    payload = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    receipt["signature"] = base64.b64encode(key.sign(payload)).decode("ascii")
    raw = json.dumps(receipt, separators=(",", ":")).encode()
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert main.authenticated_binding_receipt(
        raw,
        now,
        public_pem,
        "https://ci.example",
        "repo:ylx:pairing",
        "ylx-card-pipeline",
        "synthetic-r1",
    ) == ("paired-device-001", "sha256:" + "a" * 64)

    tampered = {**receipt, "external_device_identity": "attacker"}
    with pytest.raises(main.PipelineError, match="signature verification"):
        main.authenticated_binding_receipt(
            json.dumps(tampered).encode(),
            now,
            public_pem,
            "https://ci.example",
            "repo:ylx:pairing",
            "ylx-card-pipeline",
            "synthetic-r1",
        )
    with pytest.raises(main.PipelineError, match="receipt is invalid"):
        main.authenticated_binding_receipt(
            raw,
            now,
            public_pem,
            "https://other",
            "repo:ylx:pairing",
            "ylx-card-pipeline",
            "synthetic-r1",
        )


def test_registry_schema_and_revocation_lifecycle_are_strict() -> None:
    registry = json.loads(
        (RP_FIXTURE_ROOT / "synthetic-trusted-key-registry.json").read_text(
            encoding="utf-8"
        )
    )
    now = datetime(2026, 8, 1, tzinfo=UTC)
    unknown = {**registry, "unexpected": True}
    with pytest.raises(main.PipelineError, match="schema rejection"):
        main.validate_registry(unknown, now)
    active_with_revocation = json.loads(json.dumps(registry))
    active_with_revocation["bindings"]["paired-device-001"]["1"]["revoked_at"] = (
        "2026-07-01T00:00:00Z"
    )
    with pytest.raises(main.PipelineError, match="schema rejection"):
        main.validate_registry(active_with_revocation, now)
    future_revocation = json.loads(json.dumps(registry))
    key = future_revocation["bindings"]["paired-device-001"]["1"]
    key["status"] = "revoked"
    key["revoked_at"] = "2026-09-01T00:00:00Z"
    with pytest.raises(main.PipelineError, match="revocation lifecycle"):
        main.validate_registry(future_revocation, now)


def test_read_sessions_skips_symlinked_session_and_manifest(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    target = tmp_path / "outside-session"
    write_card_session(tmp_path)
    target.mkdir()
    (recordings / "linked-session").symlink_to(target, target_is_directory=True)
    manifest_target = tmp_path / "outside-manifest.json"
    manifest_target.write_text("{}", encoding="utf-8")
    (recordings / "card-directory-name" / "publication_manifest.json").unlink()
    (recordings / "card-directory-name" / "publication_manifest.json").symlink_to(
        manifest_target
    )

    assert main.read_sessions(recordings, allow_unsigned=True) == []


def test_session_json_root_swap_cannot_read_outside_camera_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    (session.directory / "session.json").write_text(
        json.dumps({"camera": {"video_codec": "h264"}}), encoding="utf-8"
    )
    outside = tmp_path / "outside-session"
    outside.mkdir()
    (outside / "session.json").write_text(
        json.dumps({"camera": {"video_codec": "attacker"}}), encoding="utf-8"
    )
    original_open = main.os.open
    root_opens = 0

    def racing_open(path, flags, *args, **kwargs):
        nonlocal root_opens
        if Path(path) == session.directory and kwargs.get("dir_fd") is None:
            root_opens += 1
            if root_opens == 2:
                session.directory.rename(
                    session.directory.with_name("card-directory-real")
                )
                session.directory.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(main.os, "open", racing_open)
    reread = main.read_sessions(tmp_path / "recordings", allow_unsigned=True)
    assert len(reread) == 1
    assert reread[0].camera == {}


def test_malformed_inline_signature_never_downgrades_when_unsigned_is_allowed(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "recordings" / "signed-session"
    session_dir.mkdir(parents=True)
    manifest = (RP_FIXTURE_ROOT / "admission_manifest.json").read_text(encoding="utf-8")
    manifest = manifest.replace('"signature":"6', '"signature":"7', 1)
    (session_dir / "publication_manifest.json").write_text(manifest, encoding="utf-8")
    registry = json.loads(
        (RP_FIXTURE_ROOT / "synthetic-trusted-key-registry.json").read_text(
            encoding="utf-8"
        )
    )
    with pytest.raises(main.PipelineError, match="signature verification"):
        main.read_sessions(
            tmp_path / "recordings", registry, "paired-device-001", allow_unsigned=True
        )


def test_snapshot_fails_closed_when_source_changes_after_initial_verification(
    tmp_path: Path,
) -> None:
    session = write_card_session(tmp_path)
    main.verify(session)
    (session.directory / "spool" / "source_00000.mp4").write_bytes(b"substituted bytes")

    with pytest.raises(
        main.PipelineError, match="changed while creating source snapshot"
    ):
        main.snapshot_session(session, tmp_path / "work")


def test_snapshot_rejects_intermediate_directory_replaced_by_symlink_during_openat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source_00000.mp4").write_bytes(b"attacker bytes")
    original_open = main.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "spool" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            spool = session.directory / "spool"
            spool.rename(session.directory / "spool-real")
            spool.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(main.os, "open", racing_open)
    with pytest.raises(main.PipelineError, match="cannot open spool/source_00000.mp4"):
        main.snapshot_session(session, tmp_path / "work")


def test_normalize_transcodes_both_stereo_halves_in_left_right_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    session_json = session.directory / "session.json"
    session_json.write_text(
        json.dumps({"camera": {"width": 3840, "height": 1080, "video_codec": "h264"}}),
        encoding="utf-8",
    )
    # Re-read so normalization obtains its dimensions exactly as it would on a card.
    session = main.read_sessions(tmp_path / "recordings", allow_unsigned=True)[0]
    calls = []

    def fake_encode(**kwargs) -> None:
        calls.append(kwargs)
        kwargs["output"].write_bytes(b"encoded")

    monkeypatch.setattr(main, "encode", fake_encode)
    outputs = main.normalize(session, tmp_path / "work", "ultrafast")

    assert [output.name for output in outputs] == ["left.mp4", "right.mp4"]
    assert [call["crop"] for call in calls] == [
        "crop=1920:1080:0:0",
        "crop=1920:1080:1920:0",
    ]
    assert [call["crf"] for call in calls] == [main.CRF_FOR_H264_SOURCE] * 2


def test_video_segments_use_natural_numeric_order_without_zero_padding(
    tmp_path: Path,
) -> None:
    session = write_card_session(tmp_path)
    artifacts = tuple(
        main.Artifact(
            f"spool/source_{number}.mp4",
            "video_stereo",
            1,
            hashlib.sha256(str(number).encode()).hexdigest(),
        )
        for number in (1, 10, 2)
    )
    session = main.dataclasses.replace(session, artifacts=artifacts)

    assert [artifact.display_path for artifact in session.videos("video_stereo")] == [
        "spool/source_1.mp4",
        "spool/source_2.mp4",
        "spool/source_10.mp4",
    ]


def test_rotation_happens_before_stereo_crop_and_completed_outputs_are_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    (session.directory / "session.json").write_text(
        json.dumps({"camera": {"width": 3840, "height": 1080, "video_codec": "h264"}}),
        encoding="utf-8",
    )
    session = main.read_sessions(tmp_path / "recordings", allow_unsigned=True)[0]
    calls = []

    def fake_encode(**kwargs) -> None:
        calls.append(kwargs)
        kwargs["output"].write_bytes(f"encoded-{kwargs['label']}".encode())

    monkeypatch.setattr(main, "encode", fake_encode)
    first = main.normalize(
        session,
        tmp_path / "work",
        "ultrafast",
        rotation_degrees=180,
    )
    assert [call["crop"] for call in calls] == [
        "hflip,vflip,crop=1920:1080:0:0",
        "hflip,vflip,crop=1920:1080:1920:0",
    ]
    state_path = first[0].parent / main.NORMALIZATION_STATE_FILENAME
    assert state_path.is_file()

    calls.clear()
    second = main.normalize(
        session,
        tmp_path / "work",
        "ultrafast",
        rotation_degrees=180,
    )
    assert second == first
    assert calls == []

    main.normalize(
        session,
        tmp_path / "work",
        "ultrafast",
        rotation_degrees=180,
        reuse_completed=False,
    )
    assert len(calls) == 2


def test_rotation_swaps_already_split_eye_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    left_path = session.directory / "video" / "left_00000.mp4"
    right_path = session.directory / "video" / "right_00000.mp4"
    left_path.parent.mkdir()
    left_path.write_bytes(b"source left")
    right_path.write_bytes(b"source right")
    left = main.Artifact(
        "video/left_00000.mp4", "video_left", 11, digest(b"source left")
    )
    right = main.Artifact(
        "video/right_00000.mp4", "video_right", 12, digest(b"source right")
    )
    split_session = main.dataclasses.replace(session, artifacts=(left, right))
    calls = []

    def fake_encode(**kwargs) -> None:
        calls.append(kwargs)
        kwargs["output"].write_bytes(b"encoded")

    monkeypatch.setattr(main, "encode", fake_encode)
    main.normalize(split_session, tmp_path / "work", "slow", rotation_degrees=180)

    assert calls[0]["inputs"] == [right_path]
    assert calls[1]["inputs"] == [left_path]
    assert [call["crop"] for call in calls] == ["hflip,vflip", "hflip,vflip"]
    relations = main.build_provenance(
        split_session,
        [
            {
                "display_path": "video/left.mp4",
                "role": "video_left",
                "sha256": "out-left",
            },
            {
                "display_path": "video/right.mp4",
                "role": "video_right",
                "sha256": "out-right",
            },
        ],
        rotation_degrees=180,
    )["derived_relations"]
    assert relations[0]["inputs"] == [
        {"display_path": right.display_path, "sha256": right.sha256}
    ]
    assert relations[1]["inputs"] == [
        {"display_path": left.display_path, "sha256": left.sha256}
    ]


def test_rejects_identity_that_can_escape_an_object_key_component(
    tmp_path: Path,
) -> None:
    with pytest.raises(main.PipelineError, match="invalid session_id"):
        write_card_session(tmp_path, "other-device/session")


def test_verify_rejects_artifact_path_that_escapes_session(tmp_path: Path) -> None:
    session = write_card_session(tmp_path)
    escaped = main.Artifact("../outside.mp4", "video_stereo", 1, digest(b"x"))
    escaped_session = main.dataclasses.replace(session, artifacts=(escaped,))

    with pytest.raises(main.PipelineError, match="escapes the session directory"):
        main.verify(escaped_session)


def test_upload_failure_does_not_write_completion_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingStore(MemoryObjectStore):
        def upload_file(self, *args, **kwargs) -> None:
            raise OSError("connection lost")

    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"normalized-left")
    store = FailingStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    with pytest.raises(main.PipelineError, match="publishing capture-001"):
        main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    assert store.objects == {}


def test_republish_removes_old_completion_manifest_before_fixed_key_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingStore(MemoryObjectStore):
        def upload_file(self, *args, **kwargs) -> None:
            raise OSError("connection lost")

    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"normalized-left")
    store = FailingStore()
    manifest_key = "YLX-device/capture-001/__ylx_evidence__/publication.json"
    store.objects[("bucket", manifest_key)] = (b"old completion", "application/json")
    monkeypatch.setattr(main, "object_store", lambda: store)

    with pytest.raises(main.PipelineError, match="publishing capture-001"):
        main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    assert ("bucket", manifest_key) not in store.objects


def test_upload_uses_staged_output_when_caller_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MutatingStore(MemoryObjectStore):
        def upload_file(
            self, filename: str, bucket: str, key: str, ExtraArgs: dict
        ) -> None:
            output.write_bytes(b"changed after snapshot")
            super().upload_file(filename, bucket, key, ExtraArgs)

    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    original = b"stable normalized output"
    output.write_bytes(original)
    store = MutatingStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    assert store.objects[("bucket", keys[0])][0] == original
    assert publication["files"][0]["sha256"] == digest(original)


def test_split_eye_provenance_relates_each_output_to_its_own_input(
    tmp_path: Path,
) -> None:
    session = write_card_session(tmp_path)
    left = main.Artifact("video/left_00000.mp4", "video_left", 1, "left-hash")
    right = main.Artifact("video/right_00000.mp4", "video_right", 1, "right-hash")
    split_session = main.dataclasses.replace(session, artifacts=(left, right))

    relations = main.build_provenance(
        split_session,
        [
            {
                "display_path": "video/left.mp4",
                "role": "video_left",
                "sha256": "out-left",
            },
            {
                "display_path": "video/right.mp4",
                "role": "video_right",
                "sha256": "out-right",
            },
        ],
    )["derived_relations"]
    assert relations[0]["inputs"] == [
        {"display_path": left.display_path, "sha256": "left-hash"}
    ]
    assert relations[1]["inputs"] == [
        {"display_path": right.display_path, "sha256": "right-hash"}
    ]


def test_concurrent_uploader_cannot_interleave_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InterleavingStore(MemoryObjectStore):
        nested_attempted = False

        def upload_file(
            self, filename: str, bucket: str, key: str, ExtraArgs: dict
        ) -> None:
            if not self.nested_attempted:
                self.nested_attempted = True
                with pytest.raises(
                    main.PipelineError, match="acquiring publication lease"
                ):
                    main.upload(session, [right], "bucket", "", "YLX-device", "slow")
            super().upload_file(filename, bucket, key, ExtraArgs)

    session = write_card_session(tmp_path)
    left, right = tmp_path / "left.mp4", tmp_path / "right.mp4"
    left.write_bytes(b"left bytes")
    right.write_bytes(b"right bytes")
    store = InterleavingStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    keys = main.upload(session, [left], "bucket", "", "YLX-device", "slow")
    publication = json.loads(store.objects[("bucket", keys[-1])][0])
    assert [file["display_path"] for file in publication["files"]] == ["video/left.mp4"]
    assert not any(key.endswith("publication.lock") for _, key in store.objects)


def test_staging_oserror_happens_before_any_publication_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"bytes")
    store = MemoryObjectStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    def unavailable_staging(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(main.tempfile, "TemporaryDirectory", unavailable_staging)
    with pytest.raises(OSError, match="disk full"):
        main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    assert store.objects == {}


def test_late_owner_release_cannot_delete_recovered_lease(tmp_path: Path) -> None:
    session = write_card_session(tmp_path)
    store = MemoryObjectStore()
    base = f"YLX-device/{session.session_id}"
    old_lease = main._acquire_publication_lease(store, "bucket", base)
    # Simulate an operator removing a stale lock then a new owner acquiring it.
    store.delete_object(Bucket="bucket", Key=old_lease.key)
    new_lease = main._acquire_publication_lease(store, "bucket", base)

    with pytest.raises(main.PipelineError, match="conditional lease delete"):
        main._conditional_delete(store, "bucket", old_lease.key, old_lease.etag)
    assert ("bucket", new_lease.key) in store.objects
    main._conditional_delete(store, "bucket", new_lease.key, new_lease.etag)


def test_stale_publication_lease_is_recovered_by_observed_generation(
    tmp_path: Path,
) -> None:
    session = write_card_session(tmp_path)
    store = MemoryObjectStore()
    base = f"YLX-device/{session.session_id}"
    old_lease = main._acquire_publication_lease(store, "bucket", base)
    store.modified_at[("bucket", old_lease.key)] = datetime.now(UTC) - timedelta(
        minutes=10
    )

    recovered = main._acquire_publication_lease(
        store,
        "bucket",
        base,
        stale_after_seconds=60,
    )
    assert recovered.key == old_lease.key
    assert recovered.etag != old_lease.etag
    with pytest.raises(main.PipelineError, match="conditional lease delete"):
        main._conditional_delete(store, "bucket", old_lease.key, old_lease.etag)
    main._conditional_delete(store, "bucket", recovered.key, recovered.etag)


def test_fresh_publication_lease_is_never_reclaimed(tmp_path: Path) -> None:
    session = write_card_session(tmp_path)
    store = MemoryObjectStore()
    base = f"YLX-device/{session.session_id}"
    lease = main._acquire_publication_lease(store, "bucket", base)

    with pytest.raises(main.PipelineError, match="is active"):
        main._acquire_publication_lease(
            store,
            "bucket",
            base,
            stale_after_seconds=60,
        )
    assert ("bucket", lease.key) in store.objects
    main._conditional_delete(store, "bucket", lease.key, lease.etag)


def test_endpoint_without_conditional_delete_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnsupportedConditionStore(MemoryObjectStore):
        def delete_object(self, *, Bucket: str, Key: str) -> None:
            self.objects.pop((Bucket, Key), None)

    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"bytes")
    store = UnsupportedConditionStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    with pytest.raises(main.PipelineError, match="conditional lease delete"):
        main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    assert not any(
        "/f-" in key or key.endswith("publication.json") for _, key in store.objects
    )


def test_capability_probe_rejects_endpoint_that_ignores_if_none_match(
    tmp_path: Path,
) -> None:
    class IgnoringStore(MemoryObjectStore):
        def put_object(
            self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, Metadata=None
        ):
            return super().put_object(
                Bucket=Bucket,
                Key=Key,
                Body=Body,
                ContentType=ContentType,
                Metadata=Metadata,
            )

    session = write_card_session(tmp_path)
    store = IgnoringStore()
    with pytest.raises(main.PipelineError, match="ignored IfNoneMatch"):
        main._acquire_publication_lease(
            store, "bucket", f"YLX-device/{session.session_id}"
        )
    assert store.objects == {}


def test_missing_etag_after_successful_lease_put_fails_without_guessing_cleanup(
    tmp_path: Path,
) -> None:
    class MissingLeaseEtagStore(MemoryObjectStore):
        def put_object(
            self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, Metadata=None
        ):
            response = super().put_object(
                Bucket=Bucket,
                Key=Key,
                Body=Body,
                ContentType=ContentType,
                IfNoneMatch=IfNoneMatch,
                Metadata=Metadata,
            )
            return {} if Key.endswith("publication.lock") else response

    session = write_card_session(tmp_path)
    store = MissingLeaseEtagStore()
    with pytest.raises(
        main.PipelineError, match="did not return an ETag for publication lease"
    ):
        main._acquire_publication_lease(
            store, "bucket", f"YLX-device/{session.session_id}"
        )
    assert any(key.endswith("publication.lock") for _, key in store.objects)


def test_missing_etag_after_capability_put_fails_without_conditional_delete(
    tmp_path: Path,
) -> None:
    class MissingCapabilityEtagStore(MemoryObjectStore):
        conditional_deletes = 0

        def put_object(
            self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, Metadata=None
        ):
            response = super().put_object(
                Bucket=Bucket,
                Key=Key,
                Body=Body,
                ContentType=ContentType,
                IfNoneMatch=IfNoneMatch,
                Metadata=Metadata,
            )
            return {} if "lease-capability-" in Key else response

        def delete_object(self, *, Bucket, Key, IfMatch=None) -> None:
            if IfMatch is not None:
                self.conditional_deletes += 1
            super().delete_object(Bucket=Bucket, Key=Key, IfMatch=IfMatch)

    session = write_card_session(tmp_path)
    store = MissingCapabilityEtagStore()
    with pytest.raises(
        main.PipelineError, match="did not return an ETag for conditional lease probe"
    ):
        main._acquire_publication_lease(
            store, "bucket", f"YLX-device/{session.session_id}"
        )
    assert store.conditional_deletes == 0
    assert all("lease-capability-" in key for _, key in store.objects)


def test_opaque_server_etag_is_used_verbatim_for_lease_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OpaqueEtagStore(MemoryObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.etags: dict[tuple[str, str], str] = {}
            self.conditional_values: list[str] = []

        def put_object(
            self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, Metadata=None
        ):
            if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
                )
            self.puts.append((Bucket, Key))
            self.objects[(Bucket, Key)] = (Body, ContentType)
            self.metadata[(Bucket, Key)] = dict(Metadata or {})
            self.modified_at[(Bucket, Key)] = datetime.now(UTC)
            etag = f'"opaque-generation-{len(self.etags) + 1}"'
            self.etags[(Bucket, Key)] = etag
            return {"ETag": etag}

        def delete_object(self, *, Bucket, Key, IfMatch=None) -> None:
            if IfMatch is not None:
                self.conditional_values.append(IfMatch)
                if self.etags.get((Bucket, Key)) != IfMatch:
                    raise ClientError(
                        {"Error": {"Code": "PreconditionFailed"}}, "DeleteObject"
                    )
            self.objects.pop((Bucket, Key), None)
            self.metadata.pop((Bucket, Key), None)
            self.modified_at.pop((Bucket, Key), None)
            self.etags.pop((Bucket, Key), None)

    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"bytes")
    store = OpaqueEtagStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    assert store.conditional_values
    assert all(
        value.startswith('"opaque-generation-') for value in store.conditional_values
    )


def test_failed_capability_delete_uses_owner_matched_cleanup(tmp_path: Path) -> None:
    class TransientDeleteFailureStore(MemoryObjectStore):
        conditional_deletes = 0

        def delete_object(self, *, Bucket, Key, IfMatch=None) -> None:
            if "lease-capability-" in Key and IfMatch is not None:
                self.conditional_deletes += 1
                if self.conditional_deletes == 1:
                    raise ClientError(
                        {"Error": {"Code": "ServiceUnavailable"}}, "DeleteObject"
                    )
            super().delete_object(Bucket=Bucket, Key=Key, IfMatch=IfMatch)

    session = write_card_session(tmp_path)
    store = TransientDeleteFailureStore()
    with pytest.raises(main.PipelineError, match="conditional lease delete"):
        main._acquire_publication_lease(
            store, "bucket", f"YLX-device/{session.session_id}"
        )
    assert store.conditional_deletes == 2
    assert store.objects == {}


def test_publish_error_is_preserved_when_lease_release_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DoubleFailStore(MemoryObjectStore):
        def upload_file(self, *args, **kwargs) -> None:
            raise OSError("video upload disconnected")

        def delete_object(self, *, Bucket, Key, IfMatch=None) -> None:
            if Key.endswith("publication.lock") and IfMatch is not None:
                raise ClientError(
                    {"Error": {"Code": "ServiceUnavailable"}}, "DeleteObject"
                )
            super().delete_object(Bucket=Bucket, Key=Key, IfMatch=IfMatch)

    session = write_card_session(tmp_path)
    output = tmp_path / "left.mp4"
    output.write_bytes(b"bytes")
    store = DoubleFailStore()
    monkeypatch.setattr(main, "object_store", lambda: store)

    with pytest.raises(
        main.PipelineError,
        match="publishing capture-001 failed: video upload disconnected",
    ) as raised:
        main.upload(session, [output], "bucket", "", "YLX-device", "slow")
    assert any("lease release also failed" in note for note in raised.value.__notes__)
