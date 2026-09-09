"""Presentation helpers for the recording workbench."""

from __future__ import annotations

import shutil
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from .errors import OpenAriaError
from .models import SessionInfo, Source, SourceMode
from .options import ExportOptions


class TextEntryDialog(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_dialog", "取消", show=False)
    ]

    def __init__(
        self,
        *,
        title: str,
        label: str,
        initial: str = "",
        placeholder: str = "",
        submit_label: str,
        validate: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.field_label = label
        self.initial = initial
        self.placeholder = placeholder
        self.submit_label = submit_label
        self.validate_value = validate

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
            yield Static("", id="entry-error", markup=False)
            with Horizontal(id="entry-actions"):
                yield Button("取消", id="entry-cancel", flat=True)
                yield Button(
                    self.submit_label, variant="primary", id="entry-submit", flat=True
                )

    def on_mount(self) -> None:
        field = self.query_one(Input)
        field.focus()
        field.action_end()

    @on(Input.Submitted)
    @on(Button.Pressed, "#entry-submit")
    def submit(self) -> None:
        value = self.query_one(Input).value.strip()
        error = (
            "此项不能为空"
            if not value
            else (self.validate_value(value) if self.validate_value else None)
        )
        if error:
            self.query_one("#entry-error", Static).update(error)
            self.query_one(Input).focus()
        else:
            self.dismiss(value)

    @on(Button.Pressed, "#entry-cancel")
    def action_cancel_dialog(self) -> None:
        self.dismiss(None)


class DeleteDialog(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_dialog", "取消", show=False)
    ]

    def __init__(self, description: str) -> None:
        super().__init__()
        self.description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Label("删除源录制", id="delete-title")
            with VerticalScroll(id="delete-body"):
                yield Static(self.description, id="delete-description", markup=False)
            yield Static(
                "源录制将永久删除，无法撤销。本机成片保留。", id="delete-warning"
            )
            with Horizontal(id="delete-actions"):
                yield Button("取消", id="delete-cancel", flat=True)
                yield Button(
                    "永久删除", variant="error", id="delete-confirm", flat=True
                )

    def on_mount(self) -> None:
        self.query_one("#delete-cancel", Button).focus()

    @on(Button.Pressed, "#delete-cancel")
    def action_cancel_dialog(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#delete-confirm")
    def confirm(self) -> None:
        self.dismiss(True)


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def duration(seconds: float) -> str:
    minutes, second = divmod(max(0, int(seconds)), 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour}:{minute:02}:{second:02}" if hour else f"{minute:02}:{second:02}"


def started_at(session: SessionInfo) -> str:
    try:
        return datetime.fromisoformat(session.started_at).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return session.started_at


def unavailable_text(session: SessionInfo) -> str:
    reason = session.unavailable_reason or "不可用"
    return {
        "gateway verification is missing": "等待机身校验",
        "verification actor is not the gateway": "校验来源不受支持",
        "gateway verification has no valid manifest digest": "校验摘要无效",
        "gateway marked the session unusable": "机身标记为不可用",
    }.get(reason, reason)


def source_label(source: Source) -> Text:
    mode = "局域网" if source.mode is SourceMode.LAN else "内存卡"
    location = source.location
    if source.mode is SourceMode.LAN:
        location = urllib.parse.urlsplit(source.api_base or location).netloc or location
    return Text.assemble(
        (mode, "#8cc9bd" if source.mode is SourceMode.LAN else "#d7bd7b"),
        "  ",
        (source.display_name, "bold"),
        (f"  {location}", "#929596"),
    )


def session_label(session: SessionInfo, width: int, *, exported: bool = False) -> Text:
    if not session.exportable:
        return Text.assemble(
            (session.display_name, "#929596"),
            (f"  · {unavailable_text(session)}", "#d7bd7b"),
        )
    metadata = f"  {duration(session.duration_seconds):>8}  {human_bytes(session.total_bytes):>10}"
    if width >= 88:
        metadata += f"  {started_at(session)}"
    name = Text(session.display_name or session.session_id, style="bold")
    prefix = Text("已导出 ", style="#8cc9bd") if exported else Text()
    name_width = max(8, width - Text(metadata).cell_len - prefix.cell_len)
    name.truncate(name_width, overflow="ellipsis", pad=True)
    return prefix.append(name).append(metadata, style="#929596")


def path_is_on_source_card(path: Path, source: Source) -> bool:
    if source.mode is not SourceMode.CARD or source.card_root is None:
        return False
    return path.expanduser().resolve().is_relative_to(source.card_root.resolve())


def existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(existing_parent(path)).free
    except (OSError, ValueError):
        return None


class ExportSettingsDialog(ModalScreen[ExportOptions | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "取消", show=False)
    ]

    def __init__(self, options: ExportOptions) -> None:
        super().__init__()
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="entry-dialog"):
            yield Label("导出设置")
            yield Select(
                [("H.264 · 常规成片", "h264"), ("H.265 · 较小归档，生成更慢", "hevc")],
                value=self.options.video_codec,
                allow_blank=False,
                id="export-codec",
            )
            yield Label("音频延后 (ms)，仅用于已标定的会话；默认 0")
            yield Input(
                str(self.options.audio_calibration_seconds * 1000),
                id="export-delay",
                type="number",
            )
            yield Checkbox(
                "保留原始视频和音频",
                value=self.options.retain_sources,
                id="export-retain",
            )
            yield Static("", id="entry-error")
            with Horizontal(id="entry-actions"):
                yield Button("取消", id="settings-cancel")
                yield Button("应用", id="settings-apply", variant="primary")

    @on(Button.Pressed, "#settings-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#settings-apply")
    def apply_settings(self) -> None:
        try:
            delay = float(self.query_one("#export-delay", Input).value)
            if not -1000 <= delay <= 1000:
                raise ValueError("音频补偿范围为 -1000 至 1000 ms")
            options = ExportOptions(
                video_codec=str(self.query_one("#export-codec", Select).value),
                audio_calibration_seconds=delay / 1000,
                retain_sources=self.query_one("#export-retain", Checkbox).value,
            )
        except (ValueError, OpenAriaError) as error:
            self.query_one("#entry-error", Static).update(str(error))
            return
        self.dismiss(options)
