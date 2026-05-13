"""Tests for PreferenceAgent MVP.

Run: python test_preference_agent.py [-v]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path

_pc = type(sys)("pythonclaw")
_core = type(sys)("pythonclaw.core")
_personalization = type(sys)("pythonclaw.core.personalization")
_cfg = type(sys)("pythonclaw.config")
_rate_limit = type(sys)("pythonclaw.core.rate_limit")
_cfg.PYTHONCLAW_HOME = Path(tempfile.gettempdir()) / "pythonclaw_preference_agent_test"
_cfg.get_str = lambda *args, **kwargs: kwargs.get("default", "")
_rate_limit.call_with_backoff = lambda _provider, func, *args, **kwargs: func(*args, **kwargs)
_pc.config = _cfg  # type: ignore[attr-defined]
_pc.core = _core  # type: ignore[attr-defined]
_core.personalization = _personalization  # type: ignore[attr-defined]
_core.rate_limit = _rate_limit  # type: ignore[attr-defined]
sys.modules.setdefault("pythonclaw", _pc)
sys.modules.setdefault("pythonclaw.core", _core)
sys.modules.setdefault("pythonclaw.core.personalization", _personalization)
sys.modules.setdefault("pythonclaw.config", _cfg)
sys.modules.setdefault("pythonclaw.core.rate_limit", _rate_limit)

_store_path = Path(__file__).resolve().parent / "user_profile_store.py"
_store_spec = importlib.util.spec_from_file_location(
    "pythonclaw.core.personalization.user_profile_store",
    _store_path,
)
_store = importlib.util.module_from_spec(_store_spec)  # type: ignore[arg-type]
_store.__package__ = "pythonclaw.core.personalization"  # type: ignore[attr-defined]
sys.modules["pythonclaw.core.personalization.user_profile_store"] = _store
_store_spec.loader.exec_module(_store)  # type: ignore[union-attr]

_agent_path = Path(__file__).resolve().parent / "preference_agent.py"
_agent_spec = importlib.util.spec_from_file_location(
    "pythonclaw.core.personalization.preference_agent",
    _agent_path,
)
_agent = importlib.util.module_from_spec(_agent_spec)  # type: ignore[arg-type]
_agent.__package__ = "pythonclaw.core.personalization"  # type: ignore[attr-defined]
sys.modules["pythonclaw.core.personalization.preference_agent"] = _agent
_agent_spec.loader.exec_module(_agent)  # type: ignore[union-attr]


class TestPreferenceAgentMVP(unittest.TestCase):
    def _db_path(self, tmp: str) -> Path:
        return Path(tmp) / "profiles.sqlite3"

    def _seed_due_context(self, db_path: Path, user_id: str = "123") -> int:
        context_id = _store.store_news_feedback_context(
            user_id,
            article_title="RBA rates | Energy risk",
            article_summary="- RBA rates: RBA 利率信号可能支撑澳元。\n- Energy risk: 能源风险影响商品货币。",
            article_url="https://example.com/rba",
            tags=["RBA利率", "澳元", "能源风险"],
            articles=[
                {
                    "title": "RBA rates",
                    "summary": "RBA 利率信号可能支撑澳元。",
                    "url": "https://example.com/rba",
                    "published": "2026-05-12T00:00:00Z",
                    "tags": ["RBA利率", "澳元"],
                }
            ],
            db_path=db_path,
        )
        for _ in range(_store.NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT):
            _store.log_feedback_event(
                user_id,
                "useful",
                topic="RBA利率",
                category="news_tag",
                metadata={"news_feedback_id": str(context_id)},
                db_path=db_path,
            )
        return context_id

    def test_preference_agent_creates_pending_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            context_id = self._seed_due_context(db_path)

            def fake_llm(_prompt: str, _system: str, _max_tokens: int):
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户可能更关注 RBA 利率变化对 AUD/CNY 实际换汇影响。",
                            "confidence_hint": "medium",
                            "evidence_count": 10,
                            "source_context_ids": [str(context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {"prompt_tokens": 10, "completion_tokens": 5}

            result = _agent.run_preference_agent_for_user(
                "123",
                db_path=str(db_path),
                llm_call=fake_llm,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.declarations_created, 1)
            declarations = _store.list_preference_declarations("123", status="pending", db_path=db_path)
            self.assertEqual(len(declarations), 1)
            self.assertEqual(declarations[0]["metadata"]["confidence_hint"], "medium")
            self.assertEqual(declarations[0]["metadata"]["source_context_ids"], [str(context_id)])
            due_after = _store.get_due_news_feedback_contexts("123", db_path=db_path)
            self.assertEqual(due_after, [])

    def test_preference_agent_repairs_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            context_id = self._seed_due_context(db_path)
            calls = []

            def fake_llm(_prompt: str, _system: str, _max_tokens: int):
                calls.append(_prompt)
                if len(calls) == 1:
                    return "not json", {}
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户可能偏好围绕 RBA 利率变化的换汇影响分析。",
                            "confidence_hint": "low",
                            "evidence_count": 10,
                            "source_context_ids": [str(context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {}

            result = _agent.run_preference_agent_for_user(
                "123",
                db_path=str(db_path),
                llm_call=fake_llm,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.attempts, 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(result.declarations_created, 0)
            self.assertEqual(_store.get_due_news_feedback_contexts("123", db_path=db_path), [])

    def test_preference_agent_filters_low_evidence_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            context_id = self._seed_due_context(db_path)

            def fake_llm(_prompt: str, _system: str, _max_tokens: int):
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户可能短期关注 RBA。",
                            "confidence_hint": "medium",
                            "evidence_count": 1,
                            "source_context_ids": [str(context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {}

            result = _agent.run_preference_agent_for_user(
                "123",
                db_path=str(db_path),
                llm_call=fake_llm,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.declarations_created, 0)
            self.assertEqual(_store.list_preference_declarations("123", status="pending", db_path=db_path), [])
            self.assertEqual(_store.get_due_news_feedback_contexts("123", db_path=db_path), [])

    def test_preference_agent_skips_duplicate_declaration_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            first_context_id = self._seed_due_context(db_path)

            def first_llm(_prompt: str, _system: str, _max_tokens: int):
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户更偏好有因果链和数据支撑的深度分析。",
                            "confidence_hint": "high",
                            "evidence_count": 20,
                            "source_context_ids": [str(first_context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {}

            first = _agent.run_preference_agent_for_user(
                "123",
                db_path=str(db_path),
                llm_call=first_llm,
            )
            self.assertEqual(first.declarations_created, 1)

            second_context_id = self._seed_due_context(db_path)

            def second_llm(_prompt: str, _system: str, _max_tokens: int):
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户不喜欢逻辑太浅、泛泛而谈的新闻分析。",
                            "confidence_hint": "high",
                            "evidence_count": 20,
                            "source_context_ids": [str(second_context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {}

            second = _agent.run_preference_agent_for_user(
                "123",
                db_path=str(db_path),
                llm_call=second_llm,
            )

            self.assertTrue(second.ok)
            self.assertEqual(second.declarations_created, 0)
            declarations = _store.list_preference_declarations("123", status="pending", db_path=db_path)
            self.assertEqual(len(declarations), 1)
            self.assertEqual(declarations[0]["metadata"]["preference_key"], "analysis_depth")

    def test_prompt_caps_declarations_at_three_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            self._seed_due_context(db_path)
            prompts = []

            def fake_llm(prompt: str, _system: str, _max_tokens: int):
                prompts.append(prompt)
                return json.dumps({"declarations": [], "rejected_patterns": []}, ensure_ascii=False), {}

            _agent.run_preference_agent_for_user("123", db_path=str(db_path), llm_call=fake_llm)

            self.assertIn("最多3小点", prompts[0])
            self.assertIn("最多输出 3 条 declarations", prompts[0])

    def test_preference_agent_no_due_context_no_llm_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            called = False

            def fake_llm(_prompt: str, _system: str, _max_tokens: int):
                nonlocal called
                called = True
                return "{}", {}

            result = _agent.run_preference_agent_for_user(
                "empty",
                db_path=str(db_path),
                llm_call=fake_llm,
            )

            self.assertTrue(result.ok)
            self.assertFalse(called)
            self.assertEqual(result.declarations_created, 0)

    def test_invalid_confidence_hint_fails_without_marking_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            context_id = self._seed_due_context(db_path)

            def fake_llm(_prompt: str, _system: str, _max_tokens: int):
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户关注 RBA。",
                            "confidence_hint": "certain",
                            "evidence_count": 10,
                            "source_context_ids": [str(context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {}

            result = _agent.run_preference_agent_for_user(
                "123",
                db_path=str(db_path),
                llm_call=fake_llm,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.declarations_created, 0)
            self.assertTrue(_store.get_due_news_feedback_contexts("123", db_path=db_path))

    def test_prompt_does_not_include_raw_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            context_id = self._seed_due_context(db_path)
            prompts = []

            def fake_llm(prompt: str, _system: str, _max_tokens: int):
                prompts.append(prompt)
                return json.dumps({
                    "declarations": [
                        {
                            "declaration": "用户可能关注 RBA 利率对换汇时点的影响。",
                            "confidence_hint": "medium",
                            "evidence_count": 10,
                            "source_context_ids": [str(context_id)],
                        }
                    ],
                    "rejected_patterns": [],
                }, ensure_ascii=False), {}

            _agent.run_preference_agent_for_user("123", db_path=str(db_path), llm_call=fake_llm)

            prompt = prompts[0]
            self.assertIn("contexts", prompt)
            self.assertNotIn("explicit_preferences", prompt)
            self.assertNotIn("inferred_preferences", prompt)
            self.assertNotIn("raw_events", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
