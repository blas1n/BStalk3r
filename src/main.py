"""BStalk3r CLI — connect check, scan, realtime loop, end-of-day report.

The realtime loop is pure rule-based and synchronous: scan -> exits -> entries,
sleep, repeat. No LLM / AI is invoked here by design (speed + determinism).
"""

from __future__ import annotations

import argparse
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
from src.forward_bars import ForwardBarsProvider, PolygonDailyBars
from src.intraday import (
    aggregate,
    bucket_by_feature,
    entry_features,
    reconstruct_entry,
    simulate_trade,
)
from src.market_data import AlpacaMarketData, MarketDataProvider
from src.minute_bars import MinuteBarsProvider, PolygonMinuteBars
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
    polygon_grouped_crossers,
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
    p_feat = sub.add_parser(
        "features", help="entry-time feature separability (winners vs fizzles at the cross)"
    )
    p_feat.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    p_feat.add_argument("--sample", type=int, default=150, help="crossers to sample")
    p_feat.add_argument("--entry", type=float, help="cross trigger %% (default MIN_DAY_CHANGE_PCT)")
    p_feat.add_argument("--cost-pct", type=float, help="round-trip cost %% override")
    p_feat.add_argument("--gross", action="store_true", help="ignore costs")

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
            minute_bars = PolygonMinuteBars(settings.polygon_api_key)
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
            grouped = PolygonGroupedSource(settings.polygon_api_key, bounds)
            minute_bars = PolygonMinuteBars(settings.polygon_api_key)
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

    if args.command == "features":
        try:
            bounds = ScreenBounds(
                settings.min_price, settings.max_price, settings.min_day_change_pct
            )
            grouped = PolygonGroupedSource(settings.polygon_api_key, bounds)
            minute_bars = PolygonMinuteBars(settings.polygon_api_key)
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
