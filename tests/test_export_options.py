import dataclasses

import pytest

from openaria.bridge.sdk import ExportOptions
from openaria.bridge.sdk._media import MediaPlan, build_ffmpeg_arguments
from openaria.bridge.sdk.errors import ContractError


@pytest.mark.parametrize("codec,standard_crf", [("h264", 20), ("hevc", 22)])
def test_quality_reaches_encoder_without_resizing(tmp_path, codec, standard_crf):
    plan = MediaPlan(
        mode="split",
        left_segments=(tmp_path / "left.mp4",),
        right_segments=(tmp_path / "right.mp4",),
        output_fps=30,
    )
    for quality, crf in [("standard", standard_crf), ("high", 18)]:
        options = ExportOptions(video_codec=codec, video_quality=quality)
        render = options.render_arguments()
        render.pop("audio_calibration_seconds")
        args = build_ffmpeg_arguments(
            plan,
            workdir=tmp_path / quality,
            output=tmp_path / f"{quality}.mp4",
            **render,
        )
        assert args[args.index("-crf") + 1] == str(crf)
        assert args[args.index("-c:v") + 1] == (
            "libx265" if codec == "hevc" else "libx264"
        )
        if quality == "high":
            assert args[args.index("-preset") + 1] == "medium"
        assert "hstack=inputs=2" in args[args.index("-filter_complex") + 1]
        assert "scale=" not in " ".join(args)


def test_old_receipts_default_to_standard_and_high_roundtrips():
    legacy = {
        "video_codec": "hevc",
        "audio_calibration_seconds": 0.03,
        "retain_sources": True,
    }
    standard = ExportOptions(**legacy, video_quality="standard")
    high = dataclasses.replace(standard, video_quality="high")
    assert standard.video_quality == "standard"
    assert standard != high  # Export cache compares these reconstructed options.
    assert ExportOptions(**dataclasses.asdict(high)) == high
    assert ExportOptions().video_quality == "high"


def test_unknown_quality_is_rejected():
    with pytest.raises(ContractError, match="video_quality"):
        ExportOptions(video_quality="lossless")
