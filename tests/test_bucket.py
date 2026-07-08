"""bucket_by_feature: does an entry-feature separate winners from losers?"""

from __future__ import annotations

from src.intraday import bucket_by_feature


def test_predictive_feature_shows_low_high_spread():
    # feature "vol" correlates with return: higher vol -> higher return
    samples = [({"vol": v}, float(v)) for v in [1, 2, 3, 4, 5, 6]]
    buckets = bucket_by_feature(samples, "vol", n_buckets=3)
    assert [b["bucket"] for b in buckets] == ["low", "mid", "high"]
    assert buckets[0]["avg"] < buckets[-1]["avg"]  # low bucket worse than high
    assert buckets[0]["avg"] == 1.5 and buckets[-1]["avg"] == 5.5


def test_win_rate_per_bucket():
    samples = [({"f": 1}, -5.0), ({"f": 2}, -5.0), ({"f": 3}, 10.0), ({"f": 4}, 10.0)]
    b = bucket_by_feature(samples, "f", n_buckets=2)
    assert b[0]["win_rate"] == 0.0  # both negative
    assert b[1]["win_rate"] == 100.0  # both positive


def test_too_few_samples_returns_empty():
    assert bucket_by_feature([({"f": 1}, 1.0)], "f", n_buckets=3) == []


def test_missing_feature_key_ignored():
    samples = [({"a": 1}, 1.0), ({"b": 2}, 2.0)]
    assert bucket_by_feature(samples, "a", n_buckets=3) == []  # only 1 has 'a'
