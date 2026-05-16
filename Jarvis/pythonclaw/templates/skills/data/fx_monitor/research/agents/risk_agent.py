"""
Phase 9 Step 3d — RiskAgent

Runs AFTER Phase-1 agents (fx_agent, news_agent, macro_agent).
Reads all Phase-1 AgentOutputs and synthesises contradictions and risks
into a single AgentOutput.

No external data fetches. No LLM calls. No side effects.
No executor needed — pure CPU-bound synthesis.

Signature differs from Phase-1 agents:
    await risk_agent.run(task, phase1_outputs)
The coordinator calls this explicitly after runner.run_many() completes.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone

try:
    from ..schema import AgentOutput, Finding, ResearchTask, now_iso
    from ..agent_audit import audit_agent_start, audit_agent_end, audit_agent_error
except ImportError:
    from schema import AgentOutput, Finding, ResearchTask, now_iso  # type: ignore[no-redef]
    from agent_audit import audit_agent_start, audit_agent_end, audit_agent_error  # type: ignore[no-redef]

_STALE_SOURCE_HOURS = 48.0
_STALE_SOURCE_HOURS_BY_AGENT = {
    "macro_agent": 24.0 * 7,
}
_RISK_ENTITIES: list[str] = ["AUD", "CNY", "CNYAUD"]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(value: str | None, now: datetime) -> float | None:
    dt = _parse_iso(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)


def _direction_fields(direction: str | None) -> dict[str, str | None]:
    if direction == "bullish_aud":
        return {
            "direction_for_aud": "bullish",
            "direction_for_cny": "bearish",
            "direction_for_pair": direction,
        }
    if direction == "bearish_aud":
        return {
            "direction_for_aud": "bearish",
            "direction_for_cny": "bullish",
            "direction_for_pair": direction,
        }
    if direction == "neutral":
        return {
            "direction_for_aud": "neutral",
            "direction_for_cny": "neutral",
            "direction_for_pair": direction,
        }
    return {
        "direction_for_aud": None,
        "direction_for_cny": None,
        "direction_for_pair": None,
    }


def _risk_finding(
    *,
    key: str,
    summary: str,
    direction: str | None = "neutral",
    category: str = "risk",
    subcategory: str = "",
    importance: float = 0.7,
    evidence_basis: str = "",
) -> Finding:
    return Finding(
        key=key,
        summary=summary,
        direction=direction,
        evidence_score=0.65,
        category=category,
        subcategory=subcategory,
        entities=list(_RISK_ENTITIES),
        importance=importance,
        time_sensitivity="realtime",
        time_horizon="run_level",
        evidence_basis=evidence_basis,
        **_direction_fields(direction),
    )


class RiskAgent:
    """
    Risk synthesis agent.

    Protocol:
        agent.agent_name                          → str
        await agent.run(task, phase1_outputs)     → AgentOutput

    Note: second argument phase1_outputs distinguishes this from Phase-1
    agents. The coordinator handles this explicitly.
    """

    agent_name: str = "risk_agent"

    async def run(
        self,
        task: ResearchTask,
        phase1_outputs: list[AgentOutput],
    ) -> AgentOutput:
        """Synthesise risks and contradictions from Phase-1 outputs."""
        t0 = time.monotonic()
        task_id = getattr(task, "task_id", "")
        input_finding_count = sum(len(o.findings) for o in phase1_outputs)
        audit_agent_start(
            self.agent_name, task_id,
            input_agent_count=len(phase1_outputs),
            input_finding_count=input_finding_count,
        )
        retrieved_at = now_iso()
        now_dt = datetime.now(timezone.utc)
        findings: list[Finding] = []
        risks: list[str] = []
        missing: list[str] = []

        # ── Collect all directions from Phase-1 findings ──────────────────────
        all_findings: list[Finding] = []
        for output in phase1_outputs:
            stale_threshold = _STALE_SOURCE_HOURS_BY_AGENT.get(
                output.agent_name,
                _STALE_SOURCE_HOURS,
            )
            all_findings.extend(output.findings)
            risks.extend(output.risks)
            for item in output.missing_data:
                missing.append(f"{output.agent_name}: {item}")

            if output.findings and not output.sources:
                findings.append(_risk_finding(
                    key=f"missing_sources_{output.agent_name}",
                    summary=f"{output.agent_name} 返回了发现项但没有提供来源引用。",
                    direction="neutral",
                    category="data_gap",
                    subcategory="missing_sources",
                    evidence_basis=f"source_agent={output.agent_name}; reason=no_source_refs",
                ))
                missing.append(f"missing_sources:{output.agent_name}")

            ages: list[tuple[str, float]] = []
            output_age = _age_hours(output.as_of, now_dt)
            if output_age is not None:
                ages.append(("as_of", output_age))
            for src in output.sources:
                timestamp = src.published_at or src.retrieved_at
                if not timestamp:
                    findings.append(Finding(
                        key=f"missing_source_timestamp_{output.agent_name}",
                        summary=(
                            f"{output.agent_name} 的来源缺少 published_at/retrieved_at："
                            f"{src.title or src.source or 'source'}"
                        ),
                        direction="neutral",
                        evidence_score=0.65,
                        category="data_gap",
                        subcategory="missing_source_timestamp",
                        entities=list(_RISK_ENTITIES),
                        importance=0.65,
                        time_sensitivity="realtime",
                        time_horizon="run_level",
                        evidence_basis=f"source_agent={output.agent_name}; reason=missing_timestamp",
                    ))
                    missing.append(f"missing_source_timestamp:{output.agent_name}")
                    continue
                src_age = _age_hours(timestamp, now_dt)
                if src_age is not None:
                    ages.append((src.title or src.source or "source", src_age))
            stale = [(label, age) for label, age in ages if age > stale_threshold]
            if stale:
                label, age = max(stale, key=lambda item: item[1])
                findings.append(_risk_finding(
                    key=f"stale_data_{output.agent_name}",
                    summary=(
                        f"{output.agent_name} 存在可能过期的数据：{label} "
                        f"约 {age:.0f} 小时前。"
                    ),
                    direction="neutral",
                    category="data_gap",
                    subcategory="stale_data",
                    evidence_basis=f"source_agent={output.agent_name}; age_hours={age:.0f}; threshold_hours={stale_threshold:.0f}",
                ))
                risks.append(f"{output.agent_name} 数据可能过期，需复核最新来源")
                missing.append(f"stale_data:{output.agent_name}")

        direction_agents = [
            o.agent_name
            for o in phase1_outputs
            if any(f.direction and f.direction != "neutral" for f in o.findings)
        ]

        direction_counts = Counter(
            f.direction
            for f in all_findings
            if f.direction and f.direction != "neutral"
        )
        bullish = direction_counts.get("bullish_aud", 0)
        bearish = direction_counts.get("bearish_aud", 0)

        # ── Contradiction detection ───────────────────────────────────────────
        if bullish > 0 and bearish > 0:
            findings.append(_risk_finding(
                key="signal_contradiction",
                summary=(
                    f"多空信号矛盾：{bullish} 个指标偏多 AUD，{bearish} 个偏空 AUD。"
                    "当前方向不明确，不宜仅凭单一信号判断。"
                ),
                direction="neutral",
                category="risk",
                subcategory="contradiction",
                importance=0.85,
                evidence_basis=f"source_agents={','.join(direction_agents)}; bullish={bullish}; bearish={bearish}",
            ))

        # ── Agent failure / data-gap findings ────────────────────────────────
        failed  = [o.agent_name for o in phase1_outputs if o.status == "error"]
        partial = [o.agent_name for o in phase1_outputs if o.status == "partial"]

        if failed:
            missing.extend(failed)
            findings.append(_risk_finding(
                key="data_gap_failed_agents",
                summary=f"以下数据源不可用，研究可能不完整：{', '.join(failed)}",
                direction="neutral",
                category="data_gap",
                subcategory="failed_agents",
                evidence_basis=f"source_agents={','.join(failed)}; reason=agent_error",
            ))
        if partial:
            missing.extend(partial)
            findings.append(_risk_finding(
                key="data_gap_partial_agents",
                summary=f"以下数据源部分缺失：{', '.join(partial)}",
                direction="neutral",
                category="data_gap",
                subcategory="partial_agents",
                evidence_basis=f"source_agents={','.join(partial)}; reason=agent_partial",
            ))

        # ── Low-confidence warning ────────────────────────────────────────────
        avg_confidence = (
            sum(o.confidence for o in phase1_outputs) / len(phase1_outputs)
            if phase1_outputs else 0.0
        )
        if avg_confidence < 0.4:
            risks.append(
                f"数据整体置信度较低（平均 {avg_confidence:.0%}），结论参考价值有限"
            )

        low_conf_agents = [
            f"{o.agent_name}:{o.confidence:.0%}"
            for o in phase1_outputs
            if o.confidence < 0.4
        ]
        if low_conf_agents:
            findings.append(_risk_finding(
                key="low_confidence_outputs",
                summary=f"以下代理置信度偏低：{', '.join(low_conf_agents)}",
                direction="neutral",
                category="data_gap",
                subcategory="low_confidence_outputs",
                evidence_basis=f"source_agents={','.join(low_conf_agents)}; reason=confidence_below_40pct",
            ))

        # ── Dominant trend summary ────────────────────────────────────────────
        if bullish > bearish and bullish > 0:
            findings.append(_risk_finding(
                key="dominant_signal",
                summary=(
                    f"多数指标（{bullish}/{bullish + bearish}）偏向 AUD 走强，"
                    "但不构成操作建议"
                ),
                direction="bullish_aud",
                category="risk",
                subcategory="dominant_signal",
                evidence_basis=f"source_agents={','.join(direction_agents)}; bullish={bullish}; bearish={bearish}",
            ))
        elif bearish > bullish and bearish > 0:
            findings.append(_risk_finding(
                key="dominant_signal",
                summary=(
                    f"多数指标（{bearish}/{bullish + bearish}）偏向 AUD 走弱，"
                    "但不构成操作建议"
                ),
                direction="bearish_aud",
                category="risk",
                subcategory="dominant_signal",
                evidence_basis=f"source_agents={','.join(direction_agents)}; bullish={bullish}; bearish={bearish}",
            ))
        elif not all_findings:
            findings.append(_risk_finding(
                key="no_data",
                summary="所有上游代理均未返回有效数据，风险分析无法完成",
                direction="neutral",
                category="data_gap",
                subcategory="no_data",
                evidence_basis="reason=no_upstream_findings",
            ))
        else:
            findings.append(_risk_finding(
                key="dominant_signal",
                summary="当前信号方向不明确，多空力量相对均衡",
                direction="neutral",
                category="risk",
                subcategory="dominant_signal",
                evidence_basis=f"source_agents={','.join(direction_agents)}; bullish={bullish}; bearish={bearish}",
            ))

        status = "ok" if phase1_outputs else "error"
        latency_ms = int((time.monotonic() - t0) * 1000)

        contradiction_count = 1 if (bullish > 0 and bearish > 0) else 0
        audit_agent_end(
            self.agent_name, task_id, status,
            latency_ms=latency_ms,
            finding_count=len(findings),
            contradiction_count=contradiction_count,
            data_gap_count=len([f for f in findings if f.category == "data_gap"]),
        )

        return AgentOutput(
            agent_name=self.agent_name,
            status=status,
            summary=findings[0].summary if findings else "风险分析无输出",
            findings=findings,
            sources=[],   # risk_agent synthesises — no new external sources
            as_of=retrieved_at,
            confidence=min(avg_confidence, 0.70),
            risks=list(dict.fromkeys(risks)),   # deduplicate, preserve order
            missing_data=list(dict.fromkeys(missing)),
            latency_ms=latency_ms,
            token_usage={},
            regulatory_flags=[],
        )
