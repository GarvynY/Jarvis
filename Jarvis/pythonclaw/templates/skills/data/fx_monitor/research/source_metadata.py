"""
Phase 10.6A — SourceMetadata 强契约。

结构化源元数据，替代旧版 "url=... | title=... | provider=..." 字符串。
向后兼容：不移除 SourceRef，不移除 EvidenceChunk.source。

提供：
  - SourceMetadata dataclass
  - normalize_domain(url) -> str
  - source_metadata_from_source_ref(source_ref) -> SourceMetadata
  - source_metadata_from_legacy_string(source) -> SourceMetadata
  - infer_source_type_and_tier(metadata) -> SourceMetadata
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from urllib.parse import urlparse
from typing import Any

try:
    from schema import SourceRef
except ImportError:
    from .schema import SourceRef  # type: ignore[no-redef]


# ── Source types ─────────────────────────────────────────────────────────────

SOURCE_TYPES = (
    "official_central_bank",
    "official_statistics",
    "official_government",
    "bank_fx_board",
    "market_data_api",
    "mainstream_financial_media",
    "general_news",
    "broker_research",
    "market_blog",
    "crypto_or_exchange",
    "aggregator",
    "unknown",
)

# ── Tier definitions ─────────────────────────────────────────────────────────
# Tier 1: Central bank, official statistics, official government
# Tier 2: Bank FX boards, regulated market data, Reuters/Bloomberg/FT/WSJ
# Tier 3: Mainstream media / general financial media
# Tier 4: Broker blogs / trading portals / unknown publishers
# Tier 5: Low quality / crypto / spam / unknown weak sources

# ── Domain → type/tier lookup tables ─────────────────────────────────────────

_CENTRAL_BANK_DOMAINS: tuple[str, ...] = (
    "rba.gov.au",
    "pbc.gov.cn",
    "pboc.gov.cn",
    "federalreserve.gov",
    "treasury.gov.au",
    "abs.gov.au",
    "imf.org",
    "worldbank.org",
    "bis.org",
    "ecb.europa.eu",
    "oecd.org",
    "boc.cn",
    "boj.or.jp",
    "bankofengland.co.uk",
)

_PREMIUM_MEDIA_DOMAINS: tuple[str, ...] = (
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "economist.com",
)

_MAINSTREAM_MEDIA_DOMAINS: tuple[str, ...] = (
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
    "ndtv.com",
    "cnn.com",
    "abc.net.au",
)

_BANK_FX_DOMAINS: tuple[str, ...] = (
    "icbc.com.cn",
    "boc.cn",
    "ccb.com",
    "abchina.com",
    "cmbchina.com",
    "bankcomm.com",
    "citic.com",
    "citicbank.com",
)

_MARKET_DATA_DOMAINS: tuple[str, ...] = (
    "finance.yahoo.com",
    "cmegroup.com",
    "tradingeconomics.com",
    "xe.com",
)

_BROKER_BLOG_DOMAINS: tuple[str, ...] = (
    "marketpulse.com",
    "investing.com",
    "fxstreet.com",
    "dailyfx.com",
    "forexlive.com",
    "tradingview.com",
    "tmgm.com",
    "ig.com",
    "oanda.com",
    "forex.com",
    "finimize.com",
)

_CRYPTO_DOMAINS: tuple[str, ...] = (
    "binance.com",
    "cryptorank.io",
    "coinmarketcap.com",
    "coindesk.com",
    "cointelegraph.com",
    "crypto.com",
)

_AGGREGATOR_DOMAINS: tuple[str, ...] = (
    "news.google.com",
    "news.yahoo.com",
    "flipboard.com",
)

# ── SourceMetadata dataclass ─────────────────────────────────────────────────


@dataclass
class SourceMetadata:
    """Structured source metadata for evidence quality assessment."""
    url: str = ""
    canonical_url: str = ""
    title: str = ""
    provider: str = ""
    domain: str = ""
    publisher: str = ""
    source_type: str = "unknown"
    source_tier: int = 3
    published_at: str | None = None
    retrieved_at: str = ""
    is_aggregator: bool = False
    aggregator_provider: str = ""
    quality_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceMetadata":
        if not isinstance(d, dict):
            return cls()

        def _str_field(key: str, default: str = "") -> str:
            value = d.get(key, default)
            return value if isinstance(value, str) else default

        source_type = d.get("source_type", "unknown")
        if source_type not in SOURCE_TYPES:
            source_type = "unknown"

        try:
            source_tier = int(d.get("source_tier", 3))
        except (TypeError, ValueError):
            source_tier = 3
        if source_tier not in (1, 2, 3, 4, 5):
            source_tier = 3

        return cls(
            url=_str_field("url"),
            canonical_url=_str_field("canonical_url"),
            title=_str_field("title"),
            provider=_str_field("provider"),
            domain=_str_field("domain"),
            publisher=_str_field("publisher"),
            source_type=source_type,
            source_tier=source_tier,
            published_at=d.get("published_at") if isinstance(d.get("published_at"), str) else None,
            retrieved_at=_str_field("retrieved_at"),
            is_aggregator=d.get("is_aggregator") is True,
            aggregator_provider=_str_field("aggregator_provider"),
            quality_reason=_str_field("quality_reason"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "SourceMetadata":
        if not raw or raw == "{}":
            return cls()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return cls()
        try:
            return cls.from_dict(data)
        except (AttributeError, TypeError, ValueError):
            return cls()


# ── Helper functions ─────────────────────────────────────────────────────────


def normalize_domain(url: str) -> str:
    """Extract and normalize domain from a URL.

    Returns lowercase domain without www. prefix and port.
    """
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path.split("/")[0]).lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def _domain_matches(domain: str, candidates: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in candidates)


def _infer_publisher_from_title(title: str) -> str:
    """Try to extract publisher from title suffix like '- Reuters'."""
    if not title:
        return ""
    m = re.search(r"\s[-–—]\s+([A-Za-z][\w\s&'.]+)$", title)
    if m:
        return m.group(1).strip()
    return ""


def source_metadata_from_source_ref(source_ref: SourceRef) -> "SourceMetadata":
    """Convert a SourceRef to SourceMetadata with type/tier inference."""
    domain = normalize_domain(source_ref.url)
    publisher = _infer_publisher_from_title(source_ref.title)
    is_aggregator = (
        source_ref.source in ("google_news_rss", "tavily", "web_search")
        or _domain_matches(domain, _AGGREGATOR_DOMAINS)
    )

    meta = SourceMetadata(
        url=source_ref.url,
        canonical_url="",
        title=source_ref.title,
        provider=source_ref.source,
        domain=domain,
        publisher=publisher,
        published_at=source_ref.published_at,
        retrieved_at=source_ref.retrieved_at,
        is_aggregator=is_aggregator,
        aggregator_provider=source_ref.source if is_aggregator else "",
    )
    return infer_source_type_and_tier(meta)


def source_metadata_from_legacy_string(source: str | None) -> "SourceMetadata":
    """Parse old-style source string: 'url=... | title=... | provider=...'"""
    if not source:
        return SourceMetadata(quality_reason="empty_source")

    url = ""
    title = ""
    provider = ""

    m = re.search(r"url=(https?://[^\s|,;]+)", source)
    if m:
        url = m.group(1).rstrip("。.,;")

    m = re.search(r"title=([^|]+)", source, re.I)
    if m:
        title = m.group(1).strip()

    m = re.search(r"provider=(\S+)", source, re.I)
    if m:
        provider = m.group(1).strip()

    if not url and not title and not provider:
        known_providers = ("google_news_rss", "tavily", "web_search", "yfinance", "fetch_rate.py")
        source_lower = source.strip().lower()
        for p in known_providers:
            if p in source_lower:
                provider = p
                break

    domain = normalize_domain(url)
    publisher = _infer_publisher_from_title(title)
    is_aggregator = (
        provider in ("google_news_rss", "tavily", "web_search")
        or _domain_matches(domain, _AGGREGATOR_DOMAINS)
    )

    meta = SourceMetadata(
        url=url,
        title=title,
        provider=provider,
        domain=domain,
        publisher=publisher,
        is_aggregator=is_aggregator,
        aggregator_provider=provider if is_aggregator else "",
    )
    return infer_source_type_and_tier(meta)


def infer_source_type_and_tier(metadata: "SourceMetadata") -> "SourceMetadata":
    """Infer source_type, source_tier, and quality_reason from domain/provider."""
    domain = metadata.domain
    provider = metadata.provider.lower() if metadata.provider else ""
    publisher_lower = metadata.publisher.lower() if metadata.publisher else ""

    source_type = "unknown"
    source_tier = 4
    reason = ""

    # 1. Domain-based classification
    if domain:
        if _domain_matches(domain, _CENTRAL_BANK_DOMAINS) or domain.endswith(".gov") or ".gov." in domain:
            source_type = "official_central_bank"
            source_tier = 1
            reason = f"official_domain:{domain}"
        elif _domain_matches(domain, _BANK_FX_DOMAINS):
            source_type = "bank_fx_board"
            source_tier = 2
            reason = f"bank_fx_domain:{domain}"
        elif _domain_matches(domain, _MARKET_DATA_DOMAINS):
            source_type = "market_data_api"
            source_tier = 2
            reason = f"market_data_domain:{domain}"
        elif _domain_matches(domain, _PREMIUM_MEDIA_DOMAINS):
            source_type = "mainstream_financial_media"
            source_tier = 2
            reason = f"premium_media_domain:{domain}"
        elif _domain_matches(domain, _MAINSTREAM_MEDIA_DOMAINS):
            source_type = "general_news"
            source_tier = 3
            reason = f"mainstream_domain:{domain}"
        elif _domain_matches(domain, _BROKER_BLOG_DOMAINS):
            source_type = "broker_research"
            source_tier = 4
            reason = f"broker_domain:{domain}"
        elif _domain_matches(domain, _CRYPTO_DOMAINS):
            source_type = "crypto_or_exchange"
            source_tier = 5
            reason = f"crypto_domain:{domain}"
        elif _domain_matches(domain, _AGGREGATOR_DOMAINS):
            source_type = "aggregator"
            source_tier = 4
            reason = f"aggregator_domain:{domain}"

    # 2. Publisher-based fallback (from title suffix like "- Reuters")
    if source_type == "unknown" and publisher_lower:
        if publisher_lower in ("reuters", "bloomberg", "financial times", "the wall street journal"):
            source_type = "mainstream_financial_media"
            source_tier = 2
            reason = f"publisher:{metadata.publisher}"
        elif publisher_lower in ("cnbc", "bbc", "cnn", "al jazeera", "the guardian", "nytimes"):
            source_type = "general_news"
            source_tier = 3
            reason = f"publisher:{metadata.publisher}"

    # 3. Provider-based fallback
    if source_type == "unknown" and provider:
        if provider in ("fetch_rate.py", "bank_fx_boards"):
            source_type = "bank_fx_board"
            source_tier = 2
            reason = f"provider:{provider}"
        elif provider in ("yfinance",):
            source_type = "market_data_api"
            source_tier = 2
            reason = f"provider:{provider}"
        elif provider in ("google_news_rss", "tavily", "web_search"):
            source_type = "aggregator"
            source_tier = 4
            reason = f"aggregator_provider:{provider}"

    # 4. If still unknown, tier 4
    if source_type == "unknown":
        source_tier = 4
        reason = "unrecognized_source"

    return replace(
        metadata,
        source_type=source_type,
        source_tier=source_tier,
        quality_reason=reason,
    )


# ── Tier → quality score mapping (used by EvidenceScorer) ────────────────────

def tier_to_quality_score(tier: int) -> float:
    """Map source tier to a [0,1] quality score for the evidence scorer."""
    return {
        1: 0.95,
        2: 0.82,
        3: 0.65,
        4: 0.45,
        5: 0.30,
    }.get(tier, 0.40)
