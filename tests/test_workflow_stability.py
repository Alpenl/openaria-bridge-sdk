from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from openaria.bridge.sdk import (
    DiscoveryError,
    ExportedSession,
    ExportError,
    OpenAriaSDK,
    SessionInfo,
    Source,
    SourceMode,
)
from openaria.bridge.sdk import _media as media
from openaria.bridge.sdk import client as client_module

SOURCE = Source(
    mode=SourceMode.LAN,
    location="http://127.0.0.1:8080/api/v4",
    device_id="device",
    device_label="Device",
)
SESSION = SessionInfo(
    session_id="first",
    display_name="First",
    started_at="2026-09-08T12:00:00Z",
    duration_seconds=1,
    total_bytes=10,
    manifest_sha256="a" * 64,
)


def test_batch_continues_after_bad_recording_and_retains_failure_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    refreshes = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def export_session(self, source, session, output, progress):
            calls.append(session.session_id)
            if session.session_id == "first":
                raise ExportError("corrupt recording")
            return ExportedSession(
                session_id=session.session_id,
                path=output / session.session_id,
                artifact_count=1,
                total_bytes=10,
            )

    sdk = OpenAriaSDK(output=tmp_path)

    def inventory(source=None, *, refresh=False):
        refreshes.append(refresh)
        return (SESSION, dataclasses.replace(SESSION, session_id="second"))

    monkeypatch.setattr(sdk, "list_sessions", inventory)
    monkeypatch.setattr(client_module, "DeviceApiClient", Client)
    result = sdk.export(source=SOURCE, continue_on_error=True)
    assert calls == ["first", "second"]
    assert [item.session_id for item in result.sessions] == ["second"]
    assert [(item.session_id, item.error) for item in result.failed_sessions] == [
        ("first", "corrupt recording")
    ]
    assert refreshes == [True]
    calls.clear()
    with pytest.raises(ExportError, match="corrupt recording"):
        sdk.export(source=SOURCE)
    assert calls == ["first"]


def test_batch_reconciles_removed_and_unavailable_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unavailable = dataclasses.replace(
        SESSION,
        session_id="unavailable",
        exportable=False,
        unavailable_reason="recording incomplete",
    )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def list_sessions(self, source):
            return SESSION, unavailable

        def export_session(self, source, session, output, progress):
            return ExportedSession(
                session.session_id, output / session.session_id, 1, 10
            )

    monkeypatch.setattr(client_module, "DeviceApiClient", Client)
    sdk = OpenAriaSDK(output=tmp_path)
    result = sdk.export(
        source=SOURCE,
        session_ids=["first", "gone", "unavailable"],
        continue_on_error=True,
    )
    assert [session.session_id for session in result.sessions] == ["first"]
    assert {failure.session_id for failure in result.failed_sessions} == {
        "gone",
        "unavailable",
    }
    assert result.unavailable_sessions == (unavailable,)


def test_failed_session_refresh_invalidates_the_previous_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([(SESSION,), DiscoveryError("disconnected"), ()])

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def list_sessions(self, source):
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(client_module, "DeviceApiClient", Client)
    sdk = OpenAriaSDK()
    assert sdk.list_sessions(SOURCE) == (SESSION,)
    with pytest.raises(DiscoveryError, match="disconnected"):
        sdk.list_sessions(SOURCE, refresh=True)
    assert sdk.list_sessions(SOURCE) == ()


@pytest.mark.parametrize("phase", ["hash", "frame-count", "progress"])
def test_render_failure_before_completion_never_publishes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    plan = media.MediaPlan(
        mode="stereo",
        left_segments=(),
        right_segments=(),
        stereo_segments=(tmp_path / "source.mp4",),
        audio_segments=(),
        video_start_time_seconds=0,
        video_duration_seconds=1,
        audio_start_time_seconds=None,
        audio_sample_rate=None,
        raw_mjpeg_fps=None,
        output_fps=30,
    )
    monkeypatch.setattr(media, "build_media_plan", lambda *args: plan)
    monkeypatch.setattr(media, "_measure_video", lambda _: (30, 1))
    monkeypatch.setattr(media, "_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(media, "_validate_media", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        media,
        "_run",
        lambda arguments, *args: Path(arguments[-1]).write_bytes(b"rendered"),
    )

    def failed(*args):
        raise ExportError("injected failure")

    monkeypatch.setattr(
        media, "_sha256_file", failed if phase == "hash" else lambda _: "a" * 64
    )
    monkeypatch.setattr(
        media,
        "_count_frames_and_seconds",
        failed if phase == "frame-count" else lambda _: (30, 1),
    )

    def progress(message):
        if phase == "progress" and "校验完成" in message:
            failed()

    output = tmp_path / "recording.mp4"
    with pytest.raises(ExportError, match="injected failure"):
        media.render_session_video(tmp_path, b"{}", output, progress)
    assert not output.exists()
    assert not list(tmp_path.glob(".openaria-media-*"))
