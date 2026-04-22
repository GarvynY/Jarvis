#!/usr/bin/env python3
"""
CNY/AUD real-time threshold alert monitor.

Compares the current rate against a persisted baseline.
Triggers an alert when |change| >= threshold %.

State file: ~/.pythonclaw/context/cnyaud_state.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

try:
    import yfinance as yf
except ImportError:
    print("Missing: yfinance.  Run: pip install yfinance", file=sys.stderr)
    sys.exit(1)

TICKER = "CNYAUD=X"
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

def _get_current_rate() -> float | None:
    info = yf.Ticker(TICKER).info
    for key in ("regularMarketPrice", "currentPrice", "ask", "bid", "previousClose"):
        val = info.get(key)
        if val and float(val) > 0:
            return float(val)
    return None


# ── Main logic ────────────────────────────────────────────────────────────────

def check_alert(threshold_pct: float, force_update: bool = False) -> dict:
    """
    Returns a result dict with keys:
      current_rate, meaning, timestamp, threshold_pct,
      alert (bool), alert_message (str, if alert),
      baseline_rate, baseline_time, change_pct,
      baseline_updated (bool)
    """
    current = _get_current_rate()
    now_utc = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    if current is None:
        return {
            "error": "无法获取当前汇率，请稍后重试。",
            "timestamp": now_utc,
        }

    state = _load_state()
    result: dict = {
        "pair": "CNY/AUD",
        "current_rate": round(current, 6),
        "meaning": f"1 CNY = {current:.6f} AUD",
        "timestamp": now_utc,
        "threshold_pct": threshold_pct,
        "alert": False,
        "baseline_updated": False,
    }

    has_baseline = "baseline_rate" in state

    if has_baseline:
        baseline = float(state["baseline_rate"])
        change_pct = (current - baseline) / baseline * 100
        result["baseline_rate"] = round(baseline, 6)
        result["baseline_time"] = state.get("baseline_time", "未知")
        result["change_pct"] = round(change_pct, 4)

        if abs(change_pct) >= threshold_pct:
            result["alert"] = True
            direction = "上涨" if change_pct > 0 else "下跌"
            emoji = "📈" if change_pct > 0 else "📉"
            result["alert_message"] = (
                f"{emoji} 警报！CNY/AUD 汇率{direction} {abs(change_pct):.3f}%\n"
                f"基准: {baseline:.6f}  →  当前: {current:.6f}\n"
                f"(触发阈值: ±{threshold_pct}%)"
            )
            result["alert_summary"] = (
                f"CNY {'升值' if change_pct > 0 else '贬值'} "
                f"{abs(change_pct):.3f}% vs 基准"
            )
        else:
            result["status"] = (
                f"汇率正常：当前 {current:.6f}，"
                f"变动 {change_pct:+.3f}%，未达阈值 ±{threshold_pct}%"
            )
    else:
        result["info"] = "尚无基准汇率记录，已自动保存当前汇率为基准。"

    # Update baseline when forced or no baseline exists
    if force_update or not has_baseline:
        state["baseline_rate"] = current
        state["baseline_time"] = now_utc
        _save_state(state)
        result["baseline_updated"] = True
        result["baseline_rate"] = round(current, 6)
        result["baseline_time"] = now_utc

    return result


def _format_text(r: dict) -> str:
    if "error" in r:
        return f"[错误] {r['error']}"

    lines = [
        "═══════════════════════════════════",
        "  CNY/AUD 实时阈值监控",
        "═══════════════════════════════════",
        f"当前汇率:   {r['meaning']}",
        f"查询时间:   {r['timestamp']} (UTC)",
        f"告警阈值:   ±{r['threshold_pct']}%",
    ]

    if "baseline_rate" in r and "baseline_time" in r:
        lines.append(f"基准汇率:   {r['baseline_rate']}  (保存于 {r['baseline_time']})")

    if "change_pct" in r:
        lines.append(f"涨跌幅度:   {r['change_pct']:+.4f}%")

    lines.append("")
    if r.get("alert"):
        lines.append(r["alert_message"])
    elif "status" in r:
        lines.append(f"✅ {r['status']}")
    elif "info" in r:
        lines.append(f"ℹ️  {r['info']}")

    if r.get("baseline_updated"):
        lines.append(f"🔄 基准已更新为当前汇率: {r.get('baseline_rate', 'N/A')}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor CNY/AUD exchange rate and alert on threshold breach."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Alert threshold in percent (default: 0.5%%)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Force-update the saved baseline to the current rate",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args()

    result = check_alert(args.threshold, force_update=args.update)

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_format_text(result))

    # Exit code 1 if alert triggered (useful for shell scripting)
    if result.get("alert"):
        sys.exit(1)


if __name__ == "__main__":
    main()
