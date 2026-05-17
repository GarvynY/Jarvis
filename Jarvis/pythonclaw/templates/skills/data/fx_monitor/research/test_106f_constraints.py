#!/usr/bin/env python3
"""
Phase 10.6F — PolicySignalAgent constraint integration tests.

Tests for 10.6E-fix2 constraint compliance:
  1.  test_weak_policy_not_conflict_eligible
  2.  test_demoted_macro_not_conflict_eligible
  3.  test_policy_dedup_no_inflate_reportable
  4.  test_tier4_only_no_directional_signal
  5.  test_tier1_source_allows_direction
  6.  test_aggregator_low_conf_neutral
  7.  test_dedup_rba_suppresses_macro
  8.  test_dedup_pboc_suppresses_macro
  9.  test_flag_off_unchanged_behavior
  10. test_context_balance_market_drivers_preserved
  11. test_followup_router_reportable_with_policy

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106f_constraints.py
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
    FindingCategory,
    ResearchPreset,
    ResearchTask,
    SafeUserContext,
    SourceRef,
    now_iso,
)
from conflict_detector import (  # noqa: E402
    ConflictPair,
    ConflictSummary,
    detect_conflicts,
    apply_conflict_boost,
    _is_conflict_eligible,
    CONFLICT_BOOST,
)
from schema import EvidenceFinding  # noqa: E402


# ── Test 1: Weak policy finding not conflict eligible ────────────────────────

def test_weak_policy_not_conflict_eligible() -> None:
    """policy_signal with evidence_score < 0.4 is not conflict eligible."""
    weak_policy = EvidenceFinding(
        finding_id="policy_rba_weak",
        agent_name="policy_signal_agent",
        key="policy_rba",
        summary="RBA maybe hawkish",
        direction="bullish_aud",
        chunk_ids=["chunk-rba-weak"],
        category="policy_signal",
        importance=0.6,
        evidence_score=0.3,
    )
    assert not _is_conflict_eligible(weak_policy), "Weak policy should not be eligible"

    strong_policy = EvidenceFinding(
        finding_id="policy_rba_strong",
        agent_name="policy_signal_agent",
        key="policy_rba",
        summary="RBA hawkish hold confirmed",
        direction="bullish_aud",
        chunk_ids=["chunk-rba-strong"],
        category="policy_signal",
        importance=0.8,
        evidence_score=0.7,
    )
    assert _is_conflict_eligible(strong_policy), "Strong policy should be eligible"
    print("  weak policy not conflict eligible       OK")


# ── Test 2: Demoted macro finding not conflict eligible ──────────────────────

def test_demoted_macro_not_conflict_eligible() -> None:
    """Macro finding demoted by policy dedup should not be conflict eligible."""
    demoted = EvidenceFinding(
        finding_id="macro_rba_demoted",
        agent_name="macro_agent",
        key="macro_rba",
        summary="RBA neutral",
        direction=None,
        chunk_ids=["chunk-macro-rba"],
        category="policy_signal",
        importance=0.4,
        evidence_score=0.7,
    )
    # Simulate the evidence_basis that coordinator sets on demoted findings
    demoted.evidence_basis = "(demoted: policy_signal_agent active) macro_rba from 2 sources"
    assert not _is_conflict_eligible(demoted), "Demoted macro should not be eligible"
    print("  demoted macro not conflict eligible     OK")


# ── Test 3: Policy dedup does not inflate reportable conflict count ──────────

def test_policy_dedup_no_inflate_reportable() -> None:
    """Duplicate RBA signals from both agents should not create extra reportable conflicts."""
    from coordinator import _dedup_policy_signals

    policy_output = AgentOutput(
        agent_name="policy_signal_agent",
        status="ok",
        findings=[
            Finding(key="policy_rba", summary="RBA hawkish", category="policy_signal",
                    importance=0.8, direction="bullish_aud", evidence_score=0.7),
            Finding(key="policy_pboc", summary="PBoC dovish", category="policy_signal",
                    importance=0.8, direction="bearish_aud", evidence_score=0.7),
        ],
        confidence=0.7,
    )
    macro_output = AgentOutput(
        agent_name="macro_agent",
        status="ok",
        findings=[
            Finding(key="macro_rba", summary="RBA hawkish", category="policy_signal",
                    importance=0.8, direction="bullish_aud", evidence_score=0.7,
                    direction_for_aud="bullish"),
            Finding(key="macro_pboc", summary="PBoC dovish", category="policy_signal",
                    importance=0.8, direction="bearish_aud", evidence_score=0.7,
                    direction_for_aud="bearish"),
        ],
        confidence=0.65,
    )

    deduped = _dedup_policy_signals([macro_output, policy_output])
    macro_result = next(o for o in deduped if o.agent_name == "macro_agent")

    # After dedup, macro findings should be demoted (no direction)
    for f in macro_result.findings:
        if f.key in ("macro_rba", "macro_pboc"):
            assert f.direction is None, f"{f.key} should have direction=None after dedup"

    # Now run conflict detection on the combined findings
    all_findings = []
    for output in deduped:
        for i, f in enumerate(output.findings):
            ef = EvidenceFinding(
                finding_id=f"{output.agent_name}_{f.key}",
                agent_name=output.agent_name,
                key=f.key,
                summary=f.summary,
                direction=f.direction,
                chunk_ids=[f"chunk-{output.agent_name}-{f.key}"],
                category=f.category or "policy_signal",
                importance=f.importance,
                evidence_score=f.evidence_score,
            )
            ef.evidence_basis = f.evidence_basis or ""
            all_findings.append(ef)

    result = detect_conflicts(all_findings)
    # Should be exactly 1 conflict (policy_rba bullish vs policy_pboc bearish)
    # NOT 2+ (because macro duplicates are demoted/direction=None)
    assert result.conflict_count == 1, (
        f"Expected 1 reportable conflict, got {result.conflict_count}"
    )
    print("  policy dedup no inflate reportable      OK")


# ── Test 4: Tier-4 only sources → no directional signal ──────────────────────

def test_tier4_only_no_directional_signal() -> None:
    """When only tier-4 (unknown/blog) sources, direction should be neutral or None."""
    from agents.policy_signal_agent import _build_findings

    llm_output = {
        "rba_stance": "hawkish",
        "rba_summary": "Some blog says RBA might raise",
        "rba_confidence": 0.6,
        "pboc_stance": "neutral",
        "pboc_summary": "No info",
        "pboc_confidence": 0.3,
        "fed_stance": "dovish",
        "fed_summary": "Random crypto blog says Fed cutting",
        "fed_confidence": 0.55,
    }
    # All results from tier-4 sources
    results = [
        {"url": "https://randomcryptoblog.xyz/rba-news", "title": "RBA maybe", "snippet": "...", "query": "rba interest"},
        {"url": "https://unknownsite.io/fed", "title": "Fed cut?", "snippet": "...", "query": "federal reserve"},
    ]
    findings = _build_findings(llm_output, results, {"policy_rba": results[:1], "policy_fed": results[1:]})

    for f in findings:
        if f.key in ("policy_rba", "policy_fed"):
            assert f.direction is None or f.direction == "neutral", (
                f"{f.key}: tier-4 only should not produce strong direction, got {f.direction}"
            )
    print("  tier-4 only → no directional signal     OK")


# ── Test 5: Tier-1 source allows direction ───────────────────────────────────

def test_tier1_source_allows_direction() -> None:
    """When tier-1 (official) source + high confidence, direction is assigned."""
    from agents.policy_signal_agent import _build_findings

    llm_output = {
        "rba_stance": "hawkish",
        "rba_summary": "RBA raised cash rate to 4.6%",
        "rba_confidence": 0.85,
        "pboc_stance": "dovish",
        "pboc_summary": "PBoC cut LPR by 10bp",
        "pboc_confidence": 0.8,
        "fed_stance": "neutral",
        "fed_summary": "Fed holds steady",
        "fed_confidence": 0.75,
    }
    results = [
        {"url": "https://www.rba.gov.au/media-releases/2026/mr-26-01.html", "title": "RBA Decision",
         "snippet": "The Board decided to raise...", "query": "rba interest rate"},
        {"url": "https://www.pbc.gov.cn/english/130721/5432.html", "title": "PBoC LPR",
         "snippet": "The PBoC announced...", "query": "pboc interest rate"},
        {"url": "https://www.reuters.com/markets/fed-holds", "title": "Fed steady",
         "snippet": "Federal Reserve...", "query": "federal reserve interest"},
    ]
    bucket_results = {
        "policy_rba": [results[0]],
        "policy_pboc": [results[1]],
        "policy_fed": [results[2]],
    }
    findings = _build_findings(llm_output, results, bucket_results)

    rba_f = next(f for f in findings if f.key == "policy_rba")
    pboc_f = next(f for f in findings if f.key == "policy_pboc")
    fed_f = next(f for f in findings if f.key == "policy_fed")

    assert rba_f.direction == "bullish_aud", f"RBA hawkish + tier1 should be bullish_aud, got {rba_f.direction}"
    assert pboc_f.direction == "bullish_aud", f"PBoC dovish should be bullish_aud, got {pboc_f.direction}"
    assert fed_f.direction == "neutral", f"Fed neutral should be neutral, got {fed_f.direction}"
    print("  tier-1 source allows direction          OK")


# ── Test 6: Aggregator + low confidence → neutral ────────────────────────────

def test_aggregator_low_conf_neutral() -> None:
    """Aggregator-only source with low confidence → no direction, low evidence_score."""
    from agents.policy_signal_agent import _build_findings

    llm_output = {
        "rba_stance": "hawkish",
        "rba_summary": "Maybe hawkish",
        "rba_confidence": 0.35,
        "pboc_stance": "neutral",
        "pboc_summary": "",
        "pboc_confidence": 0.2,
        "fed_stance": "dovish",
        "fed_summary": "Maybe dovish",
        "fed_confidence": 0.4,
    }
    results = [
        {"url": "https://someblog.xyz/rba", "title": "RBA", "snippet": "...", "query": "rba"},
        {"url": "https://another-unknown.com/fed", "title": "Fed", "snippet": "...", "query": "federal"},
    ]
    bucket_results = {
        "policy_rba": [results[0]],
        "policy_fed": [results[1]],
    }
    findings = _build_findings(llm_output, results, bucket_results)

    rba_f = next(f for f in findings if f.key == "policy_rba")
    assert rba_f.direction is None or rba_f.direction == "neutral", (
        f"Aggregator + low conf should not produce direction, got {rba_f.direction}"
    )
    assert rba_f.evidence_score <= 0.35, f"Expected low evidence_score, got {rba_f.evidence_score}"
    print("  aggregator + low conf → neutral         OK")


# ── Test 7: Dedup RBA suppresses macro_agent RBA ─────────────────────────────

def test_dedup_rba_suppresses_macro() -> None:
    """policy_signal_agent RBA should suppress macro_agent macro_rba."""
    from coordinator import _dedup_policy_signals

    policy_output = AgentOutput(
        agent_name="policy_signal_agent", status="ok",
        findings=[Finding(key="policy_rba", summary="RBA hold", category="policy_signal",
                          importance=0.8, direction="neutral")],
        confidence=0.7,
    )
    macro_output = AgentOutput(
        agent_name="macro_agent", status="ok",
        findings=[
            Finding(key="macro_rba", summary="RBA hold", category="policy_signal",
                    importance=0.8, direction="neutral", direction_for_aud="neutral"),
            Finding(key="macro_trade", summary="Trade OK", category="macro",
                    importance=0.65, direction="bullish_aud"),
        ],
        confidence=0.65,
    )

    result = _dedup_policy_signals([macro_output, policy_output])
    macro_r = next(o for o in result if o.agent_name == "macro_agent")
    rba_f = next(f for f in macro_r.findings if f.key == "macro_rba")
    trade_f = next(f for f in macro_r.findings if f.key == "macro_trade")

    assert rba_f.importance == 0.4
    assert rba_f.direction is None
    assert "(demoted:" in (rba_f.evidence_basis or "")
    assert trade_f.importance == 0.65
    assert trade_f.direction == "bullish_aud"
    print("  dedup RBA suppresses macro              OK")


# ── Test 8: Dedup PBoC suppresses macro_agent PBoC ───────────────────────────

def test_dedup_pboc_suppresses_macro() -> None:
    """policy_signal_agent PBoC should suppress macro_agent macro_pboc."""
    from coordinator import _dedup_policy_signals

    policy_output = AgentOutput(
        agent_name="policy_signal_agent", status="ok",
        findings=[Finding(key="policy_pboc", summary="PBoC easing", category="policy_signal",
                          importance=0.8, direction="bullish_aud", evidence_score=0.7)],
        confidence=0.7,
    )
    macro_output = AgentOutput(
        agent_name="macro_agent", status="ok",
        findings=[
            Finding(key="macro_pboc", summary="PBoC easing", category="policy_signal",
                    importance=0.8, direction="bearish_aud", direction_for_aud="bearish"),
        ],
        confidence=0.65,
    )

    result = _dedup_policy_signals([macro_output, policy_output])
    macro_r = next(o for o in result if o.agent_name == "macro_agent")
    pboc_f = next(f for f in macro_r.findings if f.key == "macro_pboc")

    assert pboc_f.importance == 0.4
    assert pboc_f.direction is None
    assert pboc_f.direction_for_aud is None
    print("  dedup PBoC suppresses macro             OK")


# ── Test 9: Flag off → behavior unchanged ────────────────────────────────────

def test_flag_off_unchanged_behavior() -> None:
    """When no policy_signal_agent output, dedup is a no-op."""
    from agents.policy_signal_agent import _ENABLE_POLICY_AGENT
    assert _ENABLE_POLICY_AGENT is True, "Flag should be True (10.6G)"

    import coordinator
    assert "policy_signal_agent" in coordinator.AGENT_REGISTRY

    # Simulate: no policy agent output → dedup is a no-op
    from coordinator import _dedup_policy_signals
    macro_output = AgentOutput(
        agent_name="macro_agent", status="ok",
        findings=[Finding(key="macro_rba", summary="RBA hold", category="policy_signal",
                          importance=0.8, direction="neutral")],
        confidence=0.65,
    )
    result = _dedup_policy_signals([macro_output])
    macro_r = next(o for o in result if o.agent_name == "macro_agent")
    rba_f = next(f for f in macro_r.findings if f.key == "macro_rba")
    # Should NOT be demoted (no policy_signal_agent output in the list)
    assert rba_f.importance == 0.8
    assert rba_f.direction == "neutral"
    print("  no policy output → dedup no-op          OK")


# ── Test 10: ContextPack balance — market_drivers preserved ──────────────────

def test_context_balance_market_drivers_preserved() -> None:
    """With policy agent active, MarketDriversAgent chunks still get selected."""
    from evidence_store import EvidenceStore

    store = EvidenceStore(":memory:")
    preset = ResearchPreset(
        name="fx_cnyaud",
        research_type="fx",
        description="CNY/AUD",
        report_sections=["汇率事实", "新闻驱动", "宏观信号", "风险与矛盾"],
        default_agents=["fx_agent", "news_agent", "macro_agent", "market_drivers_agent", "policy_signal_agent"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    task = ResearchTask(
        preset_name="fx_cnyaud",
        research_topic="CNY/AUD",
        focus_pair="CNY/AUD",
        focus_assets=["CNY", "AUD"],
        safe_user_context=SafeUserContext(purpose="living", target_rate=4.95),
    )

    # FX agent
    fx_output = AgentOutput(
        agent_name="fx_agent", status="ok",
        findings=[
            Finding(key="current_rate", summary="1 AUD = 4.89 CNY", category="fx_price",
                    importance=0.75, subcategory="current_rate",
                    source_ids=["https://open.er-api.com/v6/latest/CNY"],
                    evidence_basis="fetch_rate"),
        ],
        sources=[SourceRef(url="https://open.er-api.com/v6/latest/CNY", title="Rate", source="open.er-api.com", retrieved_at=now_iso())],
        confidence=0.85,
    )
    # Market drivers
    md_output = AgentOutput(
        agent_name="market_drivers_agent", status="ok",
        findings=[
            Finding(key="commodity_copper", summary="铜价下跌", category="commodity_trade",
                    importance=0.7, source_ids=["https://finance.yahoo.com/quote/HG=F/"],
                    evidence_basis="yfinance:commodity.copper", direction="bearish_aud"),
        ],
        sources=[SourceRef(url="https://finance.yahoo.com/quote/HG=F/", title="Copper", source="yfinance", retrieved_at=now_iso())],
        confidence=0.74,
    )
    # Policy signal agent
    policy_output = AgentOutput(
        agent_name="policy_signal_agent", status="ok",
        findings=[
            Finding(key="policy_rba", summary="RBA holds", category="policy_signal",
                    importance=0.8, direction="neutral", evidence_score=0.7,
                    source_ids=["https://rba.gov.au/"],
                    evidence_basis="policy_signal_agent:policy_rba tier=1"),
            Finding(key="policy_pboc", summary="PBoC easing", category="policy_signal",
                    importance=0.8, direction="bullish_aud", evidence_score=0.7,
                    source_ids=["https://pbc.gov.cn/"],
                    evidence_basis="policy_signal_agent:policy_pboc tier=1"),
        ],
        sources=[SourceRef(url="https://rba.gov.au/", title="RBA", source="rba.gov.au", retrieved_at=now_iso())],
        confidence=0.7,
    )
    # Risk
    risk_output = AgentOutput(
        agent_name="risk_agent", status="ok",
        findings=[
            Finding(key="signal_contradiction", summary="多空矛盾", category="risk",
                    importance=0.7, evidence_basis="risk_agent"),
        ],
        confidence=0.75,
    )

    store.ingest_outputs(task, [fx_output, md_output, policy_output, risk_output])
    pack = store.build_context_pack(
        task, preset, [],
        max_chunks_per_section=5,
        token_budget=7500,
        section_token_reserves={"fx_price": 2200, "news_event": 800, "macro": 2000, "risk": 1000},
        safe_user_context=task.safe_user_context,
    )

    agents_present = {it.agent_name for it in pack.items}
    # MarketDrivers should still be present
    md_items = [it for it in pack.items if it.agent_name == "market_drivers_agent"]
    fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
    risk_items = [it for it in pack.items if it.agent_name == "risk_agent"]

    assert len(fx_items) >= 1, "FX current_rate must be selected"
    assert len(md_items) >= 1, f"MarketDrivers squeezed out! Agents: {agents_present}"
    assert len(risk_items) >= 1, f"Risk squeezed out! Agents: {agents_present}"
    print("  context balance: market_drivers kept    OK")


# ── Test 11: Followup router uses reportable with policy ─────────────────────

def test_followup_router_reportable_with_policy() -> None:
    """Followup router should use reportable_conflict_count even with policy agent."""
    from followup_router import generate_followup_requests, _conflict_count

    task = ResearchTask(task_id="t1", preset_name="fx_cnyaud")
    outputs = [
        AgentOutput(agent_name="policy_signal_agent", status="ok"),
        AgentOutput(agent_name="fx_agent", status="ok"),
    ]

    # reportable=1 (below threshold) → no high_conflict_count followup
    conflict_data = {"conflict_count": 5, "reportable_conflict_count": 1}
    reqs = generate_followup_requests(task, outputs, conflict_summary=conflict_data)
    trigger_types = [r.trigger_type for r in reqs]
    assert "high_conflict_count" not in trigger_types, f"Should not trigger, got: {trigger_types}"

    # reportable=3 → should trigger
    conflict_data2 = {"conflict_count": 8, "reportable_conflict_count": 3}
    reqs2 = generate_followup_requests(task, outputs, conflict_summary=conflict_data2)
    trigger_types2 = [r.trigger_type for r in reqs2]
    assert "high_conflict_count" in trigger_types2, f"Should trigger for reportable=3, got: {trigger_types2}"
    print("  followup router reportable with policy  OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_weak_policy_not_conflict_eligible,
        test_demoted_macro_not_conflict_eligible,
        test_policy_dedup_no_inflate_reportable,
        test_tier4_only_no_directional_signal,
        test_tier1_source_allows_direction,
        test_aggregator_low_conf_neutral,
        test_dedup_rba_suppresses_macro,
        test_dedup_pboc_suppresses_macro,
        test_flag_off_unchanged_behavior,
        test_context_balance_market_drivers_preserved,
        test_followup_router_reportable_with_policy,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6F Constraints — {len(tests)} tests")
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
