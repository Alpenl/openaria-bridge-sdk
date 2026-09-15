# Export reliability investigation — 2026-09-13

## 2026-09-14: long-session timeouts and accelerated rendering

Read-only measurements against .180 backend `24bb508` requested HEAD for the
same first 方凳 video artifact twice. Response headers took 14.696 and 12.994
seconds despite transferring no media body. This matches that backend's
per-artifact full-session hashing and nearly consumes Bridge's 15-second
request timeout even without concurrent exports. Conductor `f89a3b7` already
contains the identity-checked verification cache; .180 has not installed it.

Bridge now gives artifact requests at least 120 seconds of socket inactivity
budget while retaining the shorter metadata/discovery budget. Transient partial
transfers retry using Range and If-Range, validate Content-Range/length/type/ETag,
and verify the complete file SHA-256. A server that ignores Range restarts the
artifact; malformed ranges and integrity failures are not retried as successes.
Exhausted retries remove only this download's temporary target. Existing targets
are rejected before any network request.

Rendering now probes real NVIDIA encoding and the timestamp options needed by
the exporter. Candidates are `OPENARIA_FFMPEG`, the bundled runtime, the optional
user runtime, and FFmpeg on PATH. The optional runtime is
`~/.local/share/openaria/ffmpeg/ffmpeg` on Linux or
`%LOCALAPPDATA%/openaria/ffmpeg/ffmpeg.exe` on Windows. No runtime is downloaded by
the application. The tested workstation's optional runtime points to its existing
FFmpeg 7.1.1 installation. Its system FFmpeg 4.4 lacks required timestamp options;
the bundled FFmpeg has no NVENC. Unsupported machines retain CPU encoding.

H.264/HEVC NVENC uses CQ 18/p6 for high quality and the configured quality value
with p4 for standard. Source dimensions, frame timestamps, audio sample-clock
alignment and final validation remain mandatory. Hardware execution failure
retries locally with the software encoder. Software H.264 can use up to 16
threads according to host CPU count. `media.json` records the actual
`output.video_encoder`; existing verified exports remain reusable.

Real measurements on the cached source of 泡沫箱
`01a09ef0-57b8-755c-b366-d5f79f1d14de`:

| Path | Local composition/validation | Frames | Output bytes |
| --- | ---: | ---: | ---: |
| Previous CPU high quality | approximately 308 s | 4901 | 676760304 |
| Automatic NVENC high quality | 90.436 s | 4901 | 1042807691 |

The 3.4x speedup excludes device transfer and uses the same source recording.
Both use 3840x1080 SBS H.264 plus audio. NVENC CQ and software CRF are different
rate controllers: equal numbers do not imply identical quality or file size;
the high-quality accelerated output is larger. No bit-identical-image claim is
made. A separate full 方凳 NVENC probe encoded in 50.584 seconds and passed all
5251 frame timestamps, with approximately 74.156 seconds through PTS validation.

Validation: 285 SDK tests and 8 subtests passed, including interrupted local HTTP
responses, real Range resumes, ignored ranges, malformed ranges, separate socket
timeouts and a real CPU fallback render that retains image/audio assertions.
Host evidence and accelerated samples: `/data2/openaria-export-optimization-20260914/`.

The supplied screenshot showed 32 sealed recordings on YLX-1C7447D1, with only
two selectable. Read-only Device API queries to `192.168.110.248:8080` confirmed
30 historical summaries had `verification: null`. The device build was
`24bb5089c60997dcfb75f9043af4a020c0b6374e`.

## Historical verification

Conductor intentionally loads historical metadata without reading all media at
startup. `open_verified_artifact` triggers verification of the requested session.
Bridge previously refused to download entries without a usable gateway verdict,
so these entries could never trigger that verification. Waiting or refreshing
alone could not resolve the mismatch.

Bridge now resolves the digest of each metadata-only manifest, validates the
session/device identity, sealed flag, artifact descriptors and total size, and
allows the download to reach the device's verification boundary. Invalid verdicts
remain blocked; a missing `verification` field is not treated as an explicit null.
Transport failures appear per recording and are retried on refresh/export.

## Clock jump

Session `00dc6ad1-095b-7f0e-a4af-e407d47989ce` contained:

| Field | Recorded value |
| --- | --- |
| Start | `2000-01-01T08:01:29.437028+08:00` |
| End | `2026-09-13T18:28:14.504397+08:00` |
| Duration | `26.338194895` seconds |
| Duration clock | `host_monotonic` |

The recovered display time is `2026-09-13T18:27:48.166202+08:00`, marked as
inferred. Bridge changes display metadata, not source manifests or their hashes.
The correction requires a plausible end date and a wall-clock discrepancy greater
than five seconds. Without a reliable end date, the UI reports an unknown date.

The accompanying local Conductor change uses monotonic elapsed time for live
recording progress and rebases the start timestamp on the calibrated end when
sealing after a clock jump. It also adjusts automatically generated names while
preserving user labels.

## Parallel processing and device I/O

The TUI now runs two recordings concurrently, with sequential artifact transfers
within each recording. SDK continuing batches accept `max_workers=1..8`. The submitted
recording window is bounded; success/failure callbacks are serialized; returned
results retain catalog order. All in-flight writes finish before a failed staging
directory is removed. Encoder concurrency is bounded without changing codec,
quality preset or CRF.

The local Conductor patch removes repeated full-session hashing on each artifact
request. The first cold request verifies the session. Later requests securely
reopen and compare the manifest and every artifact's device, inode, mode, link
count, size, mtime and ctime. Changes invalidate the cache and require full
verification. Tests retain same-size corruption detection, including corruption
of a different artifact and restored mtime. This device patch is not deployed as
part of these local changes.

## Validation

- Real device: all 32 recordings became selectable, including the 30 historical
  recordings. Discovery plus manifest resolution took 2.615 seconds in one run.
- Real export: historical sessions `01a07a98-9c05-7dc6-a3c6-373d3c942101` and
  `01a076e9-3422-70f3-b12d-3ed005ff9bad` exported concurrently in 6.984 seconds,
  including fresh discovery/listing, downloads, encoding and final verification.
  The two MP4 files were 9,913,277 and 7,116,940 bytes. Outputs are under
  `/home/alpen/DEV/artifacts/bridge-export-20260913/YLX-1C7447D1/`.
- This measurement uses the device's existing firmware, not the local device I/O
  patch. It covers two short recordings and is not a before/after speedup claim
  for multi-gigabyte recordings.
- Bridge: 277 tests and 8 subtests passed after adding regressions for deferred
  verification, corrupt downloads, cleanup with concurrent writers, bounded
  recording concurrency, failure isolation, stable result ordering, and clock
  recovery.
- Conductor: 110 recording/coordinator tests passed, including one full validation
  across multiple cold-session artifact downloads, tampering with restored mtime,
  and forward/backward wall-clock jumps while sealing. Targeted Ruff checks pass.

## Follow-up: HTTP 409 with the deployed firmware

The initial two-artifact-per-recording concurrency was not compatible with the
deployed `24bb5089` firmware. The user reported `session_not_verified`, reason
`absent`, while exporting `01a09b23-073a-7f83-9240-c5c232db0d68` (84.131 seconds,
347,719,349 source bytes, 11 artifacts). That firmware invalidates the entire
session's verification before every artifact access and releases the catalog
lock between invalidation, validation and opening. Overlapping requests for the
same session can clear one another's verification state. The earlier two short
recordings did not reveal this race.

Bridge now downloads artifacts sequentially within a recording while retaining
concurrency across recordings and overlap between downloading and rendering.
This fix works with the existing firmware and does not require a deployment.
No 409 errors are blindly retried, and integrity checks remain mandatory.

A regression HTTP server reproduces the old firmware's overlapping-request
failure. A negative control explicitly enables the former two-worker scheduler
and gets HTTP 409; the corrected scheduler completes the same export. This is
covered for both already-verified and metadata-only catalogs. The 55 targeted
integration, concurrency and workflow tests pass.

The exact user-reported session was then exported using the existing device
firmware. All 11 artifacts downloaded without HTTP 409, the three video segments
and three audio segments were composed, and final verification passed. Total
elapsed time was 205.055 seconds (about 61 seconds through downloading and input
inspection, then about 144 seconds for composition and final verification).
The result contains one successful export and no failures. The MP4 is
336,769,962 bytes at:

`/home/alpen/DEV/artifacts/bridge-export-409-fix-20260913/YLX-1C7447D1/01a09b23-073a-7f83-9240-c5c232db0d68/recording.mp4`

The already-running TUI process uses this repository's virtual environment but
must be restarted to load the changed Python module.
