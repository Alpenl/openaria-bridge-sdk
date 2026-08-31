"""Automatic full-screen interface for verified Open Aria exports."""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection
from textual.worker import get_current_worker

from .client import OpenAriaSDK
from .errors import OpenAriaError
from .models import ExportResult, SessionInfo, Source, SourceMode


class SDKBackend(Protocol):
    def discover(self, *, refresh: bool = False) -> tuple[Source, ...]: ...

    def list_sessions(
        self, source: Source | None = None, *, refresh: bool = False
    ) -> tuple[SessionInfo, ...]: ...

    def export(
        self,
        *,
        source: Source | None = None,
        session_ids: tuple[str, ...] | None = None,
        output: Path | str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ExportResult: ...


SDKFactory = Callable[..., SDKBackend]


@dataclasses.dataclass(frozen=True)
class SourceBinding:
    source: Source
    sdk: SDKBackend

    @property
    def key(self) -> str:
        identity = f"{self.source.mode.value}\0{self.source.location}"
        return hashlib.sha256(identity.encode()).hexdigest()[:20]


class TextEntryDialog(ModalScreen[str | None]):
    """Small reusable text-entry modal."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_dialog", "取消", show=False)
    ]

    def __init__(
        self,
        *,
        title: str,
        label: str,
        initial: str,
        placeholder: str,
        submit_label: str,
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.field_label = label
        self.initial = initial
        self.placeholder = placeholder
        self.submit_label = submit_label

    def compose(self) -> ComposeResult:
        with Vertical(id="entry-dialog"):
            yield Label(self.dialog_title, id="entry-title")
            yield Label(self.field_label, id="entry-label")
            yield Input(
                value=self.initial,
                placeholder=self.placeholder,
                select_on_focus=False,
                id="entry-input",
            )
            yield Static("", id="entry-error")
            with Horizontal(id="entry-actions"):
                yield Button("取消", id="entry-cancel")
                yield Button(self.submit_label, variant="primary", id="entry-submit")

    def on_mount(self) -> None:
        field = self.query_one("#entry-input", Input)
        field.focus()
        field.action_end()

    @on(Input.Submitted, "#entry-input")
    def submit_input(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    @on(Button.Pressed, "#entry-submit")
    def submit_button(self) -> None:
        self._submit(self.query_one("#entry-input", Input).value)

    @on(Button.Pressed, "#entry-cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def action_cancel_dialog(self) -> None:
        self.dismiss(None)

    def _submit(self, raw: str) -> None:
        value = raw.strip()
        if not value:
            self.query_one("#entry-error", Static).update("此项不能为空")
            return
        self.dismiss(value)


class OpenAriaTUI(App[None]):
    """Human-primary TUI that discovers both transport modes automatically."""

    CSS_PATH = "openaria.tcss"
    TITLE = "Open Aria"
    SUB_TITLE = "成片导出"
    ENABLE_COMMAND_PALETTE: ClassVar[bool] = False
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [
        (0, "-narrow"),
        (96, "-wide"),
    ]
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "request_quit", "退出"),
        Binding("r", "rescan", "重新扫描"),
        Binding("a", "add_device", "手动连接"),
        Binding("o", "choose_output", "导出目录"),
        Binding("e", "export_selected", "导出", show=False),
    ]

    def __init__(
        self,
        *,
        default_output: Path | None = None,
        sdk_factory: SDKFactory = OpenAriaSDK,
        auto_scan: bool = True,
    ) -> None:
        super().__init__()
        self.export_root = default_output or Path.home() / "OpenAria Exports"
        self._sdk_factory = sdk_factory
        self._auto_scan = auto_scan
        self._generation = 0
        self._session_request = 0
        self._pending_modes: set[SourceMode] = set()
        self._scan_errors: dict[SourceMode, str] = {}
        self._sources_by_key: dict[str, SourceBinding] = {}
        self._option_keys: dict[str, str] = {}
        self._selected_key: str | None = None
        self._sessions: tuple[SessionInfo, ...] = ()
        self._sessions_loading = False
        self._connecting = False
        self._exporting = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False, icon="")
        with Vertical(id="shell"):
            with Horizontal(id="status-bar"):
                yield LoadingIndicator(id="activity")
                yield Static("准备就绪", id="status-message")
            with Horizontal(id="workspace"):
                with Vertical(id="source-pane", classes="pane"):
                    yield Static("尚未扫描", id="source-summary")
                    yield OptionList(id="sources")
                    yield Button("手动连接", id="connect")
                with Vertical(id="sessions-pane", classes="pane"):
                    yield Static("选择一个来源", id="session-summary")
                    yield SelectionList[str](id="sessions")
                    yield Static("", id="unavailable-summary")
            with Horizontal(id="transfer-bar"):
                with Vertical(id="destination"):
                    yield Static("成片保存到", id="destination-label")
                    yield Static("", id="destination-path")
                yield Button("更改目录", id="change-output")
                yield Button("请选择会话", variant="primary", id="export")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#source-pane", Vertical).border_title = "来源"
        self.query_one("#sessions-pane", Vertical).border_title = "会话"
        self.query_one("#activity", LoadingIndicator).display = False
        self._render_destination()
        self._render_export_button()
        if self._auto_scan:
            self.call_after_refresh(self.action_rescan)

    def action_rescan(self) -> None:
        if self._exporting:
            self.notify("导出进行中，完成后才能重新扫描", severity="warning")
            return
        self._generation += 1
        generation = self._generation
        self._session_request += 1
        self._pending_modes = {SourceMode.CARD, SourceMode.LAN}
        self._scan_errors.clear()
        self._sources_by_key.clear()
        self._option_keys.clear()
        self._selected_key = None
        self._sessions = ()
        self._sessions_loading = False
        self.query_one("#sources", OptionList).clear_options()
        self.query_one("#sessions", SelectionList).clear_options()
        self.query_one("#source-summary", Static).update("正在查找...")
        self.query_one("#session-summary", Static).update("等待来源")
        self.query_one("#unavailable-summary", Static).update("")
        self._set_status("正在查找局域网设备和内存卡", busy=True)
        self._render_export_button()
        for mode in (SourceMode.CARD, SourceMode.LAN):
            self.run_worker(
                lambda selected_mode=mode: self._discover_mode(
                    selected_mode, generation
                ),
                name=f"discover-{mode.value}",
                group="discovery",
                exit_on_error=False,
                thread=True,
            )

    def _discover_mode(self, mode: SourceMode, generation: int) -> None:
        try:
            sdk = self._sdk_factory(mode=mode, output=self.export_root)
            bindings = tuple(
                SourceBinding(source=source, sdk=sdk) for source in sdk.discover()
            )
            error = None
        except (OpenAriaError, OSError, ValueError) as caught:
            bindings = ()
            error = str(caught)
        worker = get_current_worker()
        if not worker.is_cancelled:
            self.call_from_thread(
                self._finish_mode_discovery,
                mode,
                generation,
                bindings,
                error,
            )

    def _finish_mode_discovery(
        self,
        mode: SourceMode,
        generation: int,
        bindings: tuple[SourceBinding, ...],
        error: str | None,
    ) -> None:
        if generation != self._generation:
            return
        self._pending_modes.discard(mode)
        if error:
            self._scan_errors[mode] = error
        for binding in bindings:
            self._sources_by_key[binding.key] = binding
        self._render_sources()
        if self._selected_key is None and self._sources_by_key:
            self._select_source(next(iter(self._sources_by_key)))
        if self._pending_modes:
            if self._sources_by_key:
                self._set_status(
                    f"已找到 {len(self._sources_by_key)} 个来源，仍在搜索局域网",
                    busy=True,
                )
            return
        if not self._sources_by_key:
            self.query_one("#source-summary", Static).update("未找到来源")
            self.query_one("#session-summary", Static).update("没有可显示的会话")
            self._set_status("未找到设备；请检查网络、插入内存卡或手动连接")
            self._render_export_button()
        elif not self._sessions_loading:
            self._set_status(f"已找到 {len(self._sources_by_key)} 个来源")
            if self._sessions:
                self.call_after_refresh(
                    self.query_one("#sessions", SelectionList).focus
                )

    def _render_sources(self) -> None:
        option_list = self.query_one("#sources", OptionList)
        session_list = self.query_one("#sessions", SelectionList)
        restore_session_focus = self.focused is session_list
        option_list.clear_options()
        self._option_keys.clear()
        ordered = sorted(
            self._sources_by_key.values(),
            key=lambda item: (
                0 if item.source.mode is SourceMode.LAN else 1,
                item.source.display_name.casefold(),
                item.source.location,
            ),
        )
        highlighted = 0
        options: list[Option] = []
        for index, binding in enumerate(ordered):
            option_id = f"source-{binding.key}"
            self._option_keys[option_id] = binding.key
            if binding.key == self._selected_key:
                highlighted = index
            options.append(Option(_source_prompt(binding.source), id=option_id))
        option_list.add_options(options)
        if options:
            option_list.highlighted = highlighted
        if restore_session_focus:
            session_list.focus()
        pending = " · 搜索中" if self._pending_modes else ""
        self.query_one("#source-summary", Static).update(
            f"{len(options)} 个来源{pending}"
        )

    @on(OptionList.OptionHighlighted, "#sources")
    def source_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is None:
            return
        key = self._option_keys.get(event.option.id)
        if key is not None:
            self._select_source(key)

    @on(OptionList.OptionSelected, "#sources")
    def source_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        key = self._option_keys.get(event.option.id)
        if key is not None:
            self._select_source(key)

    def _select_source(self, key: str) -> None:
        if key not in self._sources_by_key or key == self._selected_key:
            return
        self._selected_key = key
        self._sessions = ()
        self._session_request += 1
        request = self._session_request
        binding = self._sources_by_key[key]
        self._sessions_loading = True
        self.query_one("#sessions", SelectionList).clear_options()
        self.query_one("#session-summary", Static).update(
            f"正在读取 {binding.source.display_name}"
        )
        self.query_one("#unavailable-summary", Static).update("")
        self._set_status("正在读取会话目录", busy=True)
        self._render_export_button()
        self.run_worker(
            lambda: self._load_sessions(binding, request),
            name="load-sessions",
            group="sessions",
            exclusive=True,
            exit_on_error=False,
            thread=True,
        )

    def _load_sessions(self, binding: SourceBinding, request: int) -> None:
        try:
            sessions = binding.sdk.list_sessions(binding.source)
            error = None
        except (OpenAriaError, OSError, ValueError) as caught:
            sessions = ()
            error = str(caught)
        worker = get_current_worker()
        if not worker.is_cancelled:
            self.call_from_thread(
                self._finish_session_load,
                binding.key,
                request,
                sessions,
                error,
            )

    def _finish_session_load(
        self,
        key: str,
        request: int,
        sessions: tuple[SessionInfo, ...],
        error: str | None,
    ) -> None:
        if request != self._session_request or key != self._selected_key:
            return
        self._sessions_loading = False
        self._sessions = sessions
        selection_list = self.query_one("#sessions", SelectionList)
        selection_list.clear_options()
        if error:
            self.query_one("#session-summary", Static).update("会话读取失败")
            self._set_status(f"无法读取会话：{error}", error=True)
            self._render_export_button()
            return
        selections = [
            Selection(
                _session_prompt(session),
                session.session_id,
                True,
                id=f"session-{index}",
            )
            for index, session in enumerate(sessions)
            if session.exportable
        ]
        selection_list.add_options(selections)
        if selections:
            selection_list.highlighted = 0
        usable = sum(session.exportable for session in sessions)
        unavailable_sessions = tuple(
            session for session in sessions if not session.exportable
        )
        self.query_one("#session-summary", Static).update(
            f"{usable} 个可导出 · {len(sessions)} 个会话"
        )
        if len(unavailable_sessions) == 1:
            unavailable = unavailable_sessions[0]
            self.query_one("#unavailable-summary", Static).update(
                f"已排除：{unavailable.display_name} · {_unavailable_text(unavailable)}"
            )
        elif unavailable_sessions:
            self.query_one("#unavailable-summary", Static).update(
                f"已自动排除 {len(unavailable_sessions)} 个未通过机身校验的会话"
            )
        else:
            self.query_one("#unavailable-summary", Static).update("")
        if usable:
            self._set_status("已自动选择全部可导出会话")
            selection_list.focus()
        else:
            self._set_status("此来源没有可导出的会话")
        self._render_export_button()

    @on(SelectionList.SelectedChanged, "#sessions")
    def selected_sessions_changed(self) -> None:
        self._render_export_button()

    @on(Button.Pressed, "#connect")
    def connect_button(self) -> None:
        self.action_add_device()

    def action_add_device(self) -> None:
        if self._exporting or self._connecting:
            return
        self.push_screen(
            TextEntryDialog(
                title="手动连接",
                label="机身地址",
                initial="",
                placeholder="192.168.110.36",
                submit_label="连接",
            ),
            self._manual_address_entered,
        )

    def _manual_address_entered(self, endpoint: str | None) -> None:
        if endpoint is None:
            return
        self._connecting = True
        generation = self._generation
        self._set_status(f"正在连接 {endpoint}", busy=True)
        self._update_controls()
        self.run_worker(
            lambda: self._connect_manual(endpoint, generation),
            name="manual-connect",
            group="manual-connect",
            exclusive=True,
            exit_on_error=False,
            thread=True,
        )

    def _connect_manual(self, endpoint: str, generation: int) -> None:
        try:
            sdk = self._sdk_factory(
                mode=SourceMode.LAN,
                endpoint=endpoint,
                output=self.export_root,
            )
            bindings = tuple(
                SourceBinding(source=source, sdk=sdk) for source in sdk.discover()
            )
            error = None
        except (OpenAriaError, OSError, ValueError) as caught:
            bindings = ()
            error = str(caught)
        worker = get_current_worker()
        if not worker.is_cancelled:
            self.call_from_thread(
                self._finish_manual_connect,
                generation,
                bindings,
                error,
            )

    def _finish_manual_connect(
        self,
        generation: int,
        bindings: tuple[SourceBinding, ...],
        error: str | None,
    ) -> None:
        self._connecting = False
        if generation != self._generation:
            return
        if error or not bindings:
            self._set_status(
                f"连接失败：{error or '未识别到 Open Aria 设备'}", error=True
            )
            self.notify("无法连接该机身地址", severity="error")
            self._update_controls()
            return
        for binding in bindings:
            self._sources_by_key[binding.key] = binding
        selected = bindings[0]
        self._selected_key = selected.key
        self._render_sources()
        self._selected_key = None
        self._select_source(selected.key)
        self.notify(f"已连接 {selected.source.display_name}")

    @on(Button.Pressed, "#change-output")
    def output_button(self) -> None:
        self.action_choose_output()

    def action_choose_output(self) -> None:
        if self._exporting:
            return
        self.push_screen(
            TextEntryDialog(
                title="导出目录",
                label="本机路径",
                initial=str(self.export_root),
                placeholder=str(Path.home() / "OpenAria Exports"),
                submit_label="使用此目录",
            ),
            self._output_entered,
        )

    def _output_entered(self, value: str | None) -> None:
        if value is None:
            return
        candidate = Path(value).expanduser()
        binding = self._selected_binding()
        if binding is not None and _path_is_on_source_card(candidate, binding.source):
            self.notify("导出目录不能位于源内存卡上", severity="error")
            return
        self.export_root = candidate
        self._render_destination()
        self._render_export_button()

    @on(Button.Pressed, "#export")
    def export_button(self) -> None:
        self.action_export_selected()

    def action_export_selected(self) -> None:
        if self._exporting or self._sessions_loading:
            return
        binding = self._selected_binding()
        if binding is None:
            return
        session_ids = tuple(self.query_one("#sessions", SelectionList).selected)
        if not session_ids:
            self.notify("请至少选择一个会话", severity="warning")
            return
        if _path_is_on_source_card(self.export_root, binding.source):
            self.notify("导出目录不能位于源内存卡上", severity="error")
            return
        selected_ids = set(session_ids)
        total_bytes = sum(
            session.total_bytes
            for session in self._sessions
            if session.session_id in selected_ids
        )
        free_bytes = _free_bytes(self.export_root)
        required_bytes = _required_export_bytes(total_bytes)
        if free_bytes is not None and required_bytes > free_bytes:
            self.notify("导出目录可用空间不足", severity="error")
            self._set_status(
                f"处理期间需要约 {_human_bytes(required_bytes)}，仅剩 "
                f"{_human_bytes(free_bytes)}",
                error=True,
            )
            return
        self._exporting = True
        self._set_status("正在准备下载并生成成片", busy=True)
        self._update_controls()
        self.run_worker(
            lambda: self._run_export(binding, session_ids),
            name="export",
            group="export",
            exclusive=True,
            exit_on_error=False,
            thread=True,
        )

    def _run_export(self, binding: SourceBinding, session_ids: tuple[str, ...]) -> None:
        worker = get_current_worker()

        def progress(message: str) -> None:
            if not worker.is_cancelled:
                self.call_from_thread(self._show_export_progress, message)

        try:
            result = binding.sdk.export(
                source=binding.source,
                session_ids=session_ids,
                output=self.export_root,
                progress=progress,
            )
            error = None
        except (OpenAriaError, OSError, ValueError) as caught:
            result = None
            error = str(caught)
        if not worker.is_cancelled:
            self.call_from_thread(self._finish_export, result, error)

    def _show_export_progress(self, message: str) -> None:
        self._set_status(message, busy=True)

    def _finish_export(self, result: ExportResult | None, error: str | None) -> None:
        self._exporting = False
        if error or result is None:
            self._set_status(f"导出失败：{error or '未知错误'}", error=True)
            self.notify("导出失败，未发布不完整会话", severity="error")
            self._update_controls()
            return
        reused = sum(session.reused for session in result.sessions)
        detail = f" · 复用 {reused} 个" if reused else ""
        self._set_status(
            f"成片导出完成：{result.exported_count} 个会话 · "
            f"{_human_bytes(result.total_bytes)}{detail}"
        )
        self.notify(f"成片已保存到 {result.output_root}", timeout=6)
        self._render_destination()
        self._update_controls()

    def action_request_quit(self) -> None:
        if self._exporting:
            self.notify("导出进行中，请等待当前任务完成", severity="warning")
            return
        self.exit()

    def _selected_binding(self) -> SourceBinding | None:
        if self._selected_key is None:
            return None
        return self._sources_by_key.get(self._selected_key)

    def _set_status(
        self, message: str, *, busy: bool = False, error: bool = False
    ) -> None:
        status = self.query_one("#status-message", Static)
        status.update(message)
        status.set_class(error, "error")
        self.query_one("#activity", LoadingIndicator).display = busy

    def _render_destination(self) -> None:
        free = _free_bytes(self.export_root)
        suffix = f" · 可用 {_human_bytes(free)}" if free is not None else ""
        self.query_one("#destination-path", Static).update(
            Text.assemble((str(self.export_root), "bold"), (suffix, "dim"))
        )

    def _render_export_button(self) -> None:
        button = self.query_one("#export", Button)
        if self._exporting:
            button.label = "正在生成成片..."
            button.disabled = True
            return
        selected = set(self.query_one("#sessions", SelectionList).selected)
        total = sum(
            session.total_bytes
            for session in self._sessions
            if session.session_id in selected
        )
        if selected:
            button.label = f"生成 {len(selected)} 个成片 · {_human_bytes(total)}"
        else:
            button.label = "请选择会话"
        button.disabled = not selected or self._sessions_loading or self._connecting

    def _update_controls(self) -> None:
        locked = self._exporting or self._connecting
        self.query_one("#sources", OptionList).disabled = locked
        self.query_one("#sessions", SelectionList).disabled = locked
        self.query_one("#connect", Button).disabled = locked
        self.query_one("#change-output", Button).disabled = self._exporting
        self._render_export_button()


def _source_prompt(source: Source) -> Text:
    mode_label = "局域网" if source.mode is SourceMode.LAN else "内存卡"
    mode_style = "bold #62b58d" if source.mode is SourceMode.LAN else "bold #d9aa52"
    return Text.assemble(
        (mode_label, mode_style),
        "  ",
        (source.display_name, "bold"),
        "\n",
        (_short_location(source), "dim"),
    )


def _session_prompt(session: SessionInfo) -> Text:
    started = session.started_at.replace("T", " ")[:16]
    status = "可导出" if session.exportable else _unavailable_text(session)
    status_style = "#62b58d" if session.exportable else "#c27a7a"
    return Text.assemble(
        (session.display_name, "bold"),
        "  ",
        (_human_bytes(session.total_bytes), "#9ba5ad"),
        "  ",
        (started, "#9ba5ad"),
        "  ",
        (status, status_style),
    )


def _unavailable_text(session: SessionInfo) -> str:
    reason = session.unavailable_reason or "不可用"
    translations = {
        "gateway verification is missing": "等待机身校验",
        "verification actor is not the gateway": "校验来源不受支持",
        "gateway verification has no valid manifest digest": "校验摘要无效",
        "gateway marked the session unusable": "机身标记为不可用",
    }
    return translations.get(reason, reason)


def _short_location(source: Source) -> str:
    if source.mode is SourceMode.CARD:
        return source.location
    parsed = urllib.parse.urlsplit(source.api_base or source.location)
    return parsed.netloc or source.location


def _path_is_on_source_card(path: Path, source: Source) -> bool:
    if source.mode is not SourceMode.CARD or source.card_root is None:
        return False
    candidate = path.expanduser().resolve()
    card_root = source.card_root.resolve()
    return candidate == card_root or candidate.is_relative_to(card_root)


def _free_bytes(path: Path) -> int | None:
    candidate = path.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return None


def _human_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _required_export_bytes(source_bytes: int) -> int:
    # Rendering briefly keeps verified source bytes and the final MP4 together.
    return source_bytes * 2
