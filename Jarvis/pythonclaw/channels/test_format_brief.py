"""
Tests for _format_research_brief() and feedback helpers.

Run: python test_format_brief.py [-v]
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

# ── Stub heavy dependencies so _telegram_helpers can import without them ──────

# telegram SDK stub — capture callback_data for assertions
class _StubButton:
    def __init__(self, text: str, callback_data: str = ""):
        self.text = text
        self.callback_data = callback_data

class _StubMarkup:
    def __init__(self, rows: list):
        self.inline_keyboard = rows

_tg = type(sys)("telegram")
_tg.InlineKeyboardButton = _StubButton  # type: ignore[attr-defined]
_tg.InlineKeyboardMarkup = _StubMarkup  # type: ignore[attr-defined]
sys.modules.setdefault("telegram", _tg)

# pythonclaw.config stub — must be set before importing _telegram_helpers
import tempfile as _tmpmod
_cfg = type(sys)("pythonclaw.config")
_cfg.PYTHONCLAW_HOME = Path(_tmpmod.gettempdir()) / "pythonclaw_test"
sys.modules.setdefault("pythonclaw.config", _cfg)

# Direct import of the helper module (avoid full package resolution)
_helpers_path = Path(__file__).resolve().parent / "_telegram_helpers.py"
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("_telegram_helpers", _helpers_path,
                                      submodule_search_locations=[])
_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]

# Patch the relative import: _telegram_helpers does `from .. import config`
# which resolves to pythonclaw.config — already stubbed above.
# We also need __package__ so the relative import works.
_mod.__package__ = "pythonclaw.channels"  # type: ignore[attr-defined]

# Ensure parent packages exist in sys.modules
_pc = type(sys)("pythonclaw")
_channels_pkg = type(sys)("pythonclaw.channels")
_channels_pkg.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[attr-defined]
_pc.channels = _channels_pkg  # type: ignore[attr-defined]
sys.modules.setdefault("pythonclaw", _pc)
sys.modules.setdefault("pythonclaw.channels", _channels_pkg)

_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
_format_research_brief = _mod._format_research_brief
_make_feedback_keyboard = _mod._make_feedback_keyboard
_make_preference_declaration_keyboard = _mod._make_preference_declaration_keyboard
_parse_feedback_callback_data = _mod._parse_feedback_callback_data
_parse_preference_declaration_callback_data = _mod._parse_preference_declaration_callback_data
_format_preference_declarations = _mod._format_preference_declarations
_extract_news_topic = _mod._extract_news_topic


# ── Lightweight stand-ins for schema dataclasses ─────────────────────────────

@dataclass
class _CostEstimate:
    llm_calls: int = 2
    estimated_tokens: int = 500
    estimated_cost_usd: float = 0.001
    total_latency_ms: int = 0


@dataclass
class _RetrievalTrace:
    trace_id: str = "t1"
    query: str = ""
    retrieved_count: int = 0
    total_chunks: int = 0
    top_scores: list[float] = field(default_factory=list)
    section_title: str = ""
    selected_chunk_ids: list[str] = field(default_factory=list)
    section_covered: bool = False
    score_distribution: dict = field(default_factory=dict)
    conflict_count: int = 0
    conflict_pairs: list[dict] = field(default_factory=list)
    boosted_chunk_ids: list[str] = field(default_factory=list)
    scoring_method: str = ""


@dataclass
class _Section:
    title: str = "汇率事实"
    content: str = "AUD走强"
    source_agents: list[str] = field(default_factory=lambda: ["fx_agent"])
    has_data_gap: bool = False
    chunk_ids: list[str] = field(default_factory=list)
    citation_ids: list[str] = field(default_factory=list)


@dataclass
class _Brief:
    task_id: str = "abcdef1234567890"
    preset_name: str = "fx_cnyaud"
    generated_at: str = "2026-05-09T12:00:00+00:00"
    conclusion: str = "结论测试"
    sections: list[_Section] = field(default_factory=list)
    user_notes: str = ""
    data_gaps: str = ""
    sources_summary: str = ""
    disclaimer: str = "免责声明"
    agent_statuses: dict[str, str] = field(default_factory=dict)
    cost_estimate: _CostEstimate = field(default_factory=_CostEstimate)
    retrieval_traces: list[_RetrievalTrace] = field(default_factory=list)


# ── Tests ────────────────────────────────────────────────────────────────────

class TestFormatBriefEvidenceTrace(unittest.TestCase):

    def test_no_traces_no_line(self):
        """No retrieval_traces and no chunk_ids → evidence line absent."""
        brief = _Brief(sections=[_Section()])
        text = _format_research_brief(brief, 5.0)
        self.assertNotIn("证据追踪", text)

    def test_traces_present(self):
        """retrieval_traces with retrieved_count → evidence line appears."""
        brief = _Brief(
            sections=[
                _Section(chunk_ids=["c1", "c2"]),
                _Section(title="新闻驱动", chunk_ids=["c3"]),
            ],
            retrieval_traces=[
                _RetrievalTrace(total_chunks=10, retrieved_count=2, selected_chunk_ids=["c1", "c2"], section_covered=True),
                _RetrievalTrace(total_chunks=10, retrieved_count=1, selected_chunk_ids=["c3"], section_covered=True),
            ],
        )
        text = _format_research_brief(brief, 3.0)
        self.assertIn("证据追踪", text)
        self.assertIn("从 10 个证据片段中筛选出 3 个", text)
        self.assertIn("覆盖 2/2 个章节", text)
        self.assertIn("识别出 0 组方向冲突", text)

    def test_dedup_chunk_ids_across_sections(self):
        """Same chunk_id in two sections is counted once."""
        brief = _Brief(
            sections=[
                _Section(chunk_ids=["c1", "c2"]),
                _Section(title="新闻驱动", chunk_ids=["c1"]),
            ],
            retrieval_traces=[_RetrievalTrace(total_chunks=8, selected_chunk_ids=["c1", "c2"], section_covered=True)],
        )
        text = _format_research_brief(brief, 2.0)
        self.assertIn("筛选出 2 个", text)

    def test_only_chunk_ids_no_traces(self):
        """chunk_ids present but no retrieval_traces → line still appears."""
        brief = _Brief(
            sections=[_Section(chunk_ids=["c1"])],
        )
        text = _format_research_brief(brief, 1.0)
        self.assertIn("证据追踪", text)
        self.assertIn("从 0 个证据片段中筛选出 1 个", text)

    def test_legacy_trace_without_new_fields_formats(self):
        """Old traces without structured selected ids still format correctly."""
        brief = _Brief(
            sections=[_Section(chunk_ids=["c1", "c2"])],
            retrieval_traces=[_RetrievalTrace(retrieved_count=2, total_chunks=9)],
        )
        text = _format_research_brief(brief, 1.0)
        self.assertIn("从 9 个证据片段中筛选出 2 个", text)
        self.assertIn("覆盖 1/1 个章节", text)

    def test_evidence_line_before_cost(self):
        """Evidence line appears before the cost footer."""
        brief = _Brief(
            sections=[_Section(chunk_ids=["c1"])],
            retrieval_traces=[_RetrievalTrace(total_chunks=3, selected_chunk_ids=["c1"], section_covered=True)],
        )
        text = _format_research_brief(brief, 1.0)
        ev_pos = text.index("证据追踪")
        cost_pos = text.index("本次研究成本")
        self.assertLess(ev_pos, cost_pos)

    def test_existing_format_unchanged(self):
        """Without evidence data, output matches original format expectations."""
        brief = _Brief(
            sections=[_Section()],
            sources_summary="- fx_agent 数据来源",
        )
        text = _format_research_brief(brief, 4.5)
        self.assertIn("📊 CNY/AUD 研究简报", text)
        self.assertIn("🔍 结论摘要", text)
        self.assertIn("📈 汇率事实", text)
        self.assertIn("📎 数据来源", text)
        self.assertIn("💰 本次研究成本", text)
        self.assertIn("⏱ 总耗时：4.5s", text)
        self.assertIn("🔖 简报 ID：abcdef12", text)
        self.assertIn("免责声明", text)
        self.assertNotIn("证据追踪", text)


    def test_chunk_ids_replaced_with_labels(self):
        """Raw chunk_ids in section content are replaced with [证据 N]."""
        cid = "chunk-383bd702-9cb9-41b6-853f-420d703fd424"
        brief = _Brief(
            sections=[_Section(
                content=f"银行报价较中间价加价 0.0222 [{cid}]。",
                chunk_ids=[cid],
            )],
        )
        text = _format_research_brief(brief, 1.0)
        self.assertNotIn(cid, text)
        self.assertIn("[证据 1]", text)

    def test_multiple_chunk_ids_numbered(self):
        """Multiple chunk_ids get sequential numbers across sections."""
        cid1 = "chunk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        cid2 = "chunk-11111111-2222-3333-4444-555555555555"
        brief = _Brief(
            sections=[
                _Section(content=f"事实 [{cid1}]", chunk_ids=[cid1]),
                _Section(title="新闻驱动", content=f"新闻 [{cid2}]", chunk_ids=[cid2]),
            ],
        )
        text = _format_research_brief(brief, 1.0)
        self.assertIn("[证据 1]", text)
        self.assertIn("[证据 2]", text)
        self.assertNotIn("chunk-", text)

    def test_no_chunk_ids_no_replacement(self):
        """Content without chunk_ids is unchanged."""
        brief = _Brief(sections=[_Section(content="普通内容")])
        text = _format_research_brief(brief, 1.0)
        self.assertIn("普通内容", text)
        self.assertNotIn("证据", text.split("证据追踪")[0] if "证据追踪" in text else text)

    def test_evidence_summary_hides_raw_ids(self):
        """Evidence summary is compact and does not expose raw chunk IDs."""
        brief = _Brief(
            sections=[_Section(chunk_ids=["c1"])],
            retrieval_traces=[
                _RetrievalTrace(
                    total_chunks=3,
                    selected_chunk_ids=["c1"],
                    section_covered=True,
                    conflict_count=2,
                )
            ],
        )
        text = _format_research_brief(brief, 1.0)
        evidence_line = next(line for line in text.splitlines() if "证据追踪" in line)
        self.assertIn("识别出 2 组方向冲突", evidence_line)
        self.assertNotIn("c1", evidence_line)


class TestFeedbackKeyboardTopic(unittest.TestCase):

    def test_no_topic(self):
        kb = _make_feedback_keyboard("news")
        data = kb.inline_keyboard[0][0].callback_data
        self.assertEqual(data, "fb:useful:news")

    def test_with_topic(self):
        kb = _make_feedback_keyboard("news", "RBA")
        data = kb.inline_keyboard[0][0].callback_data
        self.assertEqual(data, "fb:useful:news:RBA")

    def test_topic_truncated(self):
        kb = _make_feedback_keyboard("news", "a" * 50)
        data = kb.inline_keyboard[0][0].callback_data
        self.assertIn("news:" + "a" * 18, data)
        self.assertTrue(len(data) <= 64)

    def test_all_three_buttons(self):
        kb = _make_feedback_keyboard("research", "fx_cnyaud")
        buttons = kb.inline_keyboard[0]
        self.assertEqual(len(buttons), 3)
        self.assertIn("useful:research:fx_cnyaud", buttons[0].callback_data)
        self.assertIn("not_useful:research:fx_cnyaud", buttons[1].callback_data)
        self.assertIn("not_interested:research:fx_cnyaud", buttons[2].callback_data)

    def test_research_feedback_metadata_is_short_and_parseable(self):
        kb = _make_feedback_keyboard(
            "research",
            "fx_cnyaud",
            brief_id="df0083d5-abcdef",
            category="fx_cnyaud",
        )
        buttons = kb.inline_keyboard[0]
        for button in buttons:
            data = button.callback_data
            self.assertLessEqual(len(data.encode("utf-8")), 64)
            self.assertIn(":b=df0083d5-abc", data)
            self.assertIn(":c=fx_cnyaud", data)
            parsed = _parse_feedback_callback_data(data)
            self.assertEqual(parsed["source"], "research")
            self.assertEqual(parsed["topic"], "fx_cnyaud")
            self.assertEqual(parsed["brief_id"], "df0083d5-abc")
            self.assertEqual(parsed["category"], "fx_cnyaud")

    def test_legacy_feedback_callback_parse(self):
        parsed = _parse_feedback_callback_data("fb:not_useful:news:RBA")
        self.assertEqual(parsed["event_type"], "not_useful")
        self.assertEqual(parsed["source"], "news")
        self.assertEqual(parsed["topic"], "RBA")
        self.assertEqual(parsed["category"], "RBA")

    def test_preference_declaration_keyboard_is_short_and_parseable(self):
        kb = _make_preference_declaration_keyboard(12345)
        buttons = kb.inline_keyboard[0]
        confirm = buttons[0].callback_data
        reject = buttons[1].callback_data

        self.assertEqual(confirm, "pd:confirm:12345")
        self.assertEqual(reject, "pd:reject:12345")
        self.assertLessEqual(len(confirm.encode("utf-8")), 64)
        self.assertEqual(
            _parse_preference_declaration_callback_data(confirm),
            {"action": "confirm", "declaration_id": "12345"},
        )

    def test_preference_declarations_format(self):
        text = _format_preference_declarations(
            [
                {
                    "declaration": "用户可能关注 RBA 利率对换汇时点的影响。",
                    "evidence_count": 10,
                    "metadata": {"confidence_hint": "medium"},
                }
            ],
            title="待确认的隐式偏好：",
            empty="- 暂无",
        )

        self.assertIn("待确认的隐式偏好", text)
        self.assertIn("用户可能关注 RBA", text)
        self.assertIn("证据 10", text)
        self.assertIn("置信 medium", text)


class TestExtractNewsTopic(unittest.TestCase):

    def test_rba_keyword(self):
        articles = [{"keyword": "RBA interest rate decision"}]
        self.assertEqual(_extract_news_topic(articles), "RBA")

    def test_mideast(self):
        articles = [
            {"keyword": "Iran Hormuz strait"},
            {"keyword": "Iran nuclear deal"},
        ]
        self.assertEqual(_extract_news_topic(articles), "中东局势")

    def test_mixed_topics_dominant_wins(self):
        articles = [
            {"keyword": "China economy GDP"},
            {"keyword": "China yuan policy"},
            {"keyword": "RBA interest rate decision"},
        ]
        topic = _extract_news_topic(articles)
        # China×2 vs RBA×1 → China wins
        self.assertEqual(topic, "China")

    def test_empty_articles(self):
        self.assertEqual(_extract_news_topic([]), "market_news")

    def test_no_keyword_field(self):
        articles = [{"title": "Some news"}]
        self.assertEqual(_extract_news_topic(articles), "market_news")

    def test_aud_keyword(self):
        articles = [{"keyword": "Australia dollar AUD"}]
        topic = _extract_news_topic(articles)
        self.assertIn(topic, ("AUD", "Australia"))


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Telegram helpers tests (evidence trace + feedback topic)")
    print("=" * 60)
    unittest.main(verbosity=2)
