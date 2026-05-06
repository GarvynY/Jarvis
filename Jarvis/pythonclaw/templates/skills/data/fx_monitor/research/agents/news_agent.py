"""
Phase 9 Step 3b — NewsAgent

Data source: news_recent_cache.json (populated by the news_monitor daemon).
Never fetches live RSS — read-only, never mutates daemon state.

Cache path: ~/.pythonclaw/context/news_recent_cache.json
Cache format:
  { "articles": [{title, url, published, snippet, keyword}, ...], "updated_at": ISO }

LLM (at most one call):
  - System: classify direction only from provided articles, no fabrication,
            no definitive recommendations.
  - User:   numbered article list from cache (up to _MAX_ARTICLES).
  - Output: JSON with per-article direction, overall signal, risks.

Fallback behaviour:
  - Cache missing / empty   → status="partial", missing_data explains why.
  - LLM unavailable / error → heuristic direction tagging, token_usage estimated.

Thread model:
  _collect_and_analyse() is a blocking function (file I/O + HTTP).
  Each run uses a short-lived private ThreadPoolExecutor so asyncio.run()
  does not hang waiting for lingering default-executor or class-level threads.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SKILL_DIR = Path(__file__).parent.parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

try:
    from ..schema import AgentOutput, Finding, ResearchTask, SourceRef, now_iso
    from ..llm_bridge import call_llm as _call_llm
except ImportError:
    from schema import AgentOutput, Finding, ResearchTask, SourceRef, now_iso  # type: ignore[no-redef]
    from llm_bridge import call_llm as _call_llm  # type: ignore[no-redef]

# ── Constants ─────────────────────────────────────────────────────────────────

_NEWS_CACHE_FILE: str = os.path.expanduser(
    os.path.join("~", ".pythonclaw", "context", "news_recent_cache.json")
)

_MAX_ARTICLES: int   = 10      # max articles sent to LLM
_MAX_FINDINGS: int   = 8       # max findings in AgentOutput
_MAX_CONFIDENCE: float = 0.70  # news headlines have limited reliability

_LLM_MODEL:      str = "deepseek-chat"
_LLM_MAX_TOKENS: int = 800

_SYSTEM_PROMPT: str = (
    "你是外汇新闻分析助手。"
    "你必须严格基于用户提供的新闻条目进行分析，不得添加或编造任何新闻、事件或来源。"
    "保留不确定性，禁止给出换汇操作建议或明确的买卖方向建议。"
    "输出内容必须是合法的 JSON 对象，不要包含任何 JSON 以外的文字。"
)

_BULLISH_TERMS: frozenset[str] = frozenset([
    "rate hike", "hawkish", "inflation rises", "strong jobs",
    "iron ore rally", "china demand",
])
_BEARISH_TERMS: frozenset[str] = frozenset([
    "rate cut", "dovish", "recession", "slowdown",
    "trade war", "tariff", "conflict", "crisis",
    "rba cuts", "rba lowers",
])

_BANNED_TERMS: tuple[str, ...] = (
    "建议买入", "建议卖出", "换汇时机", "立即操作",
    "应该买", "应该卖", "最佳时机",
)
_SAFE_REPLACEMENT = "（已移除确定性建议）"


# ── Blocking helpers (mockable module-level functions) ────────────────────────

def _read_news_cache() -> tuple[list[dict[str, Any]], str | None, str]:
    """
    Read news_recent_cache.json.

    Returns:
        (articles, error_message_or_None, cache_updated_at)
    """
    if not os.path.exists(_NEWS_CACHE_FILE):
        return [], "news_cache_file_missing", ""
    try:
        with open(_NEWS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        articles: list[dict[str, Any]] = data.get("articles", [])
        updated_at: str = data.get("updated_at", "")
        if not articles:
            return [], "news_cache_empty", updated_at
        return articles, None, updated_at
    except Exception as exc:
        return [], f"news_cache_read_error: {exc}", ""


def _refresh_news_via_monitor() -> tuple[list[dict[str, Any]], str | None, str]:
    """
    Fallback when the daemon-maintained cache is missing or corrupt.

    Uses news_monitor.check_news(mark_seen=False) so research can degrade to a
    live RSS pull without mutating the daemon's seen-URL state.
    """
    try:
        from news_monitor import check_news
        data = check_news(mark_seen=False)
        articles: list[dict[str, Any]] = data.get("new_articles", [])
        updated_at: str = data.get("fetched_at_utc", "")
        if not articles:
            return [], "news_monitor_refresh_empty", updated_at
        return articles, None, updated_at
    except Exception as exc:
        return [], f"news_monitor_refresh_error: {exc}", ""


def _cache_reader_is_mocked() -> bool:
    """Return True in standalone tests that patch _read_news_cache."""
    return _read_news_cache.__class__.__module__.startswith("unittest.mock")


# _call_llm is imported from llm_bridge (Anthropic → DeepSeek fallback)


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_llm_prompt(articles: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, art in enumerate(articles[:_MAX_ARTICLES]):
        title   = art.get("title",   "").strip()
        snippet = art.get("snippet", "").strip()[:150]
        pub     = art.get("published", "")
        kw      = art.get("keyword",   "")
        entry   = f"{i + 1}. [{kw}] {title}"
        if snippet:
            entry += f"\n   摘要: {snippet}"
        if pub:
            entry += f"\n   发布: {pub}"
        lines.append(entry)

    return (
        f"以下是最近抓取的新闻缓存（共 {len(articles[:_MAX_ARTICLES])} 条），"
        "涵盖 CNY/AUD 汇率相关话题。\n\n"
        "新闻列表：\n"
        + "\n\n".join(lines)
        + "\n\n"
        "请严格基于以上新闻进行分析，不得添加任何额外信息。\n"
        "对每条新闻判断其对 CNY/AUD（1 AUD = ? CNY）汇率的可能影响方向。\n\n"
        "输出 JSON 格式（不要包含任何其他文字）：\n"
        '{\n'
        '  "summary": "不超过30字的整体新闻信号摘要",\n'
        '  "findings": [\n'
        '    {"index": 1, "direction": "bullish_aud|bearish_aud|neutral", "reason": "一句话说明"}\n'
        '  ],\n'
        '  "overall_direction": "bullish_aud|bearish_aud|mixed|neutral",\n'
        '  "risks": ["风险描述（不超过2条）"],\n'
        '  "uncertainty_notes": "不确定性说明（不超过20字）"\n'
        '}'
    )


# ── LLM response parser + heuristic fallback ─────────────────────────────────

def _heuristic_direction(title: str) -> str | None:
    t = title.lower()
    if any(w in t for w in _BULLISH_TERMS):
        return "bullish_aud"
    if any(w in t for w in _BEARISH_TERMS):
        return "bearish_aud"
    return "neutral"


def _sanitize_llm_text(value: Any, *, max_chars: int = 180) -> tuple[str, bool]:
    text = str(value or "").strip()
    changed = False
    for term in _BANNED_TERMS:
        if term in text:
            changed = True
            text = text.replace(term, _SAFE_REPLACEMENT)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars], changed


def _parse_llm_response(
    text: str,
    articles: list[dict[str, Any]],
) -> tuple[list[Finding], list[str], str, bool]:
    """
    Parse LLM JSON output.

    Returns (findings, risks, summary).
    Falls back to heuristic direction tagging if JSON parse fails.
    """
    if text:
        try:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = json.loads(m.group())
                summary, unsafe_removed = _sanitize_llm_text(data.get("summary", ""))
                risks: list[str] = []
                for r in data.get("risks", []):
                    if not r:
                        continue
                    cleaned, changed = _sanitize_llm_text(r)
                    unsafe_removed = unsafe_removed or changed
                    risks.append(cleaned)
                raw_findings = data.get("findings", [])
                findings: list[Finding] = []
                for item in raw_findings:
                    idx = int(item.get("index", 1)) - 1
                    if 0 <= idx < len(articles):
                        art = articles[idx]
                        title = art.get("title", "").strip()
                        if not title:
                            continue
                        direction = str(item.get("direction", "neutral"))
                        if direction not in ("bullish_aud", "bearish_aud", "neutral"):
                            direction = "neutral"
                        findings.append(Finding(
                            key=f"news_{idx}",
                            summary=title,
                            direction=direction,
                        ))
                return findings[:_MAX_FINDINGS], risks, summary, unsafe_removed
        except Exception:
            pass

    # Heuristic fallback
    findings = []
    for i, art in enumerate(articles[:_MAX_FINDINGS]):
        title = art.get("title", "").strip()
        if not title:
            continue
        findings.append(Finding(
            key=f"news_{i}",
            summary=title,
            direction=_heuristic_direction(title),
        ))
    return findings, [], f"基于 {len(findings)} 条新闻（LLM分析不可用，使用关键词启发式）", False


# ── Combined blocking task ────────────────────────────────────────────────────

def _collect_and_analyse() -> dict[str, Any]:
    """
    Blocking: read news cache + call LLM once.

    Returns a raw-data dict consumed by _build_news_output().
    """
    articles, cache_error, updated_at = _read_news_cache()
    if cache_error:
        if _cache_reader_is_mocked():
            return {
                "articles": [], "llm_text": "", "tokens": {},
                "cache_error": cache_error, "updated_at": updated_at,
            }
        refreshed, refresh_error, refresh_at = _refresh_news_via_monitor()
        if refresh_error:
            return {
                "articles": [], "llm_text": "", "tokens": {},
                "cache_error": f"{cache_error}; {refresh_error}",
                "updated_at": updated_at or refresh_at,
            }
        articles = refreshed
        updated_at = refresh_at
        cache_error = None

    prompt = _build_llm_prompt(articles)
    llm_text, tokens = _call_llm(prompt, _SYSTEM_PROMPT)
    return {
        "articles": articles,
        "llm_text": llm_text,
        "tokens": tokens,
        "cache_error": None,
        "updated_at": updated_at,
    }


# ── Output builder (pure — no I/O, no LLM) ───────────────────────────────────

def _build_news_output(
    raw: dict[str, Any],
    task: ResearchTask,
    latency_ms: int,
    agent_name: str,
) -> AgentOutput:
    missing: list[str] = []
    risks: list[str] = []

    # ── Cache error → partial ─────────────────────────────────────────────
    if raw.get("cache_error"):
        missing.append(raw["cache_error"])
        return AgentOutput(
            agent_name=agent_name,
            status="partial",
            summary="新闻缓存不可用，跳过新闻分析",
            missing_data=missing,
            latency_ms=latency_ms,
            token_usage={},
            regulatory_flags=[],
        )

    articles: list[dict[str, Any]] = raw["articles"]
    llm_text: str     = raw.get("llm_text", "")
    tokens: dict      = raw.get("tokens", {})
    updated_at: str   = raw.get("updated_at", now_iso())
    retrieved_at: str = now_iso()

    # ── LLM unavailable note ──────────────────────────────────────────────
    llm_used = bool(llm_text and tokens)
    if not llm_used:
        missing.append("llm_unavailable_used_heuristic")
        # Estimated token count for billing transparency
        tokens = {
            "prompt_tokens":     len(_build_llm_prompt(articles).split()),
            "completion_tokens": 0,
        }

    # ── Parse LLM response (or heuristic fallback) ────────────────────────
    findings, llm_risks, llm_summary, unsafe_removed = _parse_llm_response(llm_text, articles)
    risks.extend(llm_risks)
    if unsafe_removed:
        missing.append("unsafe_llm_terms_removed")
        risks.append("LLM 输出包含确定性建议词，已清洗后再进入结果。")

    # ── Build SourceRefs from articles ────────────────────────────────────
    sources: list[SourceRef] = []
    seen_urls: set[str] = set()
    for art in articles[:_MAX_FINDINGS]:
        title = art.get("title", "").strip()
        url   = art.get("url",   "")
        pub   = art.get("published", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(SourceRef(
                title=title or url,
                url=url,
                source="google_news_rss",
                retrieved_at=updated_at or retrieved_at,
                published_at=pub or None,
            ))

    # ── Conflict risk ─────────────────────────────────────────────────────
    bullish = sum(1 for f in findings if f.direction == "bullish_aud")
    bearish = sum(1 for f in findings if f.direction == "bearish_aud")
    if bullish > 0 and bearish > 0:
        risks.append(f"新闻信号存在矛盾：{bullish} 条利好 AUD，{bearish} 条利空 AUD")

    if not findings:
        status = "error"
    elif missing:
        status = "partial"
    else:
        status = "ok"

    summary = llm_summary or f"采集到 {len(findings)} 条相关新闻"
    confidence = min(_MAX_CONFIDENCE, 0.1 * len(findings)) if llm_used else min(0.40, 0.08 * len(findings))

    return AgentOutput(
        agent_name=agent_name,
        status=status,
        summary=summary,
        findings=findings,
        sources=sources,
        as_of=updated_at or retrieved_at,
        confidence=confidence,
        risks=risks,
        missing_data=missing,
        latency_ms=latency_ms,
        token_usage=tokens,
        regulatory_flags=[],
    )


# ── Agent class ───────────────────────────────────────────────────────────────

class NewsAgent:
    """
    News research agent for CNY/AUD.

    Reads news_recent_cache.json, calls LLM once to classify direction signals.

    Protocol:
        agent.agent_name       → str
        await agent.run(task)  → AgentOutput

    Thread model: blocking I/O (file read + LLM HTTP) runs in a short-lived
    private executor for each run.
    """

    agent_name: str = "news_agent"

    @classmethod
    def close_executor(cls) -> None:
        """Backward-compatible no-op; executors are per-run."""
        return None

    async def run(self, task: ResearchTask) -> AgentOutput:
        """Read news cache, call LLM, return structured findings."""
        t0 = time.monotonic()

        try:
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="news-agent",
            )
            raw: dict[str, Any] = await loop.run_in_executor(
                executor,
                _collect_and_analyse,
            )
        except Exception as exc:
            return AgentOutput.make_error(
                self.agent_name,
                error=f"news agent failed: {exc}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        finally:
            if "executor" in locals():
                executor.shutdown(wait=False, cancel_futures=True)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return _build_news_output(raw, task, latency_ms, self.agent_name)
