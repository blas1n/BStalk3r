# Pre-registration: R5 tie-break (20-day average dollar volume) — prospective test

- Registered: 2026-09-29, before the first live run with the rule
- Rule change: `src/mr_live.py` `mr_decisions` and `trailing_avg_dollar_vol`, wired in
  `src/main.py` `cmd_mr_trade`
- Simulator: `research/tiebreak/` (commit of this PR). Rebuild and run steps are in its README.
- Status: **OPEN**. Fill in the Result section only after the window closes.

## Background

The live RSI-2 selector uses a simple-average RSI-2. It is exactly 0 after two down days,
so about 437 liquid names tie at RSI = 0 every day. Before this change, `mr_decisions` picked
among the ties in Python set order, which made the pick effectively random on each run.

## Hypothesis

Among candidates tied on RSI-2, admitting the ones with the **largest 20-day average dollar
volume first** (R5) produces a higher portfolio return than random tie-breaking (R0), with
the entry/exit rules unchanged.

R5, exactly as implemented live and in the simulator:

1. primary: simple RSI-2 ascending (unchanged)
2. secondary: mean dollar volume (close × volume) over the 20 completed sessions D-1..D-20,
   today excluded, **descending**. A session where the symbol has no bar counts as 0 (divisor
   20). A symbol with no value sorts after all symbols with one.
3. final: symbol ascending, so the order is deterministic

Entry and exit thresholds (RSI ≤ 15 in, ≥ 70 out, max hold 10), the SMA200 regime filter, the
last-session $10M liquidity filter, 20 slots, equal-weight sizing and the cash cap are all
unchanged.

## Evidence so far (in-sample and holdout, 2026-09-29 cache)

These results come from the study run on the committed simulator with 10 bp per leg and R0
= 300 random seeds. "pct" is R5's percentile rank within the R0 total-return distribution.

| bucket | window | R0 p5 | R0 median | R0 p95 | R5 return | R5 Sharpe | R5 pct |
|---|---|---|---|---|---|---|---|
| train | 2025-04-25..2026-03-03 | −3.4% | +8.7% | +27.7% | **+21.4%** | 1.34 | 88th |
| test | 2026-03-04..2026-06-15 | −7.9% | +0.0% | +8.1% | **+7.9%** | 1.15 | 94th |
| holdout | 2026-06-16..2026-09-25 | −7.3% | +1.2% | +8.5% | **+10.9%** | 1.55 | 97th |
| paper window | 2026-07-01..2026-09-25 | −4.0% | +1.7% | +8.1% | **+6.6%** | 1.18 | 90th |

Caveats that are part of this registration:

- **The pre-registered selection rule did not pick R5.** Five rules (R1 symbol alpha, R2
  Wilder RSI-2, R3 3-day decline, R4 distance above SMA200, R5) were screened. The rule
  (beat the R0 median on train and on test, then take the best combined train+test Sharpe)
  selected **R2**, which **failed the holdout at the 25th percentile** (−1.8%). So no rule
  beat random under the selection rule.
- R5 was 88th–97th percentile in every bucket. It is being adopted by founder decision and
  has not passed a statistical test. The holdout and paper windows overlap.
- Multiple testing: 5 rules × 4 buckets were examined. R5's consistency may be luck across
  20 looks, and this prospective test exists because of that.

## Prospective test

- **Window:** from the first live `mr-trade` run after this change deploys (expected
  2026-09-30) through **2026-12-31**, inclusive.
- **Start equity:** the paper account equity at the close of the session immediately before
  the first live run in the window, read from Alpaca portfolio history at evaluation time
  (the same source as the end value; the DB has no equity snapshot). Cross-check it against
  the `equity $…` value printed by that first run in `logs/trade-<date>.log`.
- **Live return:** paper account equity at the last close in the window divided by the start
  equity, minus 1. Take both values from Alpaca portfolio history, and net out any deposits or
  withdrawals.
- **Simulation:** after the window closes, rebuild the panel from a fresh grouped-cache copy
  and run
  `tiebreak.py --window <first live run date> 2026-12-31 --seeds 300 --cost 0.001 --live-return <live return>`
  with the simulator exactly as committed in this PR.

### Pass criteria (fixed now)

**PASS** requires both of the following:

- (a) the live paper account's return over the window is ≥ the **75th percentile** of the
  300 random-tie-break (R0) simulations over the same window at 10 bp/leg, **and**
- (b) the R5 simulation over the same window is also ≥ that 75th percentile. This is the
  sim/live consistency check.

Any other outcome is a **FAIL**. The 75th percentile is `np.percentile(R0_returns, 75)`, as
printed by `--window` mode.

### Rules during the window

- No parameter changes to the strategy (thresholds, slots, sizing, filters, the tie-break
  key) during the window. Bug fixes that do not change decisions are allowed and must be
  logged here.
- Any gap (missed run, `DRY_RUN` flip, manual trade) is logged here with its date. The
  window is not shortened or restarted.

### Known sim ↔ live differences (accepted in advance)

- The simulator starts flat. The live account starts the window holding positions entered
  under random tie-breaking, and those roll off within about 10 sessions.
- The simulator fills at the session close. Live fills with limit orders at the ~15:30 ET
  snapshot price.
- The simulator uses split-adjusted closes. Live uses the grouped cache plus the Alpaca
  snapshot price.

## Addendum A (2026-09-29, before the window opened): hold-day alignment (#48)

Deployed before the first live run of the window (2026-09-30 04:30 KST). Live `mr-trade`
counted held days only from grouped-cache sessions, and at the ~15:30 ET run the cache ends
at the previous session, so "max hold 10" force-exited on session D+11 (the paper DB shows
11 closed positions held 11 sessions, none longer). The backtest (`mean_reversion_trades`,
`bt.all_signals`) exits at bar i+10, and the simulator modelled live's behaviour with
`HOLD_LIVE = 11`. Live now counts the session being traded (`_held_days`: entry session D is
day 0, force exit on D+10). The simulator is changed in the same commit to `HOLD_LIVE = 10`
(and `bt.portfolio` `hold_live = HOLD`), so live, the simulator and the backtest share one
definition. This is a bug fix to the stated rule (max hold 10), not a parameter change. The
prospective evaluation uses the simulator as of this addendum. Re-running the study on the
same 2026-09-29 cache (300 seeds; hold 11 reproduces the table above exactly) gives, at
hold 10, R5 return / Sharpe / pct: train +17.1% / 1.09 / 82nd, test +6.5% / 0.97 / 89th,
holdout +11.1% / 1.60 / 98th, paper window +6.4% / 1.15 / 86th (R0 medians +9.1%, +0.1%,
+0.5%, +1.7%). The selection rule still picks R2. The conclusions above do not change.

## Result

_To be filled after 2026-12-31: R0 distribution, R5 return and pct, live return and pct,
verdict._
