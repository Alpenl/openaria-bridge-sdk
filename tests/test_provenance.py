from __future__ import annotations

import hashlib

import provenance


def test_collect_reports_local_state_toolchain_and_explicit_artifact_hash(tmp_path):
    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"local artifact")

    report = provenance.collect(tmp_path, [artifact])

    assert report["git"]["commit"] is None
    assert report["git"]["clean_tree"] is False
    assert report["toolchain"]["python_version"]
    assert report["artifacts"] == [
        {
            "name": "output.mp4",
            "size_bytes": len(b"local artifact"),
            "sha256": hashlib.sha256(b"local artifact").hexdigest(),
        }
    ]
    assert "repository" not in report
    assert "dirty_paths" not in report["git"]
    assert "not card" in report["scope"]
