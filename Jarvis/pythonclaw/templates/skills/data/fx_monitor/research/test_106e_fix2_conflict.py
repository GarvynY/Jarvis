#!/usr/bin/env python3
"""
Phase 10.6E-fix2 — Conflict count consistency & weak news gate tests.

Tests:
  1.  test_duplicate_conflict_pair_counted_once
  2.  test_low_confidence_news_no_conflict_boost
  3.  test_aggregator_low_conf_news_selectable_no_boost
  4.  test_reportable_count_1_no_high_conflict_followup
  5.  test_valid_policy_conflict_counted
  6.  test_market_driver_conflict_counted_when_eligible
  7.  test_conflict_summary_has_reportable_fields
  8.  test_followup_router_uses_reportable_count
  9.  test_evidence_eval_deduplicates_across_sections
  10. test_ineligible_chunk_ids_skip_boost

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106e_fix2_conflict.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import EvidenceFinding, RetrievalTrace  # noqa: E402
from conflict_detector import (  # noqa: E402
    ConflictPair,
    ConflictSummary,
    detect_conflicts,
    apply_conflict_boost,
    _is_conflict_eligible,
    _dedup_key,
    CONFLICT_BOOST,
)
from followup_router import (  # noqa: E402
    generate_followup_requests,
    _conflict_count,
    _HIGH_CONFLICT_THRESHOLD,
)
from evidence_eval import summarize_retrieval_traces  # noqa: E402
from schema import AgentOutput, ResearchTask  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_finding(
    *,
    finding_id: str = "find-1",
    category: str = "fx_price",
    direction: str | None = "bullish_aud",
    chunk_ids: list[str] | None = None,
    importance: float = 0.5,
    evidence_score: float | None = None,
) -> EvidenceFinding:
    return EvidenceFinding(
        finding_id=finding_id,
        agent_name="test_agent",
        key="test_key",
        summary="test summary",
        direction=direction,
        chunk_ids=chunk_ids or [f"chunk-{finding_id}"],
        category=category,
        importance=importance,
        evidence_score=evidence_score,
    )


# ── Test 1: Duplicate pair counted once ──────────────────────────────────────

def test_duplicate_conflict_pair_counted_once() -> None:
    """Same conflict pair through different detection paths counted once."""
    fa = _make_finding(finding_id="f1", direction="bullish_aud", category="market_driver")
    fb = _make_finding(finding_id="f2", direction="bearish_aud", category="market_driver")
    result = detect_conflicts([fa, fb], chunk_entities={"chunk-f1": ["AUD"], "chunk-f2": ["AUD"]})
    assert result.conflict_count == 1, f"Expected 1, got {result.conflict_count}"
    print("  duplicate pair counted once              OK")


# ── Test 2: Low-confidence news no conflict boost ────────────────────────────

def test_low_confidence_news_no_conflict_boost() -> None:
    """Weak news chunk should not receive conflict boost."""
    summary = ConflictSummary(
        conflicts=[ConflictPair(
            finding_id_a="f1", finding_id_b="f2",
            chunk_id_a="news-weak", chunk_id_b="fx-strong",
            rule="same_category_opposite_direction",
        )],
        conflict_count=1,
        conflicting_chunk_ids={"news-weak", "fx-strong"},
    )
    score_map = {"news-weak": 0.4, "fx-strong": 0.6}
    apply_conflict_boost(
        score_map, summary,
        ineligible_chunk_ids={"news-weak"},
    )
    assert score_map["news-weak"] == 0.4, f"Weak news was boosted: {score_map['news-weak']}"
    assert score_map["fx-strong"] > 0.6, f"Strong chunk not boosted: {score_map['fx-strong']}"
    print("  low conf news no conflict boost         OK")


# ── Test 3: Aggregator low-conf selectable but no boost ──────────────────────

def test_aggregator_low_conf_news_selectable_no_boost() -> None:
    """Low-conf aggregator news can be selected but shouldn't be conflict-eligible."""
    news_finding = _make_finding(
        finding_id="news-agg", direction="bullish_aud",
        category="news_event", importance=0.5, evidence_score=0.1,
    )
    assert not _is_conflict_eligible(news_finding)
    fx_finding = _make_finding(
        finding_id="fx-good", direction="bearish_aud",
        category="news_event", importance=0.7, evidence_score=0.6,
    )
    result = detect_conflicts([news_finding, fx_finding])
    assert result.conflict_count == 0
    print("  aggregator low-conf no boost            OK")


# ── Test 4: reportable=1 → no high_conflict_count followup ───────────────────

def test_reportable_count_1_no_high_conflict_followup() -> None:
    """When reportable_conflict_count=1, no high_conflict_count followup."""
    task = ResearchTask(task_id="t1", preset_name="fx_cnyaud")
    outputs = [AgentOutput(agent_name="fx_agent", status="ok")]
    conflict_data = {"reportable_conflict_count": 1, "conflict_count": 1}
    reqs = generate_followup_requests(task, outputs, conflict_summary=conflict_data)
    trigger_types = [r.trigger_type for r in reqs]
    assert "high_conflict_count" not in trigger_types, f"Got: {trigger_types}"
    print("  reportable=1 no high_conflict followup  OK")


# ── Test 5: Valid policy conflict counted ────────────────────────────────────

def test_valid_policy_conflict_counted() -> None:
    """Policy signal conflicts with good evidence_score are counted."""
    fa = _make_finding(
        finding_id="macro-hawkish", direction="bullish_aud",
        category="policy_signal", importance=0.8, evidence_score=0.75,
    )
    fb = _make_finding(
        finding_id="macro-dovish", direction="bearish_aud",
        category="policy_signal", importance=0.7, evidence_score=0.7,
    )
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 1
    assert result.reportable_conflict_count == 1
    print("  valid policy conflict counted           OK")


# ── Test 6: Market driver conflict counted when eligible ─────────────────────

def test_market_driver_conflict_counted_when_eligible() -> None:
    """MarketDriversAgent findings with good scores generate reportable conflicts."""
    fa = _make_finding(
        finding_id="md-copper-down", direction="bearish_aud",
        category="market_driver", importance=0.7, evidence_score=0.85,
    )
    fb = _make_finding(
        finding_id="news-bullish", direction="bullish_aud",
        category="market_driver", importance=0.65, evidence_score=0.6,
    )
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 1
    assert result.reportable_conflict_count == 1
    print("  market driver conflict counted          OK")


# ── Test 7: ConflictSummary has reportable fields ────────────────────────────

def test_conflict_summary_has_reportable_fields() -> None:
    """ConflictSummary exposes raw, reportable, excluded counts."""
    summary = ConflictSummary(
        conflicts=[],
        conflict_count=3,
        raw_conflict_count=5,
        excluded_conflict_count=2,
    )
    d = summary.to_dict()
    assert d["reportable_conflict_count"] == 3
    assert d["raw_conflict_count"] == 5
    assert d["excluded_conflict_count"] == 2
    print("  ConflictSummary reportable fields       OK")


# ── Test 8: Followup router uses reportable count ────────────────────────────

def test_followup_router_uses_reportable_count() -> None:
    """_conflict_count prefers reportable_conflict_count."""
    # Dict with reportable=1 but raw conflict_count=6
    data = {"conflict_count": 6, "reportable_conflict_count": 1}
    assert _conflict_count(data) == 1

    # Object with reportable attribute
    class FakeSummary:
        reportable_conflict_count = 1
        conflict_count = 6
    assert _conflict_count(FakeSummary()) == 1

    # Fallback to conflict_count when no reportable
    data2 = {"conflict_count": 3}
    assert _conflict_count(data2) == 3
    print("  followup router uses reportable count   OK")


# ── Test 9: evidence_eval deduplicates across sections ───────────────────────

def test_evidence_eval_deduplicates_across_sections() -> None:
    """Same conflict pair in 2 sections should be counted once."""
    pair = {
        "finding_id_a": "f1",
        "finding_id_b": "f2",
        "rule": "same_category_opposite_direction",
    }
    trace1 = RetrievalTrace(
        query="section=汇率",
        retrieved_count=3,
        total_chunks=10,
        conflict_count=1,
        conflict_pairs=[pair],
    )
    trace2 = RetrievalTrace(
        query="section=宏观",
        retrieved_count=2,
        total_chunks=10,
        conflict_count=1,
        conflict_pairs=[pair],
    )
    result = summarize_retrieval_traces([trace1, trace2])
    assert result["reportable_conflict_count"] == 1, f"Got {result['reportable_conflict_count']}"
    assert result["raw_conflict_count"] == 2
    print("  evidence_eval dedup across sections     OK")


# ── Test 10: Ineligible chunk IDs skip boost ─────────────────────────────────

def test_ineligible_chunk_ids_skip_boost() -> None:
    """Chunks marked ineligible receive no boost even if in conflicting set."""
    summary = ConflictSummary(
        conflicts=[ConflictPair(
            finding_id_a="f1", finding_id_b="f2",
            chunk_id_a="c-weak", chunk_id_b="c-strong",
            rule="shared_entity_opposite_direction",
        )],
        conflict_count=1,
        conflicting_chunk_ids={"c-weak", "c-strong"},
    )
    scores = {"c-weak": 0.35, "c-strong": 0.55}
    apply_conflict_boost(scores, summary, ineligible_chunk_ids={"c-weak"})
    assert scores["c-weak"] == 0.35
    assert scores["c-strong"] == round(0.55 + CONFLICT_BOOST, 4)
    print("  ineligible chunk IDs skip boost         OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_duplicate_conflict_pair_counted_once,
        test_low_confidence_news_no_conflict_boost,
        test_aggregator_low_conf_news_selectable_no_boost,
        test_reportable_count_1_no_high_conflict_followup,
        test_valid_policy_conflict_counted,
        test_market_driver_conflict_counted_when_eligible,
        test_conflict_summary_has_reportable_fields,
        test_followup_router_uses_reportable_count,
        test_evidence_eval_deduplicates_across_sections,
        test_ineligible_chunk_ids_skip_boost,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6E-fix2 Conflict Consistency — {len(tests)} tests")
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
