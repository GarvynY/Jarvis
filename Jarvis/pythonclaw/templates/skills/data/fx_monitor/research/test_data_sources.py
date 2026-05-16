#!/usr/bin/env python3
"""
Phase 10.6E-0 — DataSourceRegistry 测试。

验证：
  1. registry 列出所有必需的 item_keys
  2. 未知 item_key 返回明确错误
  3. fetch_many 为每个请求的键返回结果
  4. 提供商失败时回退到下一个
  5. iron_ore 返回结构化 data_gap
  6. 成功结果包含 SourceMetadata
  7. FRED 缺少 API 密钥不崩溃
  8. 不进行外部网络调用（mock）
  9. /fx_research 不受影响

运行：
    cd Jarvis/pythonclaw/templates/skills/data/fx_monitor/research
    python test_data_sources.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from typing import Any

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data_sources import (  # noqa: E402
    DataSourceConfig,
    DataSourceRegistry,
    DataSourceResult,
    DataSourceSpec,
    data_source_registry_debug_summary,
    _fetch_yfinance,
    _fetch_fred,
)
from source_metadata import SourceMetadata  # noqa: E402


REQUIRED_ITEMS = [
    "fx.aud_usd",
    "fx.usd_cny",
    "fx.usd_cnh",
    "market.dxy",
    "market.vix",
    "commodity.oil",
    "commodity.copper",
    "commodity.iron_ore",
]


def _config_no_fred() -> DataSourceConfig:
    return DataSourceConfig(fred_api_key="", provider_timeout_sec=5)


def _mock_yfinance_success(item_key: str, days: int, _: Any) -> DataSourceResult:
    return DataSourceResult(
        item_key=item_key,
        provider="yfinance",
        status="ok",
        value=1.5432,
        previous_value=1.5400,
        change_abs=0.0032,
        change_pct=0.2078,
        as_of_date="2026-05-16",
        observations=[{"date": "2026-05-16", "close": 1.5432}],
        source_metadata=SourceMetadata(
            provider="yfinance",
            domain="finance.yahoo.com",
            source_type="market_data_api",
            source_tier=2,
            quality_reason="market_data_domain:finance.yahoo.com",
        ),
        confidence=0.85,
    )


def _mock_yfinance_failure(item_key: str, days: int, _: Any) -> DataSourceResult:
    raise ConnectionError("network timeout")


def _mock_fred_unsupported(item_key: str, days: int, _: Any) -> DataSourceResult:
    return DataSourceResult(
        item_key=item_key, provider="fred",
        status="unsupported", data_gap_reason="fred_api_key_not_configured",
    )


def test_registry_lists_all_required_items() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    specs = reg.list_specs()
    keys = [s.item_key for s in specs]
    for item in REQUIRED_ITEMS:
        assert item in keys, f"Missing item: {item}"
    assert len(specs) == 8
    print("  registry 列出全部 8 项     OK")


def test_unknown_item_key_returns_error() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    result = reg.fetch("nonexistent.item")
    assert result.status == "error"
    assert "unknown_item_key" in result.error
    assert "not_registered" in result.data_gap_reason
    print("  未知 item_key 返回错误     OK")


def test_fetch_many_returns_all_keys() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    reg._providers["yfinance"] = _mock_yfinance_success
    reg._providers["existing_fx_provider"] = _mock_yfinance_success

    keys = ["fx.aud_usd", "fx.usd_cny", "commodity.iron_ore"]
    results = reg.fetch_many(keys)
    assert set(results.keys()) == set(keys)
    assert results["fx.aud_usd"].status == "ok"
    assert results["commodity.iron_ore"].status == "missing"
    print("  fetch_many 返回所有键      OK")


def test_provider_fallback_on_failure() -> None:
    """When first provider fails, falls back to next."""
    reg = DataSourceRegistry(_config_no_fred())
    call_order: list[str] = []

    def _failing_yfinance(item_key, days, _):
        call_order.append("yfinance")
        raise RuntimeError("yfinance crashed")

    def _ok_existing(item_key, days, _):
        call_order.append("existing_fx_provider")
        return DataSourceResult(
            item_key=item_key, provider="existing_fx_provider",
            status="ok", value=0.6543, confidence=0.80,
            source_metadata=SourceMetadata(provider="existing_fx_provider"),
        )

    reg._providers["yfinance"] = _failing_yfinance
    reg._providers["existing_fx_provider"] = _ok_existing

    result = reg.fetch("fx.aud_usd")
    assert result.status == "ok"
    assert result.provider == "existing_fx_provider"
    assert call_order == ["yfinance", "existing_fx_provider"]
    print("  提供商失败回退到下一个     OK")


def test_iron_ore_returns_structured_data_gap() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    result = reg.fetch("commodity.iron_ore")
    assert result.status == "missing"
    assert result.data_gap_reason != ""
    assert result.source_metadata is None
    assert result.confidence == 0.0
    print("  iron_ore 返回结构化 data_gap OK")


def test_success_result_has_source_metadata() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    reg._providers["yfinance"] = _mock_yfinance_success

    result = reg.fetch("fx.aud_usd")
    assert result.status == "ok"
    assert result.source_metadata is not None
    assert result.source_metadata.provider == "yfinance"
    assert result.source_metadata.source_type == "market_data_api"
    assert result.source_metadata.source_tier == 2
    assert result.source_metadata.domain == "finance.yahoo.com"
    print("  成功结果包含 SourceMetadata OK")


def test_fred_no_api_key_no_crash() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    # fred is first provider for market.dxy
    reg._providers["yfinance"] = _mock_yfinance_success
    result = reg.fetch("market.dxy")
    # FRED should be skipped (unsupported), yfinance used
    assert result.status == "ok"
    assert result.provider == "yfinance"
    print("  FRED 无 API 密钥不崩溃    OK")


def test_all_providers_fail_returns_missing() -> None:
    reg = DataSourceRegistry(_config_no_fred())

    def _fail(item_key, days, _):
        raise ConnectionError("timeout")

    reg._providers["yfinance"] = _fail
    reg._providers["existing_fx_provider"] = _fail

    result = reg.fetch("fx.aud_usd")
    assert result.status == "missing"
    assert "all_providers_failed" in result.data_gap_reason
    assert "yfinance" in result.data_gap_reason
    assert "existing_fx_provider" in result.data_gap_reason
    print("  全部提供商失败返回 missing OK")


def test_result_serialization() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    reg._providers["yfinance"] = _mock_yfinance_success
    result = reg.fetch("fx.aud_usd")
    d = result.to_dict()
    assert d["item_key"] == "fx.aud_usd"
    assert d["status"] == "ok"
    assert d["source_metadata"]["provider"] == "yfinance"
    assert isinstance(d["observations"], list)
    print("  result 序列化正常          OK")


def test_debug_summary_no_network() -> None:
    summary = data_source_registry_debug_summary(DataSourceRegistry(_config_no_fred()))
    assert len(summary) == 8
    for item in summary:
        assert "item_key" in item
        assert "provider_priority" in item
        assert "default_lookback_days" in item
        assert "fred_available" in item
        assert "yfinance_mapped" in item
    iron_ore = next(s for s in summary if s["item_key"] == "commodity.iron_ore")
    assert iron_ore["yfinance_mapped"] is False
    assert iron_ore["fred_available"] is False
    print("  debug summary 无网络调用   OK")


def test_spec_fields_complete() -> None:
    reg = DataSourceRegistry(_config_no_fred())
    spec = reg.get_spec("fx.aud_usd")
    assert spec is not None
    assert spec.display_name == "AUD/USD"
    assert spec.category == "market_driver"
    assert "yfinance" in spec.provider_priority
    assert spec.default_lookback_days == 7
    assert spec.frequency == "daily"
    print("  spec 字段完整              OK")


def test_fetch_does_not_crash_on_single_item() -> None:
    """fetch_many never crashes even if one item errors."""
    reg = DataSourceRegistry(_config_no_fred())

    def _error_yfinance(item_key, days, _):
        if item_key == "fx.usd_cnh":
            raise ValueError("bad symbol")
        return _mock_yfinance_success(item_key, days, _)

    reg._providers["yfinance"] = _error_yfinance

    results = reg.fetch_many(["fx.aud_usd", "fx.usd_cnh"])
    assert results["fx.aud_usd"].status == "ok"
    assert results["fx.usd_cnh"].status == "missing"
    print("  单项失败不中断 fetch_many  OK")


def run_all() -> None:
    tests = [
        test_registry_lists_all_required_items,
        test_unknown_item_key_returns_error,
        test_fetch_many_returns_all_keys,
        test_provider_fallback_on_failure,
        test_iron_ore_returns_structured_data_gap,
        test_success_result_has_source_metadata,
        test_fred_no_api_key_no_crash,
        test_all_providers_fail_returns_missing,
        test_result_serialization,
        test_debug_summary_no_network,
        test_spec_fields_complete,
        test_fetch_does_not_crash_on_single_item,
    ]
    print("Phase 10.6E-0 — DataSourceRegistry 测试")
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
