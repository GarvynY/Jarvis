"""
macro_agent — stateless macro signals research agent.

Fetches macro indicators relevant to CNY/AUD:
  - AUD/USD rate (yfinance) as USD proxy
  - Iron ore spot proxy (yfinance SCOA.L or fallback)
  - Gold price (yfinance GC=F)
  - US 10Y yield (yfinance ^TNX)

Uses only free data sources — no API keys required.
Accepts ResearchTask, returns AgentOutput. No side effects.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..schema import (
    AgentOutput,
    Finding,
    ResearchTask,
    SourceRef,
)

AGENT_NAME = "macro_agent"

# (yfinance_ticker, label, direction_hint)
_MACRO_TICKERS: list[tuple[str, str, str]] = [
    ("AUDUSD=X",  "AUD/USD",       "fx_proxy"),
    ("GC=F",      "黄金期货",       "risk_off"),
    ("^TNX",      "美国10年期国债收益率", "rate_signal"),
    ("CL=F",      "原油期货",       "commodity"),
]


def run(task: ResearchTask) -> AgentOutput:
    """Fetch macro indicator snapshots and return structured findings."""
    t0 = time.monotonic()
    try:
        import yfinance as yf  # optional dependency — graceful fallback below
    except ImportError:
        return AgentOutput.make_error(
            AGENT_NAME,
            error="yfinance not installed — macro data unavailable",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    findings: list[Finding] = []
    sources: list[SourceRef] = []
    missing: list[str] = []
    risks: list[str] = []

    for ticker, label, hint in _MACRO_TICKERS:
        result = _fetch_ticker(yf, ticker, label, hint, retrieved_at)
        if result is None:
            missing.append(label)
            continue
        finding, source = result
        findings.append(finding)
        sources.append(source)

    # ── Macro risk synthesis ──────────────────────────────────────────────────
    gold_finding = next((f for f in findings if "黄金" in f.summary), None)
    audusd_finding = next((f for f in findings if "AUD/USD" in f.summary), None)
    if gold_finding and gold_finding.direction == "risk_off_rising":
        risks.append("黄金上涨信号：市场风险偏好下降，可能压制 AUD")
    if audusd_finding and audusd_finding.direction == "bearish_aud":
        risks.append("AUD/USD 走弱，CNY/AUD 汇率可能承压")

    status = "ok" if len(findings) >= 2 else ("partial" if findings else "error")
    confidence = min(0.8, 0.2 * len(findings))

    return AgentOutput(
        agent_name=AGENT_NAME,
        status=status,
        summary=f"获取到 {len(findings)}/{len(_MACRO_TICKERS)} 个宏观指标",
        findings=findings,
        sources=sources,
        as_of=retrieved_at,
        confidence=confidence,
        risks=risks,
        missing_data=missing,
        latency_ms=int((time.monotonic() - t0) * 1000),
    )


def _fetch_ticker(
    yf: Any, ticker: str, label: str, hint: str, retrieved_at: str
) -> tuple[Finding, SourceRef] | None:
    try:
        info = yf.Ticker(ticker).info
        price = None
        for key in ("regularMarketPrice", "currentPrice", "ask", "previousClose"):
            val = info.get(key)
            if val and float(val) > 0:
                price = float(val)
                break
        if price is None:
            return None

        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
        change_pct: float | None = None
        if prev_close and float(prev_close) > 0:
            change_pct = round((price / float(prev_close) - 1) * 100, 3)

        direction = _tag_direction(ticker, hint, change_pct)
        change_str = f"（{change_pct:+.2f}%）" if change_pct is not None else ""
        summary = f"{label}: {price:.4f}{change_str}"

        finding = Finding(key=f"macro_{ticker.lower().replace('=', '_').replace('^', '')}",
                          summary=summary,
                          direction=direction)
        source = SourceRef(
            title=f"{label} ({ticker})",
            url=f"https://finance.yahoo.com/quote/{ticker.replace('^', '%5E')}/",
            source="yfinance",
            retrieved_at=retrieved_at,
        )
        return finding, source

    except Exception:
        return None


def _tag_direction(ticker: str, hint: str, change_pct: float | None) -> str | None:
    if change_pct is None:
        return "neutral"
    if hint == "fx_proxy":         # AUDUSD=X
        if change_pct > 0.3:
            return "bullish_aud"
        if change_pct < -0.3:
            return "bearish_aud"
        return "neutral"
    if hint == "risk_off":         # gold
        if change_pct > 0.5:
            return "risk_off_rising"
        return "neutral"
    if hint == "rate_signal":      # US 10Y
        if change_pct > 1.0:
            return "bearish_aud"   # higher US rates → USD strength → AUD pressure
        return "neutral"
    return "neutral"
