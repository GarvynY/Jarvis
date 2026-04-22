#!/usr/bin/env python3
"""
Free keyword-based news monitor using Google News RSS.

Zero API credits consumed — uses standard HTTP + XML parsing only.

Tracks seen article URLs in a state file so only NEW articles are returned.
State file: ~/.pythonclaw/context/news_monitor_state.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STATE_FILE = os.path.expanduser(
    os.path.join("~", ".pythonclaw", "context", "news_monitor_state.json")
)

# Default keyword groups — can be overridden via --keywords
DEFAULT_KEYWORD_GROUPS = {
    "mideast_war": [
        "US Iran ceasefire",
        "Iran Hormuz strait",
        "Iran nuclear deal",
        "Middle East oil disruption",
    ],
    "aud_drivers": [
        "RBA interest rate decision",
        "Australia dollar AUD",
        "China Australia trade",
    ],
}


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"seen_urls": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # Keep only the 500 most recent seen URLs to prevent unlimited growth
    state["seen_urls"] = state.get("seen_urls", [])[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── RSS fetcher ───────────────────────────────────────────────────────────────

def _fetch_google_news_rss(query: str, lang: str = "en", max_items: int = 5) -> list[dict]:
    """
    Fetch Google News RSS for a keyword query.
    Returns list of {title, url, published, snippet} dicts.
    """
    params = urllib.parse.urlencode({
        "q": query,
        "hl": "en-US" if lang == "en" else "zh-CN",
        "gl": "US" if lang == "en" else "CN",
        "ceid": "US:en" if lang == "en" else "CN:zh-Hans",
    })
    url = f"https://news.google.com/rss/search?{params}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except Exception as exc:
        return [{"error": f"RSS fetch failed for '{query}': {exc}"}]

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return [{"error": f"RSS parse failed for '{query}': {exc}"}]

    items = []
    ns = {"media": "http://search.yahoo.com/mrss/"}
    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        # Strip HTML tags from description
        import re
        desc = re.sub(r"<[^>]+>", "", desc).strip()

        items.append({
            "title": title,
            "url": link,
            "published": pub,
            "snippet": desc[:200] if desc else "",
        })

    return items


# ── Main check logic ──────────────────────────────────────────────────────────

def check_news(
    keywords: list[str] | None = None,
    max_per_keyword: int = 3,
    mark_seen: bool = True,
) -> dict:
    """
    Fetch news for each keyword, return only articles not yet seen.

    Returns:
      {
        "new_articles": [...],
        "checked_keywords": [...],
        "fetched_at": "...",
        "total_new": int,
        "has_breaking": bool,
      }
    """
    if not keywords:
        # Flatten default groups
        keywords = [kw for group in DEFAULT_KEYWORD_GROUPS.values() for kw in group]

    state = _load_state()
    seen = set(state.get("seen_urls", []))
    now_utc = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    new_articles: list[dict] = []

    for kw in keywords:
        articles = _fetch_google_news_rss(kw, max_items=max_per_keyword)
        for art in articles:
            if "error" in art:
                continue
            url = art.get("url", "")
            if url and url not in seen:
                art["keyword"] = kw
                new_articles.append(art)
                if mark_seen:
                    seen.add(url)

    if mark_seen:
        state["seen_urls"] = list(seen)
        state["last_check"] = now_utc
        _save_state(state)

    return {
        "new_articles": new_articles,
        "checked_keywords": keywords,
        "fetched_at_utc": now_utc,
        "total_new": len(new_articles),
        "has_breaking": len(new_articles) > 0,
        "data_source": "Google News RSS (free, no API key)",
    }


def _format_text(result: dict) -> str:
    lines = [
        "═══════════════════════════════════",
        "  中东/外汇突发新闻监控 (RSS)",
        "═══════════════════════════════════",
        f"查询时间: {result['fetched_at_utc']} (UTC)",
        f"来源: {result['data_source']}",
        f"监控关键词: {len(result['checked_keywords'])} 个",
        "",
    ]

    if not result["has_breaking"]:
        lines.append("✅ 无新突发消息（所有关键词均无新文章）")
        return "\n".join(lines)

    lines.append(f"🚨 发现 {result['total_new']} 条新文章:")
    lines.append("")
    for art in result["new_articles"]:
        lines.append(f"[{art.get('keyword', '')}]")
        lines.append(f"  标题: {art['title']}")
        lines.append(f"  时间: {art.get('published', 'N/A')}")
        if art.get("snippet"):
            lines.append(f"  摘要: {art['snippet']}")
        lines.append(f"  链接: {art['url']}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor news via Google News RSS (free, no API key)."
    )
    parser.add_argument(
        "--keywords", nargs="*",
        help="Custom keywords to search (space-separated). Uses defaults if omitted.",
    )
    parser.add_argument(
        "--max-per-keyword", type=int, default=3,
        help="Max articles to fetch per keyword (default: 3)",
    )
    parser.add_argument(
        "--no-mark-seen", action="store_true",
        help="Don't update the seen-URL state (dry run)",
    )
    parser.add_argument(
        "--format", choices=["json", "text"], default="text",
    )
    args = parser.parse_args()

    result = check_news(
        keywords=args.keywords or None,
        max_per_keyword=args.max_per_keyword,
        mark_seen=not args.no_mark_seen,
    )

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_format_text(result))

    # Exit code 1 if breaking news found (useful for shell scripting)
    if result["has_breaking"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
