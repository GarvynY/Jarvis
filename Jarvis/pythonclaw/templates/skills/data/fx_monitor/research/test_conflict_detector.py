#!/usr/bin/env python3
"""
Phase 10C — Conflict Detector tests.

Tests:
  1.  test_bullish_vs_bearish_same_category — opposite directions detected
  2.  test_same_direction_no_conflict       — both bullish → no conflict
  3.  test_neutral_ignored                  — neutral/mixed/unknown skipped
  4.  test_different_category_no_conflict   — opposite dirs, different category → no conflict
  5.  test_shared_entity_conflict           — entity overlap + opposite direction
  6.  test_no_entity_overlap_no_conflict    — opposite dirs, no shared entity, diff category
  7.  test_empty_findings                   — 0 findings → empty summary
  8.  test_single_finding                   — 1 finding → no pair possible
  9.  test_multiple_conflicts               — 3+ findings with 2 conflict pairs
  10. test_conflict_pair_to_dict            — JSON serialization
  11. test_conflict_summary_to_dict         — JSON serialization
  12. test_apply_conflict_boost             — score boosted for conflicting chunks
  13. test_boost_no_conflicts               — no conflicts → scores unchanged
  14. test_boost_clamped_to_1               — boosted score clamped to 1.0
  15. test_conflicting_chunk_ids_collected  — summary.conflicting_chunk_ids correct
  16. test_duplicate_pair_not_repeated      — same pair not detected twice
  17. test_direction_none_skipped           — direction=None findings skipped
  18. test_mixed_rules_both_fire            — category + entity rules in one run

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_conflict_detector.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import EvidenceFinding  # noqa: E402
from conflict_detector import (  # noqa: E402
    ConflictPair,
    ConflictSummary,
    detect_conflicts,
    apply_conflict_boost,
    CONFLICT_BOOST,
    _are_opposed,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_finding(
    *,
    finding_id: str = "find-1",
    category: str = "fx_price",
    direction: str | None = "bullish_aud",
    chunk_ids: list[str] | None = None,
    importance: float = 0.5,
) -> EvidenceFinding:
    return EvidenceFinding(
        finding_id=finding_id,
        agent_name="fx_agent",
        key="test_key",
        summary="test summary",
        direction=direction,
        chunk_ids=chunk_ids or [f"chunk-{finding_id}"],
        category=category,
        importance=importance,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

def test_bullish_vs_bearish_same_category() -> None:
    fa = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price")
    fb = _make_finding(finding_id="f2", direction="bearish_aud", category="fx_price")
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 1
    cp = result.conflicts[0]
    assert cp.rule == "same_category_opposite_direction"
    assert cp.direction_a == "bullish_aud"
    assert cp.direction_b == "bearish_aud"
    assert cp.confidence == 0.9
    print("  bullish vs bearish same category  OK")


def test_same_direction_no_conflict() -> None:
    fa = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price")
    fb = _make_finding(finding_id="f2", direction="bullish_aud", category="fx_price")
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 0
    print("  same direction → no conflict      OK")


def test_neutral_ignored() -> None:
    for d in ("neutral", "mixed", "unknown"):
        fa = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price")
        fb = _make_finding(finding_id="f2", direction=d, category="fx_price")
        result = detect_conflicts([fa, fb])
        assert result.conflict_count == 0, f"direction={d} should be ignored"
    print("  neutral/mixed/unknown ignored      OK")


def test_different_category_no_conflict() -> None:
    fa = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price")
    fb = _make_finding(finding_id="f2", direction="bearish_aud", category="macro")
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 0
    print("  different category → no conflict   OK")


def test_shared_entity_conflict() -> None:
    fa = _make_finding(
        finding_id="f1", direction="bullish_aud", category="fx_price",
        chunk_ids=["chunk-a"],
    )
    fb = _make_finding(
        finding_id="f2", direction="bearish_aud", category="macro",
        chunk_ids=["chunk-b"],
    )
    entities = {"chunk-a": ["AUD", "CNY"], "chunk-b": ["AUD", "USD"]}
    result = detect_conflicts([fa, fb], chunk_entities=entities)
    assert result.conflict_count == 1
    cp = result.conflicts[0]
    assert cp.rule == "shared_entity_opposite_direction"
    assert cp.confidence == 0.7
    print("  shared entity conflict             OK")


def test_no_entity_overlap_no_conflict() -> None:
    fa = _make_finding(
        finding_id="f1", direction="bullish_aud", category="fx_price",
        chunk_ids=["chunk-a"],
    )
    fb = _make_finding(
        finding_id="f2", direction="bearish_aud", category="macro",
        chunk_ids=["chunk-b"],
    )
    entities = {"chunk-a": ["CNY"], "chunk-b": ["USD"]}
    result = detect_conflicts([fa, fb], chunk_entities=entities)
    assert result.conflict_count == 0
    print("  no entity overlap → no conflict    OK")


def test_empty_findings() -> None:
    result = detect_conflicts([])
    assert result.conflict_count == 0
    assert result.conflicts == []
    print("  empty findings → empty summary     OK")


def test_single_finding() -> None:
    fa = _make_finding(finding_id="f1", direction="bullish_aud")
    result = detect_conflicts([fa])
    assert result.conflict_count == 0
    print("  single finding → no conflict       OK")


def test_multiple_conflicts() -> None:
    f1 = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price")
    f2 = _make_finding(finding_id="f2", direction="bearish_aud", category="fx_price")
    f3 = _make_finding(finding_id="f3", direction="bearish_aud", category="fx_price")
    result = detect_conflicts([f1, f2, f3])
    assert result.conflict_count == 2, f"Expected 2, got {result.conflict_count}"
    print("  multiple conflicts detected        OK")


def test_conflict_pair_to_dict() -> None:
    cp = ConflictPair(
        finding_id_a="f1", finding_id_b="f2",
        chunk_id_a="c1", chunk_id_b="c2",
        category="fx_price",
        direction_a="bullish_aud", direction_b="bearish_aud",
        rule="same_category_opposite_direction",
        confidence=0.9,
    )
    d = cp.to_dict()
    assert d["finding_id_a"] == "f1"
    assert d["rule"] == "same_category_opposite_direction"
    import json
    json.dumps(d)
    print("  ConflictPair.to_dict() JSON-safe   OK")


def test_conflict_summary_to_dict() -> None:
    cs = ConflictSummary(
        conflicts=[],
        conflict_count=0,
        conflicting_chunk_ids={"c1", "c2"},
    )
    d = cs.to_dict()
    assert isinstance(d["conflicting_chunk_ids"], list)
    assert sorted(d["conflicting_chunk_ids"]) == ["c1", "c2"]
    import json
    json.dumps(d)
    print("  ConflictSummary.to_dict() JSON-safe OK")


def test_apply_conflict_boost() -> None:
    score_map = {"c1": 0.6, "c2": 0.5, "c3": 0.7}
    summary = ConflictSummary(conflicting_chunk_ids={"c1", "c2"})
    result = apply_conflict_boost(score_map, summary)
    assert result["c1"] == round(0.6 + CONFLICT_BOOST, 4)
    assert result["c2"] == round(0.5 + CONFLICT_BOOST, 4)
    assert result["c3"] == 0.7
    print("  apply_conflict_boost               OK")


def test_boost_no_conflicts() -> None:
    score_map = {"c1": 0.6, "c2": 0.5}
    summary = ConflictSummary()
    result = apply_conflict_boost(score_map, summary)
    assert result == {"c1": 0.6, "c2": 0.5}
    print("  boost no conflicts → unchanged     OK")


def test_boost_clamped_to_1() -> None:
    score_map = {"c1": 0.95}
    summary = ConflictSummary(conflicting_chunk_ids={"c1"})
    result = apply_conflict_boost(score_map, summary)
    assert result["c1"] == 1.0
    print("  boost clamped to 1.0               OK")


def test_conflicting_chunk_ids_collected() -> None:
    fa = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price",
                       chunk_ids=["chunk-aa"])
    fb = _make_finding(finding_id="f2", direction="bearish_aud", category="fx_price",
                       chunk_ids=["chunk-bb"])
    result = detect_conflicts([fa, fb])
    assert "chunk-aa" in result.conflicting_chunk_ids
    assert "chunk-bb" in result.conflicting_chunk_ids
    print("  conflicting_chunk_ids collected     OK")


def test_duplicate_pair_not_repeated() -> None:
    fa = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price",
                       chunk_ids=["c1"])
    fb = _make_finding(finding_id="f2", direction="bearish_aud", category="fx_price",
                       chunk_ids=["c2"])
    entities = {"c1": ["AUD"], "c2": ["AUD"]}
    result = detect_conflicts([fa, fb], chunk_entities=entities)
    assert result.conflict_count == 1, "Same pair should not be detected by both rules"
    assert result.conflicts[0].rule == "same_category_opposite_direction"
    print("  duplicate pair not repeated         OK")


def test_direction_none_skipped() -> None:
    fa = _make_finding(finding_id="f1", direction=None, category="fx_price")
    fb = _make_finding(finding_id="f2", direction="bearish_aud", category="fx_price")
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 0
    print("  direction=None skipped              OK")


def test_mixed_rules_both_fire() -> None:
    f1 = _make_finding(finding_id="f1", direction="bullish_aud", category="fx_price",
                       chunk_ids=["c1"])
    f2 = _make_finding(finding_id="f2", direction="bearish_aud", category="fx_price",
                       chunk_ids=["c2"])
    f3 = _make_finding(finding_id="f3", direction="bullish_aud", category="macro",
                       chunk_ids=["c3"])
    f4 = _make_finding(finding_id="f4", direction="bearish_aud", category="news_event",
                       chunk_ids=["c4"])
    entities = {"c3": ["AUD", "RBA"], "c4": ["AUD", "RBA"]}
    result = detect_conflicts([f1, f2, f3, f4], chunk_entities=entities)
    rules = {c.rule for c in result.conflicts}
    assert "same_category_opposite_direction" in rules
    assert "shared_entity_opposite_direction" in rules
    assert result.conflict_count == 2
    print("  mixed rules both fire              OK")


# ── Runner ───────────────────────────────────────────────────────────────────

def main() -> None:
    print("Phase 10C -- Conflict Detector tests")
    print("=" * 60)

    test_bullish_vs_bearish_same_category()
    test_same_direction_no_conflict()
    test_neutral_ignored()
    test_different_category_no_conflict()
    test_shared_entity_conflict()
    test_no_entity_overlap_no_conflict()
    test_empty_findings()
    test_single_finding()
    test_multiple_conflicts()
    test_conflict_pair_to_dict()
    test_conflict_summary_to_dict()
    test_apply_conflict_boost()
    test_boost_no_conflicts()
    test_boost_clamped_to_1()
    test_conflicting_chunk_ids_collected()
    test_duplicate_pair_not_repeated()
    test_direction_none_skipped()
    test_mixed_rules_both_fire()

    print("\n" + "=" * 60)
    print("All 18 tests passed.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
