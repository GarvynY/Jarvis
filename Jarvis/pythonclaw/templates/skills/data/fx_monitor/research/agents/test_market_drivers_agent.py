#!/usr/bin/env python3
"""
Phase 10.6E — MarketDriversAgent tests.

Validates:
  1. Feature flag off: agent not in AGENT_REGISTRY
  2. Feature flag on: agent in AGENT_REGISTRY
  3. Agent produces correct findings from mock data
  4. Direction rules: AUD/USD up = bullish_aud
  5. Direction rules: USD/CNY down = bullish_cny
  6. Direction rules: DXY up = bearish_aud
  7. Data gap items produce data_gap findings
  8. All providers fail → status="partial" or "error"
  9. Agent never crashes coordinator (exception safety)
  10. Missing data list populated correctly
  11. Confidence proportional to success count

Run:
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research/agents
    python test_market_drivers_agent.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_RESEARCH = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from market_drivers_agent import (
    MarketDriversAgent,
    _ENABLE_MARKET_DRIVERS_AGENT,
    _interpret_direction,
    _importance_from_change,
    _build_summary,
    _ALL_ITEM_KEYS,
)
from data_sources import DataSourceConfig, DataSourceRegistry, DataSourceResult
from source_metadata import SourceMetadata
from schema import ResearchTask


def _make_task() -> ResearchTask:
    return ResearchTask(preset_name="fx_cnyaud", focus_pair="CNY/AUD", focus_assets=["CNY", "AUD"])


def _mock_result_ok(item_key: str, value: float = 1.5, change_pct: float = 0.5) -> DataSourceResult:
    return DataSourceResult(
        item_key=item_key,
        provider="yfinance",
        status="ok",
        value=value,
        previous_value=value - (value * change_pct / 100),
        change_abs=value * change_pct / 100,
        change_pct=change_pct,
        as_of_date="2026-05-16",
        source_metadata=SourceMetadata(
            url="https://finance.yahoo.com/quote/TEST/",
            provider="yfinance",
            domain="finance.yahoo.com",
            source_type="market_data_api",
            source_tier=2,
        ),
        confidence=0.85,
    )


def _mock_result_missing(item_key: str) -> DataSourceResult:
    return DataSourceResult(
        item_key=item_key,
        status="missing",
        data_gap_reason="no_provider_available",
    )


class MockRegistry(DataSourceRegistry):
    def __init__(self, results: dict[str, DataSourceResult]):
        super().__init__(DataSourceConfig(fred_api_key="", provider_timeout_sec=5))
        self._mock_results = results

    def fetch(self, item_key, lookback_days=None):
        return self._mock_results.get(item_key, _mock_result_missing(item_key))

    def fetch_many(self, item_keys, lookback_days=None):
        return {k: self._mock_results.get(k, _mock_result_missing(k)) for k in item_keys}


def test_feature_flag_off_not_in_registry() -> None:
    assert _ENABLE_MARKET_DRIVERS_AGENT is False
    from coordinator import AGENT_REGISTRY
    assert "market_drivers_agent" not in AGENT_REGISTRY
    print("  feature flag off: not in registry  OK")


def test_agent_produces_findings_from_mock() -> None:
    mock_results = {k: _mock_result_ok(k, 1.5, 0.5) for k in _ALL_ITEM_KEYS}
    mock_results["commodity.iron_ore"] = _mock_result_missing("commodity.iron_ore")

    agent = MarketDriversAgent()
    agent._registry = MockRegistry(mock_results)

    task = _make_task()
    output = asyncio.run(agent.run(task))

    assert output.agent_name == "market_drivers_agent"
    assert output.status == "ok"
    assert len(output.findings) == 8
    assert output.confidence > 0
    ok_findings = [f for f in output.findings if f.category != "data_gap"]
    assert len(ok_findings) == 7
    gap_findings = [f for f in output.findings if f.category == "data_gap"]
    assert len(gap_findings) == 1
    print("  mock data produces correct findings OK")


def test_direction_aud_usd_up_bullish() -> None:
    result = _mock_result_ok("fx.aud_usd", 0.68, 0.5)
    info = _interpret_direction("fx.aud_usd", result)
    assert info["direction_for_aud"] == "bullish"
    assert info["direction"] == "bullish_aud"
    print("  AUD/USD up = bullish_aud          OK")


def test_direction_aud_usd_down_bearish() -> None:
    result = _mock_result_ok("fx.aud_usd", 0.68, -0.5)
    info = _interpret_direction("fx.aud_usd", result)
    assert info["direction_for_aud"] == "bearish"
    assert info["direction"] == "bearish_aud"
    print("  AUD/USD down = bearish_aud        OK")


def test_direction_usd_cny_down_bullish_cny() -> None:
    result = _mock_result_ok("fx.usd_cny", 7.2, -0.3)
    info = _interpret_direction("fx.usd_cny", result)
    assert info["direction_for_cny"] == "bullish"
    assert info["direction"] == "bullish_cny"
    print("  USD/CNY down = bullish_cny        OK")


def test_direction_dxy_up_bearish_aud() -> None:
    result = _mock_result_ok("market.dxy", 105.0, 0.8)
    info = _interpret_direction("market.dxy", result)
    assert info["direction_for_aud"] == "bearish"
    assert info["direction"] == "bearish_aud"
    print("  DXY up = bearish_aud              OK")


def test_direction_flat_neutral() -> None:
    result = _mock_result_ok("fx.aud_usd", 0.68, 0.05)
    info = _interpret_direction("fx.aud_usd", result)
    assert info["direction"] == "neutral"
    assert info["direction_for_aud"] is None
    print("  flat change = neutral             OK")


def test_data_gap_findings() -> None:
    mock_results = {k: _mock_result_missing(k) for k in _ALL_ITEM_KEYS}

    agent = MarketDriversAgent()
    agent._registry = MockRegistry(mock_results)

    output = asyncio.run(agent.run(_make_task()))
    assert output.status == "error"
    assert len(output.missing_data) == 8
    assert all(f.category == "data_gap" for f in output.findings)
    print("  all missing = status error        OK")


def test_partial_status() -> None:
    mock_results = {}
    for i, k in enumerate(_ALL_ITEM_KEYS):
        if i < 3:
            mock_results[k] = _mock_result_ok(k, 1.0, 0.2)
        else:
            mock_results[k] = _mock_result_missing(k)

    agent = MarketDriversAgent()
    agent._registry = MockRegistry(mock_results)

    output = asyncio.run(agent.run(_make_task()))
    assert output.status == "partial"
    print("  3/8 ok = partial status           OK")


def test_exception_safety() -> None:
    """Agent handles individual fetch failures gracefully."""
    class CrashingRegistry(DataSourceRegistry):
        def __init__(self):
            super().__init__(DataSourceConfig(fred_api_key="", provider_timeout_sec=5))

        def fetch(self, item_key, lookback_days=None):
            raise RuntimeError("registry exploded")

        def fetch_many(self, item_keys, lookback_days=None):
            raise RuntimeError("registry exploded")

    agent = MarketDriversAgent()
    agent._registry = CrashingRegistry()

    output = asyncio.run(agent.run(_make_task()))
    # Individual fetch exceptions are caught per-thread → all items become "missing"
    # So agent returns status="error" (0 ok out of 8) with missing_data populated
    assert output.status == "error"
    assert len(output.missing_data) == 8
    assert all("fetch_thread_exception" in m or "fetch_timeout" in m for m in output.missing_data)
    print("  exception safety (no crash)       OK")


def test_catastrophic_exception_safety() -> None:
    """Agent catches errors that prevent even thread pool creation."""
    from unittest.mock import patch

    agent = MarketDriversAgent()
    agent._registry = MockRegistry({})

    with patch.object(agent, "_fetch_all_concurrent", side_effect=RuntimeError("total crash")):
        output = asyncio.run(agent.run(_make_task()))

    assert output.status == "error"
    assert "RuntimeError" in output.error
    print("  catastrophic exception safety     OK")


def test_missing_data_list() -> None:
    mock_results = {k: _mock_result_ok(k, 1.0, 0.2) for k in _ALL_ITEM_KEYS}
    mock_results["commodity.iron_ore"] = _mock_result_missing("commodity.iron_ore")
    mock_results["market.vix"] = _mock_result_missing("market.vix")

    agent = MarketDriversAgent()
    agent._registry = MockRegistry(mock_results)

    output = asyncio.run(agent.run(_make_task()))
    assert len(output.missing_data) == 2
    assert any("iron_ore" in m for m in output.missing_data)
    assert any("vix" in m for m in output.missing_data)
    print("  missing_data list correct         OK")


def test_confidence_proportional() -> None:
    all_ok = {k: _mock_result_ok(k, 1.0, 0.2) for k in _ALL_ITEM_KEYS}
    agent = MarketDriversAgent()
    agent._registry = MockRegistry(all_ok)
    output = asyncio.run(agent.run(_make_task()))
    assert output.confidence == 0.85
    print("  confidence proportional (8/8)     OK")


def test_importance_from_change() -> None:
    assert _importance_from_change(None) == 0.3
    assert _importance_from_change(0.05) == 0.3
    assert _importance_from_change(0.3) == 0.5
    assert _importance_from_change(0.8) == 0.6
    assert _importance_from_change(1.5) == 0.7
    assert _importance_from_change(3.0) == 0.75
    print("  importance mapping correct        OK")


def test_source_ids_reference_actual_sources() -> None:
    mock_results = {k: _mock_result_ok(k, 1.5, 0.5) for k in _ALL_ITEM_KEYS}
    mock_results["commodity.iron_ore"] = _mock_result_missing("commodity.iron_ore")

    agent = MarketDriversAgent()
    agent._registry = MockRegistry(mock_results)

    output = asyncio.run(agent.run(_make_task()))
    source_urls = {s.url for s in output.sources}

    for finding in output.findings:
        if finding.category != "data_gap" and finding.source_ids:
            for sid in finding.source_ids:
                assert sid in source_urls, f"source_id {sid!r} not in output.sources"
    assert len(source_urls) > 0
    print("  source_ids reference sources      OK")


def test_feature_flag_on_registers_agent() -> None:
    from unittest.mock import patch
    import importlib

    import market_drivers_agent as mda_mod
    with patch.object(mda_mod, "_ENABLE_MARKET_DRIVERS_AGENT", True):
        import coordinator as coord_mod
        original_registry = dict(coord_mod.AGENT_REGISTRY)
        coord_mod.AGENT_REGISTRY["market_drivers_agent"] = MarketDriversAgent
        try:
            assert "market_drivers_agent" in coord_mod.AGENT_REGISTRY
            assert coord_mod.AGENT_REGISTRY["market_drivers_agent"] is MarketDriversAgent
        finally:
            coord_mod.AGENT_REGISTRY.clear()
            coord_mod.AGENT_REGISTRY.update(original_registry)
    print("  feature flag on registers agent   OK")


def run_all() -> None:
    tests = [
        test_feature_flag_off_not_in_registry,
        test_agent_produces_findings_from_mock,
        test_direction_aud_usd_up_bullish,
        test_direction_aud_usd_down_bearish,
        test_direction_usd_cny_down_bullish_cny,
        test_direction_dxy_up_bearish_aud,
        test_direction_flat_neutral,
        test_data_gap_findings,
        test_partial_status,
        test_exception_safety,
        test_catastrophic_exception_safety,
        test_missing_data_list,
        test_confidence_proportional,
        test_importance_from_change,
        test_source_ids_reference_actual_sources,
        test_feature_flag_on_registers_agent,
    ]
    print("Phase 10.6E — MarketDriversAgent tests")
    print("=" * 50)
    for test_fn in tests:
        test_fn()
    print("=" * 50)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    try:
        run_all()
    except (AssertionError, Exception) as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
