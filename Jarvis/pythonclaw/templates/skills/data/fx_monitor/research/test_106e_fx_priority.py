#!/usr/bin/env python3
"""
Phase 10.6E — FX target_rate_gap priority tests.

Tests:
  1.  test_target_rate_gap_selected_with_user_target
  2.  test_current_rate_always_selected
  3.  test_at_least_one_historical_context
  4.  test_market_drivers_not_squeezed_out

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106e_fx_priority.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput,
    EvidenceChunk,
    Finding,
    ResearchPreset,
    ResearchTask,
    SafeUserContext,
    SourceRef,
    now_iso,
)
from evidence_store import EvidenceStore  # noqa: E402


def _build_test_store() -> tuple[EvidenceStore, ResearchTask, ResearchPreset]:
    """Build an in-memory store with realistic FX + MarketDrivers + Risk chunks."""
    store = EvidenceStore(":memory:")

    preset = ResearchPreset(
        name="fx_cnyaud",
        research_type="fx",
        description="CNY/AUD",
        report_sections=["汇率事实", "新闻驱动", "宏观信号", "风险与矛盾"],
        default_agents=["fx_agent", "news_agent", "macro_agent", "market_drivers_agent"],
        banned_terms=[],
        default_time_horizon="short_term",
    )

    task = ResearchTask(
        preset_name="fx_cnyaud",
        research_topic="CNY/AUD 外汇研究",
        focus_pair="CNY/AUD",
        focus_assets=["CNY", "AUD"],
        safe_user_context=SafeUserContext(
            purpose="living",
            target_rate=4.95,
            preferred_topics=["澳元", "汇率"],
        ),
    )

    # FX Agent findings
    fx_findings = [
        Finding(
            key="current_rate",
            summary="1 AUD = 4.8887 CNY",
            category="fx_price",
            importance=0.75,
            subcategory="current_rate",
            source_ids=["https://open.er-api.com/v6/latest/CNY"],
            evidence_basis="fetch_rate.current_1_AUD_in_CNY",
        ),
        Finding(
            key="historical_trend",
            summary="90d: AUD/CNY -0.44%",
            category="fx_price",
            importance=0.65,
            subcategory="historical_trend",
            source_ids=["https://finance.yahoo.com/quote/CNYAUD=X/"],
            evidence_basis="fetch_rate.stats",
        ),
        Finding(
            key="recent_range",
            summary="5d range: 4.8709-4.9285",
            category="fx_price",
            importance=0.45,
            subcategory="recent_range",
            source_ids=["https://finance.yahoo.com/quote/CNYAUD=X/"],
            evidence_basis="fetch_rate.recent_history",
        ),
        Finding(
            key="target_rate_gap",
            summary="距目标4.95: 当前4.8887, 差距-1.25%",
            category="fx_price",
            importance=0.50,
            subcategory="target_rate_gap",
            source_ids=["https://open.er-api.com/v6/latest/CNY"],
            evidence_basis="target_rate_gap_calculation",
        ),
        Finding(
            key="bank_spread",
            summary="银行加价 +0.0105",
            category="fx_price",
            importance=0.35,
            subcategory="bank_spread",
            source_ids=["https://www.usdrate.top/"],
            evidence_basis="fetch_rate.bank_spread",
        ),
    ]

    fx_output = AgentOutput(
        agent_name="fx_agent",
        status="ok",
        summary="FX data OK",
        findings=fx_findings,
        sources=[
            SourceRef(url="https://open.er-api.com/v6/latest/CNY", title="CNY/AUD 市场实时汇率", source="open.er-api.com", retrieved_at=now_iso()),
            SourceRef(url="https://finance.yahoo.com/quote/CNYAUD=X/", title="CNY/AUD 历史数据", source="yfinance CNYAUD=X", retrieved_at=now_iso()),
            SourceRef(url="https://www.usdrate.top/", title="中国银行 AUD 牌价", source="Chinese bank FX boards", retrieved_at=now_iso()),
        ],
        confidence=0.85,
    )

    # Market Drivers findings (2 key ones)
    md_findings = [
        Finding(
            key="commodity_copper",
            summary="铜价 下跌 4.81%",
            category="commodity_trade",
            importance=0.7,
            subcategory="market_data",
            source_ids=["https://finance.yahoo.com/quote/HG=F/"],
            evidence_basis="yfinance:commodity.copper",
            direction="bearish_aud",
            direction_for_aud="bearish",
        ),
        Finding(
            key="fx_aud_usd",
            summary="AUD/USD 下跌 0.85%",
            category="market_driver",
            importance=0.6,
            subcategory="market_data",
            source_ids=["https://finance.yahoo.com/quote/AUDUSD=X/"],
            evidence_basis="yfinance:fx.aud_usd",
            direction="bearish_aud",
            direction_for_aud="bearish",
        ),
    ]

    md_output = AgentOutput(
        agent_name="market_drivers_agent",
        status="ok",
        summary="Market data: 7/8 OK",
        findings=md_findings,
        sources=[
            SourceRef(url="https://finance.yahoo.com/quote/HG=F/", title="commodity.copper via yfinance", source="yfinance", retrieved_at=now_iso()),
            SourceRef(url="https://finance.yahoo.com/quote/AUDUSD=X/", title="fx.aud_usd via yfinance", source="yfinance", retrieved_at=now_iso()),
        ],
        confidence=0.74,
    )

    # Risk findings
    risk_findings = [
        Finding(
            key="signal_contradiction",
            summary="多空信号矛盾",
            category="risk",
            importance=0.7,
            subcategory="contradiction",
            evidence_basis="source_agents=market_drivers_agent",
        ),
        Finding(
            key="dominant_signal",
            summary="多数指标偏向 AUD 走弱",
            category="risk",
            importance=0.6,
            subcategory="dominant_signal",
            evidence_basis="source_agents=market_drivers_agent",
        ),
    ]

    risk_output = AgentOutput(
        agent_name="risk_agent",
        status="ok",
        summary="Risk OK",
        findings=risk_findings,
        sources=[],
        confidence=0.75,
    )

    # Macro finding (1 for brevity)
    macro_output = AgentOutput(
        agent_name="macro_agent",
        status="ok",
        summary="Macro partial",
        findings=[Finding(
            key="rba_hold",
            summary="RBA 维持利率不变",
            category="policy_signal",
            importance=0.7,
            subcategory="central_bank",
            evidence_basis="macro search",
        )],
        sources=[],
        confidence=0.6,
    )

    store.ingest_outputs(task, [fx_output, md_output, risk_output, macro_output])
    return store, task, preset


def test_target_rate_gap_selected_with_user_target() -> None:
    """target_rate_gap should be selected when user has target_rate and purpose=living."""
    store, task, preset = _build_test_store()
    pack = store.build_context_pack(
        task, preset, [],
        max_chunks_per_section=5,
        token_budget=7500,
        section_token_reserves={"fx_price": 2200, "news_event": 800, "macro": 2000, "risk": 1000},
        safe_user_context=task.safe_user_context,
    )
    fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
    fx_texts = " ".join(it.text for it in fx_items)
    assert "target_rate_gap" in fx_texts or "目标" in fx_texts or "差距" in fx_texts, (
        f"target_rate_gap not found in FX items. Got {len(fx_items)} FX chunks: "
        f"{[it.text[:40] for it in fx_items]}"
    )
    print("  target_rate_gap selected with target   OK")


def test_current_rate_always_selected() -> None:
    """current_rate should always be selected (highest importance in FX)."""
    store, task, preset = _build_test_store()
    pack = store.build_context_pack(
        task, preset, [],
        max_chunks_per_section=5,
        token_budget=7500,
        section_token_reserves={"fx_price": 2200, "news_event": 800, "macro": 2000, "risk": 1000},
        safe_user_context=task.safe_user_context,
    )
    fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
    fx_texts = " ".join(it.text for it in fx_items)
    assert "current_rate" in fx_texts or "4.8887" in fx_texts, (
        f"current_rate not found. FX texts: {fx_texts[:200]}"
    )
    print("  current_rate always selected            OK")


def test_at_least_one_historical_context() -> None:
    """At least one of historical_trend or recent_range should be selected."""
    store, task, preset = _build_test_store()
    pack = store.build_context_pack(
        task, preset, [],
        max_chunks_per_section=5,
        token_budget=7500,
        section_token_reserves={"fx_price": 2200, "news_event": 800, "macro": 2000, "risk": 1000},
        safe_user_context=task.safe_user_context,
    )
    fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
    fx_texts = " ".join(it.text for it in fx_items)
    has_historical = "historical_trend" in fx_texts or "90d" in fx_texts or "-0.44%" in fx_texts
    has_recent = "recent_range" in fx_texts or "4.8709" in fx_texts
    assert has_historical or has_recent, (
        f"No historical context found. FX texts: {fx_texts[:200]}"
    )
    print("  at least one historical context         OK")


def test_market_drivers_not_squeezed_out() -> None:
    """MarketDrivers and Risk chunks should not be squeezed out by FX expansion."""
    store, task, preset = _build_test_store()
    pack = store.build_context_pack(
        task, preset, [],
        max_chunks_per_section=5,
        token_budget=7500,
        section_token_reserves={"fx_price": 2200, "news_event": 800, "macro": 2000, "risk": 1000},
        safe_user_context=task.safe_user_context,
    )
    md_items = [it for it in pack.items if it.agent_name == "market_drivers_agent"]
    risk_items = [it for it in pack.items if it.agent_name == "risk_agent"]
    assert len(md_items) >= 1, f"MarketDrivers squeezed out! Got {len(md_items)}"
    assert len(risk_items) >= 1, f"Risk squeezed out! Got {len(risk_items)}"
    print("  market_drivers + risk not squeezed out  OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_target_rate_gap_selected_with_user_target,
        test_current_rate_always_selected,
        test_at_least_one_historical_context,
        test_market_drivers_not_squeezed_out,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6E — FX target_rate_gap priority — {len(tests)} tests")
    print(f"{'='*60}")
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            print(f"  FAIL: {t.__name__} — {exc}")
    print(f"\n{'='*60}")
    if passed == len(tests):
        print(f"All {passed} tests passed.")
    else:
        print(f"{passed}/{len(tests)} tests passed, {len(tests) - passed} FAILED.")
        sys.exit(1)
