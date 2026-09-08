# Capture Clock Export

Renderer version 2 derives video cadence from the verified `frames.ndjson`
timestamps and frame ordinals. `camera.effective_fps` includes capture shutdown
time, and the MP4 container rate can be nominal, so neither determines the
split-eye output cadence. A missing frame, reversed timestamp, mismatched
identity/count, or a residual greater than half a frame rejects the export.
Variable-rate sources must be retained for a future variable-rate renderer.

Recorded PCM segment times are sample-relative. `audio.sync` is session-relative.
The optional `openaria.audio-clock.v1` evidence binds sample positions to ALSA
DMA timestamps on `CLOCK_MONOTONIC`. The SDK recomputes the sample rate, endpoint
mapping and intermediate residuals, and rejects inconsistent evidence, XRUN or
suspend. Valid measured sample rates drive `atempo` before offset placement.
DMA timestamps do not prove calibrated ADC latency or external camera sync.

Older recordings without this evidence have `continuity=unknown`; their export
verdict is `start-offset-only`. Their original WAV files remain in
`.openaria/source`. Unknown discontinuities cannot be repaired by uniform time
stretch or guessed silence. Existing renderer-1 exports are rebuilt from the
available source; the previous complete export is retained in a sibling
`.SESSION.previous-*` directory only after the replacement is verified. A failed
render leaves the previous export intact. Sources must remain accessible for
rebuilding exports whose raw video was already cleaned up.

The integrated LAN/card SDK and the legacy `main.py export-sbs` Device Session v2
entry both use this renderer. The legacy command writes only the requested MP4;
the integrated exporter additionally writes integrity and clock-quality receipts.
