#!/usr/bin/env python3
"""
Fetch CNY/AUD exchange rate with historical data and trend analysis.

Yahoo Finance ticker: CNYAUD=X  →  1 CNY = ? AUD
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

try:
    import numpy as np
    import pandas as pd
    import yfinance as yf
except ImportError as exc:
    print(
        f"Missing dependency: {exc}\nRun: pip install yfinance pandas numpy",
        file=sys.stderr,
    )
    sys.exit(1)

TICKER = "CNYAUD=X"  # 1 CNY = ? AUD


def _get_current_rate(info: dict) -> float | None:
    """Extract current mid-rate from ticker.info, trying multiple fields."""
    for key in ("regularMarketPrice", "currentPrice", "ask", "bid", "previousClose"):
        val = info.get(key)
        if val and val > 0:
            return float(val)
    return None


def fetch_rate(period: str = "90d") -> dict:
    """Return a dict with current rate, period stats, and recent history."""
    ticker = yf.Ticker(TICKER)
    info = ticker.info

    current = _get_current_rate(info)
    now_utc = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    result: dict = {
        "pair": "CNY/AUD",
        "ticker": TICKER,
        "description": "1 人民币 (CNY) = ? 澳元 (AUD)",
        "current_rate": round(current, 6) if current else None,
        "meaning": f"1 CNY = {current:.6f} AUD" if current else "unavailable",
        "fetched_at_utc": now_utc,
    }

    # Historical data
    hist = ticker.history(period=period)
    if hist.empty:
        result["error"] = "No historical data returned for this period."
        return result

    closes = hist["Close"].dropna()
    if closes.empty:
        result["error"] = "Close prices are all NaN."
        return result

    # Basic period stats
    start_val = float(closes.iloc[0])
    end_val = float(closes.iloc[-1])
    period_chg_pct = (end_val / start_val - 1) * 100

    stats: dict = {
        "period": period,
        "trading_days": int(len(closes)),
        "start_rate": round(start_val, 6),
        "end_rate": round(end_val, 6),
        "period_change_pct": round(period_chg_pct, 4),
        "high": round(float(closes.max()), 6),
        "low": round(float(closes.min()), 6),
        "mean": round(float(closes.mean()), 6),
        "volatility_std": round(float(closes.std()), 6),
    }

    # 7-day moving average trend
    if len(closes) >= 14:
        recent_7 = float(closes.iloc[-7:].mean())
        prior_7 = float(closes.iloc[-14:-7].mean())
        trend_7d = (recent_7 / prior_7 - 1) * 100
        stats["trend_7d_vs_prior_7d_pct"] = round(trend_7d, 4)
        stats["trend_direction"] = (
            "CNY升值 (AUD贬值)" if trend_7d > 0.05
            else "CNY贬值 (AUD升值)" if trend_7d < -0.05
            else "横盘震荡"
        )

    # Simple linear regression slope (annualised %)
    if len(closes) >= 5:
        x = np.arange(len(closes), dtype=float)
        slope, _ = np.polyfit(x, closes.values.astype(float), 1)
        # slope per trading day → annualised % relative to mean
        annualised_pct = (slope * 252 / float(closes.mean())) * 100
        stats["regression_trend_annualised_pct"] = round(annualised_pct, 2)

    result["stats"] = stats

    # Last 30 data points for charting / LLM context
    tail = closes.iloc[-30:]
    result["recent_history"] = [
        {"date": str(idx.date()), "rate": round(float(val), 6)}
        for idx, val in zip(tail.index, tail)
    ]

    return result


def _format_text(data: dict) -> str:
    lines = [
        "═══════════════════════════════════",
        "  CNY/AUD 人民币/澳元 汇率分析",
        "═══════════════════════════════════",
    ]
    if data.get("current_rate"):
        lines.append(f"当前汇率:  {data['meaning']}")
    lines.append(f"查询时间:  {data.get('fetched_at_utc', 'N/A')} (UTC)")

    if "error" in data:
        lines.append(f"[错误] {data['error']}")
        return "\n".join(lines)

    s = data.get("stats", {})
    if s:
        lines.append("")
        lines.append(f"── 历史区间: {s['period']} ({s['trading_days']} 交易日) ──")
        lines.append(f"起始 → 结束:  {s['start_rate']} → {s['end_rate']}  ({s['period_change_pct']:+.2f}%)")
        lines.append(f"最高 / 最低:  {s['high']} / {s['low']}")
        lines.append(f"均值 (μ):    {s['mean']}")
        lines.append(f"波动率 (σ):  {s['volatility_std']}")
        if "trend_7d_vs_prior_7d_pct" in s:
            lines.append(f"近7日趋势:  {s['trend_7d_vs_prior_7d_pct']:+.4f}%  [{s.get('trend_direction', '')}]")
        if "regression_trend_annualised_pct" in s:
            lines.append(f"线性回归趋势 (年化): {s['regression_trend_annualised_pct']:+.2f}%")

    hist = data.get("recent_history", [])
    if hist:
        lines.append("")
        lines.append("── 最近走势 (最新10条) ──")
        for point in hist[-10:]:
            lines.append(f"  {point['date']}   {point['rate']:.6f}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch CNY/AUD exchange rate with historical analysis."
    )
    parser.add_argument(
        "--period",
        default="90d",
        help="History period: 7d, 30d, 90d, 1y, 2y  (default: 90d)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args()

    data = fetch_rate(args.period)

    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(_format_text(data))


if __name__ == "__main__":
    main()
