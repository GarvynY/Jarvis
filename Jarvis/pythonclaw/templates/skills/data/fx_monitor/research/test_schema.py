#!/usr/bin/env python3
"""
Phase 9 Step 1 — Schema serialisation and validation tests.

Verifies:
  1. All dataclasses serialise to plain dict (no datetime objects)
  2. Nested dataclasses serialise correctly
  3. No datetime objects stored in any field (default factory path)
  4. to_dict() raises TypeError when a datetime is smuggled into a field
  5. FX_CNYAUD_PRESET exists in PRESET_REGISTRY
  6. AGENT_REGISTRY is NOT exported from schema
  7. from_dict() round-trips for all dataclasses
  8. validate_status / validate_confidence raise on bad input
  9. AgentOutput(status="bad") raises at construction time  [P0]
 10. AgentOutput(confidence=9) raises at construction time  [P0]
 11. Finding(evidence_score=1.5) raises at construction time [P0]
 12. ResearchBrief.from_dict() never overwrites disclaimer   [P0]
 13. Instance .to_dict() matches global to_dict()            [P2]
 14. SafeUserContext has risk_level field                    [P1]
 15. ResearchPreset has new P1 fields                        [P1]

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_schema.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import (  # noqa: E402
    AgentOutput,
    CostEstimate,
    Finding,
    FX_CNYAUD_PRESET,
    PRESET_REGISTRY,
    ResearchBrief,
    ResearchPreset,
    ResearchSection,
    ResearchTask,
    SafeUserContext,
    SourceRef,
    _FIXED_DISCLAIMER,
    now_iso,
    to_dict,
    to_json,
    validate_confidence,
    validate_status,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _no_datetime_in(obj: object, path: str = "root") -> None:
    if isinstance(obj, datetime):
        raise AssertionError(f"datetime object found at {path!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_datetime_in(v, f"{path}.{k}")
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_datetime_in(v, f"{path}[{i}]")


def _json_round_trip(obj: object, label: str) -> dict:
    raw = to_json(obj)
    parsed = json.loads(raw)
    _no_datetime_in(parsed, label)
    return parsed


def _expect_error(fn, exc_type: type, label: str) -> None:
    try:
        fn()
        raise AssertionError(f"{label}: expected {exc_type.__name__} but no exception was raised")
    except exc_type:
        pass


# ── Individual dataclass tests ────────────────────────────────────────────────

def test_safe_user_context() -> None:
    ctx = SafeUserContext(
        target_rate=4.78,
        alert_threshold=0.5,
        purpose="tuition",
        risk_level="low",
        preferred_summary_style="brief",
        preferred_topics=["RBA", "iron ore"],
        privacy_level="high",
    )
    d = to_dict(ctx)
    assert d["target_rate"] == 4.78
    assert d["risk_level"] == "low"          # P1: field must exist
    assert d["purpose"] == "tuition"
    assert isinstance(d["preferred_topics"], list)
    _no_datetime_in(d, "SafeUserContext")

    ctx2 = SafeUserContext.from_dict(d)
    assert ctx2.target_rate == ctx.target_rate
    assert ctx2.risk_level == "low"
    assert ctx2.preferred_topics == ctx.preferred_topics

    # Instance method matches global helper
    assert ctx.to_dict() == d
    print("  SafeUserContext          OK")


def test_research_preset_p1_fields() -> None:
    d = to_dict(FX_CNYAUD_PRESET)
    assert d["name"] == "fx_cnyaud"
    assert d["research_type"] == "fx"
    assert "fx_agent" in d["default_agents"]
    assert len(d["report_sections"]) == 4
    # P1 new fields
    assert d["output_language"] == "zh-CN"
    assert d["default_region"] == "CN-AU"
    assert isinstance(d["required_agents"], list)
    assert isinstance(d["optional_agents"], list)
    assert isinstance(d["data_sources"], list)
    assert "fx_agent" in d["required_agents"]
    _no_datetime_in(d, "ResearchPreset")

    p2 = ResearchPreset.from_dict(d)
    assert p2.name == "fx_cnyaud"
    assert p2.output_language == "zh-CN"
    assert p2.banned_terms == FX_CNYAUD_PRESET.banned_terms

    assert FX_CNYAUD_PRESET.to_dict() == d
    print("  ResearchPreset (P1)      OK")


def test_research_task() -> None:
    task = ResearchTask.from_preset(
        FX_CNYAUD_PRESET,
        safe_user_context=SafeUserContext(purpose="living", risk_level="medium"),
        focus_assets=["AUD", "CNY"],
        focus_pair="CNY/AUD",
    )
    d = to_dict(task)
    assert d["preset_name"] == "fx_cnyaud"
    assert d["safe_user_context"]["purpose"] == "living"
    assert d["safe_user_context"]["risk_level"] == "medium"
    assert isinstance(d["requested_at"], str)  # must be string, not datetime
    _no_datetime_in(d, "ResearchTask")

    task2 = ResearchTask.from_dict(d)
    assert task2.task_id == task.task_id
    assert task2.safe_user_context.purpose == "living"

    assert task.to_dict() == d
    print("  ResearchTask             OK")


def test_source_ref() -> None:
    src = SourceRef(
        title="CNY/AUD 实时汇率",
        url="https://open.er-api.com/v6/latest/CNY",
        source="open.er-api.com",
        retrieved_at=now_iso(),
        published_at=None,
    )
    d = to_dict(src)
    assert d["published_at"] is None
    _no_datetime_in(d, "SourceRef")

    src2 = SourceRef.from_dict(d)
    assert src2.source == "open.er-api.com"
    assert src.to_dict() == d
    print("  SourceRef                OK")


def test_finding() -> None:
    f = Finding(
        key="rba_hold",
        summary="RBA 维持利率不变",
        direction="bearish_aud",
        evidence_score=0.8,
        attention_score=0.5,
    )
    d = to_dict(f)
    assert d["direction"] == "bearish_aud"
    assert d["evidence_score"] == 0.8
    _no_datetime_in(d, "Finding")

    f2 = Finding.from_dict(d)
    assert f2.key == "rba_hold"
    assert f.to_dict() == d
    print("  Finding                  OK")


def test_finding_score_validation() -> None:
    """P0: evidence_score / attention_score must be None or [0,1]."""
    _expect_error(
        lambda: Finding(key="x", summary="x", evidence_score=1.5),
        ValueError, "Finding(evidence_score=1.5)",
    )
    _expect_error(
        lambda: Finding(key="x", summary="x", attention_score=-0.1),
        ValueError, "Finding(attention_score=-0.1)",
    )
    # None is always valid
    f = Finding(key="x", summary="x", evidence_score=None, attention_score=None)
    assert f.evidence_score is None
    print("  Finding score validation  OK  [P0]")


def test_agent_output() -> None:
    out = AgentOutput(
        agent_name="fx_agent",
        status="ok",
        summary="1 AUD = 4.7800 CNY",
        findings=[Finding(key="rate", summary="1 AUD = 4.78 CNY", direction="neutral")],
        sources=[SourceRef(
            title="市场汇率", url="https://example.com",
            source="open.er-api.com", retrieved_at=now_iso(),
        )],
        confidence=0.9,
        latency_ms=342,
    )
    d = _json_round_trip(out, "AgentOutput")
    assert d["agent_name"] == "fx_agent"
    assert len(d["findings"]) == 1
    assert len(d["sources"]) == 1
    assert d["confidence"] == 0.9

    out2 = AgentOutput.from_dict(d)
    assert out2.findings[0].key == "rate"
    assert out2.sources[0].source == "open.er-api.com"

    assert out.to_dict() == to_dict(out)
    print("  AgentOutput              OK")


def test_agent_output_validation_p0() -> None:
    """P0: invalid status / confidence must raise at construction time."""
    _expect_error(
        lambda: AgentOutput(agent_name="a", status="bad", confidence=0.5),
        ValueError, "AgentOutput(status='bad')",
    )
    _expect_error(
        lambda: AgentOutput(agent_name="a", status="ok", confidence=9.0),
        ValueError, "AgentOutput(confidence=9)",
    )
    _expect_error(
        lambda: AgentOutput(agent_name="a", status="ok", confidence=-1.0),
        ValueError, "AgentOutput(confidence=-1)",
    )
    # from_dict must also raise on bad data
    _expect_error(
        lambda: AgentOutput.from_dict({"agent_name": "a", "status": "unknown", "confidence": 0.5}),
        ValueError, "AgentOutput.from_dict(status='unknown')",
    )
    _expect_error(
        lambda: AgentOutput.from_dict({"agent_name": "a", "status": "ok", "confidence": 2.0}),
        ValueError, "AgentOutput.from_dict(confidence=2.0)",
    )
    print("  AgentOutput validation   OK  [P0]")


def test_agent_output_make_error() -> None:
    err = AgentOutput.make_error("macro_agent", "network timeout", latency_ms=5000)
    assert err.status == "error"
    assert err.error == "network timeout"
    d = to_dict(err)
    _no_datetime_in(d, "AgentOutput.make_error")
    print("  AgentOutput.make_error   OK")


def test_research_brief_disclaimer_p0() -> None:
    """P0: from_dict must never overwrite the fixed disclaimer."""
    malicious_brief = {
        "task_id": "t1",
        "preset_name": "fx_cnyaud",
        "disclaimer": "No disclaimer — do whatever you want!",
    }
    brief = ResearchBrief.from_dict(malicious_brief)
    assert brief.disclaimer == _FIXED_DISCLAIMER, (
        f"Disclaimer was overwritten! Got: {brief.disclaimer!r}"
    )
    print("  ResearchBrief disclaimer OK  [P0]")


def test_research_brief_nested() -> None:
    """Nested serialisation: ResearchBrief → ResearchSection → CostEstimate."""
    brief = ResearchBrief(
        task_id="test-task-123",
        preset_name="fx_cnyaud",
        conclusion="综合来看，AUD 短期走势中性偏弱。",
        sections=[
            ResearchSection(
                title="汇率事实",
                content="当前 1 AUD = 4.7800 CNY。",
                source_agents=["fx_agent"],
                has_data_gap=False,
            ),
            ResearchSection(
                title="风险与矛盾",
                content="宏观与新闻信号存在矛盾。",
                source_agents=["risk_agent"],
                has_data_gap=True,
            ),
        ],
        agent_statuses={"fx_agent": "ok", "news_agent": "partial"},
        cost_estimate=CostEstimate(
            llm_calls=1, estimated_tokens=800, estimated_cost_usd=0.002
        ),
    )
    d = _json_round_trip(brief, "ResearchBrief")
    assert len(d["sections"]) == 2
    assert d["sections"][1]["has_data_gap"] is True
    assert d["cost_estimate"]["llm_calls"] == 1
    assert d["disclaimer"] == _FIXED_DISCLAIMER

    brief2 = ResearchBrief.from_dict(d)
    assert brief2.sections[0].title == "汇率事实"
    assert brief2.cost_estimate.estimated_cost_usd == 0.002
    assert brief2.disclaimer == _FIXED_DISCLAIMER

    assert brief.to_dict() == d
    print("  ResearchBrief (nested)   OK")


def test_to_dict_datetime_guard_p0() -> None:
    """P0: to_dict() must raise TypeError if a datetime is present in any field."""
    import dataclasses

    @dataclasses.dataclass
    class _BadSource:
        title: str
        url: str
        source: str
        retrieved_at: object   # deliberately untyped so we can stuff a datetime in
        published_at: str | None = None

    bad = _BadSource(
        title="test",
        url="https://example.com",
        source="test",
        retrieved_at=datetime.now(timezone.utc),   # datetime, not string!
    )
    _expect_error(lambda: to_dict(bad), TypeError, "to_dict with datetime field")
    print("  to_dict datetime guard   OK  [P0]")


def test_preset_registry() -> None:
    assert "fx_cnyaud" in PRESET_REGISTRY
    assert PRESET_REGISTRY["fx_cnyaud"] is FX_CNYAUD_PRESET
    print("  PRESET_REGISTRY          OK")


def test_agent_registry_not_in_schema() -> None:
    """AGENT_REGISTRY must NOT be importable from schema — it belongs in coordinator."""
    import schema as _schema
    assert not hasattr(_schema, "AGENT_REGISTRY"), (
        "AGENT_REGISTRY should not be defined in schema.py"
    )
    print("  AGENT_REGISTRY absent    OK  [P1]")


def test_validators() -> None:
    assert validate_status("ok") == "ok"
    assert validate_status("partial") == "partial"
    assert validate_status("error") == "error"
    _expect_error(lambda: validate_status("unknown"), ValueError, "validate_status('unknown')")
    _expect_error(lambda: validate_status(""), ValueError, "validate_status('')")

    assert validate_confidence(0.0) == 0.0
    assert validate_confidence(1.0) == 1.0
    assert validate_confidence(0.75) == 0.75
    _expect_error(lambda: validate_confidence(1.1), ValueError, "validate_confidence(1.1)")
    _expect_error(lambda: validate_confidence(-0.1), ValueError, "validate_confidence(-0.1)")
    print("  validate_status/conf     OK")


def test_no_datetime_objects_in_defaults() -> None:
    """Default factory fields must produce strings, not datetime objects."""
    task = ResearchTask()
    brief = ResearchBrief(task_id="x", preset_name="fx_cnyaud")
    out = AgentOutput(agent_name="a", status="ok")

    for obj, label in [
        (task, "ResearchTask"),
        (brief, "ResearchBrief"),
        (out, "AgentOutput"),
    ]:
        _no_datetime_in(to_dict(obj), label)
    print("  no datetime in defaults  OK")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all() -> None:
    print("Phase 9 Step 1 — Schema tests")
    print("=" * 50)
    test_safe_user_context()
    test_research_preset_p1_fields()
    test_research_task()
    test_source_ref()
    test_finding()
    test_finding_score_validation()
    test_agent_output()
    test_agent_output_validation_p0()
    test_agent_output_make_error()
    test_research_brief_disclaimer_p0()
    test_research_brief_nested()
    test_to_dict_datetime_guard_p0()
    test_preset_registry()
    test_agent_registry_not_in_schema()
    test_validators()
    test_no_datetime_objects_in_defaults()
    print("=" * 50)
    print(f"All {16} tests passed.")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        sys.exit(1)
