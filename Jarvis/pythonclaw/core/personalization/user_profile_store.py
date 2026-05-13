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

SCHEMA_VERSION = 6

# ── Inference aggregation thresholds ─────────────────────────────────────────
# Minimum number of 'useful' feedback events on a topic before it may be
# promoted to high_interest_topics (net-value rule also requires useful > neg).
MIN_USEFUL_COUNT: int = 2
# Minimum number of negative feedback events before a topic may be added to
# low_interest_topics (net-value rule also requires negative > useful).
MIN_NEGATIVE_COUNT: int = 2
# Total feedback events required to reach confidence = 1.0.
CONFIDENCE_FULL_AT: int = 10
# Maximum number of topics stored per inferred list.
MAX_INFERRED_TOPICS: int = 8

# ── Safe topic taxonomy ───────────────────────────────────────────────────────
# Only topics in this set are accepted by log_feedback_event().  This prevents
# sensitive inferences (e.g. "家庭压力", "签证焦虑") from accumulating in the
# personalization store.  Extend this list as new alert categories are added.
ALLOWED_FEEDBACK_TOPICS: frozenset[str] = frozenset({
    # ── Exchange rate instruments ──
    "CNY", "AUD", "USD", "人民币", "澳元", "美元", "汇率",
    # ── Macro / economic drivers ──
    "RBA", "RBA政策", "oil", "油价", "inflation", "通胀",
    "interest_rate", "利率", "trade", "贸易",
    # ── Geopolitical / regional ──
    "China", "Australia", "中澳关系", "中东局势",
    # ── Alert / product categories ──
    "bank_rates", "银行牌价", "market_news", "市场新闻",
    "通用市场新闻", "major_news", "重大新闻",
    "volatility", "波动", "daily_report", "晨报",
    # ── Research presets ──
    "fx_cnyaud", "research",
})

EXPLICIT_PREFERENCE_KEYS = {
    "language",
    "target_rate",
    "alert_threshold",
    "purpose",
    "preferred_summary_style",
    "preferred_topics",
    "preferred_banks",
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
PREFERENCE_DECLARATION_STATUSES = {"pending", "confirmed", "rejected", "deleted"}

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
MAX_BANKS_PER_FIELD = 10
MAX_NEWS_CONTEXT_TITLE_BYTES = 1024
MAX_NEWS_CONTEXT_SUMMARY_BYTES = 4096
MAX_NEWS_CONTEXT_URL_BYTES = 1024
MAX_NEWS_CONTEXT_TIMESTAMP_BYTES = 128
NEWS_FEEDBACK_CONTEXT_TTL_DAYS = 3
NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT = 10
NEWS_FEEDBACK_IDLE_LOG_DAYS = 7
NEWS_FEEDBACK_TAG_CATEGORIES = {"news_tag", "news_article_quality"}

PREFERRED_SUMMARY_STYLES = {"brief", "standard", "detailed", "action_first"}
PRIVACY_LEVELS = {"minimal", "standard", "strict"}
ALERT_PREFERENCES = {"target_rate", "volatility", "major_news", "morning_report"}
PURPOSE_ALIASES = {
    "tuition": "tuition",
    "学费": "tuition",
    "living": "living",
    "生活": "living",
    "生活费": "living",
    "investment": "investment",
    "投资": "investment",
    "general": "general",
    "一般": "general",
    "通用": "general",
    "其他": "general",
}
SAFE_PURPOSES = {"tuition", "living", "investment", "general"}
SAFE_USER_CONTEXT_FIELDS = (
    "target_rate",
    "alert_threshold",
    "purpose",
    "risk_level",
    "preferred_summary_style",
    "preferred_topics",
    "privacy_level",
)
DEFAULT_SAFE_USER_CONTEXT: dict[str, Any] = {
    "target_rate": None,
    "alert_threshold": None,
    "purpose": None,
    "risk_level": "unknown",
    "preferred_summary_style": "standard",
    "preferred_topics": [],
    "privacy_level": "standard",
}

CANONICAL_BANK_NAMES = {
    "中国银行",
    "工商银行",
    "建设银行",
    "农业银行",
    "交通银行",
    "招商银行",
    "中信银行",
    "兴业银行",
    "光大银行",
    "浦发银行",
}
DEFAULT_PREFERRED_BANKS = ["中国银行", "建设银行", "工商银行"]
BANK_ALIASES = {
    "boc": "中国银行",
    "中行": "中国银行",
    "中国银行": "中国银行",
    "icbc": "工商银行",
    "工行": "工商银行",
    "工商银行": "工商银行",
    "ccb": "建设银行",
    "建行": "建设银行",
    "建设银行": "建设银行",
    "abc": "农业银行",
    "农行": "农业银行",
    "农业银行": "农业银行",
    "bocom": "交通银行",
    "bankcomm": "交通银行",
    "交行": "交通银行",
    "交通银行": "交通银行",
    "cmb": "招商银行",
    "招行": "招商银行",
    "招商银行": "招商银行",
    "citic": "中信银行",
    "中信": "中信银行",
    "中信银行": "中信银行",
    "cib": "兴业银行",
    "兴业": "兴业银行",
    "兴业银行": "兴业银行",
    "ceb": "光大银行",
    "光大": "光大银行",
    "光大银行": "光大银行",
    "spdb": "浦发银行",
    "浦发": "浦发银行",
    "浦发银行": "浦发银行",
}

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
    if key == "preferred_banks":
        return False
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


def normalize_preferred_banks(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,，、\s]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raise ValueError("preferred_banks must be a list")
    if len(raw_items) > MAX_BANKS_PER_FIELD:
        raise ValueError("Too many banks in preferred_banks")

    banks: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, str):
            raise ValueError(f"preferred_banks[{index}] must be a string")
        item = item.strip()
        if not item:
            continue
        bank = BANK_ALIASES.get(item.lower(), BANK_ALIASES.get(item))
        if bank not in CANONICAL_BANK_NAMES:
            raise ValueError(f"Unsupported preferred bank: {item}")
        if bank not in banks:
            banks.append(bank)
    return banks


def _validate_preference_value(key: str, value: Any) -> Any:
    if key in {"target_rate", "alert_threshold", "actionability_threshold"}:
        if value is None:
            return None
        return float(value)
    if key == "preferred_topics":
        return _validate_topic_list(value, path=key)
    if key == "preferred_banks":
        return normalize_preferred_banks(value)
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
        value = PURPOSE_ALIASES.get(value.lower(), PURPOSE_ALIASES.get(value))
        if value not in SAFE_PURPOSES:
            raise ValueError(f"Unsupported purpose: {value}")
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
    if explicit_columns and "preferred_banks_json" not in explicit_columns:
        conn.execute("ALTER TABLE explicit_preferences ADD COLUMN preferred_banks_json TEXT")

    inferred_columns = _column_names(conn, "inferred_preferences")
    if inferred_columns and "confidence" not in inferred_columns:
        conn.execute("ALTER TABLE inferred_preferences ADD COLUMN confidence REAL")

    feedback_columns = _column_names(conn, "feedback_events")
    if feedback_columns:
        for column in ("task_id", "brief_id", "section_title", "category"):
            if column not in feedback_columns:
                conn.execute(f"ALTER TABLE feedback_events ADD COLUMN {column} TEXT")

    news_context_columns = _column_names(conn, "news_feedback_context")
    if news_context_columns and "articles_json" not in news_context_columns:
        conn.execute("ALTER TABLE news_feedback_context ADD COLUMN articles_json TEXT")
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
                preferred_banks_json TEXT,
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
                task_id TEXT,
                brief_id TEXT,
                section_title TEXT,
                category TEXT,
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

            CREATE TABLE IF NOT EXISTS news_feedback_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                article_title TEXT,
                article_summary TEXT,
                article_url TEXT,
                tags_json TEXT NOT NULL,
                articles_json TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                summarized_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS preference_declarations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                declaration TEXT NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_user_created
                ON feedback_events(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_raw_events_expires
                ON raw_events(expires_at);
            CREATE INDEX IF NOT EXISTS idx_news_feedback_context_user_expires
                ON news_feedback_context(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS idx_preference_declarations_user_status
                ON preference_declarations(user_id, status, created_at);
            """
        )
        _migrate_schema(conn)
    return path


def purge_expired_raw_events(db_path: str | Path | None = None) -> int:
    """Delete expired raw events so short-term data does not accumulate."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return _purge_expired_raw_events(conn)


def purge_expired_news_feedback_context(db_path: str | Path | None = None) -> int:
    """Delete expired news feedback context rows that were already summarized."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            DELETE FROM news_feedback_context
            WHERE expires_at <= ? AND summarized_at IS NOT NULL
            """,
            (_now(),),
        )
        return int(cur.rowcount)


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
        "preferred_banks": _json_loads(row["preferred_banks_json"], []),
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
        high_value_topics = [
            row["topic"]
            for row in conn.execute(
                """
                SELECT topic, COUNT(*) AS count
                FROM feedback_events
                WHERE user_id = ? AND event_type = 'useful' AND topic IS NOT NULL AND topic != ''
                GROUP BY topic
                ORDER BY count DESC, MAX(created_at) DESC
                LIMIT 10
                """,
                (user["id"],),
            )
        ]
        false_positive_topics = [
            row["topic"]
            for row in conn.execute(
                """
                SELECT topic, COUNT(*) AS count
                FROM feedback_events
                WHERE user_id = ?
                  AND event_type IN ('not_useful', 'useless', 'not_interested')
                  AND topic IS NOT NULL AND topic != ''
                GROUP BY topic
                ORDER BY count DESC, MAX(created_at) DESC
                LIMIT 10
                """,
                (user["id"],),
            )
        ]
        feedback_summary = dict(feedback_counts)
        feedback_summary.update(
            {
                "useful_alert_count": feedback_counts.get("useful", 0),
                "ignored_alert_count": (
                    feedback_counts.get("not_useful", 0)
                    + feedback_counts.get("useless", 0)
                    + feedback_counts.get("not_interested", 0)
                ),
                "false_positive_topics": false_positive_topics,
                "high_value_topics": high_value_topics,
            }
        )

        return {
            "user": dict(user),
            "explicit_preferences": _explicit_row_to_dict(explicit),
            "inferred_preferences": _inferred_row_to_dict(inferred),
            "feedback_summary": feedback_summary,
        }


def build_safe_user_context(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the minimal personalization context allowed in LLM prompts.

    Full profiles can contain inferred preferences, feedback aggregates, and
    operational traces. Those are useful for application logic but are not
    passed to LLM providers; prompts only receive this explicit allowlist so
    raw events, detailed feedback history, behavior logs, and sensitive data
    never leave the local profile store.
    """
    profile = get_user_profile(telegram_user_id, db_path)
    if not profile:
        return dict(DEFAULT_SAFE_USER_CONTEXT)

    explicit = profile.get("explicit_preferences") or {}
    safe_context = dict(DEFAULT_SAFE_USER_CONTEXT)
    for field in SAFE_USER_CONTEXT_FIELDS:
        if field == "risk_level":
            continue
        value = explicit.get(field)
        if field == "purpose":
            value = PURPOSE_ALIASES.get(str(value).lower(), PURPOSE_ALIASES.get(value))
        if value not in (None, "", []):
            safe_context[field] = value
    return safe_context


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
                preferred_summary_style, preferred_topics_json, preferred_banks_json, privacy_level,
                preferred_reminder_time, actionability_threshold, alert_preference,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                language = excluded.language,
                target_rate = excluded.target_rate,
                alert_threshold = excluded.alert_threshold,
                purpose = excluded.purpose,
                preferred_summary_style = excluded.preferred_summary_style,
                preferred_topics_json = excluded.preferred_topics_json,
                preferred_banks_json = excluded.preferred_banks_json,
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
                _json_dumps(data.get("preferred_banks") or []),
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
    task_id: str | None = None,
    brief_id: str | None = None,
    section_title: str | None = None,
    category: str | None = None,
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Record explicit lightweight feedback such as useful/not_useful."""
    event_type = str(event_type).strip()
    if event_type not in FEEDBACK_EVENT_TYPES:
        raise ValueError(f"Unsupported feedback event_type: {event_type}")
    topic = _validate_event_text(topic, path="topic")
    raw_category = _validate_event_text(category, path="category") or topic
    category = raw_category.lower() if raw_category else None
    if (
        topic is not None
        and topic not in ALLOWED_FEEDBACK_TOPICS
        and category not in NEWS_FEEDBACK_TAG_CATEGORIES
    ):
        raise ValueError(f"topic_not_allowed:{topic}")
    task_id = _validate_event_text(task_id, path="task_id")
    brief_id = _validate_event_text(brief_id, path="brief_id")
    section_title = _validate_event_text(section_title, path="section_title")
    message_id = _validate_event_text(message_id, path="message_id")
    _reject_sensitive_payload(metadata or {}, path="metadata")
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        cur = conn.execute(
            """
            INSERT INTO feedback_events (
                user_id, event_type, topic, task_id, brief_id, section_title,
                category, message_id, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                event_type,
                topic,
                task_id,
                brief_id,
                section_title,
                category,
                message_id,
                _json_dumps(metadata or {}),
                _now(),
            ),
        )
        return int(cur.lastrowid)


def store_news_feedback_context(
    telegram_user_id: int | str,
    *,
    article_title: str,
    article_summary: str,
    article_url: str | None = None,
    tags: list[str] | None = None,
    articles: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    ttl_days: int = NEWS_FEEDBACK_CONTEXT_TTL_DAYS,
    db_path: str | Path | None = None,
) -> int:
    """Store short-lived news context for tag-level Telegram feedback.

    This table is intentionally time-bound. It stores only the article-level
    context needed to interpret a short callback token; it is not returned by
    ``get_user_profile()`` and is not sent to LLM prompts.
    """
    title = str(article_title or "").strip()
    summary = str(article_summary or "").strip()
    url = str(article_url or "").strip() if article_url is not None else None
    _reject_sensitive_string(
        title, path="article_title", max_bytes=MAX_NEWS_CONTEXT_TITLE_BYTES
    )
    _reject_sensitive_string(
        summary, path="article_summary", max_bytes=MAX_NEWS_CONTEXT_SUMMARY_BYTES
    )
    if url:
        _reject_sensitive_string(
            url, path="article_url", max_bytes=MAX_NEWS_CONTEXT_URL_BYTES
        )
    clean_tags = _validate_topic_list(tags or [], path="news_feedback_tags")
    if not clean_tags:
        raise ValueError("news_feedback_context requires at least one tag")
    clean_articles: list[dict[str, Any]] = []
    for index, item in enumerate(articles or []):
        if not isinstance(item, dict):
            raise ValueError(f"articles[{index}] must be a dict")
        item_title = str(item.get("title") or "").strip()
        item_summary = str(item.get("summary") or "").strip()
        item_url = str(item.get("url") or "").strip() if item.get("url") is not None else None
        item_published = (
            str(item.get("published") or "").strip()
            if item.get("published") is not None else None
        )
        _reject_sensitive_string(
            item_title,
            path=f"articles[{index}].title",
            max_bytes=MAX_NEWS_CONTEXT_TITLE_BYTES,
        )
        _reject_sensitive_string(
            item_summary,
            path=f"articles[{index}].summary",
            max_bytes=MAX_NEWS_CONTEXT_SUMMARY_BYTES,
        )
        if item_url:
            _reject_sensitive_string(
                item_url,
                path=f"articles[{index}].url",
                max_bytes=MAX_NEWS_CONTEXT_URL_BYTES,
            )
        if item_published:
            _reject_sensitive_string(
                item_published,
                path=f"articles[{index}].published",
                max_bytes=MAX_NEWS_CONTEXT_TIMESTAMP_BYTES,
            )
        clean_item = {
            "title": item_title,
            "summary": item_summary,
            "url": item_url,
            "published": item_published,
            "tags": _validate_topic_list(item.get("tags") or [], path=f"articles[{index}].tags"),
        }
        clean_articles.append({
            key: value for key, value in clean_item.items()
            if value not in (None, "", [])
        })
    _reject_sensitive_payload(metadata or {}, path="metadata")
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=max(1, int(ttl_days)))).isoformat(timespec="seconds")

    init_db(db_path)
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        cur = conn.execute(
            """
            INSERT INTO news_feedback_context (
                user_id, article_title, article_summary, article_url, tags_json,
                articles_json, metadata_json, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                title,
                summary,
                url,
                _json_dumps(clean_tags),
                _json_dumps(clean_articles),
                _json_dumps(metadata or {}),
                now.isoformat(timespec="seconds"),
                expires_at,
            ),
        )
        return int(cur.lastrowid)


def get_news_feedback_context(
    telegram_user_id: int | str,
    feedback_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return an unexpired short-lived news feedback context row for a user."""
    try:
        fid = int(feedback_id)
    except (TypeError, ValueError):
        return None
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return None
        row = conn.execute(
            """
            SELECT *
            FROM news_feedback_context
            WHERE id = ? AND user_id = ? AND expires_at > ?
            """,
            (fid, user["id"], _now()),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["tags"] = _json_loads(data.pop("tags_json", None), [])
        data["articles"] = _json_loads(data.pop("articles_json", None), [])
        data["metadata"] = _json_loads(data.pop("metadata_json", None), {})
        return data


def get_news_feedback_rollup_status(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return lightweight status for future PreferenceAgent scheduling/logging."""
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return {
                "feedback_count": 0,
                "context_count": 0,
                "threshold_reached": False,
                "idle_log_due": False,
            }
        rows = conn.execute(
            """
            SELECT nf.id, nf.created_at,
                   COUNT(fe.id) AS feedback_count
            FROM news_feedback_context nf
            LEFT JOIN feedback_events fe
              ON fe.user_id = nf.user_id
             AND json_extract(fe.metadata_json, '$.news_feedback_id') = CAST(nf.id AS TEXT)
            WHERE nf.user_id = ? AND nf.expires_at > ?
            GROUP BY nf.id
            """,
            (user["id"], _now()),
        ).fetchall()

    feedback_count = sum(int(row["feedback_count"]) for row in rows)
    oldest_created = min((row["created_at"] for row in rows), default=None)
    idle_log_due = False
    if oldest_created and feedback_count < NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT:
        try:
            oldest_dt = datetime.fromisoformat(str(oldest_created))
            idle_log_due = (
                datetime.now(timezone.utc) - oldest_dt
                >= timedelta(days=NEWS_FEEDBACK_IDLE_LOG_DAYS)
            )
        except ValueError:
            idle_log_due = False
    if idle_log_due:
        logger.info(
            "News feedback below summary trigger after %d days: user_id=%s count=%d threshold=%d",
            NEWS_FEEDBACK_IDLE_LOG_DAYS,
            telegram_user_id,
            feedback_count,
            NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT,
        )
    return {
        "feedback_count": feedback_count,
        "context_count": len(rows),
        "threshold_reached": feedback_count >= NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT,
        "idle_log_due": idle_log_due,
    }


def get_due_news_feedback_contexts(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return unsummarized news contexts due for PreferenceAgent processing.

    A row is due when it has feedback and either:
      - the per-user feedback count reached the trigger threshold, or
      - the row reached its 3-day expiry.
    """
    init_db(db_path)
    now = _now()
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM news_feedback_context
            WHERE user_id = ? AND summarized_at IS NULL
            ORDER BY created_at ASC
            """,
            (user["id"],),
        ).fetchall()
        feedback_rows = conn.execute(
            """
            SELECT metadata_json, event_type, topic, category, created_at
            FROM feedback_events
            WHERE user_id = ?
            """,
            (user["id"],),
        ).fetchall()

    feedback_by_context: dict[str, list[dict[str, Any]]] = {}
    for row in feedback_rows:
        metadata = _json_loads(row["metadata_json"], {}) or {}
        context_id = str(metadata.get("news_feedback_id") or "")
        if not context_id:
            continue
        feedback_by_context.setdefault(context_id, []).append(
            {
                "event_type": row["event_type"],
                "topic": row["topic"],
                "category": row["category"],
                "created_at": row["created_at"],
            }
        )

    total_feedback_count = sum(len(items) for items in feedback_by_context.values())
    threshold_reached = total_feedback_count >= NEWS_FEEDBACK_SUMMARY_TRIGGER_COUNT
    due: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        context_feedback = feedback_by_context.get(str(data["id"]), [])
        if not context_feedback:
            continue
        expired = str(data["expires_at"]) <= now
        if not expired and not threshold_reached:
            continue
        data["tags"] = _json_loads(data.pop("tags_json", None), [])
        data["articles"] = _json_loads(data.pop("articles_json", None), [])
        data["metadata"] = _json_loads(data.pop("metadata_json", None), {})
        data["feedback_events"] = context_feedback
        data["trigger_type"] = "threshold" if threshold_reached else "expired"
        due.append(data)
    return due


def mark_news_feedback_contexts_summarized(
    context_ids: list[int | str],
    db_path: str | Path | None = None,
) -> int:
    """Mark news feedback contexts as summarized after PreferenceAgent processing."""
    ids: list[int] = []
    for context_id in context_ids:
        try:
            ids.append(int(context_id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return 0
    init_db(db_path)
    placeholders = ",".join("?" for _ in ids)
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"""
            UPDATE news_feedback_context
            SET summarized_at = ?
            WHERE id IN ({placeholders})
            """,
            (_now(), *ids),
        )
        return int(cur.rowcount)


def create_preference_declaration(
    telegram_user_id: int | str,
    declaration: str,
    *,
    evidence_count: int = 0,
    source: str = "news_feedback",
    metadata: dict[str, Any] | None = None,
    status: str = "pending",
    db_path: str | Path | None = None,
) -> int:
    """Create a pending inferred preference declaration for user confirmation."""
    status = str(status or "pending").strip()
    if status not in PREFERENCE_DECLARATION_STATUSES:
        raise ValueError(f"Unsupported preference declaration status: {status}")
    text = _validate_event_text(declaration, path="declaration")
    if not text:
        raise ValueError("preference declaration cannot be empty")
    source = _validate_event_text(source, path="source") or "news_feedback"
    _reject_sensitive_payload(metadata or {}, path="metadata")
    now = _now()
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _ensure_user(conn, telegram_user_id)
        cur = conn.execute(
            """
            INSERT INTO preference_declarations (
                user_id, declaration, evidence_count, status, source,
                metadata_json, created_at, updated_at, confirmed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                text,
                max(0, int(evidence_count)),
                status,
                source,
                _json_dumps(metadata or {}),
                now,
                now,
                now if status == "confirmed" else None,
            ),
        )
        return int(cur.lastrowid)


def list_preference_declarations(
    telegram_user_id: int | str,
    *,
    status: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List inferred preference declarations without exposing raw feedback rows."""
    if status is not None and status not in PREFERENCE_DECLARATION_STATUSES:
        raise ValueError(f"Unsupported preference declaration status: {status}")
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return []
        if status is None:
            rows = conn.execute(
                """
                SELECT *
                FROM preference_declarations
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user["id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM preference_declarations
                WHERE user_id = ? AND status = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user["id"], status),
            ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["metadata"] = _json_loads(data.pop("metadata_json", None), {})
        out.append(data)
    return out


def update_preference_declaration_status(
    telegram_user_id: int | str,
    declaration_id: int | str,
    status: str,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Update declaration status: pending/confirmed/rejected/deleted."""
    status = str(status or "").strip()
    if status not in PREFERENCE_DECLARATION_STATUSES:
        raise ValueError(f"Unsupported preference declaration status: {status}")
    try:
        declaration_id_int = int(declaration_id)
    except (TypeError, ValueError):
        return False
    now = _now()
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return False
        cur = conn.execute(
            """
            UPDATE preference_declarations
            SET status = ?,
                updated_at = ?,
                confirmed_at = CASE WHEN ? = 'confirmed' THEN ? ELSE confirmed_at END
            WHERE id = ? AND user_id = ?
            """,
            (status, now, status, now, declaration_id_int, user["id"]),
        )
        return cur.rowcount > 0


def get_user_category_feedback_summary(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, float]:
    """Return category-level feedback scores without raw content/history.

    Scores are deterministic and normalised to [-1.0, 1.0]:
    useful = +1, not_useful/useless = -1, not_interested = -2.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return {}
        rows = conn.execute(
            """
            SELECT category, event_type, COUNT(*) AS cnt
            FROM feedback_events
            WHERE user_id = ?
              AND category IS NOT NULL AND category != ''
            GROUP BY category, event_type
            """,
            (user["id"],),
        ).fetchall()

    weights = {
        "useful": 1,
        "not_useful": -1,
        "useless": -1,
        "not_interested": -2,
    }
    counts_by_category: dict[str, dict[str, int]] = {}
    for row in rows:
        category = str(row["category"])
        event_type = str(row["event_type"])
        counts_by_category.setdefault(category, {})[event_type] = int(row["cnt"])

    summary: dict[str, float] = {}
    for category, counts in counts_by_category.items():
        total = sum(counts.values())
        if total <= 0:
            continue
        weighted = sum(weights.get(event, 0) * count for event, count in counts.items())
        summary[category] = round(max(-1.0, min(1.0, weighted / total)), 4)
    return summary


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


def update_inferred_preferences_from_feedback(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Aggregate feedback_events into inferred preferences using a net-value rule.

    This is a pure rule-based aggregator — no LLM is involved and raw events
    are never exposed.  Only the ``feedback_events`` table is read.  Inferred
    preferences are stored separately from explicit preferences.

    Net-value aggregation rules
    ---------------------------
    For each topic with at least one feedback event:

    high_interest_topics
        ``useful >= MIN_USEFUL_COUNT  AND  useful > negative``
    low_interest_topics
        ``negative >= MIN_NEGATIVE_COUNT  AND  negative > useful``
    Tie / insufficient data
        Topics where useful == negative (both above threshold) or neither
        reaches its threshold are not classified — avoiding overconfident
        inferences from conflicting signals.

    confidence
        ``min(1.0, total_feedback_events / CONFIDENCE_FULL_AT)`` — reflects
        how much feedback data backs the inferred preferences overall.
        This is a *volume* confidence, not per-topic accuracy.

    Returns the full updated user profile dict (same shape as
    ``get_user_profile``).  Returns ``{}`` if the user does not exist yet.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return {}

        user_id = user["id"]

        # Collect per-topic useful counts
        useful_counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT topic, COUNT(*) AS cnt
            FROM feedback_events
            WHERE user_id = ?
              AND event_type = 'useful'
              AND topic IS NOT NULL AND topic != ''
            GROUP BY topic
            """,
            (user_id,),
        ):
            useful_counts[row["topic"]] = int(row["cnt"])

        # Collect per-topic negative counts
        negative_counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT topic, COUNT(*) AS cnt
            FROM feedback_events
            WHERE user_id = ?
              AND event_type IN ('not_useful', 'useless', 'not_interested')
              AND topic IS NOT NULL AND topic != ''
            GROUP BY topic
            """,
            (user_id,),
        ):
            negative_counts[row["topic"]] = int(row["cnt"])

        # Total feedback events for confidence calculation
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM feedback_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        total_count = int(total_row["cnt"]) if total_row else 0

    # Apply net-value rule: classify each known topic exactly once
    all_topics = set(useful_counts) | set(negative_counts)
    high_scored: list[tuple[str, int]] = []
    low_scored: list[tuple[str, int]] = []

    for topic in all_topics:
        u = useful_counts.get(topic, 0)
        n = negative_counts.get(topic, 0)
        if u >= MIN_USEFUL_COUNT and u > n:
            high_scored.append((topic, u))
        elif n >= MIN_NEGATIVE_COUNT and n > u:
            low_scored.append((topic, n))
        # u == n (tie) or below threshold → no classification

    # Sort by count descending, cap at MAX_INFERRED_TOPICS
    high_scored.sort(key=lambda x: -x[1])
    low_scored.sort(key=lambda x: -x[1])
    high_interest = [t for t, _ in high_scored[:MAX_INFERRED_TOPICS]]
    low_interest = [t for t, _ in low_scored[:MAX_INFERRED_TOPICS]]
    confidence = round(min(1.0, total_count / CONFIDENCE_FULL_AT), 2)

    updates: dict[str, Any] = {
        "high_interest_topics": high_interest,
        "low_interest_topics": low_interest,
        "confidence": confidence,
    }
    return update_inferred_preferences(telegram_user_id, updates, db_path)


def clear_inferred_preferences(
    telegram_user_id: int | str,
    db_path: str | Path | None = None,
) -> bool:
    """Delete only the inferred_preferences row for a user.

    Explicit preferences and feedback records are kept intact.  This lets a
    user correct a wrong inference without losing their explicit settings or
    the feedback history that drives future re-aggregation.

    Returns ``True`` if a row was deleted, ``False`` if no inferred preferences
    existed for the user.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        user = _user_row(conn, telegram_user_id)
        if user is None:
            return False
        cur = conn.execute(
            "DELETE FROM inferred_preferences WHERE user_id = ?",
            (user["id"],),
        )
        return cur.rowcount > 0


def format_inferred_preferences_display(inferred: dict[str, Any]) -> str:
    """Return a human-readable Chinese summary of inferred preferences.

    Example output::

        Jarvis 对你的偏好理解：
        - 你可能更关注：RBA政策、中澳关系
        - 你对以下话题关注度较低：通用市场新闻
        - 推断置信度：40%（基于你的反馈次数）
        - 以上为 Jarvis 根据你的反馈自动推断，并非你主动设置的明确偏好。

    Returns an empty string when there are no inferred preferences yet so the
    caller can skip the section entirely.
    """
    high: list[str] = inferred.get("high_interest_topics") or []
    low: list[str] = inferred.get("low_interest_topics") or []
    confidence: float | None = inferred.get("confidence")

    if not high and not low:
        return ""

    lines = ["Jarvis 对你的偏好理解："]
    if high:
        lines.append(f"- 你可能更关注：{'、'.join(str(t) for t in high)}")
    if low:
        lines.append(f"- 你对以下话题关注度较低：{'、'.join(str(t) for t in low)}")
    if confidence is not None:
        pct = int(confidence * 100)
        lines.append(f"- 推断置信度：{pct}%（基于你的反馈次数）")
    lines.append("- 以上为 Jarvis 根据你的反馈自动推断，并非你主动设置的明确偏好。")
    return "\n".join(lines)
