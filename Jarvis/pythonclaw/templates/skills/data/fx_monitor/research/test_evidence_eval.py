"""
Tests for evidence_eval.py (Phase 9.1 Step 8).

Run: python test_evidence_eval.py [-v]
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

# ── ensure the research package is importable ────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evidence_eval import (
    EvidenceEvalSummary,
    compute_citation_coverage,
    compute_context_reduction_ratio,
    generate_evidence_eval_summary,
    summarize_retrieval_traces,
)
from schema import (
    ContextPack,
    ContextPackItem,
    ResearchBrief,
    ResearchSection,
    RetrievalTrace,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_brief(
    sections: list[ResearchSection] | None = None,
    traces: list[RetrievalTrace] | None = None,
) -> ResearchBrief:
    return ResearchBrief(
        task_id="eval-task-001",
        preset_name="fx_cnyaud",
        sections=sections or [],
        retrieval_traces=traces or [],
    )


def _make_section(title: str = "汇率事实", chunk_ids: list[str] | None = None) -> ResearchSection:
    return ResearchSection(title=title, content="内容", chunk_ids=chunk_ids or [])


def _make_trace(retrieved: int = 5, total: int = 20) -> RetrievalTrace:
    return RetrievalTrace(retrieved_count=retrieved, total_chunks=total)


def _make_context_pack(total_tokens: int = 800, n_items: int = 3) -> ContextPack:
    items = [
        ContextPackItem(chunk_id=f"cp-{i}", agent_name="fx_agent",
                        text="x", token_estimate=total_tokens // max(n_items, 1))
        for i in range(n_items)
    ]
    return ContextPack(items=items, total_tokens=total_tokens, budget_tokens=4000)


class _StubStore:
    def __init__(self, traces: list[RetrievalTrace] | None = None):
        self._traces = traces or []

    def list_traces(self, task_id: str) -> list[RetrievalTrace]:
        return self._traces


# ── compute_context_reduction_ratio ──────────────────────────────────────────

class TestContextReductionRatio(unittest.TestCase):

    def test_basic(self):
        self.assertAlmostEqual(
            compute_context_reduction_ratio(1000, 200), 0.8,
        )

    def test_no_reduction(self):
        self.assertAlmostEqual(
            compute_context_reduction_ratio(1000, 1000), 0.0,
        )

    def test_full_reduction(self):
        self.assertAlmostEqual(
            compute_context_reduction_ratio(1000, 0), 1.0,
        )

    def test_zero_original(self):
        self.assertEqual(compute_context_reduction_ratio(0, 500), 0.0)

    def test_clamp_negative(self):
        r = compute_context_reduction_ratio(100, 200)
        self.assertEqual(r, 0.0)


# ── compute_citation_coverage ────────────────────────────────────────────────

class TestCitationCoverage(unittest.TestCase):

    def test_all_cited(self):
        brief = _make_brief(sections=[
            _make_section(chunk_ids=["c1"]),
            _make_section(title="新闻驱动", chunk_ids=["c2"]),
        ])
        self.assertAlmostEqual(compute_citation_coverage(brief), 1.0)

    def test_partial(self):
        brief = _make_brief(sections=[
            _make_section(chunk_ids=["c1"]),
            _make_section(title="新闻驱动"),
        ])
        self.assertAlmostEqual(compute_citation_coverage(brief), 0.5)

    def test_none_cited(self):
        brief = _make_brief(sections=[_make_section(), _make_section(title="新闻驱动")])
        self.assertAlmostEqual(compute_citation_coverage(brief), 0.0)

    def test_no_sections(self):
        brief = _make_brief(sections=[])
        self.assertAlmostEqual(compute_citation_coverage(brief), 0.0)


# ── summarize_retrieval_traces ───────────────────────────────────────────────

class TestSummarizeTraces(unittest.TestCase):

    def test_empty(self):
        result = summarize_retrieval_traces([])
        self.assertEqual(result["total_retrieved"], 0)
        self.assertAlmostEqual(result["avg_noise_reduction"], 0.0)

    def test_single_trace(self):
        result = summarize_retrieval_traces([_make_trace(5, 20)])
        self.assertEqual(result["total_retrieved"], 5)
        self.assertEqual(result["total_available"], 20)
        self.assertAlmostEqual(result["avg_noise_reduction"], 0.75)

    def test_multiple_traces(self):
        traces = [_make_trace(2, 10), _make_trace(4, 20)]
        result = summarize_retrieval_traces(traces)
        self.assertEqual(result["total_retrieved"], 6)
        self.assertEqual(result["total_available"], 30)
        # (0.8 + 0.8) / 2 = 0.8
        self.assertAlmostEqual(result["avg_noise_reduction"], 0.8)

    def test_zero_total_chunks(self):
        traces = [_make_trace(0, 0)]
        result = summarize_retrieval_traces(traces)
        self.assertAlmostEqual(result["avg_noise_reduction"], 0.0)


# ── generate_evidence_eval_summary ───────────────────────────────────────────

class TestGenerateEvalSummary(unittest.TestCase):

    def test_full_summary(self):
        traces = [_make_trace(5, 20), _make_trace(3, 10)]
        store = _StubStore(traces)
        brief = _make_brief(sections=[
            _make_section(chunk_ids=["c1", "c2"]),
            _make_section(title="新闻驱动", chunk_ids=["c3"]),
            _make_section(title="宏观信号"),
        ])
        pack = _make_context_pack(total_tokens=800)

        summary = generate_evidence_eval_summary(
            "eval-task-001", store,
            brief=brief, context_pack=pack,
            original_token_estimate=4000,
        )
        self.assertEqual(summary.task_id, "eval-task-001")
        self.assertEqual(summary.selected_chunk_count, 8)
        self.assertEqual(summary.used_chunk_count, 3)
        self.assertAlmostEqual(summary.citation_coverage, 0.6667, places=3)
        self.assertAlmostEqual(summary.estimated_context_reduction_ratio, 0.8)
        self.assertGreater(summary.average_noise_reduction_ratio, 0.0)

    def test_no_brief(self):
        store = _StubStore([_make_trace(4, 16)])
        summary = generate_evidence_eval_summary("t1", store)
        self.assertEqual(summary.used_chunk_count, 0)
        self.assertAlmostEqual(summary.citation_coverage, 0.0)
        self.assertIsNone(summary.estimated_context_reduction_ratio)

    def test_no_context_pack(self):
        store = _StubStore([])
        brief = _make_brief(sections=[_make_section(chunk_ids=["c1"])])
        summary = generate_evidence_eval_summary("t2", store, brief=brief)
        self.assertIsNone(summary.estimated_context_reduction_ratio)
        self.assertEqual(summary.used_chunk_count, 1)

    def test_empty_store(self):
        store = _StubStore([])
        summary = generate_evidence_eval_summary("t3", store)
        self.assertEqual(summary.selected_chunk_count, 0)
        self.assertEqual(summary.used_chunk_count, 0)
        self.assertAlmostEqual(summary.average_noise_reduction_ratio, 0.0)

    def test_dedup_used_chunks(self):
        store = _StubStore([_make_trace(6, 20)])
        brief = _make_brief(sections=[
            _make_section(chunk_ids=["c1", "c2"]),
            _make_section(title="新闻驱动", chunk_ids=["c1"]),
        ])
        summary = generate_evidence_eval_summary("t4", store, brief=brief)
        self.assertEqual(summary.used_chunk_count, 2)


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Phase 9.1 Step 8 — evidence_eval tests")
    print("=" * 60)
    unittest.main(verbosity=2)
