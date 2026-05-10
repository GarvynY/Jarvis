#!/usr/bin/env python3
"""
Phase 9.1 Step 2 — EvidenceStore SQLite MVP 测试。

验证：
  1.  插入/获取 EvidenceChunk 完整往返
  2.  按 category 过滤
  3.  按 entities 过滤
  4.  按 min_importance 过滤
  5.  按 agent_name 过滤
  6.  按 source_type 过滤
  7.  按 time_after 过滤
  8.  mark_used_in_brief 批量标记
  9.  插入/获取 EvidenceFinding
  10. 插入/获取 CitationRef（通过 insert_citation）
  11. 插入/列出 RetrievalTrace
  12. top_k 限制
  13. 重复插入（REPLACE）
  14. 不存在的 chunk_id 返回 None
  15. delete_task 清理
  16. count_chunks 统计
  17. 内存模式与上下文管理器

运行：
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_evidence_store.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput,
    CitationRef,
    ContextPack,
    EvidenceChunk,
    EvidenceFinding,
    Finding,
    FX_CNYAUD_PRESET,
    ResearchPreset,
    ResearchTask,
    RetrievalTrace,
    SourceRef,
    now_iso,
)
from evidence_store import EvidenceStore  # noqa: E402


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _make_chunk(
    chunk_id: str = "chunk-1",
    task_id: str = "task-a",
    agent_name: str = "fx_agent",
    content: str = "测试内容",
    category: str = "fx_price",
    importance: float = 0.5,
    confidence: float = 0.6,
    entities: list[str] | None = None,
    source: str | None = "er-api.com",
    created_at: str | None = None,
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id,
        task_id=task_id,
        preset_name="fx_cnyaud",
        agent_name=agent_name,
        content=content,
        source=source,
        category=category,
        importance=importance,
        confidence=confidence,
        entities=entities or [],
        created_at=created_at or now_iso(),
        token_estimate=len(content),
    )


# ── 测试 ─────────────────────────────────────────────────────────────────────

def test_insert_and_get_chunk() -> None:
    with EvidenceStore(":memory:") as store:
        chunk = _make_chunk(entities=["AUD", "CNY"])
        store.insert_chunk(chunk)

        got = store.get_chunk("chunk-1")
        assert got is not None
        assert got.chunk_id == "chunk-1"
        assert got.task_id == "task-a"
        assert got.preset_name == "fx_cnyaud"
        assert got.agent_name == "fx_agent"
        assert got.content == "测试内容"
        assert got.source == "er-api.com"
        assert got.category == "fx_price"
        assert got.importance == 0.5
        assert got.confidence == 0.6
        assert got.entities == ["AUD", "CNY"]
        assert got.used_in_brief is False
        assert got.ttl_policy == "task"
    print("  插入/获取 chunk          OK")


def test_get_nonexistent_chunk() -> None:
    with EvidenceStore(":memory:") as store:
        assert store.get_chunk("不存在") is None
    print("  不存在 chunk 返回 None   OK")


def test_filter_by_category() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", category="fx_price"))
        store.insert_chunk(_make_chunk(chunk_id="c2", category="macro"))
        store.insert_chunk(_make_chunk(chunk_id="c3", category="fx_price"))

        results = store.query_chunks("task-a", category="fx_price")
        assert len(results) == 2
        assert all(c.category == "fx_price" for c in results)

        results2 = store.query_chunks("task-a", category="macro")
        assert len(results2) == 1
        assert results2[0].chunk_id == "c2"
    print("  按 category 过滤        OK")


def test_filter_by_entities() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", entities=["AUD", "CNY"]))
        store.insert_chunk(_make_chunk(chunk_id="c2", entities=["USD", "EUR"]))
        store.insert_chunk(_make_chunk(chunk_id="c3", entities=["AUD", "RBA"]))

        results = store.query_chunks("task-a", entities=["AUD"])
        assert len(results) == 2
        ids = {c.chunk_id for c in results}
        assert ids == {"c1", "c3"}

        results2 = store.query_chunks("task-a", entities=["EUR"])
        assert len(results2) == 1
        assert results2[0].chunk_id == "c2"

        results3 = store.query_chunks("task-a", entities=["JPY"])
        assert len(results3) == 0
    print("  按 entities 过滤        OK")


def test_filter_by_min_importance() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", importance=0.3))
        store.insert_chunk(_make_chunk(chunk_id="c2", importance=0.7))
        store.insert_chunk(_make_chunk(chunk_id="c3", importance=0.9))

        results = store.query_chunks("task-a", min_importance=0.5)
        assert len(results) == 2
        ids = {c.chunk_id for c in results}
        assert ids == {"c2", "c3"}

        results2 = store.query_chunks("task-a", min_importance=0.8)
        assert len(results2) == 1
        assert results2[0].chunk_id == "c3"
    print("  按 min_importance 过滤  OK")


def test_filter_by_agent_name() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", agent_name="fx_agent"))
        store.insert_chunk(_make_chunk(chunk_id="c2", agent_name="news_agent"))
        store.insert_chunk(_make_chunk(chunk_id="c3", agent_name="fx_agent"))

        results = store.query_chunks("task-a", agent_name="news_agent")
        assert len(results) == 1
        assert results[0].chunk_id == "c2"
    print("  按 agent_name 过滤      OK")


def test_filter_by_source_type() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", source="tavily"))
        store.insert_chunk(_make_chunk(chunk_id="c2", source="google_news_rss"))
        store.insert_chunk(_make_chunk(chunk_id="c3", source="tavily"))

        results = store.query_chunks("task-a", source_type="tavily")
        assert len(results) == 2
        assert all(c.source == "tavily" for c in results)
    print("  按 source_type 过滤     OK")


def test_filter_by_time_after() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", created_at="2026-01-01T00:00:00+00:00"))
        store.insert_chunk(_make_chunk(chunk_id="c2", created_at="2026-06-01T00:00:00+00:00"))
        store.insert_chunk(_make_chunk(chunk_id="c3", created_at="2026-12-01T00:00:00+00:00"))

        results = store.query_chunks("task-a", time_after="2026-05-01T00:00:00+00:00")
        assert len(results) == 2
        ids = {c.chunk_id for c in results}
        assert ids == {"c2", "c3"}
    print("  按 time_after 过滤      OK")


def test_combined_filters() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", category="fx_price", importance=0.9, agent_name="fx_agent"))
        store.insert_chunk(_make_chunk(chunk_id="c2", category="fx_price", importance=0.3, agent_name="fx_agent"))
        store.insert_chunk(_make_chunk(chunk_id="c3", category="macro", importance=0.9, agent_name="macro_agent"))

        results = store.query_chunks("task-a", category="fx_price", min_importance=0.5)
        assert len(results) == 1
        assert results[0].chunk_id == "c1"
    print("  组合过滤                OK")


def test_top_k_limit() -> None:
    with EvidenceStore(":memory:") as store:
        for i in range(10):
            store.insert_chunk(_make_chunk(
                chunk_id=f"c{i}",
                importance=i * 0.1,
            ))
        results = store.query_chunks("task-a", top_k=3)
        assert len(results) == 3
        assert results[0].importance >= results[1].importance >= results[2].importance
    print("  top_k 限制              OK")


def test_importance_order() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="low", importance=0.1))
        store.insert_chunk(_make_chunk(chunk_id="high", importance=0.9))
        store.insert_chunk(_make_chunk(chunk_id="mid", importance=0.5))

        results = store.query_chunks("task-a", top_k=10)
        assert results[0].chunk_id == "high"
        assert results[1].chunk_id == "mid"
        assert results[2].chunk_id == "low"
    print("  importance 降序排列     OK")


def test_mark_used_in_brief() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1"))
        store.insert_chunk(_make_chunk(chunk_id="c2"))
        store.insert_chunk(_make_chunk(chunk_id="c3"))

        updated = store.mark_used_in_brief(["c1", "c3"])
        assert updated == 2

        c1 = store.get_chunk("c1")
        c2 = store.get_chunk("c2")
        c3 = store.get_chunk("c3")
        assert c1 is not None and c1.used_in_brief is True
        assert c2 is not None and c2.used_in_brief is False
        assert c3 is not None and c3.used_in_brief is True

        assert store.mark_used_in_brief([]) == 0
    print("  mark_used_in_brief      OK")


def test_insert_and_get_finding() -> None:
    with EvidenceStore(":memory:") as store:
        finding = EvidenceFinding(
            finding_id="find-1",
            agent_name="news_agent",
            key="rba_hold",
            summary="RBA 维持利率不变",
            direction="bearish_aud",
            chunk_ids=["c1", "c2"],
            evidence_score=0.75,
            category="macro",
            importance=0.8,
        )
        store.insert_finding(finding, task_id="task-a")

        got = store.get_finding("find-1")
        assert got is not None
        assert got.finding_id == "find-1"
        assert got.key == "rba_hold"
        assert got.summary == "RBA 维持利率不变"
        assert got.direction == "bearish_aud"
        assert got.chunk_ids == ["c1", "c2"]
        assert got.evidence_score == 0.75
        assert got.category == "macro"
        assert got.importance == 0.8

        assert store.get_finding("不存在") is None
    print("  插入/获取 finding       OK")


def test_insert_and_get_citation() -> None:
    with EvidenceStore(":memory:") as store:
        citation = CitationRef(
            citation_id="cite-1",
            chunk_id="c1",
            finding_id="find-1",
            section_title="汇率事实",
            relevance_score=0.9,
        )
        store.insert_citation(citation, task_id="task-a")

        got = store.get_citation("cite-1")
        assert got is not None
        assert got.citation_id == "cite-1"
        assert got.chunk_id == "c1"
        assert got.finding_id == "find-1"
        assert got.section_title == "汇率事实"
        assert got.relevance_score == 0.9

        assert store.get_citation("不存在") is None
    print("  插入/获取 citation      OK")


def test_insert_and_list_traces() -> None:
    with EvidenceStore(":memory:") as store:
        t1 = RetrievalTrace(
            trace_id="t1",
            query="CNY/AUD 汇率",
            retrieved_count=5,
            total_chunks=20,
            top_scores=[0.9, 0.8, 0.7, 0.6, 0.5],
            latency_ms=42,
        )
        t2 = RetrievalTrace(
            trace_id="t2",
            query="RBA 利率",
            retrieved_count=3,
            total_chunks=20,
            top_scores=[0.85, 0.7, 0.55],
            latency_ms=38,
        )
        store.insert_trace(t1, task_id="task-a")
        store.insert_trace(t2, task_id="task-a")

        traces = store.list_traces("task-a")
        assert len(traces) == 2
        assert traces[0].trace_id == "t1"
        assert traces[0].query == "CNY/AUD 汇率"
        assert traces[0].top_scores == [0.9, 0.8, 0.7, 0.6, 0.5]
        assert traces[1].trace_id == "t2"

        assert store.list_traces("不存在") == []
    print("  插入/列出 traces        OK")


def test_replace_on_duplicate() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", content="版本1"))
        store.insert_chunk(_make_chunk(chunk_id="c1", content="版本2"))

        got = store.get_chunk("c1")
        assert got is not None
        assert got.content == "版本2"
        assert store.count_chunks() == 1
    print("  重复插入 REPLACE        OK")


def test_delete_task() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", task_id="task-a"))
        store.insert_chunk(_make_chunk(chunk_id="c2", task_id="task-a"))
        store.insert_chunk(_make_chunk(chunk_id="c3", task_id="task-b"))
        store.insert_trace(
            RetrievalTrace(trace_id="t1", query="q"), task_id="task-a"
        )
        store.insert_finding(
            EvidenceFinding(finding_id="f1", key="k"), task_id="task-a"
        )
        store.insert_finding(
            EvidenceFinding(finding_id="f2", key="k"), task_id="task-b"
        )
        store.insert_citation(
            CitationRef(citation_id="ci1", chunk_id="c1"), task_id="task-a"
        )

        deleted = store.delete_task("task-a")
        assert deleted == 2
        assert store.count_chunks("task-a") == 0
        assert store.count_chunks("task-b") == 1
        assert store.list_traces("task-a") == []
        assert store.get_finding("f1") is None
        assert store.get_finding("f2") is not None
        assert store.get_citation("ci1") is None
    print("  delete_task 清理        OK")


def test_count_chunks() -> None:
    with EvidenceStore(":memory:") as store:
        assert store.count_chunks() == 0
        store.insert_chunk(_make_chunk(chunk_id="c1", task_id="task-a"))
        store.insert_chunk(_make_chunk(chunk_id="c2", task_id="task-b"))
        assert store.count_chunks() == 2
        assert store.count_chunks("task-a") == 1
    print("  count_chunks 统计       OK")


def test_sort_stability_same_importance() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(
            chunk_id="c1", importance=0.5, confidence=0.3,
            created_at="2026-01-01T00:00:00+00:00",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="c2", importance=0.5, confidence=0.9,
            created_at="2026-01-01T00:00:00+00:00",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="c3", importance=0.5, confidence=0.9,
            created_at="2026-06-01T00:00:00+00:00",
        ))

        results = store.query_chunks("task-a", top_k=10)
        assert results[0].chunk_id == "c3"
        assert results[1].chunk_id == "c2"
        assert results[2].chunk_id == "c1"
    print("  排序稳定性(同importance) OK")


def test_task_isolation() -> None:
    with EvidenceStore(":memory:") as store:
        store.insert_chunk(_make_chunk(chunk_id="c1", task_id="task-a"))
        store.insert_chunk(_make_chunk(chunk_id="c2", task_id="task-b"))

        results = store.query_chunks("task-a")
        assert len(results) == 1
        assert results[0].chunk_id == "c1"
    print("  任务隔离                OK")


# ── ingest_outputs 测试 ──────────────────────────────────────────────────────

def _make_task(task_id: str = "task-a", preset_name: str = "fx_cnyaud") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        preset_name=preset_name,
        research_type="fx",
        research_topic="CNY/AUD 外汇研究",
        focus_assets=["CNY", "AUD"],
        focus_pair="CNY/AUD",
    )


def _make_output_with_findings(
    agent_name: str = "fx_agent",
    n_findings: int = 2,
    sources: list[SourceRef] | None = None,
) -> AgentOutput:
    findings = []
    for i in range(n_findings):
        findings.append(Finding(
            key=f"finding_{i}",
            summary=f"发现 {i} 的摘要内容",
            direction="bullish_aud" if i % 2 == 0 else "bearish_aud",
            category="fx_price" if i % 2 == 0 else "macro",
            importance=round(0.5 + i * 0.1, 1),
            source_ids=[s.url for s in (sources or [])] if sources else [],
        ))
    return AgentOutput(
        agent_name=agent_name,
        status="ok",
        summary="测试摘要",
        findings=findings,
        sources=sources or [],
        confidence=0.8,
    )


def test_ingest_two_findings() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = _make_output_with_findings(n_findings=2)

        results = store.ingest_outputs(task, [output])
        assert len(results) == 1
        enriched = results[0]

        assert len(enriched.chunk_ids) == 2
        assert len(enriched.finding_ids) == 2
        assert enriched.evidence_count == 2

        for cid in enriched.chunk_ids:
            chunk = store.get_chunk(cid)
            assert chunk is not None
            assert chunk.task_id == "task-a"
            assert chunk.preset_name == "fx_cnyaud"
            assert chunk.agent_name == "fx_agent"
            assert "[Context]" in chunk.content
            assert "[/Context]" in chunk.content
            assert chunk.entities == ["CNY", "AUD"]

        for fid in enriched.finding_ids:
            f = store.get_finding(fid)
            assert f is not None
            assert f.agent_name == "fx_agent"
            assert len(f.chunk_ids) == 1
    print("  ingest 两个发现         OK")


def test_ingest_preserves_original() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = _make_output_with_findings(n_findings=1)
        original_chunk_ids = output.chunk_ids.copy()

        results = store.ingest_outputs(task, [output])

        assert output.chunk_ids == original_chunk_ids
        assert output.evidence_count == 0
        assert results[0].evidence_count == 1
    print("  ingest 不修改原始对象   OK")


def test_ingest_with_source_metadata() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        sources = [
            SourceRef(
                title="RBA 利率决议",
                url="https://rba.gov.au/rates",
                source="rba_official",
                retrieved_at=now_iso(),
            ),
        ]
        output = _make_output_with_findings(
            n_findings=1,
            sources=sources,
        )
        output.findings[0].source_ids = ["https://rba.gov.au/rates"]

        results = store.ingest_outputs(task, [output])
        chunk = store.get_chunk(results[0].chunk_ids[0])
        assert chunk is not None
        assert chunk.source == "rba_official"
        assert "rba_official" in chunk.content
    print("  ingest 保留来源元数据   OK")


def test_ingest_default_category_importance() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = AgentOutput(
            agent_name="test_agent",
            status="ok",
            findings=[Finding(key="bare", summary="无类别发现")],
            confidence=0.5,
        )

        results = store.ingest_outputs(task, [output])
        chunk = store.get_chunk(results[0].chunk_ids[0])
        assert chunk is not None
        assert chunk.category == ""
        assert chunk.importance == 0.5
        assert chunk.confidence == 0.5
        assert chunk.source is None
        assert "未分类" in chunk.content
        assert "未知" in chunk.content
    print("  ingest 默认值安全       OK")


def test_ingest_context_header_format() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = _make_output_with_findings(n_findings=1)

        results = store.ingest_outputs(task, [output])
        chunk = store.get_chunk(results[0].chunk_ids[0])
        assert chunk is not None

        lines = chunk.content.split("\n")
        assert lines[0] == "[Context]"
        assert lines[1].startswith("任务：")
        assert lines[2].startswith("预设：")
        assert lines[3].startswith("代理：")
        assert lines[4].startswith("类别：")
        assert lines[5].startswith("实体：")
        assert lines[6].startswith("来源：")
        assert lines[7].startswith("检索时间：")
        assert lines[8] == "[/Context]"
        assert lines[9] == output.findings[0].summary
    print("  ingest 上下文标头格式   OK")


def test_ingest_multiple_outputs() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        out1 = _make_output_with_findings(agent_name="fx_agent", n_findings=2)
        out2 = _make_output_with_findings(agent_name="news_agent", n_findings=1)

        results = store.ingest_outputs(task, [out1, out2])
        assert len(results) == 2
        assert results[0].evidence_count == 2
        assert results[1].evidence_count == 1
        assert store.count_chunks("task-a") == 3
    print("  ingest 多个 output      OK")


def test_ingest_error_output() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = AgentOutput.make_error("fx_agent", error="超时")

        results = store.ingest_outputs(task, [output])
        assert len(results) == 1
        enriched = results[0]
        assert enriched.chunk_ids == []
        assert enriched.finding_ids == []
        assert enriched.evidence_count == 0
        assert enriched.error == "超时"
        assert store.count_chunks("task-a") == 0
    print("  ingest error 输出       OK")


def test_ingest_empty_findings() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            summary="无发现",
            findings=[],
            confidence=0.7,
        )

        results = store.ingest_outputs(task, [output])
        assert len(results) == 1
        enriched = results[0]
        assert enriched.chunk_ids == []
        assert enriched.finding_ids == []
        assert enriched.evidence_count == 0
        assert enriched.summary == "无发现"
        assert store.count_chunks("task-a") == 0
    print("  ingest 空 findings      OK")


# ── build_context_pack 测试 ──────────────────────────────────────────────────

def _seed_chunks_for_pack(store: EvidenceStore, task_id: str = "task-a") -> None:
    """插入覆盖多类别的 chunks 用于 context pack 测试。"""
    chunks = [
        _make_chunk(chunk_id="fx1", task_id=task_id, agent_name="fx_agent",
                    category="fx_price", importance=0.9, confidence=0.8,
                    content="A" * 50, source="src-fx-1"),
        _make_chunk(chunk_id="fx2", task_id=task_id, agent_name="fx_agent",
                    category="fx_price", importance=0.7, confidence=0.6,
                    content="B" * 40, source="src-fx-2"),
        _make_chunk(chunk_id="news1", task_id=task_id, agent_name="news_agent",
                    category="news_event", importance=0.8, confidence=0.7,
                    content="C" * 60, source="src-news-1"),
        _make_chunk(chunk_id="macro1", task_id=task_id, agent_name="macro_agent",
                    category="macro", importance=0.85, confidence=0.9,
                    content="D" * 45, source="src-macro-1"),
        _make_chunk(chunk_id="macro2", task_id=task_id, agent_name="macro_agent",
                    category="macro", importance=0.6, confidence=0.5,
                    content="E" * 30, source="src-macro-2"),
        _make_chunk(chunk_id="risk1", task_id=task_id, agent_name="risk_agent",
                    category="risk", importance=0.75, confidence=0.65,
                    content="F" * 35, source="src-risk-1"),
    ]
    for c in chunks:
        store.insert_chunk(c)


def test_pack_section_based_retrieval() -> None:
    preset = FX_CNYAUD_PRESET
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        _seed_chunks_for_pack(store)

        pack = store.build_context_pack(task, preset, [])
        assert isinstance(pack, ContextPack)
        assert len(pack.items) > 0

        chunk_ids = [it.chunk_id for it in pack.items]
        assert "fx1" in chunk_ids
        assert "news1" in chunk_ids
        assert "macro1" in chunk_ids
        assert "risk1" in chunk_ids
    print("  pack 按章节检索          OK")


def test_pack_max_chunks_per_section() -> None:
    single_section_preset = ResearchPreset(
        name="single",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率数据"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        for i in range(10):
            store.insert_chunk(_make_chunk(
                chunk_id=f"fx{i}", task_id="task-a", category="fx_price",
                importance=round(0.5 + i * 0.04, 2), content=f"chunk{i}" * 5,
                source=f"src-{i}",
            ))

        pack = store.build_context_pack(
            task, single_section_preset, [], max_chunks_per_section=3, token_budget=99999,
        )
        assert len(pack.items) == 3
    print("  pack max_chunks_per_sec  OK")


def test_pack_fallback_retrieval() -> None:
    custom_preset = ResearchPreset(
        name="test_preset",
        research_type="custom",
        default_agents=["agent_a"],
        report_sections=["完全无关的章节"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="only1", task_id="task-a", category="fx_price",
            importance=0.9, content="唯一数据" * 5,
        ))

        pack = store.build_context_pack(task, custom_preset, [])
        assert len(pack.items) >= 1
        assert pack.items[0].chunk_id == "only1"
    print("  pack 回退检索            OK")


def test_pack_dedup_by_chunk_id() -> None:
    preset = ResearchPreset(
        name="dup_test",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率A", "汇率B"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="shared", task_id="task-a", category="fx_price",
            importance=0.9, content="共享数据" * 5, source="unique-src",
        ))

        pack = store.build_context_pack(task, preset, [])
        chunk_ids = [it.chunk_id for it in pack.items]
        assert chunk_ids.count("shared") == 1
    print("  pack chunk_id 去重       OK")


def test_pack_dedup_by_source() -> None:
    preset = FX_CNYAUD_PRESET
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="a1", task_id="task-a", category="fx_price",
            importance=0.9, content="X" * 30, source="same-source",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="a2", task_id="task-a", category="fx_price",
            importance=0.8, content="Y" * 30, source="same-source",
        ))

        pack = store.build_context_pack(task, preset, [])
        pack_ids = [it.chunk_id for it in pack.items]
        assert "a1" in pack_ids
        assert "a2" not in pack_ids
    print("  pack source 去重         OK")


def test_pack_token_budget() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        for i in range(5):
            store.insert_chunk(_make_chunk(
                chunk_id=f"big{i}", task_id="task-a", category="fx_price",
                importance=0.9 - i * 0.05, content="X" * 100,
                source=f"src-big-{i}",
            ))

        pack = store.build_context_pack(
            task, FX_CNYAUD_PRESET, [], token_budget=250,
        )
        assert pack.total_tokens <= 250
        assert pack.budget_tokens == 250
        assert len(pack.items) <= 3
    print("  pack token 预算          OK")


def test_pack_traces_created() -> None:
    preset = FX_CNYAUD_PRESET
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        _seed_chunks_for_pack(store)

        pack = store.build_context_pack(task, preset, [])

        traces = store.list_traces("task-a")
        assert len(traces) == len(preset.report_sections)
        for t in traces:
            assert "section=" in t.query
            assert "pre_dedup=" in t.query
            assert "selected=" in t.query
            assert "noise_rate=" in t.query
            assert "chunk_ids=" in t.query
            assert t.total_chunks == 6
            assert t.retrieved_count >= 0
    print("  pack 检索追踪已创建      OK")


def test_pack_coverage() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        _seed_chunks_for_pack(store)

        pack = store.build_context_pack(task, FX_CNYAUD_PRESET, [])
        assert isinstance(pack.coverage, dict)
        total_from_coverage = sum(pack.coverage.values())
        assert total_from_coverage == len(pack.items)
    print("  pack coverage 统计       OK")


def test_pack_intra_section_dedup() -> None:
    preset = ResearchPreset(
        name="dual_keyword",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率rate综合"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="dup1", task_id="task-a", category="fx_price",
            importance=0.9, content="Z" * 20, source="src-dup",
        ))

        pack = store.build_context_pack(task, preset, [])
        ids = [it.chunk_id for it in pack.items]
        assert ids.count("dup1") == 1
    print("  pack 节内候选去重        OK")


def test_pack_empty_store() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        pack = store.build_context_pack(task, FX_CNYAUD_PRESET, [])
        assert len(pack.items) == 0
        assert pack.total_tokens == 0
    print("  pack 空存储              OK")


# ── 运行 ─────────────────────────────────────────────────────────────────────

def run_all() -> None:
    tests = [
        test_insert_and_get_chunk,
        test_get_nonexistent_chunk,
        test_filter_by_category,
        test_filter_by_entities,
        test_filter_by_min_importance,
        test_filter_by_agent_name,
        test_filter_by_source_type,
        test_filter_by_time_after,
        test_combined_filters,
        test_top_k_limit,
        test_importance_order,
        test_mark_used_in_brief,
        test_insert_and_get_finding,
        test_insert_and_get_citation,
        test_insert_and_list_traces,
        test_replace_on_duplicate,
        test_delete_task,
        test_count_chunks,
        test_sort_stability_same_importance,
        test_task_isolation,
        # Phase 9.1 Step 3 — ingest_outputs
        test_ingest_two_findings,
        test_ingest_preserves_original,
        test_ingest_with_source_metadata,
        test_ingest_default_category_importance,
        test_ingest_context_header_format,
        test_ingest_multiple_outputs,
        test_ingest_error_output,
        test_ingest_empty_findings,
        # Phase 9.1 Step 5 — build_context_pack
        test_pack_section_based_retrieval,
        test_pack_max_chunks_per_section,
        test_pack_fallback_retrieval,
        test_pack_dedup_by_chunk_id,
        test_pack_dedup_by_source,
        test_pack_token_budget,
        test_pack_traces_created,
        test_pack_coverage,
        test_pack_intra_section_dedup,
        test_pack_empty_store,
    ]
    print("Phase 9.1 Step 5 — EvidenceStore 测试")
    print("=" * 50)
    for test_fn in tests:
        test_fn()
    print("=" * 50)
    print(f"全部 {len(tests)} 项测试通过。")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        print(f"\n失败: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
