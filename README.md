# Open Aria Bridge / SDK

Open Aria Bridge / SDK is the programmatic import and publication path for
Open Aria recording cards. It discovers completed sessions, validates the
card's cryptographic and content claims, normalizes stereo video, carries
verified IMU and metadata artifacts forward, and publishes an idempotent
object-store layout.

The current 0.5 implementation exposes a Python API in `main.py` and a command
line entry through that module. The distribution name remains
`ylx-card-pipeline` for compatibility; a stable `openaria.bridge.sdk` package
and `openaria-bridge` command are planned as a versioned compatibility change,
not as part of this repository cutover.

## Safety model

- The recording card is opened as read-only evidence and is never modified.
- Every source artifact is checked against its manifest SHA-256 before use.
- Device and publication schemas are vendored and hash-pinned.
- Signed input requires an externally trusted device identity and key registry.
- `--allow-unsigned` is an explicit degraded mode and is recorded as such.
- Object-store credentials come from the process environment or workload
  identity; the CLI does not load `.env` files.
- Publication metadata is written last, so an interrupted upload cannot appear
  complete.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` with H.264 support on `PATH`
- an S3-compatible object store for publication runs

## Install and verify

```bash
uv sync --locked
uv run pytest -q
uv build
```

The tests cover strict JSON parsing, schema and signature admission, read-only
card handling, path safety, normalization recovery, real synthetic-FFmpeg
integration, idempotent object publication, and wheel/sdist packaging.

## Run

```bash
uv run main.py \
  --card /mnt/recording-card \
  --work ./work \
  --bucket recordings \
  --device-id DEVICE_ID
```

Use `uv run main.py --help` for the complete option set. A local normalization
run can stop before object-store publication:

```bash
uv run main.py --card /mnt/recording-card --work ./work --skip-upload
```

Object-store configuration is supplied by environment:

```text
S3_ENDPOINT
S3_ACCESS_KEY
S3_SECRET_KEY
S3_REGION
```

Do not place credentials in this repository or in command arguments. Use a
shell environment, CI secret store, or workload identity appropriate for the
deployment.

## Output contract

The normalizer emits separate left- and right-eye H.264 MP4 files using
`yuv420p` and `faststart`. MJPEG sources use CRF 20 and H.264 sources use CRF
18. An explicit 180-degree rotation is available for inverted rigs and becomes
part of the normalization cache identity.

Published objects use content-addressed evidence and a final Bucket Publication
manifest. Re-running the same completed input reuses matching normalized and
uploaded objects instead of creating duplicate data.

The `vendor/` directory contains only the schemas, test vectors, and fixture
corpora required by runtime validation and tests. `SOURCE.json` records the
source snapshot and per-file hashes without requiring access to another
repository.

## Provenance

Query local build and artifact metadata with:

```bash
uv run provenance.py --repository . --artifact dist/FILE
```

This reports local reproducibility facts only; it does not claim that a card,
signature, or object-store response is authentic evidence.

## License

See [LICENSE](LICENSE).
