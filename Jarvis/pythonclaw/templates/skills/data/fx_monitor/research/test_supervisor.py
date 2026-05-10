#!/usr/bin/env python3
"""
Phase 9 Step 5 — SupervisorReportWriter standalone tests.

_call_llm is mocked throughout — no network, no real LLM.

Tests:
  1.  test_ok_output              — LLM returns valid JSON → ResearchBrief with sections
  2.  test_section_order          — sections follow preset.report_sections order
  3.  test_missing_llm_section    — LLM omits one section → placeholder inserted
  4.  test_single_error_agent     — one agent error → has_data_gap=True in sections
  5.  test_banned_terms_filter    — LLM injects banned term → filtered in output
  6.  test_fallback_path          — LLM returns "" → deterministic brief
  7.  test_fallback_has_all_sections — fallback includes all preset.report_sections
  8.  test_disclaimer_hardcoded   — disclaimer never changes regardless of LLM output
  9.  test_sources_summary        — sources_summary built from SourceRef objects
  10. test_no_fabricated_agents   — source_agents in sections are real agent names
  11. test_json_safe              — ResearchBrief.to_dict() is JSON-serialisable

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_supervisor.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest.mock
from pathlib import Path

_HERE         = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput, ContextPack, ContextPackItem, CostEstimate,
    Finding, FX_CNYAUD_PRESET,
    ResearchBrief, ResearchTask, SafeUserContext, SourceRef,
)
import supervisor as _sup  # noqa: E402
from supervisor import SupervisorReportWriter, _DISCLAIMER  # noqa: E402
from evidence_store import EvidenceStore  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

_PRESET  = FX_CNYAUD_PRESET
_SECTIONS = _PRESET.report_sections    # ["汇率事实", "新闻驱动", "宏观信号", "风险与矛盾"]


def _make_task() -> ResearchTask:
    return ResearchTask(
        preset_name    = "fx_cnyaud",
        research_type  = "fx",
        research_topic = "CNY/AUD 外汇研究",
        focus_pair     = "CNY/AUD",
        focus_assets   = ["CNY", "AUD"],
        time_horizon   = "short_term",
        safe_user_context = SafeUserContext(purpose="tuition"),
    )


def _make_cost() -> CostEstimate:
    return CostEstimate(llm_calls=2, estimated_tokens=600, estimated_cost_usd=0.0002)


def _ok_agent(name: str, summary: str = "测试摘要", with_sources: bool = False) -> AgentOutput:
    out = AgentOutput(
        agent_name = name,
        status     = "ok",
        summary    = summary,
        confidence = 0.7,
        findings   = [Finding(key="f1", summary=f"{name} 发现1", direction="neutral")],
    )
    if with_sources:
        out.sources = [SourceRef(
            title       = f"{name} 数据来源",
            url         = f"https://example.com/{name}",
            source      = "mock_source",
            retrieved_at= "2026-05-02T10:00:00+00:00",
            published_at= "2026-05-02T08:00:00+00:00",
        )]
    return out


def _error_agent(name: str) -> AgentOutput:
    return AgentOutput.make_error(name, "network timeout", latency_ms=100)


def _partial_agent(name: str) -> AgentOutput:
    out = AgentOutput(
        agent_name   = name,
        status       = "partial",
        summary      = f"{name} 部分数据",
        confidence   = 0.3,
        missing_data = ["llm_unavailable"],
    )
    return out


# ── LLM mock helpers ──────────────────────────────────────────────────────────

def _make_llm_json(sections_override: list[dict] | None = None) -> str:
    """Build a valid LLM JSON response for FX_CNYAUD_PRESET sections."""
    sections = sections_override or [
        {
            "title":        "汇率事实",
            "content":      "CNY/AUD 今日参考汇率为 4.52，近期波动区间 4.45–4.60。",
            "source_agents":["fx_agent"],
        },
        {
            "title":        "新闻驱动",
            "content":      "RBA 鹰派信号持续，铁矿石价格上涨支撑 AUD。",
            "source_agents":["news_agent"],
        },
        {
            "title":        "宏观信号",
            "content":      "PBoC 维持利率稳定，美元走强对 AUD 形成压力。",
            "source_agents":["macro_agent"],
        },
        {
            "title":        "风险与矛盾",
            "content":      "多空信号方向不一致，需关注 RBA 利率决定。",
            "source_agents":["risk_agent"],
        },
    ]
    return json.dumps({
        "conclusion":  "综合来看，AUD 短期走势存在不确定性，信号较混杂。",
        "user_notes":  "留学生换汇建议关注 RBA 利率动向。",
        "sections":    sections,
    }, ensure_ascii=False)


_MOCK_TOKEN_USAGE = {"prompt_tokens": 500, "completion_tokens": 280}


def _mock_llm_ok(response: str | None = None):
    resp = response or _make_llm_json()
    return unittest.mock.patch.object(
        _sup, "_call_llm",
        return_value=(resp, _MOCK_TOKEN_USAGE),
    )


def _mock_llm_fail():
    return unittest.mock.patch.object(
        _sup, "_call_llm",
        return_value=("", {}),
    )


def _mock_llm_text(text: str):
    return unittest.mock.patch.object(
        _sup, "_call_llm",
        return_value=(text, _MOCK_TOKEN_USAGE),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_ok_output() -> None:
    """LLM returns valid JSON → ResearchBrief with correct structure."""
    outputs = [
        _ok_agent("fx_agent"),
        _ok_agent("news_agent"),
        _ok_agent("macro_agent"),
        _ok_agent("risk_agent"),
    ]
    with _mock_llm_ok():
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert brief.task_id
    assert brief.preset_name == "fx_cnyaud"
    assert brief.conclusion
    assert len(brief.sections) == len(_SECTIONS)
    assert brief.disclaimer == _DISCLAIMER
    assert brief.agent_statuses == {o.agent_name: o.status for o in outputs}
    assert brief.cost_estimate.llm_calls == _make_cost().llm_calls + 1  # +1 supervisor call

    print("\n-- test_ok_output")
    print(f"   sections={[s.title for s in brief.sections]}")
    print(f"   conclusion: {brief.conclusion[:60]}")
    print(f"   cost: calls={brief.cost_estimate.llm_calls} tokens={brief.cost_estimate.estimated_tokens}")
    print("   PASS")


async def test_section_order() -> None:
    """Sections in ResearchBrief match preset.report_sections order exactly."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    with _mock_llm_ok():
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    titles = [s.title for s in brief.sections]
    assert titles == _SECTIONS, f"Section order mismatch: {titles} vs {_SECTIONS}"

    print("\n-- test_section_order")
    print(f"   order: {titles}")
    print("   PASS")


async def test_missing_llm_section() -> None:
    """LLM omits one section → placeholder inserted at correct position."""
    # Only 3 of 4 sections returned by LLM
    partial_llm = _make_llm_json([
        {"title": "汇率事实", "content": "汇率内容", "source_agents": ["fx_agent"]},
        {"title": "新闻驱动", "content": "新闻内容", "source_agents": ["news_agent"]},
        {"title": "宏观信号", "content": "宏观内容", "source_agents": ["macro_agent"]},
        # "风险与矛盾" deliberately missing
    ])
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    with _mock_llm_text(partial_llm):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert len(brief.sections) == len(_SECTIONS), "Should have all sections"
    last = brief.sections[-1]
    assert last.title == "风险与矛盾"
    assert last.has_data_gap, "Missing section should have has_data_gap=True"

    print("\n-- test_missing_llm_section")
    print(f"   last section: title={last.title}  has_gap={last.has_data_gap}")
    print(f"   content: {last.content[:60]}")
    print("   PASS")


async def test_single_error_agent() -> None:
    """One error agent → its section has has_data_gap=True, data_gaps populated."""
    outputs = [
        _ok_agent("fx_agent"),
        _error_agent("news_agent"),
        _ok_agent("macro_agent"),
        _ok_agent("risk_agent"),
    ]
    llm_json = _make_llm_json([
        {"title": "汇率事实",  "content": "汇率内容", "source_agents": ["fx_agent"]},
        {"title": "新闻驱动",  "content": "新闻数据不可用", "source_agents": ["news_agent"]},
        {"title": "宏观信号",  "content": "宏观内容", "source_agents": ["macro_agent"]},
        {"title": "风险与矛盾","content": "风险内容", "source_agents": ["risk_agent"]},
    ])
    with _mock_llm_text(llm_json):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    # The news_agent section must flag the gap
    news_sec = next(s for s in brief.sections if s.title == "新闻驱动")
    assert news_sec.has_data_gap, "Error agent should cause has_data_gap=True"
    assert "news_agent" in brief.data_gaps, f"data_gaps should mention news_agent: {brief.data_gaps}"
    assert brief.agent_statuses["news_agent"] == "error"

    print("\n-- test_single_error_agent")
    print(f"   news_sec.has_data_gap={news_sec.has_data_gap}")
    print(f"   data_gaps: {brief.data_gaps[:80]}")
    print("   PASS")


async def test_banned_terms_filter() -> None:
    """LLM injects banned term → post-generation filter removes it."""
    banned_term = _PRESET.banned_terms[0]  # e.g. "建议买入"
    dirty_json = json.dumps({
        "conclusion":  f"综合来看{banned_term}，信号较混杂。",
        "user_notes":  "留学生注意",
        "sections": [
            {"title": "汇率事实",  "content": f"价格合适，{banned_term}",  "source_agents": ["fx_agent"]},
            {"title": "新闻驱动",  "content": "新闻正常",                  "source_agents": ["news_agent"]},
            {"title": "宏观信号",  "content": "宏观正常",                  "source_agents": ["macro_agent"]},
            {"title": "风险与矛盾","content": "风险正常",                  "source_agents": ["risk_agent"]},
        ],
    }, ensure_ascii=False)

    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    with _mock_llm_text(dirty_json):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    all_text = (
        brief.conclusion
        + brief.user_notes
        + "".join(s.content for s in brief.sections)
    )
    for term in _PRESET.banned_terms:
        assert term not in all_text, f"Banned term {term!r} found in output"

    print("\n-- test_banned_terms_filter")
    print(f"   banned term {banned_term!r} removed from conclusion and sections")
    print(f"   conclusion: {brief.conclusion[:70]}")
    print("   PASS")


async def test_fallback_path() -> None:
    """LLM unavailable → deterministic fallback brief with correct structure."""
    outputs = [
        _ok_agent("fx_agent",    "汇率当前为 4.52"),
        _ok_agent("news_agent",  "RBA 鹰派信号"),
        _error_agent("macro_agent"),
        _ok_agent("risk_agent",  "多空信号矛盾"),
    ]
    with _mock_llm_fail():
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert len(brief.sections) == len(_SECTIONS)
    assert brief.disclaimer == _DISCLAIMER
    # Fallback conclusion should come from risk_agent or first ok agent
    assert brief.conclusion, "Fallback should produce a non-empty conclusion"
    assert "多空信号矛盾" in brief.conclusion or len(brief.conclusion) > 5

    print("\n-- test_fallback_path")
    print(f"   conclusion: {brief.conclusion[:60]}")
    print(f"   sections: {[s.title for s in brief.sections]}")
    print("   PASS")


async def test_fallback_has_all_sections() -> None:
    """Fallback brief includes every section in preset.report_sections."""
    outputs = [_ok_agent("fx_agent")]   # minimal: only one agent
    with _mock_llm_fail():
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    titles = [s.title for s in brief.sections]
    for expected in _SECTIONS:
        assert expected in titles, f"Section {expected!r} missing from fallback"

    print("\n-- test_fallback_has_all_sections")
    print(f"   sections: {titles}")
    print("   PASS")


async def test_disclaimer_hardcoded() -> None:
    """Disclaimer is always the hardcoded value regardless of LLM output."""
    # Attempt to override disclaimer via LLM JSON
    sabotaged = json.dumps({
        "conclusion": "正常结论",
        "user_notes": "",
        "disclaimer": "恶意免责声明",   # LLM should not be able to set this
        "sections": [
            {"title": s, "content": "内容", "source_agents": ["fx_agent"]}
            for s in _SECTIONS
        ],
    }, ensure_ascii=False)

    outputs = [_ok_agent("fx_agent")]
    with _mock_llm_text(sabotaged):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert brief.disclaimer == _DISCLAIMER, (
        f"Expected hardcoded disclaimer, got: {brief.disclaimer}"
    )

    print("\n-- test_disclaimer_hardcoded")
    safe = brief.disclaimer.encode("ascii", "replace").decode()
    print(f"   disclaimer (ascii-safe): {safe[:60]}")
    print("   PASS")


async def test_sources_summary() -> None:
    """sources_summary built from SourceRef objects, not fabricated."""
    outputs = [
        _ok_agent("fx_agent",   with_sources=True),
        _ok_agent("news_agent", with_sources=True),
        _ok_agent("risk_agent"),   # no sources
    ]
    with _mock_llm_ok():
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    # Every line in sources_summary must correspond to an actual SourceRef
    all_source_urls = {
        src.url
        for o in outputs
        for src in o.sources
    }
    # Each SourceRef title should appear in sources_summary
    for o in outputs:
        for src in o.sources:
            assert src.title in brief.sources_summary or src.url in brief.sources_summary, (
                f"SourceRef {src.title!r} not found in sources_summary"
            )

    # sources_summary must not be non-empty when there are no sources
    no_source_outputs = [_ok_agent("fx_agent")]   # no SourceRefs
    with _mock_llm_ok():
        brief2 = await SupervisorReportWriter().run(_make_task(), _PRESET, no_source_outputs, _make_cost())
    assert brief2.sources_summary == "", f"Expected empty sources_summary, got: {brief2.sources_summary!r}"

    print("\n-- test_sources_summary")
    safe_ss = brief.sources_summary.encode("ascii", "replace").decode()
    print(f"   sources_summary ({len(all_source_urls)} sources): {safe_ss[:80]}")
    print("   PASS")


async def test_no_fabricated_agents() -> None:
    """source_agents in every section are real agent names from outputs."""
    outputs = [
        _ok_agent("fx_agent"),
        _ok_agent("news_agent"),
        _ok_agent("macro_agent"),
        _ok_agent("risk_agent"),
    ]
    # LLM tries to inject a fabricated agent name
    llm_with_fake = json.dumps({
        "conclusion": "结论",
        "user_notes": "",
        "sections": [
            {"title": "汇率事实",  "content": "内容", "source_agents": ["fx_agent", "FAKE_AGENT"]},
            {"title": "新闻驱动",  "content": "内容", "source_agents": ["news_agent"]},
            {"title": "宏观信号",  "content": "内容", "source_agents": ["macro_agent"]},
            {"title": "风险与矛盾","content": "内容", "source_agents": ["risk_agent"]},
        ],
    }, ensure_ascii=False)

    valid_names = {o.agent_name for o in outputs}
    with _mock_llm_text(llm_with_fake):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    for sec in brief.sections:
        for agent in sec.source_agents:
            assert agent in valid_names, (
                f"Fabricated agent {agent!r} in section {sec.title!r}"
            )

    print("\n-- test_no_fabricated_agents")
    for s in brief.sections:
        print(f"   [{s.title}] source_agents={s.source_agents}")
    print("   PASS")


async def test_json_safe() -> None:
    """ResearchBrief.to_dict() serialises to valid JSON for all three paths."""
    outputs = [
        _ok_agent("fx_agent",   with_sources=True),
        _ok_agent("news_agent"),
        _error_agent("macro_agent"),
        _ok_agent("risk_agent"),
    ]

    # Path 1: LLM ok
    with _mock_llm_ok():
        b1 = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())
    raw1 = json.dumps(b1.to_dict(), ensure_ascii=False)
    json.loads(raw1)   # must not raise

    # Path 2: LLM fallback
    with _mock_llm_fail():
        b2 = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())
    raw2 = json.dumps(b2.to_dict(), ensure_ascii=False)
    json.loads(raw2)

    print("\n-- test_json_safe")
    print(f"   llm_ok JSON:    {len(raw1)} chars")
    print(f"   fallback JSON:  {len(raw2)} chars")
    print("   PASS")


async def test_user_notes_from_safe_ctx() -> None:
    """user_notes in brief is derived from SafeUserContext, not from LLM output."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]

    # LLM JSON includes a user_notes field — should be ignored by supervisor
    llm_with_notes = json.dumps({
        "conclusion": "结论",
        "user_notes": "这是 LLM 生成的 user_notes，不应出现",   # should be discarded
        "sections": [
            {"title": s, "content": "内容", "source_agents": ["fx_agent"]}
            for s in _SECTIONS
        ],
    }, ensure_ascii=False)

    # Test 1: purpose="tuition" → deterministic note
    task_tuition = ResearchTask(
        preset_name="fx_cnyaud", research_type="fx", research_topic="test",
        safe_user_context=SafeUserContext(purpose="tuition"),
    )
    with _mock_llm_text(llm_with_notes):
        brief = await SupervisorReportWriter().run(task_tuition, _PRESET, outputs, _make_cost())

    assert "LLM 生成的 user_notes" not in brief.user_notes, (
        f"LLM user_notes leaked into brief: {brief.user_notes}"
    )
    assert "留学" in brief.user_notes or "tuition" in brief.user_notes.lower() or brief.user_notes == "", (
        f"Expected tuition-related note, got: {brief.user_notes}"
    )

    # Test 2: fallback path also uses SafeUserContext
    with _mock_llm_fail():
        brief_fb = await SupervisorReportWriter().run(task_tuition, _PRESET, outputs, _make_cost())

    assert "LLM 生成的 user_notes" not in brief_fb.user_notes

    print("\n-- test_user_notes_from_safe_ctx")
    print(f"   llm path user_notes:      {brief.user_notes[:60]}")
    print(f"   fallback path user_notes: {brief_fb.user_notes[:60]}")
    print("   PASS")


async def test_provenance_note_on_unattributed_section() -> None:
    """
    LLM returns a section with no valid source_agents →
    content gets a provenance warning and has_data_gap=True.
    """
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]

    # One section omits source_agents; another uses a fabricated agent name
    llm_no_source = json.dumps({
        "conclusion": "结论",
        "sections": [
            {"title": "汇率事实",  "content": "汇率内容",           "source_agents": []},           # empty
            {"title": "新闻驱动",  "content": "新闻内容",           "source_agents": ["FAKE_BOT"]},  # fabricated only
            {"title": "宏观信号",  "content": "宏观内容",           "source_agents": ["macro_agent"]},
            {"title": "风险与矛盾","content": "风险内容",           "source_agents": ["risk_agent"]},
        ],
    }, ensure_ascii=False)

    from supervisor import _UNVERIFIABLE_NOTE  # noqa: PLC0415
    with _mock_llm_text(llm_no_source):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    # Section with empty source_agents
    rate_sec = next(s for s in brief.sections if s.title == "汇率事实")
    assert rate_sec.has_data_gap,           "Empty source_agents should set has_data_gap=True"
    assert _UNVERIFIABLE_NOTE in rate_sec.content, (
        f"Provenance note missing from unattributed section. content={rate_sec.content!r}"
    )
    assert rate_sec.source_agents == [],    "Empty source_agents should stay empty"

    # Section with only fabricated agent (FAKE_BOT stripped → becomes empty → also flagged)
    news_sec = next(s for s in brief.sections if s.title == "新闻驱动")
    assert news_sec.has_data_gap,           "Fabricated-only source_agents should set has_data_gap=True"
    assert _UNVERIFIABLE_NOTE in news_sec.content

    # Section with real agent is untouched
    macro_sec = next(s for s in brief.sections if s.title == "宏观信号")
    assert not macro_sec.has_data_gap,      "Real agent with ok status should not have gap"
    assert _UNVERIFIABLE_NOTE not in macro_sec.content

    print("\n-- test_provenance_note_on_unattributed_section")
    print(f"   rate_sec:  has_gap={rate_sec.has_data_gap}  note_present={_UNVERIFIABLE_NOTE in rate_sec.content}")
    print(f"   news_sec:  has_gap={news_sec.has_data_gap}  note_present={_UNVERIFIABLE_NOTE in news_sec.content}")
    print(f"   macro_sec: has_gap={macro_sec.has_data_gap} note_absent={_UNVERIFIABLE_NOTE not in macro_sec.content}")
    print("   PASS")


# ── Phase 9.1 Step 6 — ContextPack integration tests ─────────────────────────

def _make_context_pack() -> ContextPack:
    """Build a small ContextPack with known chunk_ids for testing."""
    return ContextPack(
        items=[
            ContextPackItem(
                chunk_id="chunk-fx-1",
                agent_name="fx_agent",
                text="[Context]\nCNY/AUD 汇率 4.52\n[/Context]\n汇率上涨",
                relevance_score=0.9,
                token_estimate=30,
            ),
            ContextPackItem(
                chunk_id="chunk-news-1",
                agent_name="news_agent",
                text="[Context]\nRBA 鹰派\n[/Context]\nRBA 维持利率",
                relevance_score=0.8,
                token_estimate=25,
            ),
            ContextPackItem(
                chunk_id="chunk-macro-1",
                agent_name="macro_agent",
                text="[Context]\nPBoC 稳定\n[/Context]\n宏观信号平稳",
                relevance_score=0.7,
                token_estimate=20,
            ),
        ],
        total_tokens=75,
        budget_tokens=4000,
        coverage={"fx_agent": 1, "news_agent": 1, "macro_agent": 1},
    )


def _make_llm_json_with_chunks() -> str:
    """LLM response that references chunk_ids from the context pack."""
    return json.dumps({
        "conclusion": "综合来看，AUD 短期走势存在不确定性。",
        "sections": [
            {
                "title": "汇率事实",
                "content": "CNY/AUD 汇率今日参考值为 4.52 [chunk-fx-1]。",
                "source_agents": ["fx_agent"],
                "chunk_ids": ["chunk-fx-1"],
            },
            {
                "title": "新闻驱动",
                "content": "RBA 鹰派信号持续 [chunk-news-1]。",
                "source_agents": ["news_agent"],
                "chunk_ids": ["chunk-news-1"],
            },
            {
                "title": "宏观信号",
                "content": "PBoC 维持利率稳定 [chunk-macro-1]。",
                "source_agents": ["macro_agent"],
                "chunk_ids": ["chunk-macro-1"],
            },
            {
                "title": "风险与矛盾",
                "content": "多空信号方向不一致。",
                "source_agents": ["risk_agent"],
                "chunk_ids": [],
            },
        ],
    }, ensure_ascii=False)


async def test_context_pack_used() -> None:
    """When ContextPack is available, supervisor uses evidence-based prompt."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    pack = _make_context_pack()

    with _mock_llm_text(_make_llm_json_with_chunks()), \
         unittest.mock.patch.object(
             _sup, "EvidenceStore",
             lambda: _StubStore(pack),
         ):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    fx_sec = next(s for s in brief.sections if s.title == "汇率事实")
    assert "chunk-fx-1" in fx_sec.chunk_ids
    assert "chunk-fx-1" in fx_sec.content
    assert "CNY/AUD 汇率" not in fx_sec.content
    assert "1 AUD = X CNY 口径" in fx_sec.content

    print("\n-- test_context_pack_used")
    print(f"   fx_sec.chunk_ids={fx_sec.chunk_ids}")
    print("   PASS")


class _StubStore:
    """Minimal EvidenceStore stub for supervisor tests."""
    def __init__(self, pack: ContextPack | None = None):
        self._pack = pack or ContextPack()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def build_context_pack(self, *_args, **_kwargs) -> ContextPack:
        return self._pack

    def list_traces(self, _task_id: str):
        return []


async def test_context_pack_store_failure() -> None:
    """EvidenceStore failure falls back to old agent-based prompt behavior."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]

    class _BrokenStore:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def build_context_pack(self, *_a, **_kw):
            raise RuntimeError("disk full")

    with _mock_llm_ok(), \
         unittest.mock.patch.object(_sup, "EvidenceStore", _BrokenStore):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert len(brief.sections) == len(_SECTIONS)
    assert brief.conclusion

    print("\n-- test_context_pack_store_failure")
    print(f"   sections={len(brief.sections)}  conclusion present=True")
    print("   PASS")


async def test_empty_context_pack_data_gap() -> None:
    """Empty ContextPack should not cause hallucination — falls back to agent prompt."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    empty_pack = ContextPack(items=[], total_tokens=0, budget_tokens=4000, coverage={})

    with _mock_llm_ok(), \
         unittest.mock.patch.object(_sup, "EvidenceStore", lambda: _StubStore(empty_pack)):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert len(brief.sections) == len(_SECTIONS)

    print("\n-- test_empty_context_pack_data_gap")
    print(f"   sections={len(brief.sections)}  (used agent fallback prompt)")
    print("   PASS")


async def test_banned_filter_with_context_pack() -> None:
    """Banned-term filter still works when ContextPack is used."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    pack = _make_context_pack()

    banned_term = _PRESET.banned_terms[0]
    dirty_json = json.dumps({
        "conclusion": f"综合来看{banned_term}，信号较混杂。",
        "sections": [
            {"title": s, "content": f"内容{banned_term}", "source_agents": ["fx_agent"], "chunk_ids": []}
            for s in _SECTIONS
        ],
    }, ensure_ascii=False)

    with _mock_llm_text(dirty_json), \
         unittest.mock.patch.object(_sup, "EvidenceStore", lambda: _StubStore(pack)):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    all_text = brief.conclusion + "".join(s.content for s in brief.sections)
    for term in _PRESET.banned_terms:
        assert term not in all_text, f"Banned term {term!r} found with ContextPack"

    print("\n-- test_banned_filter_with_context_pack")
    print(f"   banned term '{banned_term}' filtered out")
    print("   PASS")


async def test_context_pack_constructor_oserror() -> None:
    """EvidenceStore raises OSError at construction → falls back to agent prompt."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]

    def _raise_os(*_a, **_kw):
        raise OSError("read-only filesystem")

    with _mock_llm_ok(), \
         unittest.mock.patch.object(_sup, "EvidenceStore", _raise_os):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert len(brief.sections) == len(_SECTIONS)
    assert brief.conclusion

    print("\n-- test_context_pack_constructor_oserror")
    print("   OSError → agent prompt fallback OK")
    print("   PASS")


async def test_context_pack_sqlite_locked() -> None:
    """OperationalError (database locked) during build_context_pack → fallback."""
    import sqlite3 as _sqlite3
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]

    class _LockedStore:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def build_context_pack(self, *_a, **_kw):
            raise _sqlite3.OperationalError("database is locked")

    with _mock_llm_ok(), \
         unittest.mock.patch.object(_sup, "EvidenceStore", _LockedStore):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert len(brief.sections) == len(_SECTIONS)

    print("\n-- test_context_pack_sqlite_locked")
    print("   OperationalError (locked) → fallback OK")
    print("   PASS")


async def test_context_pack_permission_error() -> None:
    """PermissionError during EvidenceStore open → fallback."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]

    def _raise_perm(*_a, **_kw):
        raise PermissionError("evidence.sqlite3: permission denied")

    with _mock_llm_ok(), \
         unittest.mock.patch.object(_sup, "EvidenceStore", _raise_perm):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert len(brief.sections) == len(_SECTIONS)

    print("\n-- test_context_pack_permission_error")
    print("   PermissionError → fallback OK")
    print("   PASS")


async def test_trace_retrieval_failure_non_fatal() -> None:
    """list_traces failure → brief still produced, retrieval_traces is empty."""
    outputs = [_ok_agent(a) for a in ("fx_agent", "news_agent", "macro_agent", "risk_agent")]
    pack = _make_context_pack()

    class _TraceFailStore:
        def __init__(self):
            self._pack = pack
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def build_context_pack(self, *_a, **_kw):
            return self._pack
        def list_traces(self, _task_id):
            raise RuntimeError("trace table corrupted")

    with _mock_llm_text(_make_llm_json_with_chunks()), \
         unittest.mock.patch.object(_sup, "EvidenceStore", _TraceFailStore):
        brief = await SupervisorReportWriter().run(_make_task(), _PRESET, outputs, _make_cost())

    assert isinstance(brief, ResearchBrief)
    assert brief.retrieval_traces == []

    print("\n-- test_trace_retrieval_failure_non_fatal")
    print("   list_traces failure → empty traces, brief OK")
    print("   PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 5 -- SupervisorReportWriter tests (mocked LLM)")
    print("=" * 60)

    await test_ok_output()
    await test_section_order()
    await test_missing_llm_section()
    await test_single_error_agent()
    await test_banned_terms_filter()
    await test_fallback_path()
    await test_fallback_has_all_sections()
    await test_disclaimer_hardcoded()
    await test_sources_summary()
    await test_no_fabricated_agents()
    await test_json_safe()
    await test_user_notes_from_safe_ctx()
    await test_provenance_note_on_unattributed_section()
    # Phase 9.1 Step 6 — ContextPack integration
    await test_context_pack_used()
    await test_context_pack_store_failure()
    await test_empty_context_pack_data_gap()
    await test_banned_filter_with_context_pack()
    # Phase 9.1 — EvidenceStore failure resilience
    await test_context_pack_constructor_oserror()
    await test_context_pack_sqlite_locked()
    await test_context_pack_permission_error()
    await test_trace_retrieval_failure_non_fatal()

    print("\n" + "=" * 60)
    print("All 21 tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
