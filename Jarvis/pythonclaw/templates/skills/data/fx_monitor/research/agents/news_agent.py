"""
news_agent — stateless news research agent.

Wraps news_monitor.py to produce structured AgentOutput.
Uses mark_seen=False so the research workflow never mutates daemon state.
Accepts ResearchTask, returns AgentOutput. No side effects.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SKILL_DIR = Path(__file__).parent.parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from news_monitor import _fetch_google_news_rss  # noqa: E402

from ..schema import (
    AgentOutput,
    Finding,
    ResearchTask,
    SourceRef,
)

AGENT_NAME = "news_agent"

# Keywords relevant to CNY/AUD — preset-aligned, not hard-coded in schema
_FX_CNYAUD_KEYWORDS = [
    "RBA interest rate decision",
    "Australia dollar AUD",
    "China Australia trade",
    "US Iran ceasefire",          # geopolitical risk proxy
    "China economy GDP",
    "iron ore price Australia",
]

_MAX_PER_KEYWORD = 3


def run(task: ResearchTask) -> AgentOutput:
    """Fetch relevant news headlines and return structured findings."""
    t0 = time.monotonic()
    try:
        return _fetch_and_build(task, latency_ms_ref=t0)
    except Exception as exc:
        return AgentOutput.make_error(
            AGENT_NAME,
            error=f"news fetch failed: {exc}",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


def _fetch_and_build(task: ResearchTask, latency_ms_ref: float) -> AgentOutput:
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    findings: list[Finding] = []
    sources: list[SourceRef] = []
    risks: list[str] = []
    missing: list[str] = []

    # Collect articles across all keywords (no mark_seen — read-only)
    all_articles: list[dict] = []
    failed_keywords: list[str] = []
    for kw in _FX_CNYAUD_KEYWORDS:
        results = _fetch_google_news_rss(kw, max_items=_MAX_PER_KEYWORD)
        errors = [r for r in results if "error" in r]
        valid = [r for r in results if "error" not in r]
        if errors:
            failed_keywords.append(kw)
        for art in valid:
            art["keyword"] = kw
            all_articles.append(art)

    if failed_keywords:
        missing.append(f"RSS失败关键词: {', '.join(failed_keywords[:3])}")

    if not all_articles:
        return AgentOutput(
            agent_name=AGENT_NAME,
            status="error",
            summary="所有新闻关键词抓取均失败",
            missing_data=missing,
            latency_ms=int((time.monotonic() - latency_ms_ref) * 1000),
        )

    # ── Deduplicate by URL ────────────────────────────────────────────────────
    seen_urls: set[str] = set()
    unique_articles: list[dict] = []
    for art in all_articles:
        url = art.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_articles.append(art)

    # ── Build one Finding per article (up to 8) ───────────────────────────────
    for art in unique_articles[:8]:
        title = art.get("title", "").strip()
        url = art.get("url", "")
        published = art.get("published", "")
        keyword = art.get("keyword", "")

        if not title:
            continue

        direction = _infer_direction(title, keyword)
        findings.append(Finding(
            key=f"news_{len(findings)}",
            summary=title,
            direction=direction,
        ))
        sources.append(SourceRef(
            title=title,
            url=url,
            source="google_news_rss",
            retrieved_at=retrieved_at,
            published_at=published or None,
        ))

    # ── Risk: conflicting signals ─────────────────────────────────────────────
    bullish = sum(1 for f in findings if f.direction == "bullish_aud")
    bearish = sum(1 for f in findings if f.direction == "bearish_aud")
    if bullish > 0 and bearish > 0:
        risks.append(f"新闻信号存在矛盾：{bullish} 条利好 AUD，{bearish} 条利空 AUD")

    status = "ok" if findings else ("partial" if missing else "error")
    confidence = min(0.7, 0.1 * len(findings))

    return AgentOutput(
        agent_name=AGENT_NAME,
        status=status,
        summary=f"采集到 {len(findings)} 条相关新闻（{len(_FX_CNYAUD_KEYWORDS)} 个关键词）",
        findings=findings,
        sources=sources,
        as_of=retrieved_at,
        confidence=confidence,
        risks=risks,
        missing_data=missing,
        latency_ms=int((time.monotonic() - latency_ms_ref) * 1000),
    )


def _infer_direction(title: str, keyword: str) -> str | None:
    """Heuristic direction tag — not a prediction, just a signal label."""
    title_lower = title.lower()
    # AUD-bullish signals
    if any(w in title_lower for w in ["rate hike", "hawkish", "inflation rises",
                                       "strong jobs", "iron ore rally", "china demand"]):
        return "bullish_aud"
    # AUD-bearish signals
    if any(w in title_lower for w in ["rate cut", "dovish", "recession", "slowdown",
                                       "trade war", "tariff", "conflict", "crisis",
                                       "rba cuts", "rba lowers"]):
        return "bearish_aud"
    return "neutral"
