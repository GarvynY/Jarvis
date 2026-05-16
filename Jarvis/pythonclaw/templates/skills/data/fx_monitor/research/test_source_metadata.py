#!/usr/bin/env python3
"""
Phase 10.6A — SourceMetadata tests.

Tests:
  1. Serialization/deserialization round-trip
  2. normalize_domain extracts domain from URL
  3. Google News URL flagged as aggregator
  4. Official central bank domain gets tier 1
  5. Reuters gets higher tier than unknown
  6. Legacy source string still works
  7. EvidenceStore stores source_metadata_json
  8. Old database migrates safely
  9. EvidenceScorer prefers structured metadata over legacy
  10. source_debug_info exposes correct fields

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_source_metadata.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from source_metadata import (
    SourceMetadata,
    normalize_domain,
    source_metadata_from_source_ref,
    source_metadata_from_legacy_string,
    infer_source_type_and_tier,
    tier_to_quality_score,
)
from schema import SourceRef, EvidenceChunk, EvidenceFinding, now_iso
from evidence_store import EvidenceStore
from evidence_scorer import compute_source_quality_score


# ── Tests ────────────────────────────────────────────────────────────────────

def test_serialization_roundtrip() -> None:
    """SourceMetadata serializes to JSON and deserializes back identically."""
    meta = SourceMetadata(
        url="https://reuters.com/article/1",
        title="Test Article - Reuters",
        provider="google_news_rss",
        domain="reuters.com",
        publisher="Reuters",
        source_type="mainstream_financial_media",
        source_tier=2,
        published_at="2026-05-16T10:00:00Z",
        retrieved_at="2026-05-16T10:05:00Z",
        is_aggregator=True,
        aggregator_provider="google_news_rss",
        quality_reason="premium_media_domain:reuters.com",
    )
    json_str = meta.to_json()
    restored = SourceMetadata.from_json(json_str)

    assert restored.url == meta.url
    assert restored.domain == meta.domain
    assert restored.source_type == meta.source_type
    assert restored.source_tier == meta.source_tier
    assert restored.is_aggregator == meta.is_aggregator
    assert restored.quality_reason == meta.quality_reason
    assert restored.published_at == meta.published_at

    assert SourceMetadata.from_json("{}").source_type == "unknown"
    assert SourceMetadata.from_json("").source_type == "unknown"
    assert SourceMetadata.from_json("invalid json").source_type == "unknown"

    print("  test_serialization_roundtrip PASS")


def test_from_json_handles_structural_invalid_values() -> None:
    """from_json degrades safely for valid JSON with invalid structure."""
    assert SourceMetadata.from_json("[]").source_type == "unknown"
    assert SourceMetadata.from_json("null").source_type == "unknown"
    assert SourceMetadata.from_json('"bad"').source_type == "unknown"

    bad_tier = SourceMetadata.from_json(
        '{"domain":"example.com","source_type":"general_news","source_tier":"bad"}'
    )
    assert bad_tier.domain == "example.com"
    assert bad_tier.source_type == "general_news"
    assert bad_tier.source_tier == 3

    out_of_range_tier = SourceMetadata.from_json(
        '{"source_type":"general_news","source_tier":99}'
    )
    assert out_of_range_tier.source_tier == 3

    invalid_type = SourceMetadata.from_json(
        '{"source_type":"not_a_real_type","source_tier":2}'
    )
    assert invalid_type.source_type == "unknown"
    assert invalid_type.source_tier == 2

    wrong_field_types = SourceMetadata.from_json(
        '{"domain":["bad"],"provider":42,"published_at":false,"source_tier":2}'
    )
    assert wrong_field_types.domain == ""
    assert wrong_field_types.provider == ""
    assert wrong_field_types.published_at is None
    assert wrong_field_types.source_tier == 2

    print("  test_from_json_handles_structural_invalid_values PASS")


def test_normalize_domain() -> None:
    """normalize_domain extracts clean domain from various URL formats."""
    assert normalize_domain("https://www.reuters.com/article/1") == "reuters.com"
    assert normalize_domain("http://rba.gov.au/path") == "rba.gov.au"
    assert normalize_domain("https://news.google.com/rss/search?q=test") == "news.google.com"
    assert normalize_domain("https://WWW.BBC.COM/news") == "bbc.com"
    assert normalize_domain("") == ""
    assert normalize_domain("reuters.com/article") == "reuters.com"

    print("  test_normalize_domain PASS")


def test_google_news_aggregator() -> None:
    """Google News RSS URL is flagged as aggregator, not final source."""
    ref = SourceRef(
        title="RBA Rate Decision - Reuters",
        url="https://news.google.com/rss/articles/abc123",
        source="google_news_rss",
        retrieved_at="2026-05-16T10:00:00Z",
        published_at="2026-05-16T09:00:00Z",
    )
    meta = source_metadata_from_source_ref(ref)

    assert meta.is_aggregator is True
    assert meta.aggregator_provider == "google_news_rss"
    assert meta.domain == "news.google.com"
    assert meta.publisher == "Reuters"

    print("  test_google_news_aggregator PASS")


def test_official_central_bank_tier1() -> None:
    """Official central bank domain gets tier 1, highest quality."""
    ref = SourceRef(
        title="Monetary Policy Decision",
        url="https://rba.gov.au/monetary-policy/decisions/2026/",
        source="official_document",
        retrieved_at="2026-05-16T10:00:00Z",
    )
    meta = source_metadata_from_source_ref(ref)

    assert meta.source_type == "official_central_bank"
    assert meta.source_tier == 1
    assert "official" in meta.quality_reason

    ref_pboc = SourceRef(
        title="PBoC Policy Statement",
        url="https://pbc.gov.cn/policy/2026/",
        source="official_document",
        retrieved_at="2026-05-16T10:00:00Z",
    )
    meta_pboc = source_metadata_from_source_ref(ref_pboc)
    assert meta_pboc.source_tier == 1

    print("  test_official_central_bank_tier1 PASS")


def test_reuters_higher_than_unknown() -> None:
    """Reuters gets tier 2, unknown gets tier 4."""
    ref_reuters = SourceRef(
        title="AUD Weakens on Trade Data",
        url="https://reuters.com/fx/aud-weakens",
        source="google_news_rss",
        retrieved_at="2026-05-16T10:00:00Z",
    )
    meta_reuters = source_metadata_from_source_ref(ref_reuters)

    ref_unknown = SourceRef(
        title="Some Blog Post",
        url="https://randomsite.xyz/post",
        source="web_search",
        retrieved_at="2026-05-16T10:00:00Z",
    )
    meta_unknown = source_metadata_from_source_ref(ref_unknown)

    assert meta_reuters.source_tier < meta_unknown.source_tier, (
        f"Reuters tier {meta_reuters.source_tier} should be < unknown tier {meta_unknown.source_tier}"
    )
    assert tier_to_quality_score(meta_reuters.source_tier) > tier_to_quality_score(meta_unknown.source_tier)

    print(f"  test_reuters_higher_than_unknown PASS (reuters={meta_reuters.source_tier}, unknown={meta_unknown.source_tier})")


def test_legacy_source_string() -> None:
    """Legacy 'url=... | title=... | provider=...' strings parse correctly."""
    legacy = "url=https://reuters.com/article/1 | title=AUD Falls - Reuters | provider=google_news_rss"
    meta = source_metadata_from_legacy_string(legacy)

    assert meta.url == "https://reuters.com/article/1"
    assert meta.title == "AUD Falls - Reuters"
    assert meta.provider == "google_news_rss"
    assert meta.domain == "reuters.com"
    assert meta.source_tier == 2

    empty_meta = source_metadata_from_legacy_string(None)
    assert empty_meta.source_type == "unknown"
    assert empty_meta.quality_reason == "empty_source"

    provider_only = source_metadata_from_legacy_string("google_news_rss")
    assert provider_only.provider == "google_news_rss"
    assert provider_only.is_aggregator is True

    print("  test_legacy_source_string PASS")


def test_evidence_store_stores_metadata() -> None:
    """EvidenceStore persists source_metadata_json and reads it back."""
    store = EvidenceStore(":memory:")

    meta = SourceMetadata(
        url="https://reuters.com/fx/1",
        domain="reuters.com",
        source_type="mainstream_financial_media",
        source_tier=2,
        quality_reason="premium_media_domain:reuters.com",
    )
    chunk = EvidenceChunk(
        task_id="test-task-1",
        agent_name="news_agent",
        content="Test content",
        source="url=https://reuters.com/fx/1 | title=Test | provider=google_news_rss",
        category="news_event",
        importance=0.7,
        confidence=0.7,
        source_metadata_json=meta.to_json(),
    )
    store.insert_chunk(chunk)

    retrieved = store.get_chunk(chunk.chunk_id)
    assert retrieved is not None
    assert retrieved.source_metadata_json != "{}"

    restored = SourceMetadata.from_json(retrieved.source_metadata_json)
    assert restored.domain == "reuters.com"
    assert restored.source_type == "mainstream_financial_media"
    assert restored.source_tier == 2

    store.close()
    print("  test_evidence_store_stores_metadata PASS")


def test_old_database_migrates_safely() -> None:
    """Opening an existing v5 database adds source_metadata_json column safely."""
    import sqlite3
    import tempfile, os

    db_path = os.path.join(tempfile.gettempdir(), "test_migrate_10_6a.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS evidence_chunks (
            chunk_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            preset_name TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            source TEXT,
            category TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.0,
            confidence REAL NOT NULL DEFAULT 0.0,
            entities_json TEXT NOT NULL DEFAULT '[]',
            used_in_brief INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            ttl_policy TEXT NOT NULL DEFAULT 'task',
            token_estimate INTEGER NOT NULL DEFAULT 0,
            attention_score REAL NOT NULL DEFAULT 0.0,
            composite_score REAL NOT NULL DEFAULT 0.0,
            score_importance REAL NOT NULL DEFAULT 0.0,
            score_confidence REAL NOT NULL DEFAULT 0.0,
            score_recency REAL NOT NULL DEFAULT 0.0,
            score_source_quality REAL NOT NULL DEFAULT 0.0,
            score_user_relevance REAL NOT NULL DEFAULT 0.0,
            score_conflict_value REAL NOT NULL DEFAULT 0.0,
            score_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS evidence_findings (
            finding_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            key TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            direction TEXT,
            chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_score REAL,
            category TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE IF NOT EXISTS citation_refs (
            citation_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            chunk_id TEXT NOT NULL DEFAULT '',
            finding_id TEXT,
            section_title TEXT NOT NULL DEFAULT '',
            relevance_score REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE IF NOT EXISTS retrieval_traces (
            trace_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL DEFAULT '',
            retrieved_count INTEGER NOT NULL DEFAULT 0,
            total_chunks INTEGER NOT NULL DEFAULT 0,
            top_scores_json TEXT NOT NULL DEFAULT '[]',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT NOT NULL DEFAULT '',
            section_title TEXT NOT NULL DEFAULT '',
            selected_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            section_covered INTEGER NOT NULL DEFAULT 0,
            score_distribution_json TEXT NOT NULL DEFAULT '{}',
            conflict_count INTEGER NOT NULL DEFAULT 0,
            conflict_pairs_json TEXT NOT NULL DEFAULT '[]',
            boosted_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            scoring_method TEXT NOT NULL DEFAULT '',
            fallback_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (5);
    """)
    conn.execute(
        "INSERT INTO evidence_chunks (chunk_id, task_id, agent_name, content, source) VALUES (?, ?, ?, ?, ?)",
        ("old-chunk-1", "old-task", "fx_agent", "Old content", "url=https://rba.gov.au/test | provider=fetch_rate.py"),
    )
    conn.commit()
    conn.close()

    store = EvidenceStore(db_path)
    chunk = store.get_chunk("old-chunk-1")
    assert chunk is not None
    assert chunk.source_metadata_json == "{}"

    version = store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 6, f"Expected schema version 6, got {version}"

    store.close()
    os.remove(db_path)
    print("  test_old_database_migrates_safely PASS")


def test_scorer_prefers_structured_metadata() -> None:
    """EvidenceScorer uses structured metadata tier when available, legacy when not."""
    chunk_with_meta = EvidenceChunk(
        agent_name="news_agent",
        content="Test",
        source="url=https://randomsite.xyz/article | provider=google_news_rss",
        importance=0.7,
        confidence=0.7,
        source_metadata_json=SourceMetadata(
            url="https://reuters.com/fx/1",
            domain="reuters.com",
            source_type="mainstream_financial_media",
            source_tier=2,
            quality_reason="premium_media_domain:reuters.com",
        ).to_json(),
    )
    score_with = compute_source_quality_score(chunk_with_meta)
    assert score_with == 0.82, f"Expected 0.82 for tier 2, got {score_with}"

    chunk_legacy = EvidenceChunk(
        agent_name="news_agent",
        content="Test",
        source="url=https://reuters.com/fx/1 | provider=google_news_rss",
        importance=0.7,
        confidence=0.7,
    )
    score_legacy = compute_source_quality_score(chunk_legacy)
    assert score_legacy == 0.82, f"Expected 0.82 for reuters domain fallback, got {score_legacy}"

    chunk_empty = EvidenceChunk(
        agent_name="news_agent",
        content="Test",
        source=None,
        importance=0.7,
        confidence=0.7,
    )
    score_empty = compute_source_quality_score(chunk_empty)
    assert score_empty == 0.2, f"Expected 0.2 for empty source, got {score_empty}"

    print(f"  test_scorer_prefers_structured_metadata PASS (structured={score_with}, legacy={score_legacy}, empty={score_empty})")


def test_source_debug_info() -> None:
    """EvidenceChunk.source_debug_info() exposes correct metadata fields."""
    meta = SourceMetadata(
        domain="reuters.com",
        provider="google_news_rss",
        source_type="mainstream_financial_media",
        source_tier=2,
        quality_reason="premium_media_domain:reuters.com",
        is_aggregator=True,
    )
    chunk = EvidenceChunk(
        agent_name="news_agent",
        content="Test",
        source_metadata_json=meta.to_json(),
        importance=0.5,
        confidence=0.5,
    )
    debug = chunk.source_debug_info()
    assert debug["domain"] == "reuters.com"
    assert debug["provider"] == "google_news_rss"
    assert debug["source_type"] == "mainstream_financial_media"
    assert debug["source_tier"] == 2
    assert debug["is_aggregator"] is True
    assert "reuters" in debug["quality_reason"]

    empty_chunk = EvidenceChunk(
        agent_name="fx_agent",
        content="Test",
        importance=0.5,
        confidence=0.5,
    )
    empty_debug = empty_chunk.source_debug_info()
    assert empty_debug["source_type"] == "unknown"
    assert empty_debug["source_tier"] == 0

    for raw in ("[]", "null", '"bad"'):
        invalid_chunk = EvidenceChunk(
            agent_name="news_agent",
            content="Test",
            source_metadata_json=raw,
            importance=0.5,
            confidence=0.5,
        )
        invalid_debug = invalid_chunk.source_debug_info()
        assert invalid_debug["source_type"] == "unknown"
        assert invalid_debug["source_tier"] == 0

    print("  test_source_debug_info PASS")


def test_debug_payload_exposes_source_metadata() -> None:
    """phase10_chunk_debug() includes source_metadata with all required fields."""
    sys.path.insert(0, str(Path(_HERE).parent.parent.parent.parent.parent / "web"))
    from fx_research_debug import phase10_chunk_debug

    meta = SourceMetadata(
        domain="bbc.com",
        provider="google_news_rss",
        source_type="general_news",
        source_tier=3,
        quality_reason="mainstream_domain:bbc.com",
        is_aggregator=True,
    )
    chunk = EvidenceChunk(
        agent_name="news_agent",
        content="BBC article content here",
        source="url=https://bbc.com/news/1 | provider=google_news_rss",
        source_metadata_json=meta.to_json(),
        importance=0.7,
        confidence=0.7,
    )
    debug_row = phase10_chunk_debug(chunk)

    assert "source_metadata" in debug_row, "debug row must include source_metadata"
    sm = debug_row["source_metadata"]
    assert sm["domain"] == "bbc.com"
    assert sm["provider"] == "google_news_rss"
    assert sm["source_type"] == "general_news"
    assert sm["source_tier"] == 3
    assert sm["is_aggregator"] is True
    assert "bbc" in sm["quality_reason"]

    print("  test_debug_payload_exposes_source_metadata PASS")


def test_is_aggregator_string_safety() -> None:
    """is_aggregator='false' (string) must not be parsed as True."""
    meta_false_str = SourceMetadata.from_json('{"is_aggregator": "false"}')
    assert meta_false_str.is_aggregator is False, (
        f"String 'false' should not be True, got {meta_false_str.is_aggregator}"
    )

    meta_true = SourceMetadata.from_json('{"is_aggregator": true}')
    assert meta_true.is_aggregator is True

    meta_false = SourceMetadata.from_json('{"is_aggregator": false}')
    assert meta_false.is_aggregator is False

    meta_one = SourceMetadata.from_json('{"is_aggregator": 1}')
    assert meta_one.is_aggregator is False

    print("  test_is_aggregator_string_safety PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all() -> None:
    print("Phase 10.6A — SourceMetadata tests")
    print("=" * 50)

    test_serialization_roundtrip()
    test_from_json_handles_structural_invalid_values()
    test_normalize_domain()
    test_google_news_aggregator()
    test_official_central_bank_tier1()
    test_reuters_higher_than_unknown()
    test_legacy_source_string()
    test_evidence_store_stores_metadata()
    test_old_database_migrates_safely()
    test_scorer_prefers_structured_metadata()
    test_source_debug_info()
    test_debug_payload_exposes_source_metadata()
    test_is_aggregator_string_safety()

    print("=" * 50)
    print("All 13 tests passed.")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
