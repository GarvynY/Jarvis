"""Helpers for the FX research browser debug API."""

from __future__ import annotations

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
    unavailable_error = ""
    try:
        with evidence_store_cls() as store:
            for chunk_id in selected_ids:
                chunk = store.get_chunk(chunk_id)
                if chunk is not None:
                    selected_chunks.append(phase10_chunk_debug(chunk))
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
        },
        "retrieval_traces": trace_rows,
        "query_plan": _get_query_plan_summary(task_id),
    }
