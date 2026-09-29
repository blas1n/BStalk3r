# E2E: `mrportfolio --ranked` ranks on the R5 order (#59)

`--ranked` admits same-day signals competing for free slots in the live selector's order:
entry RSI ascending, then 20-day average dollar volume (D-1..D-20) descending (unknown
last), then symbol. Run against a **copy** of `data/grouped_cache.db`; never the live file.
2-year window 2024-09-25..2026-09-25, split-adjusted, as-traded band, 10 bp/leg, 20 slots.

- [x] Before the fix `--ranked` and first-come print identical numbers (CAGR −2.6%, 1729
      trades): every signal sorted on the 0.0 default
- [x] After the fix `--ranked` differs from first-come (CAGR +1.2%, 1686 trades)
- [x] The R5 key equals live `trailing_avg_dollar_vol` for the entry session, including a
      symbol missing sessions inside the window
- [x] A trade without `entry_rsi` raises instead of ranking as 0.0
- [x] `mr-trade` (live), `src/mr_live.py` and `research/tiebreak/` are byte-for-byte unchanged
- [x] No orders placed; `mr-trade` / `trade.sh` not run
