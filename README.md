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

The first available source is opened automatically. Use the compact source
selector to switch devices or cards; moving through its options does not start
a request until the choice is confirmed. Exportable recordings without a previous
successful export are selected by default. Completed recordings show **已导出**
and remain available for manual selection. Unavailable recordings remain visible with their individual reasons
and cannot be selected.

The recording list uses the full terminal width and adapts to smaller windows.
Filter by name, session ID, or date. **全选 / 清空** affects only the matching
recordings; selections outside the filter are retained and counted explicitly.
The export bar always shows the total selected count and source size.

Successful exports are marked immediately and deselected; failures remain selected
for retry. History survives application restarts, destination changes, and switching
between LAN and card access to the same device. It is keyed by device ID, session
ID, and manifest digest, so changed recording content is treated as new. History
is stored in `$XDG_STATE_HOME/openaria-bridge/exports.sqlite3`, defaulting to
`~/.local/state/openaria-bridge/exports.sqlite3`. Existing completed exports in the
chosen destination are recognized from their receipts and media file size without
rereading video contents. Select an older export destination once to import its
history. The marker records a past success; it does not guarantee that a moved,
removed, or subsequently modified output is still intact.

**选已导出** selects only marked recordings in the current filter and clears
other selections. Mounted cards and compatible LAN firmware support **删除** for selected source recordings;
the confirmation shows the source, recording count, unexported count, and any
selections hidden by the filter. Deletion is permanent and retains local exported
videos and their history. Cancel is focused by default. Linked continuations must
be included when deleting their predecessor, and deletion proceeds from the last
continuation backward. A failure stops further deletions and reports remaining
items. Unavailable recordings cannot be selected for deletion.

LAN deletion is enabled when the device advertises `session_deletion: true`.
Older firmware keeps the action disabled with an upgrade explanation. Remote
deletion uses `POST /api/v4/sessions/delete`, with 1-200 explicit recording IDs,
their displayed manifest digests, and one idempotency key reused for transport
retries. The firmware checks digests and continuation dependencies before deleting,
and rejects deletion during capture, finalization, active downloads, or volume release.
Customer-profile devices require a token with `deleteSessions` permission; the SDK
sends a same-device Origin and `X-CSRF-Token`. The latter defaults to
`OPENARIA_DEVICE_TOKEN` and can be overridden with `OPENARIA_DEVICE_CSRF_TOKEN` when
the device uses a separate CSRF secret.

The default destination is `~/OpenAria Exports`. Its current free space is
shown in the interface. Use the on-screen **目录…** action when another
location is needed. The interface rejects a destination on the source recording
card and checks available space before starting.

If automatic LAN discovery is unavailable, choose **+ 连接** and
enter an IP address such as `192.168.110.36`. Bare addresses use the Device API
v4 default HTTP port 8080.

Keyboard navigation is available throughout the interface:

- `R` rescans the LAN and mounted cards, including manually connected devices
- `A` opens manual device connection
- `O` changes the export destination
- `Space` selects or clears a session
- `/` focuses the recording filter; `Esc` clears it
- `E` exports the selected recordings
- `Delete` opens source-deletion confirmation when the source supports deletion
- `L` switches between recordings and the task log
- `Q` exits when no export or deletion is active

Export opens **任务记录**, which retains discovery errors, real pipeline
messages, completion summaries, and output paths for the current run. The
activity indicator and elapsed time do not claim a percentage that the SDK
cannot measure. A failed recording does not stop the remaining batch. Partial
results list both completed outputs and individual failures; only failed items
remain selected for retry. The SDK reloads the source inventory before each
export and revalidates completed outputs before reuse. Source changes, destination
changes, duplicate exports, and all quit shortcuts are blocked during export.
Deletion also locks source changes, export, destination changes, and quitting
until the operation finishes.

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

`discover(refresh=True)` and `list_sessions(refresh=True)` invalidate previous
results, including when refreshing fails. Every export reads a fresh inventory.
The TUI reuses the discovered card inventory and cached recording lists when
switching sources. Use refresh to read newly added or removed recordings.
Damaged card recordings remain visible as unavailable entries with a reason;
unrelated valid takes can still be exported. Entries whose manifest cannot be
accepted use an opaque `unavailable/<directory-name>` ID and retain the original
directory name as `display_name`.

SDK exports stop at the first failure by default. To collect per-recording
errors and continue with the remaining recordings, use:

```python
result = OpenAriaSDK(mode="lan", output="./exports").export(continue_on_error=True)

for failure in result.failed_sessions:
    print(failure.session_id, failure.error)
```

`result.sessions` contains successful exports. Explicitly selected recordings
that have disappeared or become unavailable are included in `failed_sessions`.
Source discovery and catalog errors still raise. The TUI enables this continuing
batch behavior automatically.

For explicitly selected source recordings, the SDK also provides
`sdk.delete_sessions(source=source, session_ids=[...])`. It rereads the card
inventory before deleting, rejects invalid selections and incomplete continuation
selections, and returns `DeleteResult.deleted_session_ids` and
`DeleteResult.failed_sessions`. SDK callers own confirmation; the method itself
performs deletion. For LAN confirmation workflows, pass
`expected_manifests={session_id: manifest_sha256, ...}` to reject selections whose
content changed after display, including a retry after a failed attempt. Unsupported
LAN firmware raises `DeleteError` without sending a delete request.

Read-only LAN requests retry transient HTTP errors, disconnected sockets, and
truncated responses up to three total attempts. Failed download attempts remove
their own partial file before retrying. Authentication errors, invalid manifests,
SHA-256 mismatches, and local write failures are not automatically retried.
LAN discovery probes up to eight distinct addresses concurrently, using a socket
timeout of at most three seconds per attempt; recording reads and downloads keep
the configured request timeout. If the catalog revision changes during pagination,
listing restarts from the first page, up to three attempts, without mixing entries
from different revisions. TUI task records include listing duration and failure
details.

## Output and integrity

Export settings are available in the terminal interface with **P / 导出设置**,
and through the same API for LAN and card sources:

```python
from openaria.bridge.sdk import OpenAriaSDK, ExportOptions

OpenAriaSDK(mode="lan", output="./archives").export(
    session_ids=["YOUR_SESSION_ID"],
    options=ExportOptions(
        video_codec="hevc",          # H.265 Main/hvc1
        video_quality="high",        # Default: medium/CRF 18 for more detail
        retain_sources=True,         # Keep exact MP4/WAV bytes and manifest
        audio_calibration_seconds=0, # Positive values delay audio
    ),
)
```

The default is H.264 medium/CRF 18, AAC 192 kb/s, and zero physical
calibration. HEVC saves space at the cost of export time and requires a HEVC
player. Sample-clock correction and measured video frame timestamps apply to
both codecs. Physical calibration is explicit and recorded in the receipt;
use a value measured for the selected sessions, not a value from another camera
or setup. **高画质** (`video_quality="high"`) uses medium/CRF 18 for either codec.
Select `video_quality="standard"` for the previous H.264 veryfast/CRF 20 or HEVC
medium/CRF 22 settings. This high quality default preserves more recorded detail
at the cost of export time and potentially larger files; it cannot restore
detail already lost in camera capture or recording. Both quality levels keep
the original eye dimensions (1920×1080 per eye becomes 3840×1080 side by side).
A changed codec, quality, calibration, or retention option rebuilds the export
atomically, keeping the previous verified directory as a hidden backup.
With `retain_sources=True`, every original artifact remains under
`.openaria/source`, verified by its original hash; no archive transcode is
needed to preserve acquisition quality.

Device Session v1/v2 recordings remain readable. Device Session v3 adds explicit
recording encoder metadata and H.264/HEVC split-eye inputs. Source manifests and
retained source artifacts keep their original bytes and hashes. Receipts created
before the quality option existed are treated as standard quality and rebuilt
when high quality is requested.

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

Preview the TUI with sample recordings and simulated exports, without a device
or any export file writes:

```bash
uv run python tools/preview_tui.py
```

Generate terminal screenshots for review:

```bash
uv run python tools/preview_tui.py --capture /tmp/openaria-tui-preview
```

The [TUI rewrite report](docs/tui-rewrite.md) records the baseline, ablation
experiments, design decisions, and validation procedure. The
[workflow stability report](docs/workflow-stability.md) records reproduced
business-flow failures, fixes, and regression coverage.

The historical normalization, SBS, and S3-compatible publication workflows in
`main.py` remain available to existing automation. They are compatibility
interfaces and are not part of the `openaria-bridge` TUI.

Query local source and artifact provenance with `uv run provenance.py`. See
[LICENSE](LICENSE).
