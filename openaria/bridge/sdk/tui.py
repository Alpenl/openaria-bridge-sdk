"""A compact recording workbench built from Textual's native widgets."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Protocol

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Input,
    ProgressBar,
    RichLog,
    Select,
    SelectionList,
    Static,
    Tab,
    Tabs,
)
from textual.widgets.selection_list import Selection

from ._history import ExportHistory
from ._tui_widgets import (
    DeleteDialog,
    ExportSettingsDialog,
    TextEntryDialog,
    duration,
    existing_parent,
    free_bytes,
    human_bytes,
    path_is_on_source_card,
    session_label,
    source_label,
    started_at,
)
from .client import OpenAriaSDK
from .models import DeleteResult, ExportResult, SessionInfo, Source, SourceMode
from .options import ExportOptions


class SDKBackend(Protocol):
    def delete_sessions(
        self,
        *,
        source: Source,
        session_ids: tuple[str, ...],
        expected_manifests: dict[str, str] | None = None,
    ) -> DeleteResult: ...

    def discover(self, *, refresh: bool = False) -> tuple[Source, ...]: ...

    def list_sessions(
        self,
        source: Source | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[SessionInfo, ...]: ...

    def export(
        self,
        *,
        source: Source | None = None,
        session_ids: tuple[str, ...] | None = None,
        output: Path | str | None = None,
        progress: Callable[[str], None] | None = None,
        continue_on_error: bool = False,
        options: ExportOptions | None = None,
    ) -> ExportResult: ...


SDKFactory = Callable[..., SDKBackend]


@dataclass(frozen=True)
class SourceBinding:
    source: Source
    sdk: SDKBackend


class OpenAriaTUI(App[None]):
    CSS_PATH = Path(__file__).with_name("openaria.tcss")
    TITLE = "Open Aria Bridge"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [
        (0, "-narrow"),
        (90, "-wide"),
    ]
    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [
        (0, "-short"),
        (26, "-tall"),
    ]
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("e", "export_selected", "导出"),
        Binding("delete", "delete_selected", "删除", show=False),
        Binding("slash", "search", "筛选", key_display="/"),
        Binding("r", "rescan", "刷新"),
        Binding("a", "add_device", "连接", show=False),
        Binding("o", "choose_output", "目录", show=False),
        Binding("p", "export_settings", "导出设置", show=False),
        Binding("l", "toggle_log", "记录"),
        Binding("q", "quit", "退出"),
        Binding("ctrl+q", "quit", "退出", show=False, priority=True),
        Binding("ctrl+c", "quit", "退出", show=False),
        Binding("escape", "clear_search", "清除筛选", show=False),
    ]

    def __init__(
        self,
        *,
        default_output: Path | None = None,
        sdk_factory: SDKFactory = OpenAriaSDK,
        auto_scan: bool = True,
        history_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(
            Theme(
                name="openaria",
                primary="#8cc9bd",
                secondary="#d7bd7b",
                accent="#8cc9bd",
                foreground="#dcdedd",
                background="#171918",
                surface="#222524",
                panel="#222524",
                success="#8cc9bd",
                warning="#d7bd7b",
                error="#ed9b9b",
                dark=True,
            )
        )
        self.theme = "openaria"
        self.export_options = ExportOptions()
        self.export_root = (
            default_output or Path.home() / "OpenAria Exports"
        ).expanduser()
        self._sdk_factory = sdk_factory
        self._history = ExportHistory(history_path)
        self._exported_ids: set[str] = set()
        self._deleting = False
        self._manual_endpoints: set[str] = set()
        self._auto_scan = auto_scan
        self._sources: list[SourceBinding] = []
        self._source: SourceBinding | None = None
        self._sessions: tuple[SessionInfo, ...] = ()
        self._selected_ids: set[str] = set()
        self._visible_ids: set[str] = set()
        self._pending_modes: set[SourceMode] = set()
        self._sessions_loading = False
        self._connecting = False
        self._exporting = False
        self._export_started = 0.0
        self._log_count = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="workbench"):
            with Horizontal(id="masthead"):
                yield Static("Open Aria", id="brand")
                yield Static("BRIDGE  /  录制导出", id="subtitle")
                yield Static("就绪", id="connection-state", markup=False)
            with Horizontal(id="source-bar"):
                yield Select[int](
                    [("尚未发现来源", -1)],
                    allow_blank=False,
                    id="sources",
                    compact=True,
                )
                yield Button("↻", id="rescan", flat=True, tooltip="重新扫描来源 (R)")
                yield Button(
                    "+ 连接", id="connect", flat=True, tooltip="手动连接机身 (A)"
                )
            yield Tabs(
                Tab("录制", id="recordings-tab"),
                Tab("任务记录", id="activity-tab"),
                id="views",
            )
            with ContentSwitcher(initial="recordings", id="content"):
                with Vertical(id="recordings"):
                    with Horizontal(id="filter-bar"):
                        yield Input(placeholder="筛选录制…", id="filter", compact=True)
                        yield Button("全选", id="select-all", flat=True, compact=True)
                        yield Button(
                            "选已导出", id="select-exported", flat=True, compact=True
                        )
                        yield Button("删除", id="delete", flat=True, compact=True)
                    yield Static("尚未读取录制", id="session-summary", markup=False)
                    yield Static("尚未发现来源", id="empty-state", markup=False)
                    yield SelectionList[str](id="sessions")
                    yield Static("", id="session-detail", markup=False)
                with Vertical(id="activity-view"):
                    yield Static("任务记录", id="log-heading")
                    yield RichLog(
                        id="activity-log", wrap=True, min_width=1, max_lines=2000
                    )
            with Vertical(id="transfer-bar"):
                with Horizontal(id="destination-row"):
                    yield Static("", id="destination-path", markup=False)
                    yield Button(
                        "目录…",
                        id="change-output",
                        flat=True,
                        compact=True,
                        tooltip="更改导出目录 (O)",
                    )
                    yield Button(
                        "设置…",
                        id="export-settings",
                        flat=True,
                        compact=True,
                        tooltip="导出设置 (P)",
                    )
                with Horizontal(id="export-row"):
                    yield Static("尚未选择录制", id="selection-summary", markup=False)
                    yield Button("导出成片", variant="primary", id="export", flat=True)
                yield ProgressBar(
                    total=None, show_percentage=False, show_eta=False, id="progress"
                )
                with Horizontal(id="status-row"):
                    yield Static("准备就绪", id="status-message", markup=False)
                    yield Static("", id="elapsed", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._render_destination()
        self._render_sessions()
        self._sync_controls()
        self.set_interval(1, self._tick)
        if self._auto_scan:
            self.call_after_refresh(self.action_rescan)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return len(self.screen_stack) == 1 or action == "quit"

    def _log(self, message: str, *, error: bool = False) -> None:
        self._log_count += 1
        self.query_one("#log-heading", Static).update(
            f"任务记录 · {self._log_count} 条"
        )
        self.query_one(RichLog).write(
            Text.assemble(
                (datetime.now(UTC).astimezone().strftime("%H:%M:%S  "), "#929596"),
                (message, "#ed9b9b" if error else "#dcdedd"),
            )
        )

    def _status(self, message: str, *, error: bool = False) -> None:
        widget = self.query_one("#status-message", Static)
        widget.update(message)
        widget.tooltip = Text(message)
        widget.set_class(error, "error")

    @on(Button.Pressed, "#rescan")
    def action_rescan(self) -> None:
        if self._exporting or self._connecting or self._deleting:
            return
        self.workers.cancel_group(self, "sessions")
        self._sources.clear()
        self._source = None
        self._sessions = ()
        self._exported_ids.clear()
        self._selected_ids.clear()
        self._sessions_loading = False
        self._pending_modes = {SourceMode.CARD, SourceMode.LAN}
        self._render_sources()
        self._render_sessions()
        self._status("正在查找局域网设备与内存卡…")
        self._log("扫描局域网设备与内存卡")
        self._sync_controls()
        self._scan()

    @work(group="discovery", exclusive=True)
    async def _scan(self) -> None:
        # TaskGroup cancels pending UI deliveries when a newer scan replaces this one.
        async with asyncio.TaskGroup() as group:
            for mode in (SourceMode.CARD, SourceMode.LAN):
                group.create_task(self._discover(mode))

    def _probe(
        self, mode: SourceMode, endpoint: str | None = None
    ) -> list[SourceBinding]:
        kwargs = {"endpoint": endpoint} if endpoint is not None else {}
        sdk = self._sdk_factory(mode=mode, output=self.export_root, **kwargs)
        return [SourceBinding(source, sdk) for source in sdk.discover()]

    async def _discover(self, mode: SourceMode) -> None:
        async def probe(endpoint: str | None) -> None:
            try:
                bindings = await asyncio.to_thread(self._probe, mode, endpoint)
            except Exception as error:  # noqa: BLE001 - report backend failures in the task log
                self._log(
                    f"{endpoint or ('局域网' if mode is SourceMode.LAN else '内存卡')}：{error}",
                    error=True,
                )
            else:
                self._add_sources(bindings)

        async with asyncio.TaskGroup() as group:
            group.create_task(probe(None))
            if mode is SourceMode.LAN:
                for endpoint in sorted(self._manual_endpoints):
                    group.create_task(probe(endpoint))
        self._pending_modes.discard(mode)
        self._sync_controls()
        if not self._pending_modes and not self._sources:
            self._status("未找到来源")
            self._render_sessions()

    def _add_sources(
        self, bindings: list[SourceBinding], *, select: bool = False
    ) -> None:
        for binding in bindings:
            existing = next(
                (
                    index
                    for index, item in enumerate(self._sources)
                    if item.source.mode == binding.source.mode
                    and item.source.location == binding.source.location
                ),
                None,
            )
            if existing is None:
                self._sources.append(binding)
                self._log(f"已发现 {binding.source.display_name}")
            elif select:
                self._sources[existing] = binding
        self._render_sources()
        if bindings and (select or self._source is None):
            chosen = next(
                item
                for item in self._sources
                if item.source.mode == bindings[0].source.mode
                and item.source.location == bindings[0].source.location
            )
            self._select_source(chosen)

    def _render_sources(self) -> None:
        selector = self.query_one("#sources", Select)
        with selector.prevent(Select.Changed):
            options = [
                (source_label(item.source), index)
                for index, item in enumerate(self._sources)
            ]
            placeholder = "正在查找来源…" if self._pending_modes else "尚未发现来源"
            selector.set_options(options or [(Text(placeholder), -1)])
            if self._source in self._sources:
                selector.value = self._sources.index(self._source)

    @on(Select.Changed, "#sources")
    def source_changed(self, event: Select.Changed) -> None:
        if (
            isinstance(event.value, int)
            and 0 <= event.value < len(self._sources)
            and not self._exporting
            and not self._connecting
            and not self._deleting
        ):
            self._select_source(self._sources[event.value])

    def _select_source(self, binding: SourceBinding) -> None:
        if binding is self._source:
            return
        self._source = binding
        self._sessions = ()
        self._exported_ids.clear()
        self._selected_ids.clear()
        self._sessions_loading = True
        self.query_one("#filter", Input).value = ""
        self._render_sources()
        self._render_sessions()
        self._status(f"正在读取 {binding.source.display_name}…")
        self._sync_controls()
        self._load_sessions(binding)

    @work(group="sessions", exclusive=True)
    async def _load_sessions(self, binding: SourceBinding) -> None:
        started = time.monotonic()
        try:
            sessions = await asyncio.to_thread(
                binding.sdk.list_sessions, binding.source
            )
        except Exception as error:  # noqa: BLE001 - restore controls after backend failures
            self._sessions_loading = False
            self._status(f"录制读取失败：{error}", error=True)
            self._log(
                f"读取 {binding.source.display_name} 失败 "
                f"({time.monotonic() - started:.2f}s)：{error}",
                error=True,
            )
            self._render_sessions(empty="录制读取失败")
        else:
            try:
                self._exported_ids = await asyncio.to_thread(
                    self._history.exported_ids,
                    binding.source,
                    sessions,
                    self.export_root,
                )
            except Exception as error:  # noqa: BLE001 - history must not hide recordings
                self._exported_ids = set()
                self._log(f"读取导出标记失败：{error}", error=True)
            self._sessions = sessions
            self._sessions_loading = False
            self._selected_ids = {
                session.session_id
                for session in sessions
                if session.exportable and session.session_id not in self._exported_ids
            }
            self._render_sessions()
            self._status(
                "录制已就绪"
                if self._selected_ids
                else (
                    "录制均已导出" if self._exported_ids else "此来源没有可导出的录制"
                )
            )
            self._log(
                f"{binding.source.display_name} · {len(sessions)} 个录制，"
                f"{len(self._exported_ids)} 个已导出 · {time.monotonic() - started:.2f}s"
            )
            if (
                self._selected_ids
                and len(self.screen_stack) == 1
                and not isinstance(self.focused, Input)
            ):
                self.query_one("#sessions", SelectionList).focus()
        self._sync_controls()

    def _filtered_sessions(self) -> tuple[SessionInfo, ...]:
        query = self.query_one("#filter", Input).value.strip().casefold()
        return tuple(
            session
            for session in self._sessions
            if query
            in (
                f"{session.display_name} {session.session_id} {session.started_at}".casefold()
            )
        )

    def _render_sessions(self, *, empty: str | None = None) -> None:
        sessions = self.query_one("#sessions", SelectionList)
        highlighted = None
        if sessions.highlighted is not None and sessions.option_count:
            highlighted = sessions.get_option_at_index(sessions.highlighted).value
        visible = self._filtered_sessions()
        self._visible_ids = {session.session_id for session in visible}
        with sessions.prevent(SelectionList.SelectedChanged):
            sessions.clear_options()
            sessions.add_options(
                Selection(
                    session_label(
                        session,
                        max(24, self.query_one("#workbench").content_size.width - 5),
                        exported=session.session_id in self._exported_ids,
                    ),
                    session.session_id,
                    session.session_id in self._selected_ids,
                    disabled=not session.exportable,
                )
                for session in visible
            )
        usable_indices = [
            index for index, session in enumerate(visible) if session.exportable
        ]
        sessions.highlighted = next(
            (
                index
                for index in usable_indices
                if visible[index].session_id == highlighted
            ),
            next(iter(usable_indices), None),
        )
        usable = sum(session.exportable for session in self._sessions)
        summary = f"{len(self._sessions)} 个录制 · {usable} 个可导出"
        if self._exported_ids:
            summary += f" · {len(self._exported_ids)} 个已导出"
        if len(visible) != len(self._sessions):
            summary += f" · 筛选出 {len(visible)} 个"
        self.query_one("#session-summary", Static).update(summary)
        if empty is None:
            if self._sessions_loading:
                empty = "正在读取录制…"
            elif self._source is None:
                empty = "正在查找来源…" if self._pending_modes else "尚未发现来源"
            else:
                empty = "没有匹配的录制" if self._sessions else "此来源没有录制"
        self.query_one("#empty-state", Static).update(empty)
        self.query_one("#empty-state").display = not visible
        sessions.display = bool(visible)
        self.query_one("#session-detail", Static).update("")
        self._render_selection()

    def on_resize(self) -> None:
        if self.is_mounted:
            self.call_after_refresh(self._render_sessions)

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        self._render_sessions()

    @on(SelectionList.SelectedChanged, "#sessions")
    def selection_changed(self) -> None:
        selected = set(self.query_one("#sessions", SelectionList).selected)
        exportable = {
            session.session_id for session in self._sessions if session.exportable
        }
        self._selected_ids = (
            (self._selected_ids - self._visible_ids) | selected
        ) & exportable
        self._render_selection()

    @on(SelectionList.SelectionHighlighted, "#sessions")
    def session_highlighted(self, event: SelectionList.SelectionHighlighted) -> None:
        session = next(
            (
                item
                for item in self._sessions
                if item.session_id == event.selection.value
            ),
            None,
        )
        if session:
            detail = f"{session.display_name}\n{started_at(session)} · {duration(session.duration_seconds)} · {session.session_id}"
            widget = self.query_one("#session-detail", Static)
            widget.update(detail)
            widget.tooltip = Text(detail)

    @on(Button.Pressed, "#select-all")
    def toggle_selection(self) -> None:
        if (
            self._exporting
            or self._sessions_loading
            or self._connecting
            or self._deleting
        ):
            return
        visible = {
            session.session_id
            for session in self._filtered_sessions()
            if session.exportable
        }
        if visible <= self._selected_ids:
            self._selected_ids -= visible
        else:
            self._selected_ids |= visible
        self._render_sessions()

    @on(Button.Pressed, "#select-exported")
    def select_exported(self) -> None:
        if (
            self._exporting
            or self._connecting
            or self._deleting
            or self._sessions_loading
        ):
            return
        self._selected_ids = {
            session.session_id
            for session in self._filtered_sessions()
            if session.exportable and session.session_id in self._exported_ids
        }
        self._render_sessions()

    def _render_selection(self) -> None:
        chosen = [
            session
            for session in self._sessions
            if session.session_id in self._selected_ids
        ]
        summary = f"已选 {len(chosen)} 个 · {human_bytes(sum(item.total_bytes for item in chosen))}"
        hidden = len(self._selected_ids - self._visible_ids)
        if hidden:
            summary += f" · {hidden} 个在筛选外"
        self.query_one("#selection-summary", Static).update(summary)
        visible = {
            item.session_id for item in self._filtered_sessions() if item.exportable
        }
        button = self.query_one("#select-all", Button)
        button.label = "清空" if visible and visible <= self._selected_ids else "全选"
        locked = (
            self._exporting
            or self._connecting
            or self._deleting
            or self._sessions_loading
        )
        button.disabled = not visible or locked
        self.query_one("#select-exported", Button).disabled = locked or not (
            visible & self._exported_ids
        )
        delete = self.query_one("#delete", Button)
        can_delete = self._source is not None and (
            self._source.source.mode is SourceMode.CARD
            or self._source.source.capabilities.get("session_deletion", False)
        )
        delete.disabled = locked or not chosen or not can_delete
        delete.tooltip = (
            "删除选中的源录制，保留本机成片"
            if can_delete
            else "设备固件尚不支持远程删除，请升级固件后刷新来源"
        )
        self.query_one("#export", Button).disabled = not chosen or locked

    @on(Button.Pressed, "#connect")
    def action_add_device(self) -> None:
        if self._exporting or self._connecting or self._deleting:
            return
        self.push_screen(
            TextEntryDialog(
                title="连接机身",
                label="IP 地址或设备 URL",
                placeholder="192.168.110.36",
                submit_label="连接",
            ),
            self._manual_address_entered,
        )

    def _manual_address_entered(self, endpoint: str | None) -> None:
        if endpoint:
            self._connecting = True
            self._status(f"正在连接 {endpoint}…")
            self._sync_controls()
            self._connect(endpoint)

    @work(group="connection", exclusive=True)
    async def _connect(self, endpoint: str) -> None:
        try:
            bindings = await asyncio.to_thread(self._probe, SourceMode.LAN, endpoint)
            if not bindings:
                raise ValueError("未识别到 Open Aria 设备")
        except Exception as error:  # noqa: BLE001 - allow retry after backend failures
            self._status("连接失败", error=True)
            self._log(f"连接 {endpoint} 失败：{error}", error=True)
            self._show_view("activity-tab")
        else:
            self._manual_endpoints.update(
                binding.source.api_base or binding.source.location
                for binding in bindings
            )
            self._add_sources(bindings, select=True)
            self._show_view("recordings-tab")
        self._connecting = False
        self._sync_controls()

    @on(Button.Pressed, "#change-output")
    def action_choose_output(self) -> None:
        if self._exporting or self._deleting:
            return
        self.push_screen(
            TextEntryDialog(
                title="导出目录",
                label="本机路径",
                initial=str(self.export_root),
                submit_label="使用此目录",
                validate=self._output_error,
            ),
            self._output_entered,
        )

    @on(Button.Pressed, "#export-settings")
    def action_export_settings(self) -> None:
        if not self._exporting and not self._deleting:
            self.push_screen(
                ExportSettingsDialog(self.export_options), self._settings_selected
            )

    def _settings_selected(self, options: ExportOptions | None) -> None:
        if options is not None:
            self.export_options = options
            self.notify(
                f"{options.video_codec.upper()} · 音频延后 {options.audio_calibration_seconds * 1000:g} ms · "
                + ("保留源媒体" if options.retain_sources else "仅保存成片")
            )

    def _output_error(self, value: str) -> str | None:
        try:
            candidate = Path(value).expanduser().resolve()
            if self._source and path_is_on_source_card(candidate, self._source.source):
                return "导出目录不能位于源内存卡上"
            parent = existing_parent(candidate)
            if not parent.is_dir():
                return "该路径不是目录"
            if not os.access(parent, os.W_OK | os.X_OK):
                return "没有此目录的写入权限"
        except (OSError, ValueError) as error:
            return f"目录不可用：{error}"
        return None

    def _output_entered(self, value: str | None) -> None:
        if value:
            self.export_root = Path(value).expanduser().resolve()
            self._render_destination()
            if self._source and not self._sessions_loading:
                self._sessions_loading = True
                self._sync_controls()
                self._load_sessions(self._source)

    def _render_destination(self) -> None:
        free = free_bytes(self.export_root)
        suffix = (
            f" · 可用 {human_bytes(free)}" if free is not None else " · 可用空间未知"
        )
        widget = self.query_one("#destination-path", Static)
        widget.update(
            Text.assemble((str(self.export_root), "#dcdedd"), (suffix, "#929596"))
        )
        widget.tooltip = Text(str(self.export_root) + suffix)

    @on(Button.Pressed, "#export")
    def action_export_selected(self) -> None:
        if (
            self._exporting
            or self._deleting
            or self._sessions_loading
            or self._connecting
            or not self._source
            or not self._selected_ids
        ):
            return
        error = self._output_error(str(self.export_root))
        chosen = tuple(
            session
            for session in self._sessions
            if session.exportable and session.session_id in self._selected_ids
        )
        # The pipeline briefly retains both verified inputs and rendered output.
        required = sum(session.total_bytes for session in chosen) * 2
        free = free_bytes(self.export_root)
        if not error and free is not None and required > free:
            error = f"空间不足：处理期间约需 {human_bytes(required)}，可用 {human_bytes(free)}"
        if error:
            self._status(error, error=True)
            self._log(error, error=True)
            return
        self._exporting = True
        self._export_started = time.monotonic()
        self._status("正在准备导出…")
        self._log(f"开始导出 {len(chosen)} 个录制 → {self.export_root}")
        self._show_view("activity-tab")
        self._sync_controls()
        self._export(
            self._source,
            tuple(session.session_id for session in chosen),
            self.export_root,
        )

    @work(group="export", exclusive=True)
    async def _export(
        self, binding: SourceBinding, session_ids: tuple[str, ...], output: Path
    ) -> None:
        def progress(message: str) -> None:
            self.call_from_thread(self._export_progress, message)

        try:
            result = await asyncio.to_thread(
                binding.sdk.export,
                source=binding.source,
                session_ids=session_ids,
                output=output,
                progress=progress,
                continue_on_error=True,
                options=self.export_options,
            )
        except Exception as error:  # noqa: BLE001 - preserve selection for a retry
            self._status("导出失败 · 可重试", error=True)
            self._log(f"导出失败：{error}", error=True)
        else:
            self._exported_ids.update(session.session_id for session in result.sessions)
            self._selected_ids.difference_update(
                session.session_id for session in result.sessions
            )
            try:
                await asyncio.to_thread(
                    self._history.record,
                    binding.source,
                    self._sessions,
                    result.sessions,
                )
            except Exception as error:  # noqa: BLE001 - exports remain successful if state storage fails
                self._log(f"成片已导出，但保存导出标记失败：{error}", error=True)
            self._render_sessions()
            reused = sum(session.reused for session in result.sessions)
            size = human_bytes(sum(session.media_bytes for session in result.sessions))
            summary = f"导出完成 · {result.exported_count} 个成片 · {size}"
            if reused:
                summary += f" · 复用 {reused} 个"
            if result.failed_sessions:
                summary = (
                    f"导出结束 · {result.exported_count} 个成功 · "
                    f"{len(result.failed_sessions)} 个失败"
                )
                self._selected_ids.difference_update(
                    session.session_id for session in result.sessions
                )
                self._render_sessions()
            self._status(summary, error=bool(result.failed_sessions))
            self._log(summary, error=bool(result.failed_sessions))
            for session in result.sessions:
                self._log(str(session.media_path or session.path))
            for failure in result.failed_sessions:
                self._log(f"{failure.session_id}：{failure.error}", error=True)
            self._render_destination()
        finally:
            self._exporting = False
            self._tick()
            self._sync_controls()

    def _export_progress(self, message: str) -> None:
        self._status(message)
        self._log(message)

    @on(Button.Pressed, "#delete")
    def action_delete_selected(self) -> None:
        if (
            self._exporting
            or self._deleting
            or self._connecting
            or self._sessions_loading
        ):
            return
        binding = self._source
        if binding is None or not self._selected_ids:
            return
        if (
            binding.source.mode is SourceMode.LAN
            and not binding.source.capabilities.get("session_deletion", False)
        ):
            self._status("设备固件尚不支持远程删除，请升级固件后刷新来源", error=True)
            return
        chosen = tuple(
            session
            for session in self._sessions
            if session.session_id in self._selected_ids
        )
        unexported = sum(
            session.session_id not in self._exported_ids for session in chosen
        )
        names = "\n".join(session.display_name for session in chosen[:4])
        if len(chosen) > 4:
            names += f"\n另有 {len(chosen) - 4} 个录制"
        hidden = len(self._selected_ids - self._visible_ids)
        description = (
            f"{binding.source.display_name}\n{binding.source.location}\n"
            f"{len(chosen)} 个录制 · {human_bytes(sum(session.total_bytes for session in chosen))}"
            f" · {unexported} 个未导出\n"
            + (f"其中 {hidden} 个在当前筛选外\n" if hidden else "")
            + f"\n{names}"
        )

        def confirmed(value: bool | None) -> None:
            if value and self._source is binding:
                self._deleting = True
                self._sync_controls()
                self._status("正在删除源录制…")
                self._log(
                    f"删除 {binding.source.display_name} 上的 {len(chosen)} 个源录制"
                )
                self._delete(binding, tuple(session.session_id for session in chosen))

        self.push_screen(DeleteDialog(description), confirmed)

    @work(group="deletion", exclusive=True)
    async def _delete(
        self, binding: SourceBinding, session_ids: tuple[str, ...]
    ) -> None:
        try:
            kwargs = {}
            if binding.source.mode is SourceMode.LAN:
                kwargs["expected_manifests"] = {
                    session.session_id: session.manifest_sha256
                    for session in self._sessions
                    if session.session_id in session_ids
                }
            result = await asyncio.to_thread(
                binding.sdk.delete_sessions,
                source=binding.source,
                session_ids=session_ids,
                **kwargs,
            )
        except Exception as error:  # noqa: BLE001 - keep the failure visible and restore controls
            self._status(f"删除失败：{error}", error=True)
            self._log(f"删除失败：{error}", error=True)
        else:
            deleted = set(result.deleted_session_ids)
            self._sessions = tuple(
                session
                for session in self._sessions
                if session.session_id not in deleted
            )
            self._selected_ids.difference_update(deleted)
            self._exported_ids.difference_update(deleted)
            summary = f"删除完成 · {len(deleted)} 个已删除 · {len(result.failed_sessions)} 个失败"
            self._status(summary, error=bool(result.failed_sessions))
            self._log(summary, error=bool(result.failed_sessions))
            for failure in result.failed_sessions:
                self._log(f"{failure.session_id}：{failure.error}", error=True)
            for session_id in result.deleted_session_ids:
                self._log(f"已删除源录制：{session_id}")
            self._render_sessions()
        finally:
            self._deleting = False
            self._sync_controls()

    def _tick(self) -> None:
        if self._export_started:
            self.query_one("#elapsed", Static).update(
                duration(time.monotonic() - self._export_started)
            )
        if not self._exporting:
            self._export_started = 0

    def _sync_controls(self) -> None:
        locked = self._exporting or self._connecting or self._deleting
        for selector in ("#sources", "#rescan", "#connect", "#sessions", "#filter"):
            self.query_one(selector).disabled = locked
        self.query_one("#sources").disabled = locked or not self._sources
        if not self._sources:
            self._render_sources()
        self.query_one("#change-output").disabled = self._exporting or self._deleting
        self.query_one("#export-settings").disabled = self._exporting or self._deleting
        self.query_one("#progress").display = self._exporting or self._deleting
        self.query_one("#export", Button).label = (
            "导出中…" if self._exporting else "导出成片"
        )
        state = (
            "导出中"
            if self._exporting
            else (
                "连接中"
                if self._connecting
                else (
                    "扫描中" if self._pending_modes else f"{len(self._sources)} 个来源"
                )
            )
        )
        self.query_one("#connection-state", Static).update(
            "删除中" if self._deleting else state
        )
        self._render_selection()

    @on(Tabs.TabActivated, "#views")
    def view_changed(self, event: Tabs.TabActivated) -> None:
        self.query_one(ContentSwitcher).current = (
            "recordings" if event.tab.id == "recordings-tab" else "activity-view"
        )

    def _show_view(self, tab: str) -> None:
        self.query_one("#views", Tabs).active = tab

    def action_toggle_log(self) -> None:
        self._show_view(
            "recordings-tab"
            if self.query_one(Tabs).active == "activity-tab"
            else "activity-tab"
        )

    def action_search(self) -> None:
        if not self._exporting and not self._connecting and not self._deleting:
            self._show_view("recordings-tab")
            self.query_one("#filter", Input).focus()

    def action_clear_search(self) -> None:
        self.query_one("#filter", Input).value = ""
        self.query_one("#sessions", SelectionList).focus()

    def action_quit(self) -> None:
        if self._exporting or self._deleting:
            self.notify("任务进行中，请等待当前任务完成", severity="warning")
        else:
            self.exit()
