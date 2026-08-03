"""Alpaca 페이퍼 계좌를 측정해 한국어 주간 보고서를 출력한다.

프로세스 env에서 ALPACA_API_KEY / ALPACA_SECRET_KEY 를 읽는다(래퍼가 .env 로드).
자산곡선(portfolio history), 체결·체결률, 완료 왕복거래(FIFO), 보유 포지션/현금을
집계하고 RSI-2 백테스트(거래당 +0.32%, 승률 63%, Sharpe ~0.72)와 비교한다.

해설(解說)은 두 단계로 만든다:
  1) 규칙기반 한국어 해설 — 항상 생성되는 신뢰 가능한 기본. 이상징후 플래그 포함.
  2) 로컬 LLM 해설 — LLM_MODEL(기본 qwen3-coder:30b)이 응답하면 그 자연어 해설로 교체.
     실패/빈응답/타임아웃이면 조용히 규칙기반으로 폴백한다(보고서는 절대 실패하지 않음).

표준 라이브러리 + alpaca-py 만 사용. 출력은 stdout 의 보고서 텍스트.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import urllib.request
from collections import defaultdict, deque
from datetime import UTC, datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

# 백테스트 기준선(2년, 비용 10bps/leg 차감 후) — 회귀 비교의 앵커
BT_TRADE_PCT = 0.32
BT_WIN_PCT = 63.0
BT_SHARPE = 0.72
BACKTEST_KO = (
    f"백테스트 기준: 거래당 +{BT_TRADE_PCT}% · 승률 {BT_WIN_PCT:.0f}%"
    f" · Sharpe ~{BT_SHARPE} (비용 10bps/leg, 2년)"
)


def _client() -> TradingClient:
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY 미설정")
    return TradingClient(key, sec, paper=True)


def _collect(c: TradingClient) -> dict:
    """계좌에서 원시 지표를 뽑아 하나의 dict 로 반환."""
    m: dict = {}

    # 1) 자산곡선
    ph = c.get_portfolio_history(GetPortfolioHistoryRequest(period="3M", timeframe="1D"))
    eq = [e for e in (ph.equity or []) if e]
    m["days"] = len(eq)
    if len(eq) >= 2:
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1]]
        std = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        peak = eq[0]
        mdd = 0.0
        for x in eq:
            peak = max(peak, x)
            mdd = min(mdd, x / peak - 1)
        m["eq_start"] = eq[0]
        m["eq_end"] = eq[-1]
        m["eq_ret_pct"] = (eq[-1] / eq[0] - 1) * 100
        m["vol_pct"] = std * (252**0.5) * 100
        m["sharpe"] = (statistics.mean(rets) / std) * (252**0.5) if std else 0.0
        m["mdd_pct"] = mdd * 100
    m["small_sample"] = 0 < len(eq) < 60

    # 2) 체결 + 왕복거래(FIFO)
    all_orders = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
    orders = [o for o in all_orders if str(o.status.value) == "filled" and o.filled_avg_price]
    lots: dict[str, deque] = defaultdict(deque)
    realized: list[float] = []
    for o in sorted(orders, key=lambda x: x.submitted_at):
        qty = float(o.filled_qty)
        px = float(o.filled_avg_price)
        if o.side.value == "buy":
            lots[o.symbol].append([qty, px])
        else:
            remain = qty
            while remain > 1e-9 and lots[o.symbol]:
                lot = lots[o.symbol][0]
                take = min(remain, lot[0])
                realized.append((px - lot[1]) / lot[1])
                lot[0] -= take
                remain -= take
                if lot[0] <= 1e-9:
                    lots[o.symbol].popleft()
    m["fills"] = len(orders)
    m["total_orders"] = len(all_orders)
    m["fill_rate"] = len(orders) / max(1, len(all_orders)) * 100
    if realized:
        m["rt_count"] = len(realized)
        m["rt_avg_pct"] = statistics.mean(realized) * 100
        m["rt_med_pct"] = statistics.median(realized) * 100
        m["rt_win_pct"] = sum(1 for r in realized if r > 0) / len(realized) * 100

    # 3) 포지션 / 현금(마진 여부)
    a = c.get_account()
    pos = c.get_all_positions()
    m["pos_count"] = len(pos)
    m["equity"] = float(a.equity)
    m["cash"] = float(a.cash)
    m["long_mv"] = float(a.long_market_value)
    m["upl"] = sum(float(p.unrealized_pl) for p in pos)
    m["is_margin"] = float(a.cash) < 0
    return m


def _anomaly_flags(m: dict) -> list[str]:
    """규칙기반 이상징후 — 항상 신뢰 가능(LLM 비의존). 폰으로 바로 보이는 경고."""
    flags: list[str] = []
    if m.get("is_margin"):
        flags.append("⚠️ 현금 마이너스 = 마진 사용 중 (포지션 청산되며 자연 정상화, 레버리지 주시)")
    if m.get("mdd_pct", 0) <= -8:
        flags.append(f"🚨 최대낙폭 {m['mdd_pct']:+.1f}% — 백테스트(~-13%) 범위지만 급격하면 점검")
    if m.get("fill_rate", 100) < 85:
        flags.append(f"🚨 체결률 {m['fill_rate']:.0f}% 저조 — 지정가 미체결/유동성 확인 필요")
    if "rt_win_pct" in m and m["rt_win_pct"] < 45:
        flags.append(
            f"🚨 승률 {m['rt_win_pct']:.0f}% — 백테스트 63% 크게 하회, 엣지 훼손 여부 점검"
        )
    return flags


def _rule_commentary(m: dict) -> str:
    """규칙기반 한국어 애널리스트 해설 — 신뢰 가능한 기본. 내 분석 로직을 인코딩."""
    lines: list[str] = []

    # 총평 + 회귀 관점
    if "rt_avg_pct" in m:
        avg = m["rt_avg_pct"]
        if avg > BT_TRADE_PCT * 2:
            lines.append(
                f"이번 구간 거래당 +{avg:.2f}%는 백테스트 +{BT_TRADE_PCT}%를 크게 웃돌아요. "
                "좋은 성적이지만 소표본의 운 좋은 구간일 가능성이 높고, 시간이 지나면 "
                "기준선 쪽으로 회귀하는 게 정상이에요. 지금 수치를 실력으로 과신하지 마세요."
            )
        elif avg >= BT_TRADE_PCT * 0.5:
            lines.append(
                f"거래당 +{avg:.2f}%로 백테스트 +{BT_TRADE_PCT}% 근방이에요. "
                "표본이 쌓이며 기준선과 수렴하는 건강한 흐름이에요."
            )
        else:
            lines.append(
                f"거래당 +{avg:.2f}%로 백테스트 +{BT_TRADE_PCT}%를 밑돌아요. "
                "아직 표본이 작아 단정할 수 없지만, 비용 차감 후 "
                "엣지가 얇아지는지 계속 지켜봐야 해요."
            )

    # 실행 검증 — 이게 A 전략의 make-or-break(비용/체결)
    if "fill_rate" in m:
        if m["fill_rate"] >= 90:
            lines.append(
                f"체결률 {m['fill_rate']:.0f}%로 실제 슬리피지·체결이 검증됐어요. "
                "A(RSI-2)의 최대 리스크였던 '비용에 엣지가 먹히는가'를 실거래로 확인 중인 셈이라 "
                "이 지표가 가장 중요해요."
            )
        else:
            lines.append(
                f"체결률 {m['fill_rate']:.0f}%예요. 지정가 미체결이 늘면 실현 수익이 "
                "백테스트보다 얇아지니 원인(유동성/타이밍)을 봐야 해요."
            )

    # 리스크
    if "sharpe" in m and "mdd_pct" in m:
        lines.append(
            f"위험 지표는 Sharpe {m['sharpe']:+.2f} · 최대낙폭 {m['mdd_pct']:+.1f}% · "
            f"연율변동성 {m.get('vol_pct', 0):.1f}%예요. "
            + (
                "소표본이라 Sharpe는 과대평가되기 쉬워요(며칠 손실이면 급락)."
                if m.get("small_sample")
                else "표본이 어느 정도 쌓인 수치예요."
            )
        )

    # 마진
    if m.get("is_margin"):
        lines.append(
            f"현재 현금 ${m['cash']:,.0f}로 마진을 쓰고 있어요. "
            "포지션이 청산되면 자연 해소되지만, 사이징이 현금을 넘지 않는지는 계속 확인이 필요해요."
        )

    # 회귀 경고(소표본)
    if m.get("small_sample"):
        lines.append(
            f"※ {m['days']}거래일 소표본이에요. 결론이 아니라 '실행 파이프라인이 도는지'를 "
            "보는 단계이고, 판단은 표본이 60거래일 이상 쌓인 뒤에 하세요."
        )

    return "\n".join(f"• {ln}" for ln in lines)


def _llm_commentary(m: dict) -> str | None:
    """로컬 Ollama(LLM_MODEL)로 자연어 해설 생성. 실패 시 None(→규칙기반 폴백)."""
    model = os.environ.get("LLM_MODEL", "qwen3-coder:30b").strip()
    if not model:
        return None
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    timeout = float(os.environ.get("LLM_TIMEOUT_S", "180"))

    facts = (
        f"[백테스트 기준] 거래당 +{BT_TRADE_PCT}%, 승률 {BT_WIN_PCT:.0f}%, "
        f"Sharpe ~{BT_SHARPE} (비용후 2년)\n"
        f"[이번 구간] {m.get('days', 0)}거래일"
    )
    if "eq_ret_pct" in m:
        facts += (
            f", 총수익 {m['eq_ret_pct']:+.2f}%, 연율변동성 {m.get('vol_pct', 0):.1f}%, "
            f"Sharpe {m['sharpe']:+.2f}, 최대낙폭 {m['mdd_pct']:+.1f}%"
        )
    if "fill_rate" in m:
        facts += f"\n체결률 {m['fill_rate']:.0f}% ({m['fills']}/{m['total_orders']})"
    if "rt_avg_pct" in m:
        facts += (
            f", 완료 왕복 {m['rt_count']}건 평균 {m['rt_avg_pct']:+.2f}% "
            f"중앙 {m['rt_med_pct']:+.2f}% 승률 {m['rt_win_pct']:.0f}%"
        )
    facts += f"\n보유 {m.get('pos_count', 0)}포지션, 현금 ${m.get('cash', 0):,.0f}" + (
        " (마진 사용)" if m.get("is_margin") else ""
    )

    system = (
        "너는 극도로 솔직하고 냉정한 퀀트 트레이딩 애널리스트야. "
        "주어진 수치만 근거로 한국어 해설을 써. 자화자찬·과대해석은 절대 금지야. "
        "핵심 원칙: 소표본(60거래일 미만)에서 나온 높은 수익률·승률·Sharpe는 "
        "실력이 아니라 운 좋은 구간일 가능성이 높아. "
        "그러니 백테스트 수준으로의 회귀가 기본 시나리오임을 반드시 명시해. "
        "'향상/개선/우수/좋다' 같은 긍정 단어로 성과를 치켜세우지 마. "
        "체결률은 실전 비용·슬리피지 검증 관점에서 해석해(A(RSI-2)의 make-or-break). "
        "마진은 리스크로 짚어. 새로운 수치를 지어내지 마. "
        "불릿 3~4개, 각 1~2문장으로 간결하게. /no_think"
    )
    payload = json.dumps(
        {
            "model": model,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 500},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": facts + "\n\n해설:"},
            ],
        }
    ).encode()

    try:
        req = urllib.request.Request(
            f"{host}/api/chat", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("error"):
            return None
        text = (data.get("message") or {}).get("content", "")
        # qwen 계열의 <think> 추론 블록 제거
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return text if len(text) >= 30 else None
    except Exception:
        return None


def main() -> None:
    c = _client()
    m = _collect(c)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    out: list[str] = [f"📊 *BStalk3r 페이퍼 주간 보고* — {now}"]

    # 1) 자산곡선
    if "eq_ret_pct" in m:
        out.append(
            f"\n*자산* {m['days']}일: ${m['eq_start']:,.0f} → ${m['eq_end']:,.0f} "
            f"({m['eq_ret_pct']:+.2f}%)\n"
            f"변동성 {m['vol_pct']:.1f}% · Sharpe {m['sharpe']:+.2f} · "
            f"최대낙폭 {m['mdd_pct']:+.1f}%"
        )

    # 2) 체결 + 왕복
    out.append(f"\n*체결* {m['fills']}/{m['total_orders']} ({m['fill_rate']:.0f}%)")
    if "rt_avg_pct" in m:
        out.append(
            f"*왕복거래* {m['rt_count']}건: 평균 {m['rt_avg_pct']:+.2f}% · "
            f"중앙 {m['rt_med_pct']:+.2f}% · 승률 {m['rt_win_pct']:.0f}%"
        )
    out.append(f"_{BACKTEST_KO}_")

    # 3) 포지션 / 현금
    out.append(
        f"\n*포지션* {m['pos_count']}개 · 자산 ${m['equity']:,.0f} · "
        f"현금 ${m['cash']:,.0f} · 롱 ${m['long_mv']:,.0f} · 평가손익 ${m['upl']:+,.0f}"
    )

    # 4) 이상징후(항상 규칙기반)
    flags = _anomaly_flags(m)
    if flags:
        out.append("\n*이상징후*")
        out.extend(flags)

    # 5) 해설 — LLM 우선, 실패 시 규칙기반 폴백
    llm = _llm_commentary(m)
    if llm:
        out.append(f"\n🧠 *해설* _(로컬 LLM: {os.environ.get('LLM_MODEL', 'qwen3-coder:30b')})_")
        out.append(llm)
    else:
        out.append("\n🧠 *해설* _(규칙기반)_")
        out.append(_rule_commentary(m))

    print("\n".join(out))


if __name__ == "__main__":
    main()
