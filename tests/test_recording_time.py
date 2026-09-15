import dataclasses

import pytest
from test_recording_loading import SOURCE, _page
from test_workflow_stability import SESSION

from openaria.bridge.sdk._lan import _session_info
from openaria.bridge.sdk._recording_time import recording_time
from openaria.bridge.sdk._tui_widgets import session_label, started_at


def test_boot_clock_jump_recovers_recording_date_without_changing_catalog():
    row = _page("a", "recording")["items"][0]
    row.update(
        started_at="2000-01-01T08:01:29.437028+08:00",
        ended_at="2026-09-13T18:28:14.504397+08:00",
        duration_seconds=26.338194895,
        display_name="录制 2000-01-01 08:01:29",
    )
    session = _session_info(row, SOURCE)
    assert session.started_at == "2026-09-13T18:27:48.166202+08:00"
    assert session.display_name == "录制 2026-09-13 18:27:48"
    assert "推算" in started_at(session)
    assert row["started_at"].startswith("2000-")
    row["display_name"] = "录制 自定义名称"
    assert _session_info(row, SOURCE).display_name == "录制 自定义名称"


@pytest.mark.parametrize(
    "start,end,duration,note",
    [
        ("2026-09-13T10:00:00Z", "2026-09-13T10:00:26Z", 26, None),
        ("2000-01-01T00:00:00Z", "2000-01-01T00:00:26Z", 26, "未知"),
        ("invalid", None, 26, "无效"),
        ("2026-09-14T00:00:00Z", "2026-09-13T10:00:26Z", 26, "推算"),
    ],
)
def test_clock_recovery_limits(start, end, duration, note):
    corrected, reason = recording_time(start, end, duration)
    if note:
        assert note in reason
    else:
        assert corrected == start and reason is None


def test_unsynchronized_clock_is_not_shown_as_real_year_2000():
    session = dataclasses.replace(
        SESSION,
        started_at="2000-01-01T00:00:00Z",
        display_name="录制 2000-01-01 00:00:00",
        time_note="设备时钟未校准，录制日期未知",
    )
    assert "2000" not in session_label(session, 120).plain
    assert "未知" in started_at(session)
