"""Bounded streaming access to losslessly compressed recording metadata."""

import io
import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path

import zstandard
import imageio_ffmpeg


@contextmanager
def open_metadata(path: Path):
    with path.open("rb") as raw:
        if path.suffix == ".zst":
            with zstandard.ZstdDecompressor(max_window_size=1024 * 1024).stream_reader(
                raw, read_across_frames=True
            ) as decoded:
                with io.BufferedReader(decoded) as buffered:
                    yield buffered
        else:
            yield raw


def verify_flac(raw: bytes, audio: dict, segment: dict) -> None:
    if (
        len(raw) < 42
        or raw[:4] != b"fLaC"
        or raw[4] & 0x7F != 0
        or raw[5:8] != b"\x00\x00\x22"
    ):
        raise ValueError("FLAC STREAMINFO is invalid")
    packed = int.from_bytes(raw[18:26], "big")
    if (
        packed >> 44 != audio["sample_rate"]
        or ((packed >> 41) & 7) + 1 != audio["channels"]
        or ((packed >> 36) & 31) + 1 != 16
        or (packed & ((1 << 36) - 1)) != segment["end_sample"] - segment["start_sample"]
    ):
        raise ValueError("FLAC sample domain disagrees with manifest")
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-c:a",
            "pcm_s16le",
            "pipe:1",
        ],
        input=raw,
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    encoding = segment["artifact"]["storage_encoding"]
    if (
        len(decoded) != encoding["pcm_bytes"]
        or hashlib.sha256(decoded).hexdigest() != encoding["pcm_sha256"]
    ):
        raise ValueError("FLAC decoded PCM checksum mismatch")
