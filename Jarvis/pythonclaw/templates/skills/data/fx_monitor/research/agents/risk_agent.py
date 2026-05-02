"""
risk_agent — stateless risk synthesis agent.

Runs AFTER Phase-1 agents (fx_agent, news_agent, macro_agent).
Reads all Phase-1 AgentOutputs and synthesises contradictions/risks
into a single AgentOutput. Never fetches external data itself.

Accepts ResearchTask + list[AgentOutput], returns AgentOutput.
No side effects. No direct inter-agent communication.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from collections import Counter

from ..schema import (
    AgentOutput,
    Finding,
    ResearchTask,
)

AGENT_NAME = "risk_agent"


def run(task: ResearchTask, phase1_outputs: list[AgentOutput]) -> AgentOutput:
    """
    Synthesise risks and contradictions from Phase-1 outputs.

    Note: signature differs from Phase-1 agents — takes phase1_outputs as
    the second argument. Runner handles this explicitly.
    """
    t0 = time.monotonic()
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    findings: list[Finding] = []
    risks: list[str] = []
    missing: list[str] = []

    # ── Collect all directions from Phase-1 findings ──────────────────────────
    all_findings: list[Finding] = []
    for output in phase1_outputs:
        all_findings.extend(output.findings)
        risks.extend(output.risks)

    direction_counts = Counter(
        f.direction for f in all_findings if f.direction and f.direction != "neutral"
    )
    bullish = direction_counts.get("bullish_aud", 0)
    bearish = direction_counts.get("bearish_aud", 0)

    # ── Contradiction detection ───────────────────────────────────────────────
    if bullish > 0 and bearish > 0:
        findings.append(Finding(
            key="signal_contradiction",
            summary=(
                f"多空信号矛盾：{bullish} 个指标偏多 AUD，{bearish} 个偏空 AUD。"
                "当前方向不明确，不宜仅凭单一信号判断。"
            ),
            direction="neutral",
        ))

    # ── Agent failure risks ───────────────────────────────────────────────────
    failed_agents = [o.agent_name for o in phase1_outputs if o.status == "error"]
    partial_agents = [o.agent_name for o in phase1_outputs if o.status == "partial"]
    if failed_agents:
        missing.extend(failed_agents)
        findings.append(Finding(
            key="data_gap_failed_agents",
            summary=f"以下数据源不可用，研究可能不完整：{', '.join(failed_agents)}",
            direction="neutral",
        ))
    if partial_agents:
        findings.append(Finding(
            key="data_gap_partial_agents",
            summary=f"以下数据源部分缺失：{', '.join(partial_agents)}",
            direction="neutral",
        ))

    # ── Low-confidence warning ────────────────────────────────────────────────
    avg_confidence = (
        sum(o.confidence for o in phase1_outputs) / len(phase1_outputs)
        if phase1_outputs else 0.0
    )
    if avg_confidence < 0.4:
        risks.append(f"数据整体置信度较低（平均 {avg_confidence:.0%}），结论参考价值有限")

    # ── Dominant trend summary ────────────────────────────────────────────────
    if bullish > bearish and bullish > 0:
        findings.append(Finding(
            key="dominant_signal",
            summary=f"多数指标（{bullish}/{bullish+bearish}）偏向 AUD 走强，但不构成操作建议",
            direction="bullish_aud",
        ))
    elif bearish > bullish and bearish > 0:
        findings.append(Finding(
            key="dominant_signal",
            summary=f"多数指标（{bearish}/{bullish+bearish}）偏向 AUD 走弱，但不构成操作建议",
            direction="bearish_aud",
        ))
    elif not all_findings:
        findings.append(Finding(
            key="no_data",
            summary="所有上游代理均未返回有效数据，风险分析无法完成",
            direction="neutral",
        ))
    else:
        findings.append(Finding(
            key="dominant_signal",
            summary="当前信号方向不明确，多空力量相对均衡",
            direction="neutral",
        ))

    status = "ok" if phase1_outputs else "error"
    return AgentOutput(
        agent_name=AGENT_NAME,
        status=status,
        summary=findings[0].summary if findings else "风险分析无输出",
        findings=findings,
        sources=[],  # risk_agent synthesises — no new external sources
        as_of=retrieved_at,
        confidence=min(avg_confidence, 0.7),
        risks=list(dict.fromkeys(risks)),  # deduplicate, preserve order
        missing_data=missing,
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
