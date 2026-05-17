"""
Phase 10.6F — PolicySignalAgent MVP.

Dedicated agent for central bank policy signals:
  - RBA (Reserve Bank of Australia)
  - PBoC (People's Bank of China)
  - Fed (Federal Reserve / USD policy)

Uses search (Tavily or Google News RSS fallback) with focused queries per
policy bucket. Each bucket produces at most one Finding with conservative
direction assignment.

Feature flag: _ENABLE_POLICY_AGENT (default False).
When disabled, coordinator does not register this agent and MacroAgent
continues to handle policy signals as before.

Direction rules:
  - Only assign bullish_aud/bearish_aud when evidence is unambiguous.
  - Weak/contradictory evidence → neutral or direction=None.
  - Generic news headlines never produce strong directional signals.

Data gaps:
  - If official sources unavailable → data_gap finding with suggested_source.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
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
    from ..schema import AgentOutput, Finding, FindingCategory, ResearchTask, SourceRef, now_iso
    from ..llm_bridge import call_llm as _call_llm
    from ..structured_llm import call_json_with_repair, parse_json_object
    from ..agent_audit import audit_agent_start, audit_agent_end, audit_agent_error
    from ..source_metadata import SourceMetadata
except ImportError:
    from schema import AgentOutput, Finding, FindingCategory, ResearchTask, SourceRef, now_iso  # type: ignore[no-redef]
    from llm_bridge import call_llm as _call_llm  # type: ignore[no-redef]
    from structured_llm import call_json_with_repair, parse_json_object  # type: ignore[no-redef]
    from agent_audit import audit_agent_start, audit_agent_end, audit_agent_error  # type: ignore[no-redef]
    from source_metadata import SourceMetadata  # type: ignore[no-redef]

try:
    from pythonclaw.core.rate_limit import call_with_backoff
except Exception:  # noqa: BLE001
    def call_with_backoff(provider, func, *args, **kwargs):  # type: ignore[no-redef]
        return func(*args, **kwargs)

# ── Feature Flag ─────────────────────────────────────────────────────────────

_ENABLE_POLICY_AGENT: bool = True

# ── Constants ────────────────────────────────────────────────────────────────

def _current_year_range() -> str:
    """Return 'YYYY YYYY+1' based on current date to avoid hardcoded year drift."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return f"{now.year} {now.year + 1}"


def _get_policy_buckets() -> dict[str, dict[str, Any]]:
    """Build policy buckets with dynamic year range."""
    yr = _current_year_range()
    return {
        "policy_rba": {
            "queries": [
                f"RBA interest rate decision {yr}",
                "Reserve Bank Australia latest monetary policy statement",
            ],
            "entities": ["RBA", "AUD", "CNY", "CNYAUD"],
            "source_preference": ["rba.gov.au", "reuters.com", "afr.com"],
            "direction_for_aud_map": {
                "hawkish": "bullish",
                "dovish": "bearish",
                "neutral": "neutral",
            },
        },
        "policy_pboc": {
            "queries": [
                f"PBoC monetary policy rate decision {yr}",
                "中国人民银行 最新货币政策报告 利率决议",
            ],
            "entities": ["PBoC", "CNY", "AUD", "CNYAUD"],
            "source_preference": ["pbc.gov.cn", "reuters.com", "scmp.com"],
            "direction_for_aud_map": {
                "hawkish": "bearish",
                "dovish": "bullish",
                "neutral": "neutral",
            },
        },
        "policy_fed": {
            "queries": [
                f"Federal Reserve FOMC interest rate decision {yr}",
                "Fed latest monetary policy USD dollar outlook",
            ],
            "entities": ["Fed", "USD", "AUD", "CNY", "CNYAUD"],
            "source_preference": ["federalreserve.gov", "reuters.com", "bloomberg.com"],
            "direction_for_aud_map": {
                "hawkish": "bearish",
                "dovish": "bullish",
                "neutral": "neutral",
            },
        },
    }


_POLICY_BUCKETS: dict[str, dict[str, Any]] = _get_policy_buckets()

_MAX_RESULTS_PER_QUERY: int = 3
_MAX_RESULTS_TO_LLM: int = 12
_FETCH_TIMEOUT_SEC: int = 25

_LLM_MODEL: str = "deepseek-chat"
_LLM_MAX_TOKENS: int = 800

_SYSTEM_PROMPT: str = (
    "你是央行政策信号分析专家。"
    "你必须严格基于用户提供的搜索结果片段进行分析，不得添加或编造任何事实。"
    "仅当证据明确时才给出方向判断(hawkish/dovish/neutral)。"
    "如果证据不足或矛盾，必须输出 neutral 并在 confidence 中标注低值。"
    "禁止给出确定性的投资建议。"
    "输出内容必须是合法的 JSON 对象，不要包含任何 JSON 以外的文字。"
)

_VALID_STANCES = {"hawkish", "dovish", "neutral", "insufficient_evidence"}
_VALID_DIRECTIONS = {"bullish_aud", "bearish_aud", "neutral"}

_MIN_CONFIDENCE_FOR_DIRECTION: float = 0.5

# ── Source tier classification ───────────────────────────────────────────────

_TIER1_DOMAINS: tuple[str, ...] = (
    "rba.gov.au", "pbc.gov.cn", "federalreserve.gov",
    "gov.au", "gov.cn",
)

_TIER2_DOMAINS: tuple[str, ...] = (
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com",
    "afr.com",
)

_TIER3_DOMAINS: tuple[str, ...] = (
    "news.google.com", "bbc.com", "theguardian.com", "abc.net.au",
    "xinhua.net", "scmp.com",
)

_OFFICIAL_DOMAIN_KEYWORDS: tuple[str, ...] = _TIER1_DOMAINS + _TIER2_DOMAINS


def classify_source_tier(url: str) -> int:
    """Classify a URL into source tier (1=official, 2=quality finance, 3=mainstream, 4=unknown)."""
    url_lower = (url or "").lower()
    for d in _TIER1_DOMAINS:
        if d in url_lower:
            return 1
    for d in _TIER2_DOMAINS:
        if d in url_lower:
            return 2
    for d in _TIER3_DOMAINS:
        if d in url_lower:
            return 3
    return 4


def _best_source_tier(results: list[dict[str, Any]]) -> int:
    """Return the best (lowest number) source tier from results."""
    if not results:
        return 4
    return min(classify_source_tier(r.get("url", "")) for r in results)


def _is_aggregator_only(results: list[dict[str, Any]]) -> bool:
    """Check if all results come from tier 4 or aggregator sources."""
    if not results:
        return True
    return all(classify_source_tier(r.get("url", "")) >= 4 for r in results)


_TIER_SOURCE_TYPE_MAP: dict[int, str] = {
    1: "official_central_bank",
    2: "mainstream_financial_media",
    3: "general_news",
    4: "unknown",
}


def build_source_metadata_for_bucket(
    bucket_name: str,
    bucket_results: list[dict[str, Any]],
) -> SourceMetadata:
    """Build SourceMetadata for a policy finding based on its bucket results."""
    if not bucket_results:
        return SourceMetadata(
            provider="policy_signal_agent",
            source_type="unknown",
            source_tier=4,
            quality_reason=f"no_results_for_{bucket_name}",
            is_aggregator=False,
        )

    best_tier = _best_source_tier(bucket_results)
    aggregator_only = _is_aggregator_only(bucket_results)

    # Find best result for metadata
    best_result = min(bucket_results, key=lambda r: classify_source_tier(r.get("url", "")))
    url = best_result.get("url", "")
    domain = _extract_domain(url)
    title = (best_result.get("title") or "")[:100]

    source_type = _TIER_SOURCE_TYPE_MAP.get(best_tier, "unknown")
    quality_reason = f"policy_bucket:{bucket_name},best_tier={best_tier},domain={domain}"

    if aggregator_only:
        source_type = "aggregator"
        quality_reason = f"aggregator_only:{bucket_name},domain={domain}"

    return SourceMetadata(
        url=url,
        title=title,
        provider="policy_signal_agent",
        domain=domain,
        source_type=source_type,
        source_tier=best_tier,
        quality_reason=quality_reason,
        is_aggregator=aggregator_only,
        retrieved_at=now_iso(),
    )


# ── Search helpers ───────────────────────────────────────────────────────────

def _search_tavily(
    query: str,
    max_results: int,
    include_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]
        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            return []
        client = TavilyClient(api_key=api_key)
        kwargs: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        if include_domains:
            kwargs["include_domains"] = include_domains
        resp = client.search(**kwargs)
        results: list[dict[str, Any]] = []
        for r in resp.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:400],
                "source": r.get("url", ""),
                "query": query,
                "_search_method": "tavily",
            })
        return results
    except Exception:  # noqa: BLE001
        return []


def _search_google_news_rss(query: str, max_results: int) -> list[dict[str, Any]]:
    try:
        sys_path_parent = str(Path(__file__).parent.parent.parent)
        if sys_path_parent not in sys.path:
            sys.path.insert(0, sys_path_parent)
        from news_monitor import _fetch_google_news_rss  # type: ignore[import]
        items = _fetch_google_news_rss(query, max_items=max_results)
        results: list[dict[str, Any]] = []
        for item in items:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", item.get("url", "")),
                "snippet": item.get("title", ""),
                "source": item.get("link", item.get("url", "")),
                "query": query,
                "_search_method": "google_news_rss",
            })
        return results
    except Exception:  # noqa: BLE001
        return []


_BUCKET_PREFERRED_DOMAINS: dict[str, list[str]] = {
    "policy_rba": ["rba.gov.au", "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "afr.com"],
    "policy_pboc": ["pbc.gov.cn", "english.pbc.gov.cn", "reuters.com", "bloomberg.com", "ft.com", "scmp.com"],
    "policy_fed": ["federalreserve.gov", "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com"],
}


def _search_for_bucket(query: str, max_results: int, bucket_name: str) -> list[dict[str, Any]]:
    preferred = _BUCKET_PREFERRED_DOMAINS.get(bucket_name, [])
    if preferred:
        results = _search_tavily(query, max_results, include_domains=preferred)
        if results:
            return results
    results = _search_tavily(query, max_results)
    if results:
        return results
    return _search_google_news_rss(query, max_results)


def _has_official_source(results: list[dict[str, Any]]) -> bool:
    for r in results:
        url = (r.get("url") or r.get("source") or "").lower()
        for domain in _OFFICIAL_DOMAIN_KEYWORDS:
            if domain in url:
                return True
    return False


def _source_quality_score(results: list[dict[str, Any]]) -> float:
    if not results:
        return 0.0
    official_count = sum(
        1 for r in results
        if any(d in (r.get("url") or "").lower() for d in _OFFICIAL_DOMAIN_KEYWORDS)
    )
    ratio = official_count / len(results)
    return round(0.45 + ratio * 0.4, 2)


# ── LLM analysis ────────────────────────────────────────────────────────────

_OUTPUT_SCHEMA: str = """
{
  "rba_stance": "hawkish|dovish|neutral|insufficient_evidence",
  "rba_summary": "一句话描述",
  "rba_confidence": 0.0-1.0,
  "pboc_stance": "hawkish|dovish|neutral|insufficient_evidence",
  "pboc_summary": "一句话描述",
  "pboc_confidence": 0.0-1.0,
  "fed_stance": "hawkish|dovish|neutral|insufficient_evidence",
  "fed_summary": "一句话描述",
  "fed_confidence": 0.0-1.0
}
"""


def _build_user_prompt(results: list[dict[str, Any]]) -> str:
    lines = ["以下是搜索结果，请分析央行政策信号：\n"]
    for i, r in enumerate(results[:_MAX_RESULTS_TO_LLM], 1):
        title = (r.get("title") or "")[:120]
        snippet = (r.get("snippet") or "")[:300]
        url = r.get("url", "")
        lines.append(f"{i}. [{title}]({url})\n   {snippet}\n")
    lines.append(f"\n请输出 JSON，格式为：{_OUTPUT_SCHEMA}")
    return "\n".join(lines)


def _call_policy_llm(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {}
    user_prompt = _build_user_prompt(results)
    try:
        result = call_json_with_repair(
            _call_llm,
            user_prompt,
            _SYSTEM_PROMPT,
            max_tokens=_LLM_MAX_TOKENS,
            required_keys=("rba_stance", "pboc_stance", "fed_stance"),
            repair_retries=2,
            schema_hint=_OUTPUT_SCHEMA,
        )
        if result.ok:
            data = result.data or {}
            data["_token_usage"] = result.token_usage
            return data
        return {"_token_usage": result.token_usage}
    except Exception:  # noqa: BLE001
        return {}


# ── Direction inference ──────────────────────────────────────────────────────

def _stance_to_direction(stance: str, bucket_name: str) -> str | None:
    if stance not in _VALID_STANCES or stance == "insufficient_evidence":
        return None
    bucket_cfg = _POLICY_BUCKETS.get(bucket_name, {})
    direction_map = bucket_cfg.get("direction_for_aud_map", {})
    aud_dir = direction_map.get(stance)
    if aud_dir == "bullish":
        return "bullish_aud"
    if aud_dir == "bearish":
        return "bearish_aud"
    if aud_dir == "neutral":
        return "neutral"
    return None


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


# ── Findings builder ────────────────────────────────────────────────────────

def _build_findings(
    llm_output: dict[str, Any],
    all_results: list[dict[str, Any]],
    bucket_results: dict[str, list[dict[str, Any]]] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []

    bucket_to_llm_prefix = {
        "policy_rba": "rba",
        "policy_pboc": "pboc",
        "policy_fed": "fed",
    }

    for bucket_name, cfg in _POLICY_BUCKETS.items():
        prefix = bucket_to_llm_prefix[bucket_name]
        stance = str(llm_output.get(f"{prefix}_stance", "insufficient_evidence")).lower().strip()
        summary = str(llm_output.get(f"{prefix}_summary", "")).strip()
        confidence = float(llm_output.get(f"{prefix}_confidence", 0.0) or 0.0)

        if stance not in _VALID_STANCES:
            stance = "insufficient_evidence"

        # Determine bucket results and source IDs
        bucket_queries = set(q.lower() for q in cfg["queries"])
        b_results = (bucket_results or {}).get(bucket_name, [])
        if not b_results:
            b_results = [r for r in all_results if any(
                bq.split()[0] in (r.get("query") or "").lower() for bq in bucket_queries
            )]
        source_ids: list[str] = []
        for r in b_results:
            url = r.get("url", "")
            if url and url not in source_ids:
                source_ids.append(url)
        best_tier = _best_source_tier(b_results) if b_results else 4
        aggregator_only = _is_aggregator_only(b_results)

        # Direction gate: strict source quality requirement
        # Only tier 1-2 sources with sufficient confidence can produce directional signals
        direction: str | None = None
        if stance == "neutral":
            direction = "neutral"
        elif stance == "insufficient_evidence":
            direction = None
        elif confidence >= _MIN_CONFIDENCE_FOR_DIRECTION and not aggregator_only:
            if best_tier <= 2:
                direction = _stance_to_direction(stance, bucket_name)
            elif best_tier == 3 and confidence >= 0.7:
                direction = _stance_to_direction(stance, bucket_name)
            else:
                direction = "neutral"
        else:
            direction = None

        if not summary:
            summary = f"{bucket_name}: {stance}"

        importance = 0.8 if stance in ("hawkish", "dovish") and confidence >= 0.5 and best_tier <= 2 else 0.6
        evidence_score = min(0.85, confidence * 0.9) if confidence > 0 else 0.3
        # Downgrade evidence_score for aggregator-only
        if aggregator_only and confidence < 0.5:
            evidence_score = min(evidence_score, 0.35)

        finding = Finding(
            key=bucket_name,
            summary=summary,
            direction=direction,
            evidence_score=evidence_score,
            category=FindingCategory.POLICY_SIGNAL.value if hasattr(FindingCategory.POLICY_SIGNAL, "value") else "policy_signal",
            subcategory=f"{prefix}_policy",
            entities=list(cfg["entities"]),
            importance=importance,
            source_ids=source_ids[:5],
            time_sensitivity="quarterly",
            time_horizon="policy_cycle",
            evidence_basis=(
                f"policy_signal_agent:{bucket_name} from {len(source_ids)} source(s), "
                f"stance={stance}, conf={confidence:.2f}, tier={best_tier}"
            ),
            **_direction_fields(direction),
        )
        # Attach bucket-level SourceMetadata for EvidenceStore ingestion
        meta = build_source_metadata_for_bucket(bucket_name, b_results)
        finding._source_metadata_json = meta.to_json()
        findings.append(finding)

    return findings


def _build_data_gap_findings(missing_buckets: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    suggested_sources = {
        "policy_rba": "rba.gov.au/monetary-policy/",
        "policy_pboc": "pbc.gov.cn/english/",
        "policy_fed": "federalreserve.gov/monetarypolicy.htm",
    }
    for bucket in missing_buckets:
        cfg = _POLICY_BUCKETS.get(bucket, {})
        findings.append(Finding(
            key=f"{bucket}_data_gap",
            summary=f"{bucket}: 官方来源不可用",
            direction=None,
            evidence_score=0.1,
            category="data_gap",
            subcategory="policy_data_gap",
            entities=list(cfg.get("entities", ["AUD", "CNY"])),
            importance=0.4,
            source_ids=[],
            time_sensitivity="quarterly",
            evidence_basis=f"data_gap: no results for {bucket}, suggested={suggested_sources.get(bucket, '')}",
        ))
    return findings


# ── Main agent class ─────────────────────────────────────────────────────────

class PolicySignalAgent:
    agent_name: str = "policy_signal_agent"

    async def run(self, task: ResearchTask) -> AgentOutput:
        t0 = time.monotonic()
        task_id = getattr(task, "task_id", "")
        audit_agent_start(self.agent_name, task_id)

        try:
            output = await self._run_impl(task, t0)
            audit_agent_end(
                self.agent_name, task_id,
                status=output.status,
                latency_ms=output.latency_ms,
                finding_count=len(output.findings),
            )
            return output
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            audit_agent_error(self.agent_name, task_id, exc, latency_ms=latency)
            return AgentOutput.make_error(
                self.agent_name,
                f"{type(exc).__name__}: {exc}",
                latency_ms=latency,
            )

    async def _run_impl(self, task: ResearchTask, t0: float) -> AgentOutput:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="policy") as pool:
            result = await loop.run_in_executor(pool, self._collect_and_analyse, task)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return AgentOutput(
            agent_name=self.agent_name,
            status=result["status"],
            summary=result["summary"],
            findings=result["findings"],
            sources=result["sources"],
            confidence=result["confidence"],
            latency_ms=latency_ms,
            missing_data=result.get("missing_data", []),
            as_of=now_iso(),
            token_usage=result.get("token_usage"),
        )

    def _collect_and_analyse(self, task: ResearchTask) -> dict[str, Any]:
        # Generate fresh buckets at runtime to avoid year drift in long-running processes
        policy_buckets = _get_policy_buckets()

        all_results: list[dict[str, Any]] = []
        bucket_results: dict[str, list[dict[str, Any]]] = {}
        missing_buckets: list[str] = []

        for bucket_name, cfg in policy_buckets.items():
            bucket_hits: list[dict[str, Any]] = []
            for query in cfg["queries"]:
                hits = _search_for_bucket(query, _MAX_RESULTS_PER_QUERY, bucket_name)
                bucket_hits.extend(hits)
            if bucket_hits:
                bucket_results[bucket_name] = bucket_hits
                all_results.extend(bucket_hits)
            else:
                missing_buckets.append(bucket_name)

        sources: list[SourceRef] = []
        seen_urls: set[str] = set()
        for r in all_results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                domain = _extract_domain(url)
                tier = classify_source_tier(url)
                search_method = r.get("_search_method", "")
                if search_method == "google_news_rss":
                    source_label = "google_news_rss"
                elif search_method == "tavily":
                    source_label = "tavily"
                elif tier <= 2:
                    source_label = f"policy_signal_agent:{domain}"
                else:
                    source_label = domain
                sources.append(SourceRef(
                    url=url,
                    title=(r.get("title") or "")[:100],
                    source=source_label,
                    retrieved_at=now_iso(),
                ))

        if not all_results:
            gap_findings = _build_data_gap_findings(list(policy_buckets.keys()))
            return {
                "status": "partial",
                "summary": "PolicySignal: 所有搜索失败",
                "findings": gap_findings,
                "sources": [],
                "confidence": 0.2,
                "missing_data": [f"search_failed:{b}" for b in policy_buckets],
            }

        llm_output = _call_policy_llm(all_results)

        findings = _build_findings(llm_output, all_results, bucket_results)

        if missing_buckets:
            findings.extend(_build_data_gap_findings(missing_buckets))

        # Compute overall confidence
        confidences = [
            float(llm_output.get(f"{p}_confidence", 0) or 0)
            for p in ("rba", "pboc", "fed")
        ]
        avg_conf = sum(confidences) / max(1, len([c for c in confidences if c > 0]))
        has_official = _has_official_source(all_results)
        overall_conf = min(0.82, avg_conf * (1.0 if has_official else 0.8))

        status = "ok" if not missing_buckets else "partial"
        n_directional = sum(1 for f in findings if f.direction and f.direction != "neutral")
        summary = (
            f"PolicySignal: {len(bucket_results)}/3 buckets, "
            f"{n_directional} directional, "
            f"conf={overall_conf:.2f}"
        )

        return {
            "status": status,
            "summary": summary,
            "findings": findings,
            "sources": sources[:8],
            "confidence": round(overall_conf, 2),
            "missing_data": [f"search_failed:{b}" for b in missing_buckets],
            "token_usage": llm_output.get("_token_usage"),
        }


def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or url[:40]
    except Exception:  # noqa: BLE001
        return url[:40]
