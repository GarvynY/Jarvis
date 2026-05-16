#!/usr/bin/env python3
"""
Phase 10.6E — agent_audit helper tests.

Validates:
  1. Audit functions format logs without exceptions
  2. Handles None fields gracefully
  3. Handles empty strings
  4. Handles large lists/dicts (truncation)
  5. Logging failure does not propagate
  6. MarketDriversAgent triggers audit events in mock run
  7. Existing agents still run with audit enabled
  8. _format_fields produces safe output

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_agent_audit.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_AGENTS_DIR = _HERE / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from agent_audit import (
    audit_agent_start,
    audit_agent_event,
    audit_agent_end,
    audit_agent_error,
    _format_fields,
    _safe_str,
)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)


def _setup_capture() -> _LogCapture:
    handler = _LogCapture()
    logger = logging.getLogger("research.agent_audit")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


def _teardown_capture(handler: _LogCapture) -> None:
    logger = logging.getLogger("research.agent_audit")
    logger.removeHandler(handler)


def test_audit_start_formats_correctly() -> None:
    handler = _setup_capture()
    try:
        t0 = audit_agent_start("test_agent", "task-123", requested_items=["a", "b"])
        assert isinstance(t0, float)
        assert len(handler.records) == 1
        msg = handler.records[0].getMessage()
        assert "[AUDIT][agent][start]" in msg
        assert "test_agent" in msg
        assert "task-123" in msg
    finally:
        _teardown_capture(handler)
    print("  audit_start formats correctly      OK")


def test_audit_event_formats_correctly() -> None:
    handler = _setup_capture()
    try:
        audit_agent_event("ag", "t1", "fetch_done", item_count=5, provider="yfinance")
        msg = handler.records[0].getMessage()
        assert "[AUDIT][agent][event]" in msg
        assert "fetch_done" in msg
        assert "item_count=5" in msg
    finally:
        _teardown_capture(handler)
    print("  audit_event formats correctly      OK")


def test_audit_end_formats_correctly() -> None:
    handler = _setup_capture()
    try:
        audit_agent_end("ag", "t1", "ok", latency_ms=150, finding_count=3)
        msg = handler.records[0].getMessage()
        assert "[AUDIT][agent][end]" in msg
        assert "status=ok" in msg
        assert "latency_ms=150" in msg
        assert "finding_count=3" in msg
    finally:
        _teardown_capture(handler)
    print("  audit_end formats correctly        OK")


def test_audit_error_formats_correctly() -> None:
    handler = _setup_capture()
    try:
        audit_agent_error("ag", "t1", "ConnectionError: timeout", latency_ms=5000)
        msg = handler.records[0].getMessage()
        assert "[AUDIT][agent][error]" in msg
        assert "ConnectionError" in msg
    finally:
        _teardown_capture(handler)
    print("  audit_error formats correctly      OK")


def test_handles_none_fields() -> None:
    handler = _setup_capture()
    try:
        audit_agent_start("ag", "t1", x=None, y=None)
        audit_agent_event("ag", "t1", "ev", data=None)
        audit_agent_end("ag", "t1", "ok", latency_ms=None)
        audit_agent_error("ag", "t1", None)
        assert len(handler.records) == 4
    finally:
        _teardown_capture(handler)
    print("  handles None fields                OK")


def test_handles_empty_strings() -> None:
    handler = _setup_capture()
    try:
        audit_agent_start("", "", field="")
        assert len(handler.records) == 1
    finally:
        _teardown_capture(handler)
    print("  handles empty strings              OK")


def test_handles_large_lists() -> None:
    big_list = [f"item_{i}" for i in range(100)]
    result = _format_fields(items=big_list)
    assert "item_0" in result
    assert "item_99" not in result  # truncated to 20
    print("  handles large lists (truncation)   OK")


def test_format_fields_dict() -> None:
    result = _format_fields(meta={"a": 1, "b": "hello"})
    assert "a=1" in result
    assert "b=hello" in result
    print("  _format_fields dict output         OK")


def test_safe_str_limits_length() -> None:
    long = "x" * 500
    assert len(_safe_str(long, 100)) == 100
    assert _safe_str(None) == ""
    print("  _safe_str limits length            OK")


def test_logging_failure_does_not_propagate() -> None:
    logger = logging.getLogger("research.agent_audit")
    original_handlers = logger.handlers[:]
    logger.handlers = []

    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler crashed")

    logger.addHandler(BrokenHandler())
    try:
        # These should NOT raise
        audit_agent_start("ag", "t1")
        audit_agent_event("ag", "t1", "ev")
        audit_agent_end("ag", "t1", "ok")
        audit_agent_error("ag", "t1", "err")
    finally:
        logger.handlers = original_handlers
    print("  logging failure does not propagate OK")


def test_market_drivers_agent_triggers_audit() -> None:
    from market_drivers_agent import MarketDriversAgent, _ALL_ITEM_KEYS
    from data_sources import DataSourceConfig, DataSourceRegistry, DataSourceResult
    from source_metadata import SourceMetadata
    from schema import ResearchTask

    class MockReg(DataSourceRegistry):
        def __init__(self):
            super().__init__(DataSourceConfig(fred_api_key="", provider_timeout_sec=5))

        def fetch(self, item_key, lookback_days=None):
            return DataSourceResult(
                item_key=item_key, provider="mock", status="ok",
                value=1.0, change_pct=0.5, confidence=0.85,
                source_metadata=SourceMetadata(
                    url="http://test.com", provider="mock",
                    domain="test.com", source_type="market_data_api", source_tier=2,
                ),
            )

    handler = _setup_capture()
    try:
        agent = MarketDriversAgent()
        agent._registry = MockReg()
        task = ResearchTask(task_id="audit-test", preset_name="fx_cnyaud")
        output = asyncio.run(agent.run(task))
        assert output.status == "ok"

        messages = [r.getMessage() for r in handler.records]
        start_msgs = [m for m in messages if "[start]" in m]
        end_msgs = [m for m in messages if "[end]" in m]
        event_msgs = [m for m in messages if "[event]" in m]

        assert len(start_msgs) >= 1
        assert len(end_msgs) >= 1
        assert len(event_msgs) >= 1
        assert any("fetch_complete" in m for m in event_msgs)
        assert any("item_ok" in m for m in event_msgs)
    finally:
        _teardown_capture(handler)
    print("  MarketDriversAgent triggers audit  OK")


def test_fx_agent_runs_with_audit() -> None:
    from fx_agent import FXAgent
    from schema import ResearchTask

    with patch("fx_agent._fetch_rate_cached") as mock_fetch:
        mock_fetch.return_value = {
            "error": "test_mode",
            "fetched_at_utc": "2026-05-16T00:00:00Z",
            "_cache": {"hit": False},
        }
        agent = FXAgent()
        task = ResearchTask(task_id="audit-fx", preset_name="fx_cnyaud", focus_pair="CNY/AUD")
        output = asyncio.run(agent.run(task))
        assert output.status in ("partial", "error", "ok")
    print("  FXAgent runs with audit enabled    OK")


def test_risk_agent_runs_with_audit() -> None:
    from risk_agent import RiskAgent
    from schema import AgentOutput, ResearchTask

    agent = RiskAgent()
    task = ResearchTask(task_id="audit-risk", preset_name="fx_cnyaud")
    phase1 = [
        AgentOutput(agent_name="fx_agent", status="ok", confidence=0.8),
    ]
    output = asyncio.run(agent.run(task, phase1))
    assert output.status == "ok"
    print("  RiskAgent runs with audit enabled  OK")


def run_all() -> None:
    tests = [
        test_audit_start_formats_correctly,
        test_audit_event_formats_correctly,
        test_audit_end_formats_correctly,
        test_audit_error_formats_correctly,
        test_handles_none_fields,
        test_handles_empty_strings,
        test_handles_large_lists,
        test_format_fields_dict,
        test_safe_str_limits_length,
        test_logging_failure_does_not_propagate,
        test_market_drivers_agent_triggers_audit,
        test_fx_agent_runs_with_audit,
        test_risk_agent_runs_with_audit,
    ]
    print("Phase 10.6E — agent_audit tests")
    print("=" * 50)
    for test_fn in tests:
        test_fn()
    print("=" * 50)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
