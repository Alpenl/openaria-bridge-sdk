"""Black-box tests for the Linux/macOS release installer."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "install.sh"


def _project_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release(root: Path, *, duplicate_wheel: bool = False) -> Path:
    version = _project_version()
    wheel = root / f"ylx_card_pipeline-{version}-py3-none-any.whl"
    wheel.write_bytes(b"installer-test-wheel")
    constraints = root / "constraints.txt"
    constraints.write_text("textual==8.2.8\n", encoding="utf-8")
    records = [
        f"{_sha256(wheel)}  {wheel.name}",
        f"{_sha256(constraints)}  {constraints.name}",
    ]
    if duplicate_wheel:
        duplicate = root / f"ylx_card_pipeline-{version}.post1-py3-none-any.whl"
        duplicate.write_bytes(b"duplicate-wheel")
        records.append(f"{_sha256(duplicate)}  {duplicate.name}")
    (root / "SHA256SUMS").write_text("\n".join(records) + "\n", encoding="utf-8")
    return wheel


def _write_fake_uv(path: Path) -> None:
    version = _project_version()
    path.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$@" > "$OPENARIA_TEST_UV_LOG"
mkdir -p "$UV_TOOL_BIN_DIR"
cat > "$UV_TOOL_BIN_DIR/openaria-bridge" <<'EOF'
#!/bin/sh
printf 'openaria-bridge VERSION\\n'
EOF
sed -i.bak 's/VERSION/PROJECT_VERSION/' "$UV_TOOL_BIN_DIR/openaria-bridge"
rm -f "$UV_TOOL_BIN_DIR/openaria-bridge.bak"
chmod +x "$UV_TOOL_BIN_DIR/openaria-bridge"
""".replace("PROJECT_VERSION", version),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_installer(
    tmp_path: Path, release: Path, fake_uv: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OPENARIA_BRIDGE_ALLOW_FILE_URLS": "1",
            "OPENARIA_BRIDGE_RELEASE_ROOT": release.as_uri(),
            "OPENARIA_BRIDGE_UV_BIN": str(fake_uv),
            "OPENARIA_BRIDGE_BIN_DIR": str(tmp_path / "bin"),
            "OPENARIA_TEST_UV_LOG": str(tmp_path / "uv-args.txt"),
            "UV_TOOL_DIR": str(tmp_path / "tools"),
        }
    )
    return subprocess.run(
        ["sh", str(INSTALLER)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_installer_downloads_verified_release_and_invokes_uv(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    wheel = _write_release(release)
    fake_uv = tmp_path / "uv"
    _write_fake_uv(fake_uv)

    result = _run_installer(tmp_path, release, fake_uv)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Release checksums verified." in result.stdout
    assert (
        f"openaria-bridge {_project_version()} installed successfully." in result.stdout
    )
    assert "Add " in result.stdout
    args = (tmp_path / "uv-args.txt").read_text(encoding="utf-8").splitlines()
    assert args[:2] == ["--no-config", "tool"]
    assert args[2] == "install"
    assert ["--python", "3.13"] == args[3:5]
    assert "--managed-python" not in args
    assert "--force" in args
    constraints_index = args.index("--constraints")
    assert Path(args[constraints_index + 1]).name == "constraints.txt"
    assert Path(args[-1]).name == wheel.name
    assert (tmp_path / "bin" / "openaria-bridge").is_file()


def test_installer_rejects_tampered_wheel_before_uv(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    wheel = _write_release(release)
    wheel.write_bytes(b"tampered")
    fake_uv = tmp_path / "uv"
    _write_fake_uv(fake_uv)

    result = _run_installer(tmp_path, release, fake_uv)

    assert result.returncode != 0
    assert f"checksum mismatch for {wheel.name}" in result.stderr
    assert not (tmp_path / "uv-args.txt").exists()


def test_installer_rejects_ambiguous_wheel_manifest(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    _write_release(release, duplicate_wheel=True)
    fake_uv = tmp_path / "uv"
    _write_fake_uv(fake_uv)

    result = _run_installer(tmp_path, release, fake_uv)

    assert result.returncode != 0
    assert "exactly one Open Aria Bridge wheel" in result.stderr
    assert not (tmp_path / "uv-args.txt").exists()


def test_installer_rejects_wheel_paths_in_checksum_manifest(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    wheel = _write_release(release)
    checksums = release / "SHA256SUMS"
    checksums.write_text(
        checksums.read_text(encoding="utf-8").replace(wheel.name, f"./{wheel.name}"),
        encoding="utf-8",
    )
    fake_uv = tmp_path / "uv"
    _write_fake_uv(fake_uv)

    result = _run_installer(tmp_path, release, fake_uv)

    assert result.returncode != 0
    assert "exactly one Open Aria Bridge wheel" in result.stderr
    assert not (tmp_path / "uv-args.txt").exists()


def test_installer_rejects_file_urls_without_test_opt_in(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    _write_release(release)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OPENARIA_BRIDGE_RELEASE_ROOT": release.as_uri(),
        }
    )

    result = subprocess.run(
        ["sh", str(INSTALLER)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing non-HTTPS download URL" in result.stderr


def test_installer_has_valid_posix_shell_syntax() -> None:
    result = subprocess.run(
        ["sh", "-n", str(INSTALLER)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
