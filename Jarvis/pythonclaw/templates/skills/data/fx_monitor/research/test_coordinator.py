#!/usr/bin/env python3
"""
Phase 9 Step 4 — coordinator.py standalone tests.

All external I/O is mocked:
  - build_safe_user_context   → patched to return a dict directly
  - Phase-1 agents            → replaced with OkMockAgent / ErrorMockAgent

Tests:
  1. test_unknown_preset       — bad preset_name → error AgentOutput, empty task
  2. test_ok_run               — all agents succeed → ok outputs + risk_output
  3. test_agent_failure        — one agent raises → error output, rest unaffected
  4. test_missing_agent        — agent name not in AGENT_REGISTRY → skipped output
  5. test_safe_ctx_populated   — SafeUserContext fields flow from build_safe_user_context
  6. test_safe_ctx_fallback    — build_safe_user_context unavailable → defaults used
  7. test_risk_output_last     — risk_output is always the last element
  8. test_cost_estimate        — CostEstimate fields are computed correctly
  9. test_all_outputs_json_safe — every AgentOutput serialises to valid JSON
 10. test_custom_overrides     — caller overrides focus_pair, research_topic, time_horizon

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_coordinator.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest.mock
from pathlib import Path

_HERE         = Path(__file__).parent
_RESEARCH_DIR = _HERE

if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from schema import (  # noqa: E402
    AgentOutput, CostEstimate, Finding, ResearchTask, SafeUserContext,
    PRESET_REGISTRY,
)
from runner import OkMockAgent, ErrorMockAgent  # noqa: E402
import coordinator as _coord                    # noqa: E402
from evidence_store import EvidenceStore        # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

_MOCK_USER_CTX = {
    "target_rate":             4.5,
    "alert_threshold":         0.02,
    "purpose":                 "tuition",
    "risk_level":              "low",
    "preferred_summary_style": "brief",
    "preferred_topics":        ["rba", "iron_ore"],
    "privacy_level":           "standard",
}


def _patch_profile(ctx: dict | None = None):
    """Patch build_safe_user_context inside coordinator to return ctx."""
    return unittest.mock.patch(
        "coordinator.build_safe_user_context",
        return_value=ctx if ctx is not None else _MOCK_USER_CTX,
    )


def _patch_profile_raises():
    """Simulate build_safe_user_context being unavailable."""
    return unittest.mock.patch(
        "coordinator.build_safe_user_context",
        side_effect=ImportError("no db"),
    )


def _patch_agents(agent_map: dict[str, object]):
    """Replace the entire AGENT_REGISTRY for the duration of the test."""
    return unittest.mock.patch.dict(_coord.AGENT_REGISTRY, agent_map, clear=True)


def _token_agent(name: str, prompt: int = 200, completion: int = 100) -> OkMockAgent:
    """Return an OkMockAgent that injects token_usage into its output."""

    class _TokenAgent(OkMockAgent):
        async def run(self, task: ResearchTask) -> AgentOutput:
            out = await super().run(task)
            out.token_usage = {
                "prompt_tokens":     prompt,
                "completion_tokens": completion,
            }
            return out

    a = _TokenAgent(name=name)
    return a


def _print_outputs(outputs: list[AgentOutput]) -> None:
    for o in outputs:
        print(f"   [{o.agent_name:15s}] status={o.status}  conf={o.confidence:.2f}  "
              f"tokens={o.token_usage}  latency={o.latency_ms}ms")


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_unknown_preset() -> None:
    """Unknown preset_name → single error AgentOutput, task has the preset_name."""
    with _patch_profile():
        task, outputs, cost = await _coord.run_research("no_such_preset", user_id=1)

    assert task.preset_name == "no_such_preset"
    assert len(outputs) >= 1
    assert outputs[0].status == "error"
    assert "no_such_preset" in (outputs[0].error or "")
    assert isinstance(cost, CostEstimate)

    print("\n-- test_unknown_preset")
    _print_outputs(outputs)
    print("   PASS")


async def test_ok_run() -> None:
    """All agents succeed → ok phase-1 outputs + risk_output appended."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }
    with _patch_profile(), _patch_agents(mock_agents):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=42)

    preset = PRESET_REGISTRY["fx_cnyaud"]
    phase1_count = len(preset.default_agents)

    assert task.preset_name == "fx_cnyaud"
    assert len(outputs) == phase1_count + 1, (
        f"Expected {phase1_count + 1} outputs (phase1 + risk), got {len(outputs)}"
    )
    assert outputs[-1].agent_name == "risk_agent"
    for o in outputs[:-1]:
        assert o.status == "ok", f"Phase-1 agent {o.agent_name} not ok: {o.status}"

    print("\n-- test_ok_run")
    _print_outputs(outputs)
    print(f"   task_id={task.task_id[:8]}  pair={task.focus_pair}")
    print("   PASS")


async def test_agent_failure() -> None:
    """One phase-1 agent fails → error output for that agent, others unaffected."""
    mock_agents = {
        "fx_agent":    type("_FX",   (OkMockAgent,),    {"agent_name": "fx_agent"}),
        "news_agent":  type("_News", (ErrorMockAgent,),  {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro",(OkMockAgent,),     {"agent_name": "macro_agent"}),
    }
    with _patch_profile(), _patch_agents(mock_agents):
        _, outputs, _ = await _coord.run_research("fx_cnyaud", user_id=1)

    by_name = {o.agent_name: o for o in outputs}
    assert by_name["fx_agent"].status    == "ok"
    assert by_name["news_agent"].status  == "error"
    assert by_name["macro_agent"].status == "ok"
    assert by_name["risk_agent"].status  in ("ok", "partial")

    print("\n-- test_agent_failure")
    _print_outputs(outputs)
    print("   PASS")


async def test_missing_agent() -> None:
    """Agent name in preset but not in AGENT_REGISTRY → error output, rest run."""
    # Only fx_agent and news_agent present — macro_agent is absent
    partial_registry = {
        "fx_agent":   type("_FX",   (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent": type("_News", (OkMockAgent,), {"agent_name": "news_agent"}),
    }

    with _patch_profile(), _patch_agents(partial_registry):
        _, outputs, _ = await _coord.run_research("fx_cnyaud", user_id=1)

    names = [o.agent_name for o in outputs]
    assert "macro_agent" in names, f"Expected macro_agent error output, got: {names}"
    macro_out = next(o for o in outputs if o.agent_name == "macro_agent")
    assert macro_out.status == "error"

    print("\n-- test_missing_agent")
    _print_outputs(outputs)
    print("   PASS")


async def test_safe_ctx_populated() -> None:
    """SafeUserContext fields come from build_safe_user_context() return value."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }
    with _patch_profile(_MOCK_USER_CTX), _patch_agents(mock_agents):
        task, _, _ = await _coord.run_research("fx_cnyaud", user_id=99)

    ctx = task.safe_user_context
    assert ctx.purpose     == "tuition",       f"purpose mismatch: {ctx.purpose}"
    assert ctx.risk_level  == "low",           f"risk_level mismatch: {ctx.risk_level}"
    assert ctx.target_rate == 4.5,             f"target_rate mismatch: {ctx.target_rate}"

    print("\n-- test_safe_ctx_populated")
    print(f"   purpose={ctx.purpose}  risk_level={ctx.risk_level}  target_rate={ctx.target_rate}")
    print("   PASS")


async def test_safe_ctx_fallback() -> None:
    """build_safe_user_context unavailable → SafeUserContext uses defaults."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }
    with _patch_profile_raises(), _patch_agents(mock_agents):
        task, outputs, _ = await _coord.run_research("fx_cnyaud", user_id=7)

    ctx = task.safe_user_context
    # Defaults: risk_level="unknown", privacy_level="standard"
    assert ctx.risk_level    == "unknown",  f"Expected default risk_level, got: {ctx.risk_level}"
    assert ctx.privacy_level == "standard", f"Expected default privacy_level, got: {ctx.privacy_level}"
    # Workflow still completed
    assert any(o.agent_name == "risk_agent" for o in outputs)

    print("\n-- test_safe_ctx_fallback")
    print(f"   risk_level={ctx.risk_level}  privacy_level={ctx.privacy_level}")
    print("   PASS")


async def test_risk_output_last() -> None:
    """risk_agent output is always the last element in all_outputs."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }
    with _patch_profile(), _patch_agents(mock_agents):
        _, outputs, _ = await _coord.run_research("fx_cnyaud", user_id=1)

    assert outputs[-1].agent_name == "risk_agent", (
        f"Expected risk_agent last, got: {outputs[-1].agent_name}"
    )

    print("\n-- test_risk_output_last")
    print(f"   last agent: {outputs[-1].agent_name}")
    print("   PASS")


async def test_cost_estimate() -> None:
    """CostEstimate fields are computed from token_usage in outputs."""
    mock_agents = {
        "fx_agent":    _token_agent("fx_agent",    prompt=300, completion=100),
        "news_agent":  _token_agent("news_agent",  prompt=400, completion=150),
        "macro_agent": _token_agent("macro_agent", prompt=250, completion=80),
    }
    # Make classes from instances by using type override trick
    registry_patch = {
        "fx_agent":    type("_FX",    (), {
            "agent_name": "fx_agent",
            "__init__": lambda self: None,
            "run": mock_agents["fx_agent"].run.__func__
                   if hasattr(mock_agents["fx_agent"].run, "__func__")
                   else mock_agents["fx_agent"].run,
        }),
    }
    # Simpler approach: patch AGENT_REGISTRY values to be factory functions
    # that return the pre-built instances.
    class _FactoryFX:
        agent_name = "fx_agent"
        async def run(self, task: ResearchTask) -> AgentOutput:
            out = AgentOutput(agent_name=self.agent_name, status="ok", confidence=1.0)
            out.token_usage = {"prompt_tokens": 300, "completion_tokens": 100}
            return out

    class _FactoryNews:
        agent_name = "news_agent"
        async def run(self, task: ResearchTask) -> AgentOutput:
            out = AgentOutput(agent_name=self.agent_name, status="ok", confidence=1.0)
            out.token_usage = {"prompt_tokens": 400, "completion_tokens": 150}
            return out

    class _FactoryMacro:
        agent_name = "macro_agent"
        async def run(self, task: ResearchTask) -> AgentOutput:
            out = AgentOutput(agent_name=self.agent_name, status="ok", confidence=1.0)
            out.token_usage = {"prompt_tokens": 250, "completion_tokens": 80}
            return out

    with _patch_profile(), _patch_agents({
        "fx_agent":    _FactoryFX,
        "news_agent":  _FactoryNews,
        "macro_agent": _FactoryMacro,
    }):
        _, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    expected_tokens = (300 + 100) + (400 + 150) + (250 + 80)   # 1280
    assert cost.llm_calls    == 3,               f"Expected 3 llm_calls, got {cost.llm_calls}"
    assert cost.estimated_tokens == expected_tokens, (
        f"Expected {expected_tokens} tokens, got {cost.estimated_tokens}"
    )
    assert cost.estimated_cost_usd > 0,          "Expected non-zero cost"
    assert cost.total_latency_ms  >= 0,          "Expected non-negative latency"

    print("\n-- test_cost_estimate")
    print(f"   llm_calls={cost.llm_calls}  tokens={cost.estimated_tokens}  "
          f"cost=${cost.estimated_cost_usd:.6f}  latency={cost.total_latency_ms}ms")
    print("   PASS")


async def test_all_outputs_json_safe() -> None:
    """Every AgentOutput in all_outputs serialises to valid JSON."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (ErrorMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }
    with _patch_profile(), _patch_agents(mock_agents):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    for o in outputs:
        raw = json.dumps(o.to_dict(), ensure_ascii=False)
        parsed = json.loads(raw)
        assert parsed["agent_name"] == o.agent_name

    json.dumps(task.to_dict(),  ensure_ascii=False)
    json.dumps(cost.to_dict(),  ensure_ascii=False)

    print("\n-- test_all_outputs_json_safe")
    print(f"   {len(outputs)} outputs, all JSON-safe")
    print("   PASS")


async def test_custom_overrides() -> None:
    """Caller-supplied overrides replace preset defaults in ResearchTask."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }
    with _patch_profile(), _patch_agents(mock_agents):
        task, _, _ = await _coord.run_research(
            "fx_cnyaud",
            user_id=1,
            research_topic="自定义研究主题",
            focus_pair="USD/CNY",
            focus_assets=["USD", "CNY"],
            time_horizon="long_term",
            custom_subtopics=["贸易顺差", "外汇储备"],
        )

    assert task.research_topic   == "自定义研究主题",  f"research_topic: {task.research_topic}"
    assert task.focus_pair       == "USD/CNY",         f"focus_pair: {task.focus_pair}"
    assert task.focus_assets     == ["USD", "CNY"],    f"focus_assets: {task.focus_assets}"
    assert task.time_horizon     == "long_term",       f"time_horizon: {task.time_horizon}"
    assert task.custom_subtopics == ["贸易顺差", "外汇储备"], f"subtopics: {task.custom_subtopics}"

    print("\n-- test_custom_overrides")
    print(f"   topic={task.research_topic}  pair={task.focus_pair}  horizon={task.time_horizon}")
    print("   PASS")


async def test_risk_agent_exception() -> None:
    """RiskAgent raising an exception → error AgentOutput appended, no crash."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    class _BrokenRiskAgent:
        agent_name = "risk_agent"
        async def run(self, task: ResearchTask, phase1_outputs: list) -> AgentOutput:
            raise RuntimeError("synthetic risk_agent failure")

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "RiskAgent", _BrokenRiskAgent):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    risk_out = outputs[-1]
    assert risk_out.agent_name == "risk_agent",   f"Expected risk_agent, got: {risk_out.agent_name}"
    assert risk_out.status     == "error",         f"Expected error, got: {risk_out.status}"
    assert "RuntimeError" in (risk_out.error or ""), (
        f"Expected RuntimeError in error field, got: {risk_out.error}"
    )
    # Phase-1 outputs must still be present
    assert len(outputs) == 4, f"Expected 4 outputs (3 phase1 + 1 risk error), got {len(outputs)}"

    print("\n-- test_risk_agent_exception")
    _print_outputs(outputs)
    print("   PASS")


async def test_evidence_store_ingest() -> None:
    """Evidence store ingestion populates chunk_ids/finding_ids on outputs."""

    class _FindingAgent:
        agent_name = "fx_agent"
        async def run(self, task: ResearchTask) -> AgentOutput:
            return AgentOutput(
                agent_name=self.agent_name,
                status="ok",
                confidence=0.8,
                findings=[
                    Finding(key="rate_up", summary="汇率上涨", category="fx_price", importance=0.7),
                    Finding(key="rba_hold", summary="RBA 维持利率", category="macro", importance=0.6),
                ],
            )

    mock_agents = {
        "fx_agent":    _FindingAgent,
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "EvidenceStore",
                                    lambda: EvidenceStore(":memory:")):
        _, outputs, _ = await _coord.run_research("fx_cnyaud", user_id=1)

    fx_out = next(o for o in outputs if o.agent_name == "fx_agent")
    assert len(fx_out.chunk_ids) == 2, f"Expected 2 chunk_ids, got {fx_out.chunk_ids}"
    assert len(fx_out.finding_ids) == 2, f"Expected 2 finding_ids, got {fx_out.finding_ids}"
    assert fx_out.evidence_count == 2

    print("\n-- test_evidence_store_ingest")
    print(f"   fx_agent: chunks={len(fx_out.chunk_ids)}  findings={len(fx_out.finding_ids)}")
    print("   PASS")


async def test_evidence_store_returns_outputs_on_success() -> None:
    """run_research still returns (task, outputs, cost) with evidence fields."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "EvidenceStore",
                                    lambda: EvidenceStore(":memory:")):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    assert isinstance(task, ResearchTask)
    assert isinstance(cost, CostEstimate)
    assert len(outputs) == 4
    assert outputs[-1].agent_name == "risk_agent"

    print("\n-- test_evidence_store_returns_outputs_on_success")
    print(f"   outputs={len(outputs)}  task_id={task.task_id[:8]}")
    print("   PASS")


async def test_evidence_store_failure_graceful() -> None:
    """EvidenceStore failure does not break run_research — original outputs returned."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    class _BrokenStore:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def ingest_outputs(self, *_args, **_kwargs):
            raise RuntimeError("SQLite disk full")

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "EvidenceStore", _BrokenStore):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    assert len(outputs) == 4
    assert outputs[-1].agent_name == "risk_agent"
    for o in outputs[:-1]:
        assert o.status == "ok"
    # evidence_count should be 0 (original, un-enriched outputs)
    for o in outputs:
        assert o.evidence_count == 0

    print("\n-- test_evidence_store_failure_graceful")
    print(f"   outputs={len(outputs)}, all evidence_count=0 (fallback)")
    print("   PASS")


async def test_evidence_store_constructor_oserror() -> None:
    """EvidenceStore constructor raising OSError (unwritable path) → fallback."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    def _raise_os(*_a, **_kw):
        raise OSError("read-only filesystem")

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "EvidenceStore", _raise_os):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    assert len(outputs) == 4
    for o in outputs:
        assert o.evidence_count == 0

    print("\n-- test_evidence_store_constructor_oserror")
    print("   OSError at construction → fallback OK")
    print("   PASS")


async def test_evidence_store_sqlite_locked() -> None:
    """Simulated OperationalError (database is locked) → fallback."""
    import sqlite3 as _sqlite3
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    class _LockedStore:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def ingest_outputs(self, *_a, **_kw):
            raise _sqlite3.OperationalError("database is locked")

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "EvidenceStore", _LockedStore):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    assert len(outputs) == 4
    for o in outputs:
        assert o.evidence_count == 0

    print("\n-- test_evidence_store_sqlite_locked")
    print("   OperationalError (locked) → fallback OK")
    print("   PASS")


async def test_evidence_store_permission_error() -> None:
    """PermissionError during DB open → fallback."""
    mock_agents = {
        "fx_agent":    type("_FX",    (OkMockAgent,), {"agent_name": "fx_agent"}),
        "news_agent":  type("_News",  (OkMockAgent,), {"agent_name": "news_agent"}),
        "macro_agent": type("_Macro", (OkMockAgent,), {"agent_name": "macro_agent"}),
    }

    def _raise_perm(*_a, **_kw):
        raise PermissionError("evidence.sqlite3: permission denied")

    with _patch_profile(), _patch_agents(mock_agents), \
         unittest.mock.patch.object(_coord, "EvidenceStore", _raise_perm):
        task, outputs, cost = await _coord.run_research("fx_cnyaud", user_id=1)

    assert len(outputs) == 4
    for o in outputs:
        assert o.evidence_count == 0

    print("\n-- test_evidence_store_permission_error")
    print("   PermissionError → fallback OK")
    print("   PASS")


# ── Runner ────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 4 -- coordinator tests (mocked agents + profile)")
    print("=" * 60)

    await test_unknown_preset()
    await test_ok_run()
    await test_agent_failure()
    await test_missing_agent()
    await test_safe_ctx_populated()
    await test_safe_ctx_fallback()
    await test_risk_output_last()
    await test_cost_estimate()
    await test_all_outputs_json_safe()
    await test_custom_overrides()
    await test_risk_agent_exception()
    await test_evidence_store_ingest()
    await test_evidence_store_returns_outputs_on_success()
    await test_evidence_store_failure_graceful()
    await test_evidence_store_constructor_oserror()
    await test_evidence_store_sqlite_locked()
    await test_evidence_store_permission_error()

    print("\n" + "=" * 60)
    print("All 17 tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, Exception) as exc:
        import traceback
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
