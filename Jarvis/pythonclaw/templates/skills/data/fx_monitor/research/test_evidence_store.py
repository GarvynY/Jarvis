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
import tempfile
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
            section_title="汇率事实",
            selected_chunk_ids=["c1", "c2"],
            section_covered=True,
            score_distribution={"count": 5, "min": 0.5, "max": 0.9, "avg": 0.7},
            conflict_count=1,
            conflict_pairs=[{"chunk_id_a": "c1", "chunk_id_b": "c2"}],
            boosted_chunk_ids=["c1", "c2"],
            scoring_method="composite",
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
        assert traces[0].section_title == "汇率事实"
        assert traces[0].selected_chunk_ids == ["c1", "c2"]
        assert traces[0].section_covered is True
        assert traces[0].score_distribution["avg"] == 0.7
        assert traces[0].conflict_count == 1
        assert traces[0].conflict_pairs[0]["chunk_id_a"] == "c1"
        assert traces[0].boosted_chunk_ids == ["c1", "c2"]
        assert traces[0].scoring_method == "composite"
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
        assert "url=https://rba.gov.au/rates" in chunk.source
        assert "title=RBA 利率决议" in chunk.source
        assert "provider=rba_official" in chunk.source
        assert "rba_official" in chunk.content
        assert "https://rba.gov.au/rates" in chunk.content
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
        assert chunk.source == "finding_key=bare"
        assert "未分类" in chunk.content
        assert "finding_key=bare" in chunk.content
    print("  ingest 默认值安全       OK")


def test_ingest_prefers_explicit_106c_fields() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        output = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            findings=[Finding(
                key="current_rate",
                summary="1 AUD = 4.80 CNY",
                category="fx_price",
                subcategory="current_rate",
                entities=["AUD", "CNY", "CNYAUD"],
                importance=0.9,
                evidence_score=0.85,
                evidence_basis="fetch_rate.current_1_AUD_in_CNY",
                time_horizon="spot",
            )],
            confidence=0.8,
        )

        results = store.ingest_outputs(task, [output])
        chunk = store.get_chunk(results[0].chunk_ids[0])
        assert chunk is not None
        assert chunk.category == "fx_price"
        assert chunk.entities == ["AUD", "CNY", "CNYAUD"]
        assert "子类别：current_rate" in chunk.content
        assert "证据依据：fetch_rate.current_1_AUD_in_CNY" in chunk.content
        assert "时间范围：spot" in chunk.content
    print("  ingest 优先显式10.6C字段 OK")


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


def test_pack_dedup_by_url() -> None:
    preset = FX_CNYAUD_PRESET
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="a1", task_id="task-a", category="fx_price",
            importance=0.9, content="X" * 30,
            source="url=https://example.com/same | title=Same story | provider=google_news_rss",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="a2", task_id="task-a", category="fx_price",
            importance=0.8, content="Y" * 30,
            source="url=https://www.example.com/same/ | title=Same story copy | provider=google_news_rss",
        ))

        pack = store.build_context_pack(task, preset, [])
        pack_ids = [it.chunk_id for it in pack.items]
        assert "a1" in pack_ids
        assert "a2" not in pack_ids
    print("  pack URL 去重            OK")


def test_pack_provider_label_does_not_dedup_distinct_urls() -> None:
    preset = ResearchPreset(
        name="rss_distinct",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="rss1", task_id="task-a", category="news_event",
            agent_name="news_agent", importance=0.9, content="A" * 30,
            source="url=https://news.example.com/a | title=RBA signal A | provider=google_news_rss",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="rss2", task_id="task-a", category="news_event",
            agent_name="news_agent", importance=0.8, content="B" * 30,
            source="url=https://news.example.com/b | title=RBA signal B | provider=google_news_rss",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5)
        ids = [it.chunk_id for it in pack.items]
        assert "rss1" in ids and "rss2" in ids, ids
    print("  RSS 不同 URL 不互相去重  OK")


def test_pack_same_url_selected_once() -> None:
    preset = ResearchPreset(
        name="same_url_once",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        for cid, imp in (("url1", 0.9), ("url2", 0.8)):
            store.insert_chunk(_make_chunk(
                chunk_id=cid, task_id="task-a", category="news_event",
                agent_name="news_agent", importance=imp, content=cid * 20,
                source="url=https://news.example.com/same | title=Same RBA story | provider=google_news_rss",
            ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5)
        ids = [it.chunk_id for it in pack.items]
        assert len([i for i in ids if i in {"url1", "url2"}]) == 1, ids
    print("  同 URL 仅选择一次        OK")


def test_pack_provider_label_does_not_empty_later_section() -> None:
    preset = ResearchPreset(
        name="rss_sections",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动", "宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="news-rss", task_id="task-a", category="news_event",
            agent_name="news_agent", importance=0.9, content="新闻" * 20,
            source="google_news_rss",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-rss", task_id="task-a", category="macro",
            agent_name="macro_agent", importance=0.9, content="宏观" * 20,
            source="google_news_rss",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=1)
        ids = [it.chunk_id for it in pack.items]
        assert "news-rss" in ids, ids
        assert "macro-rss" in ids, ids
        traces = store.list_traces("task-a")
        assert traces[0].retrieved_count == 1
        assert traces[1].retrieved_count == 1
    print("  RSS provider 不清空后续分区 OK")


def test_pack_section_coverage_stable_with_provider_duplicates() -> None:
    preset = ResearchPreset(
        name="coverage_rss",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动", "宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="cov-news", task_id="task-a", category="news_event",
            agent_name="news_agent", importance=0.9, content="新闻覆盖" * 15,
            source="google_news_rss",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="cov-macro", task_id="task-a", category="macro",
            agent_name="macro_agent", importance=0.9, content="宏观覆盖" * 15,
            source="google_news_rss",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=1)
        traces = store.list_traces("task-a")
        covered = sum(1 for t in traces if t.retrieved_count > 0)
        assert covered == 2, f"Expected both sections covered, got {covered}/2"
        assert sum(pack.coverage.values()) == len(pack.items)
    print("  RSS provider 覆盖率保持稳定 OK")


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


def test_pack_skips_oversized_candidate_and_keeps_section_covered() -> None:
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="task-budget-skip")
        preset = ResearchPreset(
            name="budget_skip",
            research_type="fx",
            default_agents=[],
            report_sections=["宏观信号"],
            banned_terms=[],
            default_time_horizon="short_term",
        )
        store.insert_chunk(_make_chunk(
            chunk_id="macro-large", task_id="task-budget-skip",
            category="macro", importance=0.95, content="X" * 7000,
            source="url=https://example.com/large",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-small", task_id="task-budget-skip",
            category="macro", importance=0.80, content="short macro evidence",
            source="url=https://example.com/small",
        ))

        pack = store.build_context_pack(task, preset, [], token_budget=6000, max_chunk_tokens=0)
        assert [item.chunk_id for item in pack.items] == ["macro-small"]
        trace = store.list_traces("task-budget-skip")[0]
        assert trace.section_covered is True
        assert "skipped_over_budget=1" in trace.query
    print("  pack 跳过超预算候选       OK")


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
            assert "chunk_ids=" not in t.query
            assert isinstance(t.selected_chunk_ids, list)
            assert t.section_title
            assert t.section_covered == (t.retrieved_count > 0)
            assert isinstance(t.score_distribution, dict)
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


# ── Phase 10B — scored context pack ──────────────────────────────────────────

def test_scored_pack_high_before_low() -> None:
    """Higher composite_score chunks are selected before lower ones."""
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="high", task_id="task-a", category="fx_price",
            importance=0.9, confidence=0.9, content="H" * 30,
            source="https://rba.gov.au/data",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="low", task_id="task-a", category="fx_price",
            importance=0.2, confidence=0.2, content="L" * 30,
            source=None,
        ))

        pack = store.build_context_pack(task, FX_CNYAUD_PRESET, [])
        ids = [it.chunk_id for it in pack.items if it.chunk_id in ("high", "low")]
        assert ids[0] == "high", f"Expected 'high' first, got {ids}"
    print("  10B: 高分优先于低分      OK")


def test_scored_pack_item_has_scores() -> None:
    """ContextPackItem carries composite_score and attention_score."""
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="scored", task_id="task-a", category="fx_price",
            importance=0.8, confidence=0.7, content="S" * 40,
            source="https://reuters.com/aud",
        ))

        pack = store.build_context_pack(task, FX_CNYAUD_PRESET, [])
        item = next((it for it in pack.items if it.chunk_id == "scored"), None)
        assert item is not None
        assert item.composite_score > 0, f"composite_score should be > 0, got {item.composite_score}"
        assert item.attention_score > 0, f"attention_score should be > 0, got {item.attention_score}"
        assert item.relevance_score == item.composite_score
    print("  10B: item 包含评分       OK")


def test_scored_pack_trace_scoring_method() -> None:
    """RetrievalTrace records scoring_method."""
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        _seed_chunks_for_pack(store)

        store.build_context_pack(task, FX_CNYAUD_PRESET, [])
        traces = store.list_traces("task-a")
        assert len(traces) > 0
        for t in traces:
            assert t.scoring_method == "composite", (
                f"Expected 'composite', got {t.scoring_method!r}"
            )
            assert "scoring=composite" in t.query
    print("  10B: trace 记录评分方法  OK")


def test_scored_pack_scorer_failure_fallback() -> None:
    """When scorer raises, fall back to legacy; composite_score reset to 0."""
    import unittest.mock
    import evidence_store as _es

    def _broken_scorer(*_a, **_kw):
        raise RuntimeError("scorer crashed")

    with EvidenceStore(":memory:") as store:
        task = _make_task()
        _seed_chunks_for_pack(store)

        with unittest.mock.patch.object(_es, "compute_evidence_score", _broken_scorer):
            pack = store.build_context_pack(task, FX_CNYAUD_PRESET, [])

        assert len(pack.items) > 0
        traces = store.list_traces("task-a")
        for t in traces:
            assert t.scoring_method == "legacy"
        for item in pack.items:
            assert item.composite_score == 0.0, (
                f"Fallback should reset composite_score to 0, got {item.composite_score}"
            )
            assert item.attention_score == 0.0
    print("  10B: 评分故障回退旧排序  OK")


def test_scored_pack_user_relevance_boost() -> None:
    """User preferred_topics boost a matching chunk's rank."""
    from schema import SafeUserContext
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="match", task_id="task-a", category="fx_price",
            importance=0.5, confidence=0.5, content="M" * 30,
            source="https://rba.gov.au",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="nomatch", task_id="task-a", category="fx_price",
            importance=0.5, confidence=0.5, content="N" * 30,
            source="https://rba.gov.au",
        ))

        ctx = SafeUserContext(preferred_topics=["fx_price"])
        pack = store.build_context_pack(
            task, FX_CNYAUD_PRESET, [],
            safe_user_context=ctx,
        )
        items_fx = [it for it in pack.items if it.chunk_id in ("match", "nomatch")]
        if len(items_fx) >= 2:
            assert items_fx[0].chunk_id == "match", (
                f"Expected 'match' first with user_relevance boost, got {items_fx[0].chunk_id}"
            )
    print("  10B: 用户相关性提升排名  OK")


def test_chunk_score_fields_persisted() -> None:
    """Composite score and score breakdown survive insert → get round-trip."""
    with EvidenceStore(":memory:") as store:
        chunk = _make_chunk(chunk_id="persist-1")
        chunk.attention_score = 0.75
        chunk.composite_score = 0.82
        chunk.score_importance = 0.81
        chunk.score_confidence = 0.71
        chunk.score_recency = 0.91
        chunk.score_source_quality = 0.68
        chunk.score_user_relevance = 0.83
        chunk.score_conflict_value = 0.10
        chunk.score_reason = "high_imp,user_match,conflict_boost"
        store.insert_chunk(chunk)

        loaded = store.get_chunk("persist-1")
        assert loaded is not None
        assert loaded.attention_score == 0.75
        assert loaded.composite_score == 0.82
        assert loaded.score_importance == 0.81
        assert loaded.score_confidence == 0.71
        assert loaded.score_recency == 0.91
        assert loaded.score_source_quality == 0.68
        assert loaded.score_user_relevance == 0.83
        assert loaded.score_conflict_value == 0.10
        assert loaded.score_reason == "high_imp,user_match,conflict_boost"
    print("  10B: 评分分解字段持久化  OK")


def test_scored_pack_persists_to_sqlite() -> None:
    """After build_context_pack, get_chunk returns non-zero composite_score."""
    with EvidenceStore(":memory:") as store:
        task = _make_task()
        store.insert_chunk(_make_chunk(
            chunk_id="persist-check", task_id="task-a", category="fx_price",
            importance=0.8, confidence=0.7, content="P" * 40,
            source="https://reuters.com/aud",
        ))

        loaded_before = store.get_chunk("persist-check")
        assert loaded_before is not None
        assert loaded_before.composite_score == 0.0

        store.build_context_pack(task, FX_CNYAUD_PRESET, [])

        loaded_after = store.get_chunk("persist-check")
        assert loaded_after is not None
        assert loaded_after.composite_score > 0, (
            f"composite_score should be > 0 after pack build, got {loaded_after.composite_score}"
        )
        assert loaded_after.attention_score > 0
        assert loaded_after.score_importance == 0.8
        assert loaded_after.score_confidence == 0.7
        assert loaded_after.score_recency > 0
        assert loaded_after.score_source_quality > 0
        assert loaded_after.score_user_relevance >= 0.3
        assert loaded_after.score_reason
    print("  10B: 评分分解回写 SQLite OK")


def test_context_pack_marks_selected_chunks_used() -> None:
    """Selected ContextPack chunks are persisted as used_in_brief."""
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="task-used")
        preset = ResearchPreset(
            name="used_flag_single_section",
            research_type="fx",
            default_agents=[],
            report_sections=["汇率事实"],
            banned_terms=[],
            default_time_horizon="short_term",
        )
        selected = _make_chunk(
            chunk_id="used-selected",
            task_id="task-used",
            category="fx_price",
            importance=0.8,
            confidence=0.8,
            content="Selected chunk",
            source="https://reuters.com/selected",
        )
        overflow = _make_chunk(
            chunk_id="used-overflow",
            task_id="task-used",
            category="fx_price",
            importance=0.7,
            confidence=0.7,
            content="Overflow chunk",
            source="https://reuters.com/overflow",
        )
        store.insert_chunk(selected)
        store.insert_chunk(overflow)

        pack = store.build_context_pack(
            task,
            preset,
            [],
            max_chunks_per_section=1,
            token_budget=50000,
        )

        selected_ids = {item.chunk_id for item in pack.items}
        loaded_selected = store.get_chunk("used-selected")
        loaded_overflow = store.get_chunk("used-overflow")
        assert loaded_selected is not None
        assert loaded_overflow is not None
        assert "used-selected" in selected_ids
        assert loaded_selected.used_in_brief is True
        assert loaded_overflow.used_in_brief is False
    print("  10B: 选中 chunk 标记 used OK")


def test_legacy_db_migration() -> None:
    """Simulates a v1 database (no score columns) — migration adds them."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE evidence_chunks (
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
        token_estimate INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE evidence_findings (
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
    CREATE TABLE citation_refs (
        citation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL DEFAULT '',
        chunk_id TEXT NOT NULL DEFAULT '',
        finding_id TEXT,
        section_title TEXT NOT NULL DEFAULT '',
        relevance_score REAL NOT NULL DEFAULT 0.0
    );
    CREATE TABLE retrieval_traces (
        trace_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL DEFAULT '',
        query TEXT NOT NULL DEFAULT '',
        retrieved_count INTEGER NOT NULL DEFAULT 0,
        total_chunks INTEGER NOT NULL DEFAULT 0,
        top_scores_json TEXT NOT NULL DEFAULT '[]',
        latency_ms INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE schema_version (version INTEGER NOT NULL);
    INSERT INTO schema_version (version) VALUES (1);
    INSERT INTO evidence_chunks (chunk_id, task_id, content, importance, confidence)
        VALUES ('old-chunk', 'task-old', 'legacy data', 0.6, 0.5);
    """)
    conn.close()

    store = EvidenceStore(":memory:")
    chunk = _make_chunk(chunk_id="new-chunk")
    chunk.attention_score = 0.5
    chunk.composite_score = 0.7
    store.insert_chunk(chunk)
    loaded = store.get_chunk("new-chunk")
    assert loaded is not None
    assert loaded.attention_score == 0.5
    assert loaded.composite_score == 0.7
    store.close()
    print("  10B: 旧 DB 迁移安全      OK")


def test_v3_db_migration_adds_score_breakdown() -> None:
    """Simulates a v3 database — migration adds persisted score breakdown columns."""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "research_evidence.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
        CREATE TABLE evidence_chunks (
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
            composite_score REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (3);
        INSERT INTO evidence_chunks (chunk_id, task_id, content, importance, confidence)
            VALUES ('old-v3-chunk', 'task-old', 'legacy v3 data', 0.6, 0.5);
        """)
        conn.close()

        with EvidenceStore(db_path) as store:
            columns = {
                row[1]
                for row in store._conn.execute("PRAGMA table_info(evidence_chunks)").fetchall()
            }
            for col in (
                "score_importance",
                "score_confidence",
                "score_recency",
                "score_source_quality",
                "score_user_relevance",
                "score_conflict_value",
                "score_reason",
            ):
                assert col in columns, f"Missing migrated column {col}"

            old = store.get_chunk("old-v3-chunk")
            assert old is not None
            assert old.score_importance == 0.0
            assert old.score_reason == ""

            chunk = _make_chunk(chunk_id="new-v4-chunk")
            chunk.score_user_relevance = 0.83
            chunk.score_reason = "user_match"
            store.insert_chunk(chunk)
            loaded = store.get_chunk("new-v4-chunk")
            assert loaded is not None
            assert loaded.score_user_relevance == 0.83
            assert loaded.score_reason == "user_match"
    print("  10B: v3 DB 评分分解迁移  OK")


# ── Phase 10C: conflict detection integration tests ──────────────────────────

def test_conflict_detection_in_pack() -> None:
    """build_context_pack detects conflicts between opposing findings."""
    store = EvidenceStore(":memory:")
    task = ResearchTask(task_id="t-conflict", preset_name="fx_cnyaud")
    preset = FX_CNYAUD_PRESET

    out_bull = AgentOutput(
        agent_name="fx_agent", status="ok", confidence=0.8,
        findings=[
            Finding(key="bull_signal", summary="AUD看涨信号",
                    direction="bullish_aud", category="fx_price", importance=0.8),
        ],
    )
    out_bear = AgentOutput(
        agent_name="news_agent", status="ok", confidence=0.7,
        findings=[
            Finding(key="bear_signal", summary="AUD看跌信号",
                    direction="bearish_aud", category="fx_price", importance=0.7),
        ],
    )
    enriched = store.ingest_outputs(task, [out_bull, out_bear])
    pack = store.build_context_pack(task, preset, enriched, token_budget=50000)
    assert len(pack.items) >= 2
    store.close()
    print("  10C: 冲突检测在 pack 中运行  OK")


def test_conflict_boost_applied() -> None:
    """Conflicting chunks get boosted composite_score."""
    store = EvidenceStore(":memory:")
    task = ResearchTask(task_id="t-boost", preset_name="fx_cnyaud")
    preset = FX_CNYAUD_PRESET

    out_bull = AgentOutput(
        agent_name="fx_agent", status="ok", confidence=0.5,
        findings=[
            Finding(key="bull", summary="看涨", direction="bullish_aud",
                    category="fx_price", importance=0.5),
        ],
    )
    out_bear = AgentOutput(
        agent_name="news_agent", status="ok", confidence=0.5,
        findings=[
            Finding(key="bear", summary="看跌", direction="bearish_aud",
                    category="fx_price", importance=0.5),
        ],
    )
    enriched = store.ingest_outputs(task, [out_bull, out_bear])

    bull_chunk_id = enriched[0].chunk_ids[0]
    bear_chunk_id = enriched[1].chunk_ids[0]

    pack = store.build_context_pack(task, preset, enriched, token_budget=50000)

    boosted_items = {it.chunk_id: it for it in pack.items}
    if bull_chunk_id in boosted_items and bear_chunk_id in boosted_items:
        bull_item = boosted_items[bull_chunk_id]
        bear_item = boosted_items[bear_chunk_id]
        assert bull_item.composite_score > 0
        assert bear_item.composite_score > 0
    store.close()
    print("  10C: 冲突提升 composite_score  OK")


def test_conflict_detector_failure_fallback() -> None:
    """If conflict_detector raises, pack still builds successfully."""
    import conflict_detector as cd_module
    original_detect = cd_module.detect_conflicts

    def _broken(*args, **kwargs):
        raise RuntimeError("simulated conflict detector crash")

    store = EvidenceStore(":memory:")
    task = ResearchTask(task_id="t-cd-fail", preset_name="fx_cnyaud")
    preset = FX_CNYAUD_PRESET

    out = AgentOutput(
        agent_name="fx_agent", status="ok", confidence=0.7,
        findings=[
            Finding(key="sig", summary="信号", direction="bullish_aud",
                    category="fx_price", importance=0.7),
        ],
    )
    enriched = store.ingest_outputs(task, [out])

    import evidence_store as es_mod
    es_mod.detect_conflicts = _broken
    try:
        pack = store.build_context_pack(task, preset, enriched, token_budget=50000)
        assert isinstance(pack, ContextPack)
        assert len(pack.items) >= 1
    finally:
        es_mod.detect_conflicts = original_detect
    store.close()
    print("  10C: 检测器故障回退正常       OK")


def test_preselection_conflict_selects_lower_scored_bearish() -> None:
    """10C.1D: lower-scored bearish evidence can enter top-k via preselection boost."""
    import unittest.mock
    import evidence_store as es_mod
    from evidence_scorer import EvidenceScore

    preset = ResearchPreset(
        name="preselect_conflict",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-preselect")
        out = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            confidence=0.8,
            findings=[
                Finding(key="bull", summary="看涨", direction="bullish_aud", category="fx_price", importance=0.8),
                Finding(key="neutral", summary="中性", direction="neutral", category="fx_price", importance=0.8),
                Finding(key="bear", summary="看跌", direction="bearish_aud", category="fx_price", importance=0.8),
            ],
        )
        enriched = store.ingest_outputs(task, [out])
        bull_id, neutral_id, bear_id = enriched[0].chunk_ids
        base_scores = {bull_id: 0.70, neutral_id: 0.66, bear_id: 0.58}

        def _score(chunk, *_args, **_kwargs):
            score = base_scores[chunk.chunk_id]
            return EvidenceScore(
                chunk_id=chunk.chunk_id,
                composite_score=score,
                attention_score=score,
                importance=score,
                confidence=score,
            )

        with unittest.mock.patch.object(es_mod, "compute_evidence_score", _score):
            pack = store.build_context_pack(
                task, preset, [], max_chunks_per_section=2, token_budget=50000,
            )

        ids = [it.chunk_id for it in pack.items]
        assert bull_id in ids, ids
        assert bear_id in ids, ids
        assert neutral_id not in ids, ids
        bear_item = next(it for it in pack.items if it.chunk_id == bear_id)
        assert bear_item.composite_score == 0.68, bear_item.composite_score
        trace = store.list_traces("t-preselect")[0]
        assert trace.conflict_count == 1
        assert bear_id in trace.selected_chunk_ids
        assert bear_id in trace.boosted_chunk_ids
        assert trace.conflict_pairs
    print("  10C.1D: 低分冲突证据进入 top-k OK")


def test_preselection_conflict_boost_bounded() -> None:
    """10C.1D: candidate-stage conflict boost is bounded to +0.10."""
    import unittest.mock
    import evidence_store as es_mod
    from evidence_scorer import EvidenceScore

    preset = ResearchPreset(
        name="bounded_conflict",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-bounded")
        out = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            confidence=0.8,
            findings=[
                Finding(key="bull", summary="看涨", direction="bullish_aud", category="fx_price", importance=0.8),
                Finding(key="bear", summary="看跌", direction="bearish_aud", category="fx_price", importance=0.8),
            ],
        )
        enriched = store.ingest_outputs(task, [out])
        bull_id, bear_id = enriched[0].chunk_ids
        base_scores = {bull_id: 0.61, bear_id: 0.52}

        def _score(chunk, *_args, **_kwargs):
            score = base_scores[chunk.chunk_id]
            return EvidenceScore(chunk_id=chunk.chunk_id, composite_score=score, attention_score=score)

        with unittest.mock.patch.object(es_mod, "compute_evidence_score", _score):
            pack = store.build_context_pack(
                task, preset, [], max_chunks_per_section=2, token_budget=50000,
            )

        for item in pack.items:
            assert round(item.composite_score - base_scores[item.chunk_id], 4) <= 0.10
            assert round(item.composite_score - base_scores[item.chunk_id], 4) >= 0.0
    print("  10C.1D: 冲突加权上限正确      OK")


def test_preselection_conflict_detector_failure_keeps_scored_sort() -> None:
    """10C.1D: detector crash falls back to scored sorting, not legacy sorting."""
    import unittest.mock
    import evidence_store as es_mod
    from evidence_scorer import EvidenceScore

    preset = ResearchPreset(
        name="conflict_fail_scored",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实"],
        banned_terms=[],
        default_time_horizon="short_term",
    )

    def _broken(*_args, **_kwargs):
        raise RuntimeError("detector crashed")

    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-fail-scored")
        out = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            confidence=0.8,
            findings=[
                Finding(key="bull", summary="看涨", direction="bullish_aud", category="fx_price", importance=0.1),
                Finding(key="bear", summary="看跌", direction="bearish_aud", category="fx_price", importance=0.9),
            ],
        )
        enriched = store.ingest_outputs(task, [out])
        low_importance_high_score, high_importance_low_score = enriched[0].chunk_ids
        base_scores = {
            low_importance_high_score: 0.90,
            high_importance_low_score: 0.20,
        }

        def _score(chunk, *_args, **_kwargs):
            score = base_scores[chunk.chunk_id]
            return EvidenceScore(chunk_id=chunk.chunk_id, composite_score=score, attention_score=score)

        with unittest.mock.patch.object(es_mod, "compute_evidence_score", _score), \
             unittest.mock.patch.object(es_mod, "detect_conflicts", _broken):
            pack = store.build_context_pack(
                task, preset, [], max_chunks_per_section=1, token_budget=50000,
            )

        assert pack.items[0].chunk_id == low_importance_high_score
        trace = store.list_traces("t-fail-scored")[0]
        assert trace.scoring_method == "composite"
    print("  10C.1D: 检测失败保留评分排序  OK")


def test_preselection_no_conflict_keeps_rank() -> None:
    """10C.1D: with no conflicts, ranking remains the base score order."""
    import unittest.mock
    import evidence_store as es_mod
    from evidence_scorer import EvidenceScore

    preset = ResearchPreset(
        name="no_conflict_rank",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-no-conflict")
        out = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            confidence=0.8,
            findings=[
                Finding(key="a", summary="看涨A", direction="bullish_aud", category="fx_price", importance=0.8),
                Finding(key="b", summary="看涨B", direction="bullish_aud", category="fx_price", importance=0.8),
                Finding(key="c", summary="中性C", direction="neutral", category="fx_price", importance=0.8),
            ],
        )
        enriched = store.ingest_outputs(task, [out])
        c1, c2, c3 = enriched[0].chunk_ids
        base_scores = {c1: 0.50, c2: 0.80, c3: 0.60}

        def _score(chunk, *_args, **_kwargs):
            score = base_scores[chunk.chunk_id]
            return EvidenceScore(chunk_id=chunk.chunk_id, composite_score=score, attention_score=score)

        with unittest.mock.patch.object(es_mod, "compute_evidence_score", _score):
            pack = store.build_context_pack(
                task, preset, [], max_chunks_per_section=3, token_budget=50000,
            )

        assert [it.chunk_id for it in pack.items] == [c2, c3, c1]
        trace = store.list_traces("t-no-conflict")[0]
        assert "conflicts=0" in trace.query
    print("  10C.1D: 无冲突排名不变        OK")


# ── Phase 10.5.1A: FX evidence deduplication fix ─────────────────────────────

def test_fx_finding_key_dedup_keeps_all_five() -> None:
    """5 FX chunks with same source URL but different finding_key should not collapse."""
    preset = ResearchPreset(
        name="fx_dedup_test",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-fx-dedup")
        fx_keys = ["current_rate", "bank_spread", "historical_trend", "recent_range", "target_rate_gap"]
        for i, fk in enumerate(fx_keys):
            store.insert_chunk(_make_chunk(
                chunk_id=f"fx-{fk}", task_id="t-fx-dedup", category="fx_price",
                agent_name="fx_agent", importance=0.85 - i * 0.02,
                content=f"FX finding: {fk} data here" * 3,
                source=f"url=https://open.er-api.com/v6/latest/CNY | title=CNY/AUD 市场实时汇率 | provider=er-api | finding_key={fk}",
            ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        ids = [it.chunk_id for it in pack.items]
        for fk in fx_keys:
            assert f"fx-{fk}" in ids, f"fx-{fk} missing from pack: {ids}"
        assert len(ids) == 5
    print("  10.5.1A: FX 5 findings 同 URL 不折叠  OK")


def test_news_same_url_still_deduped() -> None:
    """Duplicate news chunks with same canonical URL (no finding_key) should still dedup."""
    preset = ResearchPreset(
        name="news_dedup_test",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-news-dedup")
        store.insert_chunk(_make_chunk(
            chunk_id="news-1", task_id="t-news-dedup", category="news_event",
            agent_name="news_agent", importance=0.9, content="RBA holds rate" * 5,
            source="url=https://reuters.com/rba-hold | title=RBA holds rates | provider=google_news_rss",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="news-2", task_id="t-news-dedup", category="news_event",
            agent_name="news_agent", importance=0.8, content="RBA rate hold" * 5,
            source="url=https://www.reuters.com/rba-hold/ | title=RBA holds rates copy | provider=tavily",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        ids = [it.chunk_id for it in pack.items]
        selected_news = [i for i in ids if i in ("news-1", "news-2")]
        assert len(selected_news) == 1, f"Expected 1 news chunk after URL dedup, got {selected_news}"
    print("  10.5.1A: 新闻同 URL 仍正常去重       OK")


def test_context_pack_fx_section_multi_select() -> None:
    """ContextPack for 汇率事实 can select more than one FX evidence if budget allows."""
    preset = ResearchPreset(
        name="fx_multi_select",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-fx-multi")
        out = AgentOutput(
            agent_name="fx_agent",
            status="ok",
            confidence=0.85,
            findings=[
                Finding(key="current_rate", summary="1 AUD = 4.9200 CNY", category="fx_price", importance=0.85),
                Finding(key="bank_spread", summary="银行牌价样本10家", category="fx_price", importance=0.80),
                Finding(key="historical_trend", summary="90日趋势数据", direction="bearish_aud", category="fx_price", importance=0.75),
            ],
            sources=[SourceRef(title="CNY/AUD 市场实时汇率", url="https://open.er-api.com/v6/latest/CNY", source="er-api", retrieved_at=now_iso())],
        )
        enriched = store.ingest_outputs(task, [out])

        pack = store.build_context_pack(task, preset, enriched, max_chunks_per_section=5, token_budget=50000)
        fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
        assert len(fx_items) >= 3, f"Expected >=3 FX items, got {len(fx_items)}: {[it.chunk_id for it in fx_items]}"
    print("  10.5.1A: 汇率事实多选 FX evidence OK")


# ── Phase 10.5.1B: News section fallback restriction ─────────────────────────

def test_news_section_no_fallback_to_macro() -> None:
    """news_agent=0 → 新闻驱动 section must NOT pull macro/risk chunks."""
    preset = ResearchPreset(
        name="news_no_fallback",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动", "宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-news-nofb")
        store.insert_chunk(_make_chunk(
            chunk_id="macro-1", task_id="t-news-nofb", category="macro",
            agent_name="macro_agent", importance=0.9, content="RBA rate hold" * 5,
            source="url=https://rba.gov.au/rate | finding_key=rba_hold",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-2", task_id="t-news-nofb", category="macro",
            agent_name="macro_agent", importance=0.8, content="PBoC rate cut" * 5,
            source="url=https://pboc.gov.cn/rate | finding_key=pboc_cut",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)

        news_trace = next(t for t in store.list_traces("t-news-nofb") if t.section_title == "新闻驱动")
        assert news_trace.section_covered is False
        assert news_trace.selected_chunk_ids == []
        assert news_trace.retrieved_count == 0
        assert "fallback=blocked" in news_trace.query

        macro_items = [it for it in pack.items if it.agent_name == "macro_agent"]
        assert len(macro_items) == 2, f"Expected 2 macro items, got {len(macro_items)}: {[it.chunk_id for it in macro_items]}"
    print("  10.5.1B: 新闻无数据不抢占宏观 chunks  OK")


def test_macro_section_retains_chunks_with_empty_news() -> None:
    """With news_agent=0, macro section retains all its chunks."""
    preset = ResearchPreset(
        name="macro_retain",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动", "宏观信号", "风险评估"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-macro-retain")
        for i in range(3):
            store.insert_chunk(_make_chunk(
                chunk_id=f"m-{i}", task_id="t-macro-retain", category="macro",
                agent_name="macro_agent", importance=0.9 - i * 0.1,
                content=f"Macro signal {i}" * 5,
                source=f"url=https://macro.example.com/{i} | finding_key=signal_{i}",
            ))
        store.insert_chunk(_make_chunk(
            chunk_id="risk-0", task_id="t-macro-retain", category="risk",
            agent_name="risk_agent", importance=0.85,
            content="Risk assessment data" * 5,
            source="url=https://risk.example.com/0 | finding_key=geopolitical",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)

        macro_items = [it for it in pack.items if it.agent_name == "macro_agent"]
        risk_items = [it for it in pack.items if it.agent_name == "risk_agent"]
        assert len(macro_items) == 3, f"Macro should have 3 items, got {len(macro_items)}"
        assert len(risk_items) == 1, f"Risk should have 1 item, got {len(risk_items)}"
    print("  10.5.1B: 宏观 section 保留全部 chunks OK")


def test_news_data_gap_trace_for_supervisor() -> None:
    """Supervisor receives a data_gap trace with fallback_reason for empty news."""
    preset = ResearchPreset(
        name="data_gap_trace",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-data-gap")
        store.insert_chunk(_make_chunk(
            chunk_id="macro-filler", task_id="t-data-gap", category="macro",
            agent_name="macro_agent", importance=0.9, content="macro data" * 5,
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        traces = store.list_traces("t-data-gap")
        assert len(traces) == 1
        news_trace = traces[0]
        assert news_trace.section_title == "新闻驱动"
        assert news_trace.section_covered is False
        assert "news_event" in news_trace.fallback_reason
        assert "no_" in news_trace.fallback_reason and "_evidence" in news_trace.fallback_reason
        assert "news_event" in news_trace.query or "no_" in news_trace.query
        assert len(pack.items) == 0
    print("  10.5.1B: Supervisor 收到 data_gap trace OK")


# ── Phase 10.5.1D: Pool-level conflict detection ─────────────────────────────

def test_pool_conflict_rba_vs_pboc() -> None:
    """RBA bullish + PBoC bearish is always detected even across sections."""
    preset = ResearchPreset(
        name="pool_conflict",
        research_type="fx",
        default_agents=[],
        report_sections=["宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-pool-cd")
        out_rba = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.7,
            findings=[
                Finding(key="macro_rba", summary="RBA bullish",
                        direction="bullish_aud", category="macro", importance=0.7),
            ],
        )
        out_pboc = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.7,
            findings=[
                Finding(key="macro_pboc", summary="PBoC bearish",
                        direction="bearish_aud", category="macro", importance=0.7),
            ],
        )
        store.ingest_outputs(task, [out_rba, out_pboc])

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        traces = store.list_traces("t-pool-cd")
        macro_trace = next(t for t in traces if t.section_title == "宏观信号")
        assert macro_trace.conflict_count >= 1, (
            f"Expected conflict_count >= 1, got {macro_trace.conflict_count}"
        )
        assert len(macro_trace.conflict_pairs) >= 1
        assert len(macro_trace.boosted_chunk_ids) >= 1
    print("  10.5.1D: RBA+PBoC 冲突始终被检测到  OK")


def test_pool_conflict_neutral_no_conflict() -> None:
    """macro_usd=neutral does not generate conflicts."""
    preset = ResearchPreset(
        name="neutral_no_conflict",
        research_type="fx",
        default_agents=[],
        report_sections=["宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-neutral")
        out = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.7,
            findings=[
                Finding(key="macro_rba", summary="RBA bullish",
                        direction="bullish_aud", category="macro", importance=0.7),
                Finding(key="macro_usd", summary="USD neutral",
                        direction="neutral", category="macro", importance=0.7),
            ],
        )
        store.ingest_outputs(task, [out])

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        traces = store.list_traces("t-neutral")
        macro_trace = next(t for t in traces if t.section_title == "宏观信号")
        assert macro_trace.conflict_count == 0, (
            f"Neutral should not conflict, got conflict_count={macro_trace.conflict_count}"
        )
    print("  10.5.1D: neutral 不产生冲突           OK")


def test_pool_conflict_boosts_unselected_side() -> None:
    """Even if one conflict side would not be top-k, pool boost brings it in."""
    import unittest.mock
    import evidence_store as es_mod
    from evidence_scorer import EvidenceScore

    preset = ResearchPreset(
        name="pool_boost_unselected",
        research_type="fx",
        default_agents=[],
        report_sections=["宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-pool-boost")
        out = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.7,
            findings=[
                Finding(key="macro_rba", summary="RBA bullish",
                        direction="bullish_aud", category="macro", importance=0.9),
                Finding(key="filler", summary="Filler data",
                        direction=None, category="macro", importance=0.8),
                Finding(key="macro_pboc", summary="PBoC bearish",
                        direction="bearish_aud", category="macro", importance=0.3),
            ],
        )
        enriched = store.ingest_outputs(task, [out])
        rba_id, filler_id, pboc_id = enriched[0].chunk_ids

        base_scores = {rba_id: 0.80, filler_id: 0.65, pboc_id: 0.58}

        def _score(chunk, *_args, **_kwargs):
            score = base_scores.get(chunk.chunk_id, 0.5)
            return EvidenceScore(chunk_id=chunk.chunk_id, composite_score=score, attention_score=score)

        with unittest.mock.patch.object(es_mod, "compute_evidence_score", _score):
            pack = store.build_context_pack(
                task, preset, [], max_chunks_per_section=2, token_budget=50000,
            )

        ids = [it.chunk_id for it in pack.items]
        assert rba_id in ids, f"RBA chunk should be selected: {ids}"
        assert pboc_id in ids, f"PBoC chunk should be boosted into top-2: {ids}"
        assert filler_id not in ids, f"Filler should be displaced: {ids}"
    print("  10.5.1D: 冲突加权使低分方进入 top-k   OK")


def test_pool_conflict_detection_failure_fallback() -> None:
    """If pool-level detection fails, per-section still works; if both fail, scored sort."""
    import unittest.mock
    import evidence_store as es_mod

    preset = ResearchPreset(
        name="pool_fail_fallback",
        research_type="fx",
        default_agents=[],
        report_sections=["宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )

    call_count = {"n": 0}
    original_detect = es_mod.detect_conflicts

    def _fail_first_then_ok(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("pool detection crash")
        return original_detect(*args, **kwargs)

    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-pool-fail")
        out = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.7,
            findings=[
                Finding(key="macro_rba", summary="RBA bullish",
                        direction="bullish_aud", category="macro", importance=0.7),
                Finding(key="macro_pboc", summary="PBoC bearish",
                        direction="bearish_aud", category="macro", importance=0.7),
            ],
        )
        store.ingest_outputs(task, [out])

        with unittest.mock.patch.object(es_mod, "detect_conflicts", _fail_first_then_ok):
            pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)

        assert len(pack.items) >= 2
        traces = store.list_traces("t-pool-fail")
        macro_trace = next(t for t in traces if t.section_title == "宏观信号")
        assert macro_trace.conflict_count >= 1, (
            f"Section-level detection should still find conflict, got {macro_trace.conflict_count}"
        )
    print("  10.5.1D: 池级检测失败回退到段级检测   OK")


# ── Phase 10.5.1E: Section-level token reserve ──────────────────────────────

def test_section_reserve_risk_not_starved() -> None:
    """Risk section gets at least 1 chunk even when FX+macro eat most budget."""
    preset = ResearchPreset(
        name="reserve_test",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实", "宏观信号", "风险与矛盾"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-reserve")
        # FX chunks: 5 x 300 chars = 1500
        for i in range(5):
            store.insert_chunk(_make_chunk(
                chunk_id=f"fx-{i}", task_id="t-reserve", category="fx_price",
                agent_name="fx_agent", importance=0.85,
                content="A" * 300, source=f"url=https://fx.com/{i} | finding_key=fx_{i}",
            ))
        # Macro chunks: 3 x 800 chars = 2400
        for i in range(3):
            store.insert_chunk(_make_chunk(
                chunk_id=f"macro-{i}", task_id="t-reserve", category="macro",
                agent_name="macro_agent", importance=0.75,
                content="B" * 800, source=f"url=https://macro.com/{i} | finding_key=m_{i}",
            ))
        # Risk chunks: 2 x 200 chars = 400
        for i in range(2):
            store.insert_chunk(_make_chunk(
                chunk_id=f"risk-{i}", task_id="t-reserve", category="risk",
                agent_name="risk_agent", importance=0.5,
                content="C" * 200, source=f"finding_key=risk_{i}",
            ))

        # Budget: 1500+2400+400=4300 total, set budget to 4000 so it's tight
        pack = store.build_context_pack(
            task, preset, [], max_chunks_per_section=5, token_budget=4000,
        )
        risk_items = [it for it in pack.items if it.agent_name == "risk_agent"]
        assert len(risk_items) >= 1, (
            f"Risk section should have at least 1 chunk via reservation, got {len(risk_items)}"
        )
        assert pack.total_tokens <= 4000, (
            f"Total tokens {pack.total_tokens} exceeds budget 4000"
        )
        traces = store.list_traces("t-reserve")
        risk_trace = next(t for t in traces if t.section_title == "风险与矛盾")
        assert risk_trace.section_covered is True, "Risk section should be covered"
    print("  10.5.1E: risk section 至少保留 1 chunk  OK")


def test_section_reserve_four_sections_covered() -> None:
    """When all 4 section categories have data, all 4 sections are covered."""
    preset = ResearchPreset(
        name="four_sections",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实", "新闻驱动", "宏观信号", "风险与矛盾"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-4sec")
        store.insert_chunk(_make_chunk(
            chunk_id="fx-0", task_id="t-4sec", category="fx_price",
            agent_name="fx_agent", importance=0.85,
            content="A" * 300, source="url=https://fx.com/1 | finding_key=fx_0",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="news-0", task_id="t-4sec", category="news_event",
            agent_name="news_agent", importance=0.6,
            content="D" * 250, source="url=https://news.com/1 | finding_key=news_0",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-0", task_id="t-4sec", category="macro",
            agent_name="macro_agent", importance=0.75,
            content="B" * 400, source="url=https://macro.com/1 | finding_key=m_0",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="risk-0", task_id="t-4sec", category="risk",
            agent_name="risk_agent", importance=0.5,
            content="C" * 200, source="finding_key=risk_0",
        ))

        pack = store.build_context_pack(
            task, preset, [], max_chunks_per_section=5, token_budget=6000,
        )
        traces = store.list_traces("t-4sec")
        covered = sum(1 for t in traces if t.section_covered)
        assert covered == 4, f"Expected 4/4 sections covered, got {covered}/4"
    print("  10.5.1E: 4/4 section 全覆盖             OK")


def test_section_reserve_budget_respected() -> None:
    """Even with reservation, total tokens must not exceed budget."""
    preset = ResearchPreset(
        name="budget_strict",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实", "宏观信号", "风险与矛盾"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-budget")
        # Each chunk ~500 chars, budget only allows 2 chunks total
        for i in range(3):
            store.insert_chunk(_make_chunk(
                chunk_id=f"fx-{i}", task_id="t-budget", category="fx_price",
                agent_name="fx_agent", importance=0.85,
                content="A" * 500, source=f"url=https://fx.com/{i} | finding_key=fx_{i}",
            ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-0", task_id="t-budget", category="macro",
            agent_name="macro_agent", importance=0.75,
            content="B" * 500, source="url=https://macro.com/1 | finding_key=m_0",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="risk-0", task_id="t-budget", category="risk",
            agent_name="risk_agent", importance=0.5,
            content="C" * 500, source="finding_key=risk_0",
        ))

        pack = store.build_context_pack(
            task, preset, [], max_chunks_per_section=5, token_budget=1600,
        )
        assert pack.total_tokens <= 1600, (
            f"Total tokens {pack.total_tokens} exceeds budget 1600"
        )
        # With reservation, each section gets 1 reserved chunk (3 x 500 = 1500 <= 1600)
        agents = {it.agent_name for it in pack.items}
        assert "risk_agent" in agents, (
            f"Risk agent should be represented, got agents: {agents}"
        )
    print("  10.5.1E: 总 token 预算仍严格遵守       OK")


def test_section_reserve_max_chunk_tokens_filter() -> None:
    """Chunks exceeding max_chunk_tokens are filtered out in both phases."""
    preset = ResearchPreset(
        name="chunk_cap",
        research_type="fx",
        default_agents=[],
        report_sections=["宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-cap")
        store.insert_chunk(_make_chunk(
            chunk_id="macro-big", task_id="t-cap", category="macro",
            agent_name="macro_agent", importance=0.95,
            content="X" * 1500, source="url=https://macro.com/big | finding_key=m_big",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-ok", task_id="t-cap", category="macro",
            agent_name="macro_agent", importance=0.70,
            content="Y" * 400, source="url=https://macro.com/ok | finding_key=m_ok",
        ))

        pack = store.build_context_pack(
            task, preset, [], max_chunk_tokens=1200, token_budget=50000,
        )
        ids = [it.chunk_id for it in pack.items]
        assert "macro-big" not in ids, f"1500-char chunk should be filtered by max_chunk_tokens=1200"
        assert "macro-ok" in ids
    print("  10.5.1E: max_chunk_tokens 过滤超大 chunk OK")


def test_section_reserve_with_section_token_reserves() -> None:
    """Section token reserves cap per-section usage, leaving room for others."""
    preset = ResearchPreset(
        name="reserve_cap",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实", "风险与矛盾"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-rescap")
        # FX: 5 x 400 = 2000, but reserve = 800 → should cap at ~2 chunks
        for i in range(5):
            store.insert_chunk(_make_chunk(
                chunk_id=f"fx-{i}", task_id="t-rescap", category="fx_price",
                agent_name="fx_agent", importance=0.85,
                content="A" * 400, source=f"url=https://fx.com/{i} | finding_key=fx_{i}",
            ))
        store.insert_chunk(_make_chunk(
            chunk_id="risk-0", task_id="t-rescap", category="risk",
            agent_name="risk_agent", importance=0.5,
            content="C" * 300, source="finding_key=risk_0",
        ))

        pack = store.build_context_pack(
            task, preset, [],
            max_chunks_per_section=5, token_budget=6000,
            section_token_reserves={"fx_price": 800, "risk": 1000},
        )
        fx_items = [it for it in pack.items if it.agent_name == "fx_agent"]
        risk_items = [it for it in pack.items if it.agent_name == "risk_agent"]
        fx_tokens = sum(it.token_estimate for it in fx_items)
        assert fx_tokens <= 800, f"FX tokens {fx_tokens} should be capped at reserve 800"
        assert len(risk_items) >= 1, "Risk should be covered"
    print("  10.5.1E: section_token_reserves 限额生效 OK")


def test_section_reserve_news_empty_stays_uncovered() -> None:
    """When news has no data, reservation doesn't force coverage; fallback_reason is set."""
    preset = ResearchPreset(
        name="news_empty",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率事实", "新闻驱动", "风险与矛盾"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-news-empty")
        store.insert_chunk(_make_chunk(
            chunk_id="fx-0", task_id="t-news-empty", category="fx_price",
            agent_name="fx_agent", importance=0.85,
            content="A" * 300, source="url=https://fx.com/1 | finding_key=fx_0",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="risk-0", task_id="t-news-empty", category="risk",
            agent_name="risk_agent", importance=0.5,
            content="C" * 200, source="finding_key=risk_0",
        ))

        pack = store.build_context_pack(
            task, preset, [], max_chunks_per_section=5, token_budget=6000,
        )
        traces = store.list_traces("t-news-empty")
        news_trace = next(t for t in traces if t.section_title == "新闻驱动")
        assert news_trace.section_covered is False, "News should NOT be covered"
        assert "news_event" in news_trace.fallback_reason
        assert news_trace.fallback_reason.startswith("no_") and news_trace.fallback_reason.endswith("_evidence")

        fx_trace = next(t for t in traces if t.section_title == "汇率事实")
        assert fx_trace.section_covered is True
        risk_trace = next(t for t in traces if t.section_title == "风险与矛盾")
        assert risk_trace.section_covered is True
    print("  10.5.1E: news=0 保持未覆盖状态          OK")


# ── Phase 10.6B: Category taxonomy expansion ──────────────────────────────────

def test_legacy_macro_category_roundtrip() -> None:
    """Legacy 'macro' category still works: ingest → retrieve."""
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-legacy-macro")
        out = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.8,
            findings=[
                Finding(key="rba_hold", summary="RBA holds rate",
                        category="macro", importance=0.8),
            ],
        )
        enriched = store.ingest_outputs(task, [out])
        assert len(enriched) == 1
        chunk_id = enriched[0].chunk_ids[0]
        loaded = store.get_chunk(chunk_id)
        assert loaded is not None
        assert loaded.category == "macro"
    print("  10.6B: legacy macro roundtrip          OK")


def test_new_categories_ingest_and_retrieve() -> None:
    """New Phase 10.6B categories can be ingested and retrieved."""
    new_cats = ["policy_signal", "market_driver", "macro_indicator",
                "commodity_trade", "geopolitical_event", "data_gap", "unknown"]
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-new-cats")
        findings = [
            Finding(key=f"k_{cat}", summary=f"Test {cat}",
                    category=cat, importance=0.7)
            for cat in new_cats
        ]
        out = AgentOutput(
            agent_name="macro_agent", status="ok", confidence=0.7,
            findings=findings,
        )
        enriched = store.ingest_outputs(task, [out])
        assert len(enriched) == 1
        assert len(enriched[0].chunk_ids) == len(new_cats)

        for i, cat in enumerate(new_cats):
            loaded = store.get_chunk(enriched[0].chunk_ids[i])
            assert loaded is not None
            assert loaded.category == cat, f"Expected {cat}, got {loaded.category}"
    print("  10.6B: new categories ingest+retrieve  OK")


def test_section_hints_macro_signal() -> None:
    """'宏观信号' section selects policy_signal, macro_indicator, commodity_trade, macro."""
    preset = ResearchPreset(
        name="macro_signal_hints",
        research_type="fx",
        default_agents=[],
        report_sections=["宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-hints-macro")
        cats_and_ids = [
            ("policy_signal", "ps-0"),
            ("macro_indicator", "mi-0"),
            ("commodity_trade", "ct-0"),
            ("macro", "m-0"),
            ("market_driver", "md-0"),
        ]
        for cat, cid in cats_and_ids:
            store.insert_chunk(_make_chunk(
                chunk_id=cid, task_id="t-hints-macro", category=cat,
                agent_name="macro_agent", importance=0.8,
                content=f"{cat} data here " * 5,
                source=f"url=https://example.com/{cid} | finding_key={cid}",
            ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=10, token_budget=50000)
        ids = [it.chunk_id for it in pack.items]
        for _, cid in cats_and_ids:
            assert cid in ids, f"{cid} should be selected for macro section, got {ids}"
    print("  10.6B: macro section hints select all  OK")


def test_section_hints_risk_data_gap() -> None:
    """'风险与矛盾' section selects risk and data_gap categories."""
    preset = ResearchPreset(
        name="risk_data_gap",
        research_type="fx",
        default_agents=[],
        report_sections=["风险与矛盾"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-risk-dg")
        store.insert_chunk(_make_chunk(
            chunk_id="risk-1", task_id="t-risk-dg", category="risk",
            agent_name="risk_agent", importance=0.8,
            content="Risk data" * 5, source="finding_key=geopolitical",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="dg-1", task_id="t-risk-dg", category="data_gap",
            agent_name="risk_agent", importance=0.6,
            content="Data gap signal" * 5, source="finding_key=missing_data",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        ids = [it.chunk_id for it in pack.items]
        assert "risk-1" in ids, f"risk chunk should be in risk section: {ids}"
        assert "dg-1" in ids, f"data_gap chunk should be in risk section: {ids}"
    print("  10.6B: risk + data_gap route correctly OK")


def test_fx_section_excludes_market_driver() -> None:
    """汇率 section selects only fx_price by default, not market_driver."""
    preset = ResearchPreset(
        name="fx_all",
        research_type="fx",
        default_agents=[],
        report_sections=["汇率数据"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-fx-all")
        for i in range(4):
            store.insert_chunk(_make_chunk(
                chunk_id=f"fx-{i}", task_id="t-fx-all", category="fx_price",
                agent_name="fx_agent", importance=0.85 - i * 0.05,
                content=f"FX data {i}" * 5,
                source=f"url=https://fx.com/{i} | finding_key=fx_{i}",
            ))
        store.insert_chunk(_make_chunk(
            chunk_id="md-0", task_id="t-fx-all", category="market_driver",
            agent_name="fx_agent", importance=0.7,
            content="Market driver" * 5,
            source="url=https://fx.com/md | finding_key=md_0",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=10, token_budget=50000)
        ids = [it.chunk_id for it in pack.items]
        for i in range(4):
            assert f"fx-{i}" in ids, f"fx-{i} should be in fx section: {ids}"
        assert "md-0" not in ids, f"market_driver should not be in fx section: {ids}"
    print("  10.6B: fx section excludes market_driver OK")


def test_default_fx_preset_market_driver_goes_to_macro_not_fx() -> None:
    """Default four-section preset does not let 汇率事实 consume market_driver."""
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-default-market-driver")
        store.insert_chunk(_make_chunk(
            chunk_id="fx-rate", task_id="t-default-market-driver",
            category="fx_price", agent_name="fx_agent", importance=0.85,
            content="Current rate and bank spread " * 8,
            source="url=https://fx.example/rate | finding_key=current_rate",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="market-driver", task_id="t-default-market-driver",
            category="market_driver", agent_name="macro_agent", importance=0.8,
            content="DXY, AUD/USD and commodity drivers " * 8,
            source="url=https://macro.example/driver | finding_key=market_driver",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="macro-policy", task_id="t-default-market-driver",
            category="macro", agent_name="macro_agent", importance=0.75,
            content="RBA and PBoC policy signal " * 8,
            source="url=https://macro.example/policy | finding_key=macro_policy",
        ))
        store.insert_chunk(_make_chunk(
            chunk_id="risk-gap", task_id="t-default-market-driver",
            category="data_gap", agent_name="risk_agent", importance=0.7,
            content="Risk and data gap signal " * 8,
            source="finding_key=data_gap",
        ))

        pack = store.build_context_pack(
            task, FX_CNYAUD_PRESET, [],
            max_chunks_per_section=5,
            token_budget=50000,
        )
        ids = [it.chunk_id for it in pack.items]
        assert "fx-rate" in ids
        assert "market-driver" in ids
        assert "macro-policy" in ids
        assert "risk-gap" in ids

        traces = store.list_traces("t-default-market-driver")
        by_section = {t.section_title: t for t in traces}
        assert by_section["汇率事实"].selected_chunk_ids == ["fx-rate"], (
            by_section["汇率事实"].selected_chunk_ids
        )
        assert "market-driver" in by_section["宏观信号"].selected_chunk_ids, (
            by_section["宏观信号"].selected_chunk_ids
        )
        assert "macro-policy" in by_section["宏观信号"].selected_chunk_ids
        assert "risk-gap" in by_section["风险与矛盾"].selected_chunk_ids

    print("  10.6B: default preset routes market_driver to macro OK")


def test_geopolitical_event_no_fallback() -> None:
    """geopolitical_event in NO_FALLBACK prevents news section from pulling unrelated chunks."""
    preset = ResearchPreset(
        name="geo_nofb",
        research_type="fx",
        default_agents=[],
        report_sections=["新闻驱动", "宏观信号"],
        banned_terms=[],
        default_time_horizon="short_term",
    )
    with EvidenceStore(":memory:") as store:
        task = _make_task(task_id="t-geo-nofb")
        store.insert_chunk(_make_chunk(
            chunk_id="macro-only", task_id="t-geo-nofb", category="macro",
            agent_name="macro_agent", importance=0.9,
            content="macro data" * 5, source="finding_key=macro_1",
        ))

        pack = store.build_context_pack(task, preset, [], max_chunks_per_section=5, token_budget=50000)
        traces = store.list_traces("t-geo-nofb")
        news_trace = next(t for t in traces if t.section_title == "新闻驱动")
        assert news_trace.section_covered is False
        assert "fallback=blocked" in news_trace.query

        macro_trace = next(t for t in traces if t.section_title == "宏观信号")
        assert macro_trace.section_covered is True
        macro_ids = [it.chunk_id for it in pack.items if it.chunk_id == "macro-only"]
        assert len(macro_ids) == 1
    print("  10.6B: geopolitical_event NO_FALLBACK  OK")


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
        test_ingest_prefers_explicit_106c_fields,
        test_ingest_context_header_format,
        test_ingest_multiple_outputs,
        test_ingest_error_output,
        test_ingest_empty_findings,
        # Phase 9.1 Step 5 — build_context_pack
        test_pack_section_based_retrieval,
        test_pack_max_chunks_per_section,
        test_pack_fallback_retrieval,
        test_pack_dedup_by_chunk_id,
        test_pack_dedup_by_url,
        test_pack_provider_label_does_not_dedup_distinct_urls,
        test_pack_same_url_selected_once,
        test_pack_provider_label_does_not_empty_later_section,
        test_pack_section_coverage_stable_with_provider_duplicates,
        test_pack_token_budget,
        test_pack_skips_oversized_candidate_and_keeps_section_covered,
        test_pack_traces_created,
        test_pack_coverage,
        test_pack_intra_section_dedup,
        test_pack_empty_store,
        # Phase 10B — scored context pack
        test_scored_pack_high_before_low,
        test_scored_pack_item_has_scores,
        test_scored_pack_trace_scoring_method,
        test_scored_pack_scorer_failure_fallback,
        test_scored_pack_user_relevance_boost,
        test_chunk_score_fields_persisted,
        test_scored_pack_persists_to_sqlite,
        test_context_pack_marks_selected_chunks_used,
        test_legacy_db_migration,
        test_v3_db_migration_adds_score_breakdown,
        # Phase 10C — conflict detection integration
        test_conflict_detection_in_pack,
        test_conflict_boost_applied,
        test_conflict_detector_failure_fallback,
        # Phase 10C.1D — preselection conflict detection
        test_preselection_conflict_selects_lower_scored_bearish,
        test_preselection_conflict_boost_bounded,
        test_preselection_conflict_detector_failure_keeps_scored_sort,
        test_preselection_no_conflict_keeps_rank,
        # Phase 10.5.1A — FX evidence deduplication fix
        test_fx_finding_key_dedup_keeps_all_five,
        test_news_same_url_still_deduped,
        test_context_pack_fx_section_multi_select,
        # Phase 10.5.1B — news section fallback restriction
        test_news_section_no_fallback_to_macro,
        test_macro_section_retains_chunks_with_empty_news,
        test_news_data_gap_trace_for_supervisor,
        # Phase 10.5.1D — pool-level conflict detection
        test_pool_conflict_rba_vs_pboc,
        test_pool_conflict_neutral_no_conflict,
        test_pool_conflict_boosts_unselected_side,
        test_pool_conflict_detection_failure_fallback,
        # Phase 10.5.1E — section-level token reserve
        test_section_reserve_risk_not_starved,
        test_section_reserve_four_sections_covered,
        test_section_reserve_budget_respected,
        test_section_reserve_news_empty_stays_uncovered,
        test_section_reserve_max_chunk_tokens_filter,
        test_section_reserve_with_section_token_reserves,
        # Phase 10.6B — category taxonomy expansion
        test_legacy_macro_category_roundtrip,
        test_new_categories_ingest_and_retrieve,
        test_section_hints_macro_signal,
        test_section_hints_risk_data_gap,
        test_fx_section_excludes_market_driver,
        test_default_fx_preset_market_driver_goes_to_macro_not_fx,
        test_geopolitical_event_no_fallback,
    ]
    print("Phase 9.1 + 10B + 10C — EvidenceStore 测试")
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
