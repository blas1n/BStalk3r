# E2E: split-adjusted backtest closes (#47)

Backtest commands (`mrsearch`, `mrportfolio`, `xsearch`, `calsearch`, `sentsearch`)
back-adjust grouped-cache closes from Polygon `/v3/reference/splits` events.
Run against a **copy** of `data/grouped_cache.db`; never the live file.

- [x] `/v3/reference/splits` answers on the current Polygon plan (read-only GET, HTTP 200)
- [x] A 2-year range (2024-09-25..2026-09-25) fetches in a few pages and lands in `splits_cache.db`
- [x] A second run the same day serves the range from disk (no API call)
- [x] MUU / KORU 2026-07-14: raw −94.5% / −94.3% → adjusted +9.5% / +14.8% (real same-day move kept)
- [x] LABX 2026-07-20: raw −82.6% → adjusted +4.4%
- [x] KORU 2025-02-10 and LABX 2026-03-10 splits (rows backfilled after the split, already adjusted) are **not** applied twice
- [x] CDTX 2025-11-14 +105% (no split event) is untouched
- [x] Each command prints `splits: N applied`; without a source it prints `splits: UNADJUSTED`
- [x] `mr-trade` (live) is unchanged: no split source, raw closes into RSI-2/SMA200
- [x] No orders placed; `mr-trade` / `trade.sh` not run
