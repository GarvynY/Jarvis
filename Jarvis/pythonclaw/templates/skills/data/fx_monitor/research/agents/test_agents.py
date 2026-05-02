#!/usr/bin/env python3
"""
Local smoke tests for Phase 9 agents.

Run from the project root:
    python -m pytest Jarvis/pythonclaw/templates/skills/data/fx_monitor/research/agents/test_agents.py -v
Or directly:
    python Jarvis/pythonclaw/templates/skills/data/fx_monitor/research/agents/test_agents.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make schema importable
_RESEARCH_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_RESEARCH_DIR.parent.parent.parent.parent.parent))  # Jarvis root

from fx_monitor.research.schema import (  # noqa: E402
    ResearchTask,
    SafeUserContext,
    AgentOutput,
    FX_CNYAUD_PRESET,
)
from fx_monitor.research.agents import fx_agent, news_agent, macro_agent, risk_agent  # noqa: E402


def _make_task() -> ResearchTask:
    return ResearchTask(
        preset_name="fx_cnyaud",
        research_type="fx",
        research_topic="CNY/AUD 外汇研究",
        focus_assets=["AUD", "CNY"],
        focus_pair="CNY/AUD",
        time_horizon="short_term",
        safe_user_context=SafeUserContext(purpose="tuition", risk_level="low"),
    )


def _assert_agent_output(output: AgentOutput, agent_name: str) -> None:
    assert isinstance(output, AgentOutput), f"{agent_name}: expected AgentOutput"
    assert output.agent_name == agent_name, f"{agent_name}: wrong agent_name"
    assert output.status in ("ok", "partial", "error"), f"{agent_name}: invalid status"
    assert isinstance(output.findings, list), f"{agent_name}: findings must be list"
    assert isinstance(output.sources, list), f"{agent_name}: sources must be list"
    assert isinstance(output.risks, list), f"{agent_name}: risks must be list"
    assert 0.0 <= output.confidence <= 1.0, f"{agent_name}: confidence out of range"
    assert output.latency_ms >= 0, f"{agent_name}: latency_ms must be >= 0"
    # JSON serialisable
    import json
    json.dumps(output.to_dict(), ensure_ascii=False)
    print(f"  [{output.status.upper():7s}] {agent_name}: {output.summary[:80]}")
    if output.findings:
        for f in output.findings[:3]:
            print(f"    finding: [{f.direction or 'none':15s}] {f.summary[:70]}")
    if output.risks:
        print(f"    risks: {output.risks[:2]}")
    if output.error:
        print(f"    error: {output.error}")


def test_fx_agent() -> None:
    print("\n── fx_agent ──────────────────────────────────────────────────────────")
    task = _make_task()
    output = fx_agent(task)
    _assert_agent_output(output, "fx_agent")


def test_news_agent() -> None:
    print("\n── news_agent ────────────────────────────────────────────────────────")
    task = _make_task()
    output = news_agent(task)
    _assert_agent_output(output, "news_agent")


def test_macro_agent() -> None:
    print("\n── macro_agent ───────────────────────────────────────────────────────")
    task = _make_task()
    output = macro_agent(task)
    _assert_agent_output(output, "macro_agent")


def test_risk_agent() -> None:
    print("\n── risk_agent (with mock phase-1 outputs) ────────────────────────────")
    task = _make_task()
    # Provide minimal mock outputs
    mock_fx = AgentOutput(
        agent_name="fx_agent", status="ok", summary="1 AUD = 4.7800 CNY",
        confidence=0.9,
    )
    from fx_monitor.research.schema import Finding
    mock_fx.findings = [Finding(key="current_rate", summary="1 AUD = 4.7800 CNY", direction="bullish_aud")]
    mock_news = AgentOutput(
        agent_name="news_agent", status="ok", summary="3 articles",
        confidence=0.5,
    )
    mock_news.findings = [Finding(key="news_0", summary="RBA holds rates", direction="bearish_aud")]
    output = risk_agent(task, [mock_fx, mock_news])
    _assert_agent_output(output, "risk_agent")
    # Should detect contradiction (bullish + bearish)
    keys = [f.key for f in output.findings]
    assert "signal_contradiction" in keys, f"Expected contradiction finding, got: {keys}"
    print("    contradiction detection: PASS")


def test_risk_agent_with_failed_phase1() -> None:
    print("\n── risk_agent (with failed phase-1) ─────────────────────────────────")
    task = _make_task()
    error_output = AgentOutput.make_error("fx_agent", "network error", latency_ms=100)
    output = risk_agent(task, [error_output])
    _assert_agent_output(output, "risk_agent")
    keys = [f.key for f in output.findings]
    assert "data_gap_failed_agents" in keys, f"Expected data gap finding, got: {keys}"
    print("    failed-agent detection: PASS")


if __name__ == "__main__":
    print("Phase 9 Agent Smoke Tests")
    print("=" * 60)
    try:
        test_fx_agent()
        test_news_agent()
        test_macro_agent()
        test_risk_agent()
        test_risk_agent_with_failed_phase1()
        print("\n✓ All tests passed.")
    except AssertionError as e:
        print(f"\n✗ FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {type(e).__name__}: {e}")
        sys.exit(1)
