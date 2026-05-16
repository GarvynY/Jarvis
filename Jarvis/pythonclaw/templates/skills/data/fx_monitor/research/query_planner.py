"""
Phase 10.6D — QueryBucketPlanner.

Centralised query planning for research agents.
Generates structured QueryBucket lists without replacing existing agent query logic.
Agents may optionally consult the plan; no agent is required to use it yet.

Provides:
  - QueryBucket dataclass
  - QueryPlan dataclass
  - build_query_plan(task, preset, safe_context) -> QueryPlan
  - query_plan_debug_summary(plan) -> list[dict]
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    from schema import (
        FX_CNYAUD_PRESET,
        ResearchPreset,
        ResearchTask,
        SafeUserContext,
        now_iso,
    )
except ImportError:
    from .schema import (  # type: ignore[no-redef]
        FX_CNYAUD_PRESET,
        ResearchPreset,
        ResearchTask,
        SafeUserContext,
        now_iso,
    )


# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_MAX_RESULTS: int = 5
DEEP_MODE_MAX_RESULTS: int = 10


@dataclass
class BudgetConfig:
    max_active_buckets: int = 8
    max_total_queries: int = 12
    max_total_results: int = 24


BUDGET_CONFIGS: dict[str, BudgetConfig] = {
    "normal": BudgetConfig(max_active_buckets=8, max_total_queries=20, max_total_results=40),
    "deep": BudgetConfig(max_active_buckets=12, max_total_queries=36, max_total_results=80),
    "debug": BudgetConfig(max_active_buckets=15, max_total_queries=999, max_total_results=999),
}

# ── QueryBucket ───────────────────────────────────────────────────────────────


@dataclass
class QueryBucket:
    bucket_name: str
    agent_name: str
    category: str
    queries: list[str]
    max_results: int = DEFAULT_MAX_RESULTS
    freshness_hours: int | None = None
    source_preference: list[str] = field(default_factory=list)
    priority: float = 0.5
    budget_weight: float = 1.0
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── QueryPlan ─────────────────────────────────────────────────────────────────


@dataclass
class QueryPlan:
    task_id: str
    preset_name: str
    buckets: list[QueryBucket] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    mode: str = "normal"

    def active_buckets(self) -> list[QueryBucket]:
        """Select buckets within budget, sorted by priority desc, budget_weight desc, name."""
        budget = BUDGET_CONFIGS.get(self.mode, BUDGET_CONFIGS["normal"])
        candidates = sorted(
            [b for b in self.buckets if b.enabled],
            key=lambda b: (-b.priority, -b.budget_weight, b.bucket_name),
        )

        selected: list[QueryBucket] = []
        total_queries = 0
        total_results = 0

        for b in candidates:
            if len(selected) >= budget.max_active_buckets:
                break
            new_queries = total_queries + len(b.queries)
            new_results = total_results + b.max_results
            if new_queries > budget.max_total_queries:
                break
            if new_results > budget.max_total_results:
                break
            selected.append(b)
            total_queries = new_queries
            total_results = new_results

        return selected

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "preset_name": self.preset_name,
            "mode": self.mode,
            "created_at": self.created_at,
            "buckets": [b.to_dict() for b in self.buckets],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ── focus_pair normalization ───────────────────────────────────────────────────

_PAIR_SLASH_RE = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
_PAIR_CONCAT_RE = re.compile(r"^[A-Z]{6}$")

_PRESET_DEFAULT_PAIRS: dict[str, str] = {
    "fx_cnyaud": "CNY/AUD",
}


def normalize_focus_pair(raw: str | None, preset_name: str = "") -> str:
    """Normalize and validate focus_pair, returning a safe 'XXX/YYY' string.

    Accepts:
      - 'CNY/AUD' (already canonical)
      - 'CNYAUD' or 'AUDCNY' (6 uppercase letters → split at 3)

    Rejects anything else (free text, injections, punctuation beyond '/').
    Falls back to preset default or 'CNY/AUD'.
    """
    fallback = _PRESET_DEFAULT_PAIRS.get(preset_name, "CNY/AUD")
    if not raw:
        return fallback

    cleaned = raw.strip().upper()

    if _PAIR_SLASH_RE.match(cleaned):
        return cleaned

    if _PAIR_CONCAT_RE.match(cleaned):
        return f"{cleaned[:3]}/{cleaned[3:]}"

    return fallback


# ── fx_cnyaud bucket definitions ──────────────────────────────────────────────

def _fx_cnyaud_buckets(task: ResearchTask) -> list[QueryBucket]:
    pair = normalize_focus_pair(task.focus_pair, task.preset_name)
    assets = task.focus_assets or ["CNY", "AUD"]

    return [
        # Policy signal buckets
        QueryBucket(
            bucket_name="policy_rba",
            agent_name="policy_signal_agent",
            category="policy_signal",
            queries=[
                "RBA interest rate decision",
                "Reserve Bank of Australia monetary policy statement",
                "RBA cash rate outlook",
            ],
            freshness_hours=72,
            source_preference=["rba.gov.au", "reuters.com", "bloomberg.com"],
            priority=0.9,
            budget_weight=1.2,
        ),
        QueryBucket(
            bucket_name="policy_pboc",
            agent_name="policy_signal_agent",
            category="policy_signal",
            queries=[
                "PBoC interest rate decision",
                "中国人民银行 LPR 利率",
                "PBoC monetary policy",
            ],
            freshness_hours=72,
            source_preference=["pbc.gov.cn", "reuters.com", "xinhua.net"],
            priority=0.9,
            budget_weight=1.2,
        ),
        QueryBucket(
            bucket_name="policy_fed",
            agent_name="policy_signal_agent",
            category="policy_signal",
            queries=[
                "Federal Reserve interest rate decision",
                "Fed funds rate outlook",
                "FOMC statement",
            ],
            freshness_hours=72,
            source_preference=["federalreserve.gov", "reuters.com", "bloomberg.com"],
            priority=0.85,
            budget_weight=1.0,
        ),

        # Market driver buckets
        QueryBucket(
            bucket_name="market_aud_usd",
            agent_name="market_drivers_agent",
            category="market_driver",
            queries=[
                "AUD/USD exchange rate today",
                "Australian dollar forecast",
            ],
            freshness_hours=24,
            source_preference=["reuters.com", "bloomberg.com", "finance.yahoo.com"],
            priority=0.8,
            budget_weight=1.0,
        ),
        QueryBucket(
            bucket_name="market_usd_cny",
            agent_name="market_drivers_agent",
            category="market_driver",
            queries=[
                "USD/CNY exchange rate today",
                "人民币汇率 中间价",
            ],
            freshness_hours=24,
            source_preference=["reuters.com", "boc.cn", "pbc.gov.cn"],
            priority=0.8,
            budget_weight=1.0,
        ),
        QueryBucket(
            bucket_name="market_usd_cnh",
            agent_name="market_drivers_agent",
            category="market_driver",
            queries=[
                "USD/CNH offshore yuan rate",
                "离岸人民币 CNH",
            ],
            freshness_hours=24,
            source_preference=["reuters.com", "bloomberg.com"],
            priority=0.7,
            budget_weight=0.8,
        ),
        QueryBucket(
            bucket_name="market_dxy",
            agent_name="market_drivers_agent",
            category="market_driver",
            queries=[
                "US dollar index DXY today",
                "美元指数 DXY",
            ],
            freshness_hours=24,
            source_preference=["finance.yahoo.com", "reuters.com"],
            priority=0.7,
            budget_weight=0.8,
        ),
        QueryBucket(
            bucket_name="market_vix",
            agent_name="market_drivers_agent",
            category="market_driver",
            queries=[
                "VIX volatility index",
                "market risk sentiment today",
            ],
            freshness_hours=24,
            source_preference=["finance.yahoo.com", "cmegroup.com"],
            priority=0.6,
            budget_weight=0.6,
        ),

        # Commodity buckets
        QueryBucket(
            bucket_name="commodity_iron_ore",
            agent_name="commodity_agent",
            category="commodity_trade",
            queries=[
                "iron ore price today",
                "铁矿石价格 澳大利亚出口",
            ],
            freshness_hours=48,
            source_preference=["reuters.com", "bloomberg.com"],
            priority=0.7,
            budget_weight=0.8,
        ),
        QueryBucket(
            bucket_name="commodity_oil",
            agent_name="commodity_agent",
            category="commodity_trade",
            queries=[
                "crude oil price Brent WTI",
                "原油价格 布伦特",
            ],
            freshness_hours=48,
            source_preference=["reuters.com", "bloomberg.com"],
            priority=0.6,
            budget_weight=0.6,
        ),

        # News buckets
        QueryBucket(
            bucket_name="news_fx_events",
            agent_name="news_agent",
            category="news_event",
            queries=[
                f"{pair} exchange rate news",
                "澳元 人民币 汇率 新闻",
                "AUD CNY forex news",
            ],
            freshness_hours=48,
            source_preference=["reuters.com", "bloomberg.com", "afr.com"],
            priority=0.8,
            budget_weight=1.0,
        ),
        QueryBucket(
            bucket_name="news_geopolitics",
            agent_name="news_agent",
            category="geopolitical_event",
            queries=[
                "Australia China trade relations",
                "中澳关系 贸易",
                "geopolitical risk Asia Pacific",
            ],
            freshness_hours=72,
            source_preference=["reuters.com", "bbc.com", "theguardian.com"],
            priority=0.65,
            budget_weight=0.8,
        ),

        # Macro indicator buckets
        QueryBucket(
            bucket_name="macro_australia",
            agent_name="macro_indicator_agent",
            category="macro_indicator",
            queries=[
                "Australia GDP CPI employment data",
                "澳大利亚 经济数据 GDP CPI",
                "Australia economic indicators",
            ],
            freshness_hours=168,
            source_preference=["abs.gov.au", "rba.gov.au", "reuters.com"],
            priority=0.7,
            budget_weight=1.0,
        ),
        QueryBucket(
            bucket_name="macro_china",
            agent_name="macro_indicator_agent",
            category="macro_indicator",
            queries=[
                "China GDP CPI PMI data",
                "中国 经济数据 GDP CPI PMI",
                "China economic indicators",
            ],
            freshness_hours=168,
            source_preference=["pbc.gov.cn", "xinhua.net", "reuters.com"],
            priority=0.7,
            budget_weight=1.0,
        ),
        QueryBucket(
            bucket_name="macro_us",
            agent_name="macro_indicator_agent",
            category="macro_indicator",
            queries=[
                "US non-farm payrolls CPI GDP",
                "美国 非农就业 CPI",
                "US economic data latest",
            ],
            freshness_hours=168,
            source_preference=["federalreserve.gov", "reuters.com", "bloomberg.com"],
            priority=0.65,
            budget_weight=0.8,
        ),
    ]


# ── Planner ───────────────────────────────────────────────────────────────────

_PRESET_BUCKET_REGISTRY: dict[str, Any] = {
    "fx_cnyaud": _fx_cnyaud_buckets,
}


def build_query_plan(
    task: ResearchTask,
    preset: ResearchPreset | None = None,
    safe_context: SafeUserContext | None = None,
    *,
    mode: str = "normal",
) -> QueryPlan:
    """Build a deterministic query plan for the given task and preset.

    Args:
        task: The research task.
        preset: Optional preset override; if None, looked up by task.preset_name.
        safe_context: Optional user context for relevance hints (never leaked into queries).
        mode: "normal" or "deep" — deep mode increases max_results.

    Returns:
        QueryPlan with structured buckets.
    """
    preset_name = task.preset_name or (preset.name if preset else "")
    bucket_fn = _PRESET_BUCKET_REGISTRY.get(preset_name)

    if bucket_fn is None:
        return QueryPlan(
            task_id=task.task_id,
            preset_name=preset_name,
            buckets=[],
            mode=mode,
        )

    buckets = bucket_fn(task)

    if mode == "deep":
        for b in buckets:
            b.max_results = DEEP_MODE_MAX_RESULTS

    plan = QueryPlan(
        task_id=task.task_id,
        preset_name=preset_name,
        buckets=buckets,
        mode=mode,
    )
    return plan


# ── Debug support ─────────────────────────────────────────────────────────────


def query_plan_debug_summary(plan: QueryPlan) -> dict[str, Any]:
    """Return a safe debug summary of the query plan (no user data leaked)."""
    active = plan.active_buckets()
    budget = BUDGET_CONFIGS.get(plan.mode, BUDGET_CONFIGS["normal"])
    total_queries = sum(len(b.queries) for b in active)
    total_max_results = sum(b.max_results for b in active)

    return {
        "total_buckets": len(plan.buckets),
        "active_buckets": len(active),
        "total_queries": total_queries,
        "total_max_results": total_max_results,
        "budget_mode": plan.mode,
        "budget_limited": len(active) < len([b for b in plan.buckets if b.enabled]),
        "buckets": [
            {
                "bucket_name": b.bucket_name,
                "agent_name": b.agent_name,
                "category": b.category,
                "max_results": b.max_results,
                "enabled": b.enabled,
            }
            for b in active
        ],
    }
