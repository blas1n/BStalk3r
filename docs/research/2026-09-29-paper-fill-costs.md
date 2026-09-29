# Paper fill cost vs quoted spread: RSI-2 mr-trade (issue #49)

- Measured: 2026-09-29, read-only (Alpaca SIP historical quotes, GET only, no orders)
- Script: `research/costs/spread_at_close.py` (run steps are in its docstring)
- Input: a copy of `data/bstalk3r.db`, mr-trade orders from 2026-07-10 to 2026-09-28

## Question

Paper fills cost about **3 bp round trip** (issue #49: buy fills +0.1 bp and sell fills
+2.7 bp vs the day's close, 243 buys). The research and backtests assume **10 bp per leg
(20 bp round trip)**, and the RSI-2 edge is gone at about 30 bp round trip. What would real
fills cost on the names and at the time mr-trade actually trades?

## Method

For each mr-trade order (entries and `rsi_bounce_or_max_hold` exits), take every SIP quote
within ±30 s of the order timestamp and use the median quoted spread `(ask − bid) / mid`.
The runs are at ~15:30 ET (19:3x UTC); the 2026-07-10 pre-market run is excluded. A
marketable order pays about half the spread per leg, so one round trip is about one full
spread. **Market impact is not measured.** The median order is about $54k notional, and
nothing here checks that size against displayed depth.

## Result (490 orders with quotes, 54 runs)

| slice | n | median spread | mean | notional-weighted |
|---|---|---|---|---|
| all | 490 | 7.0 bp | 13.1 bp | 13.0 bp |
| buys | 256 | 6.7 bp | 13.0 bp | 12.9 bp |
| sells | 234 | 7.5 bp | 13.2 bp | 13.2 bp |
| July | 195 | 8.6 bp | n/a | 14.9 bp |
| August | 159 | 7.6 bp | n/a | 14.5 bp |
| September | 136 | 5.2 bp | n/a | 8.9 bp |

All orders (including the pre-market run): p75 16.7 bp and p90 29.9 bp.

- **Spread-only cost per leg:** about 3.5 bp (median) to 6.5 bp (notional-weighted). A round
  trip is therefore about **7–13 bp before impact**.
- Our limit prices are marketable. Relative to the mid, buy limits sit a median +7.5 bp above
  and sell limits a median 31.7 bp below. In a real account they would fill at or near the
  far touch, not at the limit.
- The paper engine's ~3 bp round trip is below even the spread-only estimate. **Paper fills
  are not realistic,** as #49 says.
- The 20 bp round-trip research assumption is **above** the spread-only estimate. It leaves
  about 7–13 bp of room for impact and slippage before it is optimistic. The edge-death level
  (~30 bp) sits above the p75 spread but below the p90 spread, so the thin tail of names is
  where the edge is at risk.

## What this does not settle

- Impact at ~$54k per order, especially in the wide-spread tail (p90 ≈ 30 bp). The next
  measurement is quote size (bid/ask size at the touch) against order size, from the same
  quotes.
- Close-auction vs 15:30 ET continuous-market fills. The backtest marks at the close, and live
  trades 30 minutes earlier.

## Reporting (the other half of #49): belongs in BStockReport, not here

BStalk3r no longer has a user-facing paper report. Its standalone weekly report
(`scripts/weekly-report.sh` / `scripts/measure_paper.py`, launchd `com.bstalk3r.report`) was
disabled on 2026-09-29, and the weekly paper report is produced by **BStockReport**, which
reads the same Alpaca paper account read-only. The acceptance item should be implemented
there:

- state next to the BStalk3r numbers that paper fills are not realistic (paper ≈ 3 bp round
  trip vs a measured 7–13 bp quoted-spread round trip before impact)
- show round trips re-priced at the backtest assumption beside the raw paper numbers: for
  each FIFO-matched round trip, `ret_repriced = sell_close_D / buy_close_D − 1 − 2 × 0.001`
  (closes of the fill sessions, 10 bp/leg). As a lighter alternative, keep the paper fills
  and subtract `2 × 0.001 − paper_cost`.
