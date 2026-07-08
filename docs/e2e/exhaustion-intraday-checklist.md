# E2E: exhaustion-intraday (H-B v2)

Realistic intraday-trigger exhaustion short — no EOD red-close selection, no
look-ahead fill. Verifies the CLI end-to-end against live Polygon free-tier data.

- [ ] `--help` lists `exhaustion-intraday` with `--start/--end/--run-days/--run-gain/--entry/--sample`
- [ ] Command runs over a real date range (2026-06-09..2026-07-02) without crashing on free-tier 429s
- [ ] Reports candidate count, sampled/triggered counts, and short-trade aggregate (avg/med/win/hold)
- [ ] Exit-reason breakdown and squeeze-tail (max adverse up-move) are printed
- [ ] Result is directly comparable to v1 `exhaustion` on the same range (bias-removed number)
- [ ] `--gross` vs net differ by the round-trip cost (sanity)
- [ ] No real orders placed (concept backtest only; paper-safety validated at startup)
