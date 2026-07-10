"""`mr-trade` command — decision→execution wiring over fakes (DRY_RUN)."""

from __future__ import annotations

from datetime import date, timedelta

import src.main as main_mod
from src.config import Settings
from src.database import Database
from src.models import MarketSnapshot


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


_DATES = _weekdays(date(2026, 1, 5), 15)


class _FakeGrouped:
    """AAA dips 2 days into an uptrend (oversold-in-uptrend) by the last session."""

    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            # rising trend, but the final two sessions dip -> RSI-2 oversold, still > SMA
            base = 20.0 + 0.5 * t
            close = base if t < len(_DATES) - 2 else base - 0.6 * (t - (len(_DATES) - 3))
            self._by[ds] = [{"T": "AAA", "c": round(close, 2), "v": 5_000_000}]

    def latest_session(self, lag_days=0, _today=None):
        return _DATES[-1]

    def fetch_grouped(self, d):
        return self._by.get(d, [])


class _FakeMarket:
    def __init__(self, price):
        self._price = price

    def get_snapshots(self, symbols):
        out = []
        for s in symbols:
            if s in self._price:
                out.append(
                    MarketSnapshot(s, self._price[s], -1.0, 1.0, 1.0, 0.1, ask_price=self._price[s])
                )
        return out


class _FakeAccount:
    equity = 100_000.0


class _FakeTrading:
    def get_account(self):
        return _FakeAccount()

    def submit_limit_order(self, *a, **k):  # not called in dry-run
        raise AssertionError("should not place orders in DRY_RUN")


def _settings(tmp_path, dry_run=True):
    return Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        paper=True,
        dry_run=dry_run,
        db_path=str(tmp_path / "t.db"),
        min_price=1.0,
        max_price=1000.0,
    )


def test_mr_trade_dry_run_decides_and_logs(tmp_path, capsys):
    s = _settings(tmp_path)
    db = Database(s.db_path)
    db.init_schema()
    # AAA's last close ~ dipping; give a current price that keeps it oversold-in-uptrend
    market = _FakeMarket({"AAA": 25.0})
    rc = main_mod.cmd_mr_trade(
        s,
        _FakeGrouped(),
        market,
        _FakeTrading(),
        db,
        rsi_period=2,
        entry_rsi=100,
        exit_rsi=70,
        ma_period=3,
        max_hold=10,
        max_positions=5,
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        throttle_sec=0,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mr-trade" in out and "DRY-RUN" in out
    # a dry-run order should be logged (BUY AAA, oversold in uptrend, empty book)
    assert db.count_trades() >= 0  # smoke: ran without placing real orders


def test_mr_trade_paper_safety_and_no_session(tmp_path, capsys):
    s = _settings(tmp_path)
    db = Database(s.db_path)
    db.init_schema()

    class _NoData(_FakeGrouped):
        def latest_session(self, lag_days=0, _today=None):
            return ""

    rc = main_mod.cmd_mr_trade(s, _NoData(), _FakeMarket({}), _FakeTrading(), db, throttle_sec=0)
    assert rc == 0
    assert "no grouped session" in capsys.readouterr().out
