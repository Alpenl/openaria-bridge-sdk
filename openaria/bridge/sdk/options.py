"""Reproducible media settings shared by LAN and card exports."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

from .errors import ContractError


@dataclasses.dataclass(frozen=True)
class ExportOptions:
    """Positive calibration delays audio; it is never inferred across sessions.

    HEVC uses medium/CRF 22; H.264 uses veryfast/CRF 20. Retained source
    artifacts keep their manifest hashes and can be exported again losslessly.
    """

    video_codec: str = "h264"
    audio_calibration_seconds: float = 0.0
    retain_sources: bool = False

    def __post_init__(self) -> None:
        if self.video_codec not in {"h264", "hevc"}:
            raise ContractError("video_codec must be h264 or hevc")
        if not math.isfinite(self.audio_calibration_seconds):
            raise ContractError("audio calibration must be finite")
        if not isinstance(self.retain_sources, bool):
            raise ContractError("retain_sources must be boolean")

    def render_arguments(self) -> dict[str, Any]:
        return {
            "video_codec": self.video_codec,
            "preset": "medium" if self.video_codec == "hevc" else "veryfast",
            "crf": 22 if self.video_codec == "hevc" else 20,
            "audio_calibration_seconds": self.audio_calibration_seconds,
        }
