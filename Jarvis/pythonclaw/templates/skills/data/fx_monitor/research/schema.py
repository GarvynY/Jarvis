"""
Phase 9 — Generic preset-driven research workflow schema.

Design principle: ResearchPreset drives everything.
The core workflow never hard-codes a research topic.
CNY/AUD lives only in FX_CNYAUD_PRESET.

Rules:
  - All timestamps are ISO 8601 strings — no datetime objects in any field.
  - All dataclasses are JSON-serialisable (no Python-internal objects).
  - Exceptions are captured as error: str | None.
  - validate_status / validate_confidence are enforced in __post_init__,
    not just available as standalone helpers.
  - No imports of Telegram, LLM providers, personalisation, memory,
    web-search, Tavily, or external APIs.
  - AGENT_REGISTRY is NOT defined here — it belongs to the coordinator
    module that wires callable agents to preset agent-name strings.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# ── Timestamp helper ──────────────────────────────────────────────────────────

def now_iso() -> str:
    """Return current UTC time as an ISO 8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_task_id() -> str:
    return str(uuid.uuid4())


# ── Runtime JSON-safety guard ─────────────────────────────────────────────────

def _assert_json_safe(obj: Any, path: str = "root") -> None:
    """Raise TypeError if any value in obj is a datetime (or other non-JSON type).

    Called by to_dict() so callers get an early, explicit error instead of a
    confusing json.dumps failure later.
    """
    if isinstance(obj, datetime):
        raise TypeError(
            f"datetime object found at {path!r}. "
            "Use an ISO 8601 string (e.g. now_iso()) instead."
        )
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_json_safe(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _assert_json_safe(v, f"{path}[{i}]")


# ── Serialisation helpers ─────────────────────────────────────────────────────

def to_dict(obj: Any) -> dict[str, Any]:
    """Convert any dataclass (including nested) to a plain dict.

    Raises TypeError if any field contains a datetime object.
    """
    d = asdict(obj)
    _assert_json_safe(d)
    return d


def to_json(obj: Any, *, indent: int | None = 2, ensure_ascii: bool = False) -> str:
    """Convert any dataclass to a JSON string."""
    return json.dumps(to_dict(obj), indent=indent, ensure_ascii=ensure_ascii)


# ── Validation helpers ────────────────────────────────────────────────────────

_VALID_STATUSES = {"ok", "partial", "error"}

_FIXED_DISCLAIMER = (
    "⚠️ 本简报仅供参考，不构成任何投资建议或换汇操作建议。"
    "外汇市场存在风险，请结合自身情况做出判断。"
)


def validate_status(status: str) -> str:
    """Raise ValueError if status is not 'ok' / 'partial' / 'error'."""
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Must be one of {_VALID_STATUSES}"
        )
    return status


def validate_confidence(value: float) -> float:
    """Raise ValueError if value is outside [0.0, 1.0]."""
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"Confidence {value!r} must be in range [0.0, 1.0]")
    return value


# ─────────────────────────────────────────────────────────────────────────────
# 1. SafeUserContext
#    Mirrors the whitelisted output of build_safe_user_context().
#    Agents receive this — never a raw profile or telegram_user_id.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SafeUserContext:
    target_rate: float | None = None
    alert_threshold: float | None = None
    purpose: str | None = None             # "tuition" / "living" / "investment" / "general"
    risk_level: str = "unknown"            # matches build_safe_user_context() contract
    preferred_summary_style: str = "standard"   # "brief" / "standard" / "detailed"
    preferred_topics: list[str] = field(default_factory=list)
    privacy_level: str = "standard"

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SafeUserContext":
        return cls(
            target_rate=d.get("target_rate"),
            alert_threshold=d.get("alert_threshold"),
            purpose=d.get("purpose"),
            risk_level=d.get("risk_level", "unknown"),
            preferred_summary_style=d.get("preferred_summary_style", "standard"),
            preferred_topics=list(d.get("preferred_topics") or []),
            privacy_level=d.get("privacy_level", "standard"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. ResearchPreset
#    Defines one research type. Registered in PRESET_REGISTRY.
#    Future presets (equity, macro-global, …) add entries here without
#    touching coordinator or runner logic.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResearchPreset:
    # ── Required ──────────────────────────────────────────────────────────────
    name: str                           # "fx_cnyaud"
    research_type: str                  # "fx" / "equity" / "macro" / "custom"
    default_agents: list[str]           # agent names coordinator will dispatch
    report_sections: list[str]          # ordered section titles for ResearchBrief
    banned_terms: list[str]             # supervisor must never emit these
    default_time_horizon: str           # "short_term" / "medium_term" / "long_term"

    # ── Optional metadata ─────────────────────────────────────────────────────
    description: str = ""
    output_language: str = "zh-CN"      # primary language of the generated brief
    default_region: str = ""            # geographic focus, e.g. "CN-AU", "US", "GLOBAL"

    # ── Agent lists (for future presets that distinguish required vs optional) ─
    required_agents: list[str] = field(default_factory=list)
    # ^ agents that must succeed; coordinator raises if any is missing
    optional_agents: list[str] = field(default_factory=list)
    # ^ agents that run if available; coordinator tolerates their absence

    # ── Named data sources this preset expects ────────────────────────────────
    data_sources: list[str] = field(default_factory=list)
    # e.g. ["fetch_rate.py", "google_news_rss", "yfinance"]

    # ── Default task parameters for this preset ───────────────────────────────
    task_defaults: dict[str, Any] = field(default_factory=dict)
    # e.g. {"research_topic": "...", "focus_assets": [...], "focus_pair": "..."}

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchPreset":
        return cls(
            name=d["name"],
            research_type=d["research_type"],
            default_agents=list(d.get("default_agents") or []),
            report_sections=list(d.get("report_sections") or []),
            banned_terms=list(d.get("banned_terms") or []),
            default_time_horizon=d.get("default_time_horizon", "short_term"),
            description=d.get("description", ""),
            output_language=d.get("output_language", "zh-CN"),
            default_region=d.get("default_region", ""),
            required_agents=list(d.get("required_agents") or []),
            optional_agents=list(d.get("optional_agents") or []),
            data_sources=list(d.get("data_sources") or []),
            task_defaults=dict(d.get("task_defaults") or {}),
        )


# ── MVP preset ────────────────────────────────────────────────────────────────
# CNY/AUD must appear ONLY here — not in any dataclass definition or core logic.

FX_CNYAUD_PRESET = ResearchPreset(
    name="fx_cnyaud",
    research_type="fx",
    default_agents=["fx_agent", "news_agent", "macro_agent"],
    report_sections=["汇率事实", "新闻驱动", "宏观信号", "风险与矛盾"],
    banned_terms=[
        "建议买入", "建议卖出", "换汇时机", "立即操作",
        "应该买", "应该卖", "最佳时机",
    ],
    default_time_horizon="short_term",
    description="CNY/AUD 外汇研究（留学生换汇场景）",
    output_language="zh-CN",
    default_region="CN-AU",
    required_agents=["fx_agent"],
    optional_agents=["news_agent", "macro_agent"],
    data_sources=["fetch_rate.py", "google_news_rss", "yfinance"],
    task_defaults={
        "research_topic": "CNY/AUD 外汇研究",
        "focus_assets": ["CNY", "AUD"],
        "focus_pair": "CNY/AUD",
    },
)

# Coordinator resolves preset by name.
# Future presets: add an entry here — no other file changes required.
PRESET_REGISTRY: dict[str, ResearchPreset] = {
    "fx_cnyaud": FX_CNYAUD_PRESET,
    # "equity_asx":   EQUITY_ASX_PRESET,    ← future
    # "macro_global": MACRO_GLOBAL_PRESET,  ← future
}

# NOTE: AGENT_REGISTRY is intentionally NOT defined here.
# The schema layer only declares types and the preset/section structure.
# The coordinator module owns the mapping from agent-name strings to callables:
#   coordinator.py → AGENT_REGISTRY: dict[str, Callable[[ResearchTask], AgentOutput]]


# ─────────────────────────────────────────────────────────────────────────────
# 3. ResearchTask
#    Built once by coordinator, passed unchanged to every agent.
#    Agents must not modify it. Contains SafeUserContext, not a user id.
#
#    Queue contract: to_dict() produces the canonical queue message body.
#    Workers deserialise with ResearchTask.from_dict().
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResearchTask:
    # ── Identity ──────────────────────────────────────────────────────────────
    task_id: str = field(default_factory=_new_task_id)
    requested_at: str = field(default_factory=now_iso)   # ISO 8601 string, never datetime

    # ── Preset reference ──────────────────────────────────────────────────────
    preset_name: str = ""
    research_type: str = ""              # copied from preset for convenience
    research_topic: str = ""

    # ── Asset / instrument fields (optional — non-FX presets leave these empty)
    focus_assets: list[str] = field(default_factory=list)    # ["AUD", "CNY"]
    focus_pair: str | None = None                            # "CNY/AUD"; None for non-FX
    custom_subtopics: list[str] = field(default_factory=list)

    # ── Time horizon ──────────────────────────────────────────────────────────
    time_horizon: str = "short_term"

    # ── User context (whitelisted safe fields only) ───────────────────────────
    safe_user_context: SafeUserContext = field(default_factory=SafeUserContext)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_preset(
        cls,
        preset: ResearchPreset,
        safe_user_context: SafeUserContext | None = None,
        **overrides: Any,
    ) -> "ResearchTask":
        """Build a task directly from a preset. Coordinator uses this."""
        defaults = dict(preset.task_defaults or {})
        task_kwargs: dict[str, Any] = {
            "preset_name": preset.name,
            "research_type": preset.research_type,
            "research_topic": defaults.get("research_topic", preset.description),
            "focus_assets": list(defaults.get("focus_assets") or []),
            "focus_pair": defaults.get("focus_pair"),
            "custom_subtopics": list(defaults.get("custom_subtopics") or []),
            "time_horizon": defaults.get("time_horizon", preset.default_time_horizon),
            "safe_user_context": safe_user_context or SafeUserContext(),
        }
        task_kwargs.update(overrides)
        return cls(**task_kwargs)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchTask":
        ctx_raw = d.get("safe_user_context") or {}
        return cls(
            task_id=d.get("task_id", _new_task_id()),
            requested_at=d.get("requested_at", now_iso()),
            preset_name=d.get("preset_name", ""),
            research_type=d.get("research_type", ""),
            research_topic=d.get("research_topic", ""),
            focus_assets=list(d.get("focus_assets") or []),
            focus_pair=d.get("focus_pair"),
            custom_subtopics=list(d.get("custom_subtopics") or []),
            time_horizon=d.get("time_horizon", "short_term"),
            safe_user_context=SafeUserContext.from_dict(ctx_raw),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. SourceRef
#    Every piece of external data must be traceable.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SourceRef:
    title: str
    url: str
    source: str               # "google_news_rss" / "yfinance" / "fetch_rate.py" / …
    retrieved_at: str         # ISO 8601 string
    published_at: str | None = None    # ISO 8601 string; None if unknown

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceRef":
        return cls(
            title=d["title"],
            url=d["url"],
            source=d["source"],
            retrieved_at=d["retrieved_at"],
            published_at=d.get("published_at"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Finding
#    One atomic research finding from an agent.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    key: str                              # short identifier, e.g. "rba_rate_hold"
    summary: str                          # one sentence
    direction: str | None = None          # "bullish_aud" / "bearish_aud" / "neutral"
    evidence_score: float | None = None   # reserved: Phase 10 evidence weighting [0,1]
    attention_score: float | None = None  # reserved: Phase 10 attention score [0,1]

    def __post_init__(self) -> None:
        if self.evidence_score is not None:
            validate_confidence(self.evidence_score)   # reuses [0,1] range check
        if self.attention_score is not None:
            validate_confidence(self.attention_score)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        # __post_init__ will validate scores after construction
        return cls(
            key=d["key"],
            summary=d["summary"],
            direction=d.get("direction"),
            evidence_score=d.get("evidence_score"),
            attention_score=d.get("attention_score"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. AgentOutput
#    The only thing agents return. Coordinator and supervisor read this.
#    Agents never communicate directly with each other.
#
#    Queue contract: to_dict() is the canonical worker result body.
#    Coordinator deserialises with AgentOutput.from_dict().
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentOutput:
    agent_name: str
    status: str                           # "ok" / "partial" / "error"
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    as_of: str = field(default_factory=now_iso)    # ISO 8601; data currency timestamp
    confidence: float = 0.0              # validated: must be in [0.0, 1.0]
    risks: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)   # {"prompt": n, "completion": n}
    regulatory_flags: list[str] = field(default_factory=list)   # reserved: compliance agent

    def __post_init__(self) -> None:
        validate_status(self.status)
        validate_confidence(self.confidence)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def make_error(cls, agent_name: str, error: str, latency_ms: int = 0) -> "AgentOutput":
        """Convenience constructor for failed agents — runner uses this."""
        return cls(
            agent_name=agent_name,
            status="error",
            error=error,
            latency_ms=latency_ms,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentOutput":
        # __post_init__ validates status and confidence after construction
        return cls(
            agent_name=d["agent_name"],
            status=d.get("status", "error"),
            summary=d.get("summary", ""),
            findings=[Finding.from_dict(f) for f in d.get("findings") or []],
            sources=[SourceRef.from_dict(s) for s in d.get("sources") or []],
            as_of=d.get("as_of", now_iso()),
            confidence=d.get("confidence", 0.0),
            risks=list(d.get("risks") or []),
            missing_data=list(d.get("missing_data") or []),
            error=d.get("error"),
            latency_ms=d.get("latency_ms", 0),
            token_usage=dict(d.get("token_usage") or {}),
            regulatory_flags=list(d.get("regulatory_flags") or []),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. ResearchSection
#    One section of the final brief, generated by supervisor.
#    Section titles come from preset.report_sections — never hard-coded here.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResearchSection:
    title: str                           # matches one entry in preset.report_sections
    content: str                         # supervisor-generated text
    source_agents: list[str] = field(default_factory=list)   # contributing agent names
    has_data_gap: bool = False           # True if any contributing agent had status != "ok"

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchSection":
        return cls(
            title=d["title"],
            content=d.get("content", ""),
            source_agents=list(d.get("source_agents") or []),
            has_data_gap=bool(d.get("has_data_gap", False)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. CostEstimate
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CostEstimate:
    llm_calls: int = 0
    estimated_tokens: int = 0
    estimated_cost_usd: float = 0.0
    total_latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CostEstimate":
        return cls(
            llm_calls=d.get("llm_calls", 0),
            estimated_tokens=d.get("estimated_tokens", 0),
            estimated_cost_usd=d.get("estimated_cost_usd", 0.0),
            total_latency_ms=d.get("total_latency_ms", 0),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. ResearchBrief
#    Section-based output — not tied to any specific research topic.
#    FX_CNYAUD_PRESET → 4 sections. Future presets define their own sections.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResearchBrief:
    task_id: str
    preset_name: str
    generated_at: str = field(default_factory=now_iso)

    conclusion: str = ""
    sections: list[ResearchSection] = field(default_factory=list)

    user_notes: str = ""          # derived from safe_user_context, never raw profile
    data_gaps: str = ""           # aggregated from sections where has_data_gap=True
    sources_summary: str = ""

    # disclaimer is fixed compliance text — never overridden by external input
    disclaimer: str = field(default=_FIXED_DISCLAIMER)

    agent_statuses: dict[str, str] = field(default_factory=dict)   # {"fx_agent": "ok", …}
    cost_estimate: CostEstimate = field(default_factory=CostEstimate)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchBrief":
        return cls(
            task_id=d["task_id"],
            preset_name=d["preset_name"],
            generated_at=d.get("generated_at", now_iso()),
            conclusion=d.get("conclusion", ""),
            sections=[ResearchSection.from_dict(s) for s in d.get("sections") or []],
            user_notes=d.get("user_notes", ""),
            data_gaps=d.get("data_gaps", ""),
            sources_summary=d.get("sources_summary", ""),
            # disclaimer is always the fixed compliance text — ignored from dict
            agent_statuses=dict(d.get("agent_statuses") or {}),
            cost_estimate=CostEstimate.from_dict(d.get("cost_estimate") or {}),
        )
