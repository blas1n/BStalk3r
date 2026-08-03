"""Measure the live Alpaca paper account and print a concise Markdown report.

Reads ALPACA_API_KEY / ALPACA_SECRET_KEY from the process env (loaded from .env by
the wrapper). Pulls the equity curve (portfolio history), fills + fill-rate, closed
round-trips (FIFO), and open positions/cash, and compares to the RSI-2 backtest
(+0.32%/trade, 63% win, Sharpe ~0.72). Pure stdlib + alpaca-py, no local DB needed
so it stays valid wherever the keys are. Output is the report text on stdout.
"""

from __future__ import annotations

import os
import statistics
from collections import defaultdict, deque
from datetime import UTC, datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

BACKTEST = "backtest ref: +0.32%/trade · 63% win · Sharpe ~0.72 (net 10bps/leg, 2yr)"


def _client() -> TradingClient:
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    return TradingClient(key, sec, paper=True)


def main() -> None:
    c = _client()
    out: list[str] = []
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"📊 *BStalk3r paper weekly* — {now}")

    # 1) equity curve
    ph = c.get_portfolio_history(GetPortfolioHistoryRequest(period="3M", timeframe="1D"))
    eq = [e for e in (ph.equity or []) if e]
    if len(eq) >= 2:
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1]]
        std = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        sharpe = (statistics.mean(rets) / std) * (252**0.5) if std else 0.0
        peak = eq[0]
        mdd = 0.0
        for x in eq:
            peak = max(peak, x)
            mdd = min(mdd, x / peak - 1)
        out.append(
            f"\n*Equity* {len(eq)}d: ${eq[0]:,.0f} → ${eq[-1]:,.0f} "
            f"({(eq[-1] / eq[0] - 1) * 100:+.2f}%)\n"
            f"vol {std * (252**0.5) * 100:.1f}% · Sharpe {sharpe:+.2f} · maxDD {mdd * 100:+.1f}%"
        )
        if len(eq) < 60:
            out.append(f"⚠️ {len(eq)}d small sample — expect regression toward backtest")

    # 2) fills + round-trips (FIFO)
    all_orders = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
    orders = [o for o in all_orders if str(o.status.value) == "filled" and o.filled_avg_price]
    lots: dict[str, deque] = defaultdict(deque)
    realized: list[float] = []
    for o in sorted(orders, key=lambda x: x.submitted_at):
        qty = float(o.filled_qty)
        px = float(o.filled_avg_price)
        if o.side.value == "buy":
            lots[o.symbol].append([qty, px])
        else:
            remain = qty
            while remain > 1e-9 and lots[o.symbol]:
                lot = lots[o.symbol][0]
                take = min(remain, lot[0])
                realized.append((px - lot[1]) / lot[1])
                lot[0] -= take
                remain -= take
                if lot[0] <= 1e-9:
                    lots[o.symbol].popleft()
    fill_rate = len(orders) / max(1, len(all_orders)) * 100
    out.append(f"\n*Fills* {len(orders)}/{len(all_orders)} ({fill_rate:.0f}%)")
    if realized:
        wins = sum(1 for r in realized if r > 0) / len(realized) * 100
        out.append(
            f"*Round-trips* {len(realized)}: avg {statistics.mean(realized) * 100:+.2f}% · "
            f"med {statistics.median(realized) * 100:+.2f}% · win {wins:.0f}%"
        )
    out.append(f"_{BACKTEST}_")

    # 3) positions / cash (margin check)
    a = c.get_account()
    pos = c.get_all_positions()
    upl = sum(float(p.unrealized_pl) for p in pos)
    out.append(
        f"\n*Positions* {len(pos)} open · equity ${float(a.equity):,.0f} · "
        f"cash ${float(a.cash):,.0f} · long ${float(a.long_market_value):,.0f} · uPnL ${upl:+,.0f}"
    )
    if float(a.cash) < 0:
        out.append("⚠️ negative cash = margin (self-normalizes as positions exit)")

    print("\n".join(out))


if __name__ == "__main__":
    main()
