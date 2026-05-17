"""Helpers for the FX research browser debug API."""

from __future__ import annotations

import json
import re
from typing import Any


def _ensure_research_path() -> None:
    """Ensure research directory is importable."""
    import sys
    from pathlib import Path
    _research = str(
        Path(__file__).resolve().parent.parent
        / "templates" / "skills" / "data" / "fx_monitor" / "research"
    )
    if _research not in sys.path:
        sys.path.insert(0, _research)


def _get_query_plan_summary(task_id: str) -> dict[str, Any]:
    """Load query plan debug summary. Returns {} only if module is missing."""
    _ensure_research_path()
    try:
        from query_planner import build_query_plan, query_plan_debug_summary
        from schema import ResearchTask
    except ImportError as exc:
        import logging
        logging.getLogger(__name__).warning(
            "query_planner import failed: %s", exc
        )
        return {}

    task = ResearchTask(task_id=task_id, preset_name="fx_cnyaud")
    plan = build_query_plan(task)
    return query_plan_debug_summary(plan)


def _round_score(value: Any) -> float:
    """Return a compact JSON-safe score for debug output."""
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def phase10_chunk_debug(chunk: Any) -> dict[str, Any]:
    """Build a privacy-conscious Phase 10 debug row for one evidence chunk."""
    text = str(getattr(chunk, "content", "") or "")
    return {
        "chunk_id": getattr(chunk, "chunk_id", ""),
        "agent_name": getattr(chunk, "agent_name", ""),
        "category": getattr(chunk, "category", ""),
        "source": getattr(chunk, "source", ""),
        "used_in_brief": bool(getattr(chunk, "used_in_brief", False)),
        "importance": _round_score(getattr(chunk, "importance", 0.0)),
        "confidence": _round_score(getattr(chunk, "confidence", 0.0)),
        "token_estimate": int(getattr(chunk, "token_estimate", 0) or 0),
        "attention_score": _round_score(getattr(chunk, "attention_score", 0.0)),
        "composite_score": _round_score(getattr(chunk, "composite_score", 0.0)),
        "score_breakdown": {
            "importance": _round_score(getattr(chunk, "score_importance", 0.0)),
            "confidence": _round_score(getattr(chunk, "score_confidence", 0.0)),
            "recency": _round_score(getattr(chunk, "score_recency", 0.0)),
            "source_quality": _round_score(getattr(chunk, "score_source_quality", 0.0)),
            "user_relevance": _round_score(getattr(chunk, "score_user_relevance", 0.0)),
            "conflict_value": _round_score(getattr(chunk, "score_conflict_value", 0.0)),
        },
        "score_reason": getattr(chunk, "score_reason", "") or "",
        "source_metadata": (
            chunk.source_debug_info()
            if hasattr(chunk, "source_debug_info")
            else {}
        ),
        "text_preview": text[:220],
    }


def _source_meta(chunk: Any) -> dict[str, Any]:
    if hasattr(chunk, "source_debug_info"):
        info = chunk.source_debug_info()
        return info if isinstance(info, dict) else {}
    try:
        data = json.loads(getattr(chunk, "source_metadata_json", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _finding_key(chunk: Any) -> str:
    text = str(getattr(chunk, "source", "") or "")
    match = re.search(r"\bfinding_key=([^|,;\s]+)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _policy_skip_reason(chunk: Any, evidence_score: float, source_tier: int) -> str:
    confidence = _round_score(getattr(chunk, "confidence", 0.0))
    content = str(getattr(chunk, "content", "") or "").lower()
    if source_tier > 3:
        return "source_tier_gt_3"
    if "insufficient_evidence" in content:
        return "insufficient_evidence"
    if confidence < 0.5 and evidence_score < 0.6:
        return "weak_policy_signal"
    return ""


def _load_policy_candidates(store: Any, task_id: str, selected_ids: set[str]) -> list[dict[str, Any]]:
    if not hasattr(store, "query_chunks"):
        return []
    try:
        chunks = store.query_chunks(
            task_id,
            category="policy_signal",
            agent_name="policy_signal_agent",
            top_k=50,
        )
    except Exception:
        return []

    findings_by_chunk: dict[str, list[Any]] = {}
    finder = getattr(store, "_findings_by_chunk_ids_all", None)
    if callable(finder):
        try:
            findings_by_chunk = finder([getattr(c, "chunk_id", "") for c in chunks], task_id)
        except Exception:
            findings_by_chunk = {}

    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_id = getattr(chunk, "chunk_id", "")
        findings = findings_by_chunk.get(chunk_id, []) if isinstance(findings_by_chunk, dict) else []
        evidence_scores = [
            _round_score(getattr(f, "evidence_score", 0.0))
            for f in findings
            if getattr(f, "evidence_score", None) is not None
        ]
        evidence_score = max(evidence_scores) if evidence_scores else 0.0
        meta = _source_meta(chunk)
        try:
            source_tier = int(meta.get("source_tier", 3))
        except (TypeError, ValueError):
            source_tier = 3
        selected = chunk_id in selected_ids
        skip_reason = "" if selected else _policy_skip_reason(chunk, evidence_score, source_tier)
        rows.append({
            "chunk_id": chunk_id,
            "finding_key": _finding_key(chunk),
            "evidence_score": evidence_score,
            "composite_score": _round_score(getattr(chunk, "composite_score", 0.0)),
            "score_reason": getattr(chunk, "score_reason", "") or "",
            "source_tier": source_tier,
            "selected": selected,
            "skip_reason": skip_reason,
        })
    return rows


def _conflict_pair_key(pair: dict[str, Any]) -> tuple[str, str, str]:
    a = str(pair.get("finding_id_a", "") or "")
    b = str(pair.get("finding_id_b", "") or "")
    rule = str(pair.get("rule", "") or "")
    ids = sorted([a, b])
    return ids[0], ids[1], rule


def _classify_conflict_pair(pair: dict[str, Any], store: Any) -> str:
    get_finding = getattr(store, "get_finding", None)
    if not callable(get_finding):
        return "other"
    try:
        fa = get_finding(pair.get("finding_id_a", ""))
        fb = get_finding(pair.get("finding_id_b", ""))
    except Exception:
        return "other"
    if fa is None or fb is None:
        return "other"

    agents = {getattr(fa, "agent_name", ""), getattr(fb, "agent_name", "")}
    cats = {getattr(fa, "category", ""), getattr(fb, "category", "")}
    has_market = "market_drivers_agent" in agents or bool(cats & {"market_driver", "commodity_trade"})
    has_policy = "policy_signal_agent" in agents or "policy_signal" in cats
    has_news = "news_agent" in agents or "news_event" in cats
    has_fx = "fx_agent" in agents or "fx_price" in cats

    if agents == {"news_agent"} or cats == {"news_event"}:
        return "news_internal"
    if has_news and has_fx:
        return "news_vs_fx"
    if has_news and has_market:
        return "news_vs_market_driver"
    if has_policy and has_fx:
        return "policy_vs_fx"
    if has_policy and has_market:
        return "policy_vs_market_driver"
    if agents == {"policy_signal_agent"} or cats == {"policy_signal"}:
        return "policy_internal"
    return "other"


def _build_conflict_breakdown(conflict_pairs: list[dict[str, Any]], store: Any) -> dict[str, Any]:
    buckets = {
        "news_internal": 0,
        "news_vs_fx": 0,
        "news_vs_market_driver": 0,
        "policy_vs_fx": 0,
        "policy_vs_market_driver": 0,
        "policy_internal": 0,
        "other": 0,
    }
    seen: set[tuple[str, str, str]] = set()
    duplicate_count = 0
    for pair in conflict_pairs:
        if not isinstance(pair, dict):
            continue
        key = _conflict_pair_key(pair)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        bucket = _classify_conflict_pair(pair, store)
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return {
        "raw_conflict_count": len(conflict_pairs),
        "unique_conflict_count": len(seen),
        "duplicate_conflict_count": duplicate_count,
        "reportable_conflict_count": len(seen),
        **buckets,
    }


def build_phase10_debug_payload(
    task_id: str,
    traces: list[Any],
    evidence_store_cls: Any,
) -> dict[str, Any]:
    """Collect Phase 10 retrieval/scoring observability for the debug API."""
    trace_rows = [t.to_dict() if hasattr(t, "to_dict") else dict(t) for t in traces]
    selected_ids: list[str] = []
    seen: set[str] = set()
    for trace in traces:
        for chunk_id in getattr(trace, "selected_chunk_ids", []) or []:
            if chunk_id and chunk_id not in seen:
                selected_ids.append(chunk_id)
                seen.add(chunk_id)

    selected_chunks: list[dict[str, Any]] = []
    policy_candidates: list[dict[str, Any]] = []
    conflict_breakdown: dict[str, Any] = {}
    unavailable_error = ""
    try:
        with evidence_store_cls() as store:
            for chunk_id in selected_ids:
                chunk = store.get_chunk(chunk_id)
                if chunk is not None:
                    selected_chunks.append(phase10_chunk_debug(chunk))
            policy_candidates = _load_policy_candidates(store, task_id, set(selected_ids))
            conflict_breakdown = _build_conflict_breakdown(
                [
                    pair
                    for trace in traces
                    for pair in (getattr(trace, "conflict_pairs", []) or [])
                    if isinstance(pair, dict)
                ],
                store,
            )
    except Exception as exc:
        unavailable_error = str(exc)

    composite_scores = [
        row["composite_score"]
        for row in selected_chunks
        if row.get("composite_score", 0.0) > 0
    ]
    conflict_pairs: list[dict[str, Any]] = []
    boosted_ids: set[str] = set()
    scoring_methods: set[str] = set()
    for trace in traces:
        method = getattr(trace, "scoring_method", "") or ""
        if method:
            scoring_methods.add(method)
        conflict_pairs.extend(list(getattr(trace, "conflict_pairs", []) or []))
        boosted_ids.update(getattr(trace, "boosted_chunk_ids", []) or [])

    return {
        "task_id": task_id,
        "available": unavailable_error == "",
        "error": unavailable_error,
        "ranking_basis": (
            "composite_score"
            if "composite" in scoring_methods
            else "legacy_importance_confidence"
        ),
        "scoring_methods": sorted(scoring_methods),
        "selected_chunk_ids": selected_ids,
        "selected_chunks": selected_chunks,
        "score_summary": {
            "selected_count": len(selected_ids),
            "loaded_selected_count": len(selected_chunks),
            "scored_selected_count": len(composite_scores),
            "avg_composite_score": (
                round(sum(composite_scores) / len(composite_scores), 4)
                if composite_scores else 0.0
            ),
            "max_composite_score": max(composite_scores) if composite_scores else 0.0,
            "min_composite_score": min(composite_scores) if composite_scores else 0.0,
            "user_relevant_selected_count": sum(
                1
                for row in selected_chunks
                if row["score_breakdown"].get("user_relevance", 0.0) > 0.3
            ),
            "used_in_brief_count": sum(
                1 for row in selected_chunks if row.get("used_in_brief")
            ),
        },
        "conflicts": {
            "count": sum(int(getattr(t, "conflict_count", 0) or 0) for t in traces),
            "pairs": conflict_pairs,
            "boosted_chunk_ids": sorted(boosted_ids),
            "breakdown": conflict_breakdown,
        },
        "policy_candidates": policy_candidates,
        "retrieval_traces": trace_rows,
        "query_plan": _get_query_plan_summary(task_id),
    }
