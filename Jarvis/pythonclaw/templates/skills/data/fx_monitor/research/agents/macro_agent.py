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

LLM call (at most three calls: one initial call plus two JSON-repair retries):
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
import copy
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
    from ..structured_llm import call_json_with_repair, parse_json_object
except ImportError:
    from schema import AgentOutput, Finding, ResearchTask, SourceRef, now_iso  # type: ignore[no-redef]
    from llm_bridge import call_llm as _call_llm  # type: ignore[no-redef]
    from structured_llm import call_json_with_repair, parse_json_object  # type: ignore[no-redef]

try:
    from pythonclaw.core.rate_limit import call_with_backoff
except Exception:  # noqa: BLE001 - research agents can run from skill dir.
    def call_with_backoff(provider, func, *args, **kwargs):  # type: ignore[no-redef]
        return func(*args, **kwargs)

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

_LLM_MODEL:      str = "deepseek-chat"
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

# Only these high-level signal keys may carry a directional claim.
_DIRECTIONAL_KEYS: frozenset[str] = frozenset({
    "macro_rba", "macro_pboc", "macro_usd", "macro_trade",
})

_CACHE_TTL_SECONDS: float = 180.0
_COLLECT_CACHE: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}

_MACRO_ENTITY_MAP: dict[str, list[str]] = {
    "macro_rba": ["RBA", "AUD", "CNY", "CNYAUD"],
    "macro_pboc": ["PBoC", "CNY", "AUD", "CNYAUD"],
    "macro_usd": ["Fed", "USD", "AUD", "CNY", "CNYAUD"],
    "macro_trade": ["AUD", "CNY", "China", "Australia", "CNYAUD"],
}

_MACRO_SUBCATEGORY_MAP: dict[str, str] = {
    "macro_rba": "rba_policy",
    "macro_pboc": "pboc_policy",
    "macro_usd": "fed_usd_policy",
    "macro_trade": "trade_macro",
}


def _direction_fields(direction: str | None) -> dict[str, str | None]:
    if direction == "bullish_aud":
        return {
            "direction_for_aud": "bullish",
            "direction_for_cny": "bearish",
            "direction_for_pair": direction,
        }
    if direction == "bearish_aud":
        return {
            "direction_for_aud": "bearish",
            "direction_for_cny": "bullish",
            "direction_for_pair": direction,
        }
    if direction == "neutral":
        return {
            "direction_for_aud": "neutral",
            "direction_for_cny": "neutral",
            "direction_for_pair": direction,
        }
    return {
        "direction_for_aud": None,
        "direction_for_cny": None,
        "direction_for_pair": None,
    }


def _macro_signal_finding(
    *,
    key: str,
    summary: str,
    direction: str | None,
    source_ids: list[str],
) -> Finding:
    category = "policy_signal" if key in {"macro_rba", "macro_pboc", "macro_usd"} else "macro"
    return Finding(
        key=key,
        summary=summary,
        direction=direction,
        evidence_score=0.7 if source_ids else 0.45,
        category=category,
        subcategory=_MACRO_SUBCATEGORY_MAP.get(key, ""),
        entities=list(_MACRO_ENTITY_MAP.get(key, ["AUD", "CNY", "CNYAUD"])),
        importance=0.8 if key in {"macro_rba", "macro_pboc", "macro_usd"} else 0.65,
        source_ids=source_ids,
        time_sensitivity="quarterly",
        time_horizon="policy_cycle",
        evidence_basis=f"{key} from {len(source_ids)} matched macro source(s)",
        **_direction_fields(direction),
    )


def _infer_macro_detail_entities(result: dict[str, Any]) -> list[str]:
    text = " ".join(str(result.get(k, "") or "") for k in ("query", "title", "snippet")).lower()
    entities: list[str] = []
    checks = [
        ("rba", "RBA"),
        ("reserve bank", "RBA"),
        ("pboc", "PBoC"),
        ("people's bank", "PBoC"),
        ("fed", "Fed"),
        ("federal reserve", "Fed"),
        ("usd", "USD"),
        ("dollar", "USD"),
        ("aud", "AUD"),
        ("australia", "AUD"),
        ("cny", "CNY"),
        ("yuan", "CNY"),
        ("china", "CNY"),
    ]
    for needle, entity in checks:
        if needle in text and entity not in entities:
            entities.append(entity)
    for entity in ("AUD", "CNY", "CNYAUD"):
        if entity not in entities:
            entities.append(entity)
    return entities


def _copy_finding_with_direction(f: Finding, direction: str | None) -> Finding:
    return Finding(
        key=f.key,
        summary=f.summary,
        direction=direction,
        evidence_score=f.evidence_score,
        attention_score=f.attention_score,
        category=f.category,
        importance=f.importance,
        source_ids=f.source_ids,
        time_sensitivity=f.time_sensitivity,
        subcategory=f.subcategory,
        entities=f.entities,
        direction_for_aud=_direction_fields(direction)["direction_for_aud"],
        direction_for_cny=_direction_fields(direction)["direction_for_cny"],
        direction_for_pair=_direction_fields(direction)["direction_for_pair"],
        time_horizon=f.time_horizon,
        evidence_basis=f.evidence_basis,
    )


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
        response = call_with_backoff(
            "tavily",
            client.search,
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


# _call_llm is imported from llm_bridge (Anthropic → DeepSeek fallback)


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

    def _source_ids_for_signal(field: str) -> list[str]:
        """Return compact, signal-specific source URLs instead of all results."""
        patterns = {
            "rba_signal":  ("rba", "reserve bank", "澳联储"),
            "pboc_signal": ("pboc", "people's bank", "central bank", "yuan", "中国央行", "人民币"),
            "usd_signal":  ("fed", "federal reserve", "usd", "dollar", "美元", "美联储"),
        }.get(field, ())
        urls: list[str] = []
        for r in results[:_MAX_RESULTS_TO_LLM]:
            haystack = " ".join(
                str(r.get(k, "") or "")
                for k in ("query", "title", "snippet", "source")
            ).lower()
            if patterns and not any(p.lower() in haystack for p in patterns):
                continue
            url = str(r.get("url", "") or "").strip()
            if url and url not in urls:
                urls.append(url)
            if len(urls) >= 2:
                break
        return urls

    if text:
        try:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = parse_json_object(text)
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
                    source_ids = _source_ids_for_signal(field)
                    findings.append(_macro_signal_finding(
                        key=key,
                        summary=f"{label}: {direction.replace('_', ' ')}",
                        direction=direction,
                        source_ids=source_ids,
                    ))

                # Detail findings are news headlines — they carry no independent
                # directional analysis, so direction is always None.
                for i, r in enumerate(results[: max(0, _MAX_FINDINGS - len(findings))]):
                    title = r.get("title", "").strip()
                    if not title:
                        continue
                    findings.append(Finding(
                        key=f"macro_detail_{i}",
                        summary=title[:220],
                        direction=None,
                        evidence_score=0.55,
                        category="macro",
                        subcategory="macro_detail",
                        entities=_infer_macro_detail_entities(r),
                        importance=0.55,
                        source_ids=[r.get("url", "")] if r.get("url") else [],
                        time_sensitivity="quarterly",
                        time_horizon="news_cycle",
                        evidence_basis="macro search result title/snippet",
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
                evidence_score=0.45,
                category="macro",
                subcategory="raw_search_result",
                entities=_infer_macro_detail_entities(r),
                importance=0.45,
                source_ids=[r.get("url", "")] if r.get("url") else [],
                time_sensitivity="quarterly",
                time_horizon="news_cycle",
                evidence_basis="raw macro search result; LLM unavailable",
            ))
    summary = f"基于 {len(results)} 条搜索结果（LLM分析不可用）"
    return findings, [], summary, [], False


# ── Direction stabilisation ───────────────────────────────────────────────────

def _stabilise_directions(
    findings: list[Finding],
    results: list[dict[str, Any]],
) -> tuple[list[Finding], list[str]]:
    """Post-process findings to ensure direction stability.

    Rules:
      1. Only _DIRECTIONAL_KEYS may carry a non-None direction.
      2. Invalid direction values are reset to None.
      3. macro_usd: if no USD-related search results exist, direction is
         downgraded to None and a data_gap is emitted.
      4. macro_detail_* / macro_raw_* always get direction=None.

    Returns (stabilised_findings, extra_data_gaps).
    """
    usd_patterns = ("fed", "federal reserve", "usd", "dollar", "美元", "美联储")
    has_usd_evidence = any(
        any(p in " ".join(str(r.get(k, "") or "") for k in ("query", "title", "snippet")).lower()
            for p in usd_patterns)
        for r in results
    )

    extra_gaps: list[str] = []
    stable: list[Finding] = []
    for f in findings:
        if f.key not in _DIRECTIONAL_KEYS:
            if f.direction is not None:
                f = _copy_finding_with_direction(f, None)
            stable.append(f)
            continue

        direction = f.direction
        if direction is not None and direction not in _VALID_DIRECTIONS:
            direction = None

        if f.key == "macro_usd" and not has_usd_evidence:
            if direction and direction != "neutral":
                direction = None
                extra_gaps.append("macro_usd_no_source_evidence")

        stable.append(_copy_finding_with_direction(f, direction))
    return stable, extra_gaps


# ── Combined blocking task ────────────────────────────────────────────────────

def _collect_and_analyse() -> dict[str, Any]:
    """
    Blocking: run search queries + call LLM with structured-output repair.

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
    result = call_json_with_repair(
        _call_llm,
        prompt,
        _SYSTEM_PROMPT,
        max_tokens=_LLM_MAX_TOKENS,
        required_keys=("summary", "overall_direction", "risks", "data_gaps"),
        repair_retries=2,
        schema_hint=(
            "{\n"
            '  "summary": "...",\n'
            '  "rba_signal": "bullish_aud|bearish_aud|neutral|unknown",\n'
            '  "pboc_signal": "bullish_aud|bearish_aud|neutral|unknown",\n'
            '  "usd_signal": "bullish_aud|bearish_aud|neutral|unknown",\n'
            '  "overall_direction": "bullish_aud|bearish_aud|mixed|neutral",\n'
            '  "key_findings": [],\n'
            '  "risks": [],\n'
            '  "data_gaps": []\n'
            "}"
        ),
    )
    return {
        "results": results,
        "llm_text": result.text if result.ok else "",
        "tokens": result.token_usage if result.ok else {},
        "structured_error": result.error,
        "successful_queries": successful,
        "failed_queries": failed,
        "retrieved_at": retrieved_at,
    }


def _collect_and_analyse_cached() -> dict[str, Any]:
    """Return cached macro research data for short repeated invocations."""
    now = time.monotonic()
    cache_key = (id(_search_once), id(_call_llm))
    cached = _COLLECT_CACHE.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        cached_raw = cached[1]
        raw = copy.deepcopy(cached_raw)
        raw["cache_hit"] = True
        raw["tokens"] = {"prompt_tokens": 0, "completion_tokens": 0}
        return raw

    raw = _collect_and_analyse()
    _COLLECT_CACHE[cache_key] = (now, copy.deepcopy(raw))
    raw = copy.deepcopy(raw)
    raw["cache_hit"] = False
    return raw


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
    cache_hit = bool(raw.get("cache_hit"))
    llm_used = bool(llm_text and (tokens or cache_hit))
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
    findings, direction_gaps = _stabilise_directions(findings, results)
    risks.extend(llm_risks)
    missing.extend(data_gaps)
    missing.extend(direction_gaps)
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
                _collect_and_analyse_cached,
            )
        except Exception as exc:
            return AgentOutput.make_error(
                self.agent_name,
                error=f"macro agent failed: {exc}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        finally:
            if "executor" in locals():
                executor.shutdown(wait=False, cancel_futures=True)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return _build_macro_output(raw, task, latency_ms, self.agent_name)
