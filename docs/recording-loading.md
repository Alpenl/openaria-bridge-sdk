# Recording Loading Investigation

## Measurements

On 2026-09-08, the locally advertised Device API returned 27 recordings. Before
this change, descriptor probing took 32 ms and listing took 57 ms. Twelve further
fresh listings all succeeded in 57-65 ms. A headless 120x36 TUI run loaded all 27
recordings in 3.239 seconds, including the three-second mDNS discovery window.
The user's intermittent hardware failure was not reproduced during these reads.

## Changes

- The TUI previously passed `refresh=True` on every source selection. This
  repeated card inventory parsing immediately after discovery and bypassed the
  SDK's LAN list cache on return visits. Selection now uses the cache; explicit
  refresh and exports continue to obtain fresh inventories.
- Discovery previously probed addresses sequentially with the same default
  15-second socket timeout used for reads and downloads. With three attempts,
  a nonresponsive address could cost approximately 45.45 seconds. Probing now
  deduplicates addresses and uses up to eight workers with at most three seconds
  per socket attempt. Each address retains the existing three-attempt retry
  policy. Discovery still waits for outstanding probes, and multiple batches of
  addresses can take longer than one batch.
- A catalog revision change between pages previously caused immediate failure.
  Listing now discards that partial snapshot and restarts from page one, with a
  maximum of three attempts. Persistent changes still raise `ContractError`.
  Other contract errors are not retried and digest verification is unchanged.
- Listing completion and failures now include elapsed time in the TUI task log.
  The status line exposes the actual failure, with the full message in its
  tooltip and the task log.

## Verification

After the changes, the hardware listing took 61.195 ms, a cached read took
0.003 ms, and an explicit refresh took 62.475 ms. A controlled four-address
simulation with a 200 ms probe delay per address changed from 0.800 seconds
sequentially to approximately 0.201 seconds concurrently. This simulation is
not a measurement of real network timeout behavior.

The initial mDNS collection window remains three seconds so discovery retains
its opportunity to find multiple devices. These changes do not claim to reduce
that healthy first-start delay or establish the cause of the unobserved hardware
failure.

`uv run --locked pytest -q` passed all 206 tests in 24.88 seconds. New regression
coverage checks concurrent and deduplicated discovery, shorter configured
timeouts, consistent catalog recovery, permanent contract failure, reuse of card
discovery, and explicit refresh. The existing persistent catalog-change test also
checks that retrying stops after three complete pagination attempts.
