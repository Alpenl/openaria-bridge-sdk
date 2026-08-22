from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import main

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is required for the synthetic media integration test",
)


def _run_ffmpeg(*arguments: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_real_ffmpeg_rotates_before_stereo_crop_and_writes_faststart(
    tmp_path: Path,
) -> None:
    source_frame = tmp_path / "source.png"
    frame = Image.new("RGB", (128, 64), (220, 20, 20))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((64, 0, 127, 63), fill=(20, 20, 220))
    draw.rectangle((4, 4, 19, 19), fill=(245, 245, 245))
    draw.rectangle((108, 44, 123, 59), fill=(245, 245, 245))
    frame.save(source_frame)

    session_directory = tmp_path / "card" / "synthetic-session"
    source_video = session_directory / "spool" / "source_00000.mp4"
    source_video.parent.mkdir(parents=True)
    _run_ffmpeg(
        "-loop",
        "1",
        "-i",
        str(source_frame),
        "-frames:v",
        "2",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-crf",
        "10",
        "-pix_fmt",
        "yuv420p",
        str(source_video),
    )
    source_bytes = source_video.read_bytes()
    source_manifest = session_directory / "publication_manifest.json"
    source_manifest.write_text("{}\n", encoding="utf-8")
    session = main.Session(
        directory=session_directory,
        source_directory_name="synthetic-session",
        session_id="synthetic-session",
        captured_at="2026-08-11T00:00:00Z",
        duration_seconds=0.08,
        artifacts=(
            main.Artifact(
                display_path="spool/source_00000.mp4",
                role="video_stereo",
                size_bytes=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                media_type="video/mp4",
            ),
        ),
        camera={
            "width": 128,
            "height": 64,
            "layout": "left_right_side_by_side",
            "video_codec": "h264",
        },
        source_manifest_path=source_manifest,
        source_manifest_sha256=hashlib.sha256(b"{}\n").hexdigest(),
        source_manifest_revision="sha256:" + "0" * 64,
        source_signature={"status": "unsigned_degraded"},
    )

    left_video, right_video = main.normalize(
        session,
        tmp_path / "work",
        "ultrafast",
        rotation_degrees=180,
    )
    decoded = []
    for video in (left_video, right_video):
        image_path = video.with_suffix(".png")
        _run_ffmpeg("-i", str(video), "-frames:v", "1", str(image_path))
        decoded.append(Image.open(image_path).convert("RGB"))
        payload = video.read_bytes()
        assert 0 <= payload.find(b"moov") < payload.find(b"mdat")

    left_image, right_image = decoded
    left_center = left_image.getpixel((32, 32))
    right_center = right_image.getpixel((32, 32))
    assert left_center[2] > left_center[0] + 80
    assert right_center[0] > right_center[2] + 80
    assert min(left_image.getpixel((8, 8))) > 180
    assert min(right_image.getpixel((56, 56))) > 180
    assert left_image.getpixel((56, 56))[2] > left_image.getpixel((56, 56))[0] + 80
    assert right_image.getpixel((8, 8))[0] > right_image.getpixel((8, 8))[2] + 80
