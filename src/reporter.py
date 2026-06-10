"""End-of-day report from the SQLite audit trail.

Post-market only. This is the one place where AI/LLM *could* later be plugged
in (to narrate the day or propose parameter tweaks) — never in the realtime
loop. v0 is pure aggregation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.database import Database


class Reporter:
    def __init__(self, db: Database, report_dir: str):
        self.db = db
        self.report_dir = Path(report_dir).expanduser()

    def build_report(self, date: str) -> dict[str, Any]:
        num_signals = self.db.count_signals(date)
        closed = self.db.get_closed_positions(date)
        num_trades = len(closed)

        wins = [p for p in closed if (p["pnl_amount"] or 0) > 0]
        win_rate = len(wins) / num_trades if num_trades else 0.0
        total_pnl = sum(p["pnl_amount"] or 0.0 for p in closed)
        max_drawdown = _max_drawdown([p["pnl_amount"] or 0.0 for p in closed])

        return {
            "date": date,
            "num_signals": num_signals,
            "num_trades": num_trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "max_drawdown": max_drawdown,
            "trades": [
                {
                    "symbol": p["symbol"],
                    "entry_price": p["entry_price"],
                    "exit_price": p["exit_price"],
                    "pnl_amount": p["pnl_amount"],
                    "pnl_pct": p["pnl_pct"],
                    "exit_reason": p["exit_reason"],
                }
                for p in closed
            ],
        }

    def write_report(self, date: str) -> dict[str, Path]:
        report = self.build_report(date)
        self.report_dir.mkdir(parents=True, exist_ok=True)

        json_path = self.report_dir / f"{date}.json"
        json_path.write_text(json.dumps(report, indent=2))

        md_path = self.report_dir / f"{date}.md"
        md_path.write_text(_render_markdown(report))

        self.db.upsert_daily_stats(
            date=date,
            num_signals=report["num_signals"],
            num_trades=report["num_trades"],
            win_rate=report["win_rate"],
            total_pnl=report["total_pnl"],
            max_drawdown=report["max_drawdown"],
        )
        return {"json": json_path, "markdown": md_path}


def _max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough drop of the cumulative realized-PnL curve ($)."""
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def _render_markdown(r: dict[str, Any]) -> str:
    lines = [
        f"# BStalk3r daily report — {r['date']}",
        "",
        f"- Signals: **{r['num_signals']}**",
        f"- Trades: **{r['num_trades']}**",
        f"- Win rate: **{r['win_rate'] * 100:.1f}%**",
        f"- Total PnL: **${r['total_pnl']:.2f}**",
        f"- Max drawdown: **${r['max_drawdown']:.2f}**",
        "",
        "| Symbol | Entry | Exit | PnL $ | PnL % | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for t in r["trades"]:
        lines.append(
            f"| {t['symbol']} | {t['entry_price']:.2f} | "
            f"{(t['exit_price'] or 0):.2f} | {(t['pnl_amount'] or 0):.2f} | "
            f"{(t['pnl_pct'] or 0) * 100:.1f}% | {t['exit_reason']} |"
        )
    return "\n".join(lines) + "\n"
