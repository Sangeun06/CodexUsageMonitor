# Codex usage accounting (collector 0.7.5)

Reference: https://github.com/ccusage/ccusage/blob/main/rust/adapters/codex/src/README.md

- Prefer `last_token_usage`; recover missing request counts by subtracting cumulative component counters. A downward cumulative correction resets the baseline. Do not subtract cache or add reasoning to the reported total.
- Cache belongs to input; reasoning belongs to output. Preserve absent components as unknown. Server rejects contradictory component totals.
- Suppress repeated cumulative snapshots and repeated timestamp/counter fingerprints. Keep baselines, dedupe state, execution settings, and pending compaction matches across incremental reads and collector restarts.
- Use each request's recorded model, effort and service tier. Settings without a tier preserve the last recorded tier; explicit unsupported tiers clear it. `priority`/`fast` normalize to Fast, `default`/`standard` to Standard. Current config is not evidence of historical request settings.
- Probe the actual replay boundary, without relying on the session CLI version. Exclude history preceding `task_started` or `inter_agent_communication[_metadata]` with `trigger_turn=true`. For legacy forks with an available local parent, compare leading numeric usage with the parent's pre-fork history.
- Match compaction usage and markers by hashed response ID in either order. Do not add local snapshots that already cover the compaction request. Preserve the request timestamp and settings. Copied parent compaction IDs are excluded for legacy fork matching.
- Parser revision 2 rebuilds the last 14 days using existing account continuity intervals. Incomplete or truncated backfills do not replace stored detail rows. Complete reports atomically replace corrected session details and minute counters, including lower totals and empty replay-only histories. In that coverage interval, period totals use corrected events instead of raw session lifetime snapshots.
- Reject older collector versions after revision 2 reports arrive; reject out-of-order report timestamps. Preserve account/node/session identity boundaries.
- API-equivalent cost estimates and provider quota percentages remain distinct. Observed attribution plus calibrated estimates plus unallocated quota equals the provider total. Estimates never expand to fill unallocated usage. Estimates exceeding the provider meter remain visible as an estimation excess; their conversion denominator is not silently rescaled. Display rounded tenths with a largest-remainder allocation; retain full precision internally.

## Limits

The collector reads local plain JSONL paths registered in the Codex state database. Compressed `.jsonl.zst` rollouts and legacy forks with unavailable parent files do not have full ccusage parity. Parent prefix matching requires an explicit parent identity; no timing heuristic is used to discard potentially genuine requests. Missing model or component data remains unknown. Compaction and request dedupe caches are bounded to 256 entries per rollout.

## Validation

`python3 -m unittest discover -s tests -q`

Regression cases cover cumulative-only records, repeated snapshots, resets, incomplete tails, cross-chunk settings, both subagent marker spellings, false triggers, legacy forks, local/remote compaction ordering, delayed snapshots, nested cache/reasoning details, lower corrected totals, incomplete backfills, stale reports, and unallocated quota preservation.
