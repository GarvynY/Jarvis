#!/usr/bin/env python3
"""
Phase 10.6F-fix2 — SourceMetadata integration into actual execution path.

Tests:
  1. test_build_findings_attaches_metadata
  2. test_evidence_store_uses_agent_metadata
  3. test_tier1_metadata_in_chunk_debug
  4. test_aggregator_metadata_preserved
  5. test_runtime_year_not_cached
  6. test_existing_tests_no_regression

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_106f_fix2_metadata_path.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput,
    Finding,
    FindingCategory,
    ResearchPreset,
    ResearchTask,
    SafeUserContext,
    SourceRef,
    now_iso,
)
from evidence_store import EvidenceStore  # noqa: E402
from agents.policy_signal_agent import (  # noqa: E402
    _build_findings,
    _get_policy_buckets,
    build_source_metadata_for_bucket,
)


# ── Test 1: _build_findings attaches _source_metadata_json ───────────────────

def test_build_findings_attaches_metadata() -> None:
    """Each finding from _build_findings should have _source_metadata_json attribute."""
    llm_output = {
        "rba_stance": "hawkish",
        "rba_summary": "RBA raised rates",
        "rba_confidence": 0.8,
        "pboc_stance": "dovish",
        "pboc_summary": "PBoC cut LPR",
        "pboc_confidence": 0.75,
        "fed_stance": "neutral",
        "fed_summary": "Fed holds",
        "fed_confidence": 0.6,
    }
    results = [
        {"url": "https://www.rba.gov.au/mr-26-05.html", "title": "RBA Decision",
         "snippet": "Board decided...", "query": "rba interest rate"},
        {"url": "https://www.reuters.com/pboc-lpr", "title": "PBoC LPR cut",
         "snippet": "PBoC announced...", "query": "pboc monetary policy"},
        {"url": "https://www.reuters.com/fed-holds", "title": "Fed holds",
         "snippet": "Federal Reserve...", "query": "federal reserve fomc"},
    ]
    bucket_results = {
        "policy_rba": [results[0]],
        "policy_pboc": [results[1]],
        "policy_fed": [results[2]],
    }

    findings = _build_findings(llm_output, results, bucket_results)

    for f in findings:
        assert hasattr(f, "_source_metadata_json"), (
            f"{f.key} missing _source_metadata_json attribute"
        )
        meta_json = f._source_metadata_json
        assert meta_json and meta_json != "{}", f"{f.key} has empty metadata"
        meta = json.loads(meta_json)
        assert meta.get("domain"), f"{f.key} missing domain in metadata"
        assert meta.get("source_type"), f"{f.key} missing source_type"
        assert "source_tier" in meta, f"{f.key} missing source_tier"
        assert meta.get("quality_reason"), f"{f.key} missing quality_reason"
        assert "policy_bucket:" in meta["quality_reason"] or "aggregator_only:" in meta["quality_reason"]

    # Check specific tiers
    rba_f = next(f for f in findings if f.key == "policy_rba")
    rba_meta = json.loads(rba_f._source_metadata_json)
    assert rba_meta["source_tier"] == 1, f"RBA should be tier 1, got {rba_meta['source_tier']}"
    assert rba_meta["source_type"] == "official_central_bank"

    pboc_f = next(f for f in findings if f.key == "policy_pboc")
    pboc_meta = json.loads(pboc_f._source_metadata_json)
    assert pboc_meta["source_tier"] == 2, f"PBoC (reuters) should be tier 2, got {pboc_meta['source_tier']}"

    print("  _build_findings attaches metadata       OK")


# ── Test 2: EvidenceStore picks up agent-provided metadata ───────────────────

def test_evidence_store_uses_agent_metadata() -> None:
    """EvidenceStore should prefer _source_metadata_json from Finding over generic inference."""
    store = EvidenceStore(":memory:")

    task = ResearchTask(
        preset_name="fx_cnyaud",
        research_topic="CNY/AUD",
        focus_pair="CNY/AUD",
        focus_assets=["CNY", "AUD"],
    )

    # Create a finding with agent-provided metadata
    finding = Finding(
        key="policy_rba",
        summary="RBA hawkish hold",
        category="policy_signal",
        importance=0.8,
        direction="bullish_aud",
        evidence_score=0.7,
        source_ids=["https://www.rba.gov.au/mr-26-05.html"],
        evidence_basis="policy_signal_agent:policy_rba tier=1",
    )
    # Attach bucket-level metadata
    meta = build_source_metadata_for_bucket("policy_rba", [
        {"url": "https://www.rba.gov.au/mr-26-05.html", "title": "RBA Decision"},
    ])
    finding._source_metadata_json = meta.to_json()

    output = AgentOutput(
        agent_name="policy_signal_agent",
        status="ok",
        summary="PolicySignal OK",
        findings=[finding],
        sources=[SourceRef(
            url="https://www.rba.gov.au/mr-26-05.html",
            title="RBA Decision",
            source="policy_signal_agent:www.rba.gov.au",
            retrieved_at=now_iso(),
        )],
        confidence=0.75,
    )

    store.ingest_outputs(task, [output])

    # Retrieve the chunk and check its source_metadata_json
    chunks = store._conn.execute(
        "SELECT source_metadata_json FROM evidence_chunks WHERE agent_name='policy_signal_agent'"
    ).fetchall()
    assert len(chunks) >= 1, "No chunks found for policy_signal_agent"

    chunk_meta = json.loads(chunks[0][0])
    assert chunk_meta.get("source_tier") == 1, f"Expected tier 1, got {chunk_meta.get('source_tier')}"
    assert "policy_bucket:policy_rba" in chunk_meta.get("quality_reason", ""), (
        f"quality_reason should contain bucket info, got: {chunk_meta.get('quality_reason')}"
    )
    assert chunk_meta.get("source_type") == "official_central_bank"
    assert chunk_meta.get("provider") == "policy_signal_agent"
    assert chunk_meta.get("is_aggregator") is False

    print("  evidence_store uses agent metadata      OK")


# ── Test 3: Tier-1 metadata visible in chunk debug info ──────────────────────

def test_tier1_metadata_in_chunk_debug() -> None:
    """source_debug_info() on a policy chunk should show bucket-level fields."""
    from schema import EvidenceChunk

    meta = build_source_metadata_for_bucket("policy_rba", [
        {"url": "https://www.rba.gov.au/mr.html", "title": "RBA"},
    ])

    chunk = EvidenceChunk(
        task_id="test",
        preset_name="fx_cnyaud",
        agent_name="policy_signal_agent",
        content="RBA hawkish hold",
        category="policy_signal",
        importance=0.8,
        confidence=0.75,
        source_metadata_json=meta.to_json(),
    )

    debug = chunk.source_debug_info()
    assert debug["domain"] == "www.rba.gov.au", f"Got domain={debug['domain']}"
    assert debug["source_type"] == "official_central_bank"
    assert debug["source_tier"] == 1
    assert "policy_bucket:policy_rba" in debug.get("quality_reason", "")
    assert debug.get("is_aggregator") is False
    print("  tier-1 metadata in chunk debug          OK")


# ── Test 4: Aggregator metadata preserved correctly ──────────────────────────

def test_aggregator_metadata_preserved() -> None:
    """Aggregator-only bucket results produce aggregator metadata, not fake official."""
    store = EvidenceStore(":memory:")

    task = ResearchTask(preset_name="fx_cnyaud", focus_pair="CNY/AUD", focus_assets=["CNY", "AUD"])

    finding = Finding(
        key="policy_fed",
        summary="Fed maybe dovish",
        category="policy_signal",
        importance=0.6,
        direction=None,
        evidence_score=0.3,
        source_ids=["https://cryptoblog.xyz/fed-news"],
        evidence_basis="policy_signal_agent:policy_fed tier=4",
    )
    meta = build_source_metadata_for_bucket("policy_fed", [
        {"url": "https://cryptoblog.xyz/fed-news", "title": "Fed news blog"},
    ])
    finding._source_metadata_json = meta.to_json()

    output = AgentOutput(
        agent_name="policy_signal_agent",
        status="partial",
        summary="PolicySignal partial",
        findings=[finding],
        sources=[SourceRef(
            url="https://cryptoblog.xyz/fed-news",
            title="Fed blog",
            source="cryptoblog.xyz",
            retrieved_at=now_iso(),
        )],
        confidence=0.4,
    )

    store.ingest_outputs(task, [output])

    chunks = store._conn.execute(
        "SELECT source_metadata_json FROM evidence_chunks WHERE agent_name='policy_signal_agent'"
    ).fetchall()
    assert len(chunks) >= 1

    chunk_meta = json.loads(chunks[0][0])
    assert chunk_meta["source_tier"] == 4, f"Should be tier 4, got {chunk_meta['source_tier']}"
    assert chunk_meta["is_aggregator"] is True
    assert chunk_meta["source_type"] == "aggregator"
    assert "aggregator_only:policy_fed" in chunk_meta.get("quality_reason", "")
    print("  aggregator metadata preserved           OK")


# ── Test 5: Runtime year not cached at import time ───────────────────────────

def test_runtime_year_not_cached() -> None:
    """_collect_and_analyse uses _get_policy_buckets() at runtime, not module cache."""
    import inspect
    from agents.policy_signal_agent import PolicySignalAgent

    source = inspect.getsource(PolicySignalAgent._collect_and_analyse)
    assert "policy_buckets = _get_policy_buckets()" in source, (
        "_collect_and_analyse should call _get_policy_buckets() for runtime freshness"
    )
    assert "for bucket_name, cfg in policy_buckets.items()" in source, (
        "Should iterate over runtime policy_buckets, not module-level _POLICY_BUCKETS"
    )
    print("  runtime year not cached                 OK")


# ── Test 6: Existing tests still pass (quick sanity) ─────────────────────────

def test_existing_tests_no_regression() -> None:
    """Quick check that core functionality still works."""
    from agents.policy_signal_agent import _ENABLE_POLICY_AGENT, classify_source_tier

    # Flag — True when PolicySignalAgent is active
    assert _ENABLE_POLICY_AGENT is True

    # Tier classification
    assert classify_source_tier("https://www.rba.gov.au/x") == 1
    assert classify_source_tier("https://www.reuters.com/y") == 2
    assert classify_source_tier("https://bbc.com/z") == 3
    assert classify_source_tier("https://randomsite.xyz/w") == 4

    # build_source_metadata_for_bucket with no results
    meta = build_source_metadata_for_bucket("policy_rba", [])
    assert meta.source_tier == 4
    assert meta.source_type == "unknown"

    print("  existing tests no regression            OK")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_build_findings_attaches_metadata,
        test_evidence_store_uses_agent_metadata,
        test_tier1_metadata_in_chunk_debug,
        test_aggregator_metadata_preserved,
        test_runtime_year_not_cached,
        test_existing_tests_no_regression,
    ]
    print(f"\n{'='*60}")
    print(f"Phase 10.6F-fix2 Metadata Path — {len(tests)} tests")
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
