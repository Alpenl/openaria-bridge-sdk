"""Minimal command entry point for the Open Aria TUI."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys

from .tui import OpenAriaTUI


def _version() -> str:
    try:
        return importlib.metadata.version("ylx-card-pipeline")
    except importlib.metadata.PackageNotFoundError:
        return "0.3.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openaria-bridge",
        description=(
            "Open the automatic Open Aria exporter. It discovers LAN devices and "
            "mounted recording cards without setup."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "error: openaria-bridge needs an interactive terminal; "
            "use openaria.bridge.sdk.OpenAriaSDK for automation",
            file=sys.stderr,
        )
        return 2
    OpenAriaTUI().run()
    return 0
