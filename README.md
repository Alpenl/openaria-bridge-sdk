# Open Aria Bridge / SDK

Open Aria Bridge finds recordings on the local network or a mounted recording
card, verifies every source file, and automatically produces playable videos.
The ordinary product entry point is a full-screen terminal interface with no
setup and no mode flags.

## Install

On Linux or macOS, install the latest verified release with:

```bash
curl -LsSf https://github.com/Alpenl/openaria-bridge-sdk/releases/latest/download/install.sh | sh
```

The installer verifies the release checksums, installs `uv` when needed, and
creates an isolated tool environment with Python 3.13. `uv` reuses a compatible
interpreter when available and downloads one when needed; it does not install
packages into the system Python environment. The command is installed to
`~/.local/bin` by default, and the installer prints the exact PATH instruction
when that directory is not already available in the current shell.

Run the same command again to upgrade to the latest release. To uninstall:

```bash
uv tool uninstall ylx-card-pipeline
```

## Start

After installation, launch the terminal interface with:

```bash
openaria-bridge
```

For development from a source checkout, run:

```bash
uv sync --locked
uv run openaria-bridge
```

On startup, Bridge searches for both of these sources at the same time:

- Open Aria devices advertising `_ylx-capture._tcp.local.` on the LAN
- Open Aria recording cards mounted by Linux, macOS, or Windows

The first available source is opened automatically. Every exportable session
is selected by default, unavailable sessions are shown but excluded, and the
primary action displays the exact session count and total size before export.

The default destination is `~/OpenAria Exports`. Its current free space is
shown in the interface. Use the on-screen **更改目录** action when another
location is needed. The interface rejects a destination on the source recording
card and checks available space before starting.

If automatic LAN discovery is unavailable, choose **手动连接** and
enter an IP address such as `192.168.110.36`. Bare addresses use the Device API
v4 default HTTP port 8080.

Keyboard navigation is available throughout the interface:

- `R` rescans the LAN and mounted cards
- `A` opens manual device connection
- `O` changes the export destination
- `Space` selects or clears a session
- `Q` exits when no export is active

`openaria-bridge --help` and `openaria-bridge --version` are the only command
options. Programs and unattended jobs should use the Python SDK rather than
screen-scraping the TUI.

## Python SDK

The stable import path is `openaria.bridge.sdk`. The API keeps explicit source
controls for application integration while the human CLI stays automatic:

```python
from openaria.bridge.sdk import OpenAriaSDK

lan_result = OpenAriaSDK(mode="lan", output="./exports").export()
card_result = OpenAriaSDK(mode="card", output="./exports").export()

for session in lan_result.sessions:
    print(session.session_id, session.media_path, session.media_bytes)
```

Applications may call `discover()` and `list_sessions()` before export. They
may also provide `endpoint=...`, `card=...`, `device=...`, or a one-call
`export(output=...)` destination override. A future authenticated Device API
can receive its bearer token through `OPENARIA_DEVICE_TOKEN`; tokens are never
accepted as command-line arguments.

## Output and integrity

LAN and recording-card sources produce the same user-facing result:

```text
OpenAria Exports/
  YLX-30D5872D/
    SESSION_ID/
      recording.mp4
      .openaria/
        export.json
        media.json
        source/
          manifest.json
          imu/...
```

`recording.mp4` is the finished side-by-side stereo video. Bridge joins every
left-eye and right-eye MP4 segment in manifest order, places the eyes side by
side, joins all WAV segments, aligns audio using the Device Session monotonic
timeline, and writes H.264 video with AAC audio. FFmpeg is included with the
Python package; users do not install or configure it separately.

The internal receipt records source mode and location, device identity,
manifest digest, final-video digest, synchronization offset, and every source
artifact's role, path, size, and SHA-256. Bridge checks safe relative paths,
exact byte counts, and every artifact SHA-256 before rendering. LAN exports
additionally validate Device API v4 identity, required capabilities, response
ETags, content lengths, and media types. The source manifest and non-media
metadata remain under `.openaria/source` for traceability. Verified left/right
MP4 and WAV segments are task-owned temporary inputs: Bridge removes them only
after rendering, output probing, frame-count verification, and timeline
alignment all pass. A cleanup failure keeps the export incomplete.

Each session is downloaded and rendered in a hidden staging directory, the
finished MP4 is decoded briefly to validate its video and expected audio
streams, and the whole session is published with one directory rename. Failed
downloads, renders, or cleanup operations do not appear complete. Running an
export again revalidates the retained source evidence and final-media SHA-256
and reuses a matching destination.
Verified source-tree exports made by version 0.3 are upgraded to the finished
layout the next time that session is exported.

## Development

The integrated path requires Python 3.13 or newer. Verify and build it with:

```bash
uv run pytest -q
uv build
```

The historical normalization, SBS, and S3-compatible publication workflows in
`main.py` remain available to existing automation. They are compatibility
interfaces and are not part of the `openaria-bridge` TUI.

Query local source and artifact provenance with `uv run provenance.py`. See
[LICENSE](LICENSE).
