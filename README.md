# Open Aria Bridge / SDK

Open Aria Bridge / SDK discovers recordings and exports their verified source
data with one Python API or CLI. It has two first-class modes:

- **LAN mode** is the default. It discovers `_ylx-capture._tcp.local.`, probes
  each candidate's Device API v4 identity, lists sealed sessions, and downloads
  every declared artifact.
- **Card mode** scans the computer's mounted volumes for an Open Aria recording
  card. It does not require the user to know the mount path and never writes to
  the card.

Both modes produce the same local session-tree layout. Every manifest and
artifact is checked against its declared byte count and SHA-256 before the
completed directory becomes visible.

## Install and verify

Requirements for the integrated export path are Python 3.13 or newer and
[uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked
uv run pytest -q
uv build
```

The installed command is `openaria-bridge`. Inside this checkout, run it with
`uv run openaria-bridge`.

## LAN export

The default command browses the local network, shows the discovered device and
sealed sessions, then asks once before exporting all usable sessions:

```bash
uv run openaria-bridge
```

For unattended use, accept the export without a prompt:

```bash
uv run openaria-bridge --yes --output ./openaria-export
```

If multicast discovery is unavailable, provide the device directly. A bare IP
uses Device API v4's default HTTP port 8080:

```bash
uv run openaria-bridge --endpoint 192.168.110.36 --yes
```

Use `--device DEVICE_ID_OR_LABEL` when more than one device is present, and
repeat `--session SESSION_ID` to select specific sessions. A future authenticated
Device API can receive its bearer token through `OPENARIA_DEVICE_TOKEN`; tokens
are not accepted as command-line arguments.

## Recording-card export

Insert or mount the card and select card mode:

```bash
uv run openaria-bridge --mode card
```

Linux mount information, macOS `/Volumes`, and Windows drive roots are scanned.
One detected card is selected automatically; multiple cards produce a numbered
choice. The detected local path and session summary are shown before the export
confirmation.

An explicit path remains available for unusual mount layouts or automation:

```bash
uv run openaria-bridge \
  --mode card \
  --card /media/$USER/OPENARIA \
  --output ./openaria-export \
  --yes
```

`uv run main.py --mode card` and `uv run main.py --mode lan` route to the same
integrated CLI for source-checkout compatibility.

## Python API

The stable import path is `openaria.bridge.sdk`. Discovery, source selection,
session listing, download/copy, and integrity checks are all handled by one
object:

```python
from openaria.bridge.sdk import OpenAriaSDK

lan_result = OpenAriaSDK(mode="lan", output="./exports").export()
card_result = OpenAriaSDK(mode="card", output="./exports").export()

for session in lan_result.sessions:
    print(session.session_id, session.path, session.total_bytes)
```

For a non-interactive program with multiple sources, pass `device=...`,
`endpoint=...`, or `card=...`. `discover()` and `list_sessions()` expose the
same immutable `Source` and `SessionInfo` values used by `export()`.

## Output and integrity

Exports are grouped by device label and session ID:

```text
openaria-export/
  YLX-30D5872D/
    SESSION_ID/
      manifest.json
      video/...
      audio/...
      imu/...
      .openaria-export.json
```

The final receipt records the source mode and location, device identity,
manifest digest, and every artifact's role, path, size, and SHA-256. A session
is assembled in a private staging directory and published with one directory
rename. A failed or mismatched download therefore does not appear as a complete
export. Re-running against an already matching export verifies and reuses it.

mDNS records are discovery candidates, not trusted device identities. The SDK
accepts a LAN source only after its `/api/v4/device` response identifies a
supported Device API v4 device with session-list, session-detail, and artifact
download capability. Manifest response headers, exact manifest bytes, artifact
ETags, content lengths, safe relative paths, and artifact SHA-256 values are
then checked before publication.

## Advanced compatibility pipeline

The original normalization and S3-compatible publication pipeline remains in
`main.py` for existing automation. Its invocation is unchanged:

```bash
uv run main.py \
  --card /mnt/recording-card \
  --work ./work \
  --bucket recordings \
  --device-id DEVICE_ID
```

That advanced path additionally requires `ffmpeg`/`ffprobe` and object-store
configuration through `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and
`S3_REGION`. A local normalization run can still use `--skip-upload`, and the
existing `main.py export-sbs` command remains available.

## Provenance and license

Query local build and artifact metadata with:

```bash
uv run provenance.py --repository . --artifact dist/FILE
```

See [LICENSE](LICENSE).
