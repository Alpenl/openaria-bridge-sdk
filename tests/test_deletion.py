from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openaria.bridge.sdk import DeleteError, OpenAriaSDK, Source, SourceMode
from openaria.bridge.sdk import _card as card


def _inventory(root: Path):
    recordings = root / "recordings"
    recordings.mkdir(parents=True)
    sessions = []
    for index, name in enumerate(("first", "second"), start=1):
        directory = recordings / name
        directory.mkdir()
        (directory / "media.mp4").write_bytes(b"recorded")
        sessions.append(
            SimpleNamespace(
                session_id=name,
                directory=directory,
                take={
                    "take_id": "take",
                    "sequence": index,
                    "continuation_of": "first" if index == 2 else None,
                },
            )
        )
    source = Source(SourceMode.CARD, str(root), "device", "Device", card_root=root)
    return card.CardInventory(source, tuple(sessions), ())


def test_deletion_requires_explicit_selection_and_rejects_unsupported_lan() -> None:
    source = Source(SourceMode.LAN, "http://192.0.2.1", "device", "Device")
    sdk = OpenAriaSDK()
    with pytest.raises(DeleteError, match="未选择"):
        sdk.delete_sessions(source=source, session_ids=[])
    with pytest.raises(DeleteError, match="不支持远程删除"):
        sdk.delete_sessions(source=source, session_ids=["first"])


def test_deleting_predecessor_requires_its_continuations(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    with pytest.raises(DeleteError, match="后续连续录制"):
        card.delete_card_sessions(inventory, {"first"})
    assert all(item.directory.is_dir() for item in inventory.sessions)
    result = card.delete_card_sessions(inventory, {"first", "second"})
    assert result.deleted_session_ids == ("second", "first")
    assert not list((tmp_path / "recordings").iterdir())


def test_deletion_failure_preserves_predecessor(tmp_path: Path, monkeypatch) -> None:
    inventory = _inventory(tmp_path)
    calls = []

    def fail(path):
        calls.append(path)
        raise PermissionError("read-only card")

    monkeypatch.setattr(card.shutil, "rmtree", fail)
    result = card.delete_card_sessions(inventory, {"first", "second"})
    assert not result.deleted_session_ids
    assert len(result.failed_sessions) == 2
    assert calls == [inventory.sessions[1].directory]
    assert inventory.sessions[0].directory.exists()


def test_deletion_rejects_unknown_or_symlinked_recordings(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path / "card")
    with pytest.raises(DeleteError, match="无法安全识别"):
        card.delete_card_sessions(inventory, {"../outside"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    directory = inventory.sessions[1].directory
    (directory / "media.mp4").unlink()
    directory.rmdir()
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DeleteError, match="拒绝删除"):
        card.delete_card_sessions(inventory, {"second"})
    assert (outside / "keep.txt").read_text() == "keep"
