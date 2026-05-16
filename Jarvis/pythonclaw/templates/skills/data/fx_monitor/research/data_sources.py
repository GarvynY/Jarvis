"""
Phase 10.6E-0 — DataSourceRegistry for market data.

Lightweight registry that defines, resolves, and fetches market data items
with provider priority lists, standardised SourceMetadata, and structured
data_gap behaviour.

Provides:
  - DataSourceSpec dataclass
  - DataSourceResult dataclass
  - DataSourceRegistry class
  - data_source_registry_debug_summary() -> list[dict]
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from source_metadata import SourceMetadata, normalize_domain
except ImportError:
    from .source_metadata import SourceMetadata, normalize_domain  # type: ignore[no-redef]


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class DataSourceConfig:
    enabled: bool = True
    fred_api_key: str = ""
    default_lookback_days: int = 7
    provider_timeout_sec: int = 10


def _load_config() -> DataSourceConfig:
    return DataSourceConfig(
        enabled=os.environ.get("DATASOURCE_ENABLED", "true").lower() != "false",
        fred_api_key=os.environ.get("FRED_API_KEY", ""),
        default_lookback_days=int(os.environ.get("DATASOURCE_LOOKBACK_DAYS", "7")),
        provider_timeout_sec=int(os.environ.get("DATASOURCE_TIMEOUT_SEC", "10")),
    )


# ── DataSourceSpec ────────────────────────────────────────────────────────────

@dataclass
class DataSourceSpec:
    item_key: str
    display_name: str
    category: str
    provider_priority: list[str]
    default_lookback_days: int = 7
    frequency: str = "daily"
    required: bool = False
    source_type_hint: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── DataSourceResult ──────────────────────────────────────────────────────────

@dataclass
class DataSourceResult:
    item_key: str
    provider: str = ""
    status: str = "missing"  # ok / missing / error / unsupported
    value: float | str | None = None
    previous_value: float | str | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    as_of_date: str | None = None
    end_date: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    source_metadata: SourceMetadata | None = None
    error: str = ""
    data_gap_reason: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.source_metadata:
            d["source_metadata"] = self.source_metadata.to_dict()
        else:
            d["source_metadata"] = None
        return d


# ── Provider implementations ──────────────────────────────────────────────────

_YFINANCE_SYMBOLS: dict[str, str] = {
    "fx.aud_usd": "AUDUSD=X",
    "fx.usd_cny": "USDCNY=X",
    "fx.usd_cnh": "USDCNH=X",
    "market.dxy": "DX-Y.NYB",
    "market.vix": "^VIX",
    "commodity.oil": "CL=F",
    "commodity.copper": "HG=F",
}

_FRED_SERIES: dict[str, str] = {
    "fx.aud_usd": "DEXUSAL",
    "fx.usd_cny": "DEXCHUS",
    "market.dxy": "DTWEXBGS",
    "market.vix": "VIXCLS",
    "commodity.oil": "DCOILWTICO",
    "commodity.copper": "PCOPPUSDM",
}


def _fetch_yfinance(item_key: str, lookback_days: int, timeout: int) -> DataSourceResult:
    """Fetch from yfinance. Raises on import/network failure."""
    symbol = _YFINANCE_SYMBOLS.get(item_key)
    if not symbol:
        return DataSourceResult(
            item_key=item_key, provider="yfinance",
            status="unsupported", data_gap_reason=f"no_yfinance_symbol_for_{item_key}",
        )

    import yfinance as yf  # noqa: F401 — lazy import

    period = f"{lookback_days}d" if lookback_days <= 30 else f"{lookback_days // 30 + 1}mo"
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, timeout=timeout)

    if hist is None or hist.empty:
        return DataSourceResult(
            item_key=item_key, provider="yfinance",
            status="missing", data_gap_reason=f"yfinance_empty_response_{symbol}",
        )

    last_row = hist.iloc[-1]
    value = round(float(last_row["Close"]), 6)
    prev_value = round(float(hist.iloc[-2]["Close"]), 6) if len(hist) >= 2 else None

    change_abs = round(value - prev_value, 6) if prev_value is not None else None
    change_pct = round((change_abs / prev_value) * 100, 4) if prev_value and prev_value != 0 else None

    observations = []
    for date_idx, row in hist.tail(min(lookback_days, 10)).iterrows():
        observations.append({
            "date": date_idx.strftime("%Y-%m-%d"),
            "close": round(float(row["Close"]), 6),
        })

    meta = SourceMetadata(
        url=f"https://finance.yahoo.com/quote/{symbol}/",
        provider="yfinance",
        domain="finance.yahoo.com",
        source_type="market_data_api",
        source_tier=2,
        quality_reason=f"market_data_domain:finance.yahoo.com",
    )

    return DataSourceResult(
        item_key=item_key,
        provider="yfinance",
        status="ok",
        value=value,
        previous_value=prev_value,
        change_abs=change_abs,
        change_pct=change_pct,
        as_of_date=hist.index[-1].strftime("%Y-%m-%d"),
        end_date=hist.index[-1].strftime("%Y-%m-%d"),
        observations=observations,
        source_metadata=meta,
        confidence=0.85,
    )


def _fetch_fred(item_key: str, lookback_days: int, api_key: str, timeout: int) -> DataSourceResult:
    """Fetch from FRED API. Raises on network failure."""
    series_id = _FRED_SERIES.get(item_key)
    if not series_id:
        return DataSourceResult(
            item_key=item_key, provider="fred",
            status="unsupported", data_gap_reason=f"no_fred_series_for_{item_key}",
        )

    if not api_key:
        return DataSourceResult(
            item_key=item_key, provider="fred",
            status="unsupported", data_gap_reason="fred_api_key_not_configured",
        )

    import urllib.request
    import json

    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days + 7)

    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}"
        f"&api_key={api_key}"
        f"&file_type=json"
        f"&observation_start={start.strftime('%Y-%m-%d')}"
        f"&observation_end={end.strftime('%Y-%m-%d')}"
        f"&sort_order=desc"
        f"&limit=10"
    )

    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read().decode())

    obs_list = data.get("observations", [])
    valid_obs = [o for o in obs_list if o.get("value") not in (None, "", ".")]

    if not valid_obs:
        return DataSourceResult(
            item_key=item_key, provider="fred",
            status="missing", data_gap_reason=f"fred_no_valid_observations_{series_id}",
        )

    value = round(float(valid_obs[0]["value"]), 6)
    prev_value = round(float(valid_obs[1]["value"]), 6) if len(valid_obs) >= 2 else None

    change_abs = round(value - prev_value, 6) if prev_value is not None else None
    change_pct = round((change_abs / prev_value) * 100, 4) if prev_value and prev_value != 0 else None

    observations = [
        {"date": o["date"], "close": round(float(o["value"]), 6)}
        for o in valid_obs[:10]
    ]

    meta = SourceMetadata(
        url=f"https://fred.stlouisfed.org/series/{series_id}",
        provider="fred",
        domain="fred.stlouisfed.org",
        source_type="official_statistics",
        source_tier=1,
        quality_reason="official_domain:fred.stlouisfed.org",
    )

    return DataSourceResult(
        item_key=item_key,
        provider="fred",
        status="ok",
        value=value,
        previous_value=prev_value,
        change_abs=change_abs,
        change_pct=change_pct,
        as_of_date=valid_obs[0]["date"],
        end_date=valid_obs[0]["date"],
        observations=observations,
        source_metadata=meta,
        confidence=0.92,
    )


def _fetch_existing_fx(item_key: str, lookback_days: int, timeout: int) -> DataSourceResult:
    """Attempt to use existing fetch_rate.py for FX pairs."""
    if item_key not in ("fx.aud_usd", "fx.usd_cny"):
        return DataSourceResult(
            item_key=item_key, provider="existing_fx_provider",
            status="unsupported", data_gap_reason=f"existing_fx_only_supports_cnyaud",
        )

    try:
        import sys
        from pathlib import Path
        fx_dir = str(Path(__file__).resolve().parent.parent)
        if fx_dir not in sys.path:
            sys.path.insert(0, fx_dir)
        from fetch_rate import get_rates  # type: ignore
    except ImportError:
        return DataSourceResult(
            item_key=item_key, provider="existing_fx_provider",
            status="unsupported", data_gap_reason="fetch_rate_module_unavailable",
        )

    try:
        rates = get_rates()
        if not rates or not rates.get("mid"):
            return DataSourceResult(
                item_key=item_key, provider="existing_fx_provider",
                status="missing", data_gap_reason="existing_fx_empty_response",
            )

        value = float(rates["mid"])
        meta = SourceMetadata(
            url="https://open.er-api.com/v6/latest/CNY",
            provider="existing_fx_provider",
            domain="open.er-api.com",
            source_type="market_data_api",
            source_tier=2,
            quality_reason="market_data_domain:open.er-api.com",
        )

        return DataSourceResult(
            item_key=item_key,
            provider="existing_fx_provider",
            status="ok",
            value=value,
            source_metadata=meta,
            confidence=0.80,
        )
    except Exception as exc:
        return DataSourceResult(
            item_key=item_key, provider="existing_fx_provider",
            status="error", error=str(exc),
            data_gap_reason=f"existing_fx_provider_exception:{type(exc).__name__}",
        )


# ── Registry ──────────────────────────────────────────────────────────────────

_DEFAULT_SPECS: list[DataSourceSpec] = [
    DataSourceSpec(
        item_key="fx.aud_usd",
        display_name="AUD/USD",
        category="market_driver",
        provider_priority=["yfinance", "existing_fx_provider", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
    ),
    DataSourceSpec(
        item_key="fx.usd_cny",
        display_name="USD/CNY",
        category="market_driver",
        provider_priority=["yfinance", "existing_fx_provider", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
    ),
    DataSourceSpec(
        item_key="fx.usd_cnh",
        display_name="USD/CNH",
        category="market_driver",
        provider_priority=["yfinance", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
    ),
    DataSourceSpec(
        item_key="market.dxy",
        display_name="US Dollar Index (DXY)",
        category="market_driver",
        provider_priority=["fred", "yfinance", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
    ),
    DataSourceSpec(
        item_key="market.vix",
        display_name="VIX Volatility Index",
        category="market_driver",
        provider_priority=["fred", "yfinance", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
    ),
    DataSourceSpec(
        item_key="commodity.oil",
        display_name="Crude Oil (WTI)",
        category="commodity_trade",
        provider_priority=["fred", "yfinance", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
    ),
    DataSourceSpec(
        item_key="commodity.copper",
        display_name="Copper",
        category="commodity_trade",
        provider_priority=["yfinance", "fred", "data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
        notes="FRED PCOPPUSDM is monthly; yfinance HG=F is daily",
    ),
    DataSourceSpec(
        item_key="commodity.iron_ore",
        display_name="Iron Ore (62% Fe)",
        category="commodity_trade",
        provider_priority=["data_gap"],
        default_lookback_days=7,
        source_type_hint="market_data_api",
        notes="No stable free provider available yet",
    ),
]


ProviderFn = Callable[[str, int, Any], DataSourceResult]


class DataSourceRegistry:
    """Registry that resolves and fetches market data items with fallback."""

    def __init__(self, config: DataSourceConfig | None = None):
        self._config = config or _load_config()
        self._specs: dict[str, DataSourceSpec] = {
            s.item_key: s for s in _DEFAULT_SPECS
        }
        self._providers: dict[str, ProviderFn] = {
            "yfinance": self._call_yfinance,
            "fred": self._call_fred,
            "existing_fx_provider": self._call_existing_fx,
        }

    def get_spec(self, item_key: str) -> DataSourceSpec | None:
        return self._specs.get(item_key)

    def list_specs(self) -> list[DataSourceSpec]:
        return list(self._specs.values())

    def fetch(self, item_key: str, lookback_days: int | None = None) -> DataSourceResult:
        spec = self._specs.get(item_key)
        if spec is None:
            return DataSourceResult(
                item_key=item_key, status="error",
                error=f"unknown_item_key:{item_key}",
                data_gap_reason=f"item_key_not_registered:{item_key}",
            )

        days = lookback_days or spec.default_lookback_days
        errors: list[str] = []

        for provider_name in spec.provider_priority:
            if provider_name == "data_gap":
                break

            fn = self._providers.get(provider_name)
            if fn is None:
                errors.append(f"{provider_name}:no_implementation")
                continue

            try:
                result = fn(item_key, days, None)
                if result.status == "ok":
                    return result
                if result.status == "unsupported":
                    errors.append(f"{provider_name}:unsupported")
                    continue
                errors.append(f"{provider_name}:{result.data_gap_reason or result.status}")
            except Exception as exc:
                errors.append(f"{provider_name}:{type(exc).__name__}:{str(exc)[:80]}")

        return DataSourceResult(
            item_key=item_key,
            provider="",
            status="missing",
            data_gap_reason=f"all_providers_failed:[{'; '.join(errors)}]" if errors else "no_provider_available",
            confidence=0.0,
        )

    def fetch_many(self, item_keys: list[str], lookback_days: int | None = None) -> dict[str, DataSourceResult]:
        results: dict[str, DataSourceResult] = {}
        for key in item_keys:
            results[key] = self.fetch(key, lookback_days)
        return results

    def _call_yfinance(self, item_key: str, days: int, _: Any) -> DataSourceResult:
        return _fetch_yfinance(item_key, days, self._config.provider_timeout_sec)

    def _call_fred(self, item_key: str, days: int, _: Any) -> DataSourceResult:
        return _fetch_fred(item_key, days, self._config.fred_api_key, self._config.provider_timeout_sec)

    def _call_existing_fx(self, item_key: str, days: int, _: Any) -> DataSourceResult:
        return _fetch_existing_fx(item_key, days, self._config.provider_timeout_sec)


# ── Debug summary ─────────────────────────────────────────────────────────────

def data_source_registry_debug_summary(registry: DataSourceRegistry | None = None) -> list[dict[str, Any]]:
    """Return debug summary without fetching external data."""
    reg = registry or DataSourceRegistry()
    config = reg._config
    return [
        {
            "item_key": spec.item_key,
            "display_name": spec.display_name,
            "category": spec.category,
            "provider_priority": spec.provider_priority,
            "default_lookback_days": spec.default_lookback_days,
            "fred_available": bool(config.fred_api_key) and "fred" in spec.provider_priority,
            "yfinance_mapped": spec.item_key in _YFINANCE_SYMBOLS,
        }
        for spec in reg.list_specs()
    ]
