from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

import pytest

from openaria.bridge.sdk import (
    ContractError,
    OpenAriaSDK,
    SessionInfo,
    Source,
    SourceMode,
)
from openaria.bridge.sdk import _lan as lan
from openaria.bridge.sdk import client as client_module
from openaria.bridge.sdk._card import CardInventory

SOURCE = Source(
    mode=SourceMode.LAN,
    location="http://192.0.2.1:8080/api/v4",
    device_id="device",
    device_label="Device",
)


def test_discovery_probes_addresses_concurrently_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(3)
    calls: list[tuple[str, float]] = []

    class Client:
        def __init__(self, endpoint, *, timeout, token):
            self.endpoint = endpoint
            calls.append((endpoint, timeout))

        def probe(self):
            barrier.wait(timeout=2)
            if self.endpoint == "offline":
                raise lan.DiscoveryError("unreachable")
            return dataclasses.replace(SOURCE, location=self.endpoint)

    monkeypatch.setattr(lan, "DeviceApiClient", Client)
    sources = lan.probe_lan_sources(
        ["offline", "fast", "alternate", "fast"], timeout=15, token=None
    )
    assert len(sources) == 1
    assert sources[0].location in {"fast", "alternate"}
    assert sorted(calls) == [(name, 3.0) for name in ("alternate", "fast", "offline")]


def test_discovery_preserves_shorter_configured_timeout(monkeypatch) -> None:
    timeouts = []

    class Client:
        def __init__(self, endpoint, *, timeout, token):
            timeouts.append(timeout)

        def probe(self):
            return SOURCE

    monkeypatch.setattr(lan, "DeviceApiClient", Client)
    assert lan.probe_lan_sources(["fast"], timeout=0.5, token=None) == (SOURCE,)
    assert timeouts == [0.5]


def _page(revision: str, session_id: str, cursor: str | None = None) -> dict:
    return {
        "schema": "ylx.session-list.v3",
        "catalog_revision": "sha256:" + revision * 64,
        "items": [
            {
                "session_id": session_id,
                "producer_outcome": "sealed",
                "device": {
                    "device_id": SOURCE.device_id,
                    "device_label": SOURCE.device_label,
                },
                "started_at": "2026-09-08T00:00:00Z",
                "duration_seconds": 1,
                "total_bytes": 1,
                "verification": {
                    "actor": "gateway",
                    "verdict": "usable",
                    "manifest_sha256": "a" * 64,
                },
            }
        ],
        "next_cursor": cursor,
    }


def test_catalog_change_restarts_from_first_page_without_mixing_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = lan.DeviceApiClient(SOURCE.location)
    pages = iter(
        [
            _page("a", "old", "old-cursor"),
            _page("b", "second"),
            _page("b", "new", "new-cursor"),
            _page("b", "second"),
        ]
    )
    queries = []

    def get(path, *, query, label):
        queries.append(query)
        return next(pages)

    monkeypatch.setattr(client, "_json_get", get)
    monkeypatch.setattr(lan.time, "sleep", lambda _: None)
    assert [item.session_id for item in client.list_sessions(SOURCE)] == [
        "new",
        "second",
    ]
    assert [query.get("cursor") for query in queries] == [
        None,
        "old-cursor",
        None,
        "new-cursor",
    ]


def test_catalog_contract_errors_are_not_retried(monkeypatch) -> None:
    client = lan.DeviceApiClient(SOURCE.location)
    calls = []

    def get(*args, **kwargs):
        calls.append(True)
        return _page("invalid", "session")

    monkeypatch.setattr(client, "_json_get", get)
    with pytest.raises(ContractError, match="invalid catalog_revision"):
        client.list_sessions(SOURCE)
    assert len(calls) == 1


def test_card_selection_reuses_discovery_but_explicit_refresh_rereads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = dataclasses.replace(
        SOURCE, mode=SourceMode.CARD, location=str(tmp_path), card_root=tmp_path
    )
    session = SessionInfo(
        "session", "Recording", "2026-09-08T00:00:00Z", 1, 1, "a" * 64
    )
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        infos = (session,) if len(calls) == 1 else ()
        return (CardInventory(source, (), infos),)

    monkeypatch.setattr(client_module, "discover_card_inventories", discover)
    sdk = OpenAriaSDK(mode="card", card=tmp_path)
    assert sdk.discover() == (source,)
    assert sdk.list_sessions(source) == (session,)
    assert sdk.list_sessions(source) == (session,)
    assert len(calls) == 1
    assert sdk.list_sessions(source, refresh=True) == ()
    assert len(calls) == 2
