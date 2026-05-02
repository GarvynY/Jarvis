"""
Phase 9 Step 2 — Local async runner.

Runs multiple agents concurrently for one ResearchTask and collects
their AgentOutput results.

Interface is designed for local use but easy to migrate:
  - run_many() signature stays stable under queue/serverless workers
  - make_error_output() is a standalone function usable in remote workers
  - Agents are plain objects; runner never imports concrete agent modules

Agent protocol (duck-typed):
    agent.agent_name: str
    agent.run(task: ResearchTask) -> AgentOutput   # sync OR async

Sync agents are wrapped in loop.run_in_executor() using the runner's own
ThreadPoolExecutor — NOT asyncio's default executor — so asyncio.run()
does not wait for sync threads when the event loop closes.  Call
runner.close() (or use the async context manager) after all run_many()
calls to release the pool cleanly.

Limitation: a sync agent that is timed out keeps its thread running until
the thread's current call returns. For production, prefer async agents.

No imports of: Telegram, LLM providers, personalisation, memory,
               web-search, Tavily, or external APIs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import time
from datetime import datetime
from typing import Any

try:
    from .schema import AgentOutput, ResearchTask
except ImportError:
    from schema import AgentOutput, ResearchTask  # type: ignore[no-redef]


# ── Standalone helper (also usable in remote workers) ────────────────────────

def make_error_output(
    agent_name: str,
    error_message: str,
    latency_ms: int = 0,
) -> AgentOutput:
    """
    Build a JSON-safe AgentOutput for a failed or timed-out agent.

    Standalone (not a method) so remote workers can import it without
    pulling in the full runner class.
    """
    return AgentOutput.make_error(
        agent_name=agent_name,
        error=error_message,
        latency_ms=latency_ms,
    )


# ── Runner ────────────────────────────────────────────────────────────────────

class LocalAsyncRunner:
    """
    Concurrent local runner for Phase 9 research agents.

    Usage:
        async with LocalAsyncRunner() as runner:
            outputs = await runner.run_many(task, agents)

    Or without context manager (call close() manually):
        runner = LocalAsyncRunner()
        outputs = await runner.run_many(task, agents)
        runner.close()

    Migration path:
      - Local:      LocalAsyncRunner().run_many(task, agents)
      - Queue:      replace run_many body with task-queue dispatch;
                    _run_one becomes the worker entry point
      - Serverless: each agent becomes a separate function invocation;
                    coordinator aggregates results via make_error_output
    """

    def __init__(self, max_sync_workers: int = 8) -> None:
        # Dedicated pool for sync agents.
        # Using our own executor (not the asyncio default) means asyncio.run()
        # will NOT block on shutdown_default_executor() waiting for our threads.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_sync_workers,
            thread_name_prefix="runner-sync",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release the sync-agent thread pool.

        Pending tasks are not waited for (cancel_futures=True on Python 3.9+).
        Already-running threads complete on their own after this call.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def __aenter__(self) -> "LocalAsyncRunner":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_many(
        self,
        task: ResearchTask,
        agents: list[Any],
        timeout_seconds: int = 30,
    ) -> list[AgentOutput]:
        """
        Run all agents concurrently and return results in the same order.

        Guarantees:
          - Always returns a list of exactly len(agents) AgentOutputs.
          - Output order matches input agent order.
          - A timeout or exception in one agent never affects others.
          - Every returned AgentOutput is JSON-safe.
        """
        if not agents:
            return []

        coroutines = [
            self._run_one(agent, task, timeout_seconds)
            for agent in agents
        ]
        # asyncio.gather preserves order; return_exceptions=False is fine
        # because _run_one never raises — it always returns AgentOutput.
        results: list[AgentOutput] = await asyncio.gather(*coroutines)
        return list(results)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_one(
        self,
        agent: Any,
        task: ResearchTask,
        timeout_seconds: int,
    ) -> AgentOutput:
        """
        Run a single agent with timeout and full exception isolation.

        Post-conditions on successful return:
          - output is an AgentOutput instance
          - output.agent_name matches the agent's declared agent_name
          - output.latency_ms >= runner-observed wall time (if agent left it 0)
          - output passes JSON-safety check (to_dict() succeeds)
        """
        agent_name: str = getattr(agent, "agent_name", repr(agent))
        t0 = time.monotonic()

        try:
            run_fn = agent.run

            if inspect.iscoroutinefunction(run_fn):
                coro = run_fn(task)
            else:
                # Sync agent: run in our own thread pool, not asyncio default,
                # so the event loop is free and shutdown is not blocked.
                loop = asyncio.get_running_loop()
                coro = loop.run_in_executor(self._executor, run_fn, task)

            output: AgentOutput = await asyncio.wait_for(
                coro, timeout=timeout_seconds
            )

            # ── Post-return validation ─────────────────────────────────────

            # 1. Type check
            if not isinstance(output, AgentOutput):
                raise TypeError(
                    f"agent.run() must return AgentOutput, "
                    f"got {type(output).__name__}"
                )

            # 2. agent_name alignment — correct silently to prevent
            #    coordinator status-map pollution if agent mis-reports itself
            if output.agent_name != agent_name:
                output.agent_name = agent_name

            # 3. Fill latency if agent did not set its own
            runner_latency_ms = int((time.monotonic() - t0) * 1000)
            if output.latency_ms == 0:
                output.latency_ms = runner_latency_ms

            # 4. JSON-safety guard — catches datetime/bytes/etc. that slipped
            #    into output fields after __post_init__ ran
            try:
                output.to_dict()
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"agent output is not JSON-safe: {exc}"
                ) from exc

            return output

        except asyncio.TimeoutError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return make_error_output(
                agent_name,
                f"agent timed out after {timeout_seconds}s "
                f"(task_id={task.task_id[:8]})",
                latency_ms,
            )

        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - t0) * 1000)
            return make_error_output(
                agent_name,
                f"{type(exc).__name__}: {exc} "
                f"(task_id={task.task_id[:8]})",
                latency_ms,
            )


# ── Mock agents (for tests and local development only) ───────────────────────

class OkMockAgent:
    """Always succeeds after a short async delay."""

    agent_name = "ok_mock_agent"

    def __init__(self, delay: float = 0.0, name: str | None = None) -> None:
        self._delay = delay
        if name:
            self.agent_name = name

    async def run(self, task: ResearchTask) -> AgentOutput:
        if self._delay:
            await asyncio.sleep(self._delay)
        return AgentOutput(
            agent_name=self.agent_name,
            status="ok",
            summary=f"mock ok — task_id={task.task_id[:8]}",
            confidence=1.0,
        )


class ErrorMockAgent:
    """Always raises an exception inside run()."""

    agent_name = "error_mock_agent"

    def __init__(self, message: str = "deliberate mock error") -> None:
        self._message = message

    async def run(self, task: ResearchTask) -> AgentOutput:  # noqa: ARG002
        raise RuntimeError(self._message)


class TimeoutMockAgent:
    """Sleeps longer than any reasonable timeout (async — no thread)."""

    agent_name = "timeout_mock_agent"

    def __init__(self, sleep: float = 999.0) -> None:
        self._sleep = sleep

    async def run(self, task: ResearchTask) -> AgentOutput:  # noqa: ARG002
        await asyncio.sleep(self._sleep)
        return AgentOutput(agent_name=self.agent_name, status="ok")  # unreachable


class SyncOkMockAgent:
    """Synchronous agent — runner wraps it in its own thread pool."""

    agent_name = "sync_ok_mock_agent"

    def run(self, task: ResearchTask) -> AgentOutput:  # noqa: ARG002
        # Minimal sync work — no long sleep so the thread pool drains quickly
        _ = sum(range(1000))
        return AgentOutput(
            agent_name=self.agent_name,
            status="ok",
            summary="sync mock ok",
            confidence=0.8,
        )


class WrongNameMockAgent:
    """Returns an AgentOutput with a mismatched agent_name."""

    agent_name = "correct_name"

    async def run(self, task: ResearchTask) -> AgentOutput:  # noqa: ARG002
        return AgentOutput(
            agent_name="WRONG_NAME",
            status="ok",
            confidence=0.5,
        )


class PoisonedOutputMockAgent:
    """Returns an AgentOutput with a non-JSON-safe datetime injected post-init."""

    agent_name = "poisoned_output_mock_agent"

    async def run(self, task: ResearchTask) -> AgentOutput:  # noqa: ARG002
        out = AgentOutput(agent_name=self.agent_name, status="ok", confidence=0.5)
        # Bypass __post_init__ by directly assigning after construction.
        # This simulates an agent that smuggles a non-serialisable value.
        out.as_of = datetime.now()  # type: ignore[assignment]  # intentionally wrong type
        return out
