# Capture Clock Export

Renderer version 3 derives video timing from the verified `frames.ndjson`
timestamps and frame ordinals. `camera.effective_fps` includes capture shutdown
time, and the MP4 container rate can be nominal, so neither determines the
split-eye output timing. Each captured frame gets its original session-relative
PTS, rounded to the nearest microsecond; variable intervals remain variable.
A missing frame, reversed timestamp, mismatched identity/count, or timestamps
that collapse at microsecond precision reject the export. The final frame lasts
one measured average interval. Every decoded output PTS and frame duration is
checked before publication, including long source gaps and the final frame.

The MP4 encoder uses passthrough timestamps and disables B-frame reordering to
preserve portable final-frame duration. Existing quality settings remain intact;
compression efficiency may differ. A balanced FFmpeg expression is written to a
temporary filter file so long recordings do not exceed command-line limits.
Sources without a frame index retain their existing constant-rate behavior.

Recorded PCM segment times are sample-relative. `audio.sync` is session-relative.
The optional `openaria.audio-clock.v1` evidence binds sample positions to ALSA
DMA timestamps on `CLOCK_MONOTONIC`. The SDK recomputes the sample rate, endpoint
mapping and intermediate residuals, and rejects inconsistent evidence, XRUN or
suspend. Valid measured sample rates drive `atempo` before offset placement.
DMA timestamps do not prove calibrated ADC latency or external camera sync.

Older recordings without this evidence have `continuity=unknown`; their export
verdict is `start-offset-only`. Their original WAV files remain in
`.openaria/source`. Unknown discontinuities cannot be repaired by uniform time
stretch or guessed silence. Existing renderer-1/2 exports are rebuilt from the
available source; the previous complete export is retained in a sibling
`.SESSION.previous-*` directory only after the replacement is verified. A failed
render leaves the previous export intact. Sources must remain accessible for
rebuilding exports whose raw video was already cleaned up.

The integrated LAN/card SDK and the legacy `main.py export-sbs` Device Session v2
entry both use this renderer. The legacy command writes only the requested MP4;
the integrated exporter additionally writes integrity and clock-quality receipts.
