"""Black-box packaging checks for runtime contract availability."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

EXPECTED_RUNTIME_PATHS = {
    "main.py",
    "provenance.py",
    "openaria/__init__.py",
    "openaria/bridge/__init__.py",
    "openaria/bridge/sdk/__init__.py",
    "openaria/bridge/sdk/__main__.py",
    "openaria/bridge/sdk/_card.py",
    "openaria/bridge/sdk/_export.py",
    "openaria/bridge/sdk/_json.py",
    "openaria/bridge/sdk/_lan.py",
    "openaria/bridge/sdk/_media.py",
    "openaria/bridge/sdk/cli.py",
    "openaria/bridge/sdk/client.py",
    "openaria/bridge/sdk/errors.py",
    "openaria/bridge/sdk/models.py",
    "openaria/bridge/sdk/openaria.tcss",
    "openaria/bridge/sdk/tui.py",
    "vendor/ylx-contracts/SOURCE.json",
    "vendor/ylx-contracts/ylx-bucket-publication-v2.schema.json",
    "vendor/ylx-contracts/ylx-bucket-publication-v3.schema.json",
    "vendor/ylx-contracts/ylx-device-session-v1.schema.json",
    "vendor/ylx-contracts/ylx-device-session-v2.schema.json",
    "vendor/ylx-contracts/fixtures/valid/ylx-device-session-v2.audio-recorded.json",
    "vendor/ylx-contracts/fixtures/invalid/ylx-bucket-publication-v3.wrong-source-major.json",
}


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the packaging black-box test"
    repo = Path(__file__).resolve().parents[1]
    dist = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [uv, "build", "--out-dir", str(dist)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    return wheels[0], sdists[0]


def _sdist_runtime_names(sdist: Path) -> set[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        names = set()
        for member in archive.getnames():
            parts = Path(member).parts
            if len(parts) > 1:
                names.add(str(Path(*parts[1:])))
        return names


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def test_built_distributions_include_entry_module_and_vendored_contracts(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel, sdist = built_distributions

    assert EXPECTED_RUNTIME_PATHS <= _wheel_names(wheel)
    assert EXPECTED_RUNTIME_PATHS <= _sdist_runtime_names(sdist)


def test_wheel_registers_integrated_openaria_bridge_command(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel, _ = built_distributions
    with zipfile.ZipFile(wheel) as archive:
        entry_points = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        )
        contents = archive.read(entry_points).decode("utf-8")
    assert "openaria-bridge = openaria.bridge.sdk.cli:main" in contents


def test_source_distribution_includes_release_installer(
    built_distributions: tuple[Path, Path],
) -> None:
    _, sdist = built_distributions
    assert "install.sh" in _sdist_runtime_names(sdist)


@pytest.mark.parametrize("distribution_name", ("wheel", "sdist"))
def test_installed_distribution_loads_vendored_contract_validators_outside_checkout(
    tmp_path: Path,
    built_distributions: tuple[Path, Path],
    distribution_name: str,
) -> None:
    wheel, sdist = built_distributions
    distribution = {"wheel": wheel, "sdist": sdist}[distribution_name]
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the packaging black-box test"
    target = tmp_path / f"installed-{distribution_name}"
    outside = tmp_path / f"outside-checkout-{distribution_name}"
    outside.mkdir()
    install = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(target),
            "--no-deps",
            str(distribution),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    env = _pythonpath_env(target)
    script = """
import json
import main
import provenance
from openaria.bridge.sdk import OpenAriaSDK, SourceMode

device = main._device_session_v1_validator()
device_v2 = main._device_session_v2_validator()
bucket = main._bucket_publication_v2_validator()
bucket_v3 = main._bucket_publication_v3_validator()
fixture = main.ylx_contract_fixture("valid/ylx-device-session-v2.audio-recorded.json")
print(json.dumps({
    "module": main.__file__,
    "provenance_module": provenance.__file__,
    "contract_root": str(main.YLX_CONTRACT_ROOT),
    "device_schema": device.schema["$id"],
    "device_v2_schema": device_v2.schema["$id"],
    "bucket_schema": bucket.schema["$id"],
    "bucket_v3_schema": bucket_v3.schema["$id"],
    "fixture": str(fixture),
    "fixture_schema": json.loads(fixture.read_text(encoding="utf-8"))["schema"],
    "sdk_module": OpenAriaSDK.__module__,
    "lan_mode": SourceMode.LAN.value,
}, sort_keys=True))
"""
    loaded = subprocess.run(
        [sys.executable, "-c", script],
        cwd=outside,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    result = json.loads(loaded.stdout)
    assert Path(result["module"]).is_relative_to(target)
    assert Path(result["provenance_module"]).is_relative_to(target)
    assert Path(result["contract_root"]).is_relative_to(target)
    assert Path(result["fixture"]).is_relative_to(target)
    assert result["device_schema"] == "urn:ylx:schema:device-session:v1"
    assert result["device_v2_schema"] == "urn:ylx:schema:device-session:v2"
    assert result["bucket_schema"] == "urn:ylx:schema:bucket-publication:v2"
    assert result["bucket_v3_schema"] == "urn:ylx:schema:bucket-publication:v3"
    assert result["fixture_schema"] == "ylx.device-session.v2"
    assert result["sdk_module"] == "openaria.bridge.sdk.client"
    assert result["lan_mode"] == "lan"


def _pythonpath_env(target: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env["PYTHONPATH"] = str(target)
    env["PYTHONNOUSERSITE"] = "1"
    return env
