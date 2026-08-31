# Open Aria Bridge / SDK

Open Aria Bridge finds recordings on the local network or a mounted recording
card and exports their verified source data. The ordinary product entry point
is a full-screen terminal interface with no setup and no mode flags.

## Start

Open a terminal in this checkout and run:

```bash
uv sync --locked
uv run openaria-bridge
```

The installed command is also simply:

```bash
openaria-bridge
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
    print(session.session_id, session.path, session.total_bytes)
```

Applications may call `discover()` and `list_sessions()` before export. They
may also provide `endpoint=...`, `card=...`, `device=...`, or a one-call
`export(output=...)` destination override. A future authenticated Device API
can receive its bearer token through `OPENARIA_DEVICE_TOKEN`; tokens are never
accepted as command-line arguments.

## Output and integrity

LAN and recording-card sources produce the same session tree:

```text
OpenAria Exports/
  YLX-30D5872D/
    SESSION_ID/
      manifest.json
      video/...
      audio/...
      imu/...
      .openaria-export.json
```

The receipt records source mode and location, device identity, manifest digest,
and every artifact's role, path, size, and SHA-256. Bridge checks the manifest,
safe relative paths, exact byte counts, and every artifact SHA-256 before a
completed session directory becomes visible. LAN exports additionally validate
Device API v4 identity, required capabilities, response ETags, content lengths,
and media types.

Each session is assembled in a hidden staging directory and published with one
directory rename. Failed exports do not appear complete. Running an export
again revalidates and reuses an already matching destination.

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
