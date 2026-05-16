"""
Phase 9.1 — SQLite 证据存储 MVP。

轻量级运行时微型 RAG 证据存储层，用于持久化、检索和追溯研究过程中的证据块。
不使用向量数据库、LLM、Telegram、内存或用户资料。

存储路径规范：
  PYTHONCLAW_HOME/context/evidence/research_evidence.sqlite3
  支持 ":memory:" 内存模式（测试用）。

表结构：
  evidence_chunks    — 证据文本块
  evidence_findings  — 发现与证据块的关联
  citation_refs      — 简报章节对证据块的引用
  retrieval_traces   — 检索操作的可观测性记录
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import time

_log = logging.getLogger(__name__)

_PROVIDER_ONLY_SOURCE_LABELS = {"google_news_rss", "tavily", "web_search"}
_CONFLICT_SELECTION_BOOST = 0.10

from schema import (
    AgentOutput,
    CitationRef,
    ContextPack,
    ContextPackItem,
    EvidenceChunk,
    EvidenceFinding,
    Finding,
    ResearchPreset,
    ResearchTask,
    RetrievalTrace,
    SafeUserContext,
    now_iso,
)

try:
    from evidence_scorer import compute_evidence_score, fallback_score
except ImportError:
    compute_evidence_score = None  # type: ignore[assignment]
    fallback_score = None  # type: ignore[assignment]

try:
    from source_metadata import (
        SourceMetadata,
        source_metadata_from_source_ref,
        source_metadata_from_legacy_string,
    )
except ImportError:
    SourceMetadata = None  # type: ignore[assignment,misc]
    source_metadata_from_source_ref = None  # type: ignore[assignment]
    source_metadata_from_legacy_string = None  # type: ignore[assignment]

try:
    from conflict_detector import detect_conflicts, apply_conflict_boost, ConflictSummary
except ImportError:
    detect_conflicts = None  # type: ignore[assignment]
    apply_conflict_boost = None  # type: ignore[assignment]
    ConflictSummary = None  # type: ignore[assignment,misc]

_SCHEMA_VERSION = 6

_DDL = """\
CREATE TABLE IF NOT EXISTS evidence_chunks (
    chunk_id       TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL DEFAULT '',
    preset_name    TEXT NOT NULL DEFAULT '',
    agent_name     TEXT NOT NULL DEFAULT '',
    content        TEXT NOT NULL DEFAULT '',
    source         TEXT,
    category       TEXT NOT NULL DEFAULT '',
    importance     REAL NOT NULL DEFAULT 0.0,
    confidence     REAL NOT NULL DEFAULT 0.0,
    entities_json  TEXT NOT NULL DEFAULT '[]',
    used_in_brief  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT '',
    ttl_policy     TEXT NOT NULL DEFAULT 'task',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    attention_score REAL NOT NULL DEFAULT 0.0,
    composite_score REAL NOT NULL DEFAULT 0.0,
    score_importance REAL NOT NULL DEFAULT 0.0,
    score_confidence REAL NOT NULL DEFAULT 0.0,
    score_recency REAL NOT NULL DEFAULT 0.0,
    score_source_quality REAL NOT NULL DEFAULT 0.0,
    score_user_relevance REAL NOT NULL DEFAULT 0.0,
    score_conflict_value REAL NOT NULL DEFAULT 0.0,
    score_reason TEXT NOT NULL DEFAULT '',
    source_metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_chunks_task     ON evidence_chunks(task_id);
CREATE INDEX IF NOT EXISTS idx_chunks_category ON evidence_chunks(category);
CREATE INDEX IF NOT EXISTS idx_chunks_agent    ON evidence_chunks(agent_name);

CREATE TABLE IF NOT EXISTS evidence_findings (
    finding_id     TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL DEFAULT '',
    agent_name     TEXT NOT NULL DEFAULT '',
    key            TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    direction      TEXT,
    chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_score REAL,
    category       TEXT NOT NULL DEFAULT '',
    importance     REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_findings_task ON evidence_findings(task_id);

CREATE TABLE IF NOT EXISTS citation_refs (
    citation_id     TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL DEFAULT '',
    chunk_id        TEXT NOT NULL DEFAULT '',
    finding_id      TEXT,
    section_title   TEXT NOT NULL DEFAULT '',
    relevance_score REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_citations_task ON citation_refs(task_id);

CREATE TABLE IF NOT EXISTS retrieval_traces (
    trace_id        TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL DEFAULT '',
    query           TEXT NOT NULL DEFAULT '',
    retrieved_count INTEGER NOT NULL DEFAULT 0,
    total_chunks    INTEGER NOT NULL DEFAULT 0,
    top_scores_json TEXT NOT NULL DEFAULT '[]',
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    timestamp       TEXT NOT NULL DEFAULT '',
    section_title   TEXT NOT NULL DEFAULT '',
    selected_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    section_covered INTEGER NOT NULL DEFAULT 0,
    score_distribution_json TEXT NOT NULL DEFAULT '{}',
    conflict_count INTEGER NOT NULL DEFAULT 0,
    conflict_pairs_json TEXT NOT NULL DEFAULT '[]',
    boosted_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    scoring_method  TEXT NOT NULL DEFAULT '',
    fallback_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_traces_task ON retrieval_traces(task_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


def _default_db_path() -> Path:
    try:
        from pythonclaw import config
        base = config.PYTHONCLAW_HOME
    except Exception:
        base = Path.home() / ".pythonclaw"
    return base / "context" / "evidence" / "research_evidence.sqlite3"


class EvidenceStore:
    """SQLite 证据存储 MVP。

    用法：
        store = EvidenceStore()          # 文件模式（默认路径）
        store = EvidenceStore(":memory:")  # 内存模式（测试用）
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = _default_db_path()
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(_DDL)
        row = cur.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current_version = row[0] if row else 0
        if current_version < 2:
            self._migrate_to_v2(cur)
        if current_version < 3:
            self._migrate_to_v3(cur)
        if current_version < 4:
            self._migrate_to_v4(cur)
        if current_version < 5:
            self._migrate_to_v5(cur)
        if current_version < 6:
            self._migrate_to_v6(cur)
        if row is None:
            cur.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        elif current_version < _SCHEMA_VERSION:
            cur.execute(
                "UPDATE schema_version SET version = ?",
                (_SCHEMA_VERSION,),
            )
        self._conn.commit()

    def _migrate_to_v2(self, cur: Any) -> None:
        """Add Phase 10 columns if missing (safe on fresh DBs too)."""
        for col, typedef in (
            ("attention_score", "REAL NOT NULL DEFAULT 0.0"),
            ("composite_score", "REAL NOT NULL DEFAULT 0.0"),
        ):
            try:
                cur.execute(
                    f"ALTER TABLE evidence_chunks ADD COLUMN {col} {typedef}"
                )
            except Exception:
                pass
        try:
            cur.execute(
                "ALTER TABLE retrieval_traces ADD COLUMN scoring_method TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass

    def _migrate_to_v3(self, cur: Any) -> None:
        """Add structured RetrievalTrace columns if missing."""
        for col, typedef in (
            ("section_title", "TEXT NOT NULL DEFAULT ''"),
            ("selected_chunk_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("section_covered", "INTEGER NOT NULL DEFAULT 0"),
            ("score_distribution_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("conflict_count", "INTEGER NOT NULL DEFAULT 0"),
            ("conflict_pairs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("boosted_chunk_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            try:
                cur.execute(
                    f"ALTER TABLE retrieval_traces ADD COLUMN {col} {typedef}"
                )
            except Exception:
                pass

    def _migrate_to_v4(self, cur: Any) -> None:
        """Add persisted score breakdown columns if missing."""
        for col, typedef in (
            ("score_importance", "REAL NOT NULL DEFAULT 0.0"),
            ("score_confidence", "REAL NOT NULL DEFAULT 0.0"),
            ("score_recency", "REAL NOT NULL DEFAULT 0.0"),
            ("score_source_quality", "REAL NOT NULL DEFAULT 0.0"),
            ("score_user_relevance", "REAL NOT NULL DEFAULT 0.0"),
            ("score_conflict_value", "REAL NOT NULL DEFAULT 0.0"),
            ("score_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                cur.execute(
                    f"ALTER TABLE evidence_chunks ADD COLUMN {col} {typedef}"
                )
            except Exception:
                pass

    def _migrate_to_v5(self, cur: Any) -> None:
        """Add fallback_reason column to retrieval_traces."""
        try:
            cur.execute(
                "ALTER TABLE retrieval_traces ADD COLUMN fallback_reason TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass

    def _migrate_to_v6(self, cur: Any) -> None:
        """Add source_metadata_json column to evidence_chunks."""
        try:
            cur.execute(
                "ALTER TABLE evidence_chunks ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        except Exception as exc:
            if "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower():
                pass
            else:
                _log.warning("_migrate_to_v6 non-duplicate error (ignored): %s", exc)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── 写入 ─────────────────────────────────────────────────────────────────

    def insert_chunk(self, chunk: EvidenceChunk) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO evidence_chunks
               (chunk_id, task_id, preset_name, agent_name, content, source,
                category, importance, confidence, entities_json,
                used_in_brief, created_at, ttl_policy, token_estimate,
                attention_score, composite_score, score_importance,
                score_confidence, score_recency, score_source_quality,
                score_user_relevance, score_conflict_value, score_reason,
                source_metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                chunk.chunk_id,
                chunk.task_id,
                chunk.preset_name,
                chunk.agent_name,
                chunk.content,
                chunk.source,
                chunk.category,
                chunk.importance,
                chunk.confidence,
                json.dumps(chunk.entities, ensure_ascii=False),
                int(chunk.used_in_brief),
                chunk.created_at,
                chunk.ttl_policy,
                chunk.token_estimate,
                chunk.attention_score,
                chunk.composite_score,
                chunk.score_importance,
                chunk.score_confidence,
                chunk.score_recency,
                chunk.score_source_quality,
                chunk.score_user_relevance,
                chunk.score_conflict_value,
                chunk.score_reason,
                chunk.source_metadata_json,
            ),
        )
        self._conn.commit()

    def insert_finding(self, finding: EvidenceFinding, task_id: str = "") -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO evidence_findings
               (finding_id, task_id, agent_name, key, summary, direction,
                chunk_ids_json, evidence_score, category, importance)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                finding.finding_id,
                task_id,
                finding.agent_name,
                finding.key,
                finding.summary,
                finding.direction,
                json.dumps(finding.chunk_ids, ensure_ascii=False),
                finding.evidence_score,
                finding.category,
                finding.importance,
            ),
        )
        self._conn.commit()

    def insert_citation(self, citation: CitationRef, task_id: str = "") -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO citation_refs
               (citation_id, task_id, chunk_id, finding_id, section_title, relevance_score)
               VALUES (?,?,?,?,?,?)""",
            (
                citation.citation_id,
                task_id,
                citation.chunk_id,
                citation.finding_id,
                citation.section_title,
                citation.relevance_score,
            ),
        )
        self._conn.commit()

    def insert_trace(self, trace: RetrievalTrace, task_id: str = "") -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO retrieval_traces
               (trace_id, task_id, query, retrieved_count, total_chunks,
                top_scores_json, latency_ms, timestamp, section_title,
                selected_chunk_ids_json, section_covered, score_distribution_json,
                conflict_count, conflict_pairs_json, boosted_chunk_ids_json,
                scoring_method, fallback_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trace.trace_id,
                task_id,
                trace.query,
                trace.retrieved_count,
                trace.total_chunks,
                json.dumps(trace.top_scores),
                trace.latency_ms,
                trace.timestamp,
                trace.section_title,
                json.dumps(trace.selected_chunk_ids, ensure_ascii=False),
                int(trace.section_covered),
                json.dumps(trace.score_distribution, ensure_ascii=False),
                trace.conflict_count,
                json.dumps(trace.conflict_pairs, ensure_ascii=False),
                json.dumps(trace.boosted_chunk_ids, ensure_ascii=False),
                trace.scoring_method,
                trace.fallback_reason,
            ),
        )
        self._conn.commit()

    # ── 读取 ─────────────────────────────────────────────────────────────────

    def get_chunk(self, chunk_id: str) -> EvidenceChunk | None:
        row = self._conn.execute(
            "SELECT * FROM evidence_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_chunk(row)

    def get_finding(self, finding_id: str) -> EvidenceFinding | None:
        row = self._conn.execute(
            "SELECT * FROM evidence_findings WHERE finding_id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_finding(row)

    def get_citation(self, citation_id: str) -> CitationRef | None:
        row = self._conn.execute(
            "SELECT * FROM citation_refs WHERE citation_id = ?", (citation_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_citation(row)

    def query_chunks(
        self,
        task_id: str,
        *,
        category: str | None = None,
        entities: list[str] | None = None,
        agent_name: str | None = None,
        min_importance: float | None = None,
        source_type: str | None = None,
        time_after: str | None = None,
        top_k: int = 5,
    ) -> list[EvidenceChunk]:
        # 已知限制：entities 过滤在 SQL LIMIT 之后的 Python 层执行，
        # 当同时指定 entities 和 top_k 时实际返回数量可能 < top_k。
        clauses: list[str] = ["task_id = ?"]
        params: list[Any] = [task_id]

        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if agent_name is not None:
            clauses.append("agent_name = ?")
            params.append(agent_name)
        if min_importance is not None:
            clauses.append("importance >= ?")
            params.append(min_importance)
        if source_type is not None:
            clauses.append("source = ?")
            params.append(source_type)
        if time_after is not None:
            clauses.append("created_at > ?")
            params.append(time_after)

        where = " AND ".join(clauses)
        sql = (
            f"SELECT * FROM evidence_chunks WHERE {where} "
            f"ORDER BY importance DESC, confidence DESC, created_at DESC LIMIT ?"
        )
        params.append(top_k)

        rows = self._conn.execute(sql, params).fetchall()
        results = [self._row_to_chunk(r) for r in rows]

        if entities:
            target = set(entities)
            results = [
                c for c in results
                if target & set(c.entities)
            ]

        return results

    def mark_used_in_brief(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        placeholders = ",".join("?" for _ in chunk_ids)
        cur = self._conn.execute(
            f"UPDATE evidence_chunks SET used_in_brief = 1 "
            f"WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
        self._conn.commit()
        return cur.rowcount

    def list_traces(self, task_id: str) -> list[RetrievalTrace]:
        rows = self._conn.execute(
            "SELECT * FROM retrieval_traces WHERE task_id = ? ORDER BY timestamp",
            (task_id,),
        ).fetchall()
        return [self._row_to_trace(r) for r in rows]

    # ── 统计 ─────────────────────────────────────────────────────────────────

    def count_chunks(self, task_id: str | None = None) -> int:
        if task_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM evidence_chunks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM evidence_chunks"
            ).fetchone()
        return row[0] if row else 0

    # ── 摄取 ─────────────────────────────────────────────────────────────────

    _AGENT_CATEGORY_DEFAULTS: dict[str, str] = {
        "fx_agent":              "fx_price",
        "news_agent":            "news_event",
        "macro_agent":           "macro",
        "risk_agent":            "risk",
        "market_drivers_agent":  "market_driver",
        "policy_signal_agent":   "policy_signal",
        "commodity_agent":       "commodity_trade",
        "macro_indicator_agent": "macro_indicator",
    }

    def ingest_outputs(
        self,
        task: ResearchTask,
        outputs: list[AgentOutput],
    ) -> list[AgentOutput]:
        """将 AgentOutput 列表转换为 EvidenceChunk/EvidenceFinding 并持久化。

        返回带有 chunk_ids / finding_ids / evidence_count 的 AgentOutput 副本，
        不修改原始输入对象。
        """
        source_map: dict[str, Any] = {}
        for out in outputs:
            for src in out.sources:
                source_map[src.url] = src

        enriched: list[AgentOutput] = []

        for output in outputs:
            chunk_ids: list[str] = []
            finding_ids: list[str] = []

            for finding in output.findings:
                source_label = self._resolve_source(
                    finding.source_ids, output.sources, source_map,
                )
                if finding.key:
                    source_label = f"{source_label} | finding_key={finding.key}" if source_label else f"finding_key={finding.key}"
                entities = list(finding.entities) if finding.entities else (
                    list(task.focus_assets) if task.focus_assets else []
                )

                content = self._build_context_content(
                    task=task,
                    agent_name=output.agent_name,
                    finding=finding,
                    entities=entities,
                    source_label=source_label,
                    as_of=output.as_of,
                )

                raw_cat = finding.category or self._AGENT_CATEGORY_DEFAULTS.get(
                    output.agent_name, "",
                )
                category = raw_cat.value if hasattr(raw_cat, "value") else raw_cat
                importance = finding.importance if finding.importance > 0 else output.confidence

                source_meta_json = "{}"
                if source_metadata_from_source_ref is not None:
                    src_ref = None
                    if finding.source_ids:
                        for sid in finding.source_ids:
                            if sid in source_map:
                                src_ref = source_map[sid]
                                break
                    elif output.sources:
                        src_ref = output.sources[0]
                    if src_ref is not None:
                        source_meta_json = source_metadata_from_source_ref(src_ref).to_json()
                    elif source_label:
                        source_meta_json = source_metadata_from_legacy_string(source_label).to_json()

                chunk = EvidenceChunk(
                    task_id=task.task_id,
                    preset_name=task.preset_name,
                    agent_name=output.agent_name,
                    content=content,
                    source=source_label or None,
                    category=category,
                    importance=importance,
                    confidence=output.confidence,
                    entities=entities,
                    token_estimate=len(content),
                    source_metadata_json=source_meta_json,
                )
                self.insert_chunk(chunk)
                chunk_ids.append(chunk.chunk_id)

                ev_finding = EvidenceFinding(
                    agent_name=output.agent_name,
                    key=finding.key,
                    summary=finding.summary,
                    direction=finding.direction,
                    chunk_ids=[chunk.chunk_id],
                    evidence_score=finding.evidence_score,
                    category=category,
                    importance=importance,
                )
                self.insert_finding(ev_finding, task_id=task.task_id)
                finding_ids.append(ev_finding.finding_id)

            enriched.append(replace(
                output,
                chunk_ids=chunk_ids,
                finding_ids=finding_ids,
                evidence_count=len(chunk_ids),
            ))

        return enriched

    @staticmethod
    def _resolve_source(
        source_ids: list[str],
        sources: list,
        source_map: dict[str, Any],
    ) -> str:
        def _format_source(src: Any) -> str:
            title = getattr(src, "title", "") or ""
            url = getattr(src, "url", "") or ""
            provider = getattr(src, "source", "") or ""
            parts = []
            if url:
                parts.append(f"url={url}")
            if title:
                parts.append(f"title={title}")
            if provider:
                parts.append(f"provider={provider}")
            return " | ".join(parts)

        if source_ids:
            labels = []
            for sid in source_ids:
                if sid in source_map:
                    labels.append(_format_source(source_map[sid]))
                else:
                    labels.append(sid)
            return ", ".join(labels)
        if sources:
            return _format_source(sources[0])
        return ""

    @staticmethod
    def _build_context_content(
        task: ResearchTask,
        agent_name: str,
        finding: Finding,
        entities: list[str],
        source_label: str,
        as_of: str,
    ) -> str:
        lines = [
            "[Context]",
            f"任务：{task.task_id}",
            f"预设：{task.preset_name}",
            f"代理：{agent_name}",
            f"类别：{finding.category.value if hasattr(finding.category, 'value') else finding.category or '未分类'}",
            f"实体：{', '.join(entities) if entities else '无'}",
            f"来源：{source_label or '未知'}",
            f"检索时间：{as_of}",
            "[/Context]",
            finding.summary,
        ]
        if finding.subcategory:
            lines.insert(5, f"子类别：{finding.subcategory}")
        if finding.evidence_basis:
            lines.append(f"证据依据：{finding.evidence_basis}")
        if finding.time_horizon:
            lines.append(f"时间范围：{finding.time_horizon}")
        direction_parts = []
        if finding.direction_for_aud:
            direction_parts.append(f"AUD={finding.direction_for_aud}")
        if finding.direction_for_cny:
            direction_parts.append(f"CNY={finding.direction_for_cny}")
        if finding.direction_for_pair:
            direction_parts.append(f"PAIR={finding.direction_for_pair}")
        if direction_parts:
            lines.append(f"方向拆分：{', '.join(direction_parts)}")
        return "\n".join(lines)

    # ── ContextPack 构建 ──────────────────────────────────────────────────────

    _SECTION_CATEGORY_HINTS: dict[str, list[str]] = {
        # Keep market_driver out of FX fact sections. Market drivers explain
        # why FX may move, so they belong to macro/market-driver analysis.
        # Including them here lets the first section consume them via global
        # dedup before "宏观信号" can use them.
        "汇率": ["fx_price"],
        "新闻": ["news_event", "geopolitical_event"],
        "宏观": ["policy_signal", "macro_indicator", "market_driver", "commodity_trade", "macro"],
        "风险": ["risk", "data_gap"],
        "估值": ["valuation_absolute", "valuation_relative"],
        "财务": ["revenue_quality", "margin_quality", "cash_flow", "balance_sheet"],
        "竞争": ["competitive_moat", "market_position"],
        "情绪": ["sentiment"],
        "监管": ["regulatory"],
        "催化": ["catalyst"],
        "rate": ["fx_price"],
        "news": ["news_event", "geopolitical_event"],
        "macro": ["policy_signal", "macro_indicator", "market_driver", "commodity_trade", "macro"],
        "risk": ["risk", "data_gap"],
    }

    # Categories that must NOT fall back to task_only when empty.
    # Rationale: task_only retrieval pulls unrelated chunks (macro, risk)
    # into the section, consuming them via global dedup and starving
    # downstream sections that actually need them.
    _NO_FALLBACK_CATEGORIES: frozenset[str] = frozenset({"news_event", "geopolitical_event"})

    @classmethod
    def _infer_categories(cls, section_title: str) -> list[str]:
        title_lower = section_title.lower()
        seen: set[str] = set()
        cats: list[str] = []
        for keyword, category_list in cls._SECTION_CATEGORY_HINTS.items():
            if keyword in title_lower:
                for cat in category_list:
                    if cat not in seen:
                        seen.add(cat)
                        cats.append(cat)
        return cats

    @staticmethod
    def _normalise_url(url: str) -> str:
        text = (url or "").strip()
        if not text:
            return ""
        parsed = urlparse(text if re.match(r"^https?://", text, re.I) else f"https://{text}")
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = re.sub(r"/+$", "", parsed.path or "")
        return f"{host}{path}".lower()

    @staticmethod
    def _source_identity(source: str | None) -> tuple[str, str, str]:
        """Return (url_key, title_domain_key, provider_label) for deduping.

        Provider-only labels such as google_news_rss are deliberately not used
        as evidence identity keys.

        When finding_key is present in the source string, it is appended to
        url_key so that structured agent findings sharing the same data-source
        URL are not incorrectly collapsed into one.
        """
        text = (source or "").strip()
        if not text:
            return "", "", ""

        url = ""
        m_url_field = re.search(r"\burl=([^|,;\s]+)", text, flags=re.IGNORECASE)
        if m_url_field:
            url = m_url_field.group(1).strip()
        else:
            m_url = re.search(r"https?://[^\s,;|)]+", text)
            if m_url:
                url = m_url.group(0).rstrip("。.,;")

        url_key = EvidenceStore._normalise_url(url)

        finding_key = ""
        m_fk = re.search(r"\bfinding_key=([^|,;\s]+)", text, flags=re.IGNORECASE)
        if m_fk:
            finding_key = m_fk.group(1).strip().lower()
        if url_key and finding_key:
            url_key = f"{url_key}#fk={finding_key}"

        domain = urlparse(url if re.match(r"^https?://", url or "", re.I) else f"https://{url}").netloc.lower() if url else ""
        if domain.startswith("www."):
            domain = domain[4:]

        title = ""
        m_title = re.search(r"\btitle=([^|]+)", text, flags=re.IGNORECASE)
        if m_title:
            title = m_title.group(1)
        title_key = re.sub(r"\s+", " ", title).strip().lower()
        title_domain_key = f"{domain}|{title_key}" if domain and title_key else ""

        provider = ""
        m_provider = re.search(r"\bprovider=([^|,;\s]+)", text, flags=re.IGNORECASE)
        if m_provider:
            provider = m_provider.group(1).strip().lower()
        elif text.lower() in _PROVIDER_ONLY_SOURCE_LABELS:
            provider = text.lower()

        return url_key, title_domain_key, provider

    def build_context_pack(
        self,
        task: ResearchTask,
        preset: ResearchPreset,
        outputs: list[AgentOutput],  # MVP 未使用；预留给后续嵌入向量/agent 权重扩展
        *,
        max_chunks_per_section: int = 5,
        token_budget: int = 7500,
        max_chunk_tokens: int = 1200,
        section_token_reserves: dict[str, int] | None = None,
        safe_user_context: SafeUserContext | None = None,
    ) -> ContextPack:
        total_chunks_in_db = self.count_chunks(task.task_id)
        all_items: list[ContextPackItem] = []
        seen_chunk_ids: set[str] = set()
        seen_urls: set[str] = set()
        traces: list[RetrievalTrace] = []
        tokens_used = 0

        use_scorer = compute_evidence_score is not None
        scoring_method = "composite" if use_scorer else "legacy"
        total_conflict_count = 0
        global_conflict_dedup_keys: set[tuple[str, str, str]] = set()
        scored_chunks: dict[str, EvidenceChunk] = {}

        # ── Pool-level conflict pre-scan ─────────────────────────────────────
        # Detect conflicts across ALL task findings before per-section selection.
        # This ensures cross-section conflicts (e.g. macro_rba vs macro_pboc
        # in different sections) are caught reliably.
        pool_conflicting_ids: set[str] = set()
        pool_conflict_summary: ConflictSummary | None = None
        if detect_conflicts is not None:
            try:
                all_task_findings = self._query_all_directed_findings(task.task_id)
                if len(all_task_findings) >= 2:
                    all_chunk_ids = []
                    for f in all_task_findings:
                        all_chunk_ids.extend(f.chunk_ids)
                    chunk_entities_pool: dict[str, list[str]] = {}
                    if all_chunk_ids:
                        for cid in all_chunk_ids:
                            c = self.get_chunk(cid)
                            if c and c.entities:
                                chunk_entities_pool[cid] = c.entities
                    pool_conflict_summary = detect_conflicts(
                        all_task_findings, chunk_entities=chunk_entities_pool,
                    )
                    pool_conflicting_ids = pool_conflict_summary.conflicting_chunk_ids
            except Exception:
                _log.warning("pool-level conflict detection failed", exc_info=True)

        # ── Phase 1: reserve best chunk per section ─────────────────────────
        # Pre-scan every section to find its top-1 candidate, then reserve
        # token budget so that later greedy fill cannot starve any section.
        section_prepared: list[dict[str, Any]] = []
        reserved_tokens = 0
        reserved_chunk_ids: set[str] = set()
        phase1_seen_urls: set[str] = set()

        for section_title in preset.report_sections:
            categories = self._infer_categories(section_title)
            candidates: list[EvidenceChunk] = []
            candidate_ids: set[str] = set()
            fallback_reason = ""
            filter_desc_parts: list[str] = [f"task_id={task.task_id}"]

            if categories:
                for cat in categories:
                    for c in self.query_chunks(
                        task.task_id,
                        category=cat,
                        top_k=max_chunks_per_section * 3,
                    ):
                        if c.chunk_id not in candidate_ids:
                            candidate_ids.add(c.chunk_id)
                            candidates.append(c)
                filter_desc_parts.append(f"categories={categories}")

            if not candidates:
                blocked = set(categories) & self._NO_FALLBACK_CATEGORIES if categories else set()
                if blocked:
                    fallback_reason = f"no_{'_'.join(sorted(blocked))}_evidence"
                    filter_desc_parts.append(f"fallback=blocked({fallback_reason})")
                else:
                    candidates = self.query_chunks(
                        task.task_id,
                        top_k=max_chunks_per_section * 3,
                    )
                    filter_desc_parts.append("fallback=task_only")

            pre_dedup_count = len(candidates)

            # Section-level reserve limit from config
            section_reserve = 0
            if section_token_reserves and categories:
                for cat in categories:
                    if cat in section_token_reserves:
                        section_reserve = section_token_reserves[cat]
                        break

            deduped: list[EvidenceChunk] = []
            local_keys: set[str] = set()
            for c in candidates:
                if c.chunk_id in reserved_chunk_ids:
                    continue
                if max_chunk_tokens and c.token_estimate > max_chunk_tokens:
                    continue
                url_key, title_domain_key, _provider = self._source_identity(c.source)
                if url_key and url_key in phase1_seen_urls:
                    continue
                local_key = url_key or title_domain_key
                if local_key and local_key in local_keys:
                    continue
                deduped.append(c)
                if local_key:
                    local_keys.add(local_key)

            # Score candidates to find the best one for reservation
            section_score_map: dict[str, float] = {}
            if use_scorer:
                try:
                    for c in deduped:
                        es = compute_evidence_score(c, safe_user_context)
                        section_score_map[c.chunk_id] = es.composite_score
                        c.composite_score = es.composite_score
                        c.attention_score = es.attention_score
                        c.score_importance = es.importance
                        c.score_confidence = es.confidence
                        c.score_recency = es.recency_score
                        c.score_source_quality = es.source_quality_score
                        c.score_user_relevance = es.user_relevance_score
                        c.score_conflict_value = es.conflict_value
                        c.score_reason = es.reason
                        scored_chunks[c.chunk_id] = c
                    deduped.sort(
                        key=lambda c: section_score_map.get(c.chunk_id, 0.0),
                        reverse=True,
                    )
                except Exception:
                    deduped.sort(
                        key=lambda c: (c.importance, c.confidence, c.created_at),
                        reverse=True,
                    )
            else:
                deduped.sort(
                    key=lambda c: (c.importance, c.confidence, c.created_at),
                    reverse=True,
                )

            best: EvidenceChunk | None = deduped[0] if deduped else None
            if best is not None:
                reserved_chunk_ids.add(best.chunk_id)
                reserved_tokens += best.token_estimate
                url_key_best, _, _ = self._source_identity(best.source)
                if url_key_best:
                    phase1_seen_urls.add(url_key_best)

            section_prepared.append({
                "section_title": section_title,
                "candidates_all": candidates,
                "pre_dedup_count": pre_dedup_count,
                "filter_desc_parts": filter_desc_parts,
                "fallback_reason": fallback_reason,
                "reserved_chunk": best,
                "section_reserve": section_reserve,
            })

        # ── Phase 2: greedy fill with reserved budget protection ─────────
        for sec in section_prepared:
            section_title = sec["section_title"]
            if tokens_used >= token_budget:
                break

            t0 = time.monotonic()
            candidates = sec["candidates_all"]
            pre_dedup_count = sec["pre_dedup_count"]
            filter_desc_parts = sec["filter_desc_parts"]
            fallback_reason = sec["fallback_reason"]
            reserved_chunk: EvidenceChunk | None = sec["reserved_chunk"]
            section_reserve: int = sec["section_reserve"]

            deduped: list[EvidenceChunk] = []
            local_keys: set[str] = set()
            for c in candidates:
                if c.chunk_id in seen_chunk_ids:
                    continue
                if max_chunk_tokens and c.token_estimate > max_chunk_tokens:
                    continue
                url_key, title_domain_key, _provider = self._source_identity(c.source)
                if url_key and url_key in seen_urls:
                    continue
                local_key = url_key or title_domain_key
                if local_key and local_key in local_keys:
                    continue
                deduped.append(c)
                if local_key:
                    local_keys.add(local_key)

            score_map: dict[str, float] = {}
            section_conflict_count = 0
            section_conflict_pairs: list[dict[str, Any]] = []
            boosted_chunk_ids: list[str] = []
            if use_scorer:
                try:
                    for c in deduped:
                        if c.chunk_id in scored_chunks:
                            sc = scored_chunks[c.chunk_id]
                            score_map[c.chunk_id] = sc.composite_score
                            c.composite_score = sc.composite_score
                            c.attention_score = sc.attention_score
                            c.score_importance = sc.score_importance
                            c.score_confidence = sc.score_confidence
                            c.score_recency = sc.score_recency
                            c.score_source_quality = sc.score_source_quality
                            c.score_user_relevance = sc.score_user_relevance
                            c.score_conflict_value = sc.score_conflict_value
                            c.score_reason = sc.score_reason
                        else:
                            es = compute_evidence_score(c, safe_user_context)
                            score_map[c.chunk_id] = es.composite_score
                            c.composite_score = es.composite_score
                            c.attention_score = es.attention_score
                            c.score_importance = es.importance
                            c.score_confidence = es.confidence
                            c.score_recency = es.recency_score
                            c.score_source_quality = es.source_quality_score
                            c.score_user_relevance = es.user_relevance_score
                            c.score_conflict_value = es.conflict_value
                            c.score_reason = es.reason
                            scored_chunks[c.chunk_id] = c

                    # Conflict detection: combine pool-level + section-level results
                    if detect_conflicts is not None and len(deduped) >= 1:
                        try:
                            # Pool-level conflicts for chunks in THIS section
                            section_cids = {c.chunk_id for c in deduped}
                            pool_hits_here = pool_conflicting_ids & section_cids

                            # Section-level detection (original logic)
                            section_summary: ConflictSummary | None = None
                            if len(deduped) >= 2:
                                candidate_chunk_ids = [c.chunk_id for c in deduped]
                                findings = self._query_findings_by_chunks(
                                    candidate_chunk_ids, task.task_id,
                                )
                                if len(findings) >= 2:
                                    chunk_entities: dict[str, list[str]] = {
                                        c.chunk_id: c.entities
                                        for c in deduped
                                        if c.entities
                                    }
                                    section_summary = detect_conflicts(
                                        findings, chunk_entities=chunk_entities,
                                    )

                            # Merge: use section-level if it found conflicts,
                            # otherwise fall back to pool-level for this section's chunks
                            if section_summary and section_summary.conflict_count > 0:
                                effective_summary = section_summary
                            elif pool_hits_here and pool_conflict_summary:
                                effective_summary = ConflictSummary(
                                    conflicts=[
                                        cp for cp in pool_conflict_summary.conflicts
                                        if cp.chunk_id_a in section_cids or cp.chunk_id_b in section_cids
                                    ],
                                    conflict_count=len(pool_hits_here),
                                    conflicting_chunk_ids=pool_hits_here,
                                )
                            else:
                                effective_summary = None

                            if effective_summary and effective_summary.conflict_count > 0:
                                # Deduplicate conflicts globally across sections
                                unique_in_section = 0
                                for cp in effective_summary.conflicts:
                                    dk = (
                                        *sorted([cp.finding_id_a, cp.finding_id_b]),
                                        cp.rule,
                                    )
                                    if dk not in global_conflict_dedup_keys:
                                        global_conflict_dedup_keys.add(dk)
                                        unique_in_section += 1
                                section_conflict_count = unique_in_section
                                total_conflict_count += unique_in_section

                                # Identify ineligible chunks (weak news: aggregator + low confidence)
                                ineligible_cids: set[str] = set()
                                for c in deduped:
                                    if (c.category == "news_event"
                                            and c.confidence < 0.3):
                                        ineligible_cids.add(c.chunk_id)
                                    elif (c.category == "news_event"
                                          and c.confidence < 0.5
                                          and "aggregator" in (c.source or "").lower()):
                                        ineligible_cids.add(c.chunk_id)

                                before_scores = dict(score_map)
                                apply_conflict_boost(
                                    score_map, effective_summary,
                                    boost=_CONFLICT_SELECTION_BOOST,
                                    ineligible_chunk_ids=ineligible_cids,
                                )
                                boosted_chunk_ids = sorted(
                                    cid for cid in effective_summary.conflicting_chunk_ids
                                    if score_map.get(cid, 0.0) > before_scores.get(cid, 0.0)
                                )
                                for c in deduped:
                                    if c.chunk_id in score_map:
                                        delta = max(
                                            0.0,
                                            score_map[c.chunk_id] - before_scores.get(c.chunk_id, 0.0),
                                        )
                                        c.composite_score = score_map[c.chunk_id]
                                        c.attention_score = score_map[c.chunk_id]
                                        if delta > 0:
                                            c.score_conflict_value = round(delta, 4)
                                            if "conflict_boost" not in c.score_reason:
                                                c.score_reason = (
                                                    f"{c.score_reason},conflict_boost"
                                                    if c.score_reason else "conflict_boost"
                                                )
                                        scored_chunks[c.chunk_id] = c
                                section_conflict_pairs = [cp.to_dict() for cp in effective_summary.conflicts]
                        except Exception:
                            _log.warning(
                                "conflict_detector failed; using scored sort",
                                exc_info=True,
                            )

                    deduped.sort(
                        key=lambda c: score_map.get(c.chunk_id, 0.0),
                        reverse=True,
                    )
                except Exception:
                    _log.warning("evidence_scorer failed; using legacy sort", exc_info=True)
                    scoring_method = "legacy"
                    score_map.clear()
                    for c in deduped:
                        c.composite_score = 0.0
                        c.attention_score = 0.0
                        c.score_importance = 0.0
                        c.score_confidence = 0.0
                        c.score_recency = 0.0
                        c.score_source_quality = 0.0
                        c.score_user_relevance = 0.0
                        c.score_conflict_value = 0.0
                        c.score_reason = ""
                    deduped.sort(
                        key=lambda c: (c.importance, c.confidence, c.created_at),
                        reverse=True,
                    )
            else:
                deduped.sort(
                    key=lambda c: (c.importance, c.confidence, c.created_at),
                    reverse=True,
                )

            # Compute tokens reserved by OTHER sections' reserved chunks
            # (not yet selected), so we don't steal their budget.
            other_reserved_tokens = 0
            for other_sec in section_prepared:
                if other_sec["section_title"] == section_title:
                    continue
                rc: EvidenceChunk | None = other_sec["reserved_chunk"]
                if rc is not None and rc.chunk_id not in seen_chunk_ids:
                    other_reserved_tokens += rc.token_estimate

            section_items: list[ContextPackItem] = []
            skipped_over_budget = 0

            # Ensure reserved chunk is selected first if available
            if reserved_chunk is not None and reserved_chunk.chunk_id not in seen_chunk_ids:
                rc = reserved_chunk
                if tokens_used + rc.token_estimate <= token_budget:
                    rc.used_in_brief = True
                    cs = score_map.get(rc.chunk_id, rc.importance)
                    item = ContextPackItem(
                        chunk_id=rc.chunk_id,
                        agent_name=rc.agent_name,
                        text=rc.content,
                        relevance_score=cs,
                        token_estimate=rc.token_estimate,
                        composite_score=rc.composite_score,
                        attention_score=rc.attention_score,
                    )
                    section_items.append(item)
                    seen_chunk_ids.add(rc.chunk_id)
                    url_key_rc, _, _ = self._source_identity(rc.source)
                    if url_key_rc:
                        seen_urls.add(url_key_rc)
                    tokens_used += rc.token_estimate

            section_tokens_used = sum(it.token_estimate for it in section_items)
            for c in deduped:
                if len(section_items) >= max_chunks_per_section:
                    break
                if c.chunk_id in seen_chunk_ids:
                    continue
                available = token_budget - tokens_used - other_reserved_tokens
                if c.token_estimate > available:
                    skipped_over_budget += 1
                    continue
                if section_reserve and section_tokens_used + c.token_estimate > section_reserve:
                    skipped_over_budget += 1
                    continue
                c.used_in_brief = True
                cs = score_map.get(c.chunk_id, c.importance)
                item = ContextPackItem(
                    chunk_id=c.chunk_id,
                    agent_name=c.agent_name,
                    text=c.content,
                    relevance_score=cs,
                    token_estimate=c.token_estimate,
                    composite_score=c.composite_score,
                    attention_score=c.attention_score,
                )
                section_items.append(item)
                seen_chunk_ids.add(c.chunk_id)
                url_key, _title_domain_key, _provider = self._source_identity(c.source)
                if url_key:
                    seen_urls.add(url_key)
                tokens_used += c.token_estimate
                section_tokens_used += c.token_estimate

            all_items.extend(section_items)

            latency = int((time.monotonic() - t0) * 1000)
            top_scores = [
                round(item.relevance_score, 4)
                for item in section_items
            ]
            noise_rate = round(
                1.0 - len(section_items) / pre_dedup_count, 4,
            ) if pre_dedup_count > 0 else 0.0

            selected_ids = [it.chunk_id for it in section_items]
            score_values = [score_map.get(c.chunk_id, c.importance) for c in deduped]
            score_distribution = {
                "count": len(score_values),
                "min": round(min(score_values), 4) if score_values else 0.0,
                "max": round(max(score_values), 4) if score_values else 0.0,
                "avg": round(sum(score_values) / len(score_values), 4) if score_values else 0.0,
            }
            query_desc = (
                f"section={section_title} "
                f"filters=[{', '.join(filter_desc_parts)}] "
                f"pre_dedup={pre_dedup_count} post_dedup={len(deduped)} "
                f"selected={len(section_items)} noise_rate={noise_rate} "
                f"skipped_over_budget={skipped_over_budget} "
                f"scoring={scoring_method} "
                f"conflicts={section_conflict_count} "
                f"boosted={len(boosted_chunk_ids)}"
            )
            trace = RetrievalTrace(
                query=query_desc,
                retrieved_count=len(section_items),
                total_chunks=total_chunks_in_db,
                top_scores=[s for s in top_scores if 0.0 <= s <= 1.0],
                latency_ms=latency,
                section_title=section_title,
                selected_chunk_ids=selected_ids,
                section_covered=bool(section_items),
                score_distribution=score_distribution,
                conflict_count=section_conflict_count,
                conflict_pairs=section_conflict_pairs,
                boosted_chunk_ids=boosted_chunk_ids,
                scoring_method=scoring_method,
                fallback_reason=fallback_reason,
            )
            self.insert_trace(trace, task_id=task.task_id)
            traces.append(trace)

        coverage: dict[str, int] = {}
        for item in all_items:
            coverage[item.agent_name] = coverage.get(item.agent_name, 0) + 1

        selected_item_ids = [item.chunk_id for item in all_items]
        if selected_item_ids:
            try:
                self.mark_used_in_brief(selected_item_ids)
            except Exception:
                _log.warning(
                    "used_in_brief persist failed; context pack remains usable",
                    exc_info=True,
                )

        if scoring_method == "composite" and scored_chunks:
            try:
                for chunk in scored_chunks.values():
                    self._conn.execute(
                        "UPDATE evidence_chunks "
                        "SET composite_score = ?, attention_score = ?, "
                        "score_importance = ?, score_confidence = ?, "
                        "score_recency = ?, score_source_quality = ?, "
                        "score_user_relevance = ?, score_conflict_value = ?, "
                        "score_reason = ? "
                        "WHERE chunk_id = ?",
                        (
                            chunk.composite_score,
                            chunk.attention_score,
                            chunk.score_importance,
                            chunk.score_confidence,
                            chunk.score_recency,
                            chunk.score_source_quality,
                            chunk.score_user_relevance,
                            chunk.score_conflict_value,
                            chunk.score_reason,
                            chunk.chunk_id,
                        ),
                    )
                self._conn.commit()
            except Exception:
                _log.warning("score persist failed; scores are pack-local only",
                             exc_info=True)

        sections_total = len(preset.report_sections)
        sections_covered = sum(1 for t in traces if t.retrieved_count > 0)
        _log.debug(
            "context_pack_summary: scoring=%s retrieved_total=%d selected=%d "
            "used=%d section_coverage=%d/%d tokens=%d/%d conflicts=%d",
            scoring_method,
            total_chunks_in_db,
            len(all_items),
            sum(1 for it in all_items if it.token_estimate > 0),
            sections_covered,
            sections_total,
            tokens_used,
            token_budget,
            total_conflict_count,
        )

        return ContextPack(
            items=all_items,
            total_tokens=tokens_used,
            budget_tokens=token_budget,
            coverage=coverage,
        )

    def _query_findings_by_chunks(
        self, chunk_ids: list[str], task_id: str,
    ) -> list[EvidenceFinding]:
        """Return findings whose chunk_ids overlap with the given list."""
        if not chunk_ids:
            return []
        target = set(chunk_ids)
        rows = self._conn.execute(
            "SELECT * FROM evidence_findings "
            "WHERE task_id = ? AND direction IS NOT NULL",
            (task_id,),
        ).fetchall()
        results: list[EvidenceFinding] = []
        for r in rows:
            f = self._row_to_finding(r)
            if target & set(f.chunk_ids):
                results.append(f)
        return results

    def _query_all_directed_findings(self, task_id: str) -> list[EvidenceFinding]:
        """Return ALL findings with non-null direction for the given task."""
        rows = self._conn.execute(
            "SELECT * FROM evidence_findings "
            "WHERE task_id = ? AND direction IS NOT NULL "
            "AND direction NOT IN ('neutral', 'mixed', 'unknown')",
            (task_id,),
        ).fetchall()
        return [self._row_to_finding(r) for r in rows]

    # ── 清理 ─────────────────────────────────────────────────────────────────

    def delete_task(self, task_id: str) -> int:
        """删除指定任务的所有证据数据，返回删除的 chunk 数。"""
        count = self.count_chunks(task_id)
        self._conn.execute(
            "DELETE FROM evidence_chunks WHERE task_id = ?", (task_id,)
        )
        self._conn.execute(
            "DELETE FROM evidence_findings WHERE task_id = ?", (task_id,)
        )
        self._conn.execute(
            "DELETE FROM citation_refs WHERE task_id = ?", (task_id,)
        )
        self._conn.execute(
            "DELETE FROM retrieval_traces WHERE task_id = ?", (task_id,)
        )
        self._conn.commit()
        return count

    # ── 内部转换 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> EvidenceChunk:
        return EvidenceChunk(
            chunk_id=row["chunk_id"],
            task_id=row["task_id"],
            preset_name=row["preset_name"],
            agent_name=row["agent_name"],
            content=row["content"],
            source=row["source"],
            category=row["category"],
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            entities=json.loads(row["entities_json"]),
            used_in_brief=bool(row["used_in_brief"]),
            created_at=row["created_at"],
            ttl_policy=row["ttl_policy"],
            token_estimate=int(row["token_estimate"]),
            attention_score=float(row["attention_score"]),
            composite_score=float(row["composite_score"]),
            score_importance=float(row["score_importance"]),
            score_confidence=float(row["score_confidence"]),
            score_recency=float(row["score_recency"]),
            score_source_quality=float(row["score_source_quality"]),
            score_user_relevance=float(row["score_user_relevance"]),
            score_conflict_value=float(row["score_conflict_value"]),
            score_reason=row["score_reason"],
            source_metadata_json=row["source_metadata_json"] if "source_metadata_json" in row.keys() else "{}",
        )

    @staticmethod
    def _row_to_finding(row: sqlite3.Row) -> EvidenceFinding:
        return EvidenceFinding(
            finding_id=row["finding_id"],
            agent_name=row["agent_name"],
            key=row["key"],
            summary=row["summary"],
            direction=row["direction"],
            chunk_ids=json.loads(row["chunk_ids_json"]),
            evidence_score=row["evidence_score"],
            category=row["category"],
            importance=float(row["importance"]),
        )

    @staticmethod
    def _row_to_citation(row: sqlite3.Row) -> CitationRef:
        return CitationRef(
            citation_id=row["citation_id"],
            chunk_id=row["chunk_id"],
            finding_id=row["finding_id"],
            section_title=row["section_title"],
            relevance_score=float(row["relevance_score"]),
        )

    @staticmethod
    def _row_to_trace(row: sqlite3.Row) -> RetrievalTrace:
        def _json_col(name: str, default: Any) -> Any:
            try:
                raw = row[name]
            except (KeyError, IndexError):
                return default
            if raw in (None, ""):
                return default
            try:
                return json.loads(raw)
            except Exception:
                return default

        def _col(name: str, default: Any) -> Any:
            try:
                return row[name]
            except (KeyError, IndexError):
                return default

        return RetrievalTrace(
            trace_id=row["trace_id"],
            query=row["query"],
            retrieved_count=int(row["retrieved_count"]),
            total_chunks=int(row["total_chunks"]),
            top_scores=json.loads(row["top_scores_json"]),
            latency_ms=int(row["latency_ms"]),
            timestamp=row["timestamp"],
            section_title=_col("section_title", ""),
            selected_chunk_ids=list(_json_col("selected_chunk_ids_json", [])),
            section_covered=bool(_col("section_covered", 0)),
            score_distribution=dict(_json_col("score_distribution_json", {})),
            conflict_count=int(_col("conflict_count", 0)),
            conflict_pairs=list(_json_col("conflict_pairs_json", [])),
            boosted_chunk_ids=list(_json_col("boosted_chunk_ids_json", [])),
            scoring_method=_col("scoring_method", ""),
            fallback_reason=_col("fallback_reason", ""),
        )
