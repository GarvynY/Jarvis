#!/usr/bin/env python3
"""
Phase 10.6E Quality Fixes — Tests.

Tests:
  1.  test_conflict_dedup_same_pair_one_count
  2.  test_low_confidence_news_not_conflict_eligible
  3.  test_high_confidence_news_conflict_eligible
  4.  test_market_driver_conflict_still_works
  5.  test_fx_target_rate_gap_high_relevance_living
  6.  test_fx_target_rate_gap_no_boost_without_purpose
  7.  test_open_er_api_tier3_market_data
  8.  test_generic_news_relevance_capped
  9.  test_direct_fx_news_relevance_not_capped
  10. test_conflict_dedup_key_deterministic

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106e_quality_fixes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import EvidenceChunk, EvidenceFinding, SafeUserContext  # noqa: E402
from conflict_detector import (  # noqa: E402
    ConflictPair,
    detect_conflicts,
    _is_conflict_eligible,
    _dedup_key,
)
from source_metadata import (  # noqa: E402
    SourceMetadata,
    infer_source_type_and_tier,
    normalize_domain,
)
from evidence_scorer import (  # noqa: E402
    compute_user_relevance_score,
    compute_source_quality_score,
)


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


def _make_chunk(
    *,
    chunk_id: str = "c1",
    category: str = "fx_price",
    source: str = "",
    importance: float = 0.5,
    confidence: float = 0.7,
    content: str = "",
    entities: list[str] | None = None,
    source_metadata_json: str = "{}",
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id,
        task_id="t1",
        agent_name="test",
        content=content or f"test content {chunk_id}",
        source=source,
        category=category,
        importance=importance,
        confidence=confidence,
        entities=entities or ["AUD", "CNY"],
        source_metadata_json=source_metadata_json,
    )


# ── Test 1: Conflict dedup ───────────────────────────────────────────────────

def test_conflict_dedup_same_pair_one_count() -> None:
    """Same finding pair via both rules should only be counted once."""
    fa = _make_finding(
        finding_id="f1", direction="bullish_aud",
        category="market_driver", chunk_ids=["c1"],
    )
    fb = _make_finding(
        finding_id="f2", direction="bearish_aud",
        category="market_driver", chunk_ids=["c2"],
    )
    # Both same-category AND shared-entity would match, but only counted once
    result = detect_conflicts(
        [fa, fb],
        chunk_entities={"c1": ["AUD"], "c2": ["AUD"]},
    )
    assert result.conflict_count == 1, f"Expected 1, got {result.conflict_count}"
    print("  conflict dedup: same pair one count     OK")


# ── Test 2: Low-confidence news not eligible ─────────────────────────────────

def test_low_confidence_news_not_conflict_eligible() -> None:
    """Low evidence_score news_event finding cannot produce conflicts."""
    fa = _make_finding(
        finding_id="news-low", direction="bullish_aud",
        category="news_event", importance=0.5, evidence_score=0.2,
    )
    fb = _make_finding(
        finding_id="fx-high", direction="bearish_aud",
        category="news_event", importance=0.7, evidence_score=0.8,
    )
    assert not _is_conflict_eligible(fa)
    assert _is_conflict_eligible(fb)
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 0, f"Expected 0, got {result.conflict_count}"
    print("  low confidence news not eligible        OK")


# ── Test 3: High-confidence news still eligible ──────────────────────────────

def test_high_confidence_news_conflict_eligible() -> None:
    """News with decent evidence_score can still participate in conflicts."""
    fa = _make_finding(
        finding_id="news-good", direction="bullish_aud",
        category="news_event", importance=0.65, evidence_score=0.6,
    )
    fb = _make_finding(
        finding_id="news-good2", direction="bearish_aud",
        category="news_event", importance=0.65, evidence_score=0.5,
    )
    assert _is_conflict_eligible(fa)
    assert _is_conflict_eligible(fb)
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 1
    print("  high confidence news eligible           OK")


# ── Test 4: Market driver conflict still works ───────────────────────────────

def test_market_driver_conflict_still_works() -> None:
    """MarketDriversAgent findings with good scores still generate conflicts."""
    fa = _make_finding(
        finding_id="md-copper", direction="bearish_aud",
        category="market_driver", importance=0.7,
    )
    fb = _make_finding(
        finding_id="fx-rate", direction="bullish_aud",
        category="market_driver", importance=0.6,
    )
    result = detect_conflicts([fa, fb])
    assert result.conflict_count == 1
    assert result.conflicts[0].rule == "same_category_opposite_direction"
    print("  market driver conflict works            OK")


# ── Test 5: FX target_rate_gap gets high relevance ───────────────────────────

def test_fx_target_rate_gap_high_relevance_living() -> None:
    """target_rate_gap chunk gets boosted user_relevance when purpose=living."""
    chunk = _make_chunk(
        chunk_id="trg",
        category="fx_price",
        source="url=https://open.er-api.com/v6/latest/CNY | finding_key=target_rate_gap",
        importance=0.5,
        confidence=0.7,
    )
    ctx = SafeUserContext(purpose="living", target_rate=4.95)
    score = compute_user_relevance_score(chunk, ctx)
    assert score >= 0.85, f"Expected >= 0.85, got {score}"
    print("  target_rate_gap high relevance (living) OK")


# ── Test 6: target_rate_gap no boost without purpose ─────────────────────────

def test_fx_target_rate_gap_no_boost_without_purpose() -> None:
    """target_rate_gap without living/tuition purpose doesn't get special boost."""
    chunk = _make_chunk(
        chunk_id="trg",
        category="fx_price",
        source="url=https://open.er-api.com/v6/latest/CNY | finding_key=target_rate_gap",
    )
    ctx = SafeUserContext(purpose="")
    score = compute_user_relevance_score(chunk, ctx)
    assert score < 0.85, f"Expected < 0.85, got {score}"
    print("  target_rate_gap no boost without purpose OK")


# ── Test 7: open.er-api.com → tier 3 market_data_api ─────────────────────────

def test_open_er_api_tier3_market_data() -> None:
    """open.er-api.com should be classified as market_data_api tier 3."""
    meta = SourceMetadata(
        url="https://open.er-api.com/v6/latest/CNY",
        domain="open.er-api.com",
    )
    result = infer_source_type_and_tier(meta)
    assert result.source_type == "market_data_api", f"Got {result.source_type}"
    assert result.source_tier == 3, f"Got tier {result.source_tier}"
    assert "exchange_rate_api_domain" in result.quality_reason
    print("  open.er-api.com → tier 3 market_data   OK")


# ── Test 8: Generic news relevance capped ────────────────────────────────────

def test_generic_news_relevance_capped() -> None:
    """Generic oil/Middle East news should not get user_relevance > 0.6."""
    chunk = _make_chunk(
        chunk_id="news-generic",
        category="news_event",
        content="Saudi oil production falls amid Hormuz disruption",
        source="url=https://news.google.com/rss/... | provider=google_news_rss",
        entities=["OIL"],
    )
    ctx = SafeUserContext(
        purpose="living",
        preferred_topics=["地缘政治", "能源"],
    )
    score = compute_user_relevance_score(chunk, ctx)
    assert score <= 0.6, f"Expected <= 0.6, got {score}"
    print("  generic news relevance capped           OK")


# ── Test 9: Direct FX news not capped ────────────────────────────────────────

def test_direct_fx_news_relevance_not_capped() -> None:
    """News directly about AUD/RBA should keep high relevance."""
    chunk = _make_chunk(
        chunk_id="news-direct",
        category="news_event",
        content="RBA holds rates steady, Australian dollar rises",
        source="url=https://reuters.com/article/... | provider=reuters",
        entities=["AUD", "RBA"],
    )
    ctx = SafeUserContext(
        purpose="living",
        preferred_topics=["澳元"],
    )
    score = compute_user_relevance_score(chunk, ctx)
    assert score > 0.6, f"Expected > 0.6, got {score}"
    print("  direct FX news not capped               OK")


# ── Test 10: Dedup key is deterministic ──────────────────────────────────────

def test_conflict_dedup_key_deterministic() -> None:
    """Dedup key should be the same regardless of pair ordering."""
    cp1 = ConflictPair(
        finding_id_a="f2", finding_id_b="f1",
        rule="same_category_opposite_direction",
    )
    cp2 = ConflictPair(
        finding_id_a="f1", finding_id_b="f2",
        rule="same_category_opposite_direction",
    )
    assert _dedup_key(cp1) == _dedup_key(cp2)
    print("  dedup key deterministic                 OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_conflict_dedup_same_pair_one_count,
        test_low_confidence_news_not_conflict_eligible,
        test_high_confidence_news_conflict_eligible,
        test_market_driver_conflict_still_works,
        test_fx_target_rate_gap_high_relevance_living,
        test_fx_target_rate_gap_no_boost_without_purpose,
        test_open_er_api_tier3_market_data,
        test_generic_news_relevance_capped,
        test_direct_fx_news_relevance_not_capped,
        test_conflict_dedup_key_deterministic,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6E Quality Fixes — {len(tests)} tests")
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
