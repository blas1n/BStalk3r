"""Market data providers behind a stable interface.

v0 ships an Alpaca/IEX implementation that evaluates a configured watchlist
(the free IEX feed cannot screen the whole market). Swap in a Polygon/Finnhub
screener later by implementing `MarketDataProvider` — the rest of the system is
agnostic to the source.

Caveats (free IEX feed):
- RVOL here = today's cumulative volume / 20-day average daily volume. It
  understates RVOL early in the session; good enough as a v0 momentum proxy.
- float / market cap are not available from this feed -> left as None.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame

from src.models import MarketSnapshot


class FundamentalsProvider(Protocol):
    def float_shares(self, symbol: str) -> float | None: ...
    def market_cap(self, symbol: str) -> float | None: ...


class NullFundamentals:
    """No fundamentals source wired yet — everything nullable (v0 default)."""

    def float_shares(self, symbol: str) -> float | None:
        return None

    def market_cap(self, symbol: str) -> float | None:
        return None


class MarketDataProvider(Protocol):
    def get_snapshots(self, symbols: list[str]) -> list[MarketSnapshot]: ...


def _feed(name: str) -> DataFeed:
    return DataFeed.SIP if name.lower() == "sip" else DataFeed.IEX


class AlpacaMarketData:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        data_feed: str = "iex",
        fundamentals: FundamentalsProvider | None = None,
    ):
        self._client = StockHistoricalDataClient(api_key, secret_key)
        self._feed = _feed(data_feed)
        self._fundamentals = fundamentals or NullFundamentals()

    def get_snapshots(self, symbols: list[str]) -> list[MarketSnapshot]:
        if not symbols:
            return []
        snap = self._client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols, feed=self._feed)
        )
        avg_vol = self._avg_daily_volume(symbols)
        vol_accel = self._volume_acceleration(symbols)

        out: list[MarketSnapshot] = []
        for symbol in symbols:
            s = snap.get(symbol)
            if s is None:
                continue
            built = self._build(symbol, s, avg_vol.get(symbol), vol_accel.get(symbol, 1.0))
            if built is not None:
                out.append(built)
        return out

    def _build(self, symbol, s, avg_volume, vol_accel) -> MarketSnapshot | None:
        trade = getattr(s, "latest_trade", None)
        quote = getattr(s, "latest_quote", None)
        daily = getattr(s, "daily_bar", None)
        prev = getattr(s, "previous_daily_bar", None)

        last_price = getattr(trade, "price", None) or getattr(daily, "close", None)
        if not last_price:
            return None

        ask = getattr(quote, "ask_price", None)
        bid = getattr(quote, "bid_price", None)
        spread_pct = ((ask - bid) / last_price * 100) if (ask and bid and ask > 0) else 0.0

        prev_close = getattr(prev, "close", None)
        day_change_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else 0.0

        today_vol = getattr(daily, "volume", 0) or 0
        rvol = (today_vol / avg_volume) if avg_volume else 0.0

        return MarketSnapshot(
            symbol=symbol,
            last_price=float(last_price),
            day_change_pct=float(day_change_pct),
            rvol=float(rvol),
            volume_acceleration=float(vol_accel),
            spread_pct=float(spread_pct),
            bid_price=float(bid) if bid else None,
            ask_price=float(ask) if ask else None,
            float_shares=self._fundamentals.float_shares(symbol),
            market_cap=self._fundamentals.market_cap(symbol),
            timestamp=datetime.now(UTC),
        )

    def _avg_daily_volume(self, symbols: list[str], lookback_days: int = 20) -> dict[str, float]:
        end = datetime.now(UTC)
        start = end - timedelta(days=lookback_days * 2 + 5)  # pad for weekends/holidays
        bars = self._client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=self._feed,
            )
        )
        out: dict[str, float] = {}
        data = getattr(bars, "data", {}) or {}
        for symbol in symbols:
            vols = [b.volume for b in data.get(symbol, [])][-lookback_days:]
            if vols:
                out[symbol] = sum(vols) / len(vols)
        return out

    def _volume_acceleration(self, symbols: list[str]) -> dict[str, float]:
        """Last 1-minute volume vs mean of the previous 5 minutes."""
        end = datetime.now(UTC)
        start = end - timedelta(minutes=15)
        bars = self._client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                feed=self._feed,
            )
        )
        out: dict[str, float] = {}
        data = getattr(bars, "data", {}) or {}
        for symbol in symbols:
            vols = [b.volume for b in data.get(symbol, [])]
            if len(vols) >= 6:
                prev5 = sum(vols[-6:-1]) / 5
                out[symbol] = (vols[-1] / prev5) if prev5 else 1.0
            else:
                out[symbol] = 1.0
        return out
