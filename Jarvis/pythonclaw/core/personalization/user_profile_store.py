"""SQLite-backed structured user profiles for Phase 8 personalization.

This module is deliberately separate from the legacy Markdown memory system.
It stores only useful decision preferences and lightweight feedback signals.
Do not store bank accounts, balances, passports, exact addresses, government
IDs, or other sensitive financial identity data here.

Raw events are short-term operational signals only. They are never returned by
``get_user_profile()`` and must not be passed into LLM prompts.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ... import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

EXPLICIT_PREFERENCE_KEYS = {
    "language",
    "target_rate",
    "alert_threshold",
    "purpose",
    "preferred_summary_style",
    "preferred_topics",
    "privacy_level",
    "preferred_reminder_time",
    "actionability_threshold",
    "alert_preference",
}

INFERRED_PREFERENCE_KEYS = {
    "confidence",
    "high_interest_topics",
    "low_interest_topics",
}

FEEDBACK_EVENT_TYPES = {"useful", "not_useful", "useless", "not_interested"}
SENSITIVE_KEY_MARKERS = {
    "account",
    "account_number",
    "address",
    "balance",
    "bank",
    "bsb",
    "card",
    "driver_license",
    "exact_address",
    "government_id",
    "iban",
    "id_number",
    "identity",
    "license",
    "passport",
    "passport_number",
    "routing",
    "ssn",
    "swift",
    "tax",
}
MAX_RAW_EVENT_PAYLOAD_BYTES = 16 * 1024
MAX_RAW_EVENT_TTL_DAYS = 14
MAX_SHORT_TEXT_BYTES = 256
MAX_PURPOSE_BYTES = 512
MAX_TOPIC_BYTES = 96
MAX_EVENT_TEXT_BYTES = 256
MAX_METADATA_STRING_BYTES = 1024
MAX_TOPICS_PER_FIELD = 20

PREFERRED_SUMMARY_STYLES = {"brief", "standard", "detailed", "action_first"}
PRIVACY_LEVELS = {"minimal", "standard", "strict"}
ALERT_PREFERENCES = {"target_rate", "volatility", "major_news", "morning_report"}

SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"\bapi[_ -]?key\b", re.IGNORECASE),
    re.compile(r"\baccess[_ -]?token\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"\bsecret\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35,}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    re.compile(r"\bpassport(?:\s+number)?\b", re.IGNORECASE),
    re.compile(r"\bbank(?:\s+account)?\b", re.IGNORECASE),
    re.compile(r"\baccount\s+number\b", re.IGNORECASE),
    re.compile(r"银行账号|银行账户|护照|身份证|余额|银行卡"),
]


def _default_db_path() -> Path:
    return config.PYTHONCLAW_HOME / "context" / "personalization" / "user_profiles.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _reject_sensitive_string(
    value: str,
    *,
    path: str,
    max_bytes: int = MAX_METADATA_STRING_BYTES,
) -> None:
    if _byte_len(value) > max_bytes:
        raise ValueError(f"Personalization value is too long: {path}")
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"Sensitive value is not allowed: {path}")


def _reject_sensitive_payload(
    value: Any,
    *,
    path: str = "payload",
    max_string_bytes: int = MAX_METADATA_STRING_BYTES,
) -> None:
    """Reject nested data that appears to contain sensitive identity fields."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).strip()
            if _is_sensitive_key(key_text):
                raise ValueError(f"Sensitive field is not allowed: {path}.{key_text}")
            _reject_sensitive_payload(
                child,
                path=f"{path}.{key_text}",
                max_string_bytes=max_string_bytes,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_payload(
                child,
                path=f"{path}[{index}]",
                max_string_bytes=max_string_bytes,
            )
    elif isinstance(value, str):
        _reject_sensitive_string(value, path=path, max_bytes=max_string_bytes)


def _validate_topic_list(value: Any, *, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    if len(value) > MAX_TOPICS_PER_FIELD:
        raise ValueError(f"Too many topics in {path}")

    topics: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{path}[{index}] must be a string")
        item = item.strip()
        _reject_sensitive_string(item, path=f"{path}[{index}]", max_bytes=MAX_TOPIC_BYTES)
        if item:
            topics.append(item)
    return topics


def _validate_preference_value(key: str, value: Any) -> Any:
    if key in {"target_rate", "alert_threshold", "actionability_threshold"}:
        if value is None:
            return None
        return float(value)
    if key == "preferred_topics":
        return _validate_topic_list(value, path=key)
    if key == "preferred_summary_style":
        if value is None:
            return None
        value = str(value).strip()
        if value not in PREFERRED_SUMMARY_STYLES:
            raise ValueError(f"Unsupported preferred_summary_style: {value}")
        return value
    if key == "privacy_level":
        if value is None:
            return None
        value = str(value).strip()
        if value not in PRIVACY_LEVELS:
            raise ValueError(f"Unsupported privacy_level: {value}")
        return value
    if key == "alert_preference":
        if value is None:
            return None
        value = str(value).strip()
        if value not in ALERT_PREFERENCES:
            raise ValueError(f"Unsupported alert_preference: {value}")
        return value
    if key == "purpose":
        if value is None:
            return None
        value = str(value).strip()
        _reject_sensitive_string(value, path=key, max_bytes=MAX_PURPOSE_BYTES)
        return value
    if key in {"language", "preferred_reminder_time"}:
        if value is None:
            return None
        value = str(value).strip()
        _reject_sensitive_string(value, path=key, max_bytes=MAX_SHORT_TEXT_BYTES)
        return value
    if key in {"high_interest_topics", "low_interest_topics"}:
        return _validate_topic_list(value, path=key)
    if key == "confidence":
        if value is None:
            return None
        confidence = float(value)
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be between 0 and 1")
        return confidence
    _reject_sensitive_payload(value, path=key)
    return value


def _validate_event_text(value: str | None, *, path: str) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    _reject_sensitive_string(value, path=path, max_bytes=MAX_EVENT_TEXT_BYTES)
    return value


def _clean_updates(updates: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")

    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        key = str(key).strip()
        if key not in allowed:
            raise ValueError(f"Unsupported personalization field: {key}")
        if _is_sensitive_key(key):
            raise ValueError(f"Sensitive field is not allowed: {key}")
        cleaned[key] = _validate_preference_value(key, value)
    return cleaned


@contextmanager
def _connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path) if db_path is not None else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except sqlite3.Error:
        logger.exception("SQLite error in structured personalization store")
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply small in-place migrations for the structured profile store."""
    user_columns = _column_names(conn, "users")
    if user_columns and "onboarding_completed" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN onboarding_completed INTEGER NOT NULL DEFAULT 0"
        )
    if user_columns and "onboarding_completed_at" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN onboarding_completed_at TEXT")

    explicit_columns = _column_names(conn, "explicit_preferences")
    if explicit_columns and "alert_preference" not in explicit_columns:
        conn.execute("ALTER TABLE explicit_preferences ADD COLUMN alert_preference TEXT")

    inferred_columns = _column_names(conn, "inferred_preferences")
    if inferred_columns and "confidence" not in inferred_columns:
        conn.execute("ALTER TABLE inferred_preferences ADD COLUMN confidence REAL")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _purge_expired_raw_events(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM raw_events WHERE expires_at <= ?",
        (_now(),),
    )
    return int(cur.rowcount)


def init_db(db_path: str | Path | None = None) -> Path:
    """Create the personalization SQLite schema if it does not exist."""
    path = Path(db_path) if db_path is not None else _default_db_path()
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                onboarding_completed INTEGER NOT NULL DEFAULT 0,
                onboarding_completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS explicit_preferences (
                user_id INTEGER PRIMARY KEY,
                language TEXT,
                target_rate REAL,
                alert_threshold REAL,
                purpose TEXT,
                preferred_summary_style TEXT,
                preferred_topics_json TEXT,
                privacy_level TEXT,
                preferred_reminder_time TEXT,
                actionability_threshold REAL,
                alert_preference TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS inferred_preferences (
                user_id INTEGER PRIMARY KEY,
                high_interest_topics_json TEXT,
                low_interest_topics_json TEXT,
                confidence REAL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS feedback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                topic TEXT,
                message_id TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_payload_json TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_user_created
                ON feedback_events(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_raw_events_expires
                ON raw_events(expires_at);
            """
        )
        _migrate_schema(conn)
    return path


def purge_expired_raw_events(db_path: str | Path | None = None) -> int:
    """Delete expired raw events so short-term data does not accumulate."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return _purge_expired_raw_events(conn)


def _compact_database_after_delete(db_path: str | Path | None = None) -> None:
    """Reduce WAL/database remnants after an explicit profile deletion."""
    path = Path(db_path) if db_path is not None else _default_db_path()
    if str(path) == ":memory:" or not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _user_row(conn: sqlite3.Connection, telegram_user_id: int | str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE telegram_user_id = ?",
        (str(telegram_user_id),),
    ).fetchone()


def _ensure_user(conn: sqlite3.Connection, telegram_user_id: int | str) -> sqlite3.Row:
    now = _now()
    conn.execute(
        """
        INSERT INTO users (telegram_user_id, created_at, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (str(telegram_user_id), now, now),
    )
    row = _user_row(conn, telegram_user_id)
    if row is None:
        raise RuntimeError("Failed to create user profile")
    return row


def get_or_create_user(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the user row, creating it for a Telegram user id if needed."""
    if telegram_user_id in (None, ""):
        raise ValueError("telegram_user_id is required")
    init_db(db_path)
    with _connect(db_path) as conn:
        row = _ensure_user(conn, telegram_user_id)
        return dict(row)


def _explicit_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "language": row["language"],
        "target_rate": row["target_rate"],
        "alert_threshold": row["alert_threshold"],
        "purpose": row["purpose"],
        "preferred_summary_style": row["preferred_summary_style"],
        "preferred_topics": _json_loads(row["preferred_topics_json"], []),
        "privacy_level": row["privacy_level"],
        "preferred_reminder_time": row["preferred_reminder_time"],
        "actionability_threshold": row["actionability_threshold"],
        "alert_preference": row["alert_preference"],
        "updated_at": row["updated_at"],
    }


def _inferred_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "high_interest_topics": _json_loads(row["high_interest_topics_json"], []),
        "low_interest_topics": _json_loads(row["low_interest_topics_json"], []),
        "confidence": row["confidence"],
        "updated_at": row["updated_at"],
    }


def get_user_profile(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a structured user profile without raw events.

    The result is safe for application logic. Only fields from explicit and
    inferred preference tables are returned; raw short-term events are excluded.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        _purge_expired_raw_events(conn)
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return {}

        explicit = conn.execute(
            "SELECT * FROM explicit_preferences WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        inferred = conn.execute(
            "SELECT * FROM inferred_preferences WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        feedback_counts = {
            row["event_type"]: row["count"]
            for row in conn.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM feedback_events
                WHERE user_id = ?
                GROUP BY event_type
                """,
                (user["id"],),
            )
        }

        return {
            "user": dict(user),
            "explicit_preferences": _explicit_row_to_dict(explicit),
            "inferred_preferences": _inferred_row_to_dict(inferred),
            "feedback_summary": feedback_counts,
        }


def update_explicit_preferences(
    telegram_user_id: int | str,
    updates: dict[str, Any],
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Update user-provided preference fields for a Telegram user."""
    cleaned = _clean_updates(updates, EXPLICIT_PREFERENCE_KEYS)
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        current = conn.execute(
            "SELECT * FROM explicit_preferences WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        data = _explicit_row_to_dict(current)
        data.update(cleaned)

        conn.execute(
            """
            INSERT INTO explicit_preferences (
                user_id, language, target_rate, alert_threshold, purpose,
                preferred_summary_style, preferred_topics_json, privacy_level,
                preferred_reminder_time, actionability_threshold, alert_preference,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                language = excluded.language,
                target_rate = excluded.target_rate,
                alert_threshold = excluded.alert_threshold,
                purpose = excluded.purpose,
                preferred_summary_style = excluded.preferred_summary_style,
                preferred_topics_json = excluded.preferred_topics_json,
                privacy_level = excluded.privacy_level,
                preferred_reminder_time = excluded.preferred_reminder_time,
                actionability_threshold = excluded.actionability_threshold,
                alert_preference = excluded.alert_preference,
                updated_at = excluded.updated_at
            """,
            (
                user["id"],
                data.get("language"),
                data.get("target_rate"),
                data.get("alert_threshold"),
                data.get("purpose"),
                data.get("preferred_summary_style"),
                _json_dumps(data.get("preferred_topics") or []),
                data.get("privacy_level"),
                data.get("preferred_reminder_time"),
                data.get("actionability_threshold"),
                data.get("alert_preference"),
                _now(),
            ),
        )
    return get_user_profile(telegram_user_id, db_path)


def mark_onboarding_completed(
    telegram_user_id: int | str,
    *,
    completed: bool = True,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Mark whether the lightweight Telegram onboarding flow is complete."""
    init_db(db_path)
    now = _now()
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        conn.execute(
            """
            UPDATE users
            SET onboarding_completed = ?,
                onboarding_completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (1 if completed else 0, now if completed else None, now, user["id"]),
        )
    return get_user_profile(telegram_user_id, db_path)


def update_inferred_preferences(
    telegram_user_id: int | str,
    updates: dict[str, Any],
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Update lightweight inferred preference fields for a Telegram user."""
    cleaned = _clean_updates(updates, INFERRED_PREFERENCE_KEYS)
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        current = conn.execute(
            "SELECT * FROM inferred_preferences WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        data = _inferred_row_to_dict(current)
        data.update(cleaned)

        conn.execute(
            """
            INSERT INTO inferred_preferences (
                user_id, high_interest_topics_json, low_interest_topics_json,
                confidence, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                high_interest_topics_json = excluded.high_interest_topics_json,
                low_interest_topics_json = excluded.low_interest_topics_json,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (
                user["id"],
                _json_dumps(data.get("high_interest_topics") or []),
                _json_dumps(data.get("low_interest_topics") or []),
                data.get("confidence"),
                _now(),
            ),
        )
    return get_user_profile(telegram_user_id, db_path)


def delete_user_profile(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> bool:
    """Delete a user profile and all related preference/event rows."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM users WHERE telegram_user_id = ?",
            (str(telegram_user_id),),
        )
        deleted = cur.rowcount > 0
    if deleted:
        _compact_database_after_delete(db_path)
    return deleted


def log_feedback_event(
    telegram_user_id: int | str,
    event_type: str,
    *,
    topic: str | None = None,
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Record explicit lightweight feedback such as useful/not_useful."""
    event_type = str(event_type).strip()
    if event_type not in FEEDBACK_EVENT_TYPES:
        raise ValueError(f"Unsupported feedback event_type: {event_type}")
    topic = _validate_event_text(topic, path="topic")
    message_id = _validate_event_text(message_id, path="message_id")
    _reject_sensitive_payload(metadata or {}, path="metadata")
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        cur = conn.execute(
            """
            INSERT INTO feedback_events (
                user_id, event_type, topic, message_id, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                event_type,
                topic,
                message_id,
                _json_dumps(metadata or {}),
                _now(),
            ),
        )
        return int(cur.lastrowid)


def log_raw_event(
    telegram_user_id: int | str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    ttl_days: int = 14,
    db_path: str | Path | None = None,
) -> int:
    """Record a short-term raw event for feedback loops.

    Raw events are intentionally short-lived and excluded from profile reads.
    Keep payloads small and avoid sensitive financial identity data.
    """
    event_type = str(event_type).strip()
    if not event_type:
        raise ValueError("event_type is required")
    if ttl_days <= 0:
        raise ValueError("ttl_days must be positive")
    if ttl_days > MAX_RAW_EVENT_TTL_DAYS:
        raise ValueError(f"ttl_days must be <= {MAX_RAW_EVENT_TTL_DAYS}")
    _reject_sensitive_payload(payload or {}, path="payload")
    payload_json = _json_dumps(payload or {})
    if len(payload_json.encode("utf-8")) > MAX_RAW_EVENT_PAYLOAD_BYTES:
        raise ValueError("raw event payload is too large")
    init_db(db_path)
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=ttl_days)
    with _connect(db_path) as conn:
        _purge_expired_raw_events(conn)
        user = _ensure_user(conn, telegram_user_id)
        cur = conn.execute(
            """
            INSERT INTO raw_events (
                user_id, event_type, event_payload_json, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                event_type,
                payload_json,
                created_at.isoformat(timespec="seconds"),
                expires_at.isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid)
