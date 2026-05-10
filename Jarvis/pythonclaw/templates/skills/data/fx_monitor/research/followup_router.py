"""Phase 10E -- lightweight follow-up router recommendations.

The MVP is recommendation-only. It never starts agents, never calls an LLM,
and never loops. Optional execution can be wired later behind
ENABLE_FOLLOWUP_EXECUTION.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from .schema import (
        AgentOutput,
        ContextPack,
        Finding,
        FindingCategory,
        FollowupRequest,
        PRESET_REGISTRY,
        ResearchTask,
    )
except ImportError:
    from schema import (  # type: ignore[no-redef]
        AgentOutput,
        ContextPack,
        Finding,
        FindingCategory,
        FollowupRequest,
        PRESET_REGISTRY,
        ResearchTask,
    )


ENABLE_FOLLOWUP_EXECUTION: bool = False
MAX_FOLLOWUP_DEPTH: int = 1
MAX_FOLLOWUP_AGENTS: int = 2
FOLLOWUP_TIMEOUT_SECONDS: int = 30
MAX_RECOMMENDATIONS: int = 6

_HIGH_CONFLICT_THRESHOLD = 2
_HIGH_IMPORTANCE_THRESHOLD = 0.75
_LOW_CONFIDENCE_THRESHOLD = 0.45
_STALE_HOURS_DEFAULT = 72.0
_STALE_HOURS_REALTIME = 12.0

_CATEGORY_AGENT_MAP: dict[str, str] = {
    FindingCategory.FX_PRICE.value: "fx_agent",
    FindingCategory.MACRO.value: "macro_agent",
    FindingCategory.NEWS_EVENT.value: "news_agent",
    FindingCategory.RISK.value: "risk_agent",
    "fx_trend": "fx_agent",
    "macro_indicator": "macro_agent",
}


def _clamp_priority(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 4)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_hours(ts: str, now: datetime | None = None) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _target_agent_for_category(category: str, fallback: str = "macro_agent") -> str:
    return _CATEGORY_AGENT_MAP.get((category or "").lower(), fallback)


def _target_category(output: AgentOutput, finding: Finding | None = None) -> str:
    if finding is not None and finding.category:
        return str(finding.category).lower()
    if output.agent_name == "fx_agent":
        return FindingCategory.FX_PRICE.value
    if output.agent_name == "news_agent":
        return FindingCategory.NEWS_EVENT.value
    if output.agent_name == "risk_agent":
        return FindingCategory.RISK.value
    return FindingCategory.MACRO.value


def _conflict_count(conflict_summary: Any) -> int:
    if conflict_summary is None:
        return 0
    if isinstance(conflict_summary, dict):
        return int(conflict_summary.get("conflict_count") or len(conflict_summary.get("conflict_pairs") or []))
    value = getattr(conflict_summary, "conflict_count", None)
    if value is not None:
        return int(value)
    return len(getattr(conflict_summary, "conflict_pairs", []) or [])


def _make_request(
    *,
    target_agent: str,
    target_category: str,
    reason: str,
    priority: float,
    suggested_query: str,
    trigger_type: str,
    max_depth: int = MAX_FOLLOWUP_DEPTH,
) -> FollowupRequest:
    return FollowupRequest(
        target_agent=target_agent,
        target_category=(target_category or "").lower(),
        reason=reason[:240],
        priority=_clamp_priority(priority),
        suggested_query=suggested_query[:300],
        max_depth=min(MAX_FOLLOWUP_DEPTH, max(0, int(max_depth))),
        trigger_type=trigger_type,
    )


def _dedupe_and_limit(requests: list[FollowupRequest]) -> list[FollowupRequest]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[FollowupRequest] = []
    for req in sorted(requests, key=lambda r: r.priority, reverse=True):
        key = (req.trigger_type, req.target_agent, req.target_category)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(req)
        if len(deduped) >= MAX_RECOMMENDATIONS:
            break
    return deduped


def _coverage_requests(task: ResearchTask, context_pack: ContextPack | None) -> list[FollowupRequest]:
    if context_pack is None:
        return []
    preset = PRESET_REGISTRY.get(task.preset_name)
    expected_agents = list(getattr(preset, "default_agents", []) or [])
    if not expected_agents:
        return []
    covered = {agent for agent, count in (context_pack.coverage or {}).items() if int(count) > 0}
    missing = [agent for agent in expected_agents if agent not in covered]
    if not missing:
        return []
    ratio = len(covered) / max(1, len(expected_agents))
    if ratio >= 0.75:
        return []
    agent = missing[0]
    category = _target_category(AgentOutput(agent_name=agent, status="partial"))
    return [_make_request(
        target_agent=agent,
        target_category=category,
        reason=f"ContextPack 覆盖率偏低：{len(covered)}/{len(expected_agents)} 个核心代理有证据覆盖。",
        priority=0.65 + (0.75 - ratio) * 0.2,
        suggested_query=f"补充 {task.research_topic or task.preset_name} 中 {category} 相关证据。",
        trigger_type="low_section_coverage",
    )]


def generate_followup_requests(
    task: ResearchTask,
    outputs: list[AgentOutput],
    context_pack: ContextPack | None = None,
    conflict_summary: Any = None,
) -> list[FollowupRequest]:
    """Return bounded follow-up recommendations without executing agents."""
    requests: list[FollowupRequest] = []
    requests.extend(_coverage_requests(task, context_pack))

    conflicts = _conflict_count(conflict_summary)
    if conflicts >= _HIGH_CONFLICT_THRESHOLD:
        requests.append(_make_request(
            target_agent="risk_agent",
            target_category=FindingCategory.RISK.value,
            reason=f"检测到 {conflicts} 组方向冲突，需要进一步核验冲突来源和权重。",
            priority=0.7 + min(0.25, conflicts * 0.05),
            suggested_query=f"核验 {task.research_topic or task.focus_pair or task.preset_name} 的多空冲突、证据来源与不确定性。",
            trigger_type="high_conflict_count",
        ))

    for output in outputs:
        category = _target_category(output)
        if output.status != "ok" or output.missing_data:
            missing = "; ".join(output.missing_data[:3]) if output.missing_data else (output.error or output.status)
            requests.append(_make_request(
                target_agent=output.agent_name,
                target_category=category,
                reason=f"{output.agent_name} 数据不完整：{missing}",
                priority=0.75 if output.status == "error" else 0.62,
                suggested_query=f"补充 {output.agent_name} 缺失数据，并验证其对 {task.research_topic or task.focus_pair or task.preset_name} 的影响。",
                trigger_type="agent_data_missing",
            ))

        age = _age_hours(output.as_of)
        if age is not None and age >= _STALE_HOURS_DEFAULT:
            requests.append(_make_request(
                target_agent=output.agent_name,
                target_category=category,
                reason=f"{output.agent_name} 输出已过时约 {age:.0f} 小时。",
                priority=0.55 + min(0.25, age / 240.0),
                suggested_query=f"刷新 {output.agent_name} 的最新证据和时间敏感结论。",
                trigger_type="stale_evidence",
            ))

        for finding in output.findings:
            f_category = _target_category(output, finding)
            target_agent = _target_agent_for_category(f_category, fallback=output.agent_name)
            low_finding_conf = (
                finding.evidence_score is not None
                and float(finding.evidence_score) <= _LOW_CONFIDENCE_THRESHOLD
            )
            if finding.importance >= _HIGH_IMPORTANCE_THRESHOLD and (
                output.confidence <= _LOW_CONFIDENCE_THRESHOLD or low_finding_conf
            ):
                requests.append(_make_request(
                    target_agent=target_agent,
                    target_category=f_category,
                    reason=f"高重要性但低置信度信号：{finding.key}",
                    priority=0.7 + min(0.2, finding.importance * 0.1),
                    suggested_query=f"围绕“{finding.summary[:120]}”补充高质量来源和反向证据。",
                    trigger_type="high_importance_low_confidence",
                ))

            f_age = _age_hours(output.as_of)
            stale_cutoff = (
                _STALE_HOURS_REALTIME
                if finding.time_sensitivity == "realtime"
                else _STALE_HOURS_DEFAULT
            )
            if f_age is not None and f_age >= stale_cutoff:
                requests.append(_make_request(
                    target_agent=target_agent,
                    target_category=f_category,
                    reason=f"时间敏感证据可能过时：{finding.key}",
                    priority=0.6 if finding.time_sensitivity == "realtime" else 0.5,
                    suggested_query=f"刷新“{finding.summary[:120]}”相关证据。",
                    trigger_type="stale_evidence",
                ))

    return _dedupe_and_limit(requests)


def execute_followup_requests(
    requests: list[FollowupRequest],
    *,
    enable_followup_execution: bool = ENABLE_FOLLOWUP_EXECUTION,
) -> list[AgentOutput]:
    """Placeholder execution gate.

    Phase 10E intentionally does not wire agent execution. Returning [] when
    disabled keeps recommendation mode side-effect free; callers can later add
    bounded execution behind this gate.
    """
    if not enable_followup_execution:
        return []
    return []

