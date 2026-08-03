"""scripts/measure_paper.py 의 해설/이상징후/LLM 폴백 로직 단위 테스트.

scripts/ 는 패키지가 아니므로 importlib 로 파일 경로에서 직접 로드한다.
Alpaca 계좌·네트워크는 건드리지 않고 순수 함수(_anomaly_flags / _rule_commentary /
_llm_commentary)만 검증한다. LLM 호출은 urllib 를 몽키패치해 모킹한다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_paper.py"
_spec = importlib.util.spec_from_file_location("measure_paper", _PATH)
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)


# ---- 이상징후(항상 규칙기반, LLM 비의존) ----


def test_anomaly_flags_clean_account_has_none() -> None:
    m = {"is_margin": False, "mdd_pct": -1.3, "fill_rate": 95.0, "rt_win_pct": 84.0}
    assert _flags(m) == []


def test_anomaly_flags_margin() -> None:
    assert any("마진" in f for f in _flags({"is_margin": True}))


def test_anomaly_flags_deep_drawdown() -> None:
    assert any("최대낙폭" in f for f in _flags({"mdd_pct": -9.0}))


def test_anomaly_flags_low_fill_rate() -> None:
    assert any("체결률" in f for f in _flags({"fill_rate": 70.0}))


def test_anomaly_flags_low_win_rate() -> None:
    assert any("승률" in f for f in _flags({"rt_win_pct": 40.0}))


def _flags(m: dict) -> list[str]:
    return mp._anomaly_flags(m)


# ---- 규칙기반 해설 ----


def test_rule_commentary_is_korean_bullets() -> None:
    m = _full_metrics()
    text = mp._rule_commentary(m)
    assert text
    assert text.startswith("•")
    # 실행 검증(체결)과 회귀를 반드시 짚는다
    assert "체결" in text


def test_rule_commentary_flags_regression_when_hot_and_small() -> None:
    m = _full_metrics()
    m["rt_avg_pct"] = 2.02  # 백테스트 0.32% 대비 크게 상회
    m["small_sample"] = True
    text = mp._rule_commentary(m)
    assert "회귀" in text


def test_rule_commentary_below_backtest_branch() -> None:
    m = _full_metrics()
    m["rt_avg_pct"] = 0.05  # 백테스트 하회
    text = mp._rule_commentary(m)
    assert "밑돌" in text


# ---- LLM 해설(베스트에포트, 실패 시 None) ----


def test_llm_commentary_disabled_when_model_blank(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "")
    assert mp._llm_commentary(_full_metrics()) is None


def test_llm_commentary_returns_text_on_success(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "qwen3.6:27b")
    _patch_ollama(
        monkeypatch,
        {"message": {"content": "• 소표본이라 회귀 가능성이 커요.\n• 체결은 검증됐어요."}},
    )
    out = mp._llm_commentary(_full_metrics())
    assert out and "회귀" in out


def test_llm_commentary_strips_think_block(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "qwen3.6:27b")
    content = (
        "<think>모델의 내부 추론 과정이 여기에 들어갑니다</think>"
        "• 소표본이라 회귀 가능성이 커요. 체결률이 높아 실행은 검증되는 중이에요."
    )
    _patch_ollama(monkeypatch, {"message": {"content": content}})
    out = mp._llm_commentary(_full_metrics())
    assert out is not None
    assert "추론 과정" not in out
    assert "회귀" in out


def test_llm_commentary_none_on_error_field(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "qwen3.6:27b")
    _patch_ollama(monkeypatch, {"error": "unable to load model: blob corrupt"})
    assert mp._llm_commentary(_full_metrics()) is None


def test_llm_commentary_none_on_short_text(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "qwen3.6:27b")
    _patch_ollama(monkeypatch, {"message": {"content": "짧음"}})
    assert mp._llm_commentary(_full_metrics()) is None


def test_llm_commentary_none_on_exception(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "qwen3.6:27b")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(mp.urllib.request, "urlopen", boom)
    assert mp._llm_commentary(_full_metrics()) is None


# ---- helpers ----


def _patch_ollama(monkeypatch, response: dict) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(response).encode()

    monkeypatch.setattr(mp.urllib.request, "urlopen", lambda *a, **k: _Resp())


def _full_metrics() -> dict:
    return {
        "days": 38,
        "eq_start": 1_000_000.0,
        "eq_end": 1_066_623.0,
        "eq_ret_pct": 6.66,
        "vol_pct": 10.0,
        "sharpe": 4.45,
        "mdd_pct": -1.3,
        "small_sample": True,
        "fills": 187,
        "total_orders": 196,
        "fill_rate": 95.0,
        "rt_count": 83,
        "rt_avg_pct": 2.02,
        "rt_med_pct": 1.1,
        "rt_win_pct": 84.0,
        "pos_count": 21,
        "equity": 1_066_623.0,
        "cash": -28_374.0,
        "long_mv": 1_090_000.0,
        "upl": 5_000.0,
        "is_margin": True,
    }
