#!/usr/bin/env python3
"""
Phase 9 Step 3d — RiskAgent standalone tests.

No mocking required — RiskAgent is pure synthesis (no I/O).

Tests:
  1. test_contradiction_detected    — bullish + bearish → signal_contradiction
  2. test_no_contradiction_bullish  — all bullish → dominant_signal bullish
  3. test_no_contradiction_bearish  — all bearish → dominant_signal bearish
  4. test_failed_agents             — error output → data_gap_failed_agents
  5. test_partial_agents            — partial output → data_gap_partial_agents
  6. test_empty_phase1              — [] → status=error, no_data finding
  7. test_low_confidence_risk       — avg confidence < 0.4 → warning risk
  8. test_confidence_capped         — output confidence <= 0.70
  9. test_json_safe                 — output passes JSON-safety check
 10. test_dedup_risks               — duplicate risk strings collapsed once
 11. test_missing_sources_detected   — findings with no sources are flagged
 12. test_stale_data_detected        — old as_of / source timestamps are flagged
 13. test_missing_source_timestamp   — missing source timestamps are not treated as stale

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research/agents
    python test_risk_agent.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

_HERE         = Path(__file__).parent
_RESEARCH_DIR = _HERE.parent

if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from schema import (  # noqa: E402
    AgentOutput, Finding, ResearchTask, SafeUserContext, FX_CNYAUD_PRESET,
    SourceRef,
)
from agents.risk_agent import RiskAgent  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_task() -> ResearchTask:
    return ResearchTask.from_preset(
        FX_CNYAUD_PRESET,
        safe_user_context=SafeUserContext(purpose="tuition"),
        focus_assets=["AUD", "CNY"],
        focus_pair="CNY/AUD",
    )


def _ok_output(
    agent_name: str,
    directions: list[str | None],
    confidence: float = 0.7,
) -> AgentOutput:
    out = AgentOutput(
        agent_name=agent_name,
        status="ok",
        summary=f"mock output from {agent_name}",
        confidence=confidence,
    )
    out.findings = [
        Finding(key=f"f_{i}", summary=f"finding {i}", direction=d)
        for i, d in enumerate(directions)
    ]
    return out


def _print_output(output: AgentOutput) -> None:
    print(f"   status={output.status}  conf={output.confidence:.2f}  "
          f"latency={output.latency_ms}ms")
    for f in output.findings:
        print(f"   [{f.key:30s}] [{f.direction or 'none':14s}] {f.summary[:55]}")
    if output.risks:
        for r in output.risks:
            print(f"   risk: {r[:72]}")
    if output.missing_data:
        print(f"   missing: {output.missing_data}")


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_contradiction_detected() -> None:
    """Bullish + bearish findings → signal_contradiction finding."""
    phase1 = [
        _ok_output("fx_agent",    ["bullish_aud", "bullish_aud"]),
        _ok_output("news_agent",  ["bearish_aud"]),
        _ok_output("macro_agent", ["neutral"]),
    ]
    output = await RiskAgent().run(_make_task(), phase1)

    keys = {f.key for f in output.findings}
    assert "signal_contradiction" in keys, f"Expected signal_contradiction. Got: {keys}"
    assert output.status == "ok"
    assert output.agent_name == "risk_agent"

    print("\n-- test_contradiction_detected")
    _print_output(output)
    print("   PASS")


async def test_no_contradiction_bullish() -> None:
    """Only bullish signals → dominant_signal bullish, no contradiction."""
    phase1 = [
        _ok_output("fx_agent",    ["bullish_aud", "bullish_aud"]),
        _ok_output("macro_agent", ["bullish_aud"]),
    ]
    output = await RiskAgent().run(_make_task(), phase1)

    keys = {f.key for f in output.findings}
    assert "signal_contradiction" not in keys, "Should not have contradiction"
    dom = next((f for f in output.findings if f.key == "dominant_signal"), None)
    assert dom is not None, "Expected dominant_signal finding"
    assert dom.direction == "bullish_aud", f"Expected bullish, got {dom.direction}"

    print("\n-- test_no_contradiction_bullish")
    _print_output(output)
    print("   PASS")


async def test_no_contradiction_bearish() -> None:
    """Only bearish signals → dominant_signal bearish."""
    phase1 = [
        _ok_output("fx_agent",    ["bearish_aud"]),
        _ok_output("macro_agent", ["bearish_aud", "bearish_aud"]),
    ]
    output = await RiskAgent().run(_make_task(), phase1)

    dom = next((f for f in output.findings if f.key == "dominant_signal"), None)
    assert dom is not None
    assert dom.direction == "bearish_aud"

    print("\n-- test_no_contradiction_bearish")
    _print_output(output)
    print("   PASS")


async def test_failed_agents() -> None:
    """Error output → data_gap_failed_agents finding, agent in missing_data."""
    error_out = AgentOutput.make_error("fx_agent", "network error", latency_ms=100)
    phase1 = [error_out, _ok_output("news_agent", ["neutral"])]
    output = await RiskAgent().run(_make_task(), phase1)

    keys = {f.key for f in output.findings}
    assert "data_gap_failed_agents" in keys, f"Expected data_gap_failed_agents. Got: {keys}"
    assert "fx_agent" in output.missing_data

    print("\n-- test_failed_agents")
    _print_output(output)
    print("   PASS")


async def test_partial_agents() -> None:
    """Partial output → data_gap_partial_agents finding."""
    partial_out = AgentOutput(
        agent_name="news_agent",
        status="partial",
        summary="partial news",
        confidence=0.3,
        missing_data=["some_data"],
    )
    phase1 = [_ok_output("fx_agent", ["bullish_aud"]), partial_out]
    output = await RiskAgent().run(_make_task(), phase1)

    keys = {f.key for f in output.findings}
    assert "data_gap_partial_agents" in keys, f"Expected data_gap_partial_agents. Got: {keys}"
    gap = next(f for f in output.findings if f.key == "data_gap_partial_agents")
    assert gap.category == "data_gap"
    assert gap.subcategory == "partial_agents"

    print("\n-- test_partial_agents")
    _print_output(output)
    print("   PASS")


async def test_106c_risk_output_types() -> None:
    """10.6C: risk and data-gap findings carry explicit routing metadata."""
    partial_out = AgentOutput(
        agent_name="news_agent",
        status="partial",
        summary="partial news",
        confidence=0.3,
        missing_data=["no_high_relevance_news"],
    )
    phase1 = [
        _ok_output("fx_agent", ["bullish_aud"]),
        _ok_output("macro_agent", ["bearish_aud"]),
        partial_out,
    ]
    output = await RiskAgent().run(_make_task(), phase1)
    by_key = {f.key: f for f in output.findings}

    assert by_key["signal_contradiction"].category == "risk"
    assert by_key["signal_contradiction"].subcategory == "contradiction"
    assert by_key["data_gap_partial_agents"].category == "data_gap"
    assert by_key["low_confidence_outputs"].category == "data_gap"
    assert by_key["dominant_signal"].category == "risk"
    assert by_key["signal_contradiction"].evidence_basis

    print("\n-- test_106c_risk_output_types")
    print("   contradiction/risk/data_gap metadata present")
    print("   PASS")


async def test_empty_phase1() -> None:
    """Empty phase1 list → status=error, no_data finding."""
    output = await RiskAgent().run(_make_task(), [])

    assert output.status == "error", f"Expected error, got {output.status}"
    keys = {f.key for f in output.findings}
    assert "no_data" in keys, f"Expected no_data finding. Got: {keys}"

    print("\n-- test_empty_phase1")
    _print_output(output)
    print("   PASS")


async def test_low_confidence_risk() -> None:
    """Average phase1 confidence < 0.4 → low-confidence warning in risks."""
    phase1 = [
        _ok_output("fx_agent",    ["neutral"], confidence=0.2),
        _ok_output("news_agent",  ["neutral"], confidence=0.1),
    ]
    output = await RiskAgent().run(_make_task(), phase1)

    assert any("置信度" in r for r in output.risks), (
        f"Expected confidence warning risk, got: {output.risks}"
    )

    print("\n-- test_low_confidence_risk")
    print(f"   risks: {output.risks}")
    print("   PASS")


async def test_confidence_capped() -> None:
    """Output confidence <= 0.70 (min of avg phase1 conf)."""
    phase1 = [
        _ok_output("fx_agent",    ["bullish_aud"], confidence=1.0),
        _ok_output("news_agent",  ["bullish_aud"], confidence=1.0),
        _ok_output("macro_agent", ["bullish_aud"], confidence=1.0),
    ]
    output = await RiskAgent().run(_make_task(), phase1)

    assert output.confidence <= 0.70, (
        f"Confidence {output.confidence} exceeds 0.70 cap"
    )

    print("\n-- test_confidence_capped")
    print(f"   confidence={output.confidence}  (cap=0.70)")
    print("   PASS")


async def test_json_safe() -> None:
    """Output serialises to valid JSON."""
    phase1 = [
        _ok_output("fx_agent",    ["bullish_aud", "bearish_aud"]),
        _ok_output("news_agent",  ["neutral"]),
        AgentOutput.make_error("macro_agent", "yfinance not installed", latency_ms=5),
    ]
    output = await RiskAgent().run(_make_task(), phase1)

    raw    = json.dumps(output.to_dict(), ensure_ascii=False)
    parsed = json.loads(raw)
    assert parsed["agent_name"] == "risk_agent"

    print("\n-- test_json_safe")
    print(f"   JSON length: {len(raw)} chars")
    print("   PASS")


async def test_dedup_risks() -> None:
    """Risk strings from phase1 outputs are deduplicated in final output."""
    shared_risk = "日收盘价格波动偏高，参考汇率时效性有限"
    out1 = _ok_output("fx_agent", ["bullish_aud"])
    out1.risks = [shared_risk]
    out2 = _ok_output("news_agent", ["neutral"])
    out2.risks = [shared_risk]   # same risk string from a different agent

    output = await RiskAgent().run(_make_task(), [out1, out2])

    # The shared risk should appear only once
    count = output.risks.count(shared_risk)
    assert count == 1, f"Expected 1 occurrence of shared risk, got {count}"

    print("\n-- test_dedup_risks")
    print(f"   {len(output.risks)} unique risks (shared risk deduplicated)")
    print("   PASS")


async def test_missing_sources_detected() -> None:
    """Output with findings but no SourceRef → missing_sources finding."""
    phase1 = [_ok_output("news_agent", ["neutral"])]
    output = await RiskAgent().run(_make_task(), phase1)

    keys = {f.key for f in output.findings}
    assert "missing_sources_news_agent" in keys, f"Expected missing source finding, got: {keys}"
    assert "missing_sources:news_agent" in output.missing_data

    print("\n-- test_missing_sources_detected")
    _print_output(output)
    print("   PASS")


async def test_stale_news_data_detected() -> None:
    """News older than default 48h threshold → stale_data finding."""
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    out = _ok_output("news_agent", ["neutral"])
    out.as_of = old
    out.sources = [
        SourceRef(
            title="old news source",
            url="https://example.com/old",
            source="mock",
            retrieved_at=old,
            published_at=old,
        )
    ]
    output = await RiskAgent().run(_make_task(), [out])

    keys = {f.key for f in output.findings}
    assert "stale_data_news_agent" in keys, f"Expected stale data finding, got: {keys}"
    assert "stale_data:news_agent" in output.missing_data

    print("\n-- test_stale_news_data_detected")
    _print_output(output)
    print("   PASS")


async def test_macro_data_allows_one_week() -> None:
    """Macro data within one week is not stale, but older macro data is."""
    recent = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()

    recent_out = _ok_output("macro_agent", ["neutral"])
    recent_out.as_of = recent
    recent_out.sources = [
        SourceRef(
            title="recent macro source",
            url="https://example.com/recent",
            source="mock",
            retrieved_at=recent,
            published_at=recent,
        )
    ]
    recent_result = await RiskAgent().run(_make_task(), [recent_out])
    recent_keys = {f.key for f in recent_result.findings}
    assert "stale_data_macro_agent" not in recent_keys

    old_out = _ok_output("macro_agent", ["neutral"])
    old_out.as_of = old
    old_out.sources = [
        SourceRef(
            title="old macro source",
            url="https://example.com/old",
            source="mock",
            retrieved_at=old,
            published_at=old,
        )
    ]
    old_result = await RiskAgent().run(_make_task(), [old_out])
    old_keys = {f.key for f in old_result.findings}
    assert "stale_data_macro_agent" in old_keys
    assert "stale_data:macro_agent" in old_result.missing_data

    print("\n-- test_macro_data_allows_one_week")
    print(f"   recent_keys={recent_keys}")
    print(f"   old_keys={old_keys}")
    print("   PASS")


async def test_missing_source_timestamp() -> None:
    """Source without timestamps → missing timestamp finding, not stale."""
    out = _ok_output("news_agent", ["neutral"])
    out.sources = [
        SourceRef(
            title="untimed source",
            url="https://example.com/untimed",
            source="mock",
            retrieved_at="",
            published_at=None,
        )
    ]
    output = await RiskAgent().run(_make_task(), [out])

    keys = {f.key for f in output.findings}
    assert "missing_source_timestamp_news_agent" in keys, f"Expected missing timestamp, got: {keys}"
    assert "stale_data_news_agent" not in keys, "Missing timestamp should not be treated as stale"

    print("\n-- test_missing_source_timestamp")
    _print_output(output)
    print("   PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 3d -- RiskAgent tests (no I/O, no mocking)")
    print("=" * 60)
    await test_contradiction_detected()
    await test_no_contradiction_bullish()
    await test_no_contradiction_bearish()
    await test_failed_agents()
    await test_partial_agents()
    await test_106c_risk_output_types()
    await test_empty_phase1()
    await test_low_confidence_risk()
    await test_confidence_capped()
    await test_json_safe()
    await test_dedup_risks()
    await test_missing_sources_detected()
    await test_stale_news_data_detected()
    await test_macro_data_allows_one_week()
    await test_missing_source_timestamp()
    print("\n" + "=" * 60)
    print("All 14 tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
