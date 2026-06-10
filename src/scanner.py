"""Universe filtering + ranking. Pure functions over MarketSnapshot."""

from __future__ import annotations

from dataclasses import dataclass

from src.models import MarketSnapshot


@dataclass(frozen=True)
class ScanFilters:
    min_price: float
    max_price: float
    min_day_change_pct: float
    max_day_change_pct: float
    min_rvol: float
    min_volume_acceleration: float
    max_spread_pct: float


def passes_filters(s: MarketSnapshot, f: ScanFilters) -> bool:
    """True if the snapshot is inside the runnable window.

    Float / market cap are intentionally not consulted (nullable in v0).
    """
    return (
        f.min_price <= s.last_price <= f.max_price
        and f.min_day_change_pct <= s.day_change_pct <= f.max_day_change_pct
        and s.rvol >= f.min_rvol
        and s.volume_acceleration >= f.min_volume_acceleration
        and s.spread_pct <= f.max_spread_pct
    )


def score_candidate(s: MarketSnapshot) -> float:
    """Rank score — higher is a stronger runner.

    Weighted blend of the three momentum signals. Monotonic in each input so
    ranking is stable and explainable; not a probability.
    """
    return 0.45 * s.rvol + 0.40 * s.volume_acceleration + 0.15 * s.day_change_pct


def scan_candidates(snapshots: list[MarketSnapshot], f: ScanFilters) -> list[MarketSnapshot]:
    """Filter to the runnable window, strongest first."""
    passing = [s for s in snapshots if passes_filters(s, f)]
    return sorted(passing, key=score_candidate, reverse=True)
