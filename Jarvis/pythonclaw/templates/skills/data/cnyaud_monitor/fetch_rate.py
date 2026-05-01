#!/usr/bin/env python3
"""
Fetch CNY/AUD exchange rate with bank quote context and trend analysis.

Bank quote source      : Chinese bank FX boards (spot buy/sell, best effort)
Market fallback source : open.er-api.com  (free, no key, ~1 min delay)
Historical data source : yfinance CNYAUD=X (daily closes, free)
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import re
import statistics
import urllib.request

try:
    import numpy as np
    import yfinance as yf
except ImportError:
    np = None
    yf = None

TICKER = "CNYAUD=X"          # yfinance: 1 CNY = ? AUD  (for historical only)
ER_API_URL = "https://open.er-api.com/v6/latest/CNY"   # real-time source

BANK_SOURCES = [
    ("BOC", "中国银行", "https://www.boc.cn/sourcedb/whpj/"),
    ("ICBC", "工商银行", "https://www.usdrate.top/ICBC.html"),
    ("CCB", "建设银行", "https://www.usdrate.top/CCB.html"),
    ("ABC", "农业银行", "https://www.usdrate.top/ABCHINA.html"),
    ("BOCOM", "交通银行", "https://www.usdrate.top/BANKCOMM.html"),
    ("CMB", "招商银行", "https://www.usdrate.top/CMBCHINA.html"),
    ("CMBC", "民生银行", "https://www.usdrate.top/CMBC.html"),
    ("CIB", "兴业银行", "https://www.usdrate.top/CIB.html"),
    ("CEB", "光大银行", "https://www.usdrate.top/CEBBANK.html"),
    ("SPDB", "浦发银行", "https://www.usdrate.top/SPDB.html"),
]


def _decode_response(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _clean_cell(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).replace("\xa0", " ").strip()


def _to_float(value: str) -> float | None:
    value = value.strip().replace(",", "")
    if not value or value in {"-", "--"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _normalise_bank_rate(value: float | None) -> float | None:
    """Chinese bank boards usually quote CNY per 100 foreign currency."""
    if value is None:
        return None
    return round(value / 100, 4) if value > 20 else round(value, 4)


def _parse_bank_quote_page(html_text: str, bank_code: str, bank_name: str, source_url: str) -> dict | None:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.I | re.S)
    for row in rows:
        if "澳大利亚元" not in row and "澳元" not in row and "AUD" not in row.upper():
            continue
        cells = [_clean_cell(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)]
        if len(cells) < 5:
            continue

        numeric = [_to_float(c) for c in cells[1:5]]
        if len(numeric) < 4 or numeric[0] is None or numeric[2] is None:
            continue

        published = ""
        for cell in reversed(cells):
            if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2}", cell):
                published = cell
                break

        spot_buy = _normalise_bank_rate(numeric[0])
        cash_buy = _normalise_bank_rate(numeric[1])
        spot_sell = _normalise_bank_rate(numeric[2])
        cash_sell = _normalise_bank_rate(numeric[3])
        if not spot_buy or not spot_sell:
            continue

        return {
            "bank": bank_name,
            "bank_code": bank_code,
            "currency": "AUD",
            "unit": "CNY per 1 AUD",
            "spot_buy_rate": spot_buy,
            "cash_buy_rate": cash_buy,
            "spot_sell_rate": spot_sell,
            "cash_sell_rate": cash_sell,
            "published_at": published,
            "source_url": source_url,
        }
    return None


def _fetch_bank_quotes() -> tuple[list[dict], str]:
    quotes: list[dict] = []
    failures: list[str] = []
    for code, name, url in BANK_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                text = _decode_response(resp.read())
            quote = _parse_bank_quote_page(text, code, name, url)
            if quote:
                quotes.append(quote)
            else:
                failures.append(code)
        except Exception:
            failures.append(code)

    source = "Chinese bank FX boards"
    if failures:
        source += f" (unavailable: {', '.join(failures)})"
    return quotes, source


def _summarise_bank_quotes(quotes: list[dict]) -> dict | None:
    usable_sell = [q for q in quotes if q.get("spot_sell_rate")]
    usable_buy = [q for q in quotes if q.get("spot_buy_rate")]
    if not usable_sell:
        return None

    best_buying_aud = min(usable_sell, key=lambda q: q["spot_sell_rate"])
    best_selling_aud = max(usable_buy, key=lambda q: q["spot_buy_rate"]) if usable_buy else None
    sell_rates = [q["spot_sell_rate"] for q in usable_sell]
    buy_rates = [q["spot_buy_rate"] for q in usable_buy]

    summary = {
        "quote_count": len(quotes),
        "unit": "CNY per 1 AUD",
        "meaning": {
            "spot_sell_rate": "bank sells AUD to customer; relevant when paying CNY to buy AUD for tuition/living costs",
            "spot_buy_rate": "bank buys AUD from customer; relevant when converting AUD back to CNY",
        },
        "best_for_buying_aud_with_cny": {
            "bank": best_buying_aud["bank"],
            "rate_1_aud_in_cny": best_buying_aud["spot_sell_rate"],
            "published_at": best_buying_aud.get("published_at", ""),
        },
        "median_spot_sell_rate": round(statistics.median(sell_rates), 4),
        "lowest_spot_sell_rate": round(min(sell_rates), 4),
        "highest_spot_sell_rate": round(max(sell_rates), 4),
    }
    if best_selling_aud:
        summary["best_for_selling_aud_to_cny"] = {
            "bank": best_selling_aud["bank"],
            "rate_1_aud_in_cny": best_selling_aud["spot_buy_rate"],
            "published_at": best_selling_aud.get("published_at", ""),
        }
    if buy_rates:
        summary["median_spot_buy_rate"] = round(statistics.median(buy_rates), 4)
    if sell_rates and buy_rates:
        summary["median_bank_spread_pct"] = round(
            (statistics.median(sell_rates) / statistics.median(buy_rates) - 1) * 100, 3
        )
    return summary


# ── Real-time rate (primary source) ──────────────────────────────────────────

def _fetch_market_rate() -> tuple[float, str] | tuple[None, str]:
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
        if yf is None:
            raise RuntimeError("yfinance not installed")
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
        if yf is None or np is None:
            return [], None
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
    aud_per_cny, rt_source = _fetch_market_rate()  # 1 CNY = X AUD, e.g. 0.2046
    bank_quotes, bank_source = _fetch_bank_quotes()
    bank_summary = _summarise_bank_quotes(bank_quotes)

    result: dict = {
        "pair": "CNY/AUD",
        "fetched_at_utc": now_utc,
        "market_rate_source": rt_source,
        "bank_rate_source": bank_source,
    }

    if aud_per_cny:
        cny_per_aud = 1.0 / aud_per_cny
        result["market_1_AUD_in_CNY"] = round(cny_per_aud, 4)
        result["market_1_CNY_in_AUD"] = round(aud_per_cny, 6)
    else:
        result["market_rate_error"] = "无法获取市场实时汇率"

    if bank_summary:
        ref = bank_summary["best_for_buying_aud_with_cny"]
        result["current_1_AUD_in_CNY"] = ref["rate_1_aud_in_cny"]
        result["current_rate_basis"] = "bank_spot_sell_best"
        result["student_exchange_reference"] = {
            "scenario": "CNY -> AUD for tuition/living costs",
            "rate_field": "spot_sell_rate",
            **ref,
        }
        result["bank_exchange_rates"] = {
            "summary": bank_summary,
            "quotes": bank_quotes,
        }
    elif aud_per_cny:
        cny_per_aud = 1.0 / aud_per_cny
        result["current_1_AUD_in_CNY"] = round(cny_per_aud, 4)
        result["current_rate_basis"] = "market_mid_fallback"
    else:
        result["error"] = "无法获取银行牌价或市场实时汇率"

    if "current_1_AUD_in_CNY" in result:
        current = result["current_1_AUD_in_CNY"]
        result["display"] = f"1 AUD = {current:.4f} CNY ({result['current_rate_basis']})"

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

    lines.append(f"换汇参考:  {data.get('display', 'N/A')}")
    ref = data.get("student_exchange_reference")
    if ref:
        lines.append(
            f"学生买汇:  {ref['bank']} 现汇卖出价 {ref['rate_1_aud_in_cny']:.4f} CNY/AUD"
        )
    if "market_1_AUD_in_CNY" in data:
        lines.append(f"市场中间参考: 1 AUD = {data['market_1_AUD_in_CNY']:.4f} CNY")
    lines.append(f"市场数据源:  {data.get('market_rate_source', 'N/A')}")
    lines.append(f"银行数据源:  {data.get('bank_rate_source', 'N/A')}")
    lines.append(f"查询时间:  {data.get('fetched_at_utc', 'N/A')} (UTC)")

    bank = data.get("bank_exchange_rates", {}).get("summary")
    if bank:
        lines += [
            "",
            f"── 银行牌价汇总 ({bank['quote_count']} 家, 1 AUD = X CNY) ──",
            f"现汇卖出价中位数: {bank['median_spot_sell_rate']:.4f}",
            f"现汇卖出价区间:   {bank['lowest_spot_sell_rate']:.4f} - {bank['highest_spot_sell_rate']:.4f}",
        ]
        if "median_spot_buy_rate" in bank:
            lines.append(f"现汇买入价中位数: {bank['median_spot_buy_rate']:.4f}")
        if "median_bank_spread_pct" in bank:
            lines.append(f"银行买卖价差中位: {bank['median_bank_spread_pct']:.3f}%")

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
