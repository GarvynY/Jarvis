#!/usr/bin/env python3
"""Tests for Phase 10E follow-up router MVP.

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_followup_router.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from followup_router import (  # noqa: E402
    execute_followup_requests,
    generate_followup_requests,
)
from schema import (  # noqa: E402
    AgentOutput,
    ContextPack,
    Finding,
    FollowupRequest,
    FX_CNYAUD_PRESET,
    ResearchTask,
)


def _task() -> ResearchTask:
    return ResearchTask.from_preset(FX_CNYAUD_PRESET)


def _output(
    agent_name: str = "macro_agent",
    status: str = "ok",
    confidence: float = 0.8,
    *,
    findings: list[Finding] | None = None,
    missing_data: list[str] | None = None,
    as_of: str | None = None,
) -> AgentOutput:
    return AgentOutput(
        agent_name=agent_name,
        status=status,
        confidence=confidence,
        findings=findings or [],
        missing_data=missing_data or [],
        as_of=as_of or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def test_missing_data_triggers_request() -> None:
    reqs = generate_followup_requests(
        _task(),
        [_output("news_agent", "partial", 0.0, missing_data=["news_cache_stale:256h"])],
    )
    assert any(r.trigger_type == "agent_data_missing" for r in reqs)
    assert reqs[0].target_agent == "news_agent"
    print("  data missing triggers request      OK")


def test_high_conflict_triggers_request() -> None:
    reqs = generate_followup_requests(
        _task(),
        [_output()],
        conflict_summary={"conflict_count": 3, "conflict_pairs": [{"a": "x", "b": "y"}]},
    )
    risk_reqs = [r for r in reqs if r.trigger_type == "high_conflict_count"]
    assert risk_reqs
    assert risk_reqs[0].target_agent == "risk_agent"
    print("  high conflict triggers request     OK")


def test_no_problem_returns_empty() -> None:
    reqs = generate_followup_requests(
        _task(),
        [
            _output("fx_agent"),
            _output("news_agent"),
            _output("macro_agent"),
            _output("risk_agent", confidence=0.7),
        ],
        context_pack=ContextPack(coverage={"fx_agent": 1, "news_agent": 1, "macro_agent": 1}),
        conflict_summary={"conflict_count": 0},
    )
    assert reqs == []
    print("  clean inputs return empty          OK")


def test_priority_is_clamped() -> None:
    req = FollowupRequest(
        target_agent="macro_agent",
        target_category="macro",
        reason="test",
        priority=1.0,
        suggested_query="query",
        max_depth=1,
        trigger_type="unit",
    )
    assert 0.0 <= req.priority <= 1.0

    stale = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(timespec="seconds")
    reqs = generate_followup_requests(
        _task(),
        [_output("macro_agent", as_of=stale)],
    )
    assert reqs
    assert all(0.0 <= r.priority <= 1.0 for r in reqs)
    print("  priority stays in [0,1]           OK")


def test_recommendation_mode_does_not_call_agents() -> None:
    reqs = generate_followup_requests(
        _task(),
        [_output("macro_agent", "partial", 0.3, missing_data=["missing policy detail"])],
    )
    executed = execute_followup_requests(reqs, enable_followup_execution=False)
    assert executed == []
    print("  recommendation mode calls no agents OK")


def test_low_coverage_triggers_request() -> None:
    reqs = generate_followup_requests(
        _task(),
        [_output("fx_agent")],
        context_pack=ContextPack(coverage={"fx_agent": 1}, total_tokens=500, budget_tokens=6000),
    )
    assert any(r.trigger_type == "low_section_coverage" for r in reqs)
    print("  low coverage triggers request      OK")


def test_high_importance_low_confidence_triggers_request() -> None:
    finding = Finding(
        key="rba_signal",
        summary="RBA signal is potentially important but weakly supported.",
        category="macro",
        importance=0.9,
        evidence_score=0.3,
    )
    reqs = generate_followup_requests(
        _task(),
        [_output("macro_agent", confidence=0.4, findings=[finding])],
    )
    assert any(r.trigger_type == "high_importance_low_confidence" for r in reqs)
    print("  high importance low confidence     OK")


def main() -> None:
    print("Phase 10E -- follow-up router tests")
    print("=" * 56)
    test_missing_data_triggers_request()
    test_high_conflict_triggers_request()
    test_no_problem_returns_empty()
    test_priority_is_clamped()
    test_recommendation_mode_does_not_call_agents()
    test_low_coverage_triggers_request()
    test_high_importance_low_confidence_triggers_request()
    print("=" * 56)
    print("All 7 tests passed.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
