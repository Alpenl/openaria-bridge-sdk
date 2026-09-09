from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Input, RichLog, Select, SelectionList, Static, Tabs

from openaria.bridge.sdk import (
    DeleteFailure,
    DeleteResult,
    DiscoveryError,
    ExportedSession,
    ExportFailure,
    ExportResult,
    SessionInfo,
    Source,
    SourceMode,
    cli,
)
from openaria.bridge.sdk.tui import OpenAriaTUI, TextEntryDialog


@pytest.fixture(autouse=True)
def _isolated_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


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
        self.session_calls: list[Source] = []
        self.session_refreshes: list[bool] = []
        self.discovery_gates: dict[SourceMode, threading.Event] = {}
        self.session_gates: dict[SourceMode, threading.Event] = {}
        self.export_gate: threading.Event | None = None
        self.export_error: Exception | None = None
        self.failed_sessions: dict[str, str] = {}
        self.manual_error: Exception | None = None
        self.delete_calls: list[tuple[str, ...]] = []
        self.delete_gate: threading.Event | None = None
        self.delete_failures: set[str] = set()
        self.sessions = {
            SourceMode.LAN: (READY_SESSION, UNAVAILABLE_SESSION),
            SourceMode.CARD: (READY_SESSION,),
        }

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
            if self.factory.manual_error:
                raise self.factory.manual_error
            return (LAN_SOURCE,)
        if gate := self.factory.discovery_gates.get(self.mode):
            assert gate.wait(5), "discovery gate timed out"
        if self.factory.no_automatic_sources:
            raise DiscoveryError("nothing attached")
        if self.mode is SourceMode.CARD:
            time.sleep(0.02)
            return (self.factory.card_source,)
        return (LAN_SOURCE,)

    def list_sessions(
        self, source: Source | None = None, *, refresh: bool = False
    ) -> tuple[SessionInfo, ...]:
        assert source is not None
        self.factory.session_calls.append(source)
        self.factory.session_refreshes.append(refresh)
        if gate := self.factory.session_gates.get(source.mode):
            assert gate.wait(5), "session gate timed out"
        return self.factory.sessions[source.mode]

    def export(
        self,
        *,
        source: Source | None = None,
        session_ids: tuple[str, ...] | None = None,
        output: Path | str | None = None,
        progress=None,
        continue_on_error: bool = False,
        options=None,
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
                "continue_on_error": continue_on_error,
                "options": options,
            }
        )
        if progress is not None:
            progress(f"{session_ids[0]}: 1/1 video/left.mp4")
        if self.factory.export_gate:
            assert self.factory.export_gate.wait(5), "export gate timed out"
        if self.factory.export_error:
            raise self.factory.export_error
        destination = output_root / source.display_name
        return ExportResult(
            source=source,
            output_root=output_root,
            sessions=tuple(
                ExportedSession(
                    session_id=session_id,
                    path=destination / session_id,
                    artifact_count=1,
                    total_bytes=READY_SESSION.total_bytes,
                    media_path=destination / session_id / "recording.mp4",
                    media_bytes=6_000,
                )
                for session_id in session_ids
                if session_id not in self.factory.failed_sessions
            ),
            failed_sessions=tuple(
                ExportFailure(session_id, self.factory.failed_sessions[session_id])
                for session_id in session_ids
                if session_id in self.factory.failed_sessions
            ),
        )

    def delete_sessions(self, *, source, session_ids, expected_manifests=None):
        self.factory.delete_calls.append(session_ids)
        if self.factory.delete_gate:
            assert self.factory.delete_gate.wait(5)
        deleted = tuple(
            item for item in session_ids if item not in self.factory.delete_failures
        )
        self.factory.sessions[source.mode] = tuple(
            item
            for item in self.factory.sessions[source.mode]
            if item.session_id not in deleted
        )
        return DeleteResult(
            source,
            deleted,
            tuple(
                DeleteFailure(item, "read-only card")
                for item in session_ids
                if item not in deleted
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
                lambda: len(app._sources) == 2 and not app._sessions_loading,
            )

            assert set(factory.created_modes) == {SourceMode.LAN, SourceMode.CARD}
            sessions = app.query_one("#sessions", SelectionList)
            assert sessions.option_count == 2
            assert sessions.selected == [READY_SESSION.session_id]
            unavailable = sessions.get_option_at_index(1)
            assert unavailable.disabled
            assert "Pending capture" in str(unavailable.prompt)
            assert "机身标记为不可用" in str(unavailable.prompt)
            export_button = app.query_one("#export", Button)
            assert export_button.disabled is False
            assert "已选 1 个" in str(
                app.query_one("#selection-summary", Static).content
            )
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
            assert call["continue_on_error"] is True
            assert factory.session_refreshes == [False]
            status = app.query_one("#status-message", Static)
            assert "导出完成" in str(status.content)

    asyncio.run(scenario())


def test_successful_exports_are_marked_and_not_preselected_after_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        history = tmp_path / "history.sqlite3"
        app = OpenAriaTUI(
            default_output=tmp_path, sdk_factory=factory, history_path=history
        )
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.press("e")
            await _wait_for(
                pilot, lambda: bool(factory.export_calls) and not app._exporting
            )
            assert app._exported_ids == {READY_SESSION.session_id}
            assert not app._selected_ids
        app = OpenAriaTUI(
            default_output=tmp_path, sdk_factory=factory, history_path=history
        )
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            assert not app._selected_ids
            assert "已导出" in str(
                app.query_one("#sessions", SelectionList).get_option_at_index(0).prompt
            )
            await pilot.click("#select-exported")
            assert app._selected_ids == {READY_SESSION.session_id}
            assert not app.query_one("#export", Button).disabled
            assert app.query_one("#delete", Button).disabled

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (48, 18)])
def test_card_delete_requires_confirmation_locks_controls_and_keeps_failures(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        other = dataclasses.replace(READY_SESSION, session_id="second")
        factory.sessions[SourceMode.CARD] = (READY_SESSION, other)
        factory.delete_failures.add("second")
        gate = threading.Event()
        factory.delete_gate = gate
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        try:
            async with app.run_test(size=size) as pilot:
                await _wait_for(
                    pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
                )
                app._select_source(
                    next(
                        item
                        for item in app._sources
                        if item.source.mode is SourceMode.CARD
                    )
                )
                await _wait_for(pilot, lambda: not app._sessions_loading)
                await pilot.click("#delete")
                await pilot.pause()
                assert "2 个未导出" in str(
                    app.screen.query_one("#delete-description", Static).content
                )
                assert app.focused.id == "delete-cancel"
                for selector in (
                    "#delete-title",
                    "#delete-warning",
                    "#delete-cancel",
                    "#delete-confirm",
                ):
                    region = app.screen.query_one(selector).region
                    assert region.y >= 0 and region.bottom <= size[1]
                    assert region.x >= 0 and region.right <= size[0]
                await pilot.press("escape")
                assert not factory.delete_calls
                await pilot.click("#delete")
                await pilot.click("#delete-confirm")
                await _wait_for(pilot, lambda: bool(factory.delete_calls))
                for selector in (
                    "#export",
                    "#delete",
                    "#sources",
                    "#rescan",
                    "#change-output",
                    "#select-exported",
                ):
                    assert app.query_one(selector).disabled
                for key in ("q", "ctrl+c", "r", "e", "o", "delete"):
                    await pilot.press(key)
                    assert app.is_running
                gate.set()
                await _wait_for(pilot, lambda: not app._deleting)
                assert [item.session_id for item in app._sessions] == ["second"]
                assert app._selected_ids == {"second"}
                assert "1 个已删除 · 1 个失败" in str(
                    app.query_one("#status-message", Static).content
                )
                assert not app.query_one("#delete", Button).disabled
        finally:
            gate.set()

    asyncio.run(scenario())


def test_remote_delete_enabled_only_when_firmware_advertises_support(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            binding = next(
                item for item in app._sources if item.source.mode is SourceMode.LAN
            )
            capable = dataclasses.replace(
                binding,
                source=dataclasses.replace(
                    binding.source, capabilities={"session_deletion": True}
                ),
            )
            app._sources[app._sources.index(binding)] = capable
            app._select_source(capable)
            await _wait_for(pilot, lambda: not app._sessions_loading)
            assert not app.query_one("#delete", Button).disabled
            await pilot.click("#delete")
            await pilot.click("#delete-confirm")
            await _wait_for(
                pilot, lambda: bool(factory.delete_calls) and not app._deleting
            )
            assert READY_SESSION.session_id not in {
                item.session_id for item in app._sessions
            }
            assert not app._selected_ids

    asyncio.run(scenario())


def test_history_failure_does_not_hide_recordings(tmp_path: Path) -> None:
    async def scenario() -> None:
        history = tmp_path / "history.sqlite3"
        history.write_bytes(b"broken database")
        app = OpenAriaTUI(
            default_output=tmp_path,
            sdk_factory=FakeSDKFactory(tmp_path / "card"),
            history_path=history,
        )
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            assert app._selected_ids == {READY_SESSION.session_id}
            app._show_view("activity-tab")
            await pilot.pause()
            assert "读取导出标记失败" in "\n".join(
                line.text for line in app.query_one(RichLog).lines
            )

    asyncio.run(scenario())


def test_tui_manual_address_recovers_when_discovery_finds_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card", no_automatic_sources=True)
        app = OpenAriaTUI(default_output=tmp_path / "exports", sdk_factory=factory)

        async with app.run_test(size=(100, 32)) as pilot:
            await _wait_for(
                pilot,
                lambda: len(factory.created_modes) == 2 and not app._pending_modes,
            )
            assert not app._sources
            assert app.query_one("#sources", Select).disabled

            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, TextEntryDialog)
            field = app.screen.query_one("#entry-input", Input)
            field.value = "192.0.2.24"
            await pilot.press("enter")

            await _wait_for(
                pilot,
                lambda: len(app._sources) == 1 and not app._sessions_loading,
            )
            assert app._source is not None
            assert app._source.source == LAN_SOURCE

            await pilot.press("o")
            await pilot.pause()
            assert isinstance(app.screen, TextEntryDialog)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, TextEntryDialog)

    asyncio.run(scenario())


def test_rescan_reconnects_manual_devices_when_mdns_is_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card", no_automatic_sources=True)
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(pilot, lambda: not app._pending_modes)
            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one(Input).value = "192.0.2.24"
            await pilot.press("enter")
            await _wait_for(
                pilot, lambda: app._source is not None and not app._sessions_loading
            )
            replacement = dataclasses.replace(READY_SESSION, session_id="new-recording")
            factory.sessions[SourceMode.LAN] = (replacement,)
            await pilot.press("r")
            await _wait_for(
                pilot, lambda: not app._pending_modes and not app._sessions_loading
            )
            assert len(app._sources) == 1
            assert app._source.source == LAN_SOURCE
            assert app._selected_ids == {replacement.session_id}

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(48, 18), (60, 20), (80, 24), (120, 36)])
def test_tui_layout_keeps_primary_controls_on_screen(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test(size=size) as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.pause()
            assert "ctrl+p" not in app.screen.active_bindings
            transfer_bar = app.query_one("#transfer-bar")
            sessions = app.query_one("#sessions")
            assert sessions.region.bottom <= transfer_bar.region.y
            assert sessions.content_size.height >= (10 if size == (80, 24) else 4)
            for selector in (
                "#export",
                "#change-output",
                "#connect",
                "#rescan",
                "#filter",
            ):
                widget = app.query_one(selector)
                assert widget.display and widget.region.width > 0, selector
                assert 0 <= widget.region.x < widget.region.right <= app.size.width, (
                    selector
                )
                assert 0 <= widget.region.y < widget.region.bottom <= app.size.height, (
                    selector
                )

    asyncio.run(scenario())


def test_filter_preserves_hidden_selection_and_bulk_actions_exclude_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        other = dataclasses.replace(
            READY_SESSION, session_id="second", display_name="Evening capture"
        )
        factory.sessions[SourceMode.LAN] = (READY_SESSION, other, UNAVAILABLE_SESSION)
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.press("slash")
            app.query_one("#filter", Input).value = "morning"
            await pilot.pause()
            assert app.query_one("#sessions", SelectionList).option_count == 1
            assert "1 个在筛选外" in str(
                app.query_one("#selection-summary", Static).content
            )
            await pilot.click("#select-all")
            assert app._selected_ids == {other.session_id}
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#sessions", SelectionList).option_count == 3
            await pilot.click("#select-all")
            assert app._selected_ids == {READY_SESSION.session_id, other.session_id}
            await pilot.pause(0.25)
            await pilot.click("#select-all")
            assert not app._selected_ids
            assert app.query_one("#export", Button).disabled

    asyncio.run(scenario())


def test_source_navigation_requires_commit(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            original = app._source
            await pilot.click("#sources")
            await pilot.press("down")
            await pilot.pause()
            assert app._source is original
            assert len(factory.session_calls) == 1
            await pilot.press("enter")
            await _wait_for(
                pilot,
                lambda: len(factory.session_calls) == 2 and not app._sessions_loading,
            )
            assert app._source.source == factory.card_source

    asyncio.run(scenario())


def test_late_discovery_preserves_source_focus_and_filter(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        gate = threading.Event()
        factory.discovery_gates[SourceMode.CARD] = gate
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        try:
            async with app.run_test() as pilot:
                await _wait_for(
                    pilot, lambda: app._source is not None and not app._sessions_loading
                )
                await pilot.press("slash")
                field = app.query_one("#filter", Input)
                field.value = "morning"
                await pilot.pause()
                gate.set()
                await _wait_for(pilot, lambda: len(app._sources) == 2)
                assert app._source.source == LAN_SOURCE
                assert app.focused is field
                assert field.value == "morning"
                assert len(factory.session_calls) == 1
        finally:
            gate.set()

    asyncio.run(scenario())


def test_switching_sources_discards_late_session_results(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        gate = threading.Event()
        factory.session_gates[SourceMode.LAN] = gate
        card_session = dataclasses.replace(READY_SESSION, session_id="card-only")
        factory.sessions[SourceMode.CARD] = (card_session,)
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        try:
            async with app.run_test() as pilot:
                await _wait_for(
                    pilot,
                    lambda: len(app._sources) == 2 and bool(factory.session_calls),
                )
                app.query_one("#sources", Select).value = next(
                    i
                    for i, item in enumerate(app._sources)
                    if item.source.mode is SourceMode.CARD
                )
                await _wait_for(pilot, lambda: not app._sessions_loading)
                gate.set()
                await pilot.pause(0.1)
                assert app._selected_ids == {card_session.session_id}
                assert app._sessions == (card_session,)
        finally:
            gate.set()

    asyncio.run(scenario())


def test_export_locks_mutations_and_every_quit_binding_then_recovers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        gate = threading.Event()
        factory.export_gate = gate
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        try:
            async with app.run_test() as pilot:
                await _wait_for(
                    pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
                )
                await pilot.press("e")
                await _wait_for(pilot, lambda: bool(factory.export_calls))
                assert app.query_one(Tabs).active == "activity-tab"
                for selector in (
                    "#export",
                    "#sources",
                    "#sessions",
                    "#connect",
                    "#change-output",
                    "#rescan",
                ):
                    assert app.query_one(selector).disabled, selector
                assert app.query_one("#progress").display
                for key in ("q", "ctrl+q", "ctrl+c", "r", "a", "o", "e"):
                    await pilot.press(key)
                    assert app.is_running
                    assert len(app.screen_stack) == 1
                assert len(factory.export_calls) == 1
                gate.set()
                await _wait_for(pilot, lambda: not app._exporting)
                assert not app.query_one("#progress").display
                assert app.query_one("#export").disabled
                log = "\n".join(line.text for line in app.query_one(RichLog).lines)
                assert "video/left.mp4" in log and "recording.mp4" in log
        finally:
            gate.set()

    asyncio.run(scenario())


def test_failed_export_keeps_error_and_selection_for_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        factory.export_error = RuntimeError("[red]connection lost[/red]")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.press("e")
            await _wait_for(
                pilot, lambda: bool(factory.export_calls) and not app._exporting
            )
            await pilot.pause()
            assert app._selected_ids == {READY_SESSION.session_id}
            assert "导出失败" in str(app.query_one("#status-message", Static).content)
            assert "[red]connection lost[/red]" in "\n".join(
                line.text for line in app.query_one(RichLog).lines
            )
            factory.export_error = None
            await pilot.click("#export")
            await _wait_for(
                pilot, lambda: len(factory.export_calls) == 2 and not app._exporting
            )
            assert "导出完成" in str(app.query_one("#status-message", Static).content)

    asyncio.run(scenario())


def test_partial_batch_retries_only_failed_recordings(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        other = dataclasses.replace(READY_SESSION, session_id="second")
        factory.sessions[SourceMode.LAN] = (READY_SESSION, other)
        factory.failed_sessions = {READY_SESSION.session_id: "download interrupted"}
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.press("e")
            await _wait_for(
                pilot, lambda: bool(factory.export_calls) and not app._exporting
            )
            assert app._selected_ids == {READY_SESSION.session_id}
            assert "1 个成功 · 1 个失败" in str(
                app.query_one("#status-message", Static).content
            )
            log = "\n".join(line.text for line in app.query_one(RichLog).lines)
            assert "second/recording.mp4" in log
            assert "download interrupted" in log
            assert not app.query_one("#export", Button).disabled
            factory.failed_sessions.clear()
            await pilot.click("#export")
            await _wait_for(
                pilot, lambda: len(factory.export_calls) == 2 and not app._exporting
            )
            assert factory.export_calls[-1]["session_ids"] == (
                READY_SESSION.session_id,
            )
            assert "导出完成" in str(app.query_one("#status-message", Static).content)

    asyncio.run(scenario())


def test_destination_validation_stays_in_dialog_and_blocks_source_card(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        card = tmp_path / "card"
        card.mkdir()
        factory = FakeSDKFactory(card)
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test(size=(48, 18)) as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            app.query_one("#sources", Select).value = next(
                i
                for i, item in enumerate(app._sources)
                if item.source.mode is SourceMode.CARD
            )
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            field = app.screen.query_one(Input)
            field.value = str(card / "exports")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TextEntryDialog)
            assert "源内存卡" in str(
                app.screen.query_one("#entry-error", Static).content
            )
            assert app.export_root == tmp_path
            submit = app.screen.query_one("#entry-submit")
            assert (
                submit.region.right <= app.size.width
                and submit.region.bottom <= app.size.height
            )
            field.value = str(tmp_path / "valid")
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, TextEntryDialog)
            assert app.export_root == tmp_path / "valid"

    asyncio.run(scenario())


def test_insufficient_space_blocks_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openaria.bridge.sdk import tui

    monkeypatch.setattr(tui, "free_bytes", lambda _: 1)

    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.press("e")
            await pilot.pause()
            assert not factory.export_calls
            assert "空间不足" in str(app.query_one("#status-message", Static).content)

    asyncio.run(scenario())


def test_manual_failure_allows_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card", no_automatic_sources=True)
        factory.manual_error = ConnectionError("device offline")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot,
                lambda: len(factory.created_modes) == 2 and not app._pending_modes,
            )
            for failure in (True, False):
                await pilot.press("a")
                await pilot.pause()
                app.screen.query_one(Input).value = "192.0.2.24"
                await pilot.press("enter")
                await pilot.pause()
                await _wait_for(pilot, lambda: not app._connecting)
                if failure:
                    assert "连接失败" in str(
                        app.query_one("#status-message", Static).content
                    )
                    assert not app.query_one("#connect").disabled
                    factory.manual_error = None
                else:
                    await _wait_for(pilot, lambda: not app._sessions_loading)
                    assert app._source.source == LAN_SOURCE

    asyncio.run(scenario())


def test_rescan_cancels_delivery_from_previous_discovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        arrived, release = threading.Event(), threading.Event()
        stale = dataclasses.replace(
            LAN_SOURCE, mode=SourceMode.CARD, location="stale-card"
        )

        class Factory(FakeSDKFactory):
            def __call__(self, **kwargs):
                sdk = super().__call__(**kwargs)
                if (
                    sdk.mode is SourceMode.CARD
                    and self.created_modes.count(SourceMode.CARD) == 1
                ):

                    def blocked_discover(**kwargs):
                        arrived.set()
                        assert release.wait(5)
                        return (stale,)

                    sdk.discover = blocked_discover
                return sdk

        factory = Factory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        try:
            async with app.run_test() as pilot:
                await _wait_for(
                    pilot, lambda: arrived.is_set() and app._source is not None
                )
                await pilot.press("r")
                await _wait_for(
                    pilot,
                    lambda: (
                        len(factory.created_modes) == 4
                        and not app._pending_modes
                        and not app._sessions_loading
                    ),
                )
                release.set()
                await pilot.pause(0.1)
                assert len(app._sources) == 2
                assert all(item.source != stale for item in app._sources)
                assert app._source.source == LAN_SOURCE
        finally:
            release.set()

    asyncio.run(scenario())


def test_manual_reconnect_refreshes_existing_source_without_duplicate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            replacement = dataclasses.replace(READY_SESSION, session_id="new-recording")
            factory.sessions[SourceMode.LAN] = (replacement,)
            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one(Input).value = "192.0.2.24"
            await pilot.press("enter")
            await _wait_for(
                pilot,
                lambda: len(factory.session_calls) == 2 and not app._sessions_loading,
            )
            assert len(app._sources) == 2
            assert app._selected_ids == {replacement.session_id}

    asyncio.run(scenario())


@pytest.mark.parametrize("sessions", [(), (UNAVAILABLE_SESSION,)])
def test_empty_and_unavailable_inventories_never_enable_export(
    tmp_path: Path, sessions
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        factory.sessions[SourceMode.LAN] = sessions
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        async with app.run_test() as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            assert not app._selected_ids
            assert app.query_one("#export").disabled
            assert app.query_one("#select-all").disabled
            assert app.query_one("#sessions", SelectionList).option_count == len(
                sessions
            )
            app.query_one("#sessions", SelectionList).focus()
            await pilot.press("space", "e")
            assert not factory.export_calls

    asyncio.run(scenario())


def test_background_completion_does_not_disturb_open_dialog(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        gate = threading.Event()
        factory.session_gates[SourceMode.LAN] = gate
        app = OpenAriaTUI(default_output=tmp_path, sdk_factory=factory)
        try:
            async with app.run_test() as pilot:
                await _wait_for(
                    pilot,
                    lambda: len(app._sources) == 2 and bool(factory.session_calls),
                )
                await pilot.press("o")
                await pilot.pause()
                field = app.screen.query_one(Input)
                gate.set()
                await _wait_for(pilot, lambda: not app._sessions_loading)
                assert isinstance(app.screen, TextEntryDialog)
                assert app.focused is field
                await pilot.press("escape")
                await pilot.pause()
                assert not app.query_one("#export").disabled
        finally:
            gate.set()

    asyncio.run(scenario())


def test_long_labels_and_resize_preserve_selection_and_control_bounds(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        long_session = dataclasses.replace(
            READY_SESSION, display_name="长录制名称 [red] / " * 20
        )
        factory.sessions[SourceMode.LAN] = (long_session,)
        app = OpenAriaTUI(
            default_output=tmp_path / ("long-output-" * 10), sdk_factory=factory
        )
        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            for width, height in ((48, 18), (80, 24), (120, 36)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert app._selected_ids == {READY_SESSION.session_id}
                rows = app.query_one("#sessions", SelectionList)
                assert rows.selected == [READY_SESSION.session_id]
                assert "[red]" in rows.get_option_at_index(0).prompt.plain
                assert rows.region.right <= app.size.width
                assert app.query_one("#export").region.right <= app.size.width
                app.action_choose_output()
                await pilot.pause()
                dialog = app.screen.query_one("#entry-dialog")
                for selector in ("#entry-input", "#entry-submit", "#entry-cancel"):
                    child = app.screen.query_one(selector)
                    assert child.region.right <= dialog.region.right
                    assert child.region.bottom <= dialog.region.bottom
                await pilot.press("escape")

    asyncio.run(scenario())


def test_export_settings_survive_workbench_actions_and_reach_batch(
    tmp_path: Path,
) -> None:
    from textual.widgets import Checkbox

    from openaria.bridge.sdk import ExportOptions
    from openaria.bridge.sdk.tui import ExportSettingsDialog

    async def scenario() -> None:
        factory = FakeSDKFactory(tmp_path / "card")
        app = OpenAriaTUI(default_output=tmp_path / "exports", sdk_factory=factory)
        async with app.run_test(size=(80, 24)) as pilot:
            await _wait_for(
                pilot, lambda: len(app._sources) == 2 and not app._sessions_loading
            )
            await pilot.click("#export-settings")
            await pilot.pause()
            assert isinstance(app.screen, ExportSettingsDialog)
            app.screen.query_one("#export-codec", Select).value = "hevc"
            app.screen.query_one("#export-quality", Select).value = "high"
            app.screen.query_one("#export-delay", Input).value = "30"
            app.screen.query_one("#export-retain", Checkbox).value = True
            await pilot.click("#settings-apply")
            await pilot.pause()
            expected = ExportOptions("hevc", 0.030, True, "high")
            assert app.export_options == expected
            app._deleting = True
            app._sync_controls()
            app.action_export_settings()
            assert not isinstance(app.screen, ExportSettingsDialog)
            assert app.query_one("#export-settings").disabled
            app._deleting = False
            app._sync_controls()
            await pilot.press("p")
            await pilot.pause()
            assert app.screen.query_one("#export-quality", Select).value == "high"
            app.screen.query_one("#export-delay", Input).value = "1001"
            await pilot.click("#settings-apply")
            await pilot.pause()
            assert isinstance(app.screen, ExportSettingsDialog)
            await pilot.press("escape")
            await pilot.pause()
            assert app.export_options == expected
            app.action_export_selected()
            await _wait_for(
                pilot, lambda: bool(factory.export_calls) and not app._exporting
            )
            assert factory.export_calls[-1]["options"] == expected
            assert factory.export_calls[-1]["continue_on_error"] is True

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
