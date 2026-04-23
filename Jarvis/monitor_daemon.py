"""
Standalone monitoring daemon — zero LLM calls for routine checks.

Replaces PythonClaw cron jobs for:
  - Rate threshold alert  (every 30 min)
  - Middle East news alert (every 20 min)

Only the daily morning report still goes through PythonClaw + LLM.
Run with: python monitor_daemon.py
"""

import json
import logging
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path.home() / ".pythonclaw" / "monitor_daemon.log", encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)

# ── config ─────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".pythonclaw" / "pythonclaw.json"
SKILL_DIR = Path.home() / ".pythonclaw" / "context" / "skills" / "cnyaud_monitor"

RATE_INTERVAL_SEC = 30 * 60       # 30 minutes
NEWS_INTERVAL_SEC = 20 * 60       # 20 minutes
RATE_THRESHOLD_PCT = 0.3
MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _telegram_send(token: str, chat_id: int, text: str) -> None:
    """Send a Telegram message directly via Bot API — no LLM involved."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                log.error("Telegram API error: %s", resp.read().decode())
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)


def _run_script(script_name: str, *extra_args: str) -> dict | None:
    """Run a skill script and return parsed JSON output, or None on error."""
    script = SKILL_DIR / script_name
    cmd = [sys.executable, str(script), "--format", "json", *extra_args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
        raw = result.stdout.strip()
        if not raw:
            log.warning("%s produced no output. stderr: %s", script_name, result.stderr[:300])
            return None
        return json.loads(raw)
    except subprocess.TimeoutExpired:
        log.error("%s timed out", script_name)
    except json.JSONDecodeError as e:
        log.error("%s returned invalid JSON: %s", script_name, e)
    except Exception as e:
        log.error("Error running %s: %s", script_name, e)
    return None


# ── alert handlers ──────────────────────────────────────────────────────────

def check_rate_alert(token: str, chat_id: int) -> None:
    log.info("Running rate threshold check...")
    data = _run_script("monitor_alert.py", "--threshold", str(RATE_THRESHOLD_PCT))
    if data is None:
        return

    if data.get("alert"):
        msg = (
            f"⚠️ <b>汇率波动告警</b>\n"
            f"{data.get('alert_message', data.get('display', ''))}\n"
            f"数据来源: {data.get('realtime_source', 'open.er-api.com')}"
        )
        _telegram_send(token, chat_id, msg)
        log.info("Rate alert sent: %.3f%%", data.get("change_pct", 0))
    else:
        log.info(
            "Rate OK: 1 AUD = %.4f CNY (change %.3f%%)",
            data.get("current_1_AUD_in_CNY", 0),
            data.get("change_pct", 0),
        )


def check_news_alert(token: str, chat_id: int) -> None:
    log.info("Running news monitor check...")
    data = _run_script("news_monitor.py")
    if data is None:
        return

    if data.get("has_breaking"):
        articles = data.get("new_articles", [])
        lines = "\n".join(
            f"• {a['title']} ({a.get('published', '')[:16]})"
            for a in articles[:8]
        )
        msg = (
            f"🚨 <b>中东/汇率突发新闻</b>\n"
            f"{lines}\n\n"
            f"📈 以上新闻可能影响油价及 CNY/AUD 走势，建议关注澳元波动。"
        )
        _telegram_send(token, chat_id, msg)
        log.info("News alert sent: %d new articles", len(articles))
    else:
        log.info("News OK: no new breaking articles")


# ── scheduler loop ──────────────────────────────────────────────────────────

def main() -> None:
    cfg = _load_config()
    token = cfg["channels"]["telegram"]["token"]
    chat_id = cfg["channels"]["telegram"]["allowedUsers"][0]

    log.info("Monitor daemon started. Rate: every %dmin | News: every %dmin",
             RATE_INTERVAL_SEC // 60, NEWS_INTERVAL_SEC // 60)

    last_rate = 0.0
    last_news = 0.0

    while True:
        now = time.monotonic()

        if now - last_rate >= RATE_INTERVAL_SEC:
            try:
                check_rate_alert(token, chat_id)
            except Exception as e:
                log.error("Rate check error: %s", e)
            last_rate = time.monotonic()

        if now - last_news >= NEWS_INTERVAL_SEC:
            try:
                check_news_alert(token, chat_id)
            except Exception as e:
                log.error("News check error: %s", e)
            last_news = time.monotonic()

        time.sleep(60)  # wake every minute, check if intervals have elapsed


if __name__ == "__main__":
    main()
