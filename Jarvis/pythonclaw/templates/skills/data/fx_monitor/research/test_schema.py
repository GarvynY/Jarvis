#!/usr/bin/env python3
"""
Phase 9 Step 1 — Schema serialisation and validation tests.

Verifies:
  1. All dataclasses serialise to plain dict (no datetime objects)
  2. Nested dataclasses serialise correctly
  3. No datetime objects stored in any field (default factory path)
  4. to_dict() raises TypeError when a datetime is smuggled into a field
  5. FX_CNYAUD_PRESET exists in PRESET_REGISTRY
  6. AGENT_REGISTRY is NOT exported from schema
  7. from_dict() round-trips for all dataclasses
  8. validate_status / validate_confidence raise on bad input
  9. AgentOutput(status="bad") raises at construction time  [P0]
 10. AgentOutput(confidence=9) raises at construction time  [P0]
 11. Finding(evidence_score=1.5) raises at construction time [P0]
 12. ResearchBrief.from_dict() never overwrites disclaimer   [P0]
 13. Instance .to_dict() matches global to_dict()            [P2]
 14. SafeUserContext has risk_level field                    [P1]
 15. ResearchPreset has new P1 fields                        [P1]

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_schema.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput,
    CitationRef,
    ContextPack,
    ContextPackItem,
    CostEstimate,
    EvidenceChunk,
    EvidenceFinding,
    Finding,
    FX_CNYAUD_PRESET,
    PRESET_REGISTRY,
    ResearchBrief,
    ResearchPreset,
    ResearchSection,
    ResearchTask,
    RetrievalTrace,
    SafeUserContext,
    SourceRef,
    _FIXED_DISCLAIMER,
    now_iso,
    to_dict,
    to_json,
    validate_confidence,
    validate_status,
    validate_ttl_policy,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _no_datetime_in(obj: object, path: str = "root") -> None:
    if isinstance(obj, datetime):
        raise AssertionError(f"datetime object found at {path!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_datetime_in(v, f"{path}.{k}")
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_datetime_in(v, f"{path}[{i}]")


def _json_round_trip(obj: object, label: str) -> dict:
    raw = to_json(obj)
    parsed = json.loads(raw)
    _no_datetime_in(parsed, label)
    return parsed


def _expect_error(fn, exc_type: type, label: str) -> None:
    try:
        fn()
        raise AssertionError(f"{label}: expected {exc_type.__name__} but no exception was raised")
    except exc_type:
        pass


# ── Individual dataclass tests ────────────────────────────────────────────────

def test_safe_user_context() -> None:
    ctx = SafeUserContext(
        target_rate=4.78,
        alert_threshold=0.5,
        purpose="tuition",
        risk_level="low",
        preferred_summary_style="brief",
        preferred_topics=["RBA", "iron ore"],
        privacy_level="high",
    )
    d = to_dict(ctx)
    assert d["target_rate"] == 4.78
    assert d["risk_level"] == "low"          # P1: field must exist
    assert d["purpose"] == "tuition"
    assert isinstance(d["preferred_topics"], list)
    _no_datetime_in(d, "SafeUserContext")

    ctx2 = SafeUserContext.from_dict(d)
    assert ctx2.target_rate == ctx.target_rate
    assert ctx2.risk_level == "low"
    assert ctx2.preferred_topics == ctx.preferred_topics

    # Instance method matches global helper
    assert ctx.to_dict() == d
    print("  SafeUserContext          OK")


def test_research_preset_p1_fields() -> None:
    d = to_dict(FX_CNYAUD_PRESET)
    assert d["name"] == "fx_cnyaud"
    assert d["research_type"] == "fx"
    assert "fx_agent" in d["default_agents"]
    assert len(d["report_sections"]) == 4
    # P1 new fields
    assert d["output_language"] == "zh-CN"
    assert d["default_region"] == "CN-AU"
    assert isinstance(d["required_agents"], list)
    assert isinstance(d["optional_agents"], list)
    assert isinstance(d["data_sources"], list)
    assert "fx_agent" in d["required_agents"]
    _no_datetime_in(d, "ResearchPreset")

    p2 = ResearchPreset.from_dict(d)
    assert p2.name == "fx_cnyaud"
    assert p2.output_language == "zh-CN"
    assert p2.banned_terms == FX_CNYAUD_PRESET.banned_terms

    assert FX_CNYAUD_PRESET.to_dict() == d
    print("  ResearchPreset (P1)      OK")


def test_research_task() -> None:
    task = ResearchTask.from_preset(
        FX_CNYAUD_PRESET,
        safe_user_context=SafeUserContext(purpose="living", risk_level="medium"),
        focus_assets=["AUD", "CNY"],
        focus_pair="CNY/AUD",
    )
    d = to_dict(task)
    assert d["preset_name"] == "fx_cnyaud"
    assert d["safe_user_context"]["purpose"] == "living"
    assert d["safe_user_context"]["risk_level"] == "medium"
    assert isinstance(d["requested_at"], str)  # must be string, not datetime
    _no_datetime_in(d, "ResearchTask")

    task2 = ResearchTask.from_dict(d)
    assert task2.task_id == task.task_id
    assert task2.safe_user_context.purpose == "living"

    assert task.to_dict() == d
    print("  ResearchTask             OK")


def test_source_ref() -> None:
    src = SourceRef(
        title="CNY/AUD 实时汇率",
        url="https://open.er-api.com/v6/latest/CNY",
        source="open.er-api.com",
        retrieved_at=now_iso(),
        published_at=None,
    )
    d = to_dict(src)
    assert d["published_at"] is None
    _no_datetime_in(d, "SourceRef")

    src2 = SourceRef.from_dict(d)
    assert src2.source == "open.er-api.com"
    assert src.to_dict() == d
    print("  SourceRef                OK")


def test_finding() -> None:
    f = Finding(
        key="rba_hold",
        summary="RBA 维持利率不变",
        direction="bearish_aud",
        evidence_score=0.8,
        attention_score=0.5,
    )
    d = to_dict(f)
    assert d["direction"] == "bearish_aud"
    assert d["evidence_score"] == 0.8
    _no_datetime_in(d, "Finding")

    f2 = Finding.from_dict(d)
    assert f2.key == "rba_hold"
    assert f.to_dict() == d
    print("  Finding                  OK")


def test_finding_score_validation() -> None:
    """P0: evidence_score / attention_score must be None or [0,1]."""
    _expect_error(
        lambda: Finding(key="x", summary="x", evidence_score=1.5),
        ValueError, "Finding(evidence_score=1.5)",
    )
    _expect_error(
        lambda: Finding(key="x", summary="x", attention_score=-0.1),
        ValueError, "Finding(attention_score=-0.1)",
    )
    # None is always valid
    f = Finding(key="x", summary="x", evidence_score=None, attention_score=None)
    assert f.evidence_score is None
    print("  Finding score validation  OK  [P0]")


def test_agent_output() -> None:
    out = AgentOutput(
        agent_name="fx_agent",
        status="ok",
        summary="1 AUD = 4.7800 CNY",
        findings=[Finding(key="rate", summary="1 AUD = 4.78 CNY", direction="neutral")],
        sources=[SourceRef(
            title="市场汇率", url="https://example.com",
            source="open.er-api.com", retrieved_at=now_iso(),
        )],
        confidence=0.9,
        latency_ms=342,
    )
    d = _json_round_trip(out, "AgentOutput")
    assert d["agent_name"] == "fx_agent"
    assert len(d["findings"]) == 1
    assert len(d["sources"]) == 1
    assert d["confidence"] == 0.9

    out2 = AgentOutput.from_dict(d)
    assert out2.findings[0].key == "rate"
    assert out2.sources[0].source == "open.er-api.com"

    assert out.to_dict() == to_dict(out)
    print("  AgentOutput              OK")


def test_agent_output_validation_p0() -> None:
    """P0: invalid status / confidence must raise at construction time."""
    _expect_error(
        lambda: AgentOutput(agent_name="a", status="bad", confidence=0.5),
        ValueError, "AgentOutput(status='bad')",
    )
    _expect_error(
        lambda: AgentOutput(agent_name="a", status="ok", confidence=9.0),
        ValueError, "AgentOutput(confidence=9)",
    )
    _expect_error(
        lambda: AgentOutput(agent_name="a", status="ok", confidence=-1.0),
        ValueError, "AgentOutput(confidence=-1)",
    )
    # from_dict must also raise on bad data
    _expect_error(
        lambda: AgentOutput.from_dict({"agent_name": "a", "status": "unknown", "confidence": 0.5}),
        ValueError, "AgentOutput.from_dict(status='unknown')",
    )
    _expect_error(
        lambda: AgentOutput.from_dict({"agent_name": "a", "status": "ok", "confidence": 2.0}),
        ValueError, "AgentOutput.from_dict(confidence=2.0)",
    )
    print("  AgentOutput validation   OK  [P0]")


def test_agent_output_make_error() -> None:
    err = AgentOutput.make_error("macro_agent", "network timeout", latency_ms=5000)
    assert err.status == "error"
    assert err.error == "network timeout"
    d = to_dict(err)
    _no_datetime_in(d, "AgentOutput.make_error")
    print("  AgentOutput.make_error   OK")


def test_research_brief_disclaimer_p0() -> None:
    """P0: from_dict must never overwrite the fixed disclaimer."""
    malicious_brief = {
        "task_id": "t1",
        "preset_name": "fx_cnyaud",
        "disclaimer": "No disclaimer — do whatever you want!",
    }
    brief = ResearchBrief.from_dict(malicious_brief)
    assert brief.disclaimer == _FIXED_DISCLAIMER, (
        f"Disclaimer was overwritten! Got: {brief.disclaimer!r}"
    )
    print("  ResearchBrief disclaimer OK  [P0]")


def test_research_brief_nested() -> None:
    """Nested serialisation: ResearchBrief → ResearchSection → CostEstimate."""
    brief = ResearchBrief(
        task_id="test-task-123",
        preset_name="fx_cnyaud",
        conclusion="综合来看，AUD 短期走势中性偏弱。",
        sections=[
            ResearchSection(
                title="汇率事实",
                content="当前 1 AUD = 4.7800 CNY。",
                source_agents=["fx_agent"],
                has_data_gap=False,
            ),
            ResearchSection(
                title="风险与矛盾",
                content="宏观与新闻信号存在矛盾。",
                source_agents=["risk_agent"],
                has_data_gap=True,
            ),
        ],
        agent_statuses={"fx_agent": "ok", "news_agent": "partial"},
        cost_estimate=CostEstimate(
            llm_calls=1, estimated_tokens=800, estimated_cost_usd=0.002
        ),
    )
    d = _json_round_trip(brief, "ResearchBrief")
    assert len(d["sections"]) == 2
    assert d["sections"][1]["has_data_gap"] is True
    assert d["cost_estimate"]["llm_calls"] == 1
    assert d["disclaimer"] == _FIXED_DISCLAIMER

    brief2 = ResearchBrief.from_dict(d)
    assert brief2.sections[0].title == "汇率事实"
    assert brief2.cost_estimate.estimated_cost_usd == 0.002
    assert brief2.disclaimer == _FIXED_DISCLAIMER

    assert brief.to_dict() == d
    print("  ResearchBrief (nested)   OK")


def test_to_dict_datetime_guard_p0() -> None:
    """P0: to_dict() must raise TypeError if a datetime is present in any field."""
    import dataclasses

    @dataclasses.dataclass
    class _BadSource:
        title: str
        url: str
        source: str
        retrieved_at: object   # deliberately untyped so we can stuff a datetime in
        published_at: str | None = None

    bad = _BadSource(
        title="test",
        url="https://example.com",
        source="test",
        retrieved_at=datetime.now(timezone.utc),   # datetime, not string!
    )
    _expect_error(lambda: to_dict(bad), TypeError, "to_dict with datetime field")
    print("  to_dict datetime guard   OK  [P0]")


def test_preset_registry() -> None:
    assert "fx_cnyaud" in PRESET_REGISTRY
    assert PRESET_REGISTRY["fx_cnyaud"] is FX_CNYAUD_PRESET
    print("  PRESET_REGISTRY          OK")


def test_agent_registry_not_in_schema() -> None:
    """AGENT_REGISTRY must NOT be importable from schema — it belongs in coordinator."""
    import schema as _schema
    assert not hasattr(_schema, "AGENT_REGISTRY"), (
        "AGENT_REGISTRY should not be defined in schema.py"
    )
    print("  AGENT_REGISTRY absent    OK  [P1]")


def test_validators() -> None:
    assert validate_status("ok") == "ok"
    assert validate_status("partial") == "partial"
    assert validate_status("error") == "error"
    _expect_error(lambda: validate_status("unknown"), ValueError, "validate_status('unknown')")
    _expect_error(lambda: validate_status(""), ValueError, "validate_status('')")

    assert validate_confidence(0.0) == 0.0
    assert validate_confidence(1.0) == 1.0
    assert validate_confidence(0.75) == 0.75
    _expect_error(lambda: validate_confidence(1.1), ValueError, "validate_confidence(1.1)")
    _expect_error(lambda: validate_confidence(-0.1), ValueError, "validate_confidence(-0.1)")
    print("  validate_status/conf     OK")


def test_no_datetime_objects_in_defaults() -> None:
    """Default factory fields must produce strings, not datetime objects."""
    task = ResearchTask()
    brief = ResearchBrief(task_id="x", preset_name="fx_cnyaud")
    out = AgentOutput(agent_name="a", status="ok")

    for obj, label in [
        (task, "ResearchTask"),
        (brief, "ResearchBrief"),
        (out, "AgentOutput"),
    ]:
        _no_datetime_in(to_dict(obj), label)
    print("  no datetime in defaults  OK")


# ── Phase 9.1 evidence dataclass tests ───────────────────────────────────────

def test_evidence_chunk() -> None:
    chunk = EvidenceChunk(
        task_id="task-123",
        preset_name="fx_cnyaud",
        agent_name="fx_agent",
        content="1 AUD = 4.78 CNY, 90日波动率 0.003",
        source="https://open.er-api.com/v6/latest/CNY",
        category="fx_price",
        importance=0.8,
        confidence=0.65,
        entities=["AUD", "CNY", "RBA"],
        used_in_brief=True,
        ttl_policy="task",
        token_estimate=25,
    )
    d = _json_round_trip(chunk, "EvidenceChunk")
    assert d["agent_name"] == "fx_agent"
    assert d["task_id"] == "task-123"
    assert d["preset_name"] == "fx_cnyaud"
    assert d["importance"] == 0.8
    assert d["confidence"] == 0.65
    assert d["entities"] == ["AUD", "CNY", "RBA"]
    assert d["used_in_brief"] is True
    assert d["ttl_policy"] == "task"
    assert d["chunk_id"].startswith("chunk-")
    assert isinstance(d["created_at"], str)

    chunk2 = EvidenceChunk.from_dict(d)
    assert chunk2.content == chunk.content
    assert chunk2.source == chunk.source
    assert chunk2.task_id == "task-123"
    assert chunk2.entities == ["AUD", "CNY", "RBA"]
    assert chunk2.used_in_brief is True
    assert chunk.to_dict() == d

    # 向后兼容：缺失新字段时使用默认值
    chunk3 = EvidenceChunk.from_dict({"agent_name": "a"})
    assert chunk3.task_id == ""
    assert chunk3.preset_name == ""
    assert chunk3.confidence == 0.0
    assert chunk3.entities == []
    assert chunk3.used_in_brief is False
    print("  EvidenceChunk            OK")


def test_evidence_chunk_validation() -> None:
    _expect_error(
        lambda: EvidenceChunk(importance=1.5),
        ValueError, "EvidenceChunk(importance=1.5)",
    )
    _expect_error(
        lambda: EvidenceChunk(confidence=-0.1),
        ValueError, "EvidenceChunk(confidence=-0.1)",
    )
    _expect_error(
        lambda: EvidenceChunk(ttl_policy="forever"),
        ValueError, "EvidenceChunk(ttl_policy='forever')",
    )
    # 边界值
    c0 = EvidenceChunk(importance=0.0, confidence=0.0)
    assert c0.importance == 0.0
    c1 = EvidenceChunk(importance=1.0, confidence=1.0)
    assert c1.confidence == 1.0
    print("  EvidenceChunk validation OK")


def test_evidence_finding() -> None:
    ef = EvidenceFinding(
        agent_name="news_agent",
        key="rba_hold",
        summary="RBA 维持利率不变",
        direction="bearish_aud",
        chunk_ids=["chunk-abc", "chunk-def"],
        evidence_score=0.75,
        category="macro",
        importance=0.6,
    )
    d = _json_round_trip(ef, "EvidenceFinding")
    assert d["finding_id"].startswith("find-")
    assert d["chunk_ids"] == ["chunk-abc", "chunk-def"]
    assert d["evidence_score"] == 0.75

    ef2 = EvidenceFinding.from_dict(d)
    assert ef2.key == "rba_hold"
    assert ef2.chunk_ids == ef.chunk_ids
    assert ef.to_dict() == d
    print("  EvidenceFinding          OK")


def test_evidence_finding_validation() -> None:
    _expect_error(
        lambda: EvidenceFinding(evidence_score=2.0),
        ValueError, "EvidenceFinding(evidence_score=2.0)",
    )
    _expect_error(
        lambda: EvidenceFinding(importance=-0.1),
        ValueError, "EvidenceFinding(importance=-0.1)",
    )
    # None 始终有效
    ef = EvidenceFinding(evidence_score=None)
    assert ef.evidence_score is None
    # 边界值 0.0 和 1.0
    ef0 = EvidenceFinding(evidence_score=0.0, importance=0.0)
    assert ef0.evidence_score == 0.0
    ef1 = EvidenceFinding(evidence_score=1.0, importance=1.0)
    assert ef1.evidence_score == 1.0
    print("  EvidenceFinding valid.   OK")


def test_citation_ref() -> None:
    cr = CitationRef(
        chunk_id="chunk-abc",
        finding_id="find-xyz",
        section_title="汇率事实",
        relevance_score=0.9,
    )
    d = _json_round_trip(cr, "CitationRef")
    assert d["citation_id"].startswith("cite-")
    assert d["chunk_id"] == "chunk-abc"
    assert d["relevance_score"] == 0.9

    cr2 = CitationRef.from_dict(d)
    assert cr2.section_title == "汇率事实"
    assert cr.to_dict() == d
    print("  CitationRef              OK")


def test_citation_ref_validation() -> None:
    _expect_error(
        lambda: CitationRef(relevance_score=1.5),
        ValueError, "CitationRef(relevance_score=1.5)",
    )
    print("  CitationRef validation   OK")


def test_context_pack_item() -> None:
    item = ContextPackItem(
        chunk_id="chunk-abc",
        agent_name="macro_agent",
        text="RBA 利率信号: neutral",
        relevance_score=0.85,
        token_estimate=12,
    )
    d = _json_round_trip(item, "ContextPackItem")
    assert d["relevance_score"] == 0.85

    item2 = ContextPackItem.from_dict(d)
    assert item2.chunk_id == "chunk-abc"
    assert item.to_dict() == d
    print("  ContextPackItem          OK")


def test_context_pack() -> None:
    pack = ContextPack(
        items=[
            ContextPackItem(chunk_id="c1", agent_name="fx_agent", text="rate data", relevance_score=0.9, token_estimate=10),
            ContextPackItem(chunk_id="c2", agent_name="news_agent", text="news data", relevance_score=0.7, token_estimate=15),
        ],
        total_tokens=25,
        budget_tokens=2048,
        coverage={"fx_agent": 1, "news_agent": 1},
    )
    d = _json_round_trip(pack, "ContextPack")
    assert len(d["items"]) == 2
    assert d["total_tokens"] == 25
    assert d["coverage"]["fx_agent"] == 1

    pack2 = ContextPack.from_dict(d)
    assert len(pack2.items) == 2
    assert pack2.items[0].chunk_id == "c1"
    assert pack.to_dict() == d
    print("  ContextPack              OK")


def test_retrieval_trace() -> None:
    trace = RetrievalTrace(
        query="CNY/AUD 汇率事实",
        retrieved_count=5,
        total_chunks=20,
        top_scores=[0.95, 0.88, 0.72, 0.65, 0.51],
        latency_ms=42,
    )
    d = _json_round_trip(trace, "RetrievalTrace")
    assert d["trace_id"].startswith("trace-")
    assert d["retrieved_count"] == 5
    assert len(d["top_scores"]) == 5
    assert isinstance(d["timestamp"], str)

    trace2 = RetrievalTrace.from_dict(d)
    assert trace2.query == "CNY/AUD 汇率事实"
    assert trace.to_dict() == d
    print("  RetrievalTrace           OK")


def test_retrieval_trace_score_validation() -> None:
    """P2: top_scores 每个元素必须在 [0,1] 范围内。"""
    _expect_error(
        lambda: RetrievalTrace(top_scores=[0.9, 1.5]),
        ValueError, "RetrievalTrace(top_scores=[0.9, 1.5])",
    )
    _expect_error(
        lambda: RetrievalTrace(top_scores=[-0.1]),
        ValueError, "RetrievalTrace(top_scores=[-0.1])",
    )
    # 边界值有效
    t = RetrievalTrace(top_scores=[0.0, 0.5, 1.0])
    assert t.top_scores == [0.0, 0.5, 1.0]
    print("  RetrievalTrace scores    OK")


def test_context_pack_coverage_int() -> None:
    """P2: coverage 的 from_dict 应将 float 值强制转换为 int。"""
    raw = {"coverage": {"fx_agent": 1.0, "news_agent": 2.0}, "total_tokens": 100}
    pack = ContextPack.from_dict(raw)
    assert pack.coverage["fx_agent"] == 1
    assert isinstance(pack.coverage["fx_agent"], int)
    assert pack.coverage["news_agent"] == 2
    assert isinstance(pack.coverage["news_agent"], int)
    print("  ContextPack coverage int OK")


def test_validate_ttl_policy() -> None:
    assert validate_ttl_policy("session") == "session"
    assert validate_ttl_policy("task") == "task"
    assert validate_ttl_policy("persistent") == "persistent"
    _expect_error(lambda: validate_ttl_policy("forever"), ValueError, "validate_ttl_policy('forever')")
    _expect_error(lambda: validate_ttl_policy(""), ValueError, "validate_ttl_policy('')")
    print("  validate_ttl_policy      OK")


def test_agent_output_evidence_fields() -> None:
    """Phase 9.1: AgentOutput gains chunk_ids, finding_ids, evidence_count."""
    out = AgentOutput(
        agent_name="fx_agent",
        status="ok",
        chunk_ids=["chunk-1", "chunk-2"],
        finding_ids=["find-1"],
        evidence_count=2,
    )
    d = _json_round_trip(out, "AgentOutput+evidence")
    assert d["chunk_ids"] == ["chunk-1", "chunk-2"]
    assert d["finding_ids"] == ["find-1"]
    assert d["evidence_count"] == 2

    out2 = AgentOutput.from_dict(d)
    assert out2.chunk_ids == ["chunk-1", "chunk-2"]
    assert out2.evidence_count == 2

    # Backward compat: old dict without new fields
    out3 = AgentOutput.from_dict({"agent_name": "a", "status": "ok"})
    assert out3.chunk_ids == []
    assert out3.finding_ids == []
    assert out3.evidence_count == 0
    print("  AgentOutput evidence     OK")


def test_research_section_evidence_fields() -> None:
    """Phase 9.1: ResearchSection gains chunk_ids, citation_ids."""
    sec = ResearchSection(
        title="汇率事实",
        content="1 AUD = 4.78 CNY",
        source_agents=["fx_agent"],
        chunk_ids=["chunk-a"],
        citation_ids=["cite-x"],
    )
    d = _json_round_trip(sec, "ResearchSection+evidence")
    assert d["chunk_ids"] == ["chunk-a"]
    assert d["citation_ids"] == ["cite-x"]

    sec2 = ResearchSection.from_dict(d)
    assert sec2.chunk_ids == ["chunk-a"]

    # Backward compat
    sec3 = ResearchSection.from_dict({"title": "t", "content": "c"})
    assert sec3.chunk_ids == []
    assert sec3.citation_ids == []
    print("  ResearchSection evidence OK")


def test_research_brief_retrieval_traces() -> None:
    """Phase 9.1: ResearchBrief gains retrieval_traces."""
    trace = RetrievalTrace(query="test", retrieved_count=3, total_chunks=10)
    brief = ResearchBrief(
        task_id="t1",
        preset_name="fx_cnyaud",
        retrieval_traces=[trace],
    )
    d = _json_round_trip(brief, "ResearchBrief+traces")
    assert len(d["retrieval_traces"]) == 1
    assert d["retrieval_traces"][0]["query"] == "test"

    brief2 = ResearchBrief.from_dict(d)
    assert len(brief2.retrieval_traces) == 1
    assert brief2.retrieval_traces[0].retrieved_count == 3

    # Backward compat
    brief3 = ResearchBrief.from_dict({"task_id": "t2", "preset_name": "fx_cnyaud"})
    assert brief3.retrieval_traces == []
    assert brief3.disclaimer == _FIXED_DISCLAIMER
    print("  ResearchBrief traces     OK")


def test_evidence_defaults_no_datetime() -> None:
    """All Phase 9.1 default factories produce strings, not datetime objects."""
    for obj, label in [
        (EvidenceChunk(), "EvidenceChunk"),
        (EvidenceFinding(), "EvidenceFinding"),
        (CitationRef(), "CitationRef"),
        (ContextPackItem(), "ContextPackItem"),
        (ContextPack(), "ContextPack"),
        (RetrievalTrace(), "RetrievalTrace"),
    ]:
        _no_datetime_in(to_dict(obj), label)
    print("  evidence no-datetime     OK")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all() -> None:
    tests = [
        # ── Original Phase 9 tests ───────────────────────────────────────
        test_safe_user_context,
        test_research_preset_p1_fields,
        test_research_task,
        test_source_ref,
        test_finding,
        test_finding_score_validation,
        test_agent_output,
        test_agent_output_validation_p0,
        test_agent_output_make_error,
        test_research_brief_disclaimer_p0,
        test_research_brief_nested,
        test_to_dict_datetime_guard_p0,
        test_preset_registry,
        test_agent_registry_not_in_schema,
        test_validators,
        test_no_datetime_objects_in_defaults,
        # ── Phase 9.1 evidence layer tests ───────────────────────────────
        test_evidence_chunk,
        test_evidence_chunk_validation,
        test_evidence_finding,
        test_evidence_finding_validation,
        test_citation_ref,
        test_citation_ref_validation,
        test_context_pack_item,
        test_context_pack,
        test_retrieval_trace,
        test_retrieval_trace_score_validation,
        test_context_pack_coverage_int,
        test_validate_ttl_policy,
        test_agent_output_evidence_fields,
        test_research_section_evidence_fields,
        test_research_brief_retrieval_traces,
        test_evidence_defaults_no_datetime,
    ]
    print("Phase 9 + 9.1 — Schema tests")
    print("=" * 50)
    for test_fn in tests:
        test_fn()
    print("=" * 50)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        sys.exit(1)
