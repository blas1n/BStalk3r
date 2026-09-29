# E2E: backtest price band on the as-traded close (#55)

Backtest commands (`mrsearch`, `mrportfolio`, `xsearch`, `calsearch`, `sentsearch`) band
on the close each name printed at that session (`traded_close`) and compute signals and
returns on the split-adjusted close. Run against a **copy** of `data/grouped_cache.db`;
never the live file. 2-year window 2024-09-25..2026-09-25, 10 bp/leg.

- [x] `adjust_panel` records `traded_close` for located splits, for splits the cache had
      already restated (the symbol trades across the execution date with no jump), and for
      the as-of sessions between an early boundary and the execution date
- [x] An event in a gap of the symbol's history (ticker reuse: PARA, Paramount rows end
      2025-08-06, a new PARA's 1:20 split on 2026-05-08) leaves earlier rows as they are
- [x] Live config (ent15 ext70 ma200 hold10, $10M): 142 of 74,614 signals had an as-traded
      entry outside $5–1000 (94 below $5, 48 above $1000), mean −3.52%; after the fix none do
- [x] GOSS (−81.6%, 2026-02-13, as traded $2.29) and GAME (+52.8%, 2025-07-15, as traded
      $1.51) are gone from the |r|>50% list
- [x] Dollar volume is unchanged: close × volume from the same cached row is split-invariant,
      so the liquidity floor already reads the as-traded value
- [x] `mr-trade` (live) is unchanged: bands its raw latest close, never reads `traded_close`
- [x] No orders placed; `mr-trade` / `trade.sh` not run
