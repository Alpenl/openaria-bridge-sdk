from __future__ import annotations

import array
import json
import math
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from openaria.bridge.sdk._media import (
    MediaPlan,
    build_ffmpeg_arguments,
    build_media_plan,
    render_session_video,
)
from openaria.bridge.sdk.errors import ContractError, ExportError


def _indexed_manifest(root: Path, timestamps_ns: list[int]) -> dict[str, object]:
    manifest = _multi_segment_manifest()
    manifest.pop("audio")
    manifest["camera"] = {"nominal_fps": 10, "effective_fps": 7.9}
    path = root / "frames.ndjson"
    rows = [
        {
            "schema": "ylx.frame-index.v1",
            "session_id": "test-session",
            "frame": index,
            "host_monotonic_ns": timestamp,
        }
        for index, timestamp in enumerate(timestamps_ns)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest["frames"] = {
        "count": len(rows),
        "artifact": _artifact("frames.ndjson", "frames.index", "application/x-ndjson"),
    }
    return manifest


def test_render_preserves_frame_clock_instead_of_nominal_playback_speed(
    tmp_path: Path,
) -> None:
    for eye in ("left", "right"):
        for index in range(2):
            _video(tmp_path / "video" / f"{eye}_{index:05d}.mp4", "red")
    interval_ns = 125_123_456
    timestamps = [1_000_000_000 + index * interval_ns for index in range(8)]
    manifest = _indexed_manifest(tmp_path, timestamps)
    output = tmp_path / "recording.mp4"

    rendered = render_session_video(tmp_path, json.dumps(manifest).encode(), output)

    frames, duration = imageio_ffmpeg.count_frames_and_secs(str(output))
    assert frames == rendered.video_frame_count == 8
    assert rendered.output_fps == pytest.approx(1_000_000_000 / interval_ns)
    assert duration == pytest.approx(8 * interval_ns / 1_000_000_000, abs=0.01)
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-i",
            str(output),
            "-vf",
            "showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pts = [
        float(value) for value in re.findall(r"pts_time:([\d.e+-]+)", decoded.stderr)
    ]
    assert pts == pytest.approx(
        [index * interval_ns / 1_000_000_000 for index in range(8)], abs=0.00001
    )


def test_legacy_export_sbs_v2_uses_the_same_capture_clock(tmp_path: Path) -> None:
    import main

    for eye in ("left", "right"):
        for index in range(2):
            _video(tmp_path / "video" / f"{eye}_{index:05d}.mp4", "red")
    interval = 125_123_456
    manifest = _indexed_manifest(
        tmp_path, [1_000_000_000 + i * interval for i in range(8)]
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    session = main.Session(
        directory=tmp_path,
        source_directory_name="test-session",
        session_id="test-session",
        captured_at="2026-09-08T00:00:00Z",
        duration_seconds=2.0,
        artifacts=(),
        camera={},
        source_manifest_path=manifest_path,
        source_manifest_sha256="unused",
        source_manifest_revision="unused",
        source_signature={},
        source_manifest_schema=main.DEVICE_SESSION_V2_SCHEMA,
    )
    output = main.export_sbs(
        session, tmp_path / "legacy-command.mp4", verify_inputs=False, crf=20
    )
    frames, duration = imageio_ffmpeg.count_frames_and_secs(str(output))
    assert frames == 8
    assert duration == pytest.approx(8 * interval / 1e9, abs=0.01)


@pytest.mark.parametrize("fault", ["gap", "reversed", "count", "identity"])
def test_invalid_capture_clock_is_rejected_before_rendering(
    tmp_path: Path, fault: str
) -> None:
    timestamps = [1_000_000_000 + index * 100_000_000 for index in range(8)]
    if fault == "gap":
        timestamps = [
            timestamp + (300_000_000 if index >= 4 else 0)
            for index, timestamp in enumerate(timestamps)
        ]
    if fault == "reversed":
        timestamps[3] = timestamps[2]
    manifest = _indexed_manifest(tmp_path, timestamps)
    if fault == "count":
        manifest["frames"]["count"] = 9
    if fault == "identity":
        manifest["session_id"] = "another-session"
    with pytest.raises((ContractError, ExportError)):
        build_media_plan(tmp_path, json.dumps(manifest).encode())


def _run_ffmpeg(*arguments: str) -> None:
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _video(path: Path, color: str, *, duration: float = 0.4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:size=32x32:rate=10:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
    )


def _segmented_audio(root: Path) -> tuple[Path, Path]:
    complete = root / "audio" / "complete.wav"
    first = root / "audio" / "audio_00000.wav"
    second = root / "audio" / "audio_00001.wav"
    complete.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-t",
        "0.2",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-f",
        "lavfi",
        "-t",
        "0.8",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-filter_complex",
        "[1:a]aformat=channel_layouts=stereo[tone];"
        "[0:a][tone]concat=n=2:v=0:a=1[audio]",
        "-map",
        "[audio]",
        "-c:a",
        "pcm_s16le",
        str(complete),
    )
    for start, output in (("0", first), ("0.5", second)):
        _run_ffmpeg(
            "-ss",
            start,
            "-i",
            str(complete),
            "-t",
            "0.5",
            "-c:a",
            "pcm_s16le",
            str(output),
        )
    complete.unlink()
    return first, second


def _artifact(path: str, role: str, media_type: str) -> dict[str, object]:
    return {
        "artifact_id": (path.encode().hex() + "0" * 64)[:64],
        "role": role,
        "path": path,
        "media_type": media_type,
        "bytes": 1,
        "sha256": "a" * 64,
    }


def _multi_segment_manifest() -> dict[str, object]:
    video_segments = []
    for index, (start, end) in enumerate(((0.2, 0.6), (0.6, 1.0))):
        video_segments.append(
            {
                "index": index,
                "start_frame": index * 4,
                "end_frame": (index + 1) * 4,
                "start_time_seconds": start,
                "end_time_seconds": end,
                "artifacts": {
                    "left": _artifact(
                        f"video/left_{index:05d}.mp4", "video.left", "video/mp4"
                    ),
                    "right": _artifact(
                        f"video/right_{index:05d}.mp4", "video.right", "video/mp4"
                    ),
                },
            }
        )
    audio_segments = []
    for index, (start, end) in enumerate(((0.0, 0.5), (0.5, 1.0))):
        audio_segments.append(
            {
                "index": index,
                "start_sample": index * 24_000,
                "end_sample": (index + 1) * 24_000,
                "pcm_payload_bytes": 96000,
                "wav_header_bytes": 44,
                "start_time_seconds": start,
                "end_time_seconds": end,
                "artifact": _artifact(
                    f"audio/audio_{index:05d}.wav", "audio.wav", "audio/wav"
                ),
            }
        )
    for segment in audio_segments:
        segment["artifact"]["bytes"] = 96044
    return {
        "schema": "ylx.device-session.v2",
        "session_id": "test-session",
        "time": {"duration_seconds": 2.0},
        "camera": {"effective_fps": 10},
        "video": {
            "layout": "split-eyes",
            "codec": "h264",
            "container": "mp4",
            "segments": video_segments,
        },
        "audio": {
            "state": "recorded",
            "sample_rate": 48_000,
            "sample_count": 48_000,
            "channels": 2,
            "sync": {
                "time_base": "host_monotonic",
                "start_time_seconds": 0.0,
                "end_time_seconds": 1.0,
                "video_time_reference": "session_time_seconds",
            },
            "segments": audio_segments,
        },
    }


def _frame_at(video: Path, timestamp: float, output: Path) -> Image.Image:
    _run_ffmpeg(
        "-ss",
        str(timestamp),
        "-i",
        str(video),
        "-frames:v",
        "1",
        str(output),
    )
    return Image.open(output).convert("RGB")


def _audio_rms(video: Path, *, start: float, duration: float) -> float:
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            str(start),
            "-i",
            str(video),
            "-t",
            str(duration),
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    samples = array.array("f")
    samples.frombytes(completed.stdout)
    assert samples
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def _decoded_video_frame_count(video: Path, *, width: int, height: int) -> int:
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    frame_bytes = width * height * 3
    assert len(completed.stdout) % frame_bytes == 0
    return len(completed.stdout) // frame_bytes


def test_render_session_video_merges_segments_hstacks_and_trims_early_audio(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source with ' quote"
    _video(source / "video" / "left_00000.mp4", "red")
    _video(source / "video" / "right_00000.mp4", "blue")
    _video(source / "video" / "left_00001.mp4", "green")
    _video(source / "video" / "right_00001.mp4", "yellow")
    _segmented_audio(source)
    manifest = json.dumps(_multi_segment_manifest()).encode()
    output = tmp_path / "result" / "recording.mp4"
    messages: list[str] = []

    rendered = render_session_video(source, manifest, output, messages.append)

    assert rendered.path == output
    assert rendered.video_segment_count == 2
    assert rendered.audio_segment_count == 2
    assert rendered.audio_offset_seconds == -0.2
    assert rendered.has_audio is True
    assert rendered.size_bytes == output.stat().st_size
    assert any("对齐 2 段音频" in message for message in messages)
    assert _decoded_video_frame_count(output, width=64, height=32) == 8
    payload = output.read_bytes()
    assert 0 <= payload.find(b"moov") < payload.find(b"mdat")

    first = _frame_at(output, 0.1, tmp_path / "first.png")
    second = _frame_at(output, 0.6, tmp_path / "second.png")
    assert first.size == (64, 32)
    assert first.getpixel((16, 16))[0] > first.getpixel((16, 16))[2] + 80
    assert first.getpixel((48, 16))[2] > first.getpixel((48, 16))[0] + 80
    assert second.getpixel((16, 16))[1] > second.getpixel((16, 16))[0] + 40
    assert second.getpixel((48, 16))[0] > 140
    assert second.getpixel((48, 16))[1] > 140
    assert _audio_rms(output, start=0, duration=0.1) > 0.02


def test_media_plan_delays_audio_that_started_after_video(tmp_path: Path) -> None:
    left = tmp_path / "left.mp4"
    right = tmp_path / "right.mp4"
    audio = tmp_path / "audio.wav"
    plan = MediaPlan(
        mode="split",
        left_segments=(left,),
        right_segments=(right,),
        audio_segments=(audio,),
        video_start_time_seconds=0,
        video_duration_seconds=1,
        audio_start_time_seconds=0.2,
        audio_sample_rate=48_000,
        output_fps=10,
    )

    arguments = build_ffmpeg_arguments(
        plan,
        workdir=tmp_path / "work",
        output=tmp_path / "recording.mp4",
    )

    filters = arguments[arguments.index("-filter_complex") + 1]
    assert "adelay=200:all=1" in filters
    assert "apad=whole_dur=1,atrim=end=1[audio]" in filters


def test_real_device_manifest_uses_audio_sync_clock_for_alignment() -> None:
    root = Path(
        "/data2/openaria-sdk-hardware-20260831/exports-tui-fresh-0ae5728/"
        "YLX-BA9D3B63/01a05321-e0ee-72a7-a017-0e214f9d42d8"
    )
    if not root.is_dir():
        return

    plan = build_media_plan(root, (root / "manifest.json").read_bytes())

    assert plan.video_start_time_seconds == 0.98904022
    assert plan.audio_start_time_seconds == 0.973346574
    assert plan.audio_offset_seconds == -0.015693646000000006
