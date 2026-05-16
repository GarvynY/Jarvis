"""Privacy-conscious FX research run metrics and optional baselines.

Default behavior is intentionally lightweight:
  - one structured metrics row per successful run
  - no prompt, final report, evidence text, raw user id, or full source URL

Detailed baseline snapshots are opt-in and still sanitized. They are meant for
quality checkpoints, not for every production request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from pythonclaw import config
except Exception:  # pragma: no cover - standalone research tests
    config = None  # type: ignore[assignment]

try:
    from evidence_store import EvidenceStore
except ImportError:  # pragma: no cover
    from .evidence_store import EvidenceStore  # type: ignore[no-redef]

_log = logging.getLogger(__name__)

_METRICS_SCHEMA_VERSION = 1
_DEFAULT_METRICS_RETENTION_DAYS = 90
_DEFAULT_BASELINE_RETENTION_DAYS = 365
_LOW_SOURCE_QUALITY_THRESHOLD = 0.5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _base_dir() -> Path:
    if config is not None:
        base = getattr(config, "PYTHONCLAW_HOME", None)
        if base:
            return Path(base) / "context" / "baselines" / "fx_research"
    return Path.home() / ".pythonclaw" / "context" / "baselines" / "fx_research"


def _default_db_path() -> Path:
    return _base_dir() / "fx_research_metrics.sqlite3"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _get_or_create_salt(base: Path) -> bytes:
    base.mkdir(parents=True, exist_ok=True)
    salt_path = base / ".metrics_salt"
    try:
        if salt_path.exists():
            value = salt_path.read_text(encoding="utf-8").strip()
            if value:
                return value.encode("utf-8")
        value = secrets.token_hex(32)
        salt_path.write_text(value, encoding="utf-8")
        try:
            salt_path.chmod(0o600)
        except Exception:
            pass
        return value.encode("utf-8")
    except Exception:
        _log.warning("fx metrics salt unavailable; using process-local salt", exc_info=True)
        return secrets.token_hex(32).encode("utf-8")


def _hash_user_id(user_id: str | int | None, base: Path) -> str:
    if user_id is None or str(user_id) == "":
        return ""
    salt = _get_or_create_salt(base)
    digest = hashlib.sha256(salt + str(user_id).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:24]}"


def _safe_enum(value: Any, *, max_len: int = 80) -> str:
    text = str(value or "").strip()
    return text[:max_len]


def _source_domain(source: Any) -> str:
    text = str(source or "").strip()
    if not text:
        return ""
    if text in {"google_news_rss", "tavily", "web_search", "fetch_rate.py", "yfinance"}:
        return text
    candidate = text
    if "url=" in text:
        for part in text.split("|"):
            part = part.strip()
            if part.startswith("url="):
                candidate = part[4:].strip()
                break
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host[:120]


def _round(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class BaselineRecordResult:
    metrics_written: bool = False
    baseline_written: bool = False
    metrics_db_path: str = ""
    baseline_path: str = ""
    error: str = ""


class FxResearchMetricsStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "FxResearchMetricsStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fx_research_run_metrics (
                task_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                preset_name TEXT NOT NULL DEFAULT '',
                trigger TEXT NOT NULL DEFAULT '',
                user_key TEXT NOT NULL DEFAULT '',
                latency_s REAL NOT NULL DEFAULT 0.0,
                agent_statuses_json TEXT NOT NULL DEFAULT '{}',
                agent_metrics_json TEXT NOT NULL DEFAULT '{}',
                funnel_json TEXT NOT NULL DEFAULT '{}',
                quality_metrics_json TEXT NOT NULL DEFAULT '{}',
                llm_metrics_json TEXT NOT NULL DEFAULT '{}',
                source_summary_json TEXT NOT NULL DEFAULT '{}',
                selected_chunk_metrics_json TEXT NOT NULL DEFAULT '[]',
                followup_summary_json TEXT NOT NULL DEFAULT '{}',
                privacy_policy_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_fx_run_metrics_created
                ON fx_research_run_metrics(created_at);
            CREATE TABLE IF NOT EXISTS fx_research_baseline_snapshots (
                task_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                baseline_path TEXT NOT NULL DEFAULT '',
                snapshot_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_fx_baseline_created
                ON fx_research_baseline_snapshots(created_at);
            CREATE TABLE IF NOT EXISTS fx_research_metrics_schema (
                version INTEGER NOT NULL
            );
            """
        )
        row = self._conn.execute(
            "SELECT version FROM fx_research_metrics_schema ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO fx_research_metrics_schema(version) VALUES (?)",
                (_METRICS_SCHEMA_VERSION,),
            )
        self._conn.commit()

    def upsert_run_metrics(self, metrics: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO fx_research_run_metrics (
                task_id, created_at, preset_name, trigger, user_key, latency_s,
                agent_statuses_json, agent_metrics_json, funnel_json,
                quality_metrics_json, llm_metrics_json, source_summary_json,
                selected_chunk_metrics_json, followup_summary_json,
                privacy_policy_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                metrics["task_id"],
                metrics["created_at"],
                metrics["preset_name"],
                metrics["trigger"],
                metrics["user_key"],
                metrics["latency_s"],
                _json_dumps(metrics["agent_statuses"]),
                _json_dumps(metrics["agent_metrics"]),
                _json_dumps(metrics["funnel"]),
                _json_dumps(metrics["quality_metrics"]),
                _json_dumps(metrics["llm_metrics"]),
                _json_dumps(metrics["source_summary"]),
                _json_dumps(metrics["selected_chunk_metrics"]),
                _json_dumps(metrics["followup_summary"]),
                _json_dumps(metrics["privacy_policy"]),
            ),
        )
        self._conn.commit()

    def insert_baseline_snapshot(self, task_id: str, path: Path, snapshot: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO fx_research_baseline_snapshots
                (task_id, created_at, baseline_path, snapshot_json)
            VALUES (?,?,?,?)
            """,
            (task_id, snapshot["created_at"], str(path), _json_dumps(snapshot)),
        )
        self._conn.commit()

    def prune(self, *, metrics_days: int, baseline_days: int) -> None:
        now = datetime.now(timezone.utc)
        if metrics_days > 0:
            cutoff = (now - timedelta(days=metrics_days)).isoformat(timespec="seconds")
            self._conn.execute(
                "DELETE FROM fx_research_run_metrics WHERE created_at < ?", (cutoff,)
            )
        if baseline_days > 0:
            cutoff = (now - timedelta(days=baseline_days)).isoformat(timespec="seconds")
            old_paths = [
                row["baseline_path"]
                for row in self._conn.execute(
                    "SELECT baseline_path FROM fx_research_baseline_snapshots WHERE created_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            self._conn.execute(
                "DELETE FROM fx_research_baseline_snapshots WHERE created_at < ?", (cutoff,)
            )
            for raw_path in old_paths:
                try:
                    path = Path(raw_path)
                    if path.exists() and path.parent == self.db_path.parent:
                        path.unlink()
                except Exception:
                    _log.warning("failed to remove old fx baseline snapshot", exc_info=True)
        self._conn.commit()


def _load_evidence_rows(task_id: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[Any]]:
    try:
        with EvidenceStore() as store:
            chunks = store._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM evidence_chunks WHERE task_id = ? ORDER BY agent_name, created_at",
                (task_id,),
            ).fetchall()
            findings = store._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM evidence_findings WHERE task_id = ? ORDER BY agent_name, key",
                (task_id,),
            ).fetchall()
            traces = store.list_traces(task_id)
            return chunks, findings, traces
    except Exception:
        _log.warning("failed to load evidence rows for fx metrics", exc_info=True)
        return [], [], []


def build_run_metrics(
    *,
    task: Any,
    preset: Any,
    outputs: list[Any],
    brief: Any,
    latency_s: float,
    trigger: str,
    user_id: str | int | None = None,
    followup_requests: list[Any] | None = None,
) -> dict[str, Any]:
    base = _base_dir()
    task_id = str(getattr(task, "task_id", "") or getattr(brief, "task_id", ""))
    chunks, findings, traces = _load_evidence_rows(task_id)

    selected_ids: list[str] = []
    for trace in traces:
        for cid in getattr(trace, "selected_chunk_ids", []) or []:
            if cid not in selected_ids:
                selected_ids.append(cid)

    scored_chunks = [row for row in chunks if _round(row["composite_score"]) > 0]
    used_chunks = [row for row in chunks if int(row["used_in_brief"] or 0) == 1]
    source_scores = [_round(row["score_source_quality"]) for row in chunks if _round(row["score_source_quality"]) > 0]
    composite_scores = [_round(row["composite_score"]) for row in chunks if _round(row["composite_score"]) > 0]
    domains: dict[str, int] = {}
    provider_only = 0
    for row in chunks:
        domain = _source_domain(row["source"])
        if domain:
            domains[domain] = domains.get(domain, 0) + 1
            if domain in {"google_news_rss", "tavily", "web_search"}:
                provider_only += 1

    selected_chunk_metrics: list[dict[str, Any]] = []
    chunk_by_id = {row["chunk_id"]: row for row in chunks}
    for cid in selected_ids:
        row = chunk_by_id.get(cid)
        if row is None:
            continue
        selected_chunk_metrics.append({
            "chunk_id": cid,
            "agent_name": row["agent_name"],
            "category": row["category"],
            "source_domain": _source_domain(row["source"]),
            "used_in_brief": bool(row["used_in_brief"]),
            "composite_score": _round(row["composite_score"]),
            "attention_score": _round(row["attention_score"]),
            "source_quality": _round(row["score_source_quality"]),
            "score_reason": _safe_enum(row["score_reason"], max_len=160),
            "token_estimate": int(row["token_estimate"] or 0),
        })

    agent_metrics = {}
    for out in outputs:
        token_usage = getattr(out, "token_usage", {}) or {}
        agent_metrics[getattr(out, "agent_name", "unknown")] = {
            "status": getattr(out, "status", ""),
            "confidence": _round(getattr(out, "confidence", 0.0)),
            "finding_count": len(getattr(out, "findings", []) or []),
            "source_count": len(getattr(out, "sources", []) or []),
            "evidence_count": int(getattr(out, "evidence_count", 0) or 0),
            "missing_data_count": len(getattr(out, "missing_data", []) or []),
            "latency_ms": int(getattr(out, "latency_ms", 0) or 0),
            "prompt_tokens": int(token_usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(token_usage.get("completion_tokens", 0) or 0),
        }

    trace_count = len(traces)
    covered = sum(
        1 for trace in traces
        if getattr(trace, "section_covered", False)
        or int(getattr(trace, "retrieved_count", 0) or 0) > 0
    )
    fallback_count = sum(1 for trace in traces if getattr(trace, "fallback_reason", ""))
    # Deduplicate conflict pairs across sections for reportable count
    _seen_conflict_keys: set[tuple[str, str, str]] = set()
    raw_conflict_count = sum(int(getattr(trace, "conflict_count", 0) or 0) for trace in traces)
    for trace in traces:
        for cp in (getattr(trace, "conflict_pairs", None) or []):
            if isinstance(cp, dict):
                dk = tuple(sorted([cp.get("finding_id_a", ""), cp.get("finding_id_b", "")])) + (cp.get("rule", ""),)
            else:
                dk = tuple(sorted([getattr(cp, "finding_id_a", ""), getattr(cp, "finding_id_b", "")])) + (getattr(cp, "rule", ""),)
            _seen_conflict_keys.add(dk)
    conflict_count = len(_seen_conflict_keys) if _seen_conflict_keys else raw_conflict_count
    boosted_ids: set[str] = set()
    for trace in traces:
        boosted_ids.update(getattr(trace, "boosted_chunk_ids", []) or [])

    cost = getattr(brief, "cost_estimate", None)
    cost_tokens = int(getattr(cost, "estimated_tokens", 0) or 0)
    llm_metrics = {
        "llm_calls": int(getattr(cost, "llm_calls", 0) or 0),
        "estimated_tokens": cost_tokens,
        "estimated_cost_usd": _round(getattr(cost, "estimated_cost_usd", 0.0), 6),
        "total_latency_ms": int(getattr(cost, "total_latency_ms", 0) or 0),
        "prompt_path": "context_pack" if traces else "agent_output_or_fallback",
        "supervisor_prompt_tokens": None,
        "supervisor_completion_tokens": None,
        "token_detail_source": "cost_estimate_total_only",
    }

    followups = followup_requests or []
    followup_summary = {
        "count": len(followups),
        "execution_enabled": False,
        "items": [
            {
                "trigger_type": _safe_enum(getattr(req, "trigger_type", "")),
                "target_agent": _safe_enum(getattr(req, "target_agent", "")),
                "target_category": _safe_enum(getattr(req, "target_category", "")),
                "priority": _round(getattr(req, "priority", 0.0)),
            }
            for req in followups[:8]
        ],
    }

    return {
        "task_id": task_id,
        "created_at": _now_iso(),
        "preset_name": _safe_enum(getattr(task, "preset_name", "") or getattr(preset, "name", "")),
        "trigger": _safe_enum(trigger),
        "user_key": _hash_user_id(user_id, base),
        "latency_s": _round(latency_s, 2),
        "agent_statuses": dict(getattr(brief, "agent_statuses", {}) or {}),
        "agent_metrics": agent_metrics,
        "funnel": {
            "findings": len(findings),
            "chunks": len(chunks),
            "scored_chunks": len(scored_chunks),
            "selected_chunks": len(selected_ids),
            "used_in_brief": len(used_chunks),
            "unscored_chunks": max(0, len(chunks) - len(scored_chunks)),
        },
        "quality_metrics": {
            "avg_composite": _round(sum(composite_scores) / len(composite_scores)) if composite_scores else 0.0,
            "avg_source_quality": _round(sum(source_scores) / len(source_scores)) if source_scores else 0.0,
            "low_source_quality_ratio": _round(
                sum(1 for s in source_scores if s < _LOW_SOURCE_QUALITY_THRESHOLD) / len(source_scores)
            ) if source_scores else 0.0,
            "section_coverage": covered,
            "section_total": trace_count,
            "fallback_count": fallback_count,
            "conflict_count": conflict_count,
            "boosted_chunk_count": len(boosted_ids),
            "data_gaps_present": bool(getattr(brief, "data_gaps", "")),
        },
        "llm_metrics": llm_metrics,
        "source_summary": {
            "domain_counts": dict(sorted(domains.items())),
            "unique_domain_count": len(domains),
            "provider_only_count": provider_only,
        },
        "selected_chunk_metrics": selected_chunk_metrics,
        "followup_summary": followup_summary,
        "privacy_policy": {
            "raw_user_id_stored": False,
            "user_key": "salted_sha256_24hex",
            "prompt_text_stored": False,
            "response_text_stored": False,
            "final_report_text_stored": False,
            "evidence_text_stored": False,
            "full_source_url_stored": False,
            "source_domain_stored": True,
        },
    }


def build_sanitized_baseline_snapshot(metrics: dict[str, Any], *, phase10: dict[str, Any] | None = None) -> dict[str, Any]:
    phase10 = dict(phase10 or {})
    sanitized_phase10 = {
        "ranking_basis": phase10.get("ranking_basis", ""),
        "scoring_methods": phase10.get("scoring_methods", []),
        "score_summary": phase10.get("score_summary", {}),
        "conflicts": phase10.get("conflicts", {}),
        "retrieval_traces": phase10.get("retrieval_traces", []),
    }
    return {
        "schema": "fx_research_baseline_snapshot.v1",
        "created_at": metrics["created_at"],
        "task_id": metrics["task_id"],
        "run_metrics": metrics,
        "phase10_sanitized": sanitized_phase10,
        "privacy_note": (
            "Sanitized snapshot: no raw user id, prompt text, LLM response text, "
            "final report text, evidence body, or full source URLs."
        ),
    }


def should_record_baseline(explicit: bool = False) -> bool:
    return explicit or _env_bool("FX_RESEARCH_RECORD_BASELINE", False)


def record_fx_research_run(
    *,
    task: Any,
    preset: Any,
    outputs: list[Any],
    brief: Any,
    latency_s: float,
    trigger: str,
    user_id: str | int | None = None,
    followup_requests: list[Any] | None = None,
    record_baseline: bool = False,
    phase10: dict[str, Any] | None = None,
) -> BaselineRecordResult:
    result = BaselineRecordResult(metrics_db_path=str(_default_db_path()))
    try:
        metrics = build_run_metrics(
            task=task,
            preset=preset,
            outputs=outputs,
            brief=brief,
            latency_s=latency_s,
            trigger=trigger,
            user_id=user_id,
            followup_requests=followup_requests,
        )
        with FxResearchMetricsStore() as store:
            store.upsert_run_metrics(metrics)
            result.metrics_written = True
            if should_record_baseline(record_baseline):
                snapshot = build_sanitized_baseline_snapshot(metrics, phase10=phase10)
                path = _base_dir() / f"baseline_{metrics['task_id'][:8]}.json"
                path.write_text(_json_dumps(snapshot), encoding="utf-8")
                store.insert_baseline_snapshot(metrics["task_id"], path, snapshot)
                result.baseline_written = True
                result.baseline_path = str(path)
            store.prune(
                metrics_days=_env_int(
                    "FX_RESEARCH_METRICS_RETENTION_DAYS",
                    _DEFAULT_METRICS_RETENTION_DAYS,
                ),
                baseline_days=_env_int(
                    "FX_RESEARCH_BASELINE_RETENTION_DAYS",
                    _DEFAULT_BASELINE_RETENTION_DAYS,
                ),
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        _log.warning("failed to record fx research metrics", exc_info=True)
    return result
