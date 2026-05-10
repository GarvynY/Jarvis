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
init_db = _store.init_db
log_feedback_event = _store.log_feedback_event
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
