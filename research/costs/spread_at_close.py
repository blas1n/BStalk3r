"""Quoted-spread cost of the names mr-trade actually traded (issue #49). Read-only.

For every mr-trade order in a COPY of the BStalk3r DB, fetch the Alpaca SIP
historical quotes (GET only, no orders) within +/-`--window` seconds of the order
timestamp (~15:30 ET), and record the median quoted spread in bp of the mid. A
marketable order pays about half the spread per leg, so the round trip is about
one full spread, before any market impact.

Usage (from the repo root, credentials from .env; nothing is printed from them):
    cp data/bstalk3r.db "$WORK/bstalk3r.db"
    set -a; . ./.env; set +a
    uv run python research/costs/spread_at_close.py --db "$WORK/bstalk3r.db" \
        --since 2026-09-01 --out "$WORK/spreads.json"
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--db", required=True, help="COPY of data/bstalk3r.db (never the live file)")
ap.add_argument("--since", default="2026-09-01", help="first order date (UTC, YYYY-MM-DD)")
ap.add_argument("--window", type=int, default=30, help="+/- seconds around each order")
ap.add_argument("--out", default=None, help="optional JSON dump of per-order spreads")
args = ap.parse_args()

con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
rows = con.execute(
    "SELECT timestamp, symbol, side, qty, limit_price FROM orders "
    "WHERE reason IN ('entry', 'rsi_bounce_or_max_hold') AND timestamp >= ? ORDER BY timestamp",
    (args.since,),
).fetchall()

client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
by_run: dict[str, list[tuple]] = defaultdict(list)
for ts, sym, side, qty, lim in rows:
    by_run[ts[:16]].append((datetime.fromisoformat(ts), sym, side, qty, lim))

out = []
for _, orders in sorted(by_run.items()):
    t0 = min(o[0] for o in orders) - timedelta(seconds=args.window)
    t1 = max(o[0] for o in orders) + timedelta(seconds=args.window)
    syms = sorted({o[1] for o in orders})
    quotes = client.get_stock_quotes(
        StockQuotesRequest(symbol_or_symbols=syms, start=t0, end=t1, feed=DataFeed.SIP)
    ).data
    for ts, sym, side, qty, lim in orders:
        lo, hi = ts - timedelta(seconds=args.window), ts + timedelta(seconds=args.window)
        sp = [
            (q.ask_price - q.bid_price) / ((q.ask_price + q.bid_price) / 2) * 1e4
            for q in quotes.get(sym, [])
            if lo <= q.timestamp <= hi and q.bid_price > 0 and q.ask_price >= q.bid_price
        ]
        if not sp:
            continue
        mid_now = [
            (q.ask_price + q.bid_price) / 2
            for q in quotes.get(sym, [])
            if lo <= q.timestamp <= hi and q.bid_price > 0
        ]
        out.append(
            {
                "ts": ts.isoformat(), "symbol": sym, "side": side, "qty": qty,
                "notional": qty * (lim or 0), "spread_bp": statistics.median(sp),
                "limit_vs_mid_bp": ((lim or 0) / statistics.median(mid_now) - 1) * 1e4,
            }
        )

sp = sorted(o["spread_bp"] for o in out)
w = sum(o["notional"] for o in out)
q = lambda p: sp[min(len(sp) - 1, int(p * len(sp)))]  # noqa: E731
print(f"orders {len(rows)} | with SIP quotes {len(out)} | runs {len(by_run)} | since {args.since}")
print(
    f"quoted spread (bp of mid): median {statistics.median(sp):.1f}  mean {statistics.mean(sp):.1f}"
    f"  p75 {q(0.75):.1f}  p90 {q(0.90):.1f}  notional-weighted "
    f"{sum(o['spread_bp'] * o['notional'] for o in out) / w:.1f}"
)
print(
    "=> est. cost per leg ~ half-spread: median "
    f"{statistics.median(sp) / 2:.1f} bp, notional-weighted "
    f"{sum(o['spread_bp'] * o['notional'] for o in out) / w / 2:.1f} bp (impact not included)"
)
if args.out:
    json.dump(out, open(args.out, "w"), indent=1)
