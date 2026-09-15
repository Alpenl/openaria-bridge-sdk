"""One-call public API for LAN and recording-card exports."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from ._card import (
    CardInventory,
    delete_card_sessions,
    discover_card_inventories,
    export_card_session,
)
from ._lan import (
    DeviceApiClient,
    discover_mdns_endpoints,
    probe_lan_sources,
)
from .errors import (
    DeleteError,
    DiscoveryError,
    ExportError,
    MultipleSourcesError,
    OpenAriaError,
)
from .models import (
    DeleteResult,
    ExportedSession,
    ExportFailure,
    ExportResult,
    SessionInfo,
    Source,
    SourceMode,
)
from .options import ExportOptions


class OpenAriaSDK:
    """Discover one Open Aria source and export its sealed sessions.

    ``lan`` is the default product path. ``card`` discovers a mounted recording
    card without requiring its mount path. Both modes write the same verified,
    synchronized final-video output and return the same result models.
    """

    def __init__(
        self,
        *,
        mode: SourceMode | str = SourceMode.LAN,
        output: Path | str = Path("openaria-export"),
        endpoint: str | None = None,
        card: Path | str | None = None,
        device: str | None = None,
        token: str | None = None,
        discovery_timeout: float = 3.0,
        request_timeout: float = 15.0,
        card_search_roots: Iterable[Path] | None = None,
        discovery_provider: Callable[[float], Sequence[str]] | None = None,
    ) -> None:
        self.mode = SourceMode(mode)
        if discovery_timeout <= 0 or request_timeout <= 0:
            raise ValueError("discovery and request timeouts must be positive")
        if self.mode is SourceMode.LAN and card is not None:
            raise ValueError("card path is only valid in card mode")
        if self.mode is SourceMode.CARD and endpoint is not None:
            raise ValueError("endpoint is only valid in LAN mode")
        self.output = Path(output).expanduser()
        self.endpoint = endpoint
        self.card = Path(card).expanduser() if card is not None else None
        self.device = device
        self.token = (
            token if token is not None else os.environ.get("OPENARIA_DEVICE_TOKEN")
        )
        self.discovery_timeout = discovery_timeout
        self.request_timeout = request_timeout
        self.card_search_roots = (
            tuple(Path(path).expanduser() for path in card_search_roots)
            if card_search_roots is not None
            else None
        )
        self._discovery_provider = discovery_provider or discover_mdns_endpoints
        self._sources: tuple[Source, ...] | None = None
        self._card_inventories: dict[str, CardInventory] = {}
        self._session_cache: dict[tuple[str, str, str], tuple[SessionInfo, ...]] = {}

    def discover(self, *, refresh: bool = False) -> tuple[Source, ...]:
        """Return every usable source found for the selected mode."""

        if self._sources is not None and not refresh:
            return self._sources
        self._sources = None
        self._card_inventories.clear()
        self._session_cache.clear()
        if self.mode is SourceMode.LAN:
            endpoints = (
                (self.endpoint,)
                if self.endpoint is not None
                else tuple(self._discovery_provider(self.discovery_timeout))
            )
            if not endpoints:
                raise DiscoveryError(
                    f"no {self._service_description()} found in {self.discovery_timeout:g}s; "
                    "check that the device and computer share a LAN or pass --endpoint"
                )
            self._sources = probe_lan_sources(
                endpoints,
                timeout=self.request_timeout,
                token=self.token,
            )
        else:
            inventories = discover_card_inventories(
                card=self.card,
                search_roots=self.card_search_roots,
            )
            self._card_inventories = {
                inventory.source.location: inventory for inventory in inventories
            }
            self._sources = tuple(inventory.source for inventory in inventories)
        return self._sources

    def select_source(self, source: Source | None = None) -> Source:
        """Resolve an explicit source, configured selector, or sole discovery result."""

        if source is not None:
            if source.mode is not self.mode:
                raise DiscoveryError(
                    f"{source.location} is a {source.mode.value} source, not {self.mode.value}"
                )
            return source
        sources = self.discover()
        if self.device:
            selector = self.device.casefold()
            sources = tuple(
                candidate
                for candidate in sources
                if selector
                in {
                    candidate.device_id.casefold(),
                    candidate.device_label.casefold(),
                    candidate.location.casefold(),
                }
            )
            if not sources:
                raise DiscoveryError(f"no discovered source matches {self.device!r}")
        if len(sources) > 1:
            raise MultipleSourcesError([source.location for source in sources])
        if not sources:
            raise DiscoveryError("no usable Open Aria source found")
        return sources[0]

    def list_sessions(
        self,
        source: Source | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[SessionInfo, ...]:
        """List sealed sessions and preserve any gateway-unavailable entries."""

        selected = self.select_source(source)
        cache_key = (selected.location, selected.device_id, selected.device_label)
        if cache_key in self._session_cache and not refresh:
            return self._session_cache[cache_key]
        self._session_cache.pop(cache_key, None)
        if selected.mode is SourceMode.LAN:
            client = DeviceApiClient(
                selected.api_base or selected.location,
                timeout=self.request_timeout,
                token=self.token,
            )
            sessions = client.list_sessions(selected)
        else:
            if refresh:
                self._card_inventories.pop(selected.location, None)
            inventory = self._card_inventories.get(selected.location)
            if inventory is None:
                inventory = discover_card_inventories(
                    card=selected.card_root or Path(selected.location)
                )[0]
                self._card_inventories[selected.location] = inventory
            sessions = inventory.session_infos
        self._session_cache[cache_key] = sessions
        return sessions

    def export(
        self,
        *,
        source: Source | None = None,
        session_ids: Iterable[str] | None = None,
        output: Path | str | None = None,
        progress: Callable[[str], None] | None = None,
        continue_on_error: bool = False,
        options: ExportOptions | None = None,
        max_workers: int = 2,
    ) -> ExportResult:
        """Export a fresh inventory, optionally collecting per-recording failures.

        Source discovery and catalog errors always raise. By default the first
        recording error also raises; ``continue_on_error`` returns these errors
        in ``failed_sessions`` while attempting the remaining recordings.
        Continuing batches run up to ``max_workers`` recordings concurrently;
        fail-fast batches remain sequential so later recordings are not started.
        """

        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError("max_workers must be an integer between 1 and 8")
        selected = self.select_source(source)
        sessions = self.list_sessions(selected, refresh=True)
        requested = set(session_ids) if session_ids is not None else None
        by_id = {session.session_id: session for session in sessions}
        failures: list[ExportFailure] = []
        if requested is not None:
            unknown = sorted(requested - by_id.keys())
            if unknown and not continue_on_error:
                raise ExportError("unknown session id(s): " + ", ".join(unknown))
            failures.extend(
                ExportFailure(session_id, "recording no longer exists on the source")
                for session_id in unknown
            )
        chosen = tuple(
            session
            for session in sessions
            if session.exportable
            and (requested is None or session.session_id in requested)
        )
        unavailable = tuple(
            session
            for session in sessions
            if not session.exportable
            and (requested is None or session.session_id in requested)
        )
        if requested is not None and unavailable:
            detail = ", ".join(
                f"{session.session_id} ({session.unavailable_reason})"
                for session in unavailable
            )
            if not continue_on_error:
                raise ExportError(f"requested session(s) are not exportable: {detail}")
            failures.extend(
                ExportFailure(
                    session.session_id,
                    session.unavailable_reason or "recording is not exportable",
                )
                for session in unavailable
            )

        output_root = (
            self.output if output is None else Path(output).expanduser()
        ).resolve()
        exported = []
        inventory = None
        if selected.mode is SourceMode.CARD:
            inventory = self._card_inventories.get(selected.location)
            if inventory is None:
                raise DiscoveryError(f"recording card disappeared: {selected.location}")
            card_root = inventory.source.card_root
            if card_root is not None and (
                output_root == card_root or output_root.is_relative_to(card_root)
            ):
                raise ExportError(
                    "card-mode output must be outside the source recording card: "
                    f"{output_root}"
                )
        progress_lock = threading.Lock()

        def report(message: str) -> None:
            if progress is not None:
                with progress_lock:
                    progress(message)

        def export_one(session: SessionInfo) -> ExportedSession:
            if selected.mode is SourceMode.LAN:
                # Each worker owns its HTTP client/opener.
                return DeviceApiClient(
                    selected.api_base or selected.location,
                    timeout=self.request_timeout,
                    token=self.token,
                ).export_session(selected, session, output_root, report, options)
            assert inventory is not None
            return export_card_session(inventory, session, output_root, report, options)

        def collect(
            session: SessionInfo, operation: Callable[[], ExportedSession]
        ) -> None:
            try:
                result = operation()
            except (OpenAriaError, OSError) as error:
                if not continue_on_error:
                    raise
                failures.append(ExportFailure(session.session_id, str(error)))
                report(f"{session.session_id}: 导出失败：{error}")
            else:
                exported.append(result)
                report(f"{session.session_id}: 导出完成")

        if continue_on_error and max_workers > 1 and len(chosen) > 1:
            report(f"并行导出：最多 {max_workers} 个录制")
            # Submit only a bounded window; long batches do not queue thousands
            # of futures and a completed recording immediately frees its slot.
            remaining = iter(chosen)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                active = {}
                for session in remaining:
                    active[executor.submit(export_one, session)] = session
                    if len(active) == max_workers:
                        break
                while active:
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        session = active.pop(future)
                        collect(session, future.result)
                        next_session = next(remaining, None)
                        if next_session is not None:
                            active[executor.submit(export_one, next_session)] = (
                                next_session
                            )
            order = {
                session.session_id: index for index, session in enumerate(sessions)
            }
            exported.sort(key=lambda item: order[item.session_id])
            failures.sort(
                key=lambda item: (order.get(item.session_id, -1), item.session_id)
            )
        else:
            for session in chosen:
                collect(session, lambda session=session: export_one(session))
        return ExportResult(
            source=selected,
            output_root=output_root,
            sessions=tuple(exported),
            unavailable_sessions=unavailable,
            failed_sessions=tuple(failures),
        )

    def delete_sessions(
        self,
        *,
        source: Source | None = None,
        session_ids: Iterable[str],
        expected_manifests: Mapping[str, str] | None = None,
    ) -> DeleteResult:
        """Delete explicitly selected source recordings, leaving local exports intact."""
        selected = self.select_source(source)
        requested = set(session_ids)
        if not requested:
            raise DeleteError("未选择要删除的录制")
        if selected.mode is SourceMode.LAN:
            if not selected.capabilities.get("session_deletion", False):
                raise DeleteError("设备固件不支持远程删除，请升级固件后刷新来源")
            sessions = self.list_sessions(selected)
            by_id = {session.session_id: session for session in sessions}
            if requested - by_id.keys():
                raise DeleteError("部分录制已移除，请刷新列表")
            if expected_manifests is not None and (
                set(expected_manifests) != requested
                or any(
                    by_id[item].manifest_sha256 != expected_manifests[item]
                    for item in requested
                )
            ):
                raise DeleteError("录制内容已变化，请刷新后重新确认删除")
            try:
                return DeviceApiClient(
                    selected.api_base or selected.location,
                    timeout=self.request_timeout,
                    token=self.token,
                ).delete_sessions(
                    selected, tuple(by_id[item] for item in sorted(requested))
                )
            finally:
                self._session_cache.pop(
                    (selected.location, selected.device_id, selected.device_label), None
                )
        try:
            inventory = discover_card_inventories(
                card=selected.card_root or Path(selected.location)
            )[0]
            if inventory.source.device_id != selected.device_id:
                raise DeleteError("内存卡设备身份已变化，请刷新来源")
            return delete_card_sessions(inventory, requested)
        finally:
            self._card_inventories.pop(selected.location, None)
            self._session_cache.pop(
                (selected.location, selected.device_id, selected.device_label), None
            )

    @staticmethod
    def _service_description() -> str:
        return f"Open Aria service ({'_ylx-capture._tcp.local.'})"
