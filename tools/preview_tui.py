"""Interactive TUI demo and reproducible captures; never reads devices or writes exports."""

from __future__ import annotations

import argparse
import asyncio
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.cells import cell_len
from textual.widgets import Input, Static

from openaria.bridge.sdk import (
    DeleteResult,
    ExportedSession,
    ExportResult,
    SessionInfo,
    Source,
    SourceMode,
)
from openaria.bridge.sdk.tui import OpenAriaTUI


class PreviewSDK:
    def __init__(self, *, mode, output, endpoint=None, **kwargs):
        self.mode = SourceMode(mode)
        self.output = Path(output)
        self.deleted: set[str] = set()
        self.source = Source(
            mode=self.mode,
            location="http://192.168.1.108:8080/api/v4"
            if self.mode is SourceMode.LAN
            else "/media/OPENARIA",
            device_id="YLX-30D5872D",
            device_label="Open Aria · Studio"
            if self.mode is SourceMode.LAN
            else "OPENARIA SD",
            card_root=Path("/media/OPENARIA") if self.mode is SourceMode.CARD else None,
        )

    def discover(self, **kwargs):
        time.sleep(0.08 if self.mode is SourceMode.LAN else 0.2)
        return (self.source,)

    def list_sessions(self, source=None, **kwargs):
        names = (
            "工作室 / 双目校准",
            "河畔步行",
            "城市骑行 · 第一段",
            "城市骑行 · 第二段",
            "室内低照度",
            "公园长镜头",
            "立体声采样",
            "傍晚延时",
            "设备移动测试",
            "街景 / 路口",
            "校验中的录制",
            "中断的录制",
        )
        start = datetime(2026, 9, 8, 10, 42, tzinfo=timezone(timedelta(hours=8)))
        return tuple(
            SessionInfo(
                session_id=f"20260908-{index + 1:04}",
                display_name=name,
                started_at=(start - timedelta(minutes=index * 18)).isoformat(),
                duration_seconds=97 + index * 31,
                total_bytes=(148 + index * 27) * 1024 * 1024,
                manifest_sha256="a" * 64 if index < 10 else "",
                exportable=index < 10,
                unavailable_reason="gateway verification is missing"
                if index == 10
                else "gateway marked the session unusable",
            )
            for index, name in enumerate(names)
            if f"20260908-{index + 1:04}" not in self.deleted
        )

    def delete_sessions(self, *, source, session_ids, expected_manifests=None):
        self.deleted.update(session_ids)
        return DeleteResult(source, session_ids)

    def export(self, *, source, session_ids, output, progress, continue_on_error=False):
        exported = []
        for session_id in session_ids:
            for phase in ("正在读取并校验源文件", "正在生成双目成片", "成片校验完成"):
                progress(f"{session_id}: {phase}")
                time.sleep(0.12)
            path = Path(output) / source.device_id / session_id
            exported.append(
                ExportedSession(
                    session_id=session_id,
                    path=path,
                    artifact_count=4,
                    total_bytes=160 * 1024 * 1024,
                    media_path=path / "recording.mp4",
                    media_bytes=112 * 1024 * 1024,
                )
            )
        return ExportResult(
            source=source, output_root=Path(output), sessions=tuple(exported)
        )


class PreviewTUI(OpenAriaTUI):
    TITLE = "Open Aria Bridge · Demo"

    def __init__(self, **kwargs):
        self._demo_state = TemporaryDirectory(prefix="openaria-demo-")
        kwargs.setdefault(
            "history_path", Path(self._demo_state.name) / "history.sqlite3"
        )
        super().__init__(**kwargs)

    def on_mount(self):
        # Textual dispatches Mount to each class in the MRO.
        self.query_one("#subtitle", Static).update("BRIDGE  /  演示数据")


def save_capture(app: OpenAriaTUI, path: Path) -> None:
    root = ET.fromstring(app.export_screenshot())
    # Rich's SVG textLength uses codepoint count; terminals allocate two cells to CJK.
    for node in root.iter("{http://www.w3.org/2000/svg}text"):
        text = node.text or ""
        if text and "textLength" in node.attrib:
            cell_width = float(node.attrib["textLength"]) / len(text)
            node.set("textLength", str(cell_width * cell_len(text)))
            node.set("lengthAdjust", "spacingAndGlyphs")
            node.set(
                "style",
                "font-family: 'Noto Sans Mono CJK SC', monospace; font-variant-east-asian: normal; font-variant-ligatures: none",
            )
    path.write_bytes(ET.tostring(root, encoding="utf-8"))


async def capture(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for width, height in ((120, 36), (80, 24), (48, 18)):
        app = PreviewTUI(sdk_factory=PreviewSDK)
        async with app.run_test(size=(width, height)) as pilot:
            await app.workers.wait_for_complete()
            for _ in range(100):
                if len(app._sources) == 2 and not app._sessions_loading:
                    break
                await pilot.pause(0.02)
            await pilot.pause()
            save_capture(app, directory / f"recordings-{width}x{height}.svg")
            print(f"{width}x{height}: {app.query_one('#sessions').content_size}")
            if width == 120:
                app.action_export_selected()
                await pilot.pause(0.3)
                save_capture(app, directory / "export-active.svg")
                await app.workers.wait_for_complete()
                await pilot.pause()
                save_capture(app, directory / "export-complete.svg")
            if width == 80:
                app.action_choose_output()
                await pilot.pause()
                save_capture(app, directory / "destination-dialog.svg")
                await pilot.press("escape")
                app.query_one("#filter", Input).value = "no matches"
                await pilot.pause()
                save_capture(app, directory / "filtered-empty.svg")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture", type=Path, help="save SVG captures instead of opening a terminal"
    )
    args = parser.parse_args()
    if args.capture:
        asyncio.run(capture(args.capture))
    else:
        PreviewTUI(sdk_factory=PreviewSDK).run()


if __name__ == "__main__":
    main()
