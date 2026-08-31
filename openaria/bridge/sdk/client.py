"""One-call public API for LAN and recording-card exports."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from ._card import CardInventory, discover_card_inventories, export_card_session
from ._lan import (
    DeviceApiClient,
    discover_mdns_endpoints,
    probe_lan_sources,
)
from .errors import DiscoveryError, ExportError, MultipleSourcesError
from .models import ExportResult, SessionInfo, Source, SourceMode


class OpenAriaSDK:
    """Discover one Open Aria source and export its sealed sessions.

    ``lan`` is the default product path. ``card`` discovers a mounted recording
    card without requiring its mount path. Both modes write the same verified
    session-tree output and return the same result models.
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
        self._session_cache: dict[str, tuple[SessionInfo, ...]] = {}

    def discover(self, *, refresh: bool = False) -> tuple[Source, ...]:
        """Return every usable source found for the selected mode."""

        if self._sources is not None and not refresh:
            return self._sources
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
        if selected.location in self._session_cache and not refresh:
            return self._session_cache[selected.location]
        if selected.mode is SourceMode.LAN:
            client = DeviceApiClient(
                selected.api_base or selected.location,
                timeout=self.request_timeout,
                token=self.token,
            )
            sessions = client.list_sessions(selected)
        else:
            inventory = self._card_inventories.get(selected.location)
            if inventory is None:
                self.discover(refresh=True)
                inventory = self._card_inventories.get(selected.location)
            if inventory is None:
                raise DiscoveryError(f"recording card disappeared: {selected.location}")
            sessions = inventory.session_infos
        self._session_cache[selected.location] = sessions
        return sessions

    def export(
        self,
        *,
        source: Source | None = None,
        session_ids: Iterable[str] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ExportResult:
        """Discover, select, and atomically export verified sessions."""

        selected = self.select_source(source)
        sessions = self.list_sessions(selected)
        requested = set(session_ids) if session_ids is not None else None
        by_id = {session.session_id: session for session in sessions}
        if requested is not None:
            unknown = sorted(requested - by_id.keys())
            if unknown:
                raise ExportError("unknown session id(s): " + ", ".join(unknown))
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
            raise ExportError(f"requested session(s) are not exportable: {detail}")

        output_root = self.output.resolve()
        exported = []
        if selected.mode is SourceMode.LAN:
            client = DeviceApiClient(
                selected.api_base or selected.location,
                timeout=self.request_timeout,
                token=self.token,
            )
            for session in chosen:
                exported.append(
                    client.export_session(selected, session, output_root, progress)
                )
        else:
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
            for session in chosen:
                exported.append(
                    export_card_session(
                        inventory,
                        session,
                        output_root,
                        progress,
                    )
                )
        return ExportResult(
            source=selected,
            output_root=output_root,
            sessions=tuple(exported),
            unavailable_sessions=unavailable,
        )

    @staticmethod
    def _service_description() -> str:
        return f"Open Aria service ({'_ylx-capture._tcp.local.'})"
