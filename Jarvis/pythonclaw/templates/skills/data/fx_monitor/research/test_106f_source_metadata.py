#!/usr/bin/env python3
"""
Phase 10.6F-fix — SourceMetadata population & query freshness tests.

Tests:
  1. test_source_metadata_tier1_populated
  2. test_source_metadata_tier4_aggregator
  3. test_source_metadata_no_fake_official
  4. test_query_no_hardcoded_year
  5. test_query_uses_dynamic_year
  6. test_source_ref_labels_search_method

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106f_source_metadata.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from agents.policy_signal_agent import (  # noqa: E402
    _get_policy_buckets,
    _POLICY_BUCKETS,
    build_source_metadata_for_bucket,
    classify_source_tier,
    _current_year_range,
)
from source_metadata import SourceMetadata  # noqa: E402


# ── Test 1: Tier-1 source metadata properly populated ────────────────────────

def test_source_metadata_tier1_populated() -> None:
    """SourceMetadata for tier-1 official results has correct fields."""
    results = [
        {"url": "https://www.rba.gov.au/media-releases/2026/mr-26-05.html",
         "title": "RBA Cash Rate Decision May 2026", "snippet": "The Board decided...", "query": "rba"},
        {"url": "https://www.reuters.com/markets/rba-holds-rate",
         "title": "RBA holds", "snippet": "...", "query": "rba"},
    ]
    meta = build_source_metadata_for_bucket("policy_rba", results)

    assert meta.domain == "www.rba.gov.au", f"Expected rba.gov.au domain, got {meta.domain}"
    assert meta.source_tier == 1, f"Expected tier 1, got {meta.source_tier}"
    assert meta.source_type == "official_central_bank", f"Got source_type={meta.source_type}"
    assert "policy_bucket:policy_rba" in meta.quality_reason
    assert meta.is_aggregator is False
    assert meta.provider == "policy_signal_agent"
    print("  tier-1 source metadata populated        OK")


# ── Test 2: Tier-4 aggregator metadata ───────────────────────────────────────

def test_source_metadata_tier4_aggregator() -> None:
    """Aggregator-only results produce correct metadata with is_aggregator=True."""
    results = [
        {"url": "https://randomcryptoblog.xyz/rba", "title": "RBA maybe", "snippet": "...", "query": "rba"},
        {"url": "https://unknownsite.io/policy", "title": "Policy talk", "snippet": "...", "query": "rba"},
    ]
    meta = build_source_metadata_for_bucket("policy_rba", results)

    assert meta.source_tier == 4, f"Expected tier 4, got {meta.source_tier}"
    assert meta.is_aggregator is True
    assert "aggregator_only" in meta.quality_reason
    assert meta.source_type == "aggregator"
    print("  tier-4 aggregator metadata              OK")


# ── Test 3: Weak aggregator does not fake official metadata ──────────────────

def test_source_metadata_no_fake_official() -> None:
    """When only news.google.com results, don't pretend it's an official source."""
    results = [
        {"url": "https://news.google.com/articles/abc123",
         "title": "RBA rate decision", "snippet": "...", "query": "rba"},
    ]
    meta = build_source_metadata_for_bucket("policy_rba", results)

    assert meta.source_tier >= 3, f"Google News should not be tier 1-2, got {meta.source_tier}"
    assert meta.source_type != "official_central_bank", f"Should not be official, got {meta.source_type}"
    assert "news.google.com" in meta.domain or meta.is_aggregator
    print("  no fake official metadata               OK")


# ── Test 4: No hardcoded "2025 2026" in queries ─────────────────────────────

def test_query_no_hardcoded_year() -> None:
    """Policy bucket queries should not contain literal '2025 2026'."""
    buckets = _get_policy_buckets()
    for bucket_name, cfg in buckets.items():
        for query in cfg["queries"]:
            assert "2025 2026" not in query, (
                f"{bucket_name} query has hardcoded '2025 2026': {query}"
            )
    # Also check the module-level cached buckets
    for bucket_name, cfg in _POLICY_BUCKETS.items():
        for query in cfg["queries"]:
            assert "2025 2026" not in query, (
                f"Module-level {bucket_name} has hardcoded '2025 2026': {query}"
            )
    print("  no hardcoded '2025 2026' in queries     OK")


# ── Test 5: Dynamic year in queries ──────────────────────────────────────────

def test_query_uses_dynamic_year() -> None:
    """Queries should contain current year or 'latest' for freshness."""
    from datetime import datetime, timezone
    current_year = str(datetime.now(timezone.utc).year)

    buckets = _get_policy_buckets()
    for bucket_name, cfg in buckets.items():
        queries_text = " ".join(cfg["queries"]).lower()
        has_year = current_year in queries_text
        has_latest = "latest" in queries_text or "最新" in queries_text
        assert has_year or has_latest, (
            f"{bucket_name} queries lack current year or 'latest': {cfg['queries']}"
        )

    yr = _current_year_range()
    assert current_year in yr
    print("  dynamic year in queries                 OK")


# ── Test 6: SourceRef labels reflect search method ───────────────────────────

def test_source_ref_labels_search_method() -> None:
    """SourceRef source field should identify search method for aggregator detection."""
    # Simulate what _collect_and_analyse does
    from agents.policy_signal_agent import classify_source_tier

    # Google News result
    url_gn = "https://news.google.com/articles/abc"
    tier_gn = classify_source_tier(url_gn)
    assert tier_gn >= 3, f"news.google.com should be tier 3+, got {tier_gn}"

    # Official result
    url_rba = "https://www.rba.gov.au/monetary-policy/"
    tier_rba = classify_source_tier(url_rba)
    assert tier_rba == 1, f"rba.gov.au should be tier 1, got {tier_rba}"

    # Reuters
    url_reuters = "https://www.reuters.com/markets/rates"
    tier_reuters = classify_source_tier(url_reuters)
    assert tier_reuters == 2, f"reuters.com should be tier 2, got {tier_reuters}"

    # Unknown
    url_blog = "https://crypto-blog.xyz/rba-news"
    tier_blog = classify_source_tier(url_blog)
    assert tier_blog == 4, f"Unknown should be tier 4, got {tier_blog}"

    print("  source_ref labels search method         OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_source_metadata_tier1_populated,
        test_source_metadata_tier4_aggregator,
        test_source_metadata_no_fake_official,
        test_query_no_hardcoded_year,
        test_query_uses_dynamic_year,
        test_source_ref_labels_search_method,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6F-fix SourceMetadata & Freshness — {len(tests)} tests")
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
