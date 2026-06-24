"""BStalk3r CLI — connect check, scan, realtime loop, end-of-day report.

The realtime loop is pure rule-based and synchronous: scan -> exits -> entries,
sleep, repeat. No LLM / AI is invoked here by design (speed + determinism).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from src.alpaca_client import AlpacaTradingClient
from src.config import Settings, load_settings
from src.database import Database
from src.execution import ExecutionEngine
from src.forward_bars import ForwardBarsProvider, PolygonDailyBars
from src.market_data import AlpacaMarketData, MarketDataProvider
from src.models import PositionState
from src.outcomes import compute_outcomes
from src.replay import round_trip_cost, simulate
from src.risk import RiskParams, RiskState, check_entry_allowed, position_size
from src.scanner import ScanFilters, scan_candidates
from src.sources import (
    PolygonGainersSource,
    PolygonGroupedSource,
    ScreenBounds,
    SnapshotSource,
    WatchlistSource,
)
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


def _parse_floats(csv: str | None) -> list[float] | None:
    if not csv:
        return None
    return [float(x) for x in csv.split(",") if x.strip()]


def _git_commit() -> str | None:
    import subprocess
    from pathlib import Path

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return None


def _start_run(settings: Settings, db: Database, mode: str) -> int:
    """Open a provenance run stamping the exact param set + code commit."""
    return db.start_run(
        settings.param_snapshot(),
        mode=mode,
        universe_source=settings.universe_source,
        dry_run=settings.dry_run,
        git_commit=_git_commit(),
    )


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


def cmd_scan(settings: Settings, source: SnapshotSource, db: Database) -> int:
    settings.validate_paper_safety()
    run_id = _start_run(settings, db, mode="scan")
    filters = settings.build_scan_filters()
    snapshots = source.fetch()
    candidates = scan_candidates(snapshots, filters)
    # Accumulate the full screened universe (not just entry-ready) for research.
    db.record_screened(
        snapshots, settings.universe_source, {c.symbol for c in candidates}, run_id=run_id
    )

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
            run_id=run_id,
        )
    print(
        f"Screened {len(snapshots)} symbols via {settings.universe_source} "
        f"-> {len(candidates)} entry-ready candidate(s)."
    )
    # Always surface the top of the screen (useful in EOD/discovery mode where
    # intraday-only fields keep the strict entry filter at zero).
    top = sorted(snapshots, key=lambda s: s.day_change_pct, reverse=True)[:15]
    if top:
        print("Top screened runners:")
    for s in top:
        ready = "✓" if s in candidates else " "
        print(
            f"  [{ready}] {s.symbol:6s} ${s.last_price:8.2f}  "
            f"chg={s.day_change_pct:6.1f}%  rvol={s.rvol:6.1f}  "
            f"vacc={s.volume_acceleration:4.1f}  spread={s.spread_pct:.2f}%"
        )
    return 0


def cmd_report(settings: Settings, db: Database, date: str | None) -> int:
    from src.reporter import Reporter

    date = date or _today()
    paths = Reporter(db, settings.report_dir).write_report(date)
    print(f"📊 Report written: {paths['markdown']}")
    print(paths["markdown"].read_text())
    return 0


def _forward_window(session_date: str, span_days: int = 10) -> tuple[str, str]:
    """Calendar range covering the trading days after `session_date`."""
    base = datetime.fromisoformat(session_date).date()
    return (base + timedelta(days=1)).isoformat(), (base + timedelta(days=span_days)).isoformat()


def cmd_track(
    settings: Settings,
    db: Database,
    bars: ForwardBarsProvider,
    before_date: str | None = None,
    throttle_sec: int | None = None,
    limit: int | None = None,
) -> int:
    """Backfill forward outcomes for screened runners old enough to have data."""
    settings.validate_paper_safety()
    cutoff = (
        before_date
        or (datetime.now(UTC).date() - timedelta(days=settings.outcome_lag_days)).isoformat()
    )
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec
    if limit is None:
        limit = settings.outcome_track_limit

    pending = db.get_screened_pending_outcomes(cutoff)
    total_pending = len(pending)
    if limit and limit > 0:
        pending = pending[:limit]

    written = 0
    for i, row in enumerate(pending):
        start, end = _forward_window(row["session_date"])
        fwd = bars.fetch(row["symbol"], start, end)
        for o in compute_outcomes(row["last_price"], fwd, horizons=(1, 3, 5)):
            db.upsert_outcome(
                screened_id=row["id"], symbol=row["symbol"], base_date=row["session_date"], **o
            )
            written += 1
        if throttle and i < len(pending) - 1:
            time.sleep(throttle)

    remaining = total_pending - len(pending)
    tail = f" ({remaining} still pending — drains over the next runs)" if remaining else ""
    print(
        f"Processed {len(pending)}/{total_pending} pending runner(s) (cutoff {cutoff}); "
        f"wrote {written} outcome(s). Total outcomes: {db.count_outcomes()}{tail}"
    )
    return 0


def _replay_filters(
    settings: Settings, min_rvol: float | None = None, min_day_change: float | None = None
) -> ScanFilters:
    """Filters for replay over EOD data: gate price/change/rvol, leave the
    intraday-only fields (vol-accel/spread) permissive since EOD can't inform them.
    """
    return ScanFilters(
        min_price=settings.min_price,
        max_price=settings.max_price,
        min_day_change_pct=settings.min_day_change_pct
        if min_day_change is None
        else min_day_change,
        max_day_change_pct=settings.max_day_change_pct,
        min_rvol=settings.min_rvol if min_rvol is None else min_rvol,
        min_volume_acceleration=0.0,
        max_spread_pct=1e9,
    )


def _build_variants(
    settings: Settings,
    sweep_rvol: list[float] | None,
    sweep_change: list[float] | None,
) -> list[tuple[str, ScanFilters]]:
    variants: list[tuple[str, ScanFilters]] = [("baseline", _replay_filters(settings))]
    for v in sweep_rvol or []:
        variants.append((f"rvol>={v:g}", _replay_filters(settings, min_rvol=v)))
    for v in sweep_change or []:
        variants.append((f"chg>={v:g}%", _replay_filters(settings, min_day_change=v)))
    return variants


def cmd_replay(
    settings: Settings,
    db: Database,
    horizon: str = "3d",
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    sweep_rvol: list[float] | None = None,
    sweep_change: list[float] | None = None,
    cost_pct: float | None = None,
    gross: bool = False,
) -> int:
    """Re-simulate alternate parameter sets over stored screened+outcome data."""
    rows = db.get_screened_with_outcomes(horizon, start, end, source)
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)

    def cost_fn(row: dict[str, Any]) -> float:
        return round_trip_cost(
            row["last_price"],
            base_pct=base_cost,
            cheap_price=settings.replay_cheap_price,
            cheap_extra_pct=settings.replay_cheap_extra_pct,
        )

    results = [
        simulate(name, rows, f, cost_fn=cost_fn)
        for name, f in _build_variants(settings, sweep_rvol, sweep_change)
    ]

    span = f" [{start or '…'}..{end or '…'}]" if (start or end) else ""
    if gross or base_cost == 0:
        cost_note = "GROSS (no costs)"
    else:
        cost_note = (
            f"net of {base_cost:g}% round-trip "
            f"(+{settings.replay_cheap_extra_pct:g}% under ${settings.replay_cheap_price:g})"
        )
    print(f"Replay over {len(rows)} screened runner(s) @ horizon {horizon}{span} — {cost_note}")
    cols = ("variant", "enter", "score", "avg%", "med%", "win%", "maxgn%", "mdd%")
    print("  " + " ".join(f"{c:>8s}" if c != "variant" else f"{c:14s}" for c in cols))
    for r in results:
        win = _fmt(r.win_rate * 100 if r.win_rate is not None else None)
        print(
            f"  {r.name:14s} {r.n_entered:>8d} {r.n_scored:>8d} "
            f"{_fmt(r.avg_return)} {_fmt(r.median_return)} {win} "
            f"{_fmt(r.avg_max_gain)} {_fmt(r.avg_max_drawdown)}"
        )
    return 0


def _fmt(x: float | None, width: int = 8) -> str:
    return f"{x:>{width}.1f}" if x is not None else f"{'—':>{width}}"


def cmd_run(
    settings: Settings,
    source: SnapshotSource,
    market: MarketDataProvider,
    db: Database,
    trading: AlpacaTradingClient | None,
    once: bool = False,
) -> int:
    settings.validate_paper_safety()
    run_id = _start_run(settings, db, mode="run")
    entry_params: EntryParams = settings.build_entry_params()
    exit_params: ExitParams = settings.build_exit_params()
    risk_params: RiskParams = settings.build_risk_params()
    filters = settings.build_scan_filters()
    engine = ExecutionEngine(
        trading, db, settings.build_exec_params(), settings.dry_run, log, run_id=run_id
    )

    peak: dict[str, float] = {}
    log.info("loop_start", dry_run=settings.dry_run, source=settings.universe_source, run_id=run_id)

    try:
        while True:
            _tick(
                settings,
                source,
                market,
                db,
                engine,
                trading,
                entry_params,
                exit_params,
                risk_params,
                filters,
                peak,
                run_id,
            )
            if once:
                break
            time.sleep(settings.loop_interval_sec)
    except KeyboardInterrupt:
        log.info("interrupted_force_closing")
        _force_close_all(market, db, engine, exit_params, peak)
    return 0


def _tick(
    settings,
    source,
    market,
    db,
    engine,
    trading,
    entry_params,
    exit_params,
    risk_params,
    filters,
    peak,
    run_id=None,
) -> None:
    try:
        snapshots = source.fetch()
        data_healthy = len(snapshots) > 0
    except Exception as exc:  # noqa: BLE001 — data outage must halt entries, not crash
        log.warning("market_data_error", error=str(exc))
        snapshots, data_healthy = [], False

    by_symbol = {s.symbol: s for s in snapshots}
    open_positions = db.get_open_positions()
    open_symbols = {p["symbol"] for p in open_positions}
    force_close = _near_market_close(settings)

    # Accumulate the full screened universe every tick (idempotent per session).
    candidates = scan_candidates(snapshots, filters)
    if snapshots:
        db.record_screened(
            snapshots, settings.universe_source, {c.symbol for c in candidates}, run_id=run_id
        )

    # Held symbols may have dropped out of the screened set — look them up
    # directly so exits are still managed on current price.
    missing = [p["symbol"] for p in open_positions if p["symbol"] not in by_symbol]
    if missing:
        try:
            for s in market.get_snapshots(missing):
                by_symbol[s.symbol] = s
        except Exception as exc:  # noqa: BLE001
            log.warning("held_lookup_error", error=str(exc))

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

    for cand in candidates:
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
            run_id=run_id,
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
        db.insert_position(cand.symbol, datetime.now(UTC), cand.last_price, qty, run_id=run_id)
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


def _build_source(settings: Settings, market: MarketDataProvider) -> SnapshotSource:
    if settings.universe_source.lower() == "polygon":
        if settings.polygon_intraday:
            return PolygonGainersSource(settings.polygon_api_key, settings.screener_top_n)
        bounds = ScreenBounds(
            min_price=settings.min_price,
            max_price=settings.max_price,
            min_change_pct=settings.min_day_change_pct,
        )
        return PolygonGroupedSource(settings.polygon_api_key, bounds, settings.screener_top_n)
    return WatchlistSource(market, settings.universe_symbols())


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
    p_track = sub.add_parser("track", help="backfill forward outcomes for screened runners")
    p_track.add_argument("--before", help="only track runners on/before this date (YYYY-MM-DD)")
    p_track.add_argument("--limit", type=int, help="max runners to process this run")
    p_replay = sub.add_parser("replay", help="re-simulate alternate params over stored data")
    p_replay.add_argument("--horizon", default="3d", help="outcome horizon: 1d|3d|5d (default 3d)")
    p_replay.add_argument("--start", help="earliest session_date (YYYY-MM-DD)")
    p_replay.add_argument("--end", help="latest session_date (YYYY-MM-DD)")
    p_replay.add_argument("--source", help="filter by screen source (polygon|watchlist)")
    p_replay.add_argument("--sweep-min-rvol", help="comma-separated rvol thresholds, e.g. 4,8,12")
    p_replay.add_argument("--sweep-min-day-change", help="comma-separated day-change %% thresholds")
    p_replay.add_argument(
        "--cost-pct", type=float, help="round-trip cost %% override (default from .env)"
    )
    p_replay.add_argument("--gross", action="store_true", help="ignore costs (gross returns)")

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

    if args.command == "track":
        try:
            bars = PolygonDailyBars(settings.polygon_api_key)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_track(settings, db, bars, before_date=args.before, limit=args.limit)

    if args.command == "replay":
        return cmd_replay(
            settings,
            db,
            horizon=args.horizon,
            start=args.start,
            end=args.end,
            source=args.source,
            sweep_rvol=_parse_floats(args.sweep_min_rvol),
            sweep_change=_parse_floats(args.sweep_min_day_change),
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    market = AlpacaMarketData(
        settings.alpaca_api_key, settings.alpaca_secret_key, settings.data_feed
    )
    try:
        source = _build_source(settings, market)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if args.command == "scan":
        return cmd_scan(settings, source, db)
    if args.command == "report":
        return cmd_report(settings, db, getattr(args, "date", None))
    if args.command == "run":
        trading = AlpacaTradingClient(
            settings.alpaca_api_key, settings.alpaca_secret_key, paper=True
        )
        return cmd_run(settings, source, market, db, trading, once=args.once)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
