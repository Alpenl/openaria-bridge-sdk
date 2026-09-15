"""Display clock corrections without changing sealed source manifests."""

import math
from datetime import datetime, timedelta


def recording_time(
    started_at: str, ended_at: object, duration: float
) -> tuple[str, str | None]:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at) if isinstance(ended_at, str) else None
        if end is not None and end.year >= 2020 and math.isfinite(duration):
            if abs((end - start).total_seconds() - duration) > 5:
                corrected = end - timedelta(seconds=duration)
                return corrected.isoformat(), "时间由结束时间和录制时长推算"
        if start.year < 2020:
            return started_at, "设备时钟未校准，录制日期未知"
    except (ValueError, TypeError, OverflowError):
        return started_at, "录制日期无效"
    return started_at, None
