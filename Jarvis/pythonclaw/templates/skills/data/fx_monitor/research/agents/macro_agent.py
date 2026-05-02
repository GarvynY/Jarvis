"""
Phase 9 Step 3c — MacroAgent

Fetches macro signals relevant to CNY/AUD via search, then summarises
with a single LLM call.

Search strategy (priority order):
  1. Tavily API  — if TAVILY_API_KEY env var is set and tavily-python installed
  2. Google News RSS (news_monitor._fetch_google_news_rss) — free fallback

Fixed MVP query set (covers RBA / PBoC / Fed / AUD macro):
  - "RBA Reserve Bank Australia interest rate 2025"
  - "PBoC China central bank monetary policy yuan 2025"
  - "Federal Reserve USD dollar Australian dollar impact"
  - "Australia China trade AUD CNY macro outlook"

All queries used are preserved in findings / missing_data so the supervisor
can trace what was searched.

LLM call (at most one):
  - System: summarise only from provided snippets, no fabrication,
            no definitive recommendations.
  - User:   numbered search-result list with titles + snippets.
  - Output: JSON with per-signal direction, overall direction, risks.

Fallback behaviour:
  - All search fails       → status="partial" / "error", preserved in missing_data.
  - LLM unavailable/error  → raw search titles as findings, token_usage estimated.

Thread model: _collect_and_analyse() is blocking (HTTP + LLM).
Each run uses a short-lived private ThreadPoolExecutor so tests and
asyncio.run() do not hang on lingering executor threads.
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
except ImportError:
    from schema import AgentOutput, Finding, ResearchTask, SourceRef, now_iso  # type: ignore[no-redef]

# ── Constants ─────────────────────────────────────────────────────────────────

# Fixed query set for CNY/AUD macro research (MVP)
_MACRO_QUERIES: list[str] = [
    "RBA Reserve Bank Australia interest rate 2025",
    "PBoC China central bank monetary policy yuan 2025",
    "Federal Reserve USD dollar Australian dollar impact",
    "Australia China trade AUD CNY macro outlook",
]

_MAX_RESULTS_PER_QUERY: int  = 3    # Tavily / RSS results per query
_MAX_RESULTS_TO_LLM:    int  = 10   # total results passed to LLM
_MAX_FINDINGS:          int  = 8    # max findings in AgentOutput
_MAX_CONFIDENCE:        float = 0.75

_LLM_MODEL:      str = "claude-haiku-4-5-20251001"
_LLM_MAX_TOKENS: int = 1000

_SYSTEM_PROMPT: str = (
    "你是宏观经济分析助手。"
    "你必须严格基于用户提供的搜索结果片段进行分析，不得添加或编造任何事实或数据。"
    "保留不确定性，标注信息缺口，禁止给出确定性的投资或换汇建议。"
    "输出内容必须是合法的 JSON 对象，不要包含任何 JSON 以外的文字。"
)

_BANNED_TERMS: tuple[str, ...] = (
    "建议买入", "建议卖出", "换汇时机", "立即操作",
    "应该买", "应该卖", "最佳时机",
)
_SAFE_REPLACEMENT = "（已移除确定性建议）"
_VALID_DIRECTIONS = {"bullish_aud", "bearish_aud", "neutral"}


# ── Search layer (mockable) ───────────────────────────────────────────────────

def _search_tavily(query: str, max_results: int) -> list[dict[str, Any]]:
    """
    Search via Tavily API. Returns list of result dicts.
    Returns [] if Tavily unavailable or key missing.
    """
    try:
        from tavily import TavilyClient  # optional
    except ImportError:
        return []

    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return []

    try:
        client = TavilyClient(api_key)
        response = client.search(
            query=query,
            search_depth="basic",
            topic="news",
            max_results=max_results,
            include_answer=False,
        )
        results = []
        for r in response.get("results", []):
            results.append({
                "title":    r.get("title", ""),
                "url":      r.get("url", ""),
                "snippet":  r.get("content", "")[:300],
                "source":   "tavily",
                "published_at": r.get("published_date", None),
            })
        return results
    except Exception:
        return []


def _search_rss(query: str, max_results: int) -> list[dict[str, Any]]:
    """
    Fallback: Google News RSS search via news_monitor helper.
    Returns list of result dicts (same schema as Tavily results).
    """
    try:
        from news_monitor import _fetch_google_news_rss
        raw = _fetch_google_news_rss(query, max_items=max_results)
        results = []
        for r in raw:
            if "error" in r:
                continue
            results.append({
                "title":    r.get("title", ""),
                "url":      r.get("url", ""),
                "snippet":  r.get("snippet", "")[:300],
                "source":   "google_news_rss",
                "published_at": r.get("published", None),
            })
        return results
    except Exception:
        return []


def _search_once(query: str, max_results: int) -> list[dict[str, Any]]:
    """
    Run one search query: try Tavily first, fall back to RSS.
    Returns list of result dicts.
    """
    results = _search_tavily(query, max_results)
    if results:
        return results
    return _search_rss(query, max_results)


# ── LLM call (mockable) ───────────────────────────────────────────────────────

def _call_llm(
    prompt: str,
    system: str,
    max_tokens: int = _LLM_MAX_TOKENS,
) -> tuple[str, dict[str, int]]:
    """
    Make one Anthropic API call (blocking).

    Returns (response_text, token_usage_dict).
    Returns ("", {}) if anthropic is not installed or API key missing.
    """
    try:
        import anthropic  # optional
    except ImportError:
        return "", {}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "", {}

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
        msg = client.messages.create(
            model=_LLM_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text: str = msg.content[0].text if msg.content else ""
        usage: dict[str, int] = {
            "prompt_tokens":     msg.usage.input_tokens,
            "completion_tokens": msg.usage.output_tokens,
        }
        return text, usage
    except Exception:
        return "", {}


# ── Collect search results (mockable) ─────────────────────────────────────────

def _collect_search_results() -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """
    Run all _MACRO_QUERIES and collect results.

    Returns:
        (all_results, successful_queries, failed_queries)
    """
    all_results: list[dict[str, Any]] = []
    successful: list[str] = []
    failed: list[str] = []

    for query in _MACRO_QUERIES:
        results = _search_once(query, _MAX_RESULTS_PER_QUERY)
        if results:
            for r in results:
                r["query"] = query
            all_results.extend(results)
            successful.append(query)
        else:
            failed.append(query)

    return all_results, successful, failed


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_llm_prompt(
    results: list[dict[str, Any]],
    queries_used: list[str],
) -> str:
    lines: list[str] = []
    for i, r in enumerate(results[:_MAX_RESULTS_TO_LLM]):
        title   = r.get("title",   "").strip()
        snippet = r.get("snippet", "").strip()[:200]
        url     = r.get("url", "")
        query   = r.get("query", "")
        entry   = f"{i + 1}. [{query}]\n   标题: {title}"
        if snippet:
            entry += f"\n   摘要: {snippet}"
        if url:
            entry += f"\n   来源: {url}"
        lines.append(entry)

    queries_str = "\n".join(f"  - {q}" for q in queries_used)

    return (
        f"以下是关于 CNY/AUD 宏观驱动因素的搜索结果（共 {len(results[:_MAX_RESULTS_TO_LLM])} 条）。\n"
        f"使用的查询：\n{queries_str}\n\n"
        "搜索结果：\n"
        + "\n\n".join(lines)
        + "\n\n"
        "请严格基于以上搜索结果进行分析，不得添加搜索结果之外的任何事实。\n"
        "分析以下信号对 CNY/AUD（1 AUD = ? CNY）汇率的影响：RBA 信号、PBoC 信号、美联储/USD 信号、宏观贸易信号。\n\n"
        "输出 JSON 格式（不要包含任何其他文字）：\n"
        '{\n'
        '  "summary": "不超过40字的整体宏观信号摘要",\n'
        '  "rba_signal":  "bullish_aud|bearish_aud|neutral|unknown",\n'
        '  "pboc_signal": "bullish_aud|bearish_aud|neutral|unknown",\n'
        '  "usd_signal":  "bullish_aud|bearish_aud|neutral|unknown",\n'
        '  "overall_direction": "bullish_aud|bearish_aud|mixed|neutral",\n'
        '  "key_findings": ["发现1（来自搜索结果）", "发现2"],\n'
        '  "risks": ["风险描述（不超过3条）"],\n'
        '  "data_gaps": ["未能覆盖的信息缺口"]\n'
        '}'
    )


# ── LLM response parser ───────────────────────────────────────────────────────

def _parse_llm_response(
    text: str,
    results: list[dict[str, Any]],
    queries_used: list[str],
) -> tuple[list[Finding], list[str], str, list[str], bool]:
    """
    Parse LLM JSON. Returns (findings, risks, summary, llm_data_gaps).
    Falls back to raw search titles as findings on parse error.
    """
    def _sanitize_text(value: Any, *, max_chars: int = 220) -> tuple[str, bool]:
        cleaned = str(value or "").strip()
        changed = False
        for term in _BANNED_TERMS:
            if term in cleaned:
                changed = True
                cleaned = cleaned.replace(term, _SAFE_REPLACEMENT)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:max_chars], changed

    def _safe_direction(value: Any) -> str:
        direction = str(value or "neutral")
        return direction if direction in _VALID_DIRECTIONS else "neutral"

    if text:
        try:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = json.loads(m.group())
                summary, unsafe_removed = _sanitize_text(data.get("summary", ""))
                risks: list[str] = []
                for r in data.get("risks", []):
                    if not r:
                        continue
                    cleaned, changed = _sanitize_text(r)
                    unsafe_removed = unsafe_removed or changed
                    risks.append(cleaned)
                data_gaps: list[str] = []
                for g in data.get("data_gaps", []):
                    if not g:
                        continue
                    cleaned, changed = _sanitize_text(g)
                    unsafe_removed = unsafe_removed or changed
                    data_gaps.append(cleaned)

                findings: list[Finding] = []
                # One finding per signal
                signal_map = [
                    ("rba_signal",  "macro_rba",   "RBA 利率信号"),
                    ("pboc_signal", "macro_pboc",  "PBoC 货币政策信号"),
                    ("usd_signal",  "macro_usd",   "美联储/USD 信号"),
                ]
                for field, key, label in signal_map:
                    direction = str(data.get(field, "unknown"))
                    if direction == "unknown":
                        continue
                    direction = _safe_direction(direction)
                    findings.append(Finding(
                        key=key,
                        summary=f"{label}: {direction.replace('_', ' ')}",
                        direction=direction,
                    ))

                overall_direction = data.get("overall_direction")
                detail_direction = (
                    _safe_direction(overall_direction)
                    if overall_direction in _VALID_DIRECTIONS
                    else None
                )
                # Facts in detail findings come only from retrieved search titles.
                for i, r in enumerate(results[: max(0, _MAX_FINDINGS - len(findings))]):
                    title = r.get("title", "").strip()
                    if not title:
                        continue
                    findings.append(Finding(
                        key=f"macro_detail_{i}",
                        summary=title[:220],
                        direction=detail_direction,
                    ))

                return findings[:_MAX_FINDINGS], risks, summary, data_gaps, unsafe_removed
        except Exception:
            pass

    # Raw fallback: use search titles as findings
    findings = []
    for i, r in enumerate(results[:_MAX_FINDINGS]):
        title = r.get("title", "").strip()
        if title:
            findings.append(Finding(
                key=f"macro_raw_{i}",
                summary=title,
                direction=None,
            ))
    summary = f"基于 {len(results)} 条搜索结果（LLM分析不可用）"
    return findings, [], summary, [], False


# ── Combined blocking task ────────────────────────────────────────────────────

def _collect_and_analyse() -> dict[str, Any]:
    """
    Blocking: run search queries + call LLM once.

    Returns a raw-data dict consumed by _build_macro_output().
    """
    retrieved_at = now_iso()
    results, successful, failed = _collect_search_results()

    if not results:
        return {
            "results": [], "llm_text": "", "tokens": {},
            "successful_queries": successful,
            "failed_queries": failed,
            "retrieved_at": retrieved_at,
        }

    prompt = _build_llm_prompt(results, successful)
    llm_text, tokens = _call_llm(prompt, _SYSTEM_PROMPT)
    return {
        "results": results,
        "llm_text": llm_text,
        "tokens": tokens,
        "successful_queries": successful,
        "failed_queries": failed,
        "retrieved_at": retrieved_at,
    }


# ── Output builder (pure — no I/O, no LLM) ───────────────────────────────────

def _build_macro_output(
    raw: dict[str, Any],
    task: ResearchTask,
    latency_ms: int,
    agent_name: str,
) -> AgentOutput:
    missing: list[str] = []
    risks: list[str] = []

    retrieved_at:  str = raw.get("retrieved_at", now_iso())
    results:       list[dict[str, Any]] = raw.get("results", [])
    llm_text:      str  = raw.get("llm_text", "")
    tokens:        dict = raw.get("tokens", {})
    successful:    list[str] = raw.get("successful_queries", [])
    failed:        list[str] = raw.get("failed_queries", [])

    # ── Preserve queries used ─────────────────────────────────────────────
    for q in failed:
        missing.append(f"search_failed: {q}")

    # ── All searches failed ───────────────────────────────────────────────
    if not results:
        return AgentOutput(
            agent_name=agent_name,
            status="partial" if successful else "error",
            summary="宏观搜索结果不可用",
            missing_data=missing,
            latency_ms=latency_ms,
            token_usage={},
            regulatory_flags=[],
        )

    # ── LLM unavailable note ──────────────────────────────────────────────
    llm_used = bool(llm_text and tokens)
    if not llm_used:
        missing.append("llm_unavailable_used_raw_results")
        tokens = {
            "prompt_tokens":     len(_build_llm_prompt(results, successful).split()),
            "completion_tokens": 0,
        }

    # ── Parse LLM / fallback ──────────────────────────────────────────────
    findings, llm_risks, summary, data_gaps, unsafe_removed = _parse_llm_response(
        llm_text, results, successful
    )
    risks.extend(llm_risks)
    missing.extend(data_gaps)
    if unsafe_removed:
        missing.append("unsafe_llm_terms_removed")
        risks.append("LLM 输出包含确定性建议词，已清洗后再进入结果。")

    # ── Build SourceRefs ─────────────────────────────────────────────────
    sources: list[SourceRef] = []
    seen_urls: set[str] = set()
    for r in results[:_MAX_FINDINGS]:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(SourceRef(
                title=r.get("title", url),
                url=url,
                source=r.get("source", "web_search"),
                retrieved_at=retrieved_at,
                published_at=r.get("published_at") or None,
            ))

    n_findings = len(findings)
    if n_findings == 0:
        status = "error"
    elif missing:
        status = "partial"
    else:
        status = "ok"

    confidence = min(
        _MAX_CONFIDENCE,
        0.15 * n_findings + (0.1 if llm_used else 0.0),
    )

    return AgentOutput(
        agent_name=agent_name,
        status=status,
        summary=summary or f"获取到 {len(results)} 条宏观搜索结果",
        findings=findings,
        sources=sources,
        as_of=retrieved_at,
        confidence=confidence,
        risks=risks,
        missing_data=missing,
        latency_ms=latency_ms,
        token_usage=tokens,
        regulatory_flags=[],
    )


# ── Agent class ───────────────────────────────────────────────────────────────

class MacroAgent:
    """
    Macro signals research agent for CNY/AUD.

    Searches for RBA / PBoC / Fed / AUD macro news, then calls LLM once
    to classify direction signals.

    Protocol:
        agent.agent_name       → str
        await agent.run(task)  → AgentOutput

    Thread model: blocking I/O (search + LLM) runs in a short-lived private
    executor for each run.
    """

    agent_name: str = "macro_agent"

    @classmethod
    def close_executor(cls) -> None:
        """Backward-compatible no-op; executors are per-run."""
        return None

    async def run(self, task: ResearchTask) -> AgentOutput:
        """Search macro news, call LLM, return structured findings."""
        t0 = time.monotonic()

        try:
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="macro-agent",
            )
            raw: dict[str, Any] = await loop.run_in_executor(
                executor,
                _collect_and_analyse,
            )
        except Exception as exc:
            return AgentOutput.make_error(
                self.agent_name,
                error=f"macro agent failed: {exc}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        finally:
            if "executor" in locals():
                executor.shutdown(wait=True, cancel_futures=True)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return _build_macro_output(raw, task, latency_ms, self.agent_name)
