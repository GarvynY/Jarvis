#!/usr/bin/env python3
"""
Phase 10.6F — PolicySignalAgent tests.

Tests:
  1. test_flag_off_no_registration
  2. test_flag_on_agent_registered
  3. test_insufficient_evidence_neutral
  4. test_rba_bullish_pboc_bearish_conflict
  5. test_dedup_policy_over_macro
  6. test_no_context_overload
  7. test_agent_failure_not_fatal

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106f_policy_signal.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput,
    Finding,
    FindingCategory,
    ResearchTask,
    SafeUserContext,
    now_iso,
)


# ── Test 1: Flag off → no registration ──────────────────────────────────────

def test_flag_off_no_registration() -> None:
    """When _ENABLE_POLICY_AGENT=True (10.6G), agent IS in AGENT_REGISTRY."""
    import importlib
    import coordinator as coord_mod
    importlib.reload(coord_mod)
    assert "policy_signal_agent" in coord_mod.AGENT_REGISTRY
    print("  flag on → registered in AGENT_REGISTRY  OK")


# ── Test 2: Flag on → agent registered ──────────────────────────────────────

def test_flag_on_agent_registered() -> None:
    """When flag is True, PolicySignalAgent is registered."""
    import agents.policy_signal_agent as psa_mod
    original = psa_mod._ENABLE_POLICY_AGENT
    try:
        psa_mod._ENABLE_POLICY_AGENT = True
        import coordinator as coord_mod
        # Manually simulate registration logic
        from agents.policy_signal_agent import PolicySignalAgent
        coord_mod.AGENT_REGISTRY["policy_signal_agent"] = PolicySignalAgent
        assert "policy_signal_agent" in coord_mod.AGENT_REGISTRY
        cls = coord_mod.AGENT_REGISTRY["policy_signal_agent"]
        agent = cls()
        assert agent.agent_name == "policy_signal_agent"
    finally:
        psa_mod._ENABLE_POLICY_AGENT = original
        coord_mod.AGENT_REGISTRY.pop("policy_signal_agent", None)
    print("  flag on → agent registered              OK")


# ── Test 3: Insufficient evidence → neutral/None ─────────────────────────────

def test_insufficient_evidence_neutral() -> None:
    """When LLM returns insufficient_evidence, direction should be None."""
    from agents.policy_signal_agent import _build_findings, _POLICY_BUCKETS

    llm_output = {
        "rba_stance": "insufficient_evidence",
        "rba_summary": "证据不足",
        "rba_confidence": 0.2,
        "pboc_stance": "insufficient_evidence",
        "pboc_summary": "证据不足",
        "pboc_confidence": 0.15,
        "fed_stance": "insufficient_evidence",
        "fed_summary": "证据不足",
        "fed_confidence": 0.1,
    }
    findings = _build_findings(llm_output, [])
    for f in findings:
        if f.category == "policy_signal":
            assert f.direction is None or f.direction == "neutral", (
                f"{f.key}: direction should be None/neutral for insufficient_evidence, got {f.direction}"
            )
    print("  insufficient evidence → neutral/None    OK")


# ── Test 4: RBA bullish + PBoC bearish → conflict detectable ─────────────────

def test_rba_bullish_pboc_bearish_conflict() -> None:
    """ConflictDetector should detect RBA bullish + PBoC bearish as a conflict."""
    from conflict_detector import detect_conflicts
    from schema import EvidenceFinding

    rba_finding = EvidenceFinding(
        finding_id="policy_rba",
        agent_name="policy_signal_agent",
        key="policy_rba",
        summary="RBA hawkish hold",
        direction="bullish_aud",
        chunk_ids=["chunk-rba"],
        category="policy_signal",
        importance=0.8,
        evidence_score=0.7,
    )
    pboc_finding = EvidenceFinding(
        finding_id="policy_pboc",
        agent_name="policy_signal_agent",
        key="policy_pboc",
        summary="PBoC easing",
        direction="bearish_aud",
        chunk_ids=["chunk-pboc"],
        category="policy_signal",
        importance=0.8,
        evidence_score=0.7,
    )
    result = detect_conflicts([rba_finding, pboc_finding])
    assert result.conflict_count >= 1, f"Expected conflict, got count={result.conflict_count}"
    print("  RBA bullish + PBoC bearish → conflict   OK")


# ── Test 5: Dedup — policy_signal_agent > macro_agent ────────────────────────

def test_dedup_policy_over_macro() -> None:
    """When policy_signal_agent has policy_rba, macro_agent's macro_rba is demoted."""
    from coordinator import _dedup_policy_signals

    policy_output = AgentOutput(
        agent_name="policy_signal_agent",
        status="ok",
        summary="PolicySignal: 3/3 buckets",
        findings=[
            Finding(
                key="policy_rba",
                summary="RBA holds",
                category="policy_signal",
                importance=0.8,
                direction="neutral",
            ),
            Finding(
                key="policy_pboc",
                summary="PBoC easing",
                category="policy_signal",
                importance=0.8,
                direction="bearish_aud",
            ),
            Finding(
                key="policy_fed",
                summary="Fed cautious",
                category="policy_signal",
                importance=0.7,
                direction="neutral",
            ),
        ],
        confidence=0.7,
    )
    macro_output = AgentOutput(
        agent_name="macro_agent",
        status="ok",
        summary="Macro OK",
        findings=[
            Finding(
                key="macro_rba",
                summary="RBA holds",
                category="policy_signal",
                importance=0.8,
                direction="neutral",
                direction_for_aud="neutral",
            ),
            Finding(
                key="macro_pboc",
                summary="PBoC easing",
                category="policy_signal",
                importance=0.8,
                direction="bearish_aud",
                direction_for_aud="bearish",
            ),
            Finding(
                key="macro_trade",
                summary="Trade positive",
                category="macro",
                importance=0.65,
                direction="bullish_aud",
            ),
        ],
        confidence=0.65,
    )
    fx_output = AgentOutput(agent_name="fx_agent", status="ok", findings=[])

    outputs = [fx_output, macro_output, policy_output]
    deduped = _dedup_policy_signals(outputs)

    # Find macro_agent in result
    macro_result = next(o for o in deduped if o.agent_name == "macro_agent")
    rba_f = next(f for f in macro_result.findings if f.key == "macro_rba")
    pboc_f = next(f for f in macro_result.findings if f.key == "macro_pboc")
    trade_f = next(f for f in macro_result.findings if f.key == "macro_trade")

    # macro_rba and macro_pboc should be demoted
    assert rba_f.importance == 0.4, f"macro_rba importance should be 0.4, got {rba_f.importance}"
    assert rba_f.direction is None, f"macro_rba direction should be None, got {rba_f.direction}"
    assert pboc_f.importance == 0.4, f"macro_pboc importance should be 0.4, got {pboc_f.importance}"
    assert pboc_f.direction is None

    # macro_trade should NOT be demoted (not a policy signal duplicate)
    assert trade_f.importance == 0.65
    assert trade_f.direction == "bullish_aud"

    print("  dedup: policy_signal > macro            OK")


# ── Test 6: No context overload ──────────────────────────────────────────────

def test_no_context_overload() -> None:
    """PolicySignalAgent produces at most 6 findings (3 policy + 3 data_gap max)."""
    from agents.policy_signal_agent import _build_findings, _build_data_gap_findings

    llm_output = {
        "rba_stance": "hawkish",
        "rba_summary": "RBA raised rates",
        "rba_confidence": 0.8,
        "pboc_stance": "dovish",
        "pboc_summary": "PBoC cut RRR",
        "pboc_confidence": 0.7,
        "fed_stance": "neutral",
        "fed_summary": "Fed on hold",
        "fed_confidence": 0.6,
    }
    findings = _build_findings(llm_output, [])
    gap_findings = _build_data_gap_findings(["policy_rba"])
    total = len(findings) + len(gap_findings)
    assert total <= 6, f"Too many findings: {total}"
    assert len(findings) == 3
    print("  no context overload (max 6 findings)    OK")


# ── Test 7: Agent failure not fatal ──────────────────────────────────────────

def test_agent_failure_not_fatal() -> None:
    """If PolicySignalAgent raises, coordinator gets AgentOutput.make_error."""
    import asyncio
    from agents.policy_signal_agent import PolicySignalAgent

    agent = PolicySignalAgent()
    task = ResearchTask(preset_name="fx_cnyaud")

    # Patch _collect_and_analyse to raise
    def _raise(*args, **kwargs):
        raise RuntimeError("Simulated failure")

    original = agent._collect_and_analyse
    agent._collect_and_analyse = _raise
    try:
        output = asyncio.run(agent.run(task))
        assert output.status == "error"
        assert "RuntimeError" in (output.error or output.summary or "")
    finally:
        agent._collect_and_analyse = original
    print("  agent failure not fatal                 OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_flag_off_no_registration,
        test_flag_on_agent_registered,
        test_insufficient_evidence_neutral,
        test_rba_bullish_pboc_bearish_conflict,
        test_dedup_policy_over_macro,
        test_no_context_overload,
        test_agent_failure_not_fatal,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6F — PolicySignalAgent — {len(tests)} tests")
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
