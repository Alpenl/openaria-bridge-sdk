"""Manifest-driven final media rendering for verified session trees."""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import subprocess
import tempfile
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from ._json import load_json
from .errors import ContractError, ExportError

FINAL_MEDIA_NAME = "recording.mp4"
VIDEO_PRESET = "veryfast"
VIDEO_CRF = 20
AUDIO_BITRATE = "192k"
COMMAND_ERROR_LIMIT = 4000
RENDERER_NAME = "openaria-ffmpeg-sbs"
RENDERER_VERSION = 1


@dataclasses.dataclass(frozen=True)
class MediaPlan:
    """Verified source paths and timeline placement needed by FFmpeg."""

    mode: str
    left_segments: tuple[Path, ...] = ()
    right_segments: tuple[Path, ...] = ()
    stereo_segments: tuple[Path, ...] = ()
    audio_segments: tuple[Path, ...] = ()
    video_start_time_seconds: float = 0.0
    video_duration_seconds: float = 0.0
    audio_start_time_seconds: float | None = None
    audio_sample_rate: int | None = None
    raw_mjpeg_fps: float | None = None
    output_fps: float = 0.0

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_segments)

    @property
    def audio_offset_seconds(self) -> float | None:
        if self.audio_start_time_seconds is None:
            return None
        return self.audio_start_time_seconds - self.video_start_time_seconds

    @property
    def video_segment_count(self) -> int:
        if self.mode == "split":
            return len(self.left_segments)
        return len(self.stereo_segments)


@dataclasses.dataclass(frozen=True)
class RenderedMedia:
    """Integrity and synchronization facts for one completed MP4."""

    path: Path
    size_bytes: int
    sha256: str
    has_audio: bool
    video_segment_count: int
    audio_segment_count: int
    video_start_time_seconds: float
    audio_start_time_seconds: float | None
    audio_offset_seconds: float | None
    video_frame_count: int = 0
    output_fps: float = 0.0


def build_media_plan(session_root: Path, manifest_bytes: bytes) -> MediaPlan:
    """Parse the current Device Session media contract into local source paths."""

    manifest = load_json(manifest_bytes, "Device Session manifest")
    if not isinstance(manifest, dict):
        raise ContractError("Device Session manifest must be an object")
    schema = manifest.get("schema")
    if schema not in {"ylx.device-session.v1", "ylx.device-session.v2"}:
        raise ContractError(
            f"automatic media rendering does not support manifest schema {schema!r}"
        )

    video = _object(manifest.get("video"), "manifest video")
    camera = _object(manifest.get("camera"), "manifest camera")
    output_fps = _manifest_output_fps(camera)
    layout = video.get("layout")
    raw_fps: float | None = None
    if layout == "split-eyes":
        video_segments = _ordered_segments(video.get("segments"), "video")
        left: list[Path] = []
        right: list[Path] = []
        for position, segment in enumerate(video_segments):
            artifacts = _object(
                segment.get("artifacts"), f"video segment {position} artifacts"
            )
            left.append(
                _artifact_path(
                    session_root,
                    artifacts.get("left"),
                    f"video segment {position} left artifact",
                )
            )
            right.append(
                _artifact_path(
                    session_root,
                    artifacts.get("right"),
                    f"video segment {position} right artifact",
                )
            )
        video_start = _finite_seconds(
            video_segments[0].get("start_time_seconds"),
            "first video segment start_time_seconds",
        )
        video_end = _finite_seconds(
            video_segments[-1].get("end_time_seconds"),
            "last video segment end_time_seconds",
        )
        video_duration = video_end - video_start
        mode = "split"
        stereo: tuple[Path, ...] = ()
    elif layout == "raw-side-by-side":
        artifact = _object(video.get("artifact"), "raw side-by-side video artifact")
        stereo = (
            _artifact_path(session_root, artifact, "raw side-by-side video artifact"),
        )
        left = []
        right = []
        video_start = 0.0
        time = _object(manifest.get("time"), "manifest time")
        video_duration = _positive_number(
            time.get("duration_seconds"), "session duration_seconds"
        )
        mode = "stereo"
        media_type = artifact.get("media_type")
        if media_type == "video/x-motion-jpeg" or stereo[0].suffix.lower() in {
            ".mjpeg",
            ".mjpg",
        }:
            raw_fps = output_fps
    else:
        raise ContractError(f"unsupported Device Session video layout: {layout!r}")

    audio_paths: tuple[Path, ...] = ()
    audio_start: float | None = None
    sample_rate: int | None = None
    audio = manifest.get("audio")
    if isinstance(audio, dict) and audio.get("state") == "recorded":
        audio_segments = _ordered_segments(audio.get("segments"), "audio")
        audio_paths = tuple(
            _artifact_path(
                session_root,
                segment.get("artifact"),
                f"audio segment {position} artifact",
            )
            for position, segment in enumerate(audio_segments)
        )
        sync = _object(audio.get("sync"), "audio sync")
        audio_start = _finite_seconds(
            sync.get("start_time_seconds"), "audio sync start_time_seconds"
        )
        sample_rate = _positive_integer(audio.get("sample_rate"), "audio sample_rate")

    paths = [*left, *right, *stereo, *audio_paths]
    if len(paths) != len(set(paths)):
        raise ContractError("Device Session reuses one file for multiple media streams")
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ExportError(f"verified media source disappeared: {path}")

    return MediaPlan(
        mode=mode,
        left_segments=tuple(left),
        right_segments=tuple(right),
        stereo_segments=stereo,
        audio_segments=audio_paths,
        video_start_time_seconds=video_start,
        video_duration_seconds=video_duration,
        audio_start_time_seconds=audio_start,
        audio_sample_rate=sample_rate,
        raw_mjpeg_fps=raw_fps,
        output_fps=output_fps,
    )


def render_session_video(
    session_root: Path,
    manifest_bytes: bytes,
    output: Path,
    progress: Callable[[str], None] | None = None,
) -> RenderedMedia:
    """Create and validate one playable SBS MP4 without exposing partial output."""

    plan = build_media_plan(session_root, manifest_bytes)
    source_frames, _ = _measure_video(plan)
    source_duration = source_frames / plan.output_fps
    plan = dataclasses.replace(plan, video_duration_seconds=source_duration)
    if output.exists() or output.is_symlink():
        raise ExportError(f"final media target already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    executable = _ffmpeg_executable()
    _emit(
        progress,
        f"正在合并 {plan.video_segment_count} 段视频"
        + (f"并对齐 {len(plan.audio_segments)} 段音频" if plan.has_audio else ""),
    )

    with tempfile.TemporaryDirectory(
        prefix=".openaria-media-", dir=output.parent
    ) as temporary:
        workdir = Path(temporary)
        staged_output = workdir / FINAL_MEDIA_NAME
        arguments = build_ffmpeg_arguments(
            plan,
            workdir=workdir,
            output=staged_output,
        )
        _run(
            [executable, *arguments],
            "FFmpeg could not create the final recording",
        )
        _validate_media(executable, staged_output, expect_audio=plan.has_audio)
        os.replace(staged_output, output)

    size_bytes = output.stat().st_size
    if size_bytes <= 0:
        output.unlink(missing_ok=True)
        raise ExportError("FFmpeg created an empty final recording")
    digest = _sha256_file(output)
    output_frames, _ = _count_frames_and_seconds(output)
    if output_frames != source_frames:
        output.unlink(missing_ok=True)
        raise ExportError(
            "final recording frame count changed during rendering: "
            f"expected {source_frames}, got {output_frames}"
        )
    _emit(progress, f"成片校验完成（{size_bytes} 字节）")
    return RenderedMedia(
        path=output,
        size_bytes=size_bytes,
        sha256=digest,
        has_audio=plan.has_audio,
        video_segment_count=plan.video_segment_count,
        audio_segment_count=len(plan.audio_segments),
        video_start_time_seconds=plan.video_start_time_seconds,
        audio_start_time_seconds=plan.audio_start_time_seconds,
        audio_offset_seconds=plan.audio_offset_seconds,
        video_frame_count=output_frames,
        output_fps=plan.output_fps,
    )


def build_ffmpeg_arguments(
    plan: MediaPlan,
    *,
    workdir: Path,
    output: Path,
) -> list[str]:
    """Build one deterministic FFmpeg command for tests and execution."""

    workdir.mkdir(parents=True, exist_ok=True)
    arguments = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-fflags",
        "+genpts",
    ]
    filters: list[str] = []
    if plan.mode == "split":
        arguments.extend(_concat_input(plan.left_segments, workdir, "left"))
        arguments.extend(_concat_input(plan.right_segments, workdir, "right"))
        filters.append(
            "[0:v:0]setpts=PTS-STARTPTS[left];"
            "[1:v:0]setpts=PTS-STARTPTS[right];"
            "[left][right]hstack=inputs=2[video]"
        )
        video_map = "[video]"
        audio_input_index = 2
    elif plan.mode == "stereo":
        if len(plan.stereo_segments) != 1:
            arguments.extend(_concat_input(plan.stereo_segments, workdir, "stereo"))
        elif plan.raw_mjpeg_fps is not None:
            arguments.extend(
                [
                    "-f",
                    "mjpeg",
                    "-framerate",
                    _decimal(plan.raw_mjpeg_fps),
                    "-i",
                    str(plan.stereo_segments[0]),
                ]
            )
        else:
            arguments.extend(["-i", str(plan.stereo_segments[0])])
        video_map = "0:v:0"
        audio_input_index = 1
    else:
        raise ContractError(f"unsupported media plan mode: {plan.mode!r}")

    if plan.has_audio:
        arguments.extend(_concat_input(plan.audio_segments, workdir, "audio"))
        filters.append(_audio_filter(plan, audio_input_index))

    if filters:
        arguments.extend(["-filter_complex", ";".join(filters)])
    arguments.extend(["-map", video_map])
    if plan.has_audio:
        arguments.extend(["-map", "[audio]"])
    else:
        arguments.append("-an")

    arguments.extend(
        [
            "-map_metadata",
            "-1",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            VIDEO_PRESET,
            "-crf",
            str(VIDEO_CRF),
            "-pix_fmt",
            "yuv420p",
            "-r",
            _decimal(plan.output_fps),
            "-fps_mode",
            "cfr",
            "-metadata:s:v:0",
            "stereo_mode=left_right",
        ]
    )
    if plan.has_audio:
        arguments.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                AUDIO_BITRATE,
            ]
        )
    arguments.extend(
        [
            "-sn",
            "-dn",
            "-max_muxing_queue_size",
            "1024",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return arguments


def _audio_filter(plan: MediaPlan, input_index: int) -> str:
    offset = plan.audio_offset_seconds
    if offset is None:
        raise ContractError("audio media plan omitted its timeline offset")
    chain = f"[{input_index}:a:0]aresample=async=1:first_pts=0"
    if offset < -0.0005:
        chain += f",atrim=start={_decimal(-offset)},asetpts=PTS-STARTPTS"
    else:
        chain += ",asetpts=PTS-STARTPTS"
        if offset > 0.0005:
            delay_ms = max(1, round(offset * 1000))
            chain += f",adelay={delay_ms}:all=1"
    if plan.video_duration_seconds <= 0:
        raise ContractError("media plan video duration must be positive")
    duration = _decimal(plan.video_duration_seconds)
    return chain + f",apad=whole_dur={duration},atrim=end={duration}[audio]"


def _concat_input(segments: tuple[Path, ...], workdir: Path, label: str) -> list[str]:
    if not segments:
        raise ContractError(f"{label} media stream contains no segments")
    listing = workdir / f"{label}.ffconcat"
    body = "ffconcat version 1.0\n" + "".join(
        f"file '{_ffconcat_path(path.resolve())}'\n" for path in segments
    )
    listing.write_text(body, encoding="utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


def _ffconcat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _ordered_segments(value: Any, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(
            f"Device Session {label} segments must be a non-empty array"
        )
    by_index: dict[int, dict[str, Any]] = {}
    for position, raw in enumerate(value):
        segment = _object(raw, f"{label} segment {position}")
        index = segment.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ContractError(f"{label} segment {position} has an invalid index")
        if index in by_index:
            raise ContractError(f"{label} repeats segment index {index}")
        start = _finite_seconds(
            segment.get("start_time_seconds"),
            f"{label} segment {index} start_time_seconds",
        )
        end = _finite_seconds(
            segment.get("end_time_seconds"),
            f"{label} segment {index} end_time_seconds",
        )
        if end <= start:
            raise ContractError(f"{label} segment {index} has an empty time range")
        by_index[index] = segment
    indexes = sorted(by_index)
    if indexes != list(range(len(indexes))):
        raise ContractError(
            f"{label} segment indexes must start at zero and be contiguous"
        )
    ordered = tuple(by_index[index] for index in indexes)
    for previous, current in pairwise(ordered):
        previous_end = _finite_seconds(
            previous.get("end_time_seconds"), f"{label} segment end_time_seconds"
        )
        current_start = _finite_seconds(
            current.get("start_time_seconds"), f"{label} segment start_time_seconds"
        )
        if not math.isclose(previous_end, current_start, abs_tol=1e-6):
            raise ContractError(f"{label} segment timeline is not contiguous")
    return ordered


def _artifact_path(session_root: Path, value: Any, label: str) -> Path:
    artifact = _object(value, label)
    raw = artifact.get("path")
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ContractError(f"{label} has an invalid path")
    relative = Path(raw)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ContractError(f"{label} path escapes the session directory")
    return session_root / relative


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _finite_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ContractError(f"{label} must be finite and non-negative")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _finite_seconds(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _manifest_output_fps(camera: dict[str, Any]) -> float:
    nominal = camera.get("nominal_fps")
    if (
        isinstance(nominal, (int, float))
        and not isinstance(nominal, bool)
        and math.isfinite(float(nominal))
        and float(nominal) > 0
    ):
        return float(nominal)
    return _positive_number(camera.get("effective_fps"), "camera effective_fps")


def _ffmpeg_executable() -> str:
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:
        raise ExportError(
            f"the bundled FFmpeg runtime is unavailable: {error}"
        ) from error


def _measure_video(plan: MediaPlan) -> tuple[int, float]:
    if plan.mode == "split":
        left_frames, left_duration = _measure_segments(plan.left_segments)
        right_frames, right_duration = _measure_segments(plan.right_segments)
        if left_frames != right_frames:
            raise ExportError(
                "left/right source frame counts differ: "
                f"{left_frames} vs {right_frames}"
            )
        if not math.isclose(left_duration, right_duration, abs_tol=0.05):
            raise ExportError(
                "left/right source durations differ: "
                f"{left_duration:.6f}s vs {right_duration:.6f}s"
            )
        return left_frames, max(left_duration, right_duration)
    return _measure_segments(plan.stereo_segments)


def _measure_segments(segments: tuple[Path, ...]) -> tuple[int, float]:
    frame_count = 0
    duration = 0.0
    for segment in segments:
        frames, seconds = _count_frames_and_seconds(segment)
        frame_count += frames
        duration += seconds
    return frame_count, duration


def _count_frames_and_seconds(path: Path) -> tuple[int, float]:
    try:
        frames, seconds = imageio_ffmpeg.count_frames_and_secs(str(path))
    except Exception as error:
        raise ExportError(f"cannot inspect video timing for {path}: {error}") from error
    if frames <= 0 or not math.isfinite(seconds) or seconds <= 0:
        raise ExportError(f"video contains no usable frames: {path}")
    return frames, seconds


def _validate_media(executable: str, path: Path, *, expect_audio: bool) -> None:
    maps = ["-map", "0:v:0"]
    if expect_audio:
        maps.extend(["-map", "0:a:0"])
    _run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            str(path),
            *maps,
            "-t",
            "0.1",
            "-f",
            "null",
            os.devnull,
        ],
        "the final recording failed media validation",
    )


def _run(arguments: list[str], label: str) -> None:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ExportError(f"{label}: {error}") from error
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
    if len(detail) > COMMAND_ERROR_LIMIT:
        detail = detail[-COMMAND_ERROR_LIMIT:]
    raise ExportError(f"{label}: {detail}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".")


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)
