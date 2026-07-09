"""BStalk3r CLI — connect check, scan, realtime loop, end-of-day report.

The realtime loop is pure rule-based and synchronous: scan -> exits -> entries,
sleep, repeat. No LLM / AI is invoked here by design (speed + determinism).
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from src.alpaca_client import AlpacaTradingClient
from src.config import Settings, load_settings
from src.database import Database
from src.execution import ExecutionEngine
from src.exhaustion import find_exhaustion_shorts
from src.exhaustion_intraday import qualifying_run_ends, simulate_run_end_short
from src.forward_bars import ForwardBarsProvider, PolygonDailyBars
from src.intraday import (
    aggregate,
    bucket_by_feature,
    entry_features,
    reconstruct_entry,
    simulate_short_trade,
    simulate_trade,
)
from src.long_search import TradeInput, evaluate_combo, make_grid, search
from src.market_data import AlpacaMarketData, MarketDataProvider
from src.mean_reversion import mean_reversion_trades, summarize_mr
from src.minute_bars import MinuteBarsProvider, PolygonMinuteBars
from src.minute_cache import CachedMinuteBars
from src.models import PositionState
from src.outcomes import compute_outcomes
from src.replay import round_trip_cost, simulate
from src.risk import RiskParams, RiskState, check_entry_allowed, position_size
from src.scanner import ScanFilters, scan_candidates
from src.short_accumulation import build_short_record, exhaustion_setups, fade_setups
from src.short_report import summarize_short_setups
from src.sources import (
    PolygonGainersSource,
    PolygonGroupedSource,
    ScreenBounds,
    SnapshotSource,
    WatchlistSource,
    polygon_grouped_crossers,
)
from src.strategy import EntryParams, ExitParams, evaluate_entry, evaluate_exit
from src.xsectional import build_panel, cross_sectional_backtest, summarize_rebalances

_ET = ZoneInfo("America/New_York")
log = structlog.get_logger("bstalk3r")


def _minute_provider(settings: Settings) -> MinuteBarsProvider:
    """Live Polygon minute bars, wrapped in the persistent cache when enabled so
    iterative backtests over a window don't re-pay the rate-limited fetch."""
    live = PolygonMinuteBars(settings.polygon_api_key)
    if settings.minute_cache_path:
        return CachedMinuteBars(live, settings.minute_cache_path)
    return live


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


@dataclass(frozen=True)
class _IntradayVariant:
    label: str
    entry: float  # entry trigger %
    take_profit_pct: float  # fraction (0.15)
    max_hold: float  # minutes
    trailing_pct: float  # fraction (0.08)


def _intraday_variants(
    settings: Settings,
    sweep_entry: list[float] | None,
    sweep_tp: list[float] | None,
    sweep_hold: list[float] | None,
    sweep_trail: list[float] | None = None,
) -> list[_IntradayVariant]:
    """Cartesian grid over whatever dims are swept; unswept dims use .env.

    Bars are fetched once per runner, so every grid cell is just extra pure
    simulation — the rate-limited Polygon cost is independent of grid size.
    """
    entries = sweep_entry or [settings.min_day_change_pct]
    tps = sweep_tp or [settings.take_profit_pct * 100]  # CLI sweeps in %, store fraction
    holds = sweep_hold or [settings.max_hold_minutes]
    trails = sweep_trail or [settings.trailing_stop_pct * 100]
    out: list[_IntradayVariant] = []
    for e in entries:
        for tp in tps:
            for h in holds:
                for tr in trails:
                    parts = []
                    if sweep_entry:
                        parts.append(f"e{e:g}")
                    if sweep_tp:
                        parts.append(f"tp{tp:g}")
                    if sweep_hold:
                        parts.append(f"h{h:g}")
                    if sweep_trail:
                        parts.append(f"tr{tr:g}")
                    out.append(
                        _IntradayVariant(" ".join(parts) or "baseline", e, tp / 100, h, tr / 100)
                    )
    return out


def cmd_intraday(
    settings: Settings,
    db: Database,
    bars: MinuteBarsProvider,
    limit: int = 40,
    sweep_entry: list[float] | None = None,
    sweep_tp: list[float] | None = None,
    sweep_hold: list[float] | None = None,
    sweep_trail: list[float] | None = None,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
    source: str | None = None,
    train_end: str | None = None,
) -> int:
    """Intraday hit-and-run backtest: enter at the runner trigger, exit on the
    live rules. Sweep entry-trigger / take-profit / max-hold; net of costs.

    With `train_end` (YYYY-MM-DD), runners split into train (≤) / test (>) and
    each variant is scored on both — the honest out-of-sample check on whether a
    train-winning parameter set survives on unseen sessions.
    """
    settings.validate_paper_safety()
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec
    variants = _intraday_variants(settings, sweep_entry, sweep_tp, sweep_hold, sweep_trail)

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    runners = db.get_screened_band(
        settings.min_price, settings.max_price, limit=limit, source=source
    )
    # per variant: list of (session_date, trade)
    trades: dict[str, list[tuple[str, Any]]] = {v.label: [] for v in variants}
    fetched = 0
    for i, row in enumerate(runners):
        prev_close = row["last_price"] / (1 + row["day_change_pct"] / 100)
        session_bars = bars.fetch(row["symbol"], row["session_date"])
        if not session_bars:
            continue
        fetched += 1
        for v in variants:
            ep = ExitParams(
                stop_loss_pct=settings.stop_loss_pct,
                take_profit_pct=v.take_profit_pct,
                scale_out_fraction=settings.scale_out_fraction,
                trailing_stop_pct=v.trailing_pct,
                max_hold_minutes=v.max_hold,
                exit_spread_pct=settings.exit_spread_pct,
            )
            t = simulate_trade(
                session_bars,
                prev_close,
                v.entry,
                settings.min_price,
                settings.max_price,
                ep,
                cost_fn=cost_fn,
            )
            if t.entered:
                trades[v.label].append((row["session_date"], t))
        if throttle and i < len(runners) - 1:
            time.sleep(throttle)

    cost_note = "GROSS" if base_cost == 0 else f"net of {base_cost:g}%+cheap round-trip"
    print(
        f"Intraday hit-and-run over {fetched}/{len(runners)} runner(s) with minute data "
        f"({cost_note}):"
    )
    if train_end:
        _print_intraday_oos(variants, trades, train_end)
    else:
        _print_intraday_single(variants, trades)
    return 0


def _print_intraday_single(variants: list[_IntradayVariant], trades: dict[str, list]) -> None:
    hdr = f"  {'variant':14s} {'trades':>6s} {'avg%':>7s} {'med%':>7s} {'win%':>6s} {'avgHold':>7s}"
    print(hdr + "   exits")
    for v in variants:
        a = aggregate([t for _, t in trades[v.label]])
        if a is None:
            print(f"  {v.label:14s} {0:>6d}")
            continue
        top = ", ".join(
            f"{k}:{n}" for k, n in sorted(a["reasons"].items(), key=lambda x: -x[1])[:3]
        )
        body = (
            f"  {v.label:14s} {a['n']:>6d} {a['avg']:>7.1f} {a['median']:>7.1f} "
            f"{a['win_rate']:>6.1f} {a['avg_hold']:>7.1f}"
        )
        print(f"{body}   {top}")


def _print_intraday_oos(
    variants: list[_IntradayVariant], trades: dict[str, list], train_end: str
) -> None:
    print(f"  Out-of-sample split at {train_end} (train ≤ / test >):")
    hdr = (
        f"  {'variant':14s} | {'nTr':>4s} {'trAvg%':>7s} {'trWin%':>6s} "
        f"| {'nTe':>4s} {'teAvg%':>7s} {'teWin%':>6s}"
    )
    print(hdr)
    scored = []
    for v in variants:
        train = aggregate([t for sd, t in trades[v.label] if sd <= train_end])
        test = aggregate([t for sd, t in trades[v.label] if sd > train_end])
        scored.append((v, train, test))

    # mark the train-winner so its test result is easy to read
    ranked = [s for s in scored if s[1] is not None]
    best = max(ranked, key=lambda s: s[1]["avg"]) if ranked else None
    for v, train, test in scored:

        def _c(a, key, w=7):
            return f"{a[key]:>{w}.1f}" if a else f"{'—':>{w}}"

        mark = " *" if best and v.label == best[0].label else "  "
        tr_n = train["n"] if train else 0
        te_n = test["n"] if test else 0
        print(
            f"{mark}{v.label:14s} | {tr_n:>4d} {_c(train, 'avg')} {_c(train, 'win_rate', 6)} "
            f"| {te_n:>4d} {_c(test, 'avg')} {_c(test, 'win_rate', 6)}"
        )
    if best:
        v, train, test = best
        verdict = "holds up" if (test and test["avg"] > 0) else "collapses (overfit risk)"
        te = f"{test['avg']:+.1f}% / {test['win_rate']:.0f}% win" if test else "no test trades"
        print(f"\n  Train-best = {v.label} ({train['avg']:+.1f}% train) -> test {te} — {verdict}.")


def cmd_crosser(
    settings: Settings,
    grouped: PolygonGroupedSource,
    minute_bars: MinuteBarsProvider,
    date: str,
    sample: int = 150,
    entry_trigger: float | None = None,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """Survivorship-inclusive backtest: enter EVERY intraday +X% crosser (fizzles
    included), score with the live exit rules, and split all vs survivors vs
    fizzles — the honest test of whether the edge survives real detection.
    """
    settings.validate_paper_safety()
    trigger = settings.min_day_change_pct if entry_trigger is None else entry_trigger
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    today_rows = grouped.fetch_grouped(date)
    if not today_rows:
        print(f"No grouped data for {date} (free-tier lag or non-trading day).")
        return 1
    prev_by = {r["T"]: r for r in grouped.prev_session_rows(date) if r.get("T")}
    crossers = polygon_grouped_crossers(
        today_rows, prev_by, settings.min_price, settings.max_price, trigger
    )
    if not crossers:
        print(f"No +{trigger:g}% intraday crossers for {date}.")
        return 0

    n_fizzle = sum(1 for c in crossers if c["is_fizzle"])
    stride = max(1, len(crossers) // sample)
    picks = crossers[::stride][:sample]  # representative spread across the universe

    ep = settings.build_exit_params()
    tagged: list[tuple[bool, Any]] = []
    fetched = 0
    for i, cr in enumerate(picks):
        bars = minute_bars.fetch(cr["symbol"], date)
        if bars:
            fetched += 1
            t = simulate_trade(
                bars,
                cr["prev_close"],
                trigger,
                settings.min_price,
                settings.max_price,
                ep,
                cost_fn=cost_fn,
            )
            if t.entered:
                tagged.append((cr["is_fizzle"], t))
        if throttle and i < len(picks) - 1:
            time.sleep(throttle)

    print(
        f"Crosser backtest {date}: {len(crossers)} intraday +{trigger:g}% crossers "
        f"({n_fizzle / len(crossers) * 100:.0f}% fizzles), sampled {fetched} with minute data"
    )
    print(
        f"  exit: hold {ep.max_hold_minutes:g}m, tp {ep.take_profit_pct * 100:g}%, "
        f"trail {ep.trailing_stop_pct * 100:g}%; "
        f"{'GROSS' if base_cost == 0 else f'net {base_cost:g}%+cheap'}"
    )
    groups = [
        ("ALL crossers", [t for _, t in tagged]),
        ("survivors", [t for f, t in tagged if not f]),
        ("fizzles", [t for f, t in tagged if f]),
    ]
    print(f"  {'group':14s} {'trades':>6s} {'avg%':>7s} {'med%':>7s} {'win%':>6s}")
    for name, ts in groups:
        a = aggregate(ts)
        if a is None:
            print(f"  {name:14s} {0:>6d}")
            continue
        print(
            f"  {name:14s} {a['n']:>6d} {a['avg']:>7.1f} {a['median']:>7.1f} {a['win_rate']:>6.1f}"
        )
    return 0


_FEATURE_KEYS = (
    "cum_dollar_vol",
    "cum_volume",
    "vol_accel",
    "minutes_to_cross",
    "gap_pct",
    "entry_price",
)


def _pctile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def cmd_fade(
    settings: Settings,
    grouped: PolygonGroupedSource,
    minute_bars: MinuteBarsProvider,
    date: str,
    sample: int = 150,
    entry_trigger: float | None = None,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """Fade/short backtest (H-A): SHORT every intraday +X% crosser with a stop,
    split all/survivors/fizzles, and quantify the squeeze tail (max adverse
    up-move) — the strategy killer.
    """
    settings.validate_paper_safety()
    trigger = settings.min_day_change_pct if entry_trigger is None else entry_trigger
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    today_rows = grouped.fetch_grouped(date)
    if not today_rows:
        print(f"No grouped data for {date}.")
        return 1
    prev_by = {r["T"]: r for r in grouped.prev_session_rows(date) if r.get("T")}
    crossers = polygon_grouped_crossers(
        today_rows, prev_by, settings.min_price, settings.max_price, trigger
    )
    if not crossers:
        print(f"No +{trigger:g}% crossers for {date}.")
        return 0
    n_fizzle = sum(1 for c in crossers if c["is_fizzle"])
    stride = max(1, len(crossers) // sample)
    picks = crossers[::stride][:sample]

    ep = settings.build_exit_params()
    tagged: list[tuple[bool, Any]] = []
    fetched = 0
    for i, cr in enumerate(picks):
        bars = minute_bars.fetch(cr["symbol"], date)
        if bars:
            fetched += 1
            t = simulate_short_trade(
                bars,
                cr["prev_close"],
                trigger,
                settings.min_price,
                settings.max_price,
                ep,
                cost_fn=cost_fn,
            )
            if t.entered:
                tagged.append((cr["is_fizzle"], t))
        if throttle and i < len(picks) - 1:
            time.sleep(throttle)

    print(
        f"Fade/SHORT backtest {date}: {len(crossers)} +{trigger:g}% crossers "
        f"({n_fizzle / len(crossers) * 100:.0f}% fizzles), sampled {fetched}; "
        f"stop {ep.stop_loss_pct * 100:g}% tp {ep.take_profit_pct * 100:g}% "
        f"({'GROSS' if base_cost == 0 else f'net {base_cost:g}%+cheap'})"
    )
    groups = [
        ("ALL crossers", [t for _, t in tagged]),
        ("survivors", [t for f, t in tagged if not f]),
        ("fizzles", [t for f, t in tagged if f]),
    ]
    print(f"  {'group':14s} {'trades':>6s} {'avg%':>7s} {'med%':>7s} {'win%':>6s}")
    for name, ts in groups:
        a = aggregate(ts)
        if a is None:
            print(f"  {name:14s} {0:>6d}")
            continue
        print(
            f"  {name:14s} {a['n']:>6d} {a['avg']:>7.1f} {a['median']:>7.1f} {a['win_rate']:>6.1f}"
        )

    # squeeze tail: worst up-excursion = the real short risk (fill worse than stop)
    adverse = [t.max_adverse_pct for _, t in tagged]
    stop_pct = ep.stop_loss_pct * 100
    if adverse:
        gapped = sum(1 for a in adverse if a > 2 * stop_pct) / len(adverse) * 100
        print(
            f"  --- squeeze tail (max adverse up-move): mean {sum(adverse) / len(adverse):.0f}% "
            f"p90 {_pctile(adverse, 0.9):.0f}% p99 {_pctile(adverse, 0.99):.0f}% "
            f"max {max(adverse):.0f}%  |  ran >2x stop (gap-through) {gapped:.0f}%"
        )
    return 0


def cmd_features(
    settings: Settings,
    grouped: PolygonGroupedSource,
    minute_bars: MinuteBarsProvider,
    date: str,
    sample: int = 150,
    entry_trigger: float | None = None,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """Do any entry-observable features separate winners from fizzles? For each
    sampled crosser, compute features AT the cross moment + the trade's net
    return, then bucket every feature (low/mid/high) to see the return spread.
    """
    settings.validate_paper_safety()
    trigger = settings.min_day_change_pct if entry_trigger is None else entry_trigger
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    today_rows = grouped.fetch_grouped(date)
    if not today_rows:
        print(f"No grouped data for {date}.")
        return 1
    prev_by = {r["T"]: r for r in grouped.prev_session_rows(date) if r.get("T")}
    crossers = polygon_grouped_crossers(
        today_rows, prev_by, settings.min_price, settings.max_price, trigger
    )
    if not crossers:
        print(f"No +{trigger:g}% crossers for {date}.")
        return 0
    stride = max(1, len(crossers) // sample)
    picks = crossers[::stride][:sample]

    ep = settings.build_exit_params()
    samples: list[tuple[dict[str, float], float]] = []
    for i, cr in enumerate(picks):
        bars = minute_bars.fetch(cr["symbol"], date)
        if bars:
            idx = reconstruct_entry(
                bars, cr["prev_close"], trigger, settings.min_price, settings.max_price
            )
            if idx is not None:
                feats = entry_features(bars, idx, cr["prev_close"])
                t = simulate_trade(
                    bars,
                    cr["prev_close"],
                    trigger,
                    settings.min_price,
                    settings.max_price,
                    ep,
                    cost_fn=cost_fn,
                )
                if t.entered:
                    samples.append((feats, t.net_return_pct))
        if throttle and i < len(picks) - 1:
            time.sleep(throttle)

    base = sum(r for _, r in samples) / len(samples) if samples else 0.0
    print(
        f"Entry-feature separability {date}: {len(samples)} trades, "
        f"baseline avg {base:+.1f}% (does any feature's high bucket beat this?)"
    )
    for key in _FEATURE_KEYS:
        buckets = bucket_by_feature(samples, key, n_buckets=3)
        if not buckets:
            continue
        cells = " | ".join(
            f"{b['bucket']}: {b['avg']:+5.1f}% ({b['win_rate']:.0f}%w, n{b['n']})" for b in buckets
        )
        spread = buckets[-1]["avg"] - buckets[0]["avg"]
        print(f"  {key:16s} {cells}   Δ(high-low)={spread:+.1f}%")
    return 0


def _short_stats(rets: list[float]) -> str:
    if not rets:
        return "n=0"
    avg = sum(rets) / len(rets)
    med = sorted(rets)[len(rets) // 2]
    win = sum(1 for x in rets if x > 0) / len(rets) * 100
    return f"n={len(rets):>3d}  avg {avg:+5.1f}%  med {med:+5.1f}%  win {win:.0f}%"


def cmd_exhaustion(
    settings: Settings,
    grouped: PolygonGroupedSource,
    start: str,
    end: str,
    run_days: int = 3,
    run_gain: float = 50.0,
    fwd_days: int = 1,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """First-red-day / multi-day exhaustion SHORT concept test (H-B), daily bars
    only (fast, no minute fetches). Parabolic run over `run_days` sessions then a
    red day -> short; report intraday (open->close) and swing (+fwd_days) returns.
    """
    settings.validate_paper_safety()
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec

    daily: dict[str, list[dict[str, Any]]] = {}
    day = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    n_sessions = 0
    while day <= end_d:
        if day.weekday() < 5:
            rows = grouped.fetch_grouped(day.isoformat())
            if rows:
                n_sessions += 1
                for r in rows:
                    sym = r.get("T")
                    if sym and r.get("c"):
                        daily.setdefault(sym, []).append(
                            {
                                "date": day.isoformat(),
                                "open": r.get("o"),
                                "high": r.get("h"),
                                "low": r.get("l"),
                                "close": r["c"],
                            }
                        )
                if throttle:
                    time.sleep(throttle)
        day += timedelta(days=1)

    setups = find_exhaustion_shorts(
        daily, run_days, run_gain, settings.min_price, settings.max_price, fwd_days, base_cost
    )
    print(
        f"Exhaustion short {start}..{end} ({n_sessions} sessions): run ≥{run_gain:g}% over "
        f"{run_days}d then first red day. {len(setups)} setups "
        f"({'GROSS' if base_cost == 0 else f'net {base_cost:g}%+cheap'})"
    )
    if not setups:
        print("  (no setups — thin data / raise date range or lower thresholds)")
        return 0
    intraday = [s.intraday_short_ret for s in setups]
    swing = [s.swing_short_ret for s in setups if s.swing_short_ret is not None]
    print(f"  intraday short (open->close of red day):  {_short_stats(intraday)}")
    print(f"  swing short (red close -> +{fwd_days}d close): {_short_stats(swing)}")
    print("  top setups by run gain:")
    for s in sorted(setups, key=lambda x: -x.run_gain_pct)[:8]:
        sw = f"{s.swing_short_ret:+.1f}%" if s.swing_short_ret is not None else "—"
        print(
            f"    {s.symbol:6s} run+{s.run_gain_pct:5.0f}% red {s.red_day_date}  "
            f"intraday {s.intraday_short_ret:+5.1f}%  swing {sw}"
        )
    return 0


def cmd_exhaustion_intraday(
    settings: Settings,
    grouped: PolygonGroupedSource,
    minute_bars: MinuteBarsProvider,
    start: str,
    end: str,
    run_days: int = 3,
    run_gain: float = 50.0,
    entry_trigger: float = 2.0,
    sample: int = 150,
    entry_mode: str = "breakout",
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """Exhaustion SHORT v2 — realistic intraday entry, no EOD/look-ahead bias.

    Candidate = the session AFTER a parabolic run (run over `run_days` ≥
    `run_gain`%), regardless of its close. Entry (`entry_mode`) is the first
    minute-bar break of `entry_trigger`% vs the run-end close — "breakout" fades
    the up-push (short into strength), "breakdown" shorts the loss of the prior
    close (the first-red-day thesis). Exits via the live short rules; days that
    never trigger don't trade. Tradeable counterpart to `exhaustion` (v1).
    """
    settings.validate_paper_safety()
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    daily: dict[str, list[dict[str, Any]]] = {}
    day = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    n_sessions = 0
    while day <= end_d:
        if day.weekday() < 5:
            rows = grouped.fetch_grouped(day.isoformat())
            if rows:
                n_sessions += 1
                for r in rows:
                    sym = r.get("T")
                    if sym and r.get("c"):
                        daily.setdefault(sym, []).append(
                            {
                                "date": day.isoformat(),
                                "open": r.get("o"),
                                "high": r.get("h"),
                                "low": r.get("l"),
                                "close": r["c"],
                            }
                        )
                if throttle:
                    time.sleep(throttle)
        day += timedelta(days=1)

    candidates = qualifying_run_ends(
        daily, run_days, run_gain, settings.min_price, settings.max_price
    )
    stride = max(1, len(candidates) // sample) if candidates else 1
    picks = candidates[::stride][:sample]

    ep = settings.build_exit_params()
    trades: list[Any] = []
    fetched = 0
    for i, cand in enumerate(picks):
        bars = minute_bars.fetch(cand.symbol, cand.short_day_date)
        if bars:
            fetched += 1
            res = simulate_run_end_short(
                cand,
                bars,
                entry_trigger,
                settings.min_price,
                settings.max_price,
                ep,
                cost_fn=cost_fn,
                entry_mode=entry_mode,
            )
            if res is not None:
                trades.append(res)
        if throttle and i < len(picks) - 1:
            time.sleep(throttle)

    _dir = "up-break fade" if entry_mode == "breakout" else "prior-close breakdown"
    print(
        f"Exhaustion SHORT v2 [{entry_mode}: {_dir}] {start}..{end} ({n_sessions} sessions): "
        f"run ≥{run_gain:g}% over {run_days}d -> next-day {entry_trigger:g}% intraday break, "
        f"live exits. {len(candidates)} candidates, sampled {fetched}, {len(trades)} triggered "
        f"({'GROSS' if base_cost == 0 else f'net {base_cost:g}%+cheap'})"
    )
    agg = aggregate([r.trade for r in trades])
    if agg is None:
        print("  (no triggered shorts — raise date range / lower trigger)")
        return 0
    print(
        f"  intraday short:  n={agg['n']:>3d}  avg {agg['avg']:+5.1f}%  "
        f"med {agg['median']:+5.1f}%  win {agg['win_rate']:.0f}%  "
        f"avg_hold {agg['avg_hold']:.0f}m"
    )
    print(f"  exits: {agg['reasons']}")
    adverse = [r.trade.max_adverse_pct for r in trades]
    if adverse:
        print(
            f"  squeeze tail (max adverse up-move): mean {sum(adverse) / len(adverse):.0f}% "
            f"p90 {_pctile(adverse, 0.9):.0f}%  max {max(adverse):.0f}%"
        )
    print("  top setups by run gain:")
    for r in sorted(trades, key=lambda x: -x.run_gain_pct)[:8]:
        t = r.trade
        print(
            f"    {r.symbol:6s} run+{r.run_gain_pct:5.0f}% {r.short_day_date}  "
            f"short {t.net_return_pct:+5.1f}%  ({t.exit_reason}, {t.held_min:.0f}m, "
            f"adverse {t.max_adverse_pct:.0f}%)"
        )
    return 0


def _split_budget(n_a: int, n_b: int, total: int) -> tuple[int, int]:
    """Split a fetch budget of `total` fairly between two pools of sizes n_a/n_b.

    Each pool gets up to half; leftover from a pool that can't fill its half is
    handed to the other. Total taken == min(total, n_a + n_b).
    """
    half = total // 2
    a = min(n_a, half)
    b = min(n_b, total - a)
    a = min(n_a, total - b)  # reclaim if b under-filled
    return a, b


def _stride_take(items: list[Any], k: int) -> list[Any]:
    """At most `k` items, stride-sampled across `items` (not just the first k)."""
    if k <= 0:
        return []
    if len(items) <= k:
        return items
    stride = max(1, len(items) // k)
    return items[::stride][:k]


def cmd_accumulate_shorts(
    settings: Settings,
    grouped: PolygonGroupedSource,
    minute_bars: MinuteBarsProvider,
    shortability: Any,
    db: Database,
    date: str | None = None,
    run_days: int = 2,
    run_gain: float = 30.0,
    fade_trigger: float | None = None,
    exh_trigger: float = 2.0,
    exh_mode: str = "breakdown",
    sample: int = 200,
    lag_days: int = 1,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """Accumulate a forward, out-of-time dataset of *would-be* short outcomes.

    Alpaca can't short our target runners, so instead of paper-trading we record,
    per short setup (H-A fade crosser + H-B exhaustion run-end) for the target
    session: the entry-time features, the simulated intraday short outcome (live
    short rules), and the live shortable/easy_to_borrow status. Idempotent per
    (session, symbol, strategy, entry_mode) so re-runs refresh in place.

    `date=None` auto-resolves the latest available session ≥ `lag_days` old (for
    the unattended daily job — no hardcoded date, robust to weekends/holidays/lag).
    """
    settings.validate_paper_safety()
    if date is None:
        date = grouped.latest_session(lag_days=lag_days)
        if not date:
            print("accumulate-shorts: no session with grouped data in lookback window")
            return 0
    trigger = settings.min_day_change_pct if fade_trigger is None else fade_trigger
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec
    run_id = _start_run(settings, db, mode="accumulate-shorts")

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    # H-A fade crossers on the target session.
    today_rows = grouped.fetch_grouped(date)
    prev_by = {r["T"]: r for r in grouped.prev_session_rows(date) if r.get("T")}
    crossers = (
        polygon_grouped_crossers(
            today_rows, prev_by, settings.min_price, settings.max_price, trigger
        )
        if today_rows
        else []
    )
    setups = fade_setups(crossers, date, trigger)

    # H-B exhaustion run-ends whose short day == the target session. Build the
    # daily history back far enough to cover the run window (weekdays only).
    daily: dict[str, list[dict[str, Any]]] = {}
    target = datetime.fromisoformat(date).date()
    day = target - timedelta(days=run_days + 5)
    while day <= target:
        if day.weekday() < 5:
            rows = grouped.fetch_grouped(day.isoformat())
            for r in rows or []:
                sym = r.get("T")
                if sym and r.get("c"):
                    daily.setdefault(sym, []).append(
                        {
                            "date": day.isoformat(),
                            "open": r.get("o"),
                            "high": r.get("h"),
                            "low": r.get("l"),
                            "close": r["c"],
                        }
                    )
            if throttle and day != target:
                time.sleep(throttle)
        day += timedelta(days=1)
    run_ends = [
        e
        for e in qualifying_run_ends(
            daily, run_days, run_gain, settings.min_price, settings.max_price
        )
        if e.short_day_date == date
    ]
    exh = exhaustion_setups(run_ends, exh_trigger, exh_mode)

    # Split the minute-fetch budget fairly between the two strategies so neither
    # (fade's thousands of crossers, or a high-parabolic day's many run-ends) can
    # crowd out the other or blow past `sample`. Each side is stride-sampled
    # across its universe to its share.
    n_fade, n_exh = _split_budget(len(setups), len(exh), sample)
    setups = _stride_take(setups, n_fade) + _stride_take(exh, n_exh)

    ep = settings.build_exit_params()
    recorded = 0
    triggered_records: list[Any] = []
    n_shortable = 0
    for i, s in enumerate(setups):
        status = shortability.get_shortability(s.symbol)
        if status.get("shortable"):
            n_shortable += 1
        bars = minute_bars.fetch(s.symbol, s.session_date)
        if bars:
            rec = build_short_record(
                s,
                bars,
                ep,
                shortable=status.get("shortable", False),
                easy_to_borrow=status.get("easy_to_borrow", False),
                cost_fn=cost_fn,
                min_price=settings.min_price,
                max_price=settings.max_price,
            )
            if rec is not None:
                db.record_short_setup(rec, run_id=run_id)
                triggered_records.append(rec)
                recorded += 1
        if throttle and i < len(setups) - 1:
            time.sleep(throttle)

    n_fade = sum(1 for s in setups if s.strategy == "fade")
    n_exh = sum(1 for s in setups if s.strategy == "exhaustion")
    print(
        f"accumulate-shorts {date}: {len(setups)} setups "
        f"(fade {n_fade}, exhaustion {n_exh}) -> {recorded} triggered short setups recorded "
        f"({'GROSS' if base_cost == 0 else f'net {base_cost:g}%+cheap'})"
    )
    print(f"  shortable at Alpaca: {n_shortable}/{len(setups)} (the executability wall)")
    nets = [r.net_return_pct for r in triggered_records]
    if nets:
        print(f"  would-be short outcome: {_short_stats(nets)}")
    print(f"  total short_setups in db: {db.count_short_setups()}")
    return 0


def cmd_short_report(settings: Settings, db: Database) -> int:
    """Summarize the accumulated short_setups: would-be outcome by strategy ×
    borrowability. The (strategy, shortable=True) rows are the only executable
    ones — the key question is whether that subset carries any edge."""
    settings.validate_paper_safety()
    summary = summarize_short_setups(db.get_short_setups())
    if summary is None:
        print("No short_setups accumulated yet (run accumulate-shorts).")
        return 0
    print(f"Short-setup dataset: {summary['n']} setups over {summary['sessions']} session(s)")
    print(f"  {'strategy':11s} {'borrow':9s} {'n':>5s} {'avg%':>7s} {'med%':>7s} {'win%':>6s}")
    for g in summary["groups"]:
        borrow = "SHORTABLE" if g["shortable"] else "no-borrow"
        print(
            f"  {g['strategy']:11s} {borrow:9s} {g['n']:>5d} "
            f"{g['avg']:>7.1f} {g['median']:>7.1f} {g['win']:>6.0f}"
        )
    print(
        "  note: only SHORTABLE rows are executable retail — the rest are "
        "borrow-blocked (the limits-to-arbitrage wall)."
    )
    return 0


def _chrono_split(dates: list[str], train_frac: float, test_frac: float) -> dict[str, set[str]]:
    """Chronological train/test/holdout split of sorted session dates."""
    ordered = sorted(set(dates))
    n = len(ordered)
    n_tr = int(n * train_frac)
    n_te = int(n * test_frac)
    return {
        "train": set(ordered[:n_tr]),
        "test": set(ordered[n_tr : n_tr + n_te]),
        "holdout": set(ordered[n_tr + n_te :]),
    }


def cmd_long_search(
    settings: Settings,
    grouped: PolygonGroupedSource,
    minute_bars: MinuteBarsProvider,
    start: str,
    end: str,
    sample_per_day: int = 40,
    train_frac: float = 0.6,
    test_frac: float = 0.2,
    shapes: list[str] | None = None,
    entries: list[float] | None = None,
    take_profits: list[float] | None = None,
    max_holds: list[float] | None = None,
    trailings: list[float] | None = None,
    stops: list[float] | None = None,
    min_n: int = 20,
    top_k: int = 8,
    cost_pct: float | None = None,
    gross: bool = False,
    throttle_sec: int | None = None,
) -> int:
    """Search long entry shapes × exit params over the honest crosser universe
    with a chronological train/test/holdout split. Optimises on train, shows the
    out-of-sample test degradation for the top combos, and evaluates only the
    single best on holdout — so an overfit train winner is exposed, not shipped.
    Minute bars are cached, so re-runs over the same window are instant."""
    settings.validate_paper_safety()
    base_cost = 0.0 if gross else (settings.replay_cost_pct if cost_pct is None else cost_pct)
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec

    def cost_fn(price: float) -> float:
        return round_trip_cost(
            price, base_cost, settings.replay_cheap_price, settings.replay_cheap_extra_pct
        )

    # Build the crosser dataset over the window (cached minute fetches).
    by_date: dict[str, list[TradeInput]] = {}
    day = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    trigger = min(entries or [settings.min_day_change_pct])
    while day <= end_d:
        if day.weekday() < 5:
            iso = day.isoformat()
            today = grouped.fetch_grouped(iso)
            if today:
                prev_by = {r["T"]: r for r in grouped.prev_session_rows(iso) if r.get("T")}
                crossers = polygon_grouped_crossers(
                    today, prev_by, settings.min_price, settings.max_price, trigger
                )
                stride = max(1, len(crossers) // sample_per_day) if crossers else 1
                picks = crossers[::stride][:sample_per_day]
                for cr in picks:
                    bars = minute_bars.fetch(cr["symbol"], iso)
                    if bars:
                        by_date.setdefault(iso, []).append(
                            TradeInput(cr["symbol"], iso, cr["prev_close"], bars)
                        )
                    if throttle:
                        time.sleep(throttle)
        day += timedelta(days=1)

    if not by_date:
        print(f"long-search {start}..{end}: no crosser data in window.")
        return 0

    split_dates = _chrono_split(list(by_date), train_frac, test_frac)
    splits = {name: [t for d in dates for t in by_date[d]] for name, dates in split_dates.items()}
    grid = make_grid(
        shapes=shapes or ["chase", "pullback"],
        entry_min_change=entries or [settings.min_day_change_pct],
        stop=stops or [settings.stop_loss_pct],
        take_profit=take_profits or [0.10, 0.15],
        trailing=trailings or [settings.trailing_stop_pct],
        max_hold=max_holds or [60.0, 180.0],
    )
    results = search(splits, grid, settings.min_price, settings.max_price, cost_fn, min_n=min_n)

    print(
        f"long-search {start}..{end}: {sum(len(v) for v in by_date.values())} crosser-days over "
        f"{len(by_date)} sessions | split train {len(split_dates['train'])} / test "
        f"{len(split_dates['test'])} / holdout {len(split_dates['holdout'])} sessions | "
        f"{len(grid)} combos, {len(results)} with ≥{min_n} train entries "
        f"({'GROSS' if base_cost == 0 else f'net {base_cost:g}%+cheap'})"
    )
    if not results:
        print("  (no combo cleared the min-sample gate — widen window or lower --min-n)")
        return 0
    hdr = (
        f"  {'shape':9s} {'e%':>4s} {'tp':>4s} {'hold':>5s} {'trail':>5s} "
        f"{'trainN':>6s} {'trainAvg':>8s} {'trainWin':>8s} {'testAvg':>7s} {'testWin':>7s}"
    )
    print(hdr)
    for r in results[:top_k]:
        c = r["combo"]
        te = r["test"]
        te_avg = f"{te['avg']:+.1f}" if te else "—"
        te_win = f"{te['win_rate']:.0f}" if te else "—"
        print(
            f"  {c['shape']:9s} {c['entry_min_change']:>4.0f} {c['take_profit'] * 100:>4.0f} "
            f"{c['max_hold']:>5.0f} {c['trailing'] * 100:>5.0f} "
            f"{r['train']['n']:>6d} {r['train']['avg']:>+8.1f} {r['train']['win_rate']:>8.0f} "
            f"{te_avg:>7s} {te_win:>7s}"
        )

    # Touch holdout ONCE, for the single best-by-train combo (the honest estimate).
    best = results[0]
    hold = evaluate_combo(
        splits["holdout"], best["combo"], settings.min_price, settings.max_price, cost_fn
    )
    c = best["combo"]
    print(
        f"  --- best-by-train = {c['shape']} e{c['entry_min_change']:g} "
        f"tp{c['take_profit'] * 100:g} hold{c['max_hold']:g} trail{c['trailing'] * 100:g}"
    )
    if hold:
        print(
            f"      HOLDOUT (touched once): n={hold['n']} avg {hold['avg']:+.1f}% "
            f"med {hold['median']:+.1f}% win {hold['win_rate']:.0f}%"
        )
    else:
        print("      HOLDOUT: no entries (can't confirm)")
    print(
        f"  note: {len(results)} combos scored on train — the best train avg is upward-biased; "
        "trust the test/holdout columns, not train."
    )
    return 0


def cmd_xsearch(
    settings: Settings,
    grouped: PolygonGroupedSource,
    start: str,
    end: str,
    strategies: list[str] | None = None,
    formations: list[int] | None = None,
    holds: list[int] | None = None,
    quantiles: list[float] | None = None,
    min_price: float = 5.0,
    max_price: float = 1000.0,
    min_dollar_vol_m: float = 5.0,
    train_frac: float = 0.6,
    test_frac: float = 0.2,
    cost_bps: float = 10.0,
    min_n: int = 20,
    top_k: int = 12,
    throttle_sec: int | None = None,
) -> int:
    """Cross-sectional reversal/momentum search over a liquid daily universe.

    Ranks the universe by formation return, longs/shorts the quantile tails, holds
    N days; sweeps strategy × formation × hold × quantile with a chronological
    train/test/holdout split. Liquid (dollar-vol filtered) so the short leg is
    executable. All daily/grouped data — cheap, cached."""
    settings.validate_paper_safety()
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec
    cost_frac = cost_bps / 10000.0
    min_dollar_vol = min_dollar_vol_m * 1_000_000

    grouped_by_date: dict[str, list[dict[str, Any]]] = {}
    day = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    while day <= end_d:
        if day.weekday() < 5:
            iso = day.isoformat()
            rows = grouped.fetch_grouped(iso)
            if rows:
                grouped_by_date[iso] = rows
            elif throttle:
                time.sleep(throttle)  # only sleep on a live miss (cached hits are free)
        day += timedelta(days=1)

    panel = build_panel(grouped_by_date)
    ordered = sorted(panel)
    if len(ordered) < 10:
        print(f"xsearch {start}..{end}: only {len(ordered)} sessions — widen the window.")
        return 0
    split = _chrono_split(ordered, train_frac, test_frac)
    sub = {name: {d: panel[d] for d in ordered if d in dates} for name, dates in split.items()}

    grid = list(
        itertools.product(
            strategies or ["reversal", "momentum"],
            formations or [3, 5, 10],
            holds or [1, 3, 5],
            quantiles or [0.1, 0.2],
        )
    )
    scored = []
    for strat, form, hold, q in grid:
        tr = summarize_rebalances(
            cross_sectional_backtest(
                sub["train"], strat, form, hold, q, min_price, max_price, min_dollar_vol
            ),
            cost_frac,
        )
        if tr is None or tr["n"] < min_n:
            continue
        te = summarize_rebalances(
            cross_sectional_backtest(
                sub["test"], strat, form, hold, q, min_price, max_price, min_dollar_vol
            ),
            cost_frac,
        )
        scored.append({"strat": strat, "form": form, "hold": hold, "q": q, "train": tr, "test": te})
    scored.sort(key=lambda r: r["train"]["sharpe"], reverse=True)

    print(
        f"xsearch {start}..{end}: {len(ordered)} sessions | split "
        f"{len(split['train'])}/{len(split['test'])}/{len(split['holdout'])} | "
        f"{len(grid)} combos, {len(scored)} with ≥{min_n} train rebalances | "
        f"universe ${min_price:g}-{max_price:g}, ≥${min_dollar_vol_m:g}M/day, "
        f"cost {cost_bps:g}bps/leg"
    )
    if not scored:
        print("  (no combo cleared the gate — widen window / lower --min-n / relax liquidity)")
        return 0
    print(
        f"  {'strat':9s} {'form':>4s} {'hold':>4s} {'q':>4s} "
        f"{'trN':>4s} {'trAvg%':>7s} {'trWin':>6s} {'trShrp':>7s} {'teAvg%':>7s} {'teWin':>6s}"
    )
    for r in scored[:top_k]:
        te = r["test"]
        te_avg = f"{te['avg'] * 100:+.2f}" if te else "—"
        te_win = f"{te['win']:.0f}" if te else "—"
        print(
            f"  {r['strat']:9s} {r['form']:>4d} {r['hold']:>4d} {r['q']:>4.2f} "
            f"{r['train']['n']:>4d} {r['train']['avg'] * 100:>+7.2f} {r['train']['win']:>6.0f} "
            f"{r['train']['sharpe']:>7.2f} {te_avg:>7s} {te_win:>6s}"
        )

    best = scored[0]
    hold_stats = summarize_rebalances(
        cross_sectional_backtest(
            sub["holdout"],
            best["strat"],
            best["form"],
            best["hold"],
            best["q"],
            min_price,
            max_price,
            min_dollar_vol,
        ),
        cost_frac,
    )
    print(
        f"  --- best-by-train-sharpe = {best['strat']} form{best['form']} "
        f"hold{best['hold']} q{best['q']}"
    )
    if hold_stats:
        print(
            f"      HOLDOUT (touched once): n={hold_stats['n']} "
            f"avg {hold_stats['avg'] * 100:+.2f}% win {hold_stats['win']:.0f}% "
            f"sharpe {hold_stats['sharpe']:+.2f}"
        )
    else:
        print("      HOLDOUT: too few rebalances to confirm")
    print(
        f"  note: {len(scored)} combos scored — train sharpe is upward-biased; trust test/holdout."
    )
    return 0


def _panel_to_series(panel: dict[str, Any]) -> dict[str, dict[str, list]]:
    """Pivot {date:{symbol:{close,dollar_vol}}} to per-symbol time series."""
    series: dict[str, dict[str, list]] = {}
    for d in sorted(panel):
        for sym, rec in panel[d].items():
            s = series.setdefault(sym, {"dates": [], "closes": [], "dvols": []})
            s["dates"].append(d)
            s["closes"].append(rec["close"])
            s["dvols"].append(rec["dollar_vol"])
    return series


def cmd_mrsearch(
    settings: Settings,
    grouped: PolygonGroupedSource,
    start: str,
    end: str,
    rsi_periods: list[int] | None = None,
    entry_rsis: list[float] | None = None,
    exit_rsis: list[float] | None = None,
    ma_periods: list[int] | None = None,
    max_holds: list[int] | None = None,
    min_price: float = 5.0,
    max_price: float = 1000.0,
    min_dvol_m: float = 5.0,
    train_frac: float = 0.6,
    test_frac: float = 0.2,
    cost_bps: float = 10.0,
    min_n: int = 30,
    top_k: int = 12,
    throttle_sec: int | None = None,
) -> int:
    """Short-term mean-reversion (RSI-2) search over a liquid universe. Per symbol,
    dip-buy in an uptrend; sweep rsi/entry/exit/ma/hold, bucket trades by entry
    date into train/test/holdout, rank by train sharpe, holdout the best once.
    Behavioral-overreaction family — distinct from cross-sectional ranking."""
    settings.validate_paper_safety()
    throttle = settings.outcome_throttle_sec if throttle_sec is None else throttle_sec
    cost_frac = cost_bps / 10000.0
    min_dvol = min_dvol_m * 1_000_000

    grouped_by_date: dict[str, list[dict[str, Any]]] = {}
    day = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    while day <= end_d:
        if day.weekday() < 5:
            iso = day.isoformat()
            rows = grouped.fetch_grouped(iso)
            if rows:
                grouped_by_date[iso] = rows
            elif throttle:
                time.sleep(throttle)
        day += timedelta(days=1)

    panel = build_panel(grouped_by_date)
    ordered = sorted(panel)
    if len(ordered) < 40:
        print(f"mrsearch {start}..{end}: only {len(ordered)} sessions — widen (MA needs warmup).")
        return 0
    split = _chrono_split(ordered, train_frac, test_frac)
    series = _panel_to_series(panel)

    grid = list(
        itertools.product(
            rsi_periods or [2],
            entry_rsis or [5.0, 10.0],
            exit_rsis or [50.0, 70.0],
            ma_periods or [100, 200],
            max_holds or [3, 5, 10],
        )
    )
    scored = []
    for rp, er, xr, ma, mh in grid:
        buckets: dict[str, list] = {"train": [], "test": [], "holdout": []}
        for s in series.values():
            for t in mean_reversion_trades(
                s["dates"],
                s["closes"],
                s["dvols"],
                rsi_period=rp,
                entry_rsi=er,
                exit_rsi=xr,
                ma_period=ma,
                max_hold=mh,
                min_price=min_price,
                max_price=max_price,
                min_dollar_vol=min_dvol,
                cost_frac=cost_frac,
            ):
                for name, dates in split.items():
                    if t["entry_date"] in dates:
                        buckets[name].append(t)
                        break
        tr = summarize_mr(buckets["train"])
        if tr is None or tr["n"] < min_n:
            continue
        te = summarize_mr(buckets["test"])
        scored.append(
            {
                "rp": rp,
                "er": er,
                "xr": xr,
                "ma": ma,
                "mh": mh,
                "train": tr,
                "test": te,
                "hold_trades": buckets["holdout"],
            }
        )
    scored.sort(key=lambda r: r["train"]["sharpe"], reverse=True)

    print(
        f"mrsearch {start}..{end}: {len(ordered)} sessions, {len(series)} symbols | split "
        f"{len(split['train'])}/{len(split['test'])}/{len(split['holdout'])} | "
        f"{len(grid)} combos, {len(scored)} with ≥{min_n} train trades | "
        f"${min_price:g}-{max_price:g}, ≥${min_dvol_m:g}M/day, cost {cost_bps:g}bps/leg"
    )
    if not scored:
        print("  (no combo cleared the gate — widen window / lower --min-n)")
        return 0
    print(
        f"  {'rsi':>3s} {'ent':>3s} {'ext':>3s} {'ma':>4s} {'hold':>4s} "
        f"{'trN':>5s} {'trAvg%':>7s} {'trWin':>6s} {'trShrp':>7s} {'teAvg%':>7s} {'teWin':>6s}"
    )
    for r in scored[:top_k]:
        te = r["test"]
        te_avg = f"{te['avg'] * 100:+.2f}" if te else "—"
        te_win = f"{te['win']:.0f}" if te else "—"
        print(
            f"  {r['rp']:>3d} {r['er']:>3.0f} {r['xr']:>3.0f} {r['ma']:>4d} {r['mh']:>4d} "
            f"{r['train']['n']:>5d} {r['train']['avg'] * 100:>+7.2f} {r['train']['win']:>6.0f} "
            f"{r['train']['sharpe']:>7.2f} {te_avg:>7s} {te_win:>6s}"
        )

    best = scored[0]
    hold_stats = summarize_mr(best["hold_trades"])
    print(
        f"  --- best-by-train-sharpe = rsi{best['rp']} ent{best['er']:g} ext{best['xr']:g} "
        f"ma{best['ma']} hold{best['mh']}"
    )
    if hold_stats:
        print(
            f"      HOLDOUT (touched once): n={hold_stats['n']} "
            f"avg {hold_stats['avg'] * 100:+.2f}% win {hold_stats['win']:.0f}% "
            f"sharpe {hold_stats['sharpe']:+.2f}"
        )
    else:
        print("      HOLDOUT: too few trades to confirm")
    print(f"  note: {len(scored)} combos scored — train sharpe upward-biased; trust test/holdout.")
    return 0


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
    p_intra = sub.add_parser("intraday", help="intraday hit-and-run backtest over minute bars")
    p_intra.add_argument("--limit", type=int, default=40, help="runners to sample (Polygon calls)")
    p_intra.add_argument("--sweep-entry", help="comma-separated entry-trigger %% (e.g. 3,5,8,12)")
    p_intra.add_argument(
        "--sweep-take-profit", help="comma-separated take-profit %% (e.g. 6,8,10,15)"
    )
    p_intra.add_argument(
        "--sweep-max-hold", help="comma-separated max-hold minutes (e.g. 15,30,60)"
    )
    p_intra.add_argument("--sweep-trailing", help="comma-separated trailing-stop %% (e.g. 8,15,25)")
    p_intra.add_argument("--source", help="filter screened source (polygon|watchlist)")
    p_intra.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_intra.add_argument("--gross", action="store_true", help="ignore costs")
    p_intra.add_argument(
        "--train-end", help="out-of-sample split date YYYY-MM-DD (train <= / test >)"
    )
    p_cross = sub.add_parser(
        "crosser", help="survivorship-inclusive backtest (all intraday crossers, fizzles incl.)"
    )
    p_cross.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    p_cross.add_argument(
        "--sample", type=int, default=150, help="crossers to sample (Polygon calls)"
    )
    p_cross.add_argument(
        "--entry", type=float, help="intraday cross trigger %% (default MIN_DAY_CHANGE_PCT)"
    )
    p_cross.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_cross.add_argument("--gross", action="store_true", help="ignore costs")
    p_fade = sub.add_parser(
        "fade", help="fade/SHORT backtest: short every crosser, measure the squeeze tail"
    )
    p_fade.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    p_fade.add_argument("--sample", type=int, default=150, help="crossers to sample")
    p_fade.add_argument("--entry", type=float, help="cross trigger %% (default MIN_DAY_CHANGE_PCT)")
    p_fade.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_fade.add_argument("--gross", action="store_true", help="ignore costs")
    p_feat = sub.add_parser(
        "features", help="entry-time feature separability (winners vs fizzles at the cross)"
    )
    p_feat.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    p_feat.add_argument("--sample", type=int, default=150, help="crossers to sample")
    p_feat.add_argument("--entry", type=float, help="cross trigger %% (default MIN_DAY_CHANGE_PCT)")
    p_feat.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_feat.add_argument("--gross", action="store_true", help="ignore costs")
    p_exh = sub.add_parser(
        "exhaustion", help="first-red-day / multi-day exhaustion SHORT concept test (H-B)"
    )
    p_exh.add_argument("--start", required=True, help="range start YYYY-MM-DD")
    p_exh.add_argument("--end", required=True, help="range end YYYY-MM-DD")
    p_exh.add_argument("--run-days", type=int, default=3, help="parabolic run length (sessions)")
    p_exh.add_argument("--run-gain", type=float, default=50.0, help="min run gain %% over run-days")
    p_exh.add_argument("--fwd-days", type=int, default=1, help="swing horizon (sessions)")
    p_exh.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_exh.add_argument("--gross", action="store_true", help="ignore costs")
    p_exh2 = sub.add_parser(
        "exhaustion-intraday",
        help="exhaustion SHORT v2 — realistic intraday entry, no EOD/look-ahead bias (H-B v2)",
    )
    p_exh2.add_argument("--start", required=True, help="range start YYYY-MM-DD")
    p_exh2.add_argument("--end", required=True, help="range end YYYY-MM-DD")
    p_exh2.add_argument("--run-days", type=int, default=2, help="parabolic run length (sessions)")
    p_exh2.add_argument(
        "--run-gain", type=float, default=30.0, help="min run gain %% over run-days"
    )
    p_exh2.add_argument(
        "--entry", type=float, default=2.0, help="intraday up-break %% above run-end close"
    )
    p_exh2.add_argument("--sample", type=int, default=150, help="max candidates to minute-fetch")
    p_exh2.add_argument(
        "--mode",
        choices=["breakout", "breakdown"],
        default="breakout",
        help="breakout=fade the up-push; breakdown=short loss of prior close (first-red-day)",
    )
    p_exh2.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_exh2.add_argument("--gross", action="store_true", help="ignore costs")
    p_acc = sub.add_parser(
        "accumulate-shorts",
        help="record would-be short outcomes + shortable status for a session (forward dataset)",
    )
    p_acc.add_argument(
        "--date", help="target session YYYY-MM-DD (default: latest available, lagged)"
    )
    p_acc.add_argument(
        "--lag-days", type=int, default=1, help="min session age when auto-resolving --date"
    )
    p_acc.add_argument("--run-days", type=int, default=2, help="exhaustion run length (sessions)")
    p_acc.add_argument("--run-gain", type=float, default=30.0, help="min run gain %% over run-days")
    p_acc.add_argument(
        "--fade-trigger", type=float, help="fade up-cross %% (default min_day_change)"
    )
    p_acc.add_argument(
        "--exh-trigger", type=float, default=2.0, help="exhaustion break %% vs run-end"
    )
    p_acc.add_argument(
        "--exh-mode",
        choices=["breakout", "breakdown"],
        default="breakdown",
        help="exhaustion entry: breakdown=short loss of prior close (default)",
    )
    p_acc.add_argument("--sample", type=int, default=200, help="max setups to minute-fetch")
    p_acc.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_acc.add_argument("--gross", action="store_true", help="ignore costs")
    sub.add_parser(
        "short-report",
        help="summarize accumulated short_setups by strategy × borrowability",
    )
    p_ls = sub.add_parser(
        "long-search",
        help="search long entry shapes × params over crossers with train/test/holdout",
    )
    p_ls.add_argument("--start", required=True, help="window start YYYY-MM-DD")
    p_ls.add_argument("--end", required=True, help="window end YYYY-MM-DD")
    p_ls.add_argument("--sample", type=int, default=40, help="max crossers/day to fetch")
    p_ls.add_argument("--train-frac", type=float, default=0.6, help="train fraction of sessions")
    p_ls.add_argument("--test-frac", type=float, default=0.2, help="test fraction (rest=holdout)")
    p_ls.add_argument(
        "--shapes", default="chase,pullback,orb,gap", help="csv: chase,pullback,orb,gap"
    )
    p_ls.add_argument("--entries", help="csv entry %% triggers (default min_day_change)")
    p_ls.add_argument("--take-profits", default="0.10,0.15", help="csv take-profit fractions")
    p_ls.add_argument("--max-holds", default="60,180", help="csv max-hold minutes")
    p_ls.add_argument("--trailings", help="csv trailing-stop fractions (default settings)")
    p_ls.add_argument("--stops", help="csv stop-loss fractions (default settings)")
    p_ls.add_argument("--min-n", type=int, default=20, help="min train entries to keep a combo")
    p_ls.add_argument("--top-k", type=int, default=8, help="rows to print")
    p_ls.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_ls.add_argument("--gross", action="store_true", help="ignore costs")
    p_xs = sub.add_parser(
        "xsearch",
        help="cross-sectional reversal/momentum search over a liquid daily universe",
    )
    p_xs.add_argument("--start", required=True, help="window start YYYY-MM-DD")
    p_xs.add_argument("--end", required=True, help="window end YYYY-MM-DD")
    p_xs.add_argument("--strategies", default="reversal,momentum", help="csv: reversal,momentum")
    p_xs.add_argument("--formations", default="3,5,10", help="csv formation lookback (sessions)")
    p_xs.add_argument("--holds", default="1,3,5", help="csv hold length (sessions)")
    p_xs.add_argument("--quantiles", default="0.1,0.2", help="csv tail fraction each side")
    p_xs.add_argument("--min-price", type=float, default=5.0, help="universe min price")
    p_xs.add_argument("--max-price", type=float, default=1000.0, help="universe max price")
    p_xs.add_argument("--min-dvol", type=float, default=5.0, help="min $ volume/day, millions")
    p_xs.add_argument("--train-frac", type=float, default=0.6)
    p_xs.add_argument("--test-frac", type=float, default=0.2)
    p_xs.add_argument("--cost-bps", type=float, default=10.0, help="round-trip cost, bps per leg")
    p_xs.add_argument("--min-n", type=int, default=20, help="min train rebalances to keep a combo")
    p_xs.add_argument("--top-k", type=int, default=12)
    p_mr = sub.add_parser(
        "mrsearch",
        help="short-term mean-reversion (RSI-2) search over a liquid universe",
    )
    p_mr.add_argument("--start", required=True, help="window start YYYY-MM-DD")
    p_mr.add_argument("--end", required=True, help="window end YYYY-MM-DD")
    p_mr.add_argument("--rsi-periods", default="2", help="csv RSI lookback")
    p_mr.add_argument("--entry-rsis", default="5,10", help="csv oversold entry thresholds")
    p_mr.add_argument("--exit-rsis", default="50,70", help="csv bounce exit thresholds")
    p_mr.add_argument("--ma-periods", default="100,200", help="csv regime SMA length")
    p_mr.add_argument("--max-holds", default="3,5,10", help="csv max hold days")
    p_mr.add_argument("--min-price", type=float, default=5.0)
    p_mr.add_argument("--max-price", type=float, default=1000.0)
    p_mr.add_argument("--min-dvol", type=float, default=5.0, help="min $ volume/day, millions")
    p_mr.add_argument("--train-frac", type=float, default=0.6)
    p_mr.add_argument("--test-frac", type=float, default=0.2)
    p_mr.add_argument("--cost-bps", type=float, default=10.0, help="round-trip cost, bps per leg")
    p_mr.add_argument("--min-n", type=int, default=30, help="min train trades to keep a combo")
    p_mr.add_argument("--top-k", type=int, default=12)

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

    if args.command == "intraday":
        try:
            minute_bars = _minute_provider(settings)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_intraday(
            settings,
            db,
            minute_bars,
            limit=args.limit,
            sweep_entry=_parse_floats(args.sweep_entry),
            sweep_tp=_parse_floats(args.sweep_take_profit),
            sweep_hold=_parse_floats(args.sweep_max_hold),
            sweep_trail=_parse_floats(args.sweep_trailing),
            cost_pct=args.cost_pct,
            gross=args.gross,
            source=args.source,
            train_end=args.train_end,
        )

    if args.command == "crosser":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
            minute_bars = _minute_provider(settings)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_crosser(
            settings,
            grouped,
            minute_bars,
            date=args.date,
            sample=args.sample,
            entry_trigger=args.entry,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "fade":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
            minute_bars = _minute_provider(settings)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_fade(
            settings,
            grouped,
            minute_bars,
            date=args.date,
            sample=args.sample,
            entry_trigger=args.entry,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "features":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
            minute_bars = _minute_provider(settings)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_features(
            settings,
            grouped,
            minute_bars,
            date=args.date,
            sample=args.sample,
            entry_trigger=args.entry,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "exhaustion":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_exhaustion(
            settings,
            grouped,
            start=args.start,
            end=args.end,
            run_days=args.run_days,
            run_gain=args.run_gain,
            fwd_days=args.fwd_days,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "exhaustion-intraday":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
            minute_bars = _minute_provider(settings)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_exhaustion_intraday(
            settings,
            grouped,
            minute_bars,
            start=args.start,
            end=args.end,
            run_days=args.run_days,
            run_gain=args.run_gain,
            entry_trigger=args.entry,
            sample=args.sample,
            entry_mode=args.mode,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "accumulate-shorts":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
            minute_bars = _minute_provider(settings)
            shortability = AlpacaTradingClient(
                settings.alpaca_api_key, settings.alpaca_secret_key, paper=True
            )
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_accumulate_shorts(
            settings,
            grouped,
            minute_bars,
            shortability,
            db,
            date=args.date,
            run_days=args.run_days,
            run_gain=args.run_gain,
            fade_trigger=args.fade_trigger,
            exh_trigger=args.exh_trigger,
            exh_mode=args.exh_mode,
            sample=args.sample,
            lag_days=args.lag_days,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "short-report":
        return cmd_short_report(settings, db)

    if args.command == "long-search":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
            minute_bars = _minute_provider(settings)
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_long_search(
            settings,
            grouped,
            minute_bars,
            start=args.start,
            end=args.end,
            sample_per_day=args.sample,
            train_frac=args.train_frac,
            test_frac=args.test_frac,
            shapes=[s.strip() for s in args.shapes.split(",") if s.strip()],
            entries=_parse_floats(args.entries),
            take_profits=_parse_floats(args.take_profits),
            max_holds=_parse_floats(args.max_holds),
            trailings=_parse_floats(args.trailings),
            stops=_parse_floats(args.stops),
            min_n=args.min_n,
            top_k=args.top_k,
            cost_pct=args.cost_pct,
            gross=args.gross,
        )

    if args.command == "xsearch":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_xsearch(
            settings,
            grouped,
            start=args.start,
            end=args.end,
            strategies=[s.strip() for s in args.strategies.split(",") if s.strip()],
            formations=[int(x) for x in args.formations.split(",") if x.strip()],
            holds=[int(x) for x in args.holds.split(",") if x.strip()],
            quantiles=_parse_floats(args.quantiles),
            min_price=args.min_price,
            max_price=args.max_price,
            min_dollar_vol_m=args.min_dvol,
            train_frac=args.train_frac,
            test_frac=args.test_frac,
            cost_bps=args.cost_bps,
            min_n=args.min_n,
            top_k=args.top_k,
        )

    if args.command == "mrsearch":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(
                settings.polygon_api_key, bounds, cache_path=settings.grouped_cache_path
            )
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return cmd_mrsearch(
            settings,
            grouped,
            start=args.start,
            end=args.end,
            rsi_periods=[int(x) for x in args.rsi_periods.split(",") if x.strip()],
            entry_rsis=_parse_floats(args.entry_rsis),
            exit_rsis=_parse_floats(args.exit_rsis),
            ma_periods=[int(x) for x in args.ma_periods.split(",") if x.strip()],
            max_holds=[int(x) for x in args.max_holds.split(",") if x.strip()],
            min_price=args.min_price,
            max_price=args.max_price,
            min_dvol_m=args.min_dvol,
            train_frac=args.train_frac,
            test_frac=args.test_frac,
            cost_bps=args.cost_bps,
            min_n=args.min_n,
            top_k=args.top_k,
        )

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
