# Open Aria Bridge / SDK

Open Aria Bridge / SDK is frozen compatibility tooling for historical Open
Aria recording cards. It can inspect completed legacy sessions, validate the
card's cryptographic and content claims, normalize stereo video, carry verified
IMU and metadata artifacts forward, and publish an idempotent object-store
layout.

[Score D-049](https://github.com/mirrorbloom/openaria-score/blob/main/docs/DECISIONS.md#d-049-fixed-storage-and-lan-only-delivery-removable-and-interruption-workflows-retired)
makes Bridge / Desktop's LAN workflow the only current 0.5 import and
publication route. This repository is not a current product entry point,
release artifact, or acceptance gate. No new removable-media, safe-swap,
ENOSPC/inode-exhaustion, or unexpected-interruption recovery work is planned
here.

The retained implementation exposes a Python API in `main.py` and a command
line entry through that module. The distribution name remains
`ylx-card-pipeline` for compatibility; a stable `openaria.bridge.sdk` package
and `openaria-bridge` command are not part of the current 0.5 product plan.

## Safety model

- The recording card is opened as read-only evidence and is never modified.
- Every source artifact is checked against its manifest SHA-256 before use.
- Device and publication schemas are vendored and hash-pinned.
- Signed input requires an externally trusted device identity and key registry.
- `--allow-unsigned` is an explicit degraded mode and is recorded as such.
- Object-store credentials come from the process environment or workload
  identity; the CLI does not load `.env` files.
- Publication metadata is written last, so a failed legacy run cannot appear
  complete. This fail-closed behavior does not promise continuation or recovery
  after an interruption.

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
legacy-card handling, path safety, retained normalization behavior, real
synthetic-FFmpeg integration, idempotent object publication, and wheel/sdist
packaging. Passing these compatibility regressions is not a 0.5 release gate.

## Run a legacy compatibility import

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
manifest. Re-running the same completed legacy input reuses matching normalized
and uploaded objects instead of creating duplicate data. This ordinary retry
behavior is not interrupted-operation recovery.

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
