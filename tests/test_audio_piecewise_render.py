"""Synthetic audio only: verify sync through changing system clock slope."""

import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import pytest

from openaria.bridge.sdk._media import MediaPlan, _audio_filter, build_ffmpeg_arguments


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("offset", [-0.087886907, 0.1234567])
def test_slewing_clock_preserves_interior_transients(
    tmp_path: Path, direction: int, offset: float
) -> None:
    rate = 8000
    duration = 600

    def clock(t: float) -> float:
        return t + direction * 0.0005 * min(300, max(0, t - 100))

    timeline = [[t * rate, offset + clock(t)] for t in range(duration + 1)]
    source = np.zeros(duration * rate, dtype="<f4")
    times = (0.513, 31.529, 99.988, 100.123, 150.543, 250.388, 399.529, 420.111, 590.543)
    indices = [round(t * rate) for t in times]
    source[indices] = 0.9
    raw = tmp_path / "synthetic.f32"
    raw.write_bytes(source.tobytes())
    plan = MediaPlan(
        mode="split",
        audio_segments=(raw,),
        audio_start_time_seconds=offset,
        audio_sample_rate=rate,
        video_duration_seconds=duration,
        audio_clock={"mapping": "piecewise-linear", "sample_timeline": timeline},
    )
    script = tmp_path / "audio.fffilter"
    script.write_text(_audio_filter(plan, 0), encoding="ascii")
    rendered = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error",
            "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", str(raw),
            "-filter_complex_script", str(script), "-map", "[audio]",
            "-f", "f32le", "-",
        ],
        capture_output=True, check=True, timeout=60,
    )
    output = np.frombuffer(rendered.stdout, dtype="<f4")
    assert len(output) == duration * rate
    for index in indices:
        expected = round((clock(index / rate) + offset) * rate)
        lo, hi = max(0, expected - rate // 20), expected + rate // 20
        actual = lo + int(np.argmax(abs(output[lo:hi])))
        assert abs(actual - expected) <= 8, (index, actual, expected)
        assert abs(output[actual]) > 0.2


def test_long_audio_mapping_uses_script_without_video_frame_index(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = MediaPlan(
        mode="split",
        left_segments=(tmp_path / "left.ts",),
        right_segments=(tmp_path / "right.ts",),
        audio_segments=(tmp_path / "audio.wav",),
        audio_start_time_seconds=0,
        audio_sample_rate=48000,
        output_fps=30,
        video_duration_seconds=12600,
        audio_clock={
            "mapping": "piecewise-linear",
            "sample_timeline": [[t * 48000, float(t)] for t in range(12601)],
        },
    )
    arguments = build_ffmpeg_arguments(plan, workdir=workdir, output=tmp_path / "out.mp4")
    assert "-filter_complex" not in arguments
    script = Path(arguments[arguments.index("-filter_complex_script") + 1])
    assert script.stat().st_size > 131072
    assert sum(map(len, arguments)) < 8192
    audio_script = tmp_path / "long-audio.fffilter"
    audio_script.write_text(_audio_filter(plan, 0), encoding="ascii")
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.1",
            "-filter_complex_script", str(audio_script), "-map", "[audio]",
            "-t", "0.1", "-f", "null", "-",
        ],
        capture_output=True, check=True, timeout=30,
    )
