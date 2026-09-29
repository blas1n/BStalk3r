# E2E: reused tickers split at the identity break (#58)

Backtest commands (`mrsearch`, `mrportfolio`, `xsearch`, `calsearch`, `sentsearch`) rename
the earlier company's rows of a reused ticker (`PARA` → `PARA<2026-08-07` before the
break), so no series, signal or trade joins two companies. A break needs **both** a gap of
≥ 5 panel sessions in the ticker's cached series **and** a Polygon ticker event
(`/vX/reference/tickers/{t}/events`) where the current holder took the ticker inside that gap
(up to 7 days after the first new bar). Run against **copies** of the caches; never the live files.
2-year window 2024-09-25..2026-09-25, split-adjusted, as-traded band, 10 bp/leg.

- [x] `/vX/reference/tickers/{t}/events` answers on the current Polygon plan (read-only GET);
      a ticker with no current holder answers 404 → no events
- [x] PARA: Paramount rows end 2025-08-06, Banzai took `PARA` 2026-08-07 → split; the
      live-config −86.4% trade (entry 2025-07-31) is gone
- [x] B (Barnes → Barrick 2025-05-09), GOLD (Barrick → Gold.com 2025-12-02), FIG (Figma IPO
      2025-07-30), BNY, SPCX split at the gap
- [x] A gapped ticker whose holder took it before the gap (halt, thin trading) is not split
- [x] Each command prints `identity: N reused tickers split (M gapped tickers checked …)`;
      without a source it prints `identity: UNCHECKED`
- [x] A second run the same day serves every lookup from `identity_cache.db` (no API call)
- [x] `mr-trade` (live), `src/mr_live.py` and `research/tiebreak/` are byte-for-byte unchanged;
      live exposure recorded in the PR, not fixed (frozen through 2026-12-31)
- [x] No orders placed; `mr-trade` / `trade.sh` not run
