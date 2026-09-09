"""Reproducible media settings shared by LAN and card exports."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

from .errors import ContractError


@dataclasses.dataclass(frozen=True)
class ExportOptions:
    """Positive calibration delays audio; it is never inferred across sessions.

    Standard uses HEVC medium/CRF 22 or H.264 veryfast/CRF 20. High quality
    uses medium/CRF 18 for either codec, trading time and size for detail.
    Retained source artifacts keep their exact bytes and manifest hashes;
    creating a composed MP4 still performs lossy encoding.
    """

    video_codec: str = "h264"
    audio_calibration_seconds: float = 0.0
    retain_sources: bool = False
    video_quality: str = "high"

    def __post_init__(self) -> None:
        if self.video_codec not in {"h264", "hevc"}:
            raise ContractError("video_codec must be h264 or hevc")
        if self.video_quality not in {"standard", "high"}:
            raise ContractError("video_quality must be standard or high")
        if not math.isfinite(self.audio_calibration_seconds):
            raise ContractError("audio calibration must be finite")
        if not isinstance(self.retain_sources, bool):
            raise ContractError("retain_sources must be boolean")

    def render_arguments(self) -> dict[str, Any]:
        return {
            "video_codec": self.video_codec,
            "preset": (
                "medium"
                if self.video_codec == "hevc" or self.video_quality == "high"
                else "veryfast"
            ),
            "crf": (
                18
                if self.video_quality == "high"
                else 22
                if self.video_codec == "hevc"
                else 20
            ),
            "audio_calibration_seconds": self.audio_calibration_seconds,
        }
