"""BStalk3r CLI — connect check, scan, realtime loop, end-of-day report.

The realtime loop is pure rule-based and synchronous: scan -> exits -> entries,
sleep, repeat. No LLM / AI is invoked here by design (speed + determinism).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog

from src.alpaca_client import AlpacaTradingClient
from src.config import Settings, load_settings
from src.database import Database
from src.execution import ExecutionEngine
from src.market_data import AlpacaMarketData, MarketDataProvider
from src.models import PositionState
from src.risk import RiskParams, RiskState, check_entry_allowed, position_size
from src.scanner import scan_candidates
from src.strategy import EntryParams, ExitParams, evaluate_entry, evaluate_exit

_ET = ZoneInfo("America/New_York")
log = structlog.get_logger("bstalk3r")


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _near_market_close(settings: Settings, now_et: datetime | None = None) -> bool:
    """True within force_close_before_close_minutes of 16:00 ET (or after)."""
    now_et = now_et or datetime.now(_ET)
    minutes_to_close = (16 * 60) - (now_et.hour * 60 + now_et.minute)
    return minutes_to_close <= settings.force_close_before_close_minutes


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_check(settings: Settings) -> int:
    settings.validate_paper_safety()
    client = AlpacaTradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
    account = client.get_account()
    log.info(
        "account_ok",
        paper=True,
        base_url=settings.alpaca_base_url,
        equity=getattr(account, "equity", None),
        buying_power=getattr(account, "buying_power", None),
        status=str(getattr(account, "status", None)),
    )
    print(
        f"✅ Paper account connected | equity={getattr(account, 'equity', '?')} "
        f"buying_power={getattr(account, 'buying_power', '?')} "
        f"dry_run={settings.dry_run}"
    )
    return 0


def cmd_scan(settings: Settings, market: MarketDataProvider, db: Database) -> int:
    settings.validate_paper_safety()
    filters = settings.build_scan_filters()
    symbols = settings.universe_symbols()
    snapshots = market.get_snapshots(symbols)
    candidates = scan_candidates(snapshots, filters)

    for c in candidates:
        entry = evaluate_entry(c, settings.build_entry_params(), holding=False)
        db.insert_signal(
            symbol=c.symbol,
            price=c.last_price,
            day_change_pct=c.day_change_pct,
            rvol=c.rvol,
            volume_acceleration=c.volume_acceleration,
            spread_pct=c.spread_pct,
            score=entry.score,
            reason=entry.reasons,
        )
    print(f"Scanned {len(symbols)} symbols -> {len(candidates)} candidate(s):")
    for c in candidates:
        print(
            f"  {c.symbol:6s} ${c.last_price:7.2f}  "
            f"chg={c.day_change_pct:5.1f}%  rvol={c.rvol:5.1f}  "
            f"vacc={c.volume_acceleration:4.1f}  spread={c.spread_pct:.2f}%"
        )
    return 0


def cmd_report(settings: Settings, db: Database, date: str | None) -> int:
    from src.reporter import Reporter

    date = date or _today()
    paths = Reporter(db, settings.report_dir).write_report(date)
    print(f"📊 Report written: {paths['markdown']}")
    print(paths["markdown"].read_text())
    return 0


def cmd_run(
    settings: Settings,
    market: MarketDataProvider,
    db: Database,
    trading: AlpacaTradingClient | None,
    once: bool = False,
) -> int:
    settings.validate_paper_safety()
    entry_params: EntryParams = settings.build_entry_params()
    exit_params: ExitParams = settings.build_exit_params()
    risk_params: RiskParams = settings.build_risk_params()
    filters = settings.build_scan_filters()
    engine = ExecutionEngine(trading, db, settings.build_exec_params(), settings.dry_run, log)

    peak: dict[str, float] = {}
    log.info("loop_start", dry_run=settings.dry_run, universe=settings.universe_symbols())

    try:
        while True:
            _tick(
                settings,
                market,
                db,
                engine,
                trading,
                entry_params,
                exit_params,
                risk_params,
                filters,
                peak,
            )
            if once:
                break
            time.sleep(settings.loop_interval_sec)
    except KeyboardInterrupt:
        log.info("interrupted_force_closing")
        _force_close_all(market, db, engine, exit_params, peak)
    return 0


def _tick(
    settings, market, db, engine, trading, entry_params, exit_params, risk_params, filters, peak
) -> None:
    symbols = settings.universe_symbols()
    try:
        snapshots = market.get_snapshots(symbols)
        data_healthy = len(snapshots) > 0
    except Exception as exc:  # noqa: BLE001 — data outage must halt entries, not crash
        log.warning("market_data_error", error=str(exc))
        snapshots, data_healthy = [], False

    by_symbol = {s.symbol: s for s in snapshots}
    open_positions = db.get_open_positions()
    open_symbols = {p["symbol"] for p in open_positions}
    force_close = _near_market_close(settings)

    # ---- manage exits first ----
    for pos in open_positions:
        snap = by_symbol.get(pos["symbol"])
        if snap is None and not force_close:
            continue
        price = snap.last_price if snap else pos["entry_price"]
        peak[pos["symbol"]] = max(peak.get(pos["symbol"], pos["entry_price"]), price)
        state = PositionState(
            symbol=pos["symbol"],
            entry_price=pos["entry_price"],
            qty=pos["qty"],
            entry_time=datetime.fromisoformat(pos["entry_time"]),
            current_price=price,
            peak_price=peak[pos["symbol"]],
            current_spread_pct=snap.spread_pct if snap else 0.0,
            scaled_out=False,
        )
        decision = evaluate_exit(state, exit_params, datetime.now(UTC), force_close=force_close)
        if decision.should_exit:
            sell_qty = max(int(pos["qty"] * decision.fraction), 1)
            engine.submit_exit(pos["symbol"], sell_qty, price, decision.reason or "exit")
            pnl_amount = (price - pos["entry_price"]) * sell_qty
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
            db.close_position(
                pos["id"], datetime.now(UTC), price, pnl_pct, pnl_amount, decision.reason or "exit"
            )
            peak.pop(pos["symbol"], None)

    if force_close or not data_healthy:
        return

    # ---- entries ----
    equity = _account_equity(trading, settings)
    today = _today()
    realized = sum(p["pnl_amount"] or 0.0 for p in db.get_closed_positions(today))
    risk_state = RiskState(
        account_equity=equity,
        open_positions=len(db.get_open_positions()),
        trades_today=db.count_trades(today),
        realized_pnl_today=realized,
        data_healthy=data_healthy,
    )

    for cand in scan_candidates(snapshots, filters):
        if cand.symbol in open_symbols:
            continue
        entry = evaluate_entry(cand, entry_params, holding=False)
        sid = db.insert_signal(
            symbol=cand.symbol,
            price=cand.last_price,
            day_change_pct=cand.day_change_pct,
            rvol=cand.rvol,
            volume_acceleration=cand.volume_acceleration,
            spread_pct=cand.spread_pct,
            score=entry.score,
            reason=entry.reasons,
        )
        if not entry.enter:
            continue
        gate = check_entry_allowed(risk_state, risk_params)
        if not gate.allowed:
            log.info("entry_vetoed", symbol=cand.symbol, reasons=gate.reasons)
            continue
        qty = position_size(equity, cand.last_price, risk_params)
        if qty <= 0:
            continue
        engine.submit_entry(cand, qty, signal_id=sid)
        db.insert_position(cand.symbol, datetime.now(UTC), cand.last_price, qty)
        open_symbols.add(cand.symbol)
        risk_state = RiskState(
            account_equity=equity,
            open_positions=risk_state.open_positions + 1,
            trades_today=risk_state.trades_today + 1,
            realized_pnl_today=realized,
            data_healthy=data_healthy,
        )


def _force_close_all(market, db, engine, exit_params, peak) -> None:
    for pos in db.get_open_positions():
        try:
            snaps = market.get_snapshots([pos["symbol"]])
            price = snaps[0].last_price if snaps else pos["entry_price"]
        except Exception:  # noqa: BLE001
            price = pos["entry_price"]
        engine.submit_exit(pos["symbol"], pos["qty"], price, "force_close")
        pnl_amount = (price - pos["entry_price"]) * pos["qty"]
        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
        db.close_position(pos["id"], datetime.now(UTC), price, pnl_pct, pnl_amount, "force_close")


def _account_equity(trading: AlpacaTradingClient | None, settings: Settings) -> float:
    if trading is None:
        return settings.max_position_value * settings.max_concurrent_positions * 100
    try:
        return float(trading.get_account().equity)
    except Exception:  # noqa: BLE001
        return 0.0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(
        prog="bstalk3r", description="Low Float Momentum Runner (paper)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify paper account connection")
    sub.add_parser("scan", help="scan the universe once and log signals")
    p_run = sub.add_parser("run", help="run the realtime rule-based loop")
    p_run.add_argument("--once", action="store_true", help="single tick then exit")
    p_report = sub.add_parser("report", help="write end-of-day report")
    p_report.add_argument("--date", help="YYYY-MM-DD (default: today UTC)")

    args = parser.parse_args(argv)
    settings = load_settings()

    try:
        settings.validate_paper_safety()
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    db = Database(settings.db_path)
    db.init_schema()

    if args.command == "check":
        return cmd_check(settings)

    market = AlpacaMarketData(
        settings.alpaca_api_key, settings.alpaca_secret_key, settings.data_feed
    )

    if args.command == "scan":
        return cmd_scan(settings, market, db)
    if args.command == "report":
        return cmd_report(settings, db, getattr(args, "date", None))
    if args.command == "run":
        trading = AlpacaTradingClient(
            settings.alpaca_api_key, settings.alpaca_secret_key, paper=True
        )
        return cmd_run(settings, market, db, trading, once=args.once)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
