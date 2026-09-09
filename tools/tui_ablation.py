"""Run against baseline 1bc290c before rewriting the TUI."""

import asyncio
import json
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path.cwd()))

from textual.widgets import OptionList, SelectionList

from openaria.bridge.sdk.tui import OpenAriaTUI, SourceBinding

OpenAriaTUI.CSS_PATH = Path("openaria/bridge/sdk/openaria.tcss").resolve()

fixtures = runpy.run_path("tests/test_tui.py")
Factory = fixtures["FakeSDKFactory"]
SDK = fixtures["FakeSDK"]
wait_for = fixtures["_wait_for"]


class FlatTUI(OpenAriaTUI):
    CSS = """
    #source-pane { display: none; }
    .pane { border: none; padding: 0; background: transparent; }
    #workspace { padding: 0 2; }
    #unavailable-summary { height: 1; }
    """


class ExplicitTUI(OpenAriaTUI):
    async def _on_message(self, message):
        if (
            isinstance(message, OptionList.OptionHighlighted)
            and message.option_list.id == "sources"
        ):
            return
        await super()._on_message(message)


class TrackingSDK(SDK):
    def list_sessions(self, source=None, *, refresh=False):
        self.factory.reads += 1
        return super().list_sessions(source, refresh=refresh)


class TrackingFactory(Factory):
    reads = 0

    def __call__(self, *, mode, output, endpoint=None, **kwargs):
        from openaria.bridge.sdk import SourceMode

        return TrackingSDK(self, SourceMode(mode), endpoint)


async def layout(app_type, size, root):
    app = app_type(default_output=root, sdk_factory=Factory(root / "card"))
    async with app.run_test(size=size) as pilot:
        await wait_for(
            pilot,
            lambda: (
                len(app._sources_by_key) == 2
                and not app._pending_modes
                and not app._sessions_loading
            ),
        )
        await pilot.pause()
        widget = app.query_one("#sessions", SelectionList)
        app.save_screenshot(
            f"{app_type.__name__}-{size[0]}x{size[1]}.svg",
            path="/tmp/openaria-tui-before",
        )
        return {
            "width": widget.content_size.width,
            "height": widget.content_size.height,
        }


async def navigation(app_type, root):
    factory = TrackingFactory(root / "card")
    app = app_type(default_output=root, sdk_factory=factory)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(
            pilot,
            lambda: (
                len(app._sources_by_key) == 2
                and not app._pending_modes
                and not app._sessions_loading
            ),
        )
        app.query_one("#sources", OptionList).focus()
        before = factory.reads
        source_list = app.query_one("#sources", OptionList)
        source_list.highlighted = (
            1 if app._selected_binding().source.mode.value == "lan" else 0
        )
        await pilot.pause(0.1)
        return {"initial_reads": before, "reads_after_cursor_down": factory.reads}


def main():
    Path("/tmp/openaria-tui-before").mkdir(exist_ok=True)
    with TemporaryDirectory() as name:
        root = Path(name)
        results = {
            "baseline_commit": "1bc290c24553b1dba2b0d8c58b43ab8ce6275a03",
            "layout": {},
        }
        for size in ((120, 36), (80, 24)):
            results["layout"][str(size)] = {
                kind.__name__: asyncio.run(layout(kind, size, root))
                for kind in (OpenAriaTUI, FlatTUI)
            }
        results["source_navigation"] = {
            kind.__name__: asyncio.run(navigation(kind, root))
            for kind in (OpenAriaTUI, ExplicitTUI)
        }
        original_key = SourceBinding.key
        SourceBinding.key = property(
            lambda self: f"{self.source.mode.value}:{self.source.location}"
        )
        try:
            for test in (
                "test_tui_discovers_both_modes_preselects_and_exports",
                "test_tui_manual_address_recovers_when_discovery_finds_nothing",
                "test_tui_narrow_layout_keeps_primary_controls_on_screen",
            ):
                with TemporaryDirectory() as case:
                    fixtures[test](Path(case))
            results["without_source_hash"] = "3 existing TUI scenarios passed"
        finally:
            SourceBinding.key = original_key
        report = json.dumps(results, indent=2)
        Path("/tmp/openaria-tui-before/results.json").write_text(report)
        print(report)


if __name__ == "__main__":
    main()
