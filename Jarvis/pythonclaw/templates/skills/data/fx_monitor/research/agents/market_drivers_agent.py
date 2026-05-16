"""
Phase 10.6E — MarketDriversAgent MVP.

Uses DataSourceRegistry to fetch 8 market data items and generates structured
findings with rule-based direction interpretation. No LLM calls.

Feature flag: _ENABLE_MARKET_DRIVERS_AGENT (default False).
When disabled, coordinator does not register this agent and existing
/fx_research output is unchanged.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import Any

_FETCH_TOTAL_TIMEOUT_SEC: int = 30

try:
    from ..schema import AgentOutput, Finding, FindingCategory, ResearchTask, SourceRef, now_iso
except ImportError:
    from schema import AgentOutput, Finding, FindingCategory, ResearchTask, SourceRef, now_iso  # type: ignore[no-redef]

try:
    from data_sources import DataSourceConfig, DataSourceRegistry, DataSourceResult
    from source_metadata import SourceMetadata
    from agent_audit import audit_agent_start, audit_agent_event, audit_agent_end, audit_agent_error
except ImportError:
    try:
        from ..data_sources import DataSourceConfig, DataSourceRegistry, DataSourceResult  # type: ignore[no-redef]
        from ..source_metadata import SourceMetadata  # type: ignore[no-redef]
        from ..agent_audit import audit_agent_start, audit_agent_event, audit_agent_end, audit_agent_error  # type: ignore[no-redef]
    except ImportError:
        import sys
        from pathlib import Path
        _research_dir = str(Path(__file__).resolve().parent.parent)
        if _research_dir not in sys.path:
            sys.path.insert(0, _research_dir)
        from data_sources import DataSourceConfig, DataSourceRegistry, DataSourceResult  # type: ignore[no-redef]
        from source_metadata import SourceMetadata  # type: ignore[no-redef]
        from agent_audit import audit_agent_start, audit_agent_event, audit_agent_end, audit_agent_error  # type: ignore[no-redef]


_ENABLE_MARKET_DRIVERS_AGENT: bool = False

_ALL_ITEM_KEYS: list[str] = [
    "fx.aud_usd",
    "fx.usd_cny",
    "fx.usd_cnh",
    "market.dxy",
    "market.vix",
    "commodity.oil",
    "commodity.copper",
    "commodity.iron_ore",
]

_DIRECTION_RULES: dict[str, dict[str, Any]] = {
    "fx.aud_usd": {
        "positive_for": "AUD",
        "direction_field": "direction_for_aud",
        "bullish_condition": "up",
        "summary_template": "AUD/USD {direction_word} {change_pct:.2f}% 至 {value:.4f}",
    },
    "fx.usd_cny": {
        "positive_for": "CNY",
        "direction_field": "direction_for_cny",
        "bullish_condition": "down",
        "summary_template": "USD/CNY {direction_word} {change_pct:.2f}% 至 {value:.4f}",
    },
    "fx.usd_cnh": {
        "positive_for": "CNY",
        "direction_field": "direction_for_cny",
        "bullish_condition": "down",
        "summary_template": "USD/CNH 离岸 {direction_word} {change_pct:.2f}% 至 {value:.4f}",
    },
    "market.dxy": {
        "positive_for": "USD",
        "direction_field": "direction_for_aud",
        "bullish_condition": "up",
        "summary_template": "美元指数 DXY {direction_word} {change_pct:.2f}% 至 {value:.2f}",
    },
    "market.vix": {
        "positive_for": "neutral",
        "direction_field": None,
        "bullish_condition": None,
        "summary_template": "VIX 波动率指数 {direction_word} 至 {value:.2f}",
    },
    "commodity.oil": {
        "positive_for": "neutral",
        "direction_field": None,
        "bullish_condition": None,
        "summary_template": "原油价格 (WTI) {direction_word} {change_pct:.2f}% 至 ${value:.2f}",
    },
    "commodity.copper": {
        "positive_for": "AUD",
        "direction_field": "direction_for_aud",
        "bullish_condition": "up",
        "summary_template": "铜价 {direction_word} {change_pct:.2f}% 至 ${value:.4f}",
    },
    "commodity.iron_ore": {
        "positive_for": "AUD",
        "direction_field": "direction_for_aud",
        "bullish_condition": "up",
        "summary_template": "铁矿石价格 {direction_word}",
    },
}

_CATEGORY_MAP: dict[str, str] = {
    "fx.aud_usd": FindingCategory.MARKET_DRIVER,
    "fx.usd_cny": FindingCategory.MARKET_DRIVER,
    "fx.usd_cnh": FindingCategory.MARKET_DRIVER,
    "market.dxy": FindingCategory.MARKET_DRIVER,
    "market.vix": FindingCategory.MARKET_DRIVER,
    "commodity.oil": FindingCategory.COMMODITY_TRADE,
    "commodity.copper": FindingCategory.COMMODITY_TRADE,
    "commodity.iron_ore": FindingCategory.COMMODITY_TRADE,
}


def _interpret_direction(item_key: str, result: DataSourceResult) -> dict[str, Any]:
    """Rule-based direction interpretation. Returns direction metadata."""
    rule = _DIRECTION_RULES.get(item_key, {})
    if not rule or result.status != "ok" or result.change_pct is None:
        return {"direction": "neutral", "direction_for_aud": None, "direction_for_cny": None}

    change = result.change_pct
    threshold = 0.1

    if abs(change) < threshold:
        move = "flat"
    elif change > 0:
        move = "up"
    else:
        move = "down"

    direction_field = rule.get("direction_field")
    bullish_cond = rule.get("bullish_condition")
    positive_for = rule.get("positive_for", "neutral")

    direction_for_aud = None
    direction_for_cny = None

    if direction_field and bullish_cond and move != "flat":
        is_bullish = (move == bullish_cond)
        if positive_for == "AUD":
            direction_for_aud = "bullish" if is_bullish else "bearish"
        elif positive_for == "CNY":
            direction_for_cny = "bullish" if is_bullish else "bearish"
        elif positive_for == "USD":
            direction_for_aud = "bearish" if is_bullish else "bullish"

    overall = "neutral"
    if direction_for_aud:
        overall = f"{direction_for_aud}_aud"
    elif direction_for_cny:
        overall = f"{direction_for_cny}_cny"

    return {
        "direction": overall,
        "direction_for_aud": direction_for_aud,
        "direction_for_cny": direction_for_cny,
        "move": move,
    }


def _build_summary(item_key: str, result: DataSourceResult, move: str) -> str:
    """Build a Chinese summary sentence for a data result."""
    rule = _DIRECTION_RULES.get(item_key, {})
    template = rule.get("summary_template", "{item_key}: {value}")

    direction_words = {"up": "上涨", "down": "下跌", "flat": "持平"}
    direction_word = direction_words.get(move, "变动")

    value = result.value if result.value is not None else 0
    change_pct = abs(result.change_pct) if result.change_pct is not None else 0

    try:
        return template.format(
            direction_word=direction_word,
            value=value,
            change_pct=change_pct,
            item_key=item_key,
        )
    except (KeyError, ValueError):
        return f"{item_key}: {value}"


def _importance_from_change(change_pct: float | None) -> float:
    """Map change magnitude to importance score [0.3, 0.9]."""
    if change_pct is None:
        return 0.3
    mag = abs(change_pct)
    if mag < 0.1:
        return 0.3
    if mag < 0.5:
        return 0.5
    if mag < 1.0:
        return 0.6
    if mag < 2.0:
        return 0.7
    return min(0.9, 0.7 + (mag - 2.0) * 0.05)


class MarketDriversAgent:
    """Phase-1 agent that fetches market data from DataSourceRegistry."""

    agent_name: str = "market_drivers_agent"

    def __init__(self, config: DataSourceConfig | None = None):
        self._config = config
        self._registry: DataSourceRegistry | None = None

    def _get_registry(self) -> DataSourceRegistry:
        if self._registry is None:
            self._registry = DataSourceRegistry(self._config)
        return self._registry

    async def run(self, task: ResearchTask) -> AgentOutput:
        t0 = time.monotonic()
        task_id = getattr(task, "task_id", "")
        audit_agent_start(self.agent_name, task_id, requested_items=_ALL_ITEM_KEYS)
        try:
            output = await self._run_impl(task, t0)
            audit_agent_end(
                self.agent_name, task_id, output.status,
                latency_ms=output.latency_ms,
                finding_count=len(output.findings),
                data_gap_count=len(output.missing_data),
                confidence=output.confidence,
            )
            return output
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            audit_agent_error(self.agent_name, task_id, f"{type(exc).__name__}: {exc}", latency_ms=latency_ms)
            return AgentOutput.make_error(
                self.agent_name,
                f"{type(exc).__name__}: {exc}",
                latency_ms=latency_ms,
            )

    def _fetch_all_concurrent(self) -> dict[str, DataSourceResult]:
        """Fetch all items concurrently with per-item threads and total timeout."""
        registry = self._get_registry()
        results: dict[str, DataSourceResult] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(registry.fetch, key): key
                for key in _ALL_ITEM_KEYS
            }
            done, not_done = concurrent.futures.wait(
                futures, timeout=_FETCH_TOTAL_TIMEOUT_SEC,
            )
            for fut in done:
                key = futures[fut]
                try:
                    results[key] = fut.result()
                except Exception:
                    results[key] = DataSourceResult(
                        item_key=key, status="missing",
                        data_gap_reason="fetch_thread_exception",
                    )
            for fut in not_done:
                key = futures[fut]
                fut.cancel()
                results[key] = DataSourceResult(
                    item_key=key, status="missing",
                    data_gap_reason="fetch_timeout",
                )

        return results

    async def _run_impl(self, task: ResearchTask, t0: float) -> AgentOutput:
        task_id = getattr(task, "task_id", "")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, self._fetch_all_concurrent)

        audit_agent_event(
            self.agent_name, task_id, "fetch_complete",
            successful_items=[k for k, v in results.items() if v.status == "ok"],
            missing_items=[k for k, v in results.items() if v.status != "ok"],
        )

        findings: list[Finding] = []
        sources: list[SourceRef] = []
        missing_data: list[str] = []

        for item_key in _ALL_ITEM_KEYS:
            result = results[item_key]

            if result.status == "ok":
                audit_agent_event(
                    self.agent_name, task_id, "item_ok",
                    item_key=item_key, provider=result.provider,
                    confidence=result.confidence,
                )
                direction_info = _interpret_direction(item_key, result)
                move = direction_info.get("move", "flat")
                summary = _build_summary(item_key, result, move)

                source_url = ""
                if result.source_metadata and result.source_metadata.url:
                    source_url = result.source_metadata.url
                    sources.append(SourceRef(
                        url=source_url,
                        title=f"{item_key} via {result.provider}",
                        source=result.provider or "market_data",
                        retrieved_at=now_iso(),
                    ))

                findings.append(Finding(
                    key=item_key.replace(".", "_"),
                    summary=summary,
                    direction=direction_info["direction"],
                    category=_CATEGORY_MAP.get(item_key, FindingCategory.MARKET_DRIVER),
                    importance=_importance_from_change(result.change_pct),
                    source_ids=[source_url] if source_url else [],
                    time_sensitivity="realtime",
                    subcategory="market_data",
                    entities=task.focus_assets or ["CNY", "AUD"],
                    direction_for_aud=direction_info.get("direction_for_aud"),
                    direction_for_cny=direction_info.get("direction_for_cny"),
                    evidence_basis=f"{result.provider}:{item_key}",
                ))
            else:
                audit_agent_event(
                    self.agent_name, task_id, "item_gap",
                    item_key=item_key, data_gap_reason=result.data_gap_reason or result.status,
                )
                missing_data.append(f"{item_key}:{result.data_gap_reason or result.status}")
                findings.append(Finding(
                    key=f"{item_key.replace('.', '_')}_gap",
                    summary=f"{item_key} 数据暂不可用: {result.data_gap_reason or result.status}",
                    direction="neutral",
                    category="data_gap",
                    importance=0.1,
                    time_sensitivity="realtime",
                    subcategory="data_gap",
                    entities=task.focus_assets or ["CNY", "AUD"],
                    evidence_basis=f"data_gap:{item_key}",
                ))

        ok_count = sum(1 for r in results.values() if r.status == "ok")
        total = len(_ALL_ITEM_KEYS)
        confidence = round(ok_count / total * 0.85, 2) if total > 0 else 0.0

        status = "ok" if ok_count >= 4 else ("partial" if ok_count >= 1 else "error")

        latency_ms = int((time.monotonic() - t0) * 1000)

        return AgentOutput(
            agent_name=self.agent_name,
            status=status,
            summary=f"市场数据: {ok_count}/{total} 项获取成功",
            findings=findings,
            sources=sources,
            confidence=confidence,
            missing_data=missing_data,
            latency_ms=latency_ms,
        )
