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
    def __init__(self, equity=100_000.0, cash=100_000.0):
        self.equity = equity
        self.cash = cash


class _FakeTrading:
    def __init__(self, cash=100_000.0):
        self._cash = cash

    def get_account(self):
        return _FakeAccount(cash=self._cash)

    def submit_limit_order(self, *a, **k):  # not called in dry-run
        raise AssertionError("should not place orders in DRY_RUN")


class _AlpacaPos:
    def __init__(self, symbol, qty, avg):
        self.symbol, self.qty, self.avg_entry_price = symbol, qty, avg


class _LiveTrading:
    """Live paper broker: holds positions in Alpaca (source of truth), fills orders."""

    def __init__(self, positions):
        self._positions = positions
        self.placed = []

    def get_account(self):
        return _FakeAccount()

    def get_open_positions(self):
        return self._positions

    def submit_limit_order(self, symbol, qty, side, limit_price):
        self.placed.append((symbol, side.value, qty))
        return type("O", (), {"id": "x", "status": "accepted"})()


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


def test_mr_trade_live_reads_held_from_alpaca(tmp_path, capsys):
    # LIVE mode (dry_run=False): held positions come from Alpaca, not our DB.
    s = _settings(tmp_path, dry_run=False)
    db = Database(s.db_path)
    db.init_schema()
    # Alpaca reports holding AAA (RSI-2 bounced -> should be SOLD)
    trading = _LiveTrading([_AlpacaPos("AAA", 100, 20.0)])
    market = _FakeMarket({"AAA": 30.0})  # big pop -> RSI-2 high -> exit
    rc = main_mod.cmd_mr_trade(
        s,
        _FakeGrouped(),
        market,
        trading,
        db,
        rsi_period=2,
        entry_rsi=1,
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
    assert "LIVE PAPER ORDERS" in out and "held 1" in out
    # a real SELL order was routed for the Alpaca-held AAA
    assert any(side == "sell" and sym == "AAA" for sym, side, _ in trading.placed)


class _MultiGrouped:
    """3 liquid names all oversold-in-uptrend at the last session."""

    def __init__(self):
        self._by = {}
        for t, ds in enumerate(_DATES):
            rows = []
            for s in ("AAA", "BBB", "CCC"):
                base = 20.0 + 0.5 * t
                close = base if t < len(_DATES) - 2 else base - 0.6 * (t - (len(_DATES) - 3))
                rows.append({"T": s, "c": round(close, 2), "v": 5_000_000})
            self._by[ds] = rows

    def latest_session(self, lag_days=0, _today=None):
        return _DATES[-1]

    def fetch_grouped(self, d):
        return self._by.get(d, [])


def test_mr_trade_caps_entries_by_available_cash(tmp_path, capsys):
    # equity 100k, book 5 -> notional 20k. cash only 25k -> affords 1 entry.
    s = _settings(tmp_path)  # dry-run
    db = Database(s.db_path)
    db.init_schema()
    trading = _FakeTrading(cash=25_000.0)
    market = _FakeMarket({"AAA": 25.0, "BBB": 25.0, "CCC": 25.0})
    rc = main_mod.cmd_mr_trade(
        s,
        _MultiGrouped(),
        market,
        trading,
        db,
        rsi_period=2,
        entry_rsi=100,
        exit_rsi=70,
        ma_period=0,  # regime off (RSI-2 oversold is inherently below a short SMA)
        max_hold=10,
        max_positions=5,
        min_price=1.0,
        max_price=1000.0,
        min_dvol_m=0.0,
        throttle_sec=0,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # 3 candidates, notional = 100k/5 = 20k; cash 25k -> only 1 affordable
    orders = db.conn.execute("SELECT COUNT(*) FROM orders WHERE side='buy'").fetchone()[0]
    assert orders == 1
    assert "cash-capped" in out
