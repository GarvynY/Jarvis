"""
Phase 10.6E — Unified lightweight agent audit logging.

Non-fatal, JSON-safe structured audit events for research agents.
Uses standard logging with consistent [AUDIT][agent] tags.

All functions are safe to call with any arguments — they never raise.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_log = logging.getLogger("research.agent_audit")


def _safe_str(value: Any, max_len: int = 200) -> str:
    try:
        s = str(value) if value is not None else ""
        return s[:max_len]
    except Exception:
        return ""


def _format_fields(**fields: Any) -> str:
    parts: list[str] = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            parts.append(f"{k}=[{','.join(_safe_str(x, 60) for x in v[:20])}]")
        elif isinstance(v, dict):
            parts.append(f"{k}={{{','.join(f'{kk}={_safe_str(vv,40)}' for kk,vv in list(v.items())[:10])}}}")
        else:
            parts.append(f"{k}={_safe_str(v, 100)}")
    return " ".join(parts)


def audit_agent_start(agent_name: str, task_id: str, **fields: Any) -> float:
    """Log agent start event. Returns monotonic start time for latency calc."""
    try:
        extra = _format_fields(**fields) if fields else ""
        _log.info(
            "[AUDIT][agent][start] agent=%s task_id=%s %s",
            agent_name, task_id, extra,
        )
    except Exception:
        pass
    return time.monotonic()


def audit_agent_event(agent_name: str, task_id: str, event: str, **fields: Any) -> None:
    """Log a named event during agent execution."""
    try:
        extra = _format_fields(**fields) if fields else ""
        _log.info(
            "[AUDIT][agent][event] agent=%s task_id=%s event=%s %s",
            agent_name, task_id, event, extra,
        )
    except Exception:
        pass


def audit_agent_end(
    agent_name: str,
    task_id: str,
    status: str,
    *,
    latency_ms: int | None = None,
    **fields: Any,
) -> None:
    """Log agent completion event."""
    try:
        extra = _format_fields(latency_ms=latency_ms, status=status, **fields) if fields or latency_ms else f"status={status}"
        if latency_ms is not None and not fields:
            extra = f"status={status} latency_ms={latency_ms}"
        else:
            extra = _format_fields(status=status, latency_ms=latency_ms, **fields)
        _log.info(
            "[AUDIT][agent][end] agent=%s task_id=%s %s",
            agent_name, task_id, extra,
        )
    except Exception:
        pass


def audit_agent_error(agent_name: str, task_id: str, error: str, **fields: Any) -> None:
    """Log agent error event (non-fatal — does not raise)."""
    try:
        extra = _format_fields(**fields) if fields else ""
        _log.warning(
            "[AUDIT][agent][error] agent=%s task_id=%s error=%s %s",
            agent_name, task_id, _safe_str(error, 200), extra,
        )
    except Exception:
        pass
