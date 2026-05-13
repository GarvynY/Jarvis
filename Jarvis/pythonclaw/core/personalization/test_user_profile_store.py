"""Tests for feedback-to-evidence linkage fields.

Run: python test_user_profile_store.py [-v]
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_pc = type(sys)("pythonclaw")
_core = type(sys)("pythonclaw.core")
_personalization = type(sys)("pythonclaw.core.personalization")
_cfg = type(sys)("pythonclaw.config")
_cfg.PYTHONCLAW_HOME = Path(tempfile.gettempdir()) / "pythonclaw_profile_test"
_pc.config = _cfg  # type: ignore[attr-defined]
_pc.core = _core  # type: ignore[attr-defined]
_core.personalization = _personalization  # type: ignore[attr-defined]
sys.modules.setdefault("pythonclaw", _pc)
sys.modules.setdefault("pythonclaw.core", _core)
sys.modules.setdefault("pythonclaw.core.personalization", _personalization)
sys.modules.setdefault("pythonclaw.config", _cfg)

_store_path = Path(__file__).resolve().parent / "user_profile_store.py"
_spec = importlib.util.spec_from_file_location(
    "pythonclaw.core.personalization.user_profile_store",
    _store_path,
)
_store = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_store.__package__ = "pythonclaw.core.personalization"  # type: ignore[attr-defined]
sys.modules["pythonclaw.core.personalization.user_profile_store"] = _store
_spec.loader.exec_module(_store)  # type: ignore[union-attr]

get_user_category_feedback_summary = _store.get_user_category_feedback_summary
get_due_news_feedback_contexts = _store.get_due_news_feedback_contexts
get_news_feedback_context = _store.get_news_feedback_context
get_news_feedback_rollup_status = _store.get_news_feedback_rollup_status
init_db = _store.init_db
create_preference_declaration = _store.create_preference_declaration
list_preference_declarations = _store.list_preference_declarations
log_feedback_event = _store.log_feedback_event
mark_news_feedback_contexts_summarized = _store.mark_news_feedback_contexts_summarized
purge_expired_news_feedback_context = _store.purge_expired_news_feedback_context
store_news_feedback_context = _store.store_news_feedback_context
update_preference_declaration_status = _store.update_preference_declaration_status
update_inferred_preferences_from_feedback = _store.update_inferred_preferences_from_feedback


class TestFeedbackEventsPhase10D(unittest.TestCase):

    def _db_path(self, tmp: str) -> Path:
        return Path(tmp) / "profiles.sqlite3"

    def test_legacy_feedback_rows_migrate_without_breaking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_user_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE feedback_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        topic TEXT,
                        message_id TEXT,
                        metadata_json TEXT,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO users (id, telegram_user_id, created_at, updated_at)
                    VALUES (1, '123', '2026-05-10T00:00:00+00:00', '2026-05-10T00:00:00+00:00');
                    INSERT INTO feedback_events (
                        user_id, event_type, topic, message_id, metadata_json, created_at
                    )
                    VALUES (1, 'useful', 'RBA', '42', '{}', '2026-05-10T00:00:00+00:00');
                    """
                )

            init_db(db_path)
            with sqlite3.connect(db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(feedback_events)")}
            self.assertIn("task_id", columns)
            self.assertIn("brief_id", columns)
            self.assertIn("section_title", columns)
            self.assertIn("category", columns)
            with sqlite3.connect(db_path) as conn:
                news_columns = {row[1] for row in conn.execute("PRAGMA table_info(news_feedback_context)")}
                declaration_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(preference_declarations)")
                }
            self.assertIn("articles_json", news_columns)
            self.assertIn("declaration", declaration_columns)
            self.assertIn("status", declaration_columns)

            update_inferred_preferences_from_feedback("123", db_path=db_path)
            log_feedback_event("123", "useful", topic="fx_cnyaud", category="macro", db_path=db_path)
            summary = get_user_category_feedback_summary("123", db_path=db_path)
            self.assertEqual(summary["macro"], 1.0)

    def test_feedback_stores_task_brief_section_and_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            event_id = log_feedback_event(
                "456",
                "useful",
                topic="fx_cnyaud",
                task_id="task-abc123",
                brief_id="brief-7890",
                section_title="宏观信号",
                category="macro",
                message_id="99",
                metadata={"source": "unit_test"},
                db_path=db_path,
            )
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT event_type, topic, task_id, brief_id, section_title,
                           category, message_id, metadata_json
                    FROM feedback_events
                    WHERE id = ?
                    """,
                    (event_id,),
                ).fetchone()

            self.assertEqual(row["event_type"], "useful")
            self.assertEqual(row["topic"], "fx_cnyaud")
            self.assertEqual(row["task_id"], "task-abc123")
            self.assertEqual(row["brief_id"], "brief-7890")
            self.assertEqual(row["section_title"], "宏观信号")
            self.assertEqual(row["category"], "macro")
            self.assertEqual(row["message_id"], "99")
            self.assertIn("unit_test", row["metadata_json"])

    def test_feedback_category_is_normalized_to_lowercase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            log_feedback_event("456", "useful", topic="fx_cnyaud", category="MACRO", db_path=db_path)

            summary = get_user_category_feedback_summary("456", db_path=db_path)

            self.assertEqual(summary["macro"], 1.0)
            self.assertNotIn("MACRO", summary)

    def test_user_category_feedback_summary_is_computed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            log_feedback_event("789", "useful", topic="fx_cnyaud", category="macro", db_path=db_path)
            log_feedback_event("789", "useful", topic="fx_cnyaud", category="macro", db_path=db_path)
            log_feedback_event("789", "not_useful", topic="fx_cnyaud", category="macro", db_path=db_path)
            log_feedback_event("789", "not_interested", topic="market_news", category="news", db_path=db_path)
            log_feedback_event("789", "not_interested", topic="market_news", category="alerts", db_path=db_path)
            log_feedback_event("789", "not_interested", topic="market_news", category="alerts", db_path=db_path)

            summary = get_user_category_feedback_summary("789", db_path=db_path)

            self.assertEqual(summary["macro"], 0.3333)
            self.assertEqual(summary["news"], -1.0)
            self.assertEqual(summary["alerts"], -1.0)

    def test_no_raw_evidence_content_is_stored_in_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            raw_evidence = "RAW_EVIDENCE_CONTENT_SHOULD_NOT_BE_STORED"
            log_feedback_event(
                "999",
                "not_useful",
                topic="fx_cnyaud",
                brief_id="abc12345",
                category="macro",
                metadata={"source": "inline_button:research"},
                db_path=db_path,
            )
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("SELECT * FROM feedback_events").fetchall()
                column_names = [d[0] for d in conn.execute("SELECT * FROM feedback_events").description]

            stored = "\n".join(
                "|".join(str(value) for value in row)
                for row in rows
            )
            self.assertNotIn(raw_evidence, stored)
            self.assertNotIn("content", ",".join(column_names).lower())

    def test_news_feedback_context_stores_tags_with_three_day_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            long_summary = "RBA 利率信号可能支撑澳元。" * 20
            feedback_id = store_news_feedback_context(
                "111",
                article_title="RBA signals higher rates",
                article_summary=long_summary,
                article_url="https://example.com/rba",
                tags=["RBA利率", "澳元走强"],
                articles=[
                    {
                        "title": "RBA signals higher rates",
                        "summary": long_summary,
                        "url": "https://example.com/rba",
                        "published": "2026-05-12T00:00:00Z",
                        "tags": ["RBA利率", "澳元走强"],
                    }
                ],
                db_path=db_path,
            )

            row = get_news_feedback_context("111", feedback_id, db_path=db_path)

            self.assertIsNotNone(row)
            self.assertEqual(row["tags"], ["RBA利率", "澳元走强"])
            self.assertEqual(row["articles"][0]["title"], "RBA signals higher rates")
            self.assertEqual(row["article_summary"], long_summary)
            self.assertEqual(row["articles"][0]["summary"], long_summary)
            self.assertEqual(row["articles"][0]["tags"], ["RBA利率", "澳元走强"])
            self.assertIn("T", row["expires_at"])
            self.assertNotEqual(row["created_at"][:10], row["expires_at"][:10])

    def test_news_tag_feedback_can_use_dynamic_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            log_feedback_event(
                "222",
                "useful",
                topic="RBA利率",
                category="news_tag",
                metadata={"news_feedback_id": "1"},
                db_path=db_path,
            )

            summary = get_user_category_feedback_summary("222", db_path=db_path)

            self.assertEqual(summary["news_tag"], 1.0)

    def test_news_feedback_rollup_status_reaches_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            feedback_id = store_news_feedback_context(
                "333",
                article_title="RBA news",
                article_summary="RBA summary",
                tags=["RBA利率"],
                db_path=db_path,
            )
            for _ in range(_store.NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT):
                log_feedback_event(
                    "333",
                    "useful",
                    topic="RBA利率",
                    category="news_tag",
                    metadata={"news_feedback_id": str(feedback_id)},
                    db_path=db_path,
                )

            status = get_news_feedback_rollup_status("333", db_path=db_path)

            self.assertEqual(status["feedback_count"], _store.NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT)
            self.assertTrue(status["threshold_reached"])

    def test_expired_news_feedback_context_is_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            feedback_id = store_news_feedback_context(
                "444",
                article_title="old news",
                article_summary="old summary",
                tags=["能源风险"],
                ttl_days=1,
                db_path=db_path,
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE news_feedback_context SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (feedback_id,),
                )

            self.assertIsNone(get_news_feedback_context("444", feedback_id, db_path=db_path))
            self.assertEqual(purge_expired_news_feedback_context(db_path=db_path), 0)

    def test_due_news_feedback_contexts_include_expired_feedback_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            feedback_id = store_news_feedback_context(
                "555",
                article_title="RBA news",
                article_summary="RBA summary",
                tags=["RBA利率"],
                db_path=db_path,
            )
            log_feedback_event(
                "555",
                "useful",
                topic="RBA利率",
                category="news_tag",
                metadata={"news_feedback_id": str(feedback_id)},
                db_path=db_path,
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE news_feedback_context SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (feedback_id,),
                )

            due = get_due_news_feedback_contexts("555", db_path=db_path)

            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["trigger_type"], "expired")
            self.assertEqual(due[0]["feedback_events"][0]["topic"], "RBA利率")
            self.assertEqual(mark_news_feedback_contexts_summarized([feedback_id], db_path=db_path), 1)
            self.assertEqual(purge_expired_news_feedback_context(db_path=db_path), 1)

    def test_due_news_feedback_contexts_include_threshold_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            feedback_id = store_news_feedback_context(
                "666",
                article_title="RBA news",
                article_summary="RBA summary",
                tags=["RBA利率"],
                db_path=db_path,
            )
            for _ in range(_store.NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT):
                log_feedback_event(
                    "666",
                    "useful",
                    topic="RBA利率",
                    category="news_tag",
                    metadata={"news_feedback_id": str(feedback_id)},
                    db_path=db_path,
                )

            due = get_due_news_feedback_contexts("666", db_path=db_path)

            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["trigger_type"], "threshold")

    def test_summarized_context_feedback_does_not_retrigger_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            old_feedback_id = store_news_feedback_context(
                "667",
                article_title="RBA old news",
                article_summary="RBA old summary",
                tags=["RBA利率"],
                db_path=db_path,
            )
            for _ in range(_store.NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT):
                log_feedback_event(
                    "667",
                    "useful",
                    topic="RBA利率",
                    category="news_tag",
                    metadata={"news_feedback_id": str(old_feedback_id)},
                    db_path=db_path,
                )
            self.assertEqual(mark_news_feedback_contexts_summarized([old_feedback_id], db_path=db_path), 1)

            new_feedback_id = store_news_feedback_context(
                "667",
                article_title="RBA new news",
                article_summary="RBA new summary",
                tags=["RBA利率"],
                db_path=db_path,
            )
            log_feedback_event(
                "667",
                "useful",
                topic="RBA利率",
                category="news_tag",
                metadata={"news_feedback_id": str(new_feedback_id)},
                db_path=db_path,
            )

            status = get_news_feedback_rollup_status("667", db_path=db_path)
            due = get_due_news_feedback_contexts("667", db_path=db_path)

            self.assertEqual(status["feedback_count"], 1)
            self.assertFalse(status["threshold_reached"])
            self.assertEqual(due, [])

    def test_preference_declaration_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db_path(tmp)
            declaration_id = create_preference_declaration(
                "777",
                "用户可能更关注 RBA 利率变化对 AUD/CNY 的实际换汇影响。",
                evidence_count=6,
                source="news_feedback",
                metadata={"context_ids": ["1", "2"]},
                db_path=db_path,
            )

            pending = list_preference_declarations("777", status="pending", db_path=db_path)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["id"], declaration_id)
            self.assertEqual(pending[0]["evidence_count"], 6)
            self.assertEqual(pending[0]["metadata"], {"context_ids": ["1", "2"]})

            self.assertTrue(
                update_preference_declaration_status(
                    "777", declaration_id, "confirmed", db_path=db_path
                )
            )
            confirmed = list_preference_declarations("777", status="confirmed", db_path=db_path)
            self.assertEqual(len(confirmed), 1)
            self.assertIsNotNone(confirmed[0]["confirmed_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
