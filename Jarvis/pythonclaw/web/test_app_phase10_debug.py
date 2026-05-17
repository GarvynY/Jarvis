"""Tests for FX research Phase 10 debug API helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_HELPER_PATH = Path(__file__).with_name("fx_research_debug.py")
_SPEC = importlib.util.spec_from_file_location("fx_research_debug", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["fx_research_debug"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

build_phase10_debug_payload = _MODULE.build_phase10_debug_payload


@dataclass
class _Trace:
    selected_chunk_ids: list[str] = field(default_factory=list)
    scoring_method: str = "composite"
    conflict_count: int = 0
    conflict_pairs: list[dict[str, Any]] = field(default_factory=list)
    boosted_chunk_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_chunk_ids": self.selected_chunk_ids,
            "scoring_method": self.scoring_method,
            "conflict_count": self.conflict_count,
            "conflict_pairs": self.conflict_pairs,
            "boosted_chunk_ids": self.boosted_chunk_ids,
        }


class _Store:
    chunks: dict[str, Any] = {}
    findings: dict[str, Any] = {}
    policy_chunks: list[Any] = []
    findings_by_chunk: dict[str, list[Any]] = {}

    def __enter__(self) -> "_Store":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def get_chunk(self, chunk_id: str) -> Any:
        return self.chunks.get(chunk_id)

    def get_finding(self, finding_id: str) -> Any:
        return self.findings.get(finding_id)

    def query_chunks(self, task_id: str, **kwargs: Any) -> list[Any]:
        if kwargs.get("category") == "policy_signal":
            return list(self.policy_chunks)
        return []

    def _findings_by_chunk_ids_all(self, chunk_ids: list[str], task_id: str) -> dict[str, list[Any]]:
        return {
            chunk_id: self.findings_by_chunk.get(chunk_id, [])
            for chunk_id in chunk_ids
        }


def _chunk(chunk_id: str, **overrides: Any) -> Any:
    data = {
        "chunk_id": chunk_id,
        "agent_name": "news_agent",
        "category": "macro_policy",
        "source": "https://reuters.com/example",
        "content": "A" * 260,
        "used_in_brief": True,
        "importance": 0.8,
        "confidence": 0.7,
        "token_estimate": 120,
        "attention_score": 0.77,
        "composite_score": 0.82,
        "score_importance": 0.8,
        "score_confidence": 0.7,
        "score_recency": 0.9,
        "score_source_quality": 0.95,
        "score_user_relevance": 0.6,
        "score_conflict_value": 0.1,
        "score_reason": "official_source,user_match,conflict_boost",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_phase10_debug_payload_includes_selected_score_breakdown() -> None:
    _Store.findings = {}
    _Store.policy_chunks = []
    _Store.findings_by_chunk = {}
    _Store.chunks = {
        "c1": _chunk("c1"),
        "c2": _chunk(
            "c2",
            used_in_brief=False,
            composite_score=0.0,
            score_user_relevance=0.2,
        ),
    }
    traces = [
        _Trace(
            selected_chunk_ids=["c1", "c2", "c1"],
            conflict_count=1,
            conflict_pairs=[{"a": "c1", "b": "c2", "type": "direction"}],
            boosted_chunk_ids=["c1"],
        )
    ]

    payload = build_phase10_debug_payload("task-1", traces, _Store)

    assert payload["available"] is True
    assert payload["ranking_basis"] == "composite_score"
    assert payload["selected_chunk_ids"] == ["c1", "c2"]
    assert payload["score_summary"]["selected_count"] == 2
    assert payload["score_summary"]["loaded_selected_count"] == 2
    assert payload["score_summary"]["scored_selected_count"] == 1
    assert payload["score_summary"]["used_in_brief_count"] == 1
    assert payload["score_summary"]["user_relevant_selected_count"] == 1
    assert payload["conflicts"]["count"] == 1
    assert payload["conflicts"]["boosted_chunk_ids"] == ["c1"]
    assert payload["selected_chunks"][0]["score_breakdown"] == {
        "importance": 0.8,
        "confidence": 0.7,
        "recency": 0.9,
        "source_quality": 0.95,
        "user_relevance": 0.6,
        "conflict_value": 0.1,
    }
    assert len(payload["selected_chunks"][0]["text_preview"]) == 220


def test_phase10_debug_payload_includes_policy_candidates_and_conflict_breakdown() -> None:
    policy_chunk = _chunk(
        "p1",
        agent_name="policy_signal_agent",
        category="policy_signal",
        source="url=https://news.google.com/policy | finding_key=policy_rba",
        source_metadata_json='{"source_tier": 3, "domain": "news.google.com"}',
        used_in_brief=True,
        composite_score=0.71,
        score_reason="baseline",
    )
    _Store.chunks = {"p1": policy_chunk}
    _Store.policy_chunks = [policy_chunk]
    _Store.findings_by_chunk = {
        "p1": [SimpleNamespace(evidence_score=0.72)],
    }
    _Store.findings = {
        "n1": SimpleNamespace(agent_name="news_agent", category="news_event"),
        "m1": SimpleNamespace(agent_name="market_drivers_agent", category="market_driver"),
        "pfind": SimpleNamespace(agent_name="policy_signal_agent", category="policy_signal"),
        "fx1": SimpleNamespace(agent_name="fx_agent", category="fx_price"),
    }

    payload = build_phase10_debug_payload(
        "task-policy",
        [
            _Trace(
                selected_chunk_ids=["p1"],
                conflict_count=2,
                conflict_pairs=[
                    {"finding_id_a": "n1", "finding_id_b": "m1", "rule": "shared_entity_opposite_direction"},
                    {"finding_id_a": "pfind", "finding_id_b": "fx1", "rule": "shared_entity_opposite_direction"},
                ],
            )
        ],
        _Store,
    )

    assert payload["policy_candidates"][0]["finding_key"] == "policy_rba"
    assert payload["policy_candidates"][0]["evidence_score"] == 0.72
    assert payload["policy_candidates"][0]["source_tier"] == 3
    assert payload["policy_candidates"][0]["selected"] is True
    assert payload["policy_candidates"][0]["skip_reason"] == ""
    breakdown = payload["conflicts"]["breakdown"]
    assert breakdown["raw_conflict_count"] == 2
    assert breakdown["unique_conflict_count"] == 2
    assert breakdown["news_vs_market_driver"] == 1
    assert breakdown["policy_vs_fx"] == 1


def test_phase10_debug_payload_reports_store_failure() -> None:
    class BrokenStore:
        def __enter__(self) -> "BrokenStore":
            raise RuntimeError("db unavailable")

        def __exit__(self, *_args: Any) -> None:
            return None

    payload = build_phase10_debug_payload(
        "task-2",
        [_Trace(selected_chunk_ids=["missing"], scoring_method="legacy")],
        BrokenStore,
    )

    assert payload["available"] is False
    assert "db unavailable" in payload["error"]
    assert payload["ranking_basis"] == "legacy_importance_confidence"
    assert payload["selected_chunks"] == []


_get_query_plan_summary = _MODULE._get_query_plan_summary


def test_query_plan_summary_returns_budget_stats() -> None:
    summary = _get_query_plan_summary("test-task-id")
    assert isinstance(summary, dict), f"Expected dict, got {type(summary)}"
    assert summary["total_buckets"] == 15
    assert summary["active_buckets"] < 15
    assert summary["budget_mode"] == "normal"
    assert summary["budget_limited"] is True
    assert "buckets" in summary
    for item in summary["buckets"]:
        assert "bucket_name" in item, f"Missing bucket_name: {item}"
        assert "agent_name" in item, f"Missing agent_name: {item}"
        assert "category" in item, f"Missing category: {item}"
        assert "max_results" in item, f"Missing max_results: {item}"
        assert "enabled" in item, f"Missing enabled: {item}"


def test_query_plan_summary_no_raw_queries() -> None:
    summary = _get_query_plan_summary("test-task-id")
    for item in summary["buckets"]:
        assert "queries" not in item, f"Raw queries exposed: {item}"
        assert "source_preference" not in item, f"source_preference exposed: {item}"


def test_query_plan_summary_no_user_data_leak() -> None:
    summary = _get_query_plan_summary("test-task-id")
    serialized = str(summary)
    assert "4.85" not in serialized, "target_rate leaked"
    assert "tuition" not in serialized, "purpose leaked"


def test_query_plan_in_debug_payload() -> None:
    _Store.chunks = {}
    payload = build_phase10_debug_payload("task-plan", [_Trace()], _Store)
    assert "query_plan" in payload
    assert isinstance(payload["query_plan"], dict)
    assert payload["query_plan"]["total_buckets"] == 15
    assert payload["query_plan"]["budget_limited"] is True


def run_all() -> None:
    tests = [
        test_phase10_debug_payload_includes_selected_score_breakdown,
        test_phase10_debug_payload_includes_policy_candidates_and_conflict_breakdown,
        test_phase10_debug_payload_reports_store_failure,
        test_query_plan_summary_returns_budget_stats,
        test_query_plan_summary_no_raw_queries,
        test_query_plan_summary_no_user_data_leak,
        test_query_plan_in_debug_payload,
    ]
    print("Web Phase 10 debug helper tests")
    print("=" * 40)
    for test in tests:
        test()
        print(f"  {test.__name__} OK")
    print("=" * 40)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()
