from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, SelectionList, Static

from openaria.bridge.sdk import (
    DiscoveryError,
    ExportedSession,
    ExportResult,
    SessionInfo,
    Source,
    SourceMode,
    cli,
)
from openaria.bridge.sdk.tui import OpenAriaTUI, TextEntryDialog

LAN_SOURCE = Source(
    mode=SourceMode.LAN,
    location="http://192.0.2.24:8080/api/v4",
    api_base="http://192.0.2.24:8080/api/v4",
    device_id="device-lan",
    device_label="Open Aria LAN",
)
READY_SESSION = SessionInfo(
    session_id="session-ready",
    display_name="Morning capture",
    started_at="2026-08-31T08:30:00+08:00",
    duration_seconds=30,
    total_bytes=12_000,
    manifest_sha256="a" * 64,
)
UNAVAILABLE_SESSION = SessionInfo(
    session_id="session-unavailable",
    display_name="Pending capture",
    started_at="2026-08-31T08:35:00+08:00",
    duration_seconds=10,
    total_bytes=4_000,
    manifest_sha256="",
    exportable=False,
    unavailable_reason="gateway marked the session unusable",
)


class FakeSDKFactory:
    def __init__(self, card_root: Path, *, no_automatic_sources: bool = False) -> None:
        self.card_source = Source(
            mode=SourceMode.CARD,
            location=str(card_root),
            card_root=card_root,
            device_id="device-card",
            device_label="Open Aria Card",
        )
        self.no_automatic_sources = no_automatic_sources
        self.created_modes: list[SourceMode] = []
        self.export_calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        mode: SourceMode | str,
        output: Path,
        endpoint: str | None = None,
        **_: Any,
    ) -> FakeSDK:
        selected_mode = SourceMode(mode)
        self.created_modes.append(selected_mode)
        return FakeSDK(self, selected_mode, endpoint)


class FakeSDK:
    def __init__(
        self, factory: FakeSDKFactory, mode: SourceMode, endpoint: str | None
    ) -> None:
        self.factory = factory
        self.mode = mode
        self.endpoint = endpoint

    def discover(self, *, refresh: bool = False) -> tuple[Source, ...]:
        if self.endpoint is not None:
            return (LAN_SOURCE,)
        if self.factory.no_automatic_sources:
            raise DiscoveryError("nothing attached")
        if self.mode is SourceMode.CARD:
            time.sleep(0.02)
            return (self.factory.card_source,)
        return (LAN_SOURCE,)

    def list_sessions(
        self, source: Source | None = None, *, refresh: bool = False
    ) -> tuple[SessionInfo, ...]:
        if source is not None and source.mode is SourceMode.CARD:
            return (READY_SESSION,)
        return (READY_SESSION, UNAVAILABLE_SESSION)

    def export(
        self,
        *,
        source: Source | None = None,
        session_ids: tuple[str, ...] | None = None,
        output: Path | str | None = None,
        progress=None,
    ) -> ExportResult:
        assert source is not None
        assert session_ids is not None
        assert output is not None
        output_root = Path(output).resolve()
        self.factory.export_calls.append(
            {
                "source": source,
                "session_ids": session_ids,
                "output": output_root,
            }
        )
        if progress is not None:
            progress(f"{session_ids[0]}: 1/1 video/left.mp4")
        destination = output_root / source.display_name / session_ids[0]
        return ExportResult(
            source=source,
            output_root=output_root,
            sessions=(
                ExportedSession(
                    session_id=session_ids[0],
                    path=destination,
                    artifact_count=1,
                    total_bytes=READY_SESSION.total_bytes,
                ),
            ),
        )


def test_tui_discovers_both_modes_preselects_and_exports(tmp_path: Path) -> None:
    async def scenario() -> None:
        card_root = tmp_path / "card"
        card_root.mkdir()
        output = tmp_path / "exports"
        factory = FakeSDKFactory(card_root)
        app = OpenAriaTUI(default_output=output, sdk_factory=factory)

        async with app.run_test(size=(120, 36), notifications=True) as pilot:
            await _wait_for(
                pilot,
                lambda: (
                    app.query_one("#sources", OptionList).option_count == 2
                    and not app._sessions_loading
                ),
            )

            assert set(factory.created_modes) == {SourceMode.LAN, SourceMode.CARD}
            sessions = app.query_one("#sessions", SelectionList)
            assert sessions.option_count == 1
            assert sessions.selected == [READY_SESSION.session_id]
            unavailable = app.query_one("#unavailable-summary", Static)
            assert "Pending capture" in str(unavailable.content)
            assert "机身标记为不可用" in str(unavailable.content)
            export_button = app.query_one("#export", Button)
            assert export_button.disabled is False
            assert "导出 1 个会话" in str(export_button.label)
            assert app.focused is sessions, repr(app.focused)

            await pilot.press("space")
            await pilot.pause()
            assert sessions.selected == []
            assert export_button.disabled is True
            await pilot.press("space")
            await pilot.pause()
            assert sessions.selected == [READY_SESSION.session_id]

            await pilot.click("#export")
            await _wait_for(pilot, lambda: len(factory.export_calls) == 1)
            await _wait_for(pilot, lambda: not app._exporting)

            call = factory.export_calls[0]
            assert call["source"] == LAN_SOURCE
            assert call["session_ids"] == (READY_SESSION.session_id,)
            assert call["output"] == output.resolve()
            status = app.query_one("#status-message", Static)
            assert "导出完成" in str(status.content)

    asyncio.run(scenario())


def test_tui_manual_address_recovers_when_discovery_finds_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card", no_automatic_sources=True)
        app = OpenAriaTUI(default_output=tmp_path / "exports", sdk_factory=factory)

        async with app.run_test(size=(100, 32)) as pilot:
            await _wait_for(pilot, lambda: not app._pending_modes)
            assert app.query_one("#sources", OptionList).option_count == 0

            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, TextEntryDialog)
            field = app.screen.query_one("#entry-input", Input)
            field.value = "192.0.2.24"
            await pilot.press("enter")

            await _wait_for(
                pilot,
                lambda: (
                    app.query_one("#sources", OptionList).option_count == 1
                    and not app._sessions_loading
                ),
            )
            assert app._selected_binding() is not None
            assert app._selected_binding().source == LAN_SOURCE

            await pilot.press("o")
            await pilot.pause()
            assert isinstance(app.screen, TextEntryDialog)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, TextEntryDialog)

    asyncio.run(scenario())


def test_tui_narrow_layout_keeps_primary_controls_on_screen(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = OpenAriaTUI(default_output=tmp_path, auto_scan=False)
        async with app.run_test(size=(72, 28)) as pilot:
            await pilot.pause()
            assert app.screen.has_class("-narrow")
            assert "ctrl+p" not in app.screen.active_bindings
            source_pane = app.query_one("#source-pane")
            sessions_pane = app.query_one("#sessions-pane")
            transfer_bar = app.query_one("#transfer-bar")
            export_button = app.query_one("#export", Button)

            assert source_pane.region.bottom <= sessions_pane.region.y
            assert sessions_pane.region.bottom <= transfer_bar.region.y
            assert 0 <= export_button.region.x
            assert export_button.region.right <= app.size.width
            assert export_button.region.bottom <= app.size.height

    asyncio.run(scenario())


def test_cli_surface_has_no_operational_flags() -> None:
    help_text = cli.build_parser().format_help()
    assert "--version" in help_text
    assert "--mode" not in help_text
    assert "--endpoint" not in help_text
    assert "--output" not in help_text
    assert "--session" not in help_text
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["--mode", "card"])
    assert exited.value.code == 2


def test_cli_launches_tui_in_an_interactive_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[bool] = []

    class FakeStream:
        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            return None

    class FakeApp:
        def run(self) -> None:
            launched.append(True)

    monkeypatch.setattr(cli.sys, "stdin", FakeStream())
    monkeypatch.setattr(cli.sys, "stdout", FakeStream())
    monkeypatch.setattr(cli, "OpenAriaTUI", FakeApp)

    assert cli.main([]) == 0
    assert launched == [True]


async def _wait_for(
    pilot: Pilot, predicate, *, attempts: int = 200, delay: float = 0.01
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(delay)
    raise AssertionError("TUI state did not settle before the test timeout")
