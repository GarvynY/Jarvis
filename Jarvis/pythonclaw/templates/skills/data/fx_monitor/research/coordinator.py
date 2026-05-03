"""
Phase 9 Step 4 — Research coordinator.

Strict architectural rules
──────────────────────────
1. coordinator.py is the ONLY Phase 9 module that calls build_safe_user_context().
2. run_research() assembles the workflow from PRESET_REGISTRY + AGENT_REGISTRY only —
   never hard-codes CNY/AUD or any other pair in the core control flow.
3. Phase-1 agents run in parallel via LocalAsyncRunner.run_many().
   A failure in one agent never interrupts the others.
4. RiskAgent runs sequentially after phase-1, reading only phase1_outputs.
5. The return value is (ResearchTask, list[AgentOutput], CostEstimate).
   The supervisor downstream receives only AgentOutputs — it never queries
   external data or reads the user profile directly.

No imports of: Telegram, memory, raw DB, Tavily, or external APIs.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# ── sys.path bootstrap (coordinator lives 6 levels below Jarvis root) ─────────
# coordinator.py → research/ → fx_monitor/ → data/ → skills/ → templates/
#               → pythonclaw/ → Jarvis/
_JARVIS_ROOT = Path(__file__).parents[6]
if str(_JARVIS_ROOT) not in sys.path:
    sys.path.insert(0, str(_JARVIS_ROOT))

try:
    from .schema import (
        AgentOutput, CostEstimate, PRESET_REGISTRY,
        ResearchTask, SafeUserContext,
    )
    from .runner import LocalAsyncRunner
    from .agents import FXAgent, MacroAgent, NewsAgent, RiskAgent
except ImportError:
    from schema import (  # type: ignore[no-redef]
        AgentOutput, CostEstimate, PRESET_REGISTRY,
        ResearchTask, SafeUserContext,
    )
    from runner import LocalAsyncRunner  # type: ignore[no-redef]
    from agents import FXAgent, MacroAgent, NewsAgent, RiskAgent  # type: ignore[no-redef]

# Module-level import so tests can patch coordinator.build_safe_user_context.
# Falls back to a no-op that returns {} when pythonclaw is not on sys.path.
try:
    from pythonclaw.core.personalization import build_safe_user_context
except Exception:  # noqa: BLE001
    def build_safe_user_context(*_args: object, **_kwargs: object) -> dict:  # type: ignore[misc]
        return {}


# ── Agent registry ────────────────────────────────────────────────────────────
# Maps the agent-name strings declared in ResearchPreset.default_agents to the
# concrete class.  Coordinator instantiates a fresh object per request.

AGENT_REGISTRY: dict[str, type] = {
    "fx_agent":    FXAgent,
    "news_agent":  NewsAgent,
    "macro_agent": MacroAgent,
    # "sentiment_agent": SentimentAgent,   ← future
}

# ── Per-preset task defaults ──────────────────────────────────────────────────
# Keeps CNY/AUD out of the main control flow.
# Any preset that needs default focus_assets / focus_pair adds an entry here.

_PRESET_TASK_DEFAULTS: dict[str, dict[str, Any]] = {
    "fx_cnyaud": {
        "research_topic": "CNY/AUD 外汇研究",
        "focus_assets":   ["CNY", "AUD"],
        "focus_pair":     "CNY/AUD",
    },
}

# ── Haiku pricing (approximate, USD per token) ────────────────────────────────
# claude-haiku-4-5-20251001: $0.25 / 1M input, $1.25 / 1M output
_HAIKU_INPUT_PER_TOKEN  = 0.25  / 1_000_000
_HAIKU_OUTPUT_PER_TOKEN = 1.25  / 1_000_000


# ── CostEstimate helper ───────────────────────────────────────────────────────

def _compute_cost(
    all_outputs: list[AgentOutput],
    phase1_latencies: list[int],
    risk_latency: int,
) -> CostEstimate:
    """Compute approximate cost from token_usage fields in all outputs."""
    llm_calls     = 0
    total_input   = 0
    total_output  = 0

    for out in all_outputs:
        usage = out.token_usage or {}
        prompt  = usage.get("prompt_tokens",     0)
        comp    = usage.get("completion_tokens",  0)
        if prompt > 0 or comp > 0:
            llm_calls    += 1
            total_input  += prompt
            total_output += comp

    estimated_tokens   = total_input + total_output
    estimated_cost_usd = (
        total_input  * _HAIKU_INPUT_PER_TOKEN
        + total_output * _HAIKU_OUTPUT_PER_TOKEN
    )

    # phase-1 agents ran in parallel → wall time ≈ max latency
    phase1_wall = max(phase1_latencies) if phase1_latencies else 0
    total_latency_ms = phase1_wall + risk_latency

    return CostEstimate(
        llm_calls=llm_calls,
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        total_latency_ms=total_latency_ms,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_research(
    preset_name: str,
    user_id: str | int,
    research_topic: str | None = None,
    focus_assets: list[str] | None = None,
    focus_pair: str | None = None,
    custom_subtopics: list[str] | None = None,
    time_horizon: str | None = None,
) -> tuple[ResearchTask, list[AgentOutput], CostEstimate]:
    """
    Run a full research workflow for the given preset and user.

    Parameters
    ----------
    preset_name:      Key in PRESET_REGISTRY (e.g. "fx_cnyaud").
    user_id:          Telegram user id — passed to build_safe_user_context().
    research_topic:   Overrides preset default if provided.
    focus_assets:     Overrides preset default if provided.
    focus_pair:       Overrides preset default if provided.
    custom_subtopics: Passed through to ResearchTask.
    time_horizon:     Overrides preset default if provided.

    Returns
    -------
    (task, all_outputs, cost_estimate)

    all_outputs = phase1_outputs + [risk_output]  (risk_output is always last)
    """

    # ── 1. Resolve preset ─────────────────────────────────────────────────────
    preset = PRESET_REGISTRY.get(preset_name)
    if preset is None:
        known = list(PRESET_REGISTRY.keys())
        error_out = AgentOutput.make_error(
            "coordinator",
            f"Unknown preset {preset_name!r}. Known presets: {known}",
        )
        empty_task = ResearchTask(preset_name=preset_name)
        return empty_task, [error_out], CostEstimate()

    # ── 2. Load safe user context (short blocking SQLite read) ──────────────────
    # coordinator.py is the ONLY Phase 9 module that calls build_safe_user_context.
    # Called synchronously — it is a brief local SQLite read, not a network call.
    # Using run_in_executor(None, ...) with the default executor causes asyncio.run()
    # to block at shutdown waiting for the default executor to drain; avoid that.
    raw_ctx: dict[str, Any] = {}
    try:
        raw_ctx = build_safe_user_context(user_id)
    except Exception:  # noqa: BLE001
        # Profile store unavailable (e.g. no DB in tests) → use defaults.
        pass

    safe_ctx = SafeUserContext.from_dict(raw_ctx)

    # ── 3. Build ResearchTask ─────────────────────────────────────────────────
    defaults = _PRESET_TASK_DEFAULTS.get(preset_name, {})

    task = ResearchTask(
        preset_name      = preset.name,
        research_type    = preset.research_type,
        safe_user_context= safe_ctx,
        research_topic   = research_topic   or defaults.get("research_topic", preset.description),
        focus_assets     = focus_assets     or list(defaults.get("focus_assets", [])),
        focus_pair       = focus_pair       or defaults.get("focus_pair"),
        custom_subtopics = list(custom_subtopics or []),
        time_horizon     = time_horizon     or preset.default_time_horizon,
    )

    # ── 4. Instantiate phase-1 agents from registry ───────────────────────────
    phase1_agents: list[Any] = []
    skipped: list[AgentOutput] = []

    for agent_name in preset.default_agents:
        cls = AGENT_REGISTRY.get(agent_name)
        if cls is None:
            skipped.append(AgentOutput.make_error(
                agent_name,
                f"agent {agent_name!r} not found in AGENT_REGISTRY",
            ))
        else:
            phase1_agents.append(cls())

    # ── 5. Run phase-1 agents in parallel ─────────────────────────────────────
    phase1_outputs: list[AgentOutput]
    async with LocalAsyncRunner() as runner:
        phase1_outputs = await runner.run_many(task, phase1_agents)

    phase1_outputs = skipped + phase1_outputs   # skipped agents prepended

    # ── 6. Run RiskAgent sequentially after phase-1 ───────────────────────────
    risk_agent = RiskAgent()
    t_risk = time.monotonic()
    try:
        risk_output = await risk_agent.run(task, phase1_outputs)
    except Exception as exc:  # noqa: BLE001
        risk_output = AgentOutput.make_error(
            "risk_agent",
            f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.monotonic() - t_risk) * 1000),
        )
    risk_latency = int((time.monotonic() - t_risk) * 1000)

    # ── 7. Compute CostEstimate ───────────────────────────────────────────────
    all_outputs = phase1_outputs + [risk_output]
    phase1_latencies = [o.latency_ms for o in phase1_outputs]
    cost = _compute_cost(all_outputs, phase1_latencies, risk_latency)

    return task, all_outputs, cost
