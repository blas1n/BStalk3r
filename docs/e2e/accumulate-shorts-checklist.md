# E2E: accumulate-shorts (short-strategy forward dataset)

Since Alpaca can't short our target runners, accumulate a forward OOS dataset of
would-be short outcomes + live shortable status instead of paper-trading.

- [ ] `--help` lists `accumulate-shorts` with `--date/--run-days/--run-gain/--fade-trigger/--exh-trigger/--exh-mode`
- [ ] Runs on a real recent session without crashing on Polygon free-tier 429s
- [ ] Detects H-A fade crossers AND H-B exhaustion run-ends for the session
- [ ] Reports `shortable at Alpaca: N/total` — expected ~0 (the executability wall)
- [ ] Persists rows to `short_setups` (count grows), idempotent on re-run (no dupes)
- [ ] Records entry-time features + would-be short outcome per triggered setup
- [ ] No real orders placed (paper-safety validated; this only reads Alpaca assets)
