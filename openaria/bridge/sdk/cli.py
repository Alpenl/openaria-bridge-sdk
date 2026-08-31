"""Command-line entry point for the integrated SDK."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import OpenAriaSDK
from .errors import MultipleSourcesError, OpenAriaError
from .models import SessionInfo, Source, SourceMode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openaria-bridge",
        description=(
            "Discover an Open Aria device on the LAN or a mounted recording card, "
            "then export verified session data."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in SourceMode),
        default=SourceMode.LAN.value,
        help="source mode (default: lan)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("openaria-export"),
        help="export directory (default: ./openaria-export)",
    )
    parser.add_argument(
        "--endpoint",
        help="manual Device API host or URL; LAN discovery remains the default",
    )
    parser.add_argument(
        "--card",
        type=Path,
        help="mounted card path; card mode normally discovers it automatically",
    )
    parser.add_argument(
        "--device",
        help="device id, label, or discovered location to select",
    )
    parser.add_argument(
        "--session",
        action="append",
        dest="session_ids",
        help="export only this session id; repeat for multiple sessions",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=3.0,
        help="seconds to browse mDNS (default: 3)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=15.0,
        help="seconds allowed for each Device API operation (default: 15)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the discovered sessions without exporting",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="export without an interactive confirmation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == SourceMode.LAN.value and args.card is not None:
        parser.error("--card requires --mode card")
    if args.mode == SourceMode.CARD.value and args.endpoint is not None:
        parser.error("--endpoint is only valid in LAN mode")
    try:
        sdk = OpenAriaSDK(
            mode=args.mode,
            output=args.output,
            endpoint=args.endpoint,
            card=args.card,
            device=args.device,
            discovery_timeout=args.discovery_timeout,
            request_timeout=args.request_timeout,
        )
        sources = sdk.discover()
        source = _choose_source(sources, assume_yes=args.yes, selector=args.device)
        sessions = sdk.list_sessions(source)
        _print_source(source)
        _print_sessions(sessions)
        if args.list:
            return 0
        selected = _selected_sessions(sessions, args.session_ids)
        unavailable = tuple(session for session in selected if not session.exportable)
        if args.session_ids is not None and unavailable:
            detail = ", ".join(
                f"{session.session_id} ({session.unavailable_reason})"
                for session in unavailable
            )
            raise OpenAriaError(f"requested session(s) are not exportable: {detail}")
        available = tuple(session for session in selected if session.exportable)
        if not available:
            print("Nothing to export: no usable sealed sessions found.")
            return 0
        if not args.yes and not _confirm_export(available, args.output):
            print("Export cancelled.")
            return 0
        result = sdk.export(
            source=source,
            session_ids=args.session_ids,
            progress=lambda message: print(f"  {message}"),
        )
    except MultipleSourcesError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (OpenAriaError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    reused = sum(session.reused for session in result.sessions)
    print(
        f"Done: {result.exported_count} session(s), {_human_bytes(result.total_bytes)} "
        f"under {result.output_root}"
    )
    if reused:
        print(f"Reused {reused} already verified session export(s).")
    if result.unavailable_sessions:
        print(
            f"Skipped {len(result.unavailable_sessions)} session(s) that the gateway "
            "did not mark usable."
        )
    return 0


def _choose_source(
    sources: tuple[Source, ...], *, assume_yes: bool, selector: str | None
) -> Source:
    if selector:
        folded = selector.casefold()
        matches = tuple(
            source
            for source in sources
            if folded
            in {
                source.device_id.casefold(),
                source.device_label.casefold(),
                source.location.casefold(),
            }
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise OpenAriaError(f"no source matches {selector!r}")
        sources = matches
    if len(sources) == 1:
        return sources[0]
    if assume_yes or not sys.stdin.isatty():
        raise MultipleSourcesError([source.location for source in sources])
    print("Multiple Open Aria sources found:")
    for index, source in enumerate(sources, start=1):
        print(f"  {index}. {source.display_name}  {source.location}")
    while True:
        answer = input(f"Select a source [1-{len(sources)}]: ").strip()
        try:
            selected = int(answer)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(sources):
            return sources[selected - 1]
        print("Enter one of the listed numbers.")


def _selected_sessions(
    sessions: tuple[SessionInfo, ...], requested: list[str] | None
) -> tuple[SessionInfo, ...]:
    if requested is None:
        return sessions
    requested_set = set(requested)
    known = {session.session_id for session in sessions}
    unknown = sorted(requested_set - known)
    if unknown:
        raise OpenAriaError("unknown session id(s): " + ", ".join(unknown))
    return tuple(session for session in sessions if session.session_id in requested_set)


def _print_source(source: Source) -> None:
    print(f"Source:   {source.display_name}")
    print(f"Mode:     {source.mode.value}")
    print(f"Location: {source.location}")


def _print_sessions(sessions: tuple[SessionInfo, ...]) -> None:
    available = sum(session.exportable for session in sessions)
    print(f"Sessions: {available} usable / {len(sessions)} total")
    for session in sessions:
        status = (
            "ready"
            if session.exportable
            else f"unavailable: {session.unavailable_reason}"
        )
        print(
            f"  {session.session_id}  {_human_bytes(session.total_bytes):>9}  "
            f"{session.started_at}  {status}"
        )


def _confirm_export(sessions: tuple[SessionInfo, ...], output: Path) -> bool:
    if not sys.stdin.isatty():
        raise OpenAriaError("non-interactive export requires --yes")
    total = sum(session.total_bytes for session in sessions)
    answer = input(
        f"Export {len(sessions)} session(s), {_human_bytes(total)}, to {output}? [Y/n] "
    ).strip()
    return answer.casefold() in {"", "y", "yes"}


def _human_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
