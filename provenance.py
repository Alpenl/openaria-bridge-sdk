"""Query reproducibility facts for a pipeline run or produced artifact.

This reports local build state. It intentionally makes no claim that a card,
an upload endpoint, or an object-store response is authentic evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def command_output(arguments: list[str], repository: Path) -> str | None:
    try:
        completed = subprocess.run(
            arguments,
            cwd=repository,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def sha256_of(path: Path) -> tuple[int, str]:
    """Hash one open descriptor, so reported size and digest are coherent."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"artifact changed while hashing: {path.name}")
    return before.st_size, digest.hexdigest()


def tool_version(command: str) -> str | None:
    output = command_output([command, "-version"], Path.cwd())
    return output.splitlines()[0] if output else None


def artifact_record(path: Path) -> dict:
    size_bytes, sha256 = sha256_of(path)
    return {"name": path.name, "size_bytes": size_bytes, "sha256": sha256}


def collect(repository: Path, artifacts: list[Path]) -> dict:
    """Return queryable local state and hashes for explicitly named outputs."""
    status = command_output(["git", "status", "--porcelain=v1"], repository)
    return {
        "schema_version": 1,
        "git": {
            "commit": command_output(["git", "rev-parse", "HEAD"], repository),
            "clean_tree": status == "",
            "dirty_path_count": len(status.splitlines()) if status else 0,
        },
        "toolchain": {
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "ffmpeg_version": tool_version("ffmpeg"),
            "ffmpeg_on_path": shutil.which("ffmpeg") is not None,
        },
        "artifacts": [artifact_record(path) for path in artifacts],
        "scope": "local reproducibility metadata; not card, session-signature, or storage evidence",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    arguments = parser.parse_args()
    print(
        json.dumps(
            collect(arguments.repository, arguments.artifact), indent=2, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
