#!/usr/bin/env python3
"""
Phase 10A — Evidence Scorer MVP tests.

Tests:
  1.  test_high_importance_confidence  — high imp+conf → high composite
  2.  test_low_importance_confidence   — low values → low composite
  3.  test_stale_chunk_lower           — 48h old chunk scores lower on recency
  4.  test_fresh_chunk_high_recency    — just-created chunk → recency ≈ 1.0
  5.  test_official_source_high        — rba.gov source → 0.95
  6.  test_reputable_source_medium     — reuters → 0.82
  7.  test_unknown_source_low          — random URL → 0.4
  8.  test_empty_source_lowest         — None source → 0.2
  9.  test_user_topic_match_boost      — matching category → 0.8
  10. test_user_topic_entity_match     — matching entity → 0.8
  11. test_user_no_context_neutral     — no SafeUserContext → 0.3
  12. test_invalid_timestamp_no_crash  — garbage timestamp → 0.5 recency
  13. test_empty_timestamp_no_crash    — empty string → 0.5 recency
  14. test_all_scores_clamped          — importance > 1.0 clamped
  15. test_composite_formula           — verify weighted sum
  16. test_fallback_score              — fallback uses imp+conf only
  17. test_evidence_score_to_dict      — JSON serialization
  18. test_reason_tags                 — reason string contains expected tags
  19. test_score_breakdown_fields      — ScoreBreakdown dataclass
  20. test_zero_chunk_baseline         — all-zero chunk → deterministic baseline
  21. test_rba_domain_beats_provider_label
  22. test_reuters_beats_marketpulse
  23. test_crypto_sources_low_for_fx_macro
  24. test_missing_url_title_no_crash
  25. test_conflict_value_in_composite
  26. test_conflict_value_invalid_safe

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_evidence_scorer.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import EvidenceChunk, SafeUserContext, now_iso  # noqa: E402
from evidence_scorer import (  # noqa: E402
    EvidenceScore,
    ScoreBreakdown,
    compute_recency_score,
    compute_source_quality_score,
    compute_user_relevance_score,
    compute_evidence_score,
    fallback_score,
    W_IMPORTANCE,
    W_CONFIDENCE,
    W_RECENCY,
    W_SOURCE_QUALITY,
    W_USER_RELEVANCE,
    W_CONFLICT,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_chunk(
    *,
    chunk_id: str = "chunk-test-1",
    importance: float = 0.5,
    confidence: float = 0.5,
    source: str | None = None,
    category: str = "",
    entities: list[str] | None = None,
    created_at: str | None = None,
    content: str = "test content",
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id,
        task_id="task-test",
        agent_name="fx_agent",
        content=content,
        source=source,
        category=category,
        importance=importance,
        confidence=confidence,
        entities=entities or [],
        created_at=created_at or now_iso(),
    )


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hours_ago(h: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=h)
    return dt.isoformat(timespec="seconds")


# ── Tests ────────────────────────────────────────────────────────────────────

def test_high_importance_confidence() -> None:
    chunk = _make_chunk(importance=0.9, confidence=0.9, source="https://rba.gov.au/data")
    score = compute_evidence_score(chunk)
    assert score.composite_score >= 0.7, f"Expected ≥0.7, got {score.composite_score}"
    assert score.importance == 0.9
    assert score.confidence == 0.9
    print("  high imp+conf → high composite  OK")


def test_low_importance_confidence() -> None:
    chunk = _make_chunk(importance=0.1, confidence=0.1, source=None)
    score = compute_evidence_score(chunk)
    assert score.composite_score <= 0.35, f"Expected ≤0.35, got {score.composite_score}"
    print("  low imp+conf → low composite    OK")


def test_stale_chunk_lower() -> None:
    fresh = _make_chunk(importance=0.7, confidence=0.7, created_at=_now_str())
    stale = _make_chunk(importance=0.7, confidence=0.7, created_at=_hours_ago(48))
    now = _now_str()
    s_fresh = compute_evidence_score(fresh, now_iso_str=now)
    s_stale = compute_evidence_score(stale, now_iso_str=now)
    assert s_fresh.recency_score > s_stale.recency_score, (
        f"fresh recency {s_fresh.recency_score} should > stale {s_stale.recency_score}"
    )
    assert s_fresh.composite_score > s_stale.composite_score
    print(f"  stale chunk lower (fresh={s_fresh.recency_score:.3f} > stale={s_stale.recency_score:.3f})  OK")


def test_fresh_chunk_high_recency() -> None:
    chunk = _make_chunk(created_at=_now_str())
    rec = compute_recency_score(chunk, now_iso_str=_now_str())
    assert rec >= 0.95, f"Fresh chunk recency should be ≥0.95, got {rec}"
    print(f"  fresh chunk recency={rec:.4f}    OK")


def test_official_source_high() -> None:
    chunk = _make_chunk(source="https://rba.gov.au/statistics/tables")
    sq = compute_source_quality_score(chunk)
    assert sq == 0.95, f"Official source should be 0.95, got {sq}"
    print("  official source → 0.95          OK")


def test_reputable_source_medium() -> None:
    chunk = _make_chunk(source="https://www.reuters.com/article/aud")
    sq = compute_source_quality_score(chunk)
    assert sq == 0.82, f"Reuters should be 0.82, got {sq}"
    print("  reputable source → 0.82         OK")


def test_unknown_source_low() -> None:
    chunk = _make_chunk(source="https://random-blog.xyz/post/123")
    sq = compute_source_quality_score(chunk)
    assert sq == 0.4, f"Unknown source should be 0.4, got {sq}"
    print("  unknown source → 0.4            OK")


def test_empty_source_lowest() -> None:
    for src in (None, "", "  "):
        chunk = _make_chunk(source=src)
        sq = compute_source_quality_score(chunk)
        assert sq == 0.2, f"Empty source ({src!r}) should be 0.2, got {sq}"
    print("  empty source → 0.2              OK")


def test_user_topic_match_boost() -> None:
    ctx = SafeUserContext(preferred_topics=["rba", "fx_price"])
    chunk = _make_chunk(category="fx_price")
    ur = compute_user_relevance_score(chunk, ctx)
    assert ur >= 0.8, f"Matching category should be at least 0.8, got {ur}"
    print("  user topic category match ≥ 0.8 OK")


def test_user_topic_entity_match() -> None:
    ctx = SafeUserContext(preferred_topics=["CNY/AUD", "iron_ore"])
    chunk = _make_chunk(entities=["CNY/AUD", "USD"])
    ur = compute_user_relevance_score(chunk, ctx)
    assert ur >= 0.8, f"Matching entity should be at least 0.8, got {ur}"
    print("  user topic entity match ≥ 0.8   OK")


def test_user_no_context_neutral() -> None:
    chunk = _make_chunk(category="fx_price")
    ur = compute_user_relevance_score(chunk, None)
    assert ur == 0.3, f"No context should be 0.3, got {ur}"
    ur2 = compute_user_relevance_score(chunk, SafeUserContext())
    assert ur2 == 0.3, f"Empty prefs should be 0.3, got {ur2}"
    print("  no user context → 0.3           OK")


def test_positive_category_feedback_boosts_relevance() -> None:
    chunk = _make_chunk(category="macro")
    ur = compute_user_relevance_score(
        chunk,
        None,
        category_feedback_summary={"macro": 0.75},
    )
    assert ur == 0.8, f"Positive category feedback should boost to 0.8, got {ur}"
    score = compute_evidence_score(
        chunk,
        now_iso_str=_now_str(),
        category_feedback_summary={"macro": 0.75},
    )
    assert score.user_relevance_score == 0.8
    print("  positive category feedback boost OK")


def test_negative_category_feedback_lowers_relevance() -> None:
    chunk = _make_chunk(category="macro")
    ctx = SafeUserContext(preferred_topics=["macro"])
    ur = compute_user_relevance_score(
        chunk,
        ctx,
        category_feedback_summary={"macro": -1.0},
    )
    assert ur == 0.1, f"Negative category feedback should lower to 0.1, got {ur}"
    print("  negative category feedback lowers OK")


def test_news_tag_feedback_maps_to_evidence_categories() -> None:
    ctx = SafeUserContext()
    setattr(ctx, "category_feedback_summary", {"news_tag": 1.0})
    news = _make_chunk(category="news_event")
    macro = _make_chunk(category="macro")
    risk = _make_chunk(category="risk")
    fx = _make_chunk(category="fx_price")

    assert compute_user_relevance_score(news, ctx) == 0.84
    assert compute_user_relevance_score(macro, ctx) == 0.78
    assert compute_user_relevance_score(risk, ctx) == 0.74
    assert compute_user_relevance_score(fx, ctx) == 0.64
    print("  news_tag feedback maps categories OK")


def test_inferred_interest_topics_map_to_macro_news_risk() -> None:
    ctx = SafeUserContext()
    setattr(ctx, "inferred_high_interest_topics", ["地缘政治风险", "能源价格"])
    chunk = _make_chunk(
        category="macro",
        content="OPEC and Hormuz disruption may affect oil prices, inflation and AUD.",
    )
    ur = compute_user_relevance_score(chunk, ctx)
    assert ur >= 0.86, f"Energy/geopolitical inferred topics should boost macro, got {ur}"
    score = compute_evidence_score(chunk, ctx, now_iso_str=_now_str())
    assert score.user_relevance_score == ur
    print("  inferred topic mapping boost OK")


def test_quality_dislike_penalizes_shallow_chunks() -> None:
    ctx = SafeUserContext()
    setattr(ctx, "inferred_high_interest_topics", ["澳元走势"])
    setattr(ctx, "inferred_low_interest_topics", ["逻辑太浅"])
    chunk = _make_chunk(
        category="news_event",
        content="Aussie Dollar fatigue? Technical signs hint at an AUD/USD pullback.",
        source="https://marketpulse.com/aud-usd-pullback",
    )
    ur = compute_user_relevance_score(
        chunk,
        ctx,
        category_feedback_summary={"news_article_quality": -1.0},
    )
    assert 0.35 <= ur <= 0.6, f"Shallow quality dislike should penalize, got {ur}"
    print("  quality dislike penalty OK")


def test_explicit_fx_use_and_bank_preferences_boost_relevance() -> None:
    ctx = SafeUserContext(purpose="living", target_rate=4.78)
    setattr(ctx, "preferred_banks", ["中国银行"])
    fx = _make_chunk(
        category="fx_price",
        content="中国银行 AUD 现汇卖出价为 4.9347。",
        source="Chinese bank FX boards",
    )
    macro = _make_chunk(category="macro", content="General monetary policy update.")

    assert compute_user_relevance_score(fx, ctx) >= 0.75
    assert compute_user_relevance_score(macro, ctx) == 0.3
    print("  explicit FX use/bank preference boost OK")


def test_invalid_timestamp_no_crash() -> None:
    chunk = _make_chunk(created_at="not-a-date")
    rec = compute_recency_score(chunk)
    assert rec == 0.5, f"Invalid timestamp should be 0.5, got {rec}"
    score = compute_evidence_score(chunk)
    assert 0.0 <= score.composite_score <= 1.0
    print("  invalid timestamp → 0.5, no crash  OK")


def test_empty_timestamp_no_crash() -> None:
    chunk = _make_chunk()
    chunk.created_at = ""  # force empty after construction
    rec = compute_recency_score(chunk)
    assert rec == 0.5, f"Empty timestamp should be 0.5, got {rec}"
    print("  empty timestamp → 0.5           OK")


def test_all_scores_clamped() -> None:
    chunk = _make_chunk(importance=0.99, confidence=0.99)
    score = compute_evidence_score(chunk)
    assert 0.0 <= score.composite_score <= 1.0
    assert 0.0 <= score.recency_score <= 1.0
    assert 0.0 <= score.source_quality_score <= 1.0
    assert 0.0 <= score.user_relevance_score <= 1.0
    assert 0.0 <= score.importance <= 1.0
    assert 0.0 <= score.confidence <= 1.0

    over = _make_chunk(importance=0.5, confidence=0.5)
    over.importance = 1.5
    over.confidence = 2.0
    s2 = compute_evidence_score(over)
    assert s2.importance == 1.0, f"importance should be clamped to 1.0, got {s2.importance}"
    assert s2.confidence == 1.0, f"confidence should be clamped to 1.0, got {s2.confidence}"
    assert 0.0 <= s2.composite_score <= 1.0
    print("  all scores clamped [0,1]        OK")


def test_composite_formula() -> None:
    """Verify the composite matches the documented weighted sum."""
    chunk = _make_chunk(
        importance=0.8,
        confidence=0.6,
        source="https://rba.gov.au/data",
        category="macro",
        created_at=_now_str(),
    )
    ctx = SafeUserContext(preferred_topics=["macro"])
    now = _now_str()
    score = compute_evidence_score(chunk, ctx, now_iso_str=now)

    expected = (
        0.8 * W_IMPORTANCE
        + 0.6 * W_CONFIDENCE
        + score.recency_score * W_RECENCY
        + 0.95 * W_SOURCE_QUALITY
        + score.user_relevance_score * W_USER_RELEVANCE
        + 0.0 * W_CONFLICT
    )
    expected = max(0.0, min(1.0, round(expected, 4)))
    assert abs(score.composite_score - expected) < 0.001, (
        f"Expected {expected}, got {score.composite_score}"
    )
    print(f"  composite formula verified ({score.composite_score:.4f})  OK")


def test_fallback_score() -> None:
    chunk = _make_chunk(importance=0.7, confidence=0.5)
    fb = fallback_score(chunk)
    expected = round(0.7 * 0.6 + 0.5 * 0.4, 4)
    assert abs(fb.composite_score - expected) < 0.001, (
        f"Fallback expected {expected}, got {fb.composite_score}"
    )
    assert fb.reason == "fallback"
    assert fb.recency_score == 0.5
    assert fb.source_quality_score == 0.4
    assert fb.user_relevance_score == 0.3
    print(f"  fallback_score({expected})          OK")


def test_evidence_score_to_dict() -> None:
    chunk = _make_chunk(importance=0.6, confidence=0.4)
    score = compute_evidence_score(chunk)
    d = score.to_dict()
    assert isinstance(d, dict)
    assert d["chunk_id"] == "chunk-test-1"
    assert isinstance(d["composite_score"], float)
    assert "reason" in d
    assert "conflict_value" in d
    import json
    json.dumps(d)
    print("  to_dict() JSON-safe             OK")


def test_reason_tags() -> None:
    # High importance + official source + fresh
    chunk = _make_chunk(
        importance=0.9,
        confidence=0.9,
        source="https://rba.gov.au",
        created_at=_now_str(),
    )
    score = compute_evidence_score(chunk, now_iso_str=_now_str())
    assert "high_imp" in score.reason, f"Expected 'high_imp' in {score.reason}"
    assert "high_conf" in score.reason
    assert "official" in score.reason
    assert "fresh" in score.reason

    # Low everything
    stale = _make_chunk(importance=0.1, confidence=0.1, created_at=_hours_ago(72))
    s2 = compute_evidence_score(stale, now_iso_str=_now_str())
    assert "stale" in s2.reason, f"Expected 'stale' in {s2.reason}"
    print("  reason tags correct             OK")


def test_score_breakdown_fields() -> None:
    sb = ScoreBreakdown(
        importance=0.8,
        confidence=0.6,
        recency_score=0.9,
        source_quality_score=0.7,
        user_relevance_score=0.3,
        conflict_value=0.2,
    )
    assert sb.importance == 0.8
    assert sb.confidence == 0.6
    assert sb.recency_score == 0.9
    assert sb.source_quality_score == 0.7
    assert sb.user_relevance_score == 0.3
    assert sb.conflict_value == 0.2
    print("  ScoreBreakdown fields           OK")


def test_zero_chunk_baseline() -> None:
    chunk = _make_chunk(importance=0.0, confidence=0.0, source=None)
    chunk.created_at = ""  # force empty to get neutral recency
    score = compute_evidence_score(chunk)
    assert score.composite_score >= 0.0
    assert score.composite_score <= 0.5
    assert score.importance == 0.0
    assert score.confidence == 0.0
    expected_baseline = round(
        0.0 * W_IMPORTANCE
        + 0.0 * W_CONFIDENCE
        + 0.5 * W_RECENCY
        + 0.2 * W_SOURCE_QUALITY
        + 0.3 * W_USER_RELEVANCE
        + 0.0 * W_CONFLICT,
        4,
    )
    assert abs(score.composite_score - expected_baseline) < 0.001, (
        f"Zero baseline expected {expected_baseline}, got {score.composite_score}"
    )
    print(f"  zero chunk baseline={expected_baseline}     OK")


def test_rba_domain_beats_provider_label() -> None:
    rba = _make_chunk(
        source=(
            "url=https://www.rba.gov.au/media-releases/2026/mr-26-01.html"
            " | title=Statement by the Monetary Policy Board"
            " | provider=google_news_rss"
        )
    )
    provider_only = _make_chunk(source="google_news_rss")
    assert compute_source_quality_score(rba) > compute_source_quality_score(provider_only)
    print("  rba.gov.au beats provider label OK")


def test_reuters_beats_marketpulse() -> None:
    reuters = _make_chunk(source="url=https://www.reuters.com/markets/currencies/aud-cny | provider=google_news_rss")
    marketpulse = _make_chunk(source="url=https://www.marketpulse.com/forex/aud-usd-pullback | provider=google_news_rss")
    assert compute_source_quality_score(reuters) > compute_source_quality_score(marketpulse)
    print("  Reuters beats MarketPulse       OK")


def test_crypto_sources_low_for_fx_macro() -> None:
    binance = _make_chunk(source="url=https://www.binance.com/en/news/aud-usd | title=AUD macro outlook")
    cryptorank = _make_chunk(source="url=https://cryptorank.io/news/pboc-yuan | title=PBoC strategic pause")
    assert compute_source_quality_score(binance) <= 0.45
    assert compute_source_quality_score(cryptorank) <= 0.45
    print("  crypto sources low for FX macro OK")


def test_missing_url_title_no_crash() -> None:
    for src in (None, "", "google_news_rss"):
        chunk = _make_chunk(source=src)
        sq = compute_source_quality_score(chunk)
        assert 0.0 <= sq <= 1.0
    assert compute_source_quality_score(_make_chunk(source="google_news_rss")) == 0.4
    print("  missing URL/title no crash      OK")


def test_conflict_value_in_composite() -> None:
    chunk = _make_chunk(
        importance=0.4,
        confidence=0.5,
        source="https://www.reuters.com/markets/currencies/aud-cny",
        category="macro",
        created_at=_now_str(),
    )
    now = _now_str()
    base = compute_evidence_score(chunk, now_iso_str=now, conflict_value=0.0)
    conflicted = compute_evidence_score(chunk, now_iso_str=now, conflict_value=1.0)
    expected_delta = W_CONFLICT
    actual_delta = round(conflicted.composite_score - base.composite_score, 4)
    assert conflicted.conflict_value == 1.0
    assert abs(actual_delta - expected_delta) < 0.001, (
        f"Expected conflict contribution {expected_delta}, got {actual_delta}"
    )
    print("  conflict value enters composite OK")


def test_conflict_value_invalid_safe() -> None:
    chunk = _make_chunk()
    chunk.conflict_value = None  # type: ignore[attr-defined]
    none_score = compute_evidence_score(chunk, now_iso_str=_now_str())
    assert none_score.conflict_value == 0.0

    chunk.conflict_value = "bad-value"  # type: ignore[attr-defined]
    bad_score = compute_evidence_score(chunk, now_iso_str=_now_str())
    assert bad_score.conflict_value == 0.0

    chunk.conflict_value = ""  # type: ignore[attr-defined]
    empty_score = compute_evidence_score(chunk, now_iso_str=_now_str())
    assert empty_score.conflict_value == 0.0
    print("  invalid conflict value safe      OK")


# ── Runner ───────────────────────────────────────────────────────────────────

def main() -> None:
    print("Phase 10A -- Evidence Scorer MVP tests")
    print("=" * 60)

    test_high_importance_confidence()
    test_low_importance_confidence()
    test_stale_chunk_lower()
    test_fresh_chunk_high_recency()
    test_official_source_high()
    test_reputable_source_medium()
    test_unknown_source_low()
    test_empty_source_lowest()
    test_user_topic_match_boost()
    test_user_topic_entity_match()
    test_user_no_context_neutral()
    test_positive_category_feedback_boosts_relevance()
    test_negative_category_feedback_lowers_relevance()
    test_news_tag_feedback_maps_to_evidence_categories()
    test_inferred_interest_topics_map_to_macro_news_risk()
    test_quality_dislike_penalizes_shallow_chunks()
    test_explicit_fx_use_and_bank_preferences_boost_relevance()
    test_invalid_timestamp_no_crash()
    test_empty_timestamp_no_crash()
    test_all_scores_clamped()
    test_composite_formula()
    test_fallback_score()
    test_evidence_score_to_dict()
    test_reason_tags()
    test_score_breakdown_fields()
    test_zero_chunk_baseline()
    test_rba_domain_beats_provider_label()
    test_reuters_beats_marketpulse()
    test_crypto_sources_low_for_fx_macro()
    test_missing_url_title_no_crash()
    test_conflict_value_in_composite()
    test_conflict_value_invalid_safe()

    print("\n" + "=" * 60)
    print("All 32 tests passed.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
