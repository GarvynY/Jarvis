"""
Phase 10A — Evidence Scorer MVP.

Rule-based, deterministic evidence scoring for attention-inspired routing.
No LLM calls, no external APIs, no Telegram modifications.

Scoring formula (composite_score):
    importance      × 0.30
  + confidence      × 0.20
  + recency_score   × 0.20
  + source_quality  × 0.15
  + user_relevance  × 0.10
  + conflict_value   × 0.05

All sub-scores and the composite are clamped to [0.0, 1.0].
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from schema import EvidenceChunk, SafeUserContext

try:
    from schema import EvidenceChunk as _EC, SafeUserContext as _SUC, now_iso
except ImportError:
    from .schema import EvidenceChunk as _EC, SafeUserContext as _SUC, now_iso  # type: ignore[no-redef]

try:
    from source_metadata import SourceMetadata, tier_to_quality_score
except ImportError:
    try:
        from .source_metadata import SourceMetadata, tier_to_quality_score  # type: ignore[no-redef]
    except ImportError:
        SourceMetadata = None  # type: ignore[assignment,misc]
        tier_to_quality_score = None  # type: ignore[assignment]


# ── Weights ──────────────────────────────────────────────────────────────────

W_IMPORTANCE: float = 0.30
W_CONFIDENCE: float = 0.20
W_RECENCY: float = 0.20
W_SOURCE_QUALITY: float = 0.15
W_USER_RELEVANCE: float = 0.10
W_CONFLICT: float = 0.05

# ── Recency decay ────────────────────────────────────────────────────────────

_RECENCY_HALF_LIFE_HOURS: float = 12.0

# ── Source quality patterns ──────────────────────────────────────────────────

_OFFICIAL_DOMAINS: tuple[str, ...] = (
    "rba.gov.au",
    "pbc.gov.cn",
    "pboc.gov.cn",
    "federalreserve.gov",
    "federalreserve.gov.au",
    "treasury.gov.au",
    "abs.gov.au",
    "imf.org",
    "worldbank.org",
    "bis.org",
    "ecb.europa.eu",
    "oecd.org",
    "boc.cn",
)

_OFFICIAL_TEXT_PATTERNS: tuple[str, ...] = (
    "reserve bank of australia",
    "monetary policy decision",
    "statement by the monetary policy board",
    "people's bank of china",
    "people’s bank of china",
    "pboc",
    "central bank",
    "official file",
    "official document",
    "government",
    "treasury",
    "bank fx boards",
    "chinese bank fx boards",
)

_PREMIUM_NEWS_DOMAINS: tuple[str, ...] = (
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "economist.com",
)

_PREMIUM_NEWS_TEXT_PATTERNS: tuple[str, ...] = (
    "reuters",
    "bloomberg",
    "financial times",
    "wall street journal",
    "the economist",
)

_MAINSTREAM_NEWS_DOMAINS: tuple[str, ...] = (
    "cnbc.com",
    "bbc.com",
    "bbc.co.uk",
    "theguardian.com",
    "nytimes.com",
    "aljazeera.com",
    "afr.com",
    "xinhua.net",
    "xinhuanet.com",
    "nikkei.com",
    "smh.com.au",
)

_MAINSTREAM_NEWS_TEXT_PATTERNS: tuple[str, ...] = (
    "cnbc",
    "bbc",
    "guardian",
    "new york times",
    "al jazeera",
    "xinhua",
    "nikkei",
)

_MARKET_BLOG_DOMAINS: tuple[str, ...] = (
    "marketpulse.com",
    "investing.com",
    "fxstreet.com",
    "dailyfx.com",
    "forexlive.com",
    "tradingview.com",
    "tmgm.com",
    "ig.com",
    "oanda.com",
)

_CRYPTO_LOW_AUTHORITY_DOMAINS: tuple[str, ...] = (
    "binance.com",
    "cryptorank.io",
    "coinmarketcap.com",
    "coindesk.com",
    "cointelegraph.com",
    "crypto.com",
)

_PROVIDER_ONLY_SOURCES: tuple[str, ...] = (
    "google_news_rss",
    "tavily",
    "web_search",
)

_TOPIC_CATEGORY_BOOSTS: tuple[tuple[tuple[str, ...], dict[str, float], tuple[str, ...]], ...] = (
    (
        ("地缘政治", "地缘政治风险", "中东", "中东局势", "霍尔木兹", "hormuz", "geopolitical"),
        {"news_event": 0.82, "macro": 0.76, "risk": 0.78, "fx_price": 0.45},
        ("地缘", "中东", "霍尔木兹", "hormuz", "iran", "geopolitical", "risk sentiment"),
    ),
    (
        ("能源", "能源风险", "能源价格", "油价", "oil", "opec", "commodity"),
        {"news_event": 0.82, "macro": 0.78, "risk": 0.72, "fx_price": 0.45},
        ("能源", "油价", "oil", "opec", "commodity", "大宗商品", "通胀", "inflation"),
    ),
    (
        ("澳元", "澳元走势", "商品货币", "aud", "cny/aud", "汇率"),
        {"fx_price": 0.78, "macro": 0.72, "news_event": 0.65, "risk": 0.55},
        ("澳元", "aud", "cny/aud", "commodity currency", "汇率", "换汇"),
    ),
    (
        ("rba", "澳洲央行", "澳大利亚预算", "通胀", "利率", "央行", "pboc", "中国央行"),
        {"macro": 0.80, "news_event": 0.68, "risk": 0.58, "fx_price": 0.45},
        ("rba", "pboc", "央行", "利率", "通胀", "inflation", "budget", "monetary"),
    ),
    (
        ("银行", "中国银行", "牌价", "现汇", "bank"),
        {"fx_price": 0.75, "risk": 0.45},
        ("银行", "中国银行", "bank", "牌价", "现汇"),
    ),
)

_QUALITY_NEGATIVE_PATTERNS: tuple[str, ...] = (
    "逻辑太浅",
    "shallow",
    "单纯技术分析",
    "technical signs",
    "technical analysis",
    "短期情绪",
    "pullback",
    "缺乏数据",
    "无数据支撑",
    "重复旧新闻",
)


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    """Per-dimension score breakdown for debugging."""
    importance: float = 0.0
    confidence: float = 0.0
    recency_score: float = 0.0
    source_quality_score: float = 0.0
    user_relevance_score: float = 0.0
    conflict_value: float = 0.0


@dataclass
class EvidenceScore:
    """Composite score for one EvidenceChunk."""
    chunk_id: str = ""
    composite_score: float = 0.0
    attention_score: float = 0.0
    importance: float = 0.0
    confidence: float = 0.0
    recency_score: float = 0.0
    source_quality_score: float = 0.0
    user_relevance_score: float = 0.0
    conflict_value: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "composite_score": self.composite_score,
            "attention_score": self.attention_score,
            "importance": self.importance,
            "confidence": self.confidence,
            "recency_score": self.recency_score,
            "source_quality_score": self.source_quality_score,
            "user_relevance_score": self.user_relevance_score,
            "conflict_value": self.conflict_value,
            "reason": self.reason,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def _safe_float(value: object, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp. Returns None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _source_fields(chunk: _EC) -> tuple[str, str, str]:
    """Return (url-ish text, title-ish text, fallback source text)."""
    source = str(getattr(chunk, "source", None) or "").strip()
    url = str(getattr(chunk, "source_url", "") or "").strip()
    title = str(getattr(chunk, "source_title", "") or "").strip()

    # EvidenceStore stores SourceRef metadata in a compact text block so older
    # EvidenceChunk schema versions can still carry URL/title signal.
    if not url:
        m = re.search(r"https?://[^\s,;|)]+", source)
        if m:
            url = m.group(0).rstrip("。.,;")
    if not title:
        m = re.search(r"title=([^|]+)", source, flags=re.IGNORECASE)
        if m:
            title = m.group(1).strip()
    return url, title, source


def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if re.match(r"^https?://", url, re.I) else f"https://{url}")
    host = (parsed.netloc or parsed.path).lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def _domain_matches(domain: str, candidates: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in candidates)


def _text_has_any(text: str, patterns: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(p in lower for p in patterns)


def _normalise_terms(values: object) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    terms: list[str] = []
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except TypeError:
        return []
    for value in iterator:
        text = str(value or "").strip().lower()
        if text:
            terms.append(text)
    return terms


def _chunk_search_text(chunk: _EC) -> str:
    parts = [
        getattr(chunk, "category", "") or "",
        getattr(chunk, "content", "") or "",
        getattr(chunk, "source", "") or "",
        " ".join(getattr(chunk, "entities", None) or []),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def _topic_relevance_boost(
    *,
    category: str,
    chunk_text: str,
    positive_terms: list[str],
) -> float:
    best = 0.0
    for aliases, category_boosts, evidence_keywords in _TOPIC_CATEGORY_BOOSTS:
        if not any(alias.lower() in term for term in positive_terms for alias in aliases):
            continue
        category_score = category_boosts.get(category, 0.0)
        if category_score:
            best = max(best, category_score)
        if any(keyword.lower() in chunk_text for keyword in evidence_keywords):
            best = max(best, min(0.9, category_score + 0.08 if category_score else 0.72))
    return best


def _quality_penalty(
    *,
    chunk_text: str,
    negative_terms: list[str],
    category_feedback_summary: dict[str, float] | None,
) -> float:
    has_quality_dislike = any(
        "逻辑太浅" in term
        or "shallow" in term
        or "技术" in term
        or "短期情绪" in term
        for term in negative_terms
    )
    if category_feedback_summary:
        try:
            quality_feedback = float(category_feedback_summary.get("news_article_quality", 0.0) or 0.0)
        except (TypeError, ValueError):
            quality_feedback = 0.0
        has_quality_dislike = has_quality_dislike or quality_feedback < 0
    if not has_quality_dislike:
        return 0.0
    if any(pattern.lower() in chunk_text for pattern in _QUALITY_NEGATIVE_PATTERNS):
        return 0.25
    return 0.0


# ── Sub-score functions ──────────────────────────────────────────────────────

def compute_recency_score(
    chunk: _EC,
    now_iso_str: str | None = None,
) -> float:
    """Exponential decay based on chunk age. Half-life = 12 hours.

    Returns 1.0 for just-created chunks, decaying toward 0.0.
    Invalid or missing timestamps return 0.5 (neutral).
    """
    ts = getattr(chunk, "created_at", "")
    dt = _parse_iso(ts)
    if dt is None:
        return 0.5

    if now_iso_str:
        now = _parse_iso(now_iso_str)
        if now is None:
            now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
    score = 0.5 ** (age_hours / _RECENCY_HALF_LIFE_HOURS)
    return _clamp(round(score, 4))


def compute_source_quality_score(chunk: _EC) -> float:
    """Deterministic source quality using structured metadata first, legacy fallback.

    Priority:
      1. If chunk.source_metadata_json is valid and has source_tier → use tier_to_quality_score
      2. Otherwise, fall back to URL/domain/title heuristic (legacy path)

    Returns representative bands:
        0.95 — official central bank / regulator / government / bank board
        0.82 — Reuters / Bloomberg / FT / WSJ / official-file style reporting
        0.68 — mainstream news media
        0.45 — market blogs / broker portals / trading sites
        0.32 — crypto venues or low-authority crypto content for FX macro
        0.40 — provider-only labels such as google_news_rss without URL/title
        0.20 — empty source metadata
    """
    # Phase 10.6A: prefer structured SourceMetadata
    if SourceMetadata is not None and tier_to_quality_score is not None:
        raw_meta = getattr(chunk, "source_metadata_json", None) or "{}"
        if raw_meta and raw_meta != "{}":
            try:
                meta = SourceMetadata.from_json(raw_meta)
                if meta.source_type != "unknown" or meta.source_tier != 3:
                    return tier_to_quality_score(meta.source_tier)
            except Exception:
                pass

    # Legacy fallback path
    url, title, source = _source_fields(chunk)
    domain = _domain_from_url(url)
    text = " ".join(part for part in (title, source) if part).strip()

    if not any((domain, text)):
        return 0.2

    if domain:
        if _domain_matches(domain, _OFFICIAL_DOMAINS) or domain.endswith(".gov") or ".gov." in domain:
            return 0.95
        if _domain_matches(domain, _PREMIUM_NEWS_DOMAINS):
            return 0.82
        if _domain_matches(domain, _MAINSTREAM_NEWS_DOMAINS):
            return 0.68
        if _domain_matches(domain, _CRYPTO_LOW_AUTHORITY_DOMAINS):
            return 0.32
        if _domain_matches(domain, _MARKET_BLOG_DOMAINS):
            return 0.45

    if _text_has_any(text, _OFFICIAL_TEXT_PATTERNS):
        return 0.90
    if _text_has_any(text, _PREMIUM_NEWS_TEXT_PATTERNS):
        return 0.80
    if _text_has_any(text, _MAINSTREAM_NEWS_TEXT_PATTERNS):
        return 0.65
    if _text_has_any(text, ("binance", "cryptorank", "crypto exchange", "cryptocurrency exchange")):
        return 0.32
    if _text_has_any(text, ("marketpulse", "broker", "trading portal", "technical signs", "aud/usd pullback")):
        return 0.45

    if source.lower() in _PROVIDER_ONLY_SOURCES:
        return 0.4

    return 0.4


def compute_user_relevance_score(
    chunk: _EC,
    safe_user_context: _SUC | None = None,
    category_feedback_summary: dict[str, float] | None = None,
) -> float:
    """Score boost when chunk category/entities match preferences or feedback.

    Returns:
        0.8  — category or entity matches preferred_topics
        0.3  — no match or no user context/feedback
    """
    base = 0.3
    prefs = []
    preferred_banks: list[str] = []
    inferred_high_terms: list[str] = []
    inferred_low_terms: list[str] = []
    purpose = ""
    target_rate = None
    if safe_user_context is not None:
        prefs = getattr(safe_user_context, "preferred_topics", None) or []
        preferred_banks = _normalise_terms(getattr(safe_user_context, "preferred_banks", None))
        purpose = str(getattr(safe_user_context, "purpose", "") or "").strip().lower()
        target_rate = getattr(safe_user_context, "target_rate", None)
        inferred_high_terms = _normalise_terms(
            getattr(safe_user_context, "inferred_high_interest_topics", None)
        )
        inferred_low_terms = _normalise_terms(
            getattr(safe_user_context, "inferred_low_interest_topics", None)
        )
        if category_feedback_summary is None:
            maybe_summary = getattr(safe_user_context, "category_feedback_summary", None)
            if isinstance(maybe_summary, dict):
                category_feedback_summary = maybe_summary

    prefs_lower = {p.lower() for p in prefs}
    positive_terms = sorted(set(prefs_lower) | set(preferred_banks) | set(inferred_high_terms))
    negative_terms = inferred_low_terms

    category = (getattr(chunk, "category", "") or "").lower()
    if category == "fx_price" and (purpose in {"living", "tuition", "remittance"} or target_rate is not None):
        base = max(base, 0.5)

    if category and category in prefs_lower:
        base = 0.8

    entities = getattr(chunk, "entities", None) or []
    for ent in entities:
        if ent.lower() in prefs_lower:
            base = 0.8
            break

    chunk_text = _chunk_search_text(chunk)
    for term in positive_terms:
        if term and term in chunk_text:
            base = max(base, 0.82)
            break

    topic_boost = _topic_relevance_boost(
        category=category,
        chunk_text=chunk_text,
        positive_terms=positive_terms,
    )
    if topic_boost:
        base = max(base, topic_boost)

    feedback_score = None
    if category_feedback_summary and category:
        feedback_score = category_feedback_summary.get(category)
        if feedback_score is None:
            news_tag_score = category_feedback_summary.get("news_tag")
            if news_tag_score is not None and category in {"news_event", "macro", "risk", "fx_price"}:
                feedback_score = float(news_tag_score) * {
                    "news_event": 0.85,
                    "macro": 0.70,
                    "risk": 0.60,
                    "fx_price": 0.35,
                }.get(category, 0.0)
    if feedback_score is not None:
        try:
            value = max(-1.0, min(1.0, float(feedback_score)))
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            base = max(base, min(0.9, 0.5 + value * 0.4))
        elif value < 0:
            base = min(base, max(0.1, 0.3 + value * 0.2))

    penalty = _quality_penalty(
        chunk_text=chunk_text,
        negative_terms=negative_terms,
        category_feedback_summary=category_feedback_summary,
    )
    if penalty:
        base = max(0.1, base - penalty)

    return _clamp(round(base, 4))


# ── Main scoring function ───────────────────────────────────────────────────

def compute_evidence_score(
    chunk: _EC,
    safe_user_context: _SUC | None = None,
    *,
    now_iso_str: str | None = None,
    category_feedback_summary: dict[str, float] | None = None,
    conflict_value: float | None = None,
) -> EvidenceScore:
    """Compute composite evidence score for a single chunk.

    Pure computation, no I/O. Safe to call in tight loops.
    """
    imp = _clamp(getattr(chunk, "importance", 0.0))
    conf = _clamp(getattr(chunk, "confidence", 0.0))
    rec = compute_recency_score(chunk, now_iso_str=now_iso_str)
    sq = compute_source_quality_score(chunk)
    ur = compute_user_relevance_score(
        chunk,
        safe_user_context,
        category_feedback_summary=category_feedback_summary,
    )
    cv = _clamp(_safe_float(
        getattr(chunk, "conflict_value", 0.0)
        if conflict_value is None
        else conflict_value
    ))

    composite = _clamp(round(
        imp * W_IMPORTANCE
        + conf * W_CONFIDENCE
        + rec * W_RECENCY
        + sq * W_SOURCE_QUALITY
        + ur * W_USER_RELEVANCE
        + cv * W_CONFLICT,
        4,
    ))

    parts: list[str] = []
    if imp >= 0.7:
        parts.append("high_imp")
    if conf >= 0.7:
        parts.append("high_conf")
    if rec >= 0.8:
        parts.append("fresh")
    elif rec <= 0.3:
        parts.append("stale")
    if sq >= 0.9:
        parts.append("official")
    if ur >= 0.8:
        parts.append("user_match")
    reason = ",".join(parts) if parts else "baseline"

    return EvidenceScore(
        chunk_id=getattr(chunk, "chunk_id", ""),
        composite_score=composite,
        attention_score=composite,
        importance=imp,
        confidence=conf,
        recency_score=rec,
        source_quality_score=sq,
        user_relevance_score=ur,
        conflict_value=cv,
        reason=reason,
    )


# ── Fallback ─────────────────────────────────────────────────────────────────

def fallback_score(chunk: _EC) -> EvidenceScore:
    """Safe fallback using only importance and confidence (no timestamp/source)."""
    imp = _clamp(getattr(chunk, "importance", 0.0))
    conf = _clamp(getattr(chunk, "confidence", 0.0))
    composite = _clamp(round(imp * 0.6 + conf * 0.4, 4))

    return EvidenceScore(
        chunk_id=getattr(chunk, "chunk_id", ""),
        composite_score=composite,
        attention_score=composite,
        importance=imp,
        confidence=conf,
        recency_score=0.5,
        source_quality_score=0.4,
        user_relevance_score=0.3,
        conflict_value=0.0,
        reason="fallback",
    )
