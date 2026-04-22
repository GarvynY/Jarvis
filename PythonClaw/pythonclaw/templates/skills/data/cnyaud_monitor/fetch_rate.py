#!/usr/bin/env python3
"""
Fetch CNY/AUD exchange rate with historical data and trend analysis.

Real-time rate source  : open.er-api.com  (free, no key, ~1 min delay)
Historical data source : yfinance CNYAUD=X (daily closes, free)
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.request

try:
    import numpy as np
    import yfinance as yf
except ImportError as exc:
    print(
        f"Missing dependency: {exc}\nRun: pip install yfinance numpy",
        file=sys.stderr,
    )
    sys.exit(1)

TICKER = "CNYAUD=X"          # yfinance: 1 CNY = ? AUD  (for historical only)
ER_API_URL = "https://open.er-api.com/v6/latest/CNY"   # real-time source


# ── Real-time rate (primary source) ──────────────────────────────────────────

def _fetch_realtime_rate() -> tuple[float, str] | tuple[None, str]:
    """
    Returns (rate, source) where rate = 1 CNY in AUD.
    Tries open.er-api.com first, falls back to yfinance.
    """
    # 1) open.er-api.com  (accurate, ~1 min delay, no key needed)
    try:
        req = urllib.request.Request(ER_API_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        if data.get("result") == "success":
            aud = data["rates"].get("AUD")
            if aud and float(aud) > 0:
                return float(aud), "open.er-api.com"
    except Exception:
        pass

    # 2) yfinance fallback
    try:
        info = yf.Ticker(TICKER).info
        for key in ("regularMarketPrice", "currentPrice", "ask", "bid", "previousClose"):
            val = info.get(key)
            if val and float(val) > 0:
                return float(val), "yfinance (fallback)"
    except Exception:
        pass

    return None, "unavailable"


# ── Historical data (yfinance) ────────────────────────────────────────────────

def _fetch_history(period: str) -> tuple[list, dict] | tuple[list, None]:
    """Returns (recent_history_list, stats_dict). Both empty/None on failure."""
    try:
        hist = yf.Ticker(TICKER).history(period=period)
        if hist.empty:
            return [], None
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return [], None

        start_val = float(closes.iloc[0])
        end_val   = float(closes.iloc[-1])

        stats: dict = {
            "period": period,
            "trading_days": int(len(closes)),
            "start_rate_cny_per_aud": round(1 / start_val, 4),
            "end_rate_cny_per_aud":   round(1 / end_val, 4),
            "period_change_pct": round((end_val / start_val - 1) * 100, 4),
            "high_cny_aud": round(float(closes.max()), 6),
            "low_cny_aud":  round(float(closes.min()), 6),
            "mean_cny_aud": round(float(closes.mean()), 6),
            "volatility_std": round(float(closes.std()), 6),
            "data_source": "yfinance CNYAUD=X",
        }

        if len(closes) >= 14:
            recent_7 = float(closes.iloc[-7:].mean())
            prior_7  = float(closes.iloc[-14:-7].mean())
            trend_7d = (recent_7 / prior_7 - 1) * 100
            stats["trend_7d_pct"] = round(trend_7d, 4)
            stats["trend_direction"] = (
                "CNY升值 (AUD贬值)" if trend_7d > 0.05
                else "CNY贬值 (AUD升值)" if trend_7d < -0.05
                else "横盘震荡"
            )

        if len(closes) >= 5:
            x = np.arange(len(closes), dtype=float)
            slope, _ = np.polyfit(x, closes.values.astype(float), 1)
            stats["regression_trend_annualised_pct"] = round(
                (slope * 252 / float(closes.mean())) * 100, 2
            )

        tail = closes.iloc[-30:]
        recent = [
            {
                "date": str(idx.date()),
                "cny_per_aud": round(1 / float(v), 4),
                "aud_per_cny": round(float(v), 6),
            }
            for idx, v in zip(tail.index, tail)
        ]
        return recent, stats

    except Exception:
        return [], None


# ── Main fetch ────────────────────────────────────────────────────────────────

def fetch_rate(period: str = "90d") -> dict:
    now_utc = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    aud_per_cny, rt_source = _fetch_realtime_rate()  # 1 CNY = X AUD, e.g. 0.2046

    result: dict = {
        "pair": "CNY/AUD",
        "fetched_at_utc": now_utc,
        "realtime_source": rt_source,
    }

    if aud_per_cny:
        cny_per_aud = 1.0 / aud_per_cny              # 1 AUD = Y CNY, e.g. 4.888
        result["current_1_AUD_in_CNY"] = round(cny_per_aud, 4)
        result["current_1_CNY_in_AUD"] = round(aud_per_cny, 6)
        result["display"] = (
            f"1 AUD = {cny_per_aud:.4f} CNY  |  1 CNY = {aud_per_cny:.6f} AUD"
        )
    else:
        result["error"] = "无法获取实时汇率（所有数据源均失败）"

    recent, stats = _fetch_history(period)
    if stats:
        result["stats"] = stats
    if recent:
        result["recent_history"] = recent

    return result


# ── Text formatter ────────────────────────────────────────────────────────────

def _format_text(data: dict) -> str:
    lines = [
        "═══════════════════════════════════",
        "  CNY/AUD 人民币/澳元 汇率分析",
        "═══════════════════════════════════",
    ]

    if "error" in data:
        lines.append(f"[错误] {data['error']}")
        return "\n".join(lines)

    lines.append(f"实时汇率:  {data.get('display', 'N/A')}")
    lines.append(f"数据来源:  {data.get('realtime_source', 'N/A')}")
    lines.append(f"查询时间:  {data.get('fetched_at_utc', 'N/A')} (UTC)")

    s = data.get("stats")
    if s:
        lines += [
            "",
            f"── 历史区间: {s['period']} ({s['trading_days']} 交易日) ──",
            f"区间变动:    {s['period_change_pct']:+.2f}%",
            f"高 / 低:     {s['high_cny_aud']} / {s['low_cny_aud']}  (CNY per AUD, yfinance)",
            f"均值 (μ):    {s['mean_cny_aud']}",
            f"波动率 (σ):  {s['volatility_std']}",
        ]
        if "trend_7d_pct" in s:
            lines.append(
                f"近7日趋势:  {s['trend_7d_pct']:+.4f}%  [{s.get('trend_direction', '')}]"
            )
        if "regression_trend_annualised_pct" in s:
            lines.append(
                f"线性回归趋势 (年化): {s['regression_trend_annualised_pct']:+.2f}%"
            )

    hist = data.get("recent_history", [])
    if hist:
        lines += ["", "── 最近走势 (最新10条, 1 AUD = X CNY) ──"]
        for pt in hist[-10:]:
            lines.append(f"  {pt['date']}   {pt['cny_per_aud']:.4f} CNY/AUD")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch CNY/AUD exchange rate with historical analysis."
    )
    parser.add_argument("--period", default="90d",
                        help="History period: 7d, 30d, 90d, 1y (default: 90d)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    data = fetch_rate(args.period)

    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(_format_text(data))


if __name__ == "__main__":
    main()
