# E2E checklist — Low Float Runner (paper)

Non-web project: verify each item manually against an Alpaca **paper** account.
Unit tests cover the pure rules + loop wiring (Alpaca mocked); this checklist
covers the live SDK glue that unit tests deliberately do not hit.

## Setup
- [ ] `cp .env.example .env`, paste **paper** `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`
- [ ] `uv venv && uv pip install -e ".[dev]"` succeeds
- [ ] `.env` is git-ignored (`git status` shows no `.env`)

## Safety guards (no network needed)
- [x] `PAPER=false ... bstalk3r check` exits non-zero with a refusal message *(verified: exit 2)*
- [x] `ALPACA_BASE_URL=https://api.alpaca.markets ... bstalk3r check` is rejected *(verified: exit 2)*
- [x] `bstalk3r --help` lists `check / scan / run / report` *(verified)*

## Connectivity
- [ ] `bstalk3r check` prints "✅ Paper account connected" with equity + buying power
- [ ] Account `status` is ACTIVE

## Scan
- [ ] `bstalk3r scan` runs without error during market hours
- [ ] Output lists `UNIVERSE` symbols evaluated and any candidates
- [ ] `sqlite3 data/bstalk3r.db "SELECT * FROM signals;"` shows logged signals with `reason_json`

## Dry-run loop (DRY_RUN=true)
- [ ] `bstalk3r run --once` completes a tick with no exception
- [ ] No order reaches the broker; `orders` rows (if any) have status `dry_run`
- [ ] If a candidate qualifies, a `positions` row is opened and a `signals` row written
- [ ] Ctrl-C on `bstalk3r run` triggers force-close logging for open positions

## Live paper orders (DRY_RUN=false) — paper account only
- [ ] A qualifying candidate produces a **limit** BUY (never market) on the paper account
- [ ] Limit price = min(ask, last × 1.003), rounded to 2 dp
- [ ] Alpaca order id is recorded in `orders.alpaca_order_id`
- [ ] An exit condition (stop / take-profit / trailing / max-hold) produces a SELL and closes the `positions` row with pnl
- [ ] Broker error (e.g. bad symbol) is recorded as status `error` and does **not** crash the loop

## Risk
- [ ] With `MAX_CONCURRENT_POSITIONS=1`, a second candidate is vetoed (`entry_vetoed` log)
- [ ] Position size never exceeds `MAX_POSITION_VALUE / price` shares
- [ ] After `MAX_DAILY_TRADES` entries, further entries are vetoed

## Report
- [ ] `bstalk3r report` writes `reports/<date>.md` and `reports/<date>.json`
- [ ] `daily_stats` row exists for the date with num_trades / win_rate / total_pnl / max_drawdown
