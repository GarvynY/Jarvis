"""
fx_agent — stateless FX rate research agent.

Wraps fetch_rate.py to produce structured AgentOutput.
Accepts ResearchTask, returns AgentOutput. No side effects.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow importing fetch_rate from the parent skill directory
_SKILL_DIR = Path(__file__).parent.parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from fetch_rate import fetch_rate  # noqa: E402

from ..schema import (
    AgentOutput,
    Finding,
    ResearchTask,
    SourceRef,
)

AGENT_NAME = "fx_agent"


def run(task: ResearchTask) -> AgentOutput:
    """Fetch live CNY/AUD rate data and return structured findings."""
    t0 = time.monotonic()
    try:
        data = fetch_rate(period="90d")
        return _build_output(data, task, latency_ms=int((time.monotonic() - t0) * 1000))
    except Exception as exc:
        return AgentOutput.make_error(
            AGENT_NAME,
            error=f"fetch_rate failed: {exc}",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


# ── Output builder ────────────────────────────────────────────────────────────

def _build_output(data: dict, task: ResearchTask, latency_ms: int) -> AgentOutput:
    findings: list[Finding] = []
    sources: list[SourceRef] = []
    risks: list[str] = []
    missing: list[str] = []
    retrieved_at = data.get("fetched_at_utc", "")

    # ── Current rate finding ──────────────────────────────────────────────────
    if "error" in data:
        missing.append("current_rate")
    else:
        current = data.get("current_1_AUD_in_CNY")
        basis = data.get("current_rate_basis", "unknown")
        market = data.get("market_1_AUD_in_CNY")

        if current:
            summary = f"1 AUD = {current:.4f} CNY（{basis}）"
            if market and basis != "market_mid_fallback":
                spread_vs_market = round(current - market, 4)
                summary += f"，市场中间价 {market:.4f}，银行加价 +{spread_vs_market:.4f}"
            findings.append(Finding(
                key="current_rate",
                summary=summary,
                direction=None,
            ))

        # ── Bank quote source ─────────────────────────────────────────────────
        rt_source = data.get("market_rate_source", "")
        bank_source = data.get("bank_rate_source", "")
        if rt_source and rt_source != "unavailable":
            sources.append(SourceRef(
                title="CNY/AUD 市场实时汇率",
                url="https://open.er-api.com/v6/latest/CNY",
                source=rt_source,
                retrieved_at=retrieved_at,
            ))
        if bank_source and "unavailable" not in bank_source.lower():
            sources.append(SourceRef(
                title="中国各银行 AUD 牌价",
                url="https://www.usdrate.top/",
                source=bank_source,
                retrieved_at=retrieved_at,
            ))

        # ── Bank spread finding ───────────────────────────────────────────────
        bank_summary = (data.get("bank_exchange_rates") or {}).get("summary")
        if bank_summary:
            spread = bank_summary.get("median_bank_spread_pct")
            if spread is not None:
                findings.append(Finding(
                    key="bank_spread",
                    summary=f"银行买卖价差中位数 {spread:.3f}%（现汇卖出中位 {bank_summary['median_spot_sell_rate']:.4f}）",
                    direction=None,
                ))
        else:
            missing.append("bank_quotes")

    # ── Historical trend finding ──────────────────────────────────────────────
    stats = data.get("stats")
    if stats:
        period_chg = stats.get("period_change_pct")
        direction = None
        if period_chg is not None:
            if period_chg > 0.5:
                direction = "bullish_aud"
            elif period_chg < -0.5:
                direction = "bearish_aud"
            else:
                direction = "neutral"

        summary_parts = [
            f"{stats['period']} 区间变动 {period_chg:+.2f}%",
            f"高 {stats['high_cny_aud']} / 低 {stats['low_cny_aud']} CNY/AUD（yfinance）",
        ]
        trend_dir = stats.get("trend_direction")
        if trend_dir:
            summary_parts.append(f"近7日: {trend_dir}（{stats.get('trend_7d_pct', 0):+.2f}%）")

        findings.append(Finding(
            key="historical_trend",
            summary="；".join(summary_parts),
            direction=direction,
        ))
        sources.append(SourceRef(
            title="CNY/AUD 历史数据",
            url="https://finance.yahoo.com/quote/CNYAUD=X/",
            source="yfinance CNYAUD=X",
            retrieved_at=retrieved_at,
        ))
    else:
        missing.append("historical_data")

    # ── Volatility risk ───────────────────────────────────────────────────────
    if stats and stats.get("volatility_std", 0) > 0.003:
        risks.append(f"波动率较高（σ={stats['volatility_std']:.4f}），近期汇率波动明显")

    confidence = 0.9 if "error" not in data and stats else (0.5 if "error" not in data else 0.1)

    return AgentOutput(
        agent_name=AGENT_NAME,
        status="ok" if findings else "error",
        summary=findings[0].summary if findings else "无法获取汇率数据",
        findings=findings,
        sources=sources,
        as_of=retrieved_at,
        confidence=confidence,
        risks=risks,
        missing_data=missing,
        latency_ms=latency_ms,
    )
