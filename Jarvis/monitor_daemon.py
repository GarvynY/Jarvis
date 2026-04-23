"""
Standalone monitoring daemon.

Three monitoring modes:
  1. Rate check (every 30 min)  — no LLM, tracks 48h rolling high
  2. News check (every 20 min)  — LLM filters relevance + brief analysis
       "影响有限" → silent skip; real impact → send Telegram with analysis
  3. Combined alert             — LLM analysis (with rate context) when:
       breaking news + rate drop ≥ COMBINED_THRESHOLD_PCT from 48h high

Run with: python monitor_daemon.py
"""

import json
import logging
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# ── config ──────────────────────────────────────────────────────────────────

CONFIG_PATH  = Path.home() / ".pythonclaw" / "pythonclaw.json"
SKILL_DIR    = Path.home() / ".pythonclaw" / "context" / "skills" / "cnyaud_monitor"
STATE_PATH   = Path.home() / ".pythonclaw" / "context" / "daemon_state.json"

RATE_INTERVAL_SEC      = 30 * 60   # how often to fetch rate
NEWS_INTERVAL_SEC      = 20 * 60   # how often to scan news
SIMPLE_THRESHOLD_PCT   = 0.3       # rate-only alert (no LLM)
COMBINED_THRESHOLD_PCT = 0.8       # news + rate → LLM alert
RATE_HISTORY_HOURS     = 48        # window for rolling high
COOLDOWN_HOURS         = 2         # min gap between combined LLM alerts


# ── config / state helpers ───────────────────────────────────────────────────

def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"rate_history": [], "last_combined_alert": None}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── telegram ─────────────────────────────────────────────────────────────────

def _telegram_send(token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                log.error("Telegram API error: %s", resp.read().decode())
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)


# ── skill scripts ─────────────────────────────────────────────────────────────

def _run_script(script_name: str, *extra_args: str) -> dict | None:
    script = SKILL_DIR / script_name
    cmd = [sys.executable, str(script), "--format", "json", *extra_args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
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


# ── 48-hour rolling high ──────────────────────────────────────────────────────

def _record_rate(state: dict, cny_per_aud: float) -> None:
    """Append current rate to history and prune entries older than 48h."""
    now_iso = datetime.now(timezone.utc).isoformat()
    state["rate_history"].append({"ts": now_iso, "cny_per_aud": cny_per_aud})

    cutoff = datetime.now(timezone.utc) - timedelta(hours=RATE_HISTORY_HOURS)
    state["rate_history"] = [
        h for h in state["rate_history"]
        if datetime.fromisoformat(h["ts"]) > cutoff
    ]


def _48h_high(state: dict) -> float | None:
    """Return the highest 1 AUD = X CNY seen in the last 48 hours."""
    history = state.get("rate_history", [])
    if not history:
        return None
    return max(h["cny_per_aud"] for h in history)


# ── LLM analysis ─────────────────────────────────────────────────────────────

NO_IMPACT_SIGNAL = "影响有限"   # if response starts with this, skip sending

def _llm_news_analysis(
    api_key: str,
    articles: list[dict],
    rate_info: dict | None = None,
) -> str:
    """
    Call DeepSeek to analyze news impact on CNY/AUD.

    - rate_info=None  → news-only check (filter + brief analysis)
    - rate_info=dict  → combined alert (always relevant, include rate context)

    Returns analysis text, or a string starting with NO_IMPACT_SIGNAL if
    news has no meaningful CNY/AUD relevance (news-only mode only).
    """
    from openai import OpenAI

    headlines = "\n".join(f"- {a['title']}" for a in articles[:5])

    if rate_info:
        # Combined mode: rate context is already a strong signal, skip filter
        rate_ctx = (
            f"\n\n同期汇率异动：近48小时最高 1 AUD = {rate_info['high_48h']:.4f} CNY，"
            f"当前 1 AUD = {rate_info['current_cny']:.4f} CNY，"
            f"较高点跌幅 {rate_info['drop_pct']:.2f}%。"
        )
        prompt = (
            f"以下突发新闻伴随汇率明显下跌同步出现，请用中文3句话分析：\n\n"
            f"新闻：\n{headlines}"
            f"{rate_ctx}\n\n"
            f"分析重点：新闻驱动汇率下跌的逻辑、后续走势判断、换汇建议。"
            f"语气专业简洁，不超过100字。"
        )
    else:
        # News-only mode: first filter by relevance
        prompt = (
            f"以下是刚出现的新闻，请判断对 CNY/AUD（人民币/澳元）汇率的影响：\n\n"
            f"{headlines}\n\n"
            f"要求：\n"
            f"- 如果新闻与 CNY/AUD 汇率关联不明显（如历史回顾、无关地区事件），"
            f"只回复'{NO_IMPACT_SIGNAL}，无需关注'\n"
            f"- 如果有实质影响，用3句话分析：驱动逻辑、方向判断、换汇建议\n"
            f"- 语气专业简洁，不超过80字"
        )

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
        timeout=60.0,
    )
    response = client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


# ── main check functions ───────────────────────────────────────────────────────

def check_rate(state: dict, token: str, chat_id: int) -> dict | None:
    """
    Fetch rate, update 48h history.
    If simple threshold breached (no news context): send plain alert.
    Returns rate data for use in combined check.
    """
    log.info("Running rate check...")
    data = _run_script("monitor_alert.py", "--threshold", str(SIMPLE_THRESHOLD_PCT))
    if data is None:
        return None

    current = data.get("current_1_AUD_in_CNY", 0)
    _record_rate(state, current)

    high = _48h_high(state)
    drop = ((high - current) / high * 100) if high else 0

    log.info(
        "Rate: 1 AUD = %.4f CNY | 48h high = %s | drop from high = %.2f%%",
        current,
        f"{high:.4f}" if high else "N/A",
        drop,
    )

    # Simple rate alert (no LLM) — only when news is not breaking
    if data.get("alert"):
        msg = (
            f"⚠️ <b>汇率波动告警</b>\n"
            f"当前: 1 AUD = {current:.4f} CNY\n"
            f"48小时最高点: {high:.4f} CNY\n"
            f"较高点变动: {drop:+.2f}%\n"
            f"数据来源: {data.get('realtime_source', 'open.er-api.com')}"
        )
        _telegram_send(token, chat_id, msg)
        log.info("Simple rate alert sent.")

    return {"data": data, "current_cny": current, "high_48h": high, "drop_pct": drop}


def check_news(api_key: str, token: str, chat_id: int) -> list[dict]:
    """
    Scan news RSS. If breaking news, call LLM to filter + analyze.
    - LLM says "影响有限" → silent skip (irrelevant news)
    - LLM gives real analysis → send Telegram with headlines + analysis
    Returns list of new articles (empty if none).
    """
    log.info("Running news check...")
    data = _run_script("news_monitor.py")
    if data is None:
        return []

    articles = data.get("new_articles", [])
    if not data.get("has_breaking"):
        log.info("News OK: no new breaking articles")
        return []

    log.info("Breaking news: %d new articles — calling LLM for relevance check", len(articles))
    try:
        analysis = _llm_news_analysis(api_key, articles)
    except Exception as e:
        log.error("LLM news analysis failed: %s", e)
        analysis = "（分析暂时不可用）"

    if analysis.startswith(NO_IMPACT_SIGNAL):
        log.info("LLM: news not relevant to CNY/AUD, skipping alert")
        return articles   # still return so combined check can use them

    headlines = "\n".join(f"• {a['title']}" for a in articles[:5])
    msg = (
        f"📰 <b>CNY/AUD 相关新闻</b>\n\n"
        f"{headlines}\n\n"
        f"🤖 <b>影响分析</b>\n{analysis}"
    )
    _telegram_send(token, chat_id, msg)
    log.info("News alert sent with LLM analysis.")
    return articles


def check_combined(
    state: dict,
    api_key: str,
    token: str,
    chat_id: int,
    rate_info: dict,
    breaking_articles: list[dict],
) -> None:
    """
    If breaking news AND rate dropped ≥ COMBINED_THRESHOLD_PCT from 48h high:
    call LLM for analysis and send enriched alert.
    """
    if not breaking_articles:
        return

    drop_pct = rate_info.get("drop_pct", 0)
    high_48h = rate_info.get("high_48h")
    current  = rate_info.get("current_cny", 0)

    if drop_pct < COMBINED_THRESHOLD_PCT:
        log.info(
            "Combined check: news triggered but rate drop %.2f%% < threshold %.1f%%",
            drop_pct, COMBINED_THRESHOLD_PCT,
        )
        return

    # Cooldown check — avoid spamming
    last = state.get("last_combined_alert")
    if last:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if elapsed < COOLDOWN_HOURS * 3600:
            log.info("Combined alert suppressed (cooldown: %.0f min left)",
                     (COOLDOWN_HOURS * 3600 - elapsed) / 60)
            return

    log.info(
        "COMBINED ALERT: drop %.2f%% >= %.1f%% with %d breaking articles — calling LLM",
        drop_pct, COMBINED_THRESHOLD_PCT, len(breaking_articles),
    )

    try:
        analysis = _llm_news_analysis(api_key, breaking_articles, rate_info)
    except Exception as e:
        log.error("LLM analysis failed: %s", e)
        analysis = "（LLM分析暂时不可用）"

    headlines = "\n".join(f"• {a['title']}" for a in breaking_articles[:5])
    msg = (
        f"🔴 <b>联合告警：突发新闻 + 汇率大幅波动</b>\n\n"
        f"📉 <b>汇率</b>\n"
        f"48小时最高: 1 AUD = {high_48h:.4f} CNY\n"
        f"当前:       1 AUD = {current:.4f} CNY\n"
        f"较高点跌幅: {drop_pct:.2f}%\n\n"
        f"📰 <b>触发新闻</b>\n{headlines}\n\n"
        f"🤖 <b>AI 简析</b>\n{analysis}\n\n"
        f"⚠️ 仅供参考，不构成投资建议"
    )
    _telegram_send(token, chat_id, msg)
    state["last_combined_alert"] = datetime.now(timezone.utc).isoformat()
    log.info("Combined LLM alert sent.")


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    cfg     = _load_config()
    token   = cfg["channels"]["telegram"]["token"]
    chat_id = cfg["channels"]["telegram"]["allowedUsers"][0]
    api_key = cfg["llm"]["deepseek"]["apiKey"]

    log.info(
        "Monitor daemon started | rate every %dmin | news every %dmin "
        "| simple threshold %.1f%% | combined threshold %.1f%%",
        RATE_INTERVAL_SEC // 60, NEWS_INTERVAL_SEC // 60,
        SIMPLE_THRESHOLD_PCT, COMBINED_THRESHOLD_PCT,
    )

    state      = _load_state()
    last_rate  = 0.0
    last_news  = 0.0
    last_rate_info: dict | None = None
    pending_articles: list[dict] = []

    while True:
        now = time.monotonic()
        state_dirty = False

        # ── rate check ────────────────────────────────────────────────────────
        if now - last_rate >= RATE_INTERVAL_SEC:
            try:
                rate_info = check_rate(state, token, chat_id)
                if rate_info:
                    last_rate_info = rate_info
                    state_dirty = True
            except Exception as e:
                log.error("Rate check error: %s", e)
            last_rate = time.monotonic()

        # ── news check ────────────────────────────────────────────────────────
        if now - last_news >= NEWS_INTERVAL_SEC:
            try:
                articles = check_news(api_key, token, chat_id)
                if articles:
                    pending_articles = articles
            except Exception as e:
                log.error("News check error: %s", e)
            last_news = time.monotonic()

        # ── combined check (runs after either check produces new data) ────────
        if pending_articles and last_rate_info:
            try:
                check_combined(
                    state, api_key, token, chat_id,
                    last_rate_info, pending_articles,
                )
                state_dirty = True
                pending_articles = []   # consumed
            except Exception as e:
                log.error("Combined check error: %s", e)

        if state_dirty:
            _save_state(state)

        time.sleep(60)


if __name__ == "__main__":
    main()
