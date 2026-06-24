# BStalk3r

**Low Float Momentum Runner** research system on **Alpaca Paper Trading**.

Detects intraday low-float / high-RVOL runners, enters with rule-based limit
orders, manages exits (stop / scale-out / trailing / time / spread), and logs
**every** signal, order, position and P&L to SQLite. Realtime decisions are
100% rule-based and synchronous — **no LLM / AI is ever called in the trading
loop** (speed + determinism). AI is reserved, optionally, for *post-market*
reports only.

> ⚠️ **Research / paper-trading only.** Live trading is unsupported *by design*:
> the app refuses to start unless it is pointed at the Alpaca paper endpoint
> with `PAPER=true`. Orders go to a paper account; nothing here can place a
> live order.

---

## Architecture

```
            ┌──────────────┐   snapshots   ┌────────────┐
 universe ─▶│ market_data  │──────────────▶│  scanner   │ filter + rank (pure)
            │ (Alpaca/IEX) │               └─────┬──────┘
            └──────────────┘                     ▼
                                           ┌────────────┐
                                           │  strategy  │ entry / exit rules (pure)
                                           └─────┬──────┘
                                                 ▼
                                           ┌────────────┐  veto
                                           │    risk    │ sizing + limits (pure)
                                           └─────┬──────┘
                                                 ▼
            ┌──────────────┐   limit order  ┌────────────┐
            │ alpaca_client│◀───────────────│ execution  │ dry-run aware
            └──────────────┘                └─────┬──────┘
                                                  ▼
                                           ┌────────────┐
                                           │  database  │ SQLite audit trail
                                           └─────┬──────┘
                                                 ▼
                                           ┌────────────┐
                                           │  reporter  │ end-of-day report
                                           └────────────┘
```

- **Pure, fully-tested rule modules** (`scanner`, `strategy`, `risk`) depend only
  on plain dataclasses — never on the Alpaca SDK — so they are fast and trivially
  unit-tested.
- **SDK is isolated** in `alpaca_client` (trading) and `market_data` (quotes/bars).
  Swap in Polygon/Finnhub/Nasdaq Data Link later by implementing
  `MarketDataProvider` / `FundamentalsProvider`; nothing else changes.

---

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or plain `pip`).

```bash
# 1. install
uv venv && uv pip install -e ".[dev]"
#    (pip:  python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]")

# 2. configure — copy the template and paste your PAPER keys
cp .env.example .env
#    edit .env -> ALPACA_API_KEY / ALPACA_SECRET_KEY  (from the Paper dashboard)

# 3. confirm the paper account connects
uv run bstalk3r check
#    ✅ Paper account connected | equity=100000 buying_power=... dry_run=True

# 4. scan the watchlist once (logs signals to SQLite, places nothing)
uv run bstalk3r scan

# 5. run the realtime loop — DRY_RUN=true by default: evaluates + logs, no orders
uv run bstalk3r run            # ctrl-C force-closes open positions
uv run bstalk3r run --once     # single tick then exit (handy for testing)

# 6. end-of-day report (markdown + json under reports/)
uv run bstalk3r report                 # today (UTC)
uv run bstalk3r report --date 2026-06-10

# 7. backfill forward outcomes on screened runners (research fuel)
uv run bstalk3r track                   # runners >= OUTCOME_LAG_DAYS old
uv run bstalk3r track --before 2026-06-01 --limit 50

# 8. replay alternate parameters over the accumulated data (retrospection)
uv run bstalk3r replay --horizon 3d
uv run bstalk3r replay --horizon 3d --sweep-min-rvol 4,8,12,16

# 9. intraday hit-and-run backtest over minute bars (the real strategy shape)
uv run bstalk3r intraday --limit 40 --sweep-max-hold 15,30,60
```

### Getting Alpaca paper keys
Log in at <https://app.alpaca.markets/>, switch to the **Paper** account, open
**"View API Keys"**, generate a key pair, and paste both into `.env`. Keys are
never committed (`.env` is git-ignored; only `.env.example` is tracked).

---

## Dry-run & safety

| Guard | Behaviour |
|---|---|
| `DRY_RUN=true` (default) | Rules run and every decision is logged, but **no order is sent** — orders are stored with status `dry_run`. |
| `PAPER=true` (required) | App **refuses to start** if `false`. |
| `ALPACA_BASE_URL` | Must be the paper host (`paper-api.alpaca.markets`); a live URL is rejected at startup. |
| Risk gate | New entries are vetoed on max-positions, daily-trade cap, daily-loss limit, or any data error. |
| Data outage | A failed market-data fetch marks the loop "unhealthy" and **halts new entries** (open positions are still managed). |

Flip `DRY_RUN=false` only when you want the loop to actually place **paper**
orders. There is no live path.

---

## Strategy (v0)

**Entry** (all must hold, position not already open):
price ∈ `[$1, $50]`, day change ∈ `[5%, 40%]`, `RVOL ≥ 8`,
`volume_acceleration ≥ 3`, spread within `MAX_SPREAD_PCT`.

**Exit** (priority order): force-close near the bell → stop-loss `-5%` →
max-hold `30 min` → spread blow-out → trailing stop `-8%` from peak →
first take-profit at `+15%` scales out `50%`.

**Sizing / risk:** at most `1%` of equity at risk per trade (bounded by the
`-5%` stop distance), capped by `MAX_POSITION_VALUE`; `1–3` concurrent
positions; halt new entries at `-3%` on the day or after `MAX_DAILY_TRADES`.

All thresholds live in `.env` (see `.env.example`) and are loaded via
`pydantic-settings`.

### `float` / market cap
No reliable **free** source provides share float, so `float_shares` /
`market_cap` are **nullable** and the v0 rules work without them. The
`FundamentalsProvider` interface is ready for a paid source later.

---

## Universe source (watchlist vs Polygon screener)

`UNIVERSE_SOURCE` picks where candidates come from:

| Mode | What it does | Data | Cost |
|---|---|---|---|
| `watchlist` | Evaluates the fixed `UNIVERSE` list via Alpaca/IEX | intraday | free |
| `polygon` + `POLYGON_INTRADAY=false` | Screens the **whole US market** for the **prior session's** runners via Polygon grouped daily bars (one call, ~12k tickers, cached per day) | **end-of-day** | **free** |
| `polygon` + `POLYGON_INTRADAY=true` | Screens **intraday** top gainers via Polygon's snapshot endpoint | intraday (15-min delayed) | **paid** (Stocks Starter+) |

**Free Polygon = EOD discovery.** The free tier returns `403` on the intraday
snapshot endpoint, so grouped mode surfaces *yesterday's* runners — a research /
next-day watchlist. Grouped bars carry no intraday quote or per-minute volume,
so `volume_acceleration=1.0` and `spread_pct=0`, and the strict entry filter
(RVOL≥8 **and** vol-accel≥3) stays at zero by design — intraday entries need the
intraday feed. Example free-tier `scan`:

```
Screened 50 symbols via polygon -> 0 entry-ready candidate(s).
Top screened runners:
  [ ] CCTG   $  1.78  chg= 271.5%  rvol=  3.7  vacc=1.0  spread=0.00%
  [ ] RGNT   $  2.41  chg=  88.3%  rvol=  9.2  vacc=1.0  spread=0.00%
  ...
```

To trade these intraday, upgrade Polygon and flip `POLYGON_INTRADAY=true` — no
code change. Both paths implement `SnapshotSource` in `src/sources.py`.

## Data feed limitations (free / IEX)

The free Alpaca feed is **IEX**, used for `watchlist` metrics and for pricing
held positions during exits. Consequences for v0:

- IEX cannot screen the whole market (hence the Polygon screener above for
  discovery) and is thinner than SIP.
- `RVOL` (watchlist mode) = today's cumulative volume ÷ 20-day average daily
  volume; it **understates** RVOL early in the session. Good enough as a proxy.
- Set `DATA_FEED=sip` only if your account is subscribed to SIP.

---

## Scheduled accumulation (launchd, macOS)

The point of the data layers is to **accumulate daily**. A launchd agent runs
`scan` + `track` each weekday morning so the runner universe and forward outcomes
build up unattended.

```bash
# install / refresh the agent for the current checkout (renders paths, loads it)
bash scripts/install-launchd.sh

# run the job once now to verify
bash scripts/daily.sh
tail -f logs/daily-$(date +%Y%m%d).log

# is it registered?
launchctl list | grep bstalk3r

# remove it
bash scripts/install-launchd.sh --uninstall
```

- Schedule: **Tue–Sat 09:00 host-local** — captures each US session (Mon–Fri
  close) the next morning, once free-tier EOD data is available.
- Job: `bstalk3r scan` (persist the latest session's screened runners) then
  `bstalk3r track` (backfill outcomes for runners ≥ `OUTCOME_LAG_DAYS` old).
- Runs against this checkout's `.env` and `data/bstalk3r.db`; logs to `logs/`.
- Once enough data has accumulated, `bstalk3r replay --sweep-min-rvol …` turns it
  into parameter decisions.

When you move to a paid intraday Polygon plan, add a market-hours `bstalk3r run`
agent for live (paper) dry-run entries; the schedule template is the model.

---

## Development

```bash
uv run pytest --cov=src --cov-fail-under=80   # tests + coverage gate
uv run ruff check src/ tests/                 # lint
uv run ruff format src/ tests/                # format
```

The pure rule modules are exhaustively unit-tested; the loop wiring is covered
with Alpaca mocked at the boundary. The SDK glue + live commands are verified
against a paper account via `docs/e2e/low-float-runner-checklist.md`.

A `.devcontainer/` is provided for a reproducible Python 3.11 environment.

## SQLite schema

`param_sets`, `runs`, `screened`, `outcomes`, `signals`, `orders`, `positions`,
`daily_stats` — the full audit trail, built so the data supports **retrospection
of parameters/strategy**, not just record-keeping:

- **`screened`** — longitudinal research dataset: **every** screened runner (not
  just entry-ready ones) upserted per `(session_date, symbol, source)`, stamped
  with the trading date the data represents.
- **`runs` + `param_sets`** — provenance. Every run records the exact parameter
  set (hashed, deduped) + git commit; every signal/order/position/screened row
  carries `run_id`. So you can group outcomes by parameter set and answer "which
  thresholds produced this?" after changing them.
- **`outcomes`** — forward results per screened runner: `bstalk3r track` fetches
  the following sessions' daily bars (Polygon, free historical) and records
  +1d/+3d/+5d return, max gain, and max drawdown vs the runner's close. This is
  the *fuel* for judging whether a parameter set would have caught the winners.
  Free-tier bars lag, so `track` only processes runners ≥ `OUTCOME_LAG_DAYS` old
  and backfills as data appears (idempotent).

**Replay (retrospection).** `bstalk3r replay` re-simulates alternate parameter
sets over the accumulated `screened` + `outcomes` data — reusing the *live*
`scanner.passes_filters`, so it can never drift from real entry logic. It reports,
per variant, how many runners would have been entered and how that set actually
did (avg / median forward return, win rate, avg max gain, avg max drawdown).
Sweep a threshold to compare:

```
$ bstalk3r replay --horizon 3d --sweep-min-rvol 8,12,18
Replay over 5 screened runner(s) @ horizon 3d
  variant           enter    score     avg%     med%     win%   maxgn%     mdd%
  baseline              1        1     14.0     14.0    100.0     16.3     -1.0
  rvol>=12              1        1     14.0     14.0    100.0     16.3     -1.0
  rvol>=18              0        0        —        —        —        —        —
```

Replay gates on the fields EOD data has (price / day-change / rvol) and leaves
the intraday-only gates (vol-accel / spread) permissive; once intraday data is
accumulated (paid Polygon), those become sweepable too. Design notes:
`~/Docs/BStalk3r/Retrospection_Data_Model_2026-06-10.md`.

**Costs are on by default.** Metrics are *net* of a round-trip transaction-cost
assumption (`REPLAY_COST_PCT`, default 2%, plus a surcharge for sub-`$REPLAY_CHEAP_PRICE`
names — low-float runners have brutal spreads). This is a research assumption,
not a measured spread (grouped EOD has no quote). Use `--cost-pct X` to override
or `--gross` to ignore costs. Costs matter: a thin gross edge on these illiquid
names usually goes net-negative — the cost model keeps the retrospection honest.

**Intraday hit-and-run** (`bstalk3r intraday`). The EOD replay above measures a
*different* strategy (buy-at-close, hold N days); the real strategy is intraday
"hit and run". This backtests it on **free historical minute bars**: for each
screened runner it reconstructs the entry (first minute the day-change crosses
the trigger, prior close backed out of `day_change_pct`) and walks the minute
bars applying the **live** `strategy.evaluate_exit` (stop / take-profit /
trailing / max-hold / force-close-at-bell), swept across max-hold windows, net of
costs:

```
$ bstalk3r intraday --limit 40 --sweep-max-hold 15,30,60
Intraday hit-and-run over 40/40 runner(s) with minute data (entry +5%, net of 2%+cheap round-trip):
  max_hold   trades     avg%     med%     win%  avgHold   exits
        15       40     -0.9     -1.1     47.5     12.1   max_hold:23, stop_loss:11, take_profit_scale:3, ...
        30       40     -0.1     -0.0     47.5     19.4   max_hold:20, stop_loss:12, take_profit_scale:4, ...
        60       40      0.8      0.2     52.5     33.8   max_hold:17, stop_loss:13, take_profit_scale:5, ...
```

That first real sample is telling: buy-and-hold-N-days was deeply net-negative
(−2 to −5%), but **intraday hit-and-run is ~breakeven net** and *improves* with a
longer cap (60-min +0.8% / 52.5% win) — the strategy *shape* looks right, while
exiting too fast (15-min) hurts. Promising, not proven: n=40, one ~2-week regime,
survivorship-optimistic.

Caveats: survivorship-optimistic (only stocks already known to have run that day);
fills modeled at the triggering bar's close; take-profit is a full exit (no
partial scale-out) in v1. It answers *execution-given-detection* — if hit-and-run
doesn't profit even with hindsight entry timing, paying for live intraday
detection isn't worth it. Design: `~/Docs/BStalk3r/Retrospection_Data_Model_2026-06-10.md`.

Inspect with any SQLite client:

```bash
sqlite3 data/bstalk3r.db ".tables"
sqlite3 data/bstalk3r.db "SELECT session_date, symbol, day_change_pct, rvol, entry_ready FROM screened ORDER BY session_date DESC, day_change_pct DESC LIMIT 20;"
sqlite3 data/bstalk3r.db "SELECT symbol, status, limit_price FROM orders ORDER BY id DESC LIMIT 10;"
```
