"""
Phase 9.1 Step 8 — Evidence evaluation MVP.

Lightweight, pure-computation metrics for Runtime Micro-RAG quality.
No LLM calls, no external API dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schema import ContextPack, ResearchBrief, RetrievalTrace

try:
    from .schema import RetrievalTrace as _RT
except ImportError:
    from schema import RetrievalTrace as _RT  # type: ignore[no-redef]


@dataclass
class EvidenceEvalSummary:
    task_id: str = ""
    selected_chunk_count: int = 0
    used_chunk_count: int = 0
    average_noise_reduction_ratio: float = 0.0
    citation_coverage: float = 0.0
    estimated_context_reduction_ratio: float | None = None


def compute_context_reduction_ratio(
    original_token_estimate: int,
    context_pack_token_estimate: int,
) -> float:
    if original_token_estimate <= 0:
        return 0.0
    ratio = 1.0 - context_pack_token_estimate / original_token_estimate
    return round(max(0.0, min(1.0, ratio)), 4)


def compute_citation_coverage(brief: ResearchBrief) -> float:
    sections = brief.sections
    if not sections:
        return 0.0
    cited = sum(1 for s in sections if getattr(s, "chunk_ids", None))
    return round(cited / len(sections), 4)


def summarize_retrieval_traces(
    traces: list[RetrievalTrace],
) -> dict[str, float | int]:
    if not traces:
        return {"total_retrieved": 0, "total_available": 0, "avg_noise_reduction": 0.0}

    total_retrieved = sum(
        len(getattr(t, "selected_chunk_ids", []) or []) or t.retrieved_count
        for t in traces
    )
    total_available = sum(t.total_chunks for t in traces)
    sections_covered = sum(1 for t in traces if getattr(t, "section_covered", False) or t.retrieved_count > 0)
    conflict_count = sum(getattr(t, "conflict_count", 0) for t in traces)

    ratios: list[float] = []
    for t in traces:
        if t.total_chunks > 0:
            ratios.append(1.0 - t.retrieved_count / t.total_chunks)

    avg_noise = round(sum(ratios) / len(ratios), 4) if ratios else 0.0

    return {
        "total_retrieved": total_retrieved,
        "total_available": total_available,
        "sections_covered": sections_covered,
        "conflict_count": conflict_count,
        "avg_noise_reduction": avg_noise,
    }


def generate_evidence_eval_summary(
    task_id: str,
    evidence_store: object,
    *,
    brief: ResearchBrief | None = None,
    context_pack: ContextPack | None = None,
    original_token_estimate: int = 0,
) -> EvidenceEvalSummary:
    store = evidence_store
    traces = store.list_traces(task_id) if hasattr(store, "list_traces") else []  # type: ignore[union-attr]

    trace_summary = summarize_retrieval_traces(traces)
    selected = trace_summary["total_retrieved"]

    used_ids: set[str] = set()
    if brief is not None:
        for sec in brief.sections:
            for cid in getattr(sec, "chunk_ids", []):
                used_ids.add(cid)

    cov = compute_citation_coverage(brief) if brief is not None else 0.0

    ctx_ratio: float | None = None
    if context_pack is not None and original_token_estimate > 0:
        ctx_ratio = compute_context_reduction_ratio(
            original_token_estimate, context_pack.total_tokens,
        )

    return EvidenceEvalSummary(
        task_id=task_id,
        selected_chunk_count=int(selected),
        used_chunk_count=len(used_ids),
        average_noise_reduction_ratio=float(trace_summary["avg_noise_reduction"]),
        citation_coverage=cov,
        estimated_context_reduction_ratio=ctx_ratio,
    )
