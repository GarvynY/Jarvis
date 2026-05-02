#!/usr/bin/env python3
"""
Phase 9 Step 2 — LocalAsyncRunner tests.

Verifies:
   1. test_all_ok               — all outputs status="ok"
   2. test_one_error_isolated   — error isolated, others unaffected
   3. test_one_timeout_isolated — timeout isolated, wall time bounded
   4. test_output_length_stable — always len(agents) results (n=0,1,3,5)
   5. test_output_order_stable  — positions map to agent order
   6. test_sync_agent_wrapped   — sync run() wrapped via own executor
   7. test_empty_agents         — [] -> []
   8. test_make_error_output    — JSON-safe, correct fields
   9. test_wrong_return_type    — dict return becomes error output    [P0]
  10. test_latency_filled       — runner fills latency_ms if agent left it 0  [P1]
  11. test_agent_name_corrected — mismatched agent_name corrected    [P1]
  12. test_poisoned_output      — non-JSON-safe output becomes error  [P0]

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_runner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import AgentOutput, ResearchTask, SafeUserContext  # noqa: E402
from runner import (  # noqa: E402
    LocalAsyncRunner,
    OkMockAgent,
    ErrorMockAgent,
    TimeoutMockAgent,
    SyncOkMockAgent,
    WrongNameMockAgent,
    PoisonedOutputMockAgent,
    make_error_output,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _task() -> ResearchTask:
    return ResearchTask(
        preset_name="fx_cnyaud",
        research_type="fx",
        research_topic="test",
        safe_user_context=SafeUserContext(),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _assert_json_safe(output: AgentOutput, label: str) -> None:
    try:
        json.dumps(output.to_dict(), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label}: output is not JSON-safe: {exc}") from exc


def _print_outputs(outputs: list[AgentOutput]) -> None:
    for o in outputs:
        flag = "OK" if o.status == "ok" else "!!"
        print(f"    [{flag}] [{o.status:7s}] {o.agent_name}  latency={o.latency_ms}ms"
              + (f"  err={o.error[:60]}" if o.error else ""))


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_all_ok() -> None:
    async with LocalAsyncRunner() as runner:
        agents = [OkMockAgent(name="a"), OkMockAgent(name="b"), OkMockAgent(name="c")]
        outputs = await runner.run_many(_task(), agents)

    assert len(outputs) == 3
    for o in outputs:
        assert o.status == "ok", f"{o.agent_name}: expected ok, got {o.status}"
        _assert_json_safe(o, o.agent_name)

    print("\n-- test_all_ok")
    _print_outputs(outputs)
    print("   PASS")


async def test_one_error_isolated() -> None:
    async with LocalAsyncRunner() as runner:
        agents = [OkMockAgent(name="a"), ErrorMockAgent(), OkMockAgent(name="c")]
        outputs = await runner.run_many(_task(), agents)

    assert len(outputs) == 3
    assert outputs[0].status == "ok",    f"a should be ok"
    assert outputs[1].status == "error", f"error_mock should be error"
    assert outputs[2].status == "ok",    f"c should be ok"
    assert "deliberate mock error" in (outputs[1].error or "")
    for o in outputs:
        _assert_json_safe(o, o.agent_name)

    print("\n-- test_one_error_isolated")
    _print_outputs(outputs)
    print("   PASS")


async def test_one_timeout_isolated() -> None:
    async with LocalAsyncRunner() as runner:
        agents = [OkMockAgent(name="a"), TimeoutMockAgent(), OkMockAgent(name="c")]
        t0 = time.monotonic()
        outputs = await runner.run_many(_task(), agents, timeout_seconds=1)
        elapsed = time.monotonic() - t0

    assert len(outputs) == 3
    assert outputs[0].status == "ok",    "a should be ok"
    assert outputs[1].status == "error", "timeout should be error"
    assert outputs[2].status == "ok",    "c should be ok"
    assert "timed out" in (outputs[1].error or "")
    assert elapsed < 5.0, f"run_many took {elapsed:.1f}s — timeout not working"
    for o in outputs:
        _assert_json_safe(o, o.agent_name)

    print("\n-- test_one_timeout_isolated")
    _print_outputs(outputs)
    print(f"   wall time: {elapsed:.2f}s  (timeout=1s)")
    print("   PASS")


async def test_output_length_stable() -> None:
    for n in [0, 1, 3, 5]:
        async with LocalAsyncRunner() as runner:
            agents = [OkMockAgent(name=f"a{i}") for i in range(n)]
            outputs = await runner.run_many(_task(), agents)
        assert len(outputs) == n, f"n={n}: expected {n} outputs, got {len(outputs)}"

    print("\n-- test_output_length_stable")
    print("   n=0,1,3,5 all returned correct length")
    print("   PASS")


async def test_output_order_stable() -> None:
    names = ["first", "second", "third", "fourth"]
    async with LocalAsyncRunner() as runner:
        agents = [OkMockAgent(name=n) for n in names]
        outputs = await runner.run_many(_task(), agents)
        for agent, output in zip(agents, outputs):
            assert output.agent_name == agent.agent_name, (
                f"Expected {agent.agent_name}, got {output.agent_name}"
            )

        # Mixed ok / error / ok — order must still hold
        mixed = [OkMockAgent(name="x"), ErrorMockAgent(), OkMockAgent(name="z")]
        mixed_out = await runner.run_many(_task(), mixed, timeout_seconds=1)

    assert mixed_out[0].agent_name == "x"
    assert mixed_out[1].agent_name == "error_mock_agent"
    assert mixed_out[2].agent_name == "z"

    print("\n-- test_output_order_stable")
    print(f"   order: {[o.agent_name for o in outputs]}")
    print("   PASS")


async def test_sync_agent_wrapped() -> None:
    async with LocalAsyncRunner() as runner:
        agents = [OkMockAgent(name="async_a"), SyncOkMockAgent(), OkMockAgent(name="async_b")]
        outputs = await runner.run_many(_task(), agents)

    assert len(outputs) == 3
    assert outputs[1].agent_name == "sync_ok_mock_agent"
    assert outputs[1].status == "ok"
    for o in outputs:
        _assert_json_safe(o, o.agent_name)

    print("\n-- test_sync_agent_wrapped")
    _print_outputs(outputs)
    print("   PASS")


async def test_empty_agents() -> None:
    async with LocalAsyncRunner() as runner:
        outputs = await runner.run_many(_task(), [])
    assert outputs == []

    print("\n-- test_empty_agents")
    print("   [] -> []  PASS")


async def test_make_error_output() -> None:
    out = make_error_output("test_agent", "something broke", latency_ms=123)
    assert out.agent_name == "test_agent"
    assert out.status == "error"
    assert out.error == "something broke"
    assert out.latency_ms == 123
    _assert_json_safe(out, "make_error_output")

    print("\n-- test_make_error_output")
    d = out.to_dict()
    print(f"   agent_name={d['agent_name']}  status={d['status']}  "
          f"latency_ms={d['latency_ms']}  error={d['error']!r}")
    print("   PASS")


async def test_wrong_return_type() -> None:
    """P0: agent returning non-AgentOutput must produce an error output."""
    class BadReturnAgent:
        agent_name = "bad_return_agent"
        async def run(self, task: ResearchTask) -> AgentOutput:  # noqa: ANN201
            return {"status": "ok"}  # type: ignore[return-value]

    async with LocalAsyncRunner() as runner:
        outputs = await runner.run_many(_task(), [BadReturnAgent()])

    assert len(outputs) == 1
    assert outputs[0].status == "error"
    assert "AgentOutput" in (outputs[0].error or "")

    print("\n-- test_wrong_return_type")
    print(f"   error: {outputs[0].error}")
    print("   PASS")


async def test_latency_filled() -> None:
    """P1: runner fills latency_ms=0 with its own observed wall time."""
    async with LocalAsyncRunner() as runner:
        agents = [OkMockAgent(name="a", delay=0.05)]  # 50ms delay
        outputs = await runner.run_many(_task(), agents)

    out = outputs[0]
    assert out.status == "ok"
    # Runner should have filled latency to at least ~50ms
    assert out.latency_ms >= 40, (
        f"Expected latency >= 40ms (runner fill), got {out.latency_ms}ms"
    )

    print("\n-- test_latency_filled")
    print(f"   latency_ms={out.latency_ms}ms  (agent had delay=50ms)")
    print("   PASS")


async def test_agent_name_corrected() -> None:
    """P1: runner corrects agent_name if it doesn't match the agent's declared name."""
    async with LocalAsyncRunner() as runner:
        outputs = await runner.run_many(_task(), [WrongNameMockAgent()])

    out = outputs[0]
    assert out.status == "ok", f"Expected ok, got {out.status}"
    assert out.agent_name == "correct_name", (
        f"Expected agent_name corrected to 'correct_name', got {out.agent_name!r}"
    )

    print("\n-- test_agent_name_corrected")
    print(f"   agent_name corrected to: {out.agent_name!r}")
    print("   PASS")


async def test_poisoned_output() -> None:
    """P0: non-JSON-safe output (datetime field) must become error output."""
    async with LocalAsyncRunner() as runner:
        outputs = await runner.run_many(_task(), [PoisonedOutputMockAgent()])

    out = outputs[0]
    assert out.status == "error", f"Expected error for poisoned output, got {out.status}"
    assert "JSON" in (out.error or "") or "datetime" in (out.error or ""), (
        f"Error message should mention JSON/datetime, got: {out.error!r}"
    )
    _assert_json_safe(out, "poisoned_output")

    print("\n-- test_poisoned_output")
    print(f"   error: {out.error}")
    print("   PASS")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Phase 9 Step 2 -- LocalAsyncRunner tests")
    print("=" * 55)

    await test_all_ok()
    await test_one_error_isolated()
    await test_one_timeout_isolated()
    await test_output_length_stable()
    await test_output_order_stable()
    await test_sync_agent_wrapped()
    await test_empty_agents()
    await test_make_error_output()
    await test_wrong_return_type()
    await test_latency_filled()
    await test_agent_name_corrected()
    await test_poisoned_output()

    print("\n" + "=" * 55)
    print("All 12 tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, Exception) as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}")
        sys.exit(1)
