# BStalk3r — Retrospection-Grade Data Model (2026-06-10)

## Problem
Current accumulation logs *what we did* (signals/orders/positions) + *what ran*
(screened universe). That alone can't drive retrospection of parameters/strategy.
To answer **"would different parameters have done better?"** we need three layers:

1. **Provenance** — every decision attributable to the exact parameter set + code.
2. **Outcome** — what actually happened to screened/entered names afterward.
3. **Replay** — re-simulate alternate parameters offline against stored data.

The pure rule modules (`scanner`/`strategy`/`risk`) were kept side-effect-free
precisely so the replay harness can reuse them unchanged.

## Schema

### Layer 1 — provenance (Lift 1, folded into PR #3)
**param_sets** — dedup by hash, one row per distinct config
- `id`, `hash` UNIQUE (sha256 of canonical params json), `params_json`, `created_at`

**runs** — one row per process invocation
- `id` (run_id), `started_at`, `mode` (scan|run|once), `universe_source`,
  `dry_run`, `param_set_id` FK, `git_commit`, `notes`

Add `run_id` (nullable) to **signals / orders / positions / screened**. Every
decision is then attributable: row → run → param_set. Changing a threshold makes
a new param_set; old data stays correctly tagged.

### Layer 2 — outcome (Lift 2)
**outcomes** — forward results per screened observation
- `id`, `screened_id` FK, `symbol`, `base_date` (runner's session_date),
  `horizon` ('1d'|'3d'|'5d'|'intraday'), `ref_price`, `fwd_price`,
  `fwd_return_pct`, `max_gain_pct`, `max_drawdown_pct`, `computed_at`
- UNIQUE(screened_id, horizon)

Filled by `bstalk3r track`: for screened runners ≥1 session old lacking outcomes,
fetch forward daily bars (Polygon aggregates — free tier covers historical) and
compute returns + max gain/MDD. Idempotent. (bloasis forward-tracking pattern.)
Raw bars are NOT stored in v1 — the tracker/replay re-fetch + cache on demand;
`outcomes` holds the computed summary to keep the DB lean.

### Layer 3 — replay / backtest (Lift 3)
`bstalk3r replay --params <overrides>`: load stored screened runners + their
forward price paths, re-run `scanner`/`strategy`/`risk` under the alternate
param set, output comparative metrics (entry count, win rate, avg fwd return,
max-gain capture, MDD). Compare param sets side by side. (bloasis `grid run`.)

## Lifts (sequential PRs)
- **Lift 1 — provenance** (this branch / PR #3): param_sets + runs + run_id
  stamping. Merge as "screened universe + provenance".
- **Lift 2 — outcomes**: `outcomes` table + `bstalk3r track` + Polygon forward
  bars. Then the launchd schedule runs `scan`/`run` intraday + `track` post-close.
- **Lift 3 — replay**: offline re-simulation harness over stored data.

## Notes
- Free Polygon: intraday snapshot = 403 (paid); grouped daily + historical
  aggregates = free. Outcomes/replay use the free aggregates endpoint.
- Live trading is a future goal — paper-only guard stays a closed gate.
- Forward-return realism: daily bars give 1/3/5d returns + daily-H/L max-gain/MDD;
  exact intraday exit-timing replay needs minute bars (fetch on demand for
  entered names only).
