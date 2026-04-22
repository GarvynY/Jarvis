#!/usr/bin/env python3
"""
CNY/AUD real-time threshold alert monitor.

Real-time rate source: open.er-api.com (free, no key, ~1 min delay)
Fallback source      : yfinance CNYAUD=X

State file: ~/.pythonclaw/context/cnyaud_state.json

Alert is expressed in terms of "1 AUD = X CNY" (the direction users care about).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request

STATE_FILE = os.path.expanduser(
    os.path.join("~", ".pythonclaw", "context", "cnyaud_state.json")
)
ER_API_URL = "https://open.er-api.com/v6/latest/CNY"


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── Rate fetch ────────────────────────────────────────────────────────────────

def _get_current_rate() -> tuple[float, str] | tuple[None, str]:
    """
    Returns (aud_per_cny, source).
    aud_per_cny: how many AUD for 1 CNY  (e.g. 0.2045)
    Inverse to get CNY per AUD: 1 / aud_per_cny ≈ 4.89
    """
    # 1) open.er-api.com
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
        import yfinance as yf
        info = yf.Ticker("CNYAUD=X").info
        for key in ("regularMarketPrice", "currentPrice", "ask", "bid", "previousClose"):
            val = info.get(key)
            if val and float(val) > 0:
                return float(val), "yfinance (fallback)"
    except Exception:
        pass

    return None, "unavailable"


# ── Main logic ────────────────────────────────────────────────────────────────

def check_alert(threshold_pct: float, force_update: bool = False) -> dict:
    aud_per_cny, source = _get_current_rate()
    now_utc = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    if aud_per_cny is None:
        return {"error": "无法获取当前汇率，请稍后重试。", "timestamp": now_utc}

    cny_per_aud = 1.0 / aud_per_cny   # "1 AUD = X CNY" — the user-facing number

    state = _load_state()
    result: dict = {
        "pair": "CNY/AUD",
        "realtime_source": source,
        # Both directions, clearly labelled
        "current_1_AUD_in_CNY": round(cny_per_aud, 4),
        "current_1_CNY_in_AUD": round(aud_per_cny, 6),
        "display": f"1 AUD = {cny_per_aud:.4f} CNY  |  1 CNY = {aud_per_cny:.6f} AUD",
        "timestamp": now_utc,
        "threshold_pct": threshold_pct,
        "alert": False,
        "baseline_updated": False,
    }

    has_baseline = "baseline_aud_per_cny" in state

    if has_baseline:
        baseline_aud = float(state["baseline_aud_per_cny"])
        baseline_cny = 1.0 / baseline_aud
        change_pct = (aud_per_cny - baseline_aud) / baseline_aud * 100

        result["baseline_1_AUD_in_CNY"] = round(baseline_cny, 4)
        result["baseline_time"] = state.get("baseline_time", "未知")
        result["change_pct"] = round(change_pct, 4)

        if abs(change_pct) >= threshold_pct:
            result["alert"] = True
            # From user's perspective: CNY strengthens = AUD gets cheaper in CNY terms
            # change_pct > 0 means AUD/CNY rose → CNY weakened
            if change_pct > 0:
                direction = "CNY贬值 (AUD升值)"
                emoji = "📉"
            else:
                direction = "CNY升值 (AUD贬值)"
                emoji = "📈"

            result["alert_message"] = (
                f"{emoji} 警报！{direction} {abs(change_pct):.3f}%\n"
                f"基准: 1 AUD = {baseline_cny:.4f} CNY\n"
                f"当前: 1 AUD = {cny_per_aud:.4f} CNY\n"
                f"(触发阈值: ±{threshold_pct}%)"
            )
        else:
            result["status"] = (
                f"汇率正常：1 AUD = {cny_per_aud:.4f} CNY，"
                f"变动 {change_pct:+.3f}%，未达阈值 ±{threshold_pct}%"
            )
    else:
        result["info"] = "首次运行，已将当前汇率保存为基准。"

    if force_update or not has_baseline:
        state["baseline_aud_per_cny"] = aud_per_cny
        state["baseline_time"] = now_utc
        _save_state(state)
        result["baseline_updated"] = True
        result["baseline_1_AUD_in_CNY"] = round(cny_per_aud, 4)
        result["baseline_time"] = now_utc

    return result


def _format_text(r: dict) -> str:
    if "error" in r:
        return f"[错误] {r['error']}"

    lines = [
        "═══════════════════════════════════",
        "  CNY/AUD 实时阈值监控",
        "═══════════════════════════════════",
        f"当前汇率:   {r['display']}",
        f"数据来源:   {r.get('realtime_source', 'N/A')}",
        f"查询时间:   {r['timestamp']} (UTC)",
        f"告警阈值:   ±{r['threshold_pct']}%",
    ]

    if "baseline_1_AUD_in_CNY" in r:
        lines.append(
            f"基准汇率:   1 AUD = {r['baseline_1_AUD_in_CNY']:.4f} CNY"
            f"  (保存于 {r.get('baseline_time', '?')})"
        )

    if "change_pct" in r:
        lines.append(f"涨跌幅度:   {r['change_pct']:+.4f}%")

    lines.append("")
    if r.get("alert"):
        lines.append(r["alert_message"])
    elif "status" in r:
        lines.append(f"✅ {r['status']}")
    elif "info" in r:
        lines.append(f"ℹ️  {r['info']}")

    if r.get("baseline_updated") and not r.get("alert"):
        lines.append(f"🔄 基准已更新: 1 AUD = {r.get('baseline_1_AUD_in_CNY', 'N/A'):.4f} CNY")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor CNY/AUD exchange rate and alert on threshold breach."
    )
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Alert threshold in percent (default: 0.5%%)")
    parser.add_argument("--update", action="store_true",
                        help="Force-update the saved baseline to the current rate")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    result = check_alert(args.threshold, force_update=args.update)

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_format_text(result))

    if result.get("alert"):
        sys.exit(1)


if __name__ == "__main__":
    main()
