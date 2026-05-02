#!/usr/bin/env python3
"""
CNY/AUD threshold alert monitor.

Primary rate basis: bank spot sell rate for CNY -> AUD student exchange.
Market fallback   : open.er-api.com / yfinance through fetch_rate.py

State file: ~/.pythonclaw/context/cnyaud_state.json

Alert is expressed in terms of "1 AUD = X CNY" (the direction users care about).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

STATE_FILE = os.path.expanduser(
    os.path.join("~", ".pythonclaw", "context", "cnyaud_state.json")
)
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

def _get_current_rate() -> tuple[float, str, dict] | tuple[None, str, dict]:
    """
    Returns (cny_per_aud, source, full_data).
    Prefer bank spot sell rate because the tuition/living-cost scenario is
    CNY -> AUD, where the customer buys AUD from the bank.
    """
    try:
        from fetch_rate import fetch_rate
        data = fetch_rate("7d")
        rate = data.get("current_1_AUD_in_CNY")
        if rate and float(rate) > 0:
            return float(rate), data.get("current_rate_basis", "unknown"), data
    except Exception:
        pass

    return None, "unavailable", {}


# ── Main logic ────────────────────────────────────────────────────────────────

def check_alert(threshold_pct: float, force_update: bool = False) -> dict:
    cny_per_aud, source, full_data = _get_current_rate()
    now_utc = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    if cny_per_aud is None:
        return {"error": "无法获取当前汇率，请稍后重试。", "timestamp": now_utc}

    state = _load_state()
    result: dict = {
        "pair": "CNY/AUD",
        "rate_basis": source,
        "current_1_AUD_in_CNY": round(cny_per_aud, 4),
        "display": f"1 AUD = {cny_per_aud:.4f} CNY ({source})",
        "timestamp": now_utc,
        "threshold_pct": threshold_pct,
        "alert": False,
        "baseline_updated": False,
    }
    if full_data.get("student_exchange_reference"):
        result["student_exchange_reference"] = full_data["student_exchange_reference"]
    if full_data.get("bank_exchange_rates"):
        result["bank_exchange_rates"] = full_data["bank_exchange_rates"]

    if "baseline_cny_per_aud" not in state and "baseline_aud_per_cny" in state:
        try:
            state["baseline_cny_per_aud"] = 1.0 / float(state["baseline_aud_per_cny"])
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    has_baseline = "baseline_cny_per_aud" in state

    if has_baseline:
        baseline_cny = float(state["baseline_cny_per_aud"])
        change_pct = (cny_per_aud - baseline_cny) / baseline_cny * 100

        result["baseline_1_AUD_in_CNY"] = round(baseline_cny, 4)
        result["baseline_time"] = state.get("baseline_time", "未知")
        result["change_pct"] = round(change_pct, 4)

        if abs(change_pct) >= threshold_pct:
            result["alert"] = True
            if change_pct > 0:
                direction = "买 AUD 变贵 (AUD 对人民币升值)"
                emoji = "📉"
            else:
                direction = "买 AUD 变便宜 (AUD 对人民币贬值)"
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
        state["baseline_cny_per_aud"] = cny_per_aud
        state["baseline_time"] = now_utc
        state["baseline_basis"] = source
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
        f"价格口径:   {r.get('rate_basis', 'N/A')}",
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
