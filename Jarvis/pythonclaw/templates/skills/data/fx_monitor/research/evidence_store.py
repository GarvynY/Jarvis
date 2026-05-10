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
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import time

_log = logging.getLogger(__name__)

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
    now_iso,
)

_SCHEMA_VERSION = 1

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
    token_estimate INTEGER NOT NULL DEFAULT 0
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
    timestamp       TEXT NOT NULL DEFAULT ''
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
        if row is None:
            cur.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        self._conn.commit()

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
                used_in_brief, created_at, ttl_policy, token_estimate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                top_scores_json, latency_ms, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                trace.trace_id,
                task_id,
                trace.query,
                trace.retrieved_count,
                trace.total_chunks,
                json.dumps(trace.top_scores),
                trace.latency_ms,
                trace.timestamp,
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
        "fx_agent":    "fx_price",
        "news_agent":  "news_event",
        "macro_agent": "macro",
        "risk_agent":  "risk",
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
        source_map: dict[str, str] = {}
        for out in outputs:
            for src in out.sources:
                source_map[src.url] = src.source

        enriched: list[AgentOutput] = []

        for output in outputs:
            chunk_ids: list[str] = []
            finding_ids: list[str] = []

            for finding in output.findings:
                source_label = self._resolve_source(
                    finding.source_ids, output.sources, source_map,
                )
                entities = list(task.focus_assets) if task.focus_assets else []

                content = self._build_context_content(
                    task=task,
                    agent_name=output.agent_name,
                    finding=finding,
                    entities=entities,
                    source_label=source_label,
                    as_of=output.as_of,
                )

                category = finding.category or self._AGENT_CATEGORY_DEFAULTS.get(
                    output.agent_name, "",
                )
                importance = finding.importance if finding.importance > 0 else output.confidence

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
        source_map: dict[str, str],
    ) -> str:
        if source_ids:
            labels = []
            for sid in source_ids:
                if sid in source_map:
                    labels.append(source_map[sid])
                else:
                    labels.append(sid)
            return ", ".join(labels)
        if sources:
            return sources[0].source
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
            f"类别：{finding.category or '未分类'}",
            f"实体：{', '.join(entities) if entities else '无'}",
            f"来源：{source_label or '未知'}",
            f"检索时间：{as_of}",
            "[/Context]",
            finding.summary,
        ]
        return "\n".join(lines)

    # ── ContextPack 构建 ──────────────────────────────────────────────────────

    _SECTION_CATEGORY_HINTS: dict[str, list[str]] = {
        "汇率": ["fx_price"],
        "新闻": ["news_event"],
        "宏观": ["macro"],
        "风险": ["risk"],
        "估值": ["valuation_absolute", "valuation_relative"],
        "财务": ["revenue_quality", "margin_quality", "cash_flow", "balance_sheet"],
        "竞争": ["competitive_moat", "market_position"],
        "情绪": ["sentiment"],
        "监管": ["regulatory"],
        "催化": ["catalyst"],
        "rate": ["fx_price"],
        "news": ["news_event"],
        "macro": ["macro"],
        "risk": ["risk"],
    }

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

    def build_context_pack(
        self,
        task: ResearchTask,
        preset: ResearchPreset,
        outputs: list[AgentOutput],  # MVP 未使用；预留给后续嵌入向量/agent 权重扩展
        *,
        max_chunks_per_section: int = 5,
        token_budget: int = 4000,
    ) -> ContextPack:
        total_chunks_in_db = self.count_chunks(task.task_id)
        all_items: list[ContextPackItem] = []
        seen_chunk_ids: set[str] = set()
        seen_sources: set[str] = set()
        traces: list[RetrievalTrace] = []
        tokens_used = 0

        for section_title in preset.report_sections:
            if tokens_used >= token_budget:
                break

            t0 = time.monotonic()
            categories = self._infer_categories(section_title)

            candidates: list[EvidenceChunk] = []
            candidate_ids: set[str] = set()
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
            pre_dedup_count = len(candidates)

            if not candidates:
                candidates = self.query_chunks(
                    task.task_id,
                    top_k=max_chunks_per_section * 3,
                )
                filter_desc_parts.append("fallback=task_only")
                pre_dedup_count = len(candidates)

            deduped: list[EvidenceChunk] = []
            local_sources: set[str] = set()
            for c in candidates:
                if c.chunk_id in seen_chunk_ids:
                    continue
                source_key = c.source or ""
                if source_key and (source_key in seen_sources or source_key in local_sources):
                    continue
                deduped.append(c)
                if source_key:
                    local_sources.add(source_key)

            deduped.sort(
                key=lambda c: (c.importance, c.confidence, c.created_at),
                reverse=True,
            )

            section_items: list[ContextPackItem] = []
            for c in deduped[:max_chunks_per_section]:
                if tokens_used + c.token_estimate > token_budget:
                    break
                item = ContextPackItem(
                    chunk_id=c.chunk_id,
                    agent_name=c.agent_name,
                    text=c.content,
                    relevance_score=c.importance,
                    token_estimate=c.token_estimate,
                )
                section_items.append(item)
                seen_chunk_ids.add(c.chunk_id)
                if c.source:
                    seen_sources.add(c.source)
                tokens_used += c.token_estimate

            all_items.extend(section_items)

            latency = int((time.monotonic() - t0) * 1000)
            top_scores = [round(c.importance, 4) for c in deduped[:max_chunks_per_section]]
            noise_rate = round(
                1.0 - len(section_items) / pre_dedup_count, 4,
            ) if pre_dedup_count > 0 else 0.0

            selected_ids = [it.chunk_id for it in section_items]
            query_desc = (
                f"section={section_title} "
                f"filters=[{', '.join(filter_desc_parts)}] "
                f"pre_dedup={pre_dedup_count} post_dedup={len(deduped)} "
                f"selected={len(section_items)} noise_rate={noise_rate} "
                f"chunk_ids={selected_ids}"
            )
            trace = RetrievalTrace(
                query=query_desc,
                retrieved_count=len(section_items),
                total_chunks=total_chunks_in_db,
                top_scores=[s for s in top_scores if 0.0 <= s <= 1.0],
                latency_ms=latency,
            )
            self.insert_trace(trace, task_id=task.task_id)
            traces.append(trace)

        coverage: dict[str, int] = {}
        for item in all_items:
            coverage[item.agent_name] = coverage.get(item.agent_name, 0) + 1

        sections_total = len(preset.report_sections)
        sections_covered = sum(1 for t in traces if t.retrieved_count > 0)
        _log.debug(
            "context_pack_summary: retrieved_total=%d selected=%d "
            "used=%d section_coverage=%d/%d tokens=%d/%d",
            total_chunks_in_db,
            len(all_items),
            sum(1 for it in all_items if it.token_estimate > 0),
            sections_covered,
            sections_total,
            tokens_used,
            token_budget,
        )

        return ContextPack(
            items=all_items,
            total_tokens=tokens_used,
            budget_tokens=token_budget,
            coverage=coverage,
        )

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
        return RetrievalTrace(
            trace_id=row["trace_id"],
            query=row["query"],
            retrieved_count=int(row["retrieved_count"]),
            total_chunks=int(row["total_chunks"]),
            top_scores=json.loads(row["top_scores_json"]),
            latency_ms=int(row["latency_ms"]),
            timestamp=row["timestamp"],
        )
