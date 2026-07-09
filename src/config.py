"""Typed configuration via pydantic-settings (single source of truth).

Loads from environment / .env, validates at startup, and builds the frozen
param objects consumed by the pure rule modules. Includes a hard paper-trading
safety guard — the app refuses to run against the live endpoint.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.execution import ExecParams
from src.risk import RiskParams
from src.scanner import ScanFilters
from src.strategy import EntryParams, ExitParams

_PAPER_HOST = "paper-api.alpaca.markets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- Alpaca / safety ----
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    paper: bool = True
    data_feed: str = "iex"

    dry_run: bool = True
    db_path: str = "data/bstalk3r.db"
    report_dir: str = "reports"
    # Persistent minute-bar cache so iterative backtests don't re-hit the
    # rate-limited API. Empty string disables caching (always fetch live).
    minute_cache_path: str = "data/minute_cache.db"
    # Persistent grouped-daily cache (the other repeated backtest fetch). Empty
    # disables. Only non-empty results are cached (too-recent 403s may land later).
    grouped_cache_path: str = "data/grouped_cache.db"
    # Persistent per-day news cache (scored Polygon headlines). Empty disables.
    news_cache_path: str = "data/news_cache.db"

    # universe source: "watchlist" | "polygon"
    universe_source: str = "watchlist"
    universe: str = "AAPL,TSLA,NVDA,AMD,SOFI,PLTR"
    polygon_api_key: str = ""
    screener_top_n: int = 50
    # False -> grouped daily bars (free tier, EOD). True -> snapshot gainers
    # (intraday, requires a paid Polygon plan).
    polygon_intraday: bool = False

    # Outcome tracking: only track runners at least this many days old (free-tier
    # forward bars lag), throttle Polygon calls (5 req/min limit), and bound how
    # many runners one `track` run drains so a backlog spreads over days within
    # the rate budget instead of bursting into 429s.
    outcome_lag_days: int = 8
    outcome_throttle_sec: int = 13
    outcome_track_limit: int = 40

    # Replay transaction-cost assumption (round-trip %). Low-priced names get a
    # surcharge (wider spreads). Research assumption — EOD data has no real quote.
    replay_cost_pct: float = 2.0
    replay_cheap_price: float = 2.0
    replay_cheap_extra_pct: float = 2.0

    # ---- scanner ----
    min_price: float = 1.0
    max_price: float = 50.0
    min_day_change_pct: float = 5.0
    max_day_change_pct: float = 40.0
    min_rvol: float = 8.0
    min_volume_acceleration: float = 3.0
    max_spread_pct: float = 1.0

    # ---- risk ----
    max_risk_per_trade_pct: float = 0.01
    max_position_value: float = 2000.0
    max_concurrent_positions: int = 2
    daily_max_loss_pct: float = 0.03
    max_daily_trades: int = 20

    # ---- strategy ----
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    scale_out_fraction: float = 0.5
    trailing_stop_pct: float = 0.08
    max_hold_minutes: float = 30.0
    exit_spread_pct: float = 1.5
    force_close_before_close_minutes: int = 10

    # ---- execution ----
    limit_slippage_pct: float = 0.003
    order_fill_timeout_sec: int = 20
    loop_interval_sec: int = 15

    # ---- safety guard ----
    def validate_paper_safety(self) -> None:
        """Refuse to run unless clearly pointed at paper trading.

        Live trading is unsupported by design; this is the structural guard.
        """
        if not self.paper:
            raise RuntimeError("Refusing to run: PAPER=false. This project is paper-only.")
        if _PAPER_HOST not in self.alpaca_base_url:
            raise RuntimeError(
                f"Refusing to run: ALPACA_BASE_URL must be the paper host "
                f"({_PAPER_HOST}), got {self.alpaca_base_url!r}."
            )

    def universe_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.universe.split(",") if s.strip()]

    # ---- param builders ----
    def build_scan_filters(self) -> ScanFilters:
        return ScanFilters(
            min_price=self.min_price,
            max_price=self.max_price,
            min_day_change_pct=self.min_day_change_pct,
            max_day_change_pct=self.max_day_change_pct,
            min_rvol=self.min_rvol,
            min_volume_acceleration=self.min_volume_acceleration,
            max_spread_pct=self.max_spread_pct,
        )

    def build_entry_params(self) -> EntryParams:
        return EntryParams(
            min_price=self.min_price,
            max_price=self.max_price,
            min_day_change_pct=self.min_day_change_pct,
            max_day_change_pct=self.max_day_change_pct,
            min_rvol=self.min_rvol,
            min_volume_acceleration=self.min_volume_acceleration,
            max_spread_pct=self.max_spread_pct,
        )

    def build_exit_params(self) -> ExitParams:
        return ExitParams(
            stop_loss_pct=self.stop_loss_pct,
            take_profit_pct=self.take_profit_pct,
            scale_out_fraction=self.scale_out_fraction,
            trailing_stop_pct=self.trailing_stop_pct,
            max_hold_minutes=self.max_hold_minutes,
            exit_spread_pct=self.exit_spread_pct,
        )

    def build_risk_params(self) -> RiskParams:
        return RiskParams(
            max_risk_per_trade_pct=self.max_risk_per_trade_pct,
            max_position_value=self.max_position_value,
            max_concurrent_positions=self.max_concurrent_positions,
            daily_max_loss_pct=self.daily_max_loss_pct,
            max_daily_trades=self.max_daily_trades,
            stop_loss_pct=self.stop_loss_pct,
        )

    def build_exec_params(self) -> ExecParams:
        return ExecParams(
            limit_slippage_pct=self.limit_slippage_pct,
            order_fill_timeout_sec=self.order_fill_timeout_sec,
        )

    def param_snapshot(self) -> dict[str, float | int | bool | str]:
        """All decision-affecting thresholds, for provenance hashing.

        Excludes run attributes (dry_run, source) and secrets — those live on
        the runs row, not the param_set.
        """
        return {
            "min_price": self.min_price,
            "max_price": self.max_price,
            "min_day_change_pct": self.min_day_change_pct,
            "max_day_change_pct": self.max_day_change_pct,
            "min_rvol": self.min_rvol,
            "min_volume_acceleration": self.min_volume_acceleration,
            "max_spread_pct": self.max_spread_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "scale_out_fraction": self.scale_out_fraction,
            "trailing_stop_pct": self.trailing_stop_pct,
            "max_hold_minutes": self.max_hold_minutes,
            "exit_spread_pct": self.exit_spread_pct,
            "force_close_before_close_minutes": self.force_close_before_close_minutes,
            "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
            "max_position_value": self.max_position_value,
            "max_concurrent_positions": self.max_concurrent_positions,
            "daily_max_loss_pct": self.daily_max_loss_pct,
            "max_daily_trades": self.max_daily_trades,
            "limit_slippage_pct": self.limit_slippage_pct,
            "order_fill_timeout_sec": self.order_fill_timeout_sec,
            "screener_top_n": self.screener_top_n,
            "polygon_intraday": self.polygon_intraday,
        }


def load_settings() -> Settings:
    return Settings()
