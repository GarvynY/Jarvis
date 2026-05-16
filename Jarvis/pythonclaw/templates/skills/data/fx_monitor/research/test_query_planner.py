#!/usr/bin/env python3
"""
Phase 10.6D — QueryBucketPlanner 测试（含预算选择）。

验证：
  1. fx_cnyaud 查询计划包含预期的桶
  2. 已禁用的桶被排除
  3. max_results 和 freshness_hours 已设置
  4. 规划器是确定性的
  5. safe_context 不泄露私有信息到查询中
  6. deep mode 调整 max_results
  7. 未知 preset 返回空计划
  8. debug summary 安全且完整
  9. active_buckets 按优先级/预算权重选择
  10. normal 模式桶数少于 15
  11. max_total_results 强制执行
  12. deep 模式多于 normal
  13. debug 模式可显示全部桶
  14. 输出顺序确定

运行：
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_query_planner.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import ResearchTask, SafeUserContext, FX_CNYAUD_PRESET  # noqa: E402
from query_planner import (  # noqa: E402
    BudgetConfig,
    BUDGET_CONFIGS,
    QueryBucket,
    QueryPlan,
    build_query_plan,
    query_plan_debug_summary,
    DEFAULT_MAX_RESULTS,
    DEEP_MODE_MAX_RESULTS,
)


def _make_task(task_id: str = "t-plan", preset_name: str = "fx_cnyaud") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        preset_name=preset_name,
        focus_pair="CNY/AUD",
        focus_assets=["CNY", "AUD"],
    )


EXPECTED_BUCKETS = [
    "policy_rba",
    "policy_pboc",
    "policy_fed",
    "market_aud_usd",
    "market_usd_cny",
    "market_usd_cnh",
    "market_dxy",
    "market_vix",
    "commodity_iron_ore",
    "commodity_oil",
    "news_fx_events",
    "news_geopolitics",
    "macro_australia",
    "macro_china",
    "macro_us",
]


def test_fx_cnyaud_contains_expected_buckets() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    names = [b.bucket_name for b in plan.buckets]
    for expected in EXPECTED_BUCKETS:
        assert expected in names, f"Missing bucket: {expected}, got {names}"
    assert len(plan.buckets) == 15
    print("  fx_cnyaud 包含所有 15 个桶  OK")


def test_disabled_buckets_excluded() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    plan.buckets[0].enabled = False
    plan.buckets[1].enabled = False
    active = plan.active_buckets()
    disabled_names = {plan.buckets[0].bucket_name, plan.buckets[1].bucket_name}
    for b in active:
        assert b.bucket_name not in disabled_names, f"Disabled bucket appeared: {b.bucket_name}"
    print("  已禁用桶被排除             OK")


def test_max_results_and_freshness_set() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    for b in plan.buckets:
        assert b.max_results > 0, f"{b.bucket_name}: max_results should be > 0"
        assert b.freshness_hours is None or b.freshness_hours > 0, (
            f"{b.bucket_name}: freshness_hours invalid"
        )
    policy_rba = next(b for b in plan.buckets if b.bucket_name == "policy_rba")
    assert policy_rba.freshness_hours == 72
    assert policy_rba.max_results == DEFAULT_MAX_RESULTS
    print("  max_results/freshness 设置正确 OK")


def test_planner_deterministic() -> None:
    task = _make_task()
    plan1 = build_query_plan(task, FX_CNYAUD_PRESET)
    plan2 = build_query_plan(task, FX_CNYAUD_PRESET)
    names1 = [b.bucket_name for b in plan1.active_buckets()]
    names2 = [b.bucket_name for b in plan2.active_buckets()]
    assert names1 == names2
    print("  规划器确定性               OK")


def test_safe_context_no_leak() -> None:
    task = _make_task()
    ctx = SafeUserContext(
        target_rate=4.85,
        purpose="tuition",
        preferred_topics=["fx_price", "policy_signal"],
    )
    plan = build_query_plan(task, FX_CNYAUD_PRESET, safe_context=ctx)
    all_queries = []
    for b in plan.buckets:
        all_queries.extend(b.queries)
    combined = " ".join(all_queries).lower()
    assert "4.85" not in combined, "target_rate leaked into queries"
    assert "tuition" not in combined, "purpose leaked into queries"
    print("  safe_context 无泄露        OK")


def test_deep_mode_increases_max_results() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="deep")
    assert plan.mode == "deep"
    for b in plan.buckets:
        assert b.max_results == DEEP_MODE_MAX_RESULTS, (
            f"{b.bucket_name}: expected {DEEP_MODE_MAX_RESULTS}, got {b.max_results}"
        )
    print("  deep mode max_results=10   OK")


def test_unknown_preset_empty_plan() -> None:
    task = ResearchTask(task_id="t-unknown", preset_name="nonexistent_preset")
    plan = build_query_plan(task)
    assert plan.buckets == []
    assert plan.preset_name == "nonexistent_preset"
    print("  未知 preset 返回空计划     OK")


def test_debug_summary_safe() -> None:
    task = _make_task()
    ctx = SafeUserContext(target_rate=4.85, purpose="tuition")
    plan = build_query_plan(task, FX_CNYAUD_PRESET, safe_context=ctx)
    summary = query_plan_debug_summary(plan)
    assert "total_buckets" in summary
    assert "active_buckets" in summary
    assert "total_queries" in summary
    assert "total_max_results" in summary
    assert "budget_mode" in summary
    assert "budget_limited" in summary
    assert "buckets" in summary
    for item in summary["buckets"]:
        assert "bucket_name" in item
        assert "agent_name" in item
        assert "category" in item
        assert "max_results" in item
        assert "enabled" in item
        assert "queries" not in item
        assert "source_preference" not in item
    serialized = str(summary)
    assert "4.85" not in serialized
    assert "tuition" not in serialized
    print("  debug summary 安全且完整   OK")


def test_plan_serialization() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    d = plan.to_dict()
    assert d["task_id"] == "t-plan"
    assert d["preset_name"] == "fx_cnyaud"
    assert len(d["buckets"]) == 15
    json_str = plan.to_json()
    assert "policy_rba" in json_str
    assert "fx_cnyaud" in json_str
    print("  plan 序列化正常            OK")


def test_bucket_categories_valid() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    valid_cats = {
        "policy_signal", "market_driver", "commodity_trade",
        "news_event", "geopolitical_event", "macro_indicator",
    }
    for b in plan.buckets:
        assert b.category in valid_cats, f"{b.bucket_name}: invalid category {b.category}"
    print("  所有桶 category 合法       OK")


def test_priority_and_budget_weight() -> None:
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    for b in plan.buckets:
        assert 0.0 < b.priority <= 1.0, f"{b.bucket_name}: priority out of range"
        assert 0.0 < b.budget_weight <= 2.0, f"{b.bucket_name}: budget_weight out of range"
    policy_rba = next(b for b in plan.buckets if b.bucket_name == "policy_rba")
    assert policy_rba.priority == 0.9
    assert policy_rba.budget_weight == 1.2
    print("  priority/budget_weight 合理 OK")


# ── Phase 10.6D fix: budget-aware selection tests ─────────────────────────────

def test_active_selects_by_priority_not_list_order() -> None:
    """active_buckets sorts by priority desc, budget_weight desc, name — not insertion order."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET)
    active = plan.active_buckets()
    for i in range(len(active) - 1):
        a, b = active[i], active[i + 1]
        assert (a.priority, a.budget_weight) >= (b.priority, b.budget_weight) or (
            a.priority == b.priority and a.budget_weight == b.budget_weight
            and a.bucket_name <= b.bucket_name
        ), f"Order violation: {a.bucket_name}(p={a.priority},w={a.budget_weight}) before {b.bucket_name}"
    print("  按优先级/权重排序选择      OK")


def test_normal_mode_fewer_than_15() -> None:
    """Normal mode budget limits active buckets to fewer than all 15."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
    active = plan.active_buckets()
    assert len(active) < 15, f"Normal should have < 15 active, got {len(active)}"
    assert len(active) <= BUDGET_CONFIGS["normal"].max_active_buckets
    print("  normal 模式少于 15 个桶    OK")


def test_max_total_results_enforced() -> None:
    """Total max_results across active buckets respects budget."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
    active = plan.active_buckets()
    total_results = sum(b.max_results for b in active)
    budget = BUDGET_CONFIGS["normal"]
    assert total_results <= budget.max_total_results, (
        f"Total results {total_results} exceeds budget {budget.max_total_results}"
    )
    print("  max_total_results 强制执行 OK")


def test_max_total_queries_enforced() -> None:
    """Total queries across active buckets respects budget."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
    active = plan.active_buckets()
    total_queries = sum(len(b.queries) for b in active)
    budget = BUDGET_CONFIGS["normal"]
    assert total_queries <= budget.max_total_queries, (
        f"Total queries {total_queries} exceeds budget {budget.max_total_queries}"
    )
    print("  max_total_queries 强制执行 OK")


def test_deep_more_than_normal() -> None:
    """Deep mode allows more active buckets than normal."""
    task = _make_task()
    normal_plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
    deep_plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="deep")
    normal_active = len(normal_plan.active_buckets())
    deep_active = len(deep_plan.active_buckets())
    assert deep_active >= normal_active, (
        f"Deep ({deep_active}) should have >= normal ({normal_active}) active buckets"
    )
    print("  deep 多于 normal           OK")


def test_debug_mode_shows_all() -> None:
    """Debug mode can show all 15 buckets."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="debug")
    active = plan.active_buckets()
    assert len(active) == 15, f"Debug should show all 15, got {len(active)}"
    print("  debug 模式显示全部 15 桶   OK")


def test_output_order_deterministic() -> None:
    """Multiple calls produce identical active_buckets order."""
    task = _make_task()
    orders = []
    for _ in range(5):
        plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
        orders.append([b.bucket_name for b in plan.active_buckets()])
    for i in range(1, 5):
        assert orders[i] == orders[0], f"Run {i} differs from run 0"
    print("  输出顺序确定性             OK")


def test_high_priority_buckets_selected_first() -> None:
    """policy_rba (priority=0.9) always selected over commodity_oil (priority=0.6)."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
    active_names = [b.bucket_name for b in plan.active_buckets()]
    assert "policy_rba" in active_names, f"policy_rba should be active: {active_names}"
    assert "policy_pboc" in active_names, f"policy_pboc should be active: {active_names}"
    print("  高优先级桶优先选择         OK")


def test_debug_summary_reports_budget_stats() -> None:
    """Debug summary includes budget metadata."""
    task = _make_task()
    plan = build_query_plan(task, FX_CNYAUD_PRESET, mode="normal")
    summary = query_plan_debug_summary(plan)
    assert summary["total_buckets"] == 15
    assert summary["active_buckets"] < 15
    assert summary["budget_mode"] == "normal"
    assert summary["budget_limited"] is True
    assert summary["total_queries"] > 0
    assert summary["total_max_results"] > 0
    assert summary["total_max_results"] <= BUDGET_CONFIGS["normal"].max_total_results
    print("  debug summary 含预算统计   OK")


def run_all() -> None:
    tests = [
        test_fx_cnyaud_contains_expected_buckets,
        test_disabled_buckets_excluded,
        test_max_results_and_freshness_set,
        test_planner_deterministic,
        test_safe_context_no_leak,
        test_deep_mode_increases_max_results,
        test_unknown_preset_empty_plan,
        test_debug_summary_safe,
        test_plan_serialization,
        test_bucket_categories_valid,
        test_priority_and_budget_weight,
        # Budget-aware selection tests
        test_active_selects_by_priority_not_list_order,
        test_normal_mode_fewer_than_15,
        test_max_total_results_enforced,
        test_max_total_queries_enforced,
        test_deep_more_than_normal,
        test_debug_mode_shows_all,
        test_output_order_deterministic,
        test_high_priority_buckets_selected_first,
        test_debug_summary_reports_budget_stats,
    ]
    print("Phase 10.6D — QueryBucketPlanner 测试")
    print("=" * 50)
    for test_fn in tests:
        test_fn()
    print("=" * 50)
    print(f"全部 {len(tests)} 项测试通过。")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        print(f"\n失败: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
