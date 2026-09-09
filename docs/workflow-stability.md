# Workflow Stability

Branch: `refactor/tui-workbench`.

This pass addresses business-flow failures after the initial TUI rewrite.
Regression tests first reproduced stale state, batch abortion, download failure,
and premature media publication. The implementation keeps the existing manifest,
artifact, take-graph, and final-media verification rules.

## Reproduced Failures And Changes

| Trigger | Previous Behavior | Corrected Behavior |
| --- | --- | --- |
| Remove a recording and refresh the card | Cached card inventory still lists the deleted recording | Refresh rereads the mounted card |
| Refresh after a card disappears or a catalog request fails | A later call returns a previous successful result | Failed refresh invalidates the old result |
| One card manifest is incomplete or invalid | The entire card becomes undiscoverable | The rejected directory remains unavailable with its reason; independent valid takes remain usable |
| A take loses its predecessor or repeats an identity | Whole-card discovery aborts | The affected take is rejected while unrelated takes remain available |
| A malformed directory name equals a healthy recording ID | Unavailable placeholders can collide with selectable IDs | Rejected directories have separate opaque identifiers |
| The first selected recording fails | Later healthy recordings are never attempted | The TUI continues, reports successes and failures, and retains only failed selections for retry |
| A selected recording disappears or becomes unavailable | The whole selected batch aborts | Continuing batches record that selection as a failure and attempt the rest |
| A GET returns HTTP 503, closes the socket, or truncates its body | Discovery or export fails immediately; some read exceptions escape normalization | Up to three complete attempts recover transient errors, with bounded backoff |
| Artifact destination already exists | Error cleanup deletes the preexisting file | Cleanup only removes a file created by that attempt |
| Final hash, frame inspection, or completion callback raises | `recording.mp4` has already been published and can block a later upgrade attempt | All checks and the callback finish inside media staging, before publication |
| Refresh a manually connected device while mDNS is unavailable | The connected device disappears from the interface | Refresh probes successful manual addresses as well as automatic discovery |

## Contracts And Limits

- `export()` keeps its default fail-on-first-error contract. The additive
  `continue_on_error=True` option collects SDK and filesystem errors per recording
  in `ExportResult.failed_sessions`. Each `ExportFailure` has `session_id` and
  `error`; successful exports remain in `sessions`.
- Source discovery, catalog parsing, and source/output relationship errors still
  raise. An export always refreshes its inventory before resolving selections.
- The legacy `read_sessions()` call stays strict by default. The card SDK uses
  its optional error callback to isolate rejected directories and invalid takes.
  Linked recordings are validated together; missing predecessors, duplicates,
  device mismatches, and invalid sequence order are not accepted.
- HTTP 408, 429, 500, 502, 503, and 504, connection errors, and interrupted reads
  may retry. There are three total attempts, with 0.15 s and 0.30 s backoffs and
  the configured socket timeout on each attempt. A retry downloads the affected
  response from the beginning; it does not resume unverified partial content.
- Authentication rejection, invalid contracts, digest mismatch, and local write
  errors are not transport retries. Exhausted retries clean up the staging tree
  and preserve the ability to start a new export.
- No new production dependency or general workflow framework is introduced.

## Verification

The final run passed **201 tests in 24.08 s**, adding 26 cases after the
175-test TUI rewrite baseline. Both the wheel and source distribution build
successfully. Ruff passes for the SDK, TUI, and affected tests; `git diff --check`
also passes. `main.py` retains one preexisting ISC004 warning in the unrelated
FFmpeg filter argument list, verified against the branch's original HEAD.

Regression coverage is in `tests/test_workflow_stability.py`,
`tests/test_integrated_sdk.py`, `tests/test_publication.py`, and `tests/test_tui.py`.
The HTTP tests use a local Device API server that can return transient/permanent
statuses, close sockets, and truncate descriptor, catalog, manifest, and artifact
responses. Existing media tests invoke real FFmpeg with generated recordings.

```bash
uv run --locked pytest -q
uv build
```

Hardware acceptance remains necessary for real card removal, a physical Device
API source, and long recordings. Local HTTP fault injection and generated media
do not establish behavior for every firmware or storage device.
