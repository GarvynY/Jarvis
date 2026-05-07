#!/usr/bin/env python3
"""Small authenticated Markdown editor for Garvyn Labs.

The public site is static. This service only powers /admin/ and /api/* behind
HTTP Basic Auth and writes Markdown files plus content/manifest.json.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SITE_ROOT = Path(os.environ.get("GARVYNLABS_SITE_ROOT", "/var/www/garvynlabs"))
CONTENT_ROOT = SITE_ROOT / "content"
MANIFEST_PATH = CONTENT_ROOT / "manifest.json"
USERNAME = os.environ.get("GARVYNLABS_ADMIN_USER", "garvyn")
PASSWORD = os.environ.get("GARVYNLABS_ADMIN_PASSWORD", "")
HOST = os.environ.get("GARVYNLABS_ADMIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("GARVYNLABS_ADMIN_PORT", "8090"))

CATEGORIES = {"ai-news", "ai-thinking", "ai-technology", "jarvis"}
CATEGORY_LABELS = {
    "jarvis": "Jarvis",
    "ai-news": "AI动态",
    "ai-thinking": "AI产品思考",
    "ai-technology": "AI产品技术",
}
SLUG_RE = re.compile(r"[^\w-]+", re.UNICODE)


ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Garvyn Labs Admin</title>
  <style>
    :root { --ink:#111827; --muted:#607085; --line:#d9dee7; --teal:#0f766e; --paper:#fbfdff; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:linear-gradient(180deg,#f7fcff,#fff); }
    header { position:sticky; top:0; z-index:3; padding:18px 28px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--line); background:rgba(255,255,255,.88); backdrop-filter:blur(18px); }
    h1 { margin:0; font-size:20px; }
    main { display:grid; grid-template-columns:320px minmax(0,1fr); gap:18px; padding:18px; }
    aside, section { border:1px solid var(--line); border-radius:8px; background:rgba(255,255,255,.82); }
    aside { padding:14px; min-height:calc(100vh - 94px); }
    section { padding:18px; }
    button, input, select, textarea { font:inherit; }
    button { border:1px solid var(--line); border-radius:6px; background:#fff; padding:9px 12px; cursor:pointer; }
    button.primary { background:var(--teal); color:white; border-color:var(--teal); }
    button.danger { color:#b42318; border-color:#f3b7b1; }
    button.danger:disabled { color:#9aa4b2; border-color:var(--line); cursor:not-allowed; }
    .list { display:grid; gap:14px; margin-top:12px; }
    .group { display:grid; gap:8px; }
    .group-title { margin:8px 0 0; padding-bottom:6px; border-bottom:1px solid var(--line); font-size:13px; color:var(--muted); font-weight:700; }
    .item { display:grid; grid-template-columns:24px minmax(0,1fr); gap:8px; align-items:start; text-align:left; padding:10px; border:1px solid var(--line); border-radius:6px; background:#fff; }
    .item input { width:auto; margin-top:3px; }
    .item strong { display:block; }
    .item span { color:var(--muted); font-size:12px; }
    .grid { display:grid; grid-template-columns:1fr 190px 160px; gap:10px; margin-bottom:10px; }
    label { display:block; color:var(--muted); font-size:12px; margin:10px 0 5px; }
    input, select, textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; background:#fff; }
    textarea { min-height:54vh; resize:vertical; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height:1.55; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }
    .status { color:var(--muted); font-size:13px; }
    @media (max-width: 920px) { main { grid-template-columns:1fr; } .grid { grid-template-columns:1fr; } aside { min-height:auto; } }
  </style>
</head>
<body>
  <header><h1>Garvyn Labs Admin</h1><a href="/" target="_blank">打开网站</a></header>
  <main>
    <aside>
      <div class="toolbar"><button class="primary" id="newBtn">新建笔记</button><button id="uploadBtn" style="color:var(--teal);border-color:var(--teal)">上传 MD</button><button class="danger" id="deleteSelectedBtn" disabled>删除选中</button><button id="refreshBtn">刷新</button></div>
      <input type="file" id="uploadInput" accept=".md" style="display:none">
      <div class="list" id="articles"></div>
    </aside>
    <section>
      <div class="grid">
        <div><label>标题</label><input id="title"></div>
        <div><label>栏目</label><select id="category"><option value="ai-news">AI动态</option><option value="ai-thinking">AI产品思考</option><option value="ai-technology">AI产品技术</option><option value="jarvis">Jarvis</option></select></div>
        <div><label>日期</label><input id="date" type="date"></div>
      </div>
      <label>Slug</label><input id="slug" placeholder="article-slug">
      <label>摘要</label><input id="summary" placeholder="列表页显示的简介">
      <label>Markdown</label><textarea id="body" spellcheck="false"></textarea>
      <div class="toolbar"><button class="primary" id="saveBtn">保存</button><span class="status" id="status"></span></div>
    </section>
  </main>
  <script>
    let manifest = { articles: [] };
    let current = null;
    const $ = (id) => document.getElementById(id);
    const CATS = ['ai-news', 'ai-thinking', 'ai-technology', 'jarvis'];
    const CAT_LABELS = { 'jarvis': 'Jarvis', 'ai-news': 'AI动态', 'ai-thinking': 'AI产品思考', 'ai-technology': 'AI产品技术' };

    function parseFrontmatter(text) {
      const m = text.match(/^---\\r?\\n([\\s\\S]*?)\\r?\\n---\\r?\\n?([\\s\\S]*)$/);
      if (!m) return { meta: {}, body: text };
      const meta = {};
      m[1].split(/\\r?\\n/).forEach(line => {
        const ci = line.indexOf(':');
        if (ci > 0) meta[line.slice(0, ci).trim()] = line.slice(ci + 1).trim().replace(/^["']|["']$/g, '');
      });
      return { meta, body: m[2] };
    }
    function extractH1(body) {
      const m = body.match(/^#\\s+(.+)$/m);
      return m ? m[1].trim() : '';
    }

    function today() { return new Date().toISOString().slice(0, 10); }
    function slugify(value) {
      return String(value)
        .normalize("NFKC")
        .toLowerCase()
        .trim()
        .replace(/[^\\p{Letter}\\p{Number}]+/gu, "-")
        .replace(/^-+|-+$/g, "");
    }
    function escapeHtml(value) {
      return String(value || "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    }
    function notifyPublicSite() {
      localStorage.setItem("garvynlabs-content-updated", String(Date.now()));
    }
    function selectedSlugs() {
      return [...document.querySelectorAll(".article-check:checked")].map((item) => item.value);
    }
    function updateDeleteSelectedState() {
      $("deleteSelectedBtn").disabled = selectedSlugs().length === 0;
    }
    function clearEditor(status) {
      current = null;
      $("title").value = "";
      $("category").value = "ai-news";
      $("date").value = today();
      $("slug").value = "";
      $("summary").value = "";
      $("body").value = "";
      $("status").textContent = status || "";
    }
    async function api(path, options) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
    }
    async function load() {
      manifest = await api("/api/articles");
      const groups = CATS.map((category) => {
        const articles = (manifest.articles || [])
          .filter((a) => a.category === category)
          .sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")) || String(a.title || "").localeCompare(String(b.title || "")));
        if (!articles.length) return "";
        return `
          <div class="group">
            <div class="group-title">${CAT_LABELS[category]} · ${articles.length}</div>
            ${articles.map((a) => `
              <div class="item" data-slug="${escapeHtml(a.slug)}">
                <input class="article-check" type="checkbox" value="${escapeHtml(a.slug)}" aria-label="选择 ${escapeHtml(a.title)}">
                <button type="button" data-open="${escapeHtml(a.slug)}" style="padding:0;border:0;background:transparent;text-align:left">
                  <strong>${escapeHtml(a.title)}</strong><span>${escapeHtml(a.date || "")} · ${escapeHtml(a.slug)}</span>
                </button>
              </div>`).join("")}
          </div>`;
      }).join("");
      $("articles").innerHTML = groups || `<div class="empty">还没有 Markdown 文档。</div>`;
      document.querySelectorAll("[data-open]").forEach((item) => item.onclick = () => openArticle(item.dataset.open));
      document.querySelectorAll(".article-check").forEach((item) => item.onchange = updateDeleteSelectedState);
      updateDeleteSelectedState();
    }
    async function openArticle(slug) {
      current = manifest.articles.find((a) => a.slug === slug);
      const data = await api(`/api/article?slug=${encodeURIComponent(slug)}`);
      $("title").value = current.title || "";
      $("category").value = current.category || "ai-news";
      $("date").value = current.date || today();
      $("slug").value = current.slug || "";
      $("summary").value = current.summary || "";
      $("body").value = data.body || "";
      $("status").textContent = "已加载";
    }
    $("newBtn").onclick = () => {
      current = null;
      $("title").value = "";
      $("category").value = "ai-news";
      $("date").value = today();
      $("slug").value = "";
      $("summary").value = "";
      $("body").value = "# 新笔记\\n\\n";
      $("status").textContent = "新建中";
    };
    $("refreshBtn").onclick = load;
    $("title").addEventListener("input", () => { if (!current && !$("slug").value) $("slug").value = slugify($("title").value); });
    $("saveBtn").onclick = async () => {
      const payload = {
        originalSlug: current?.slug || "",
        title: $("title").value,
        category: $("category").value,
        date: $("date").value,
        slug: $("slug").value || slugify($("title").value),
        summary: $("summary").value,
        body: $("body").value
      };
      $("status").textContent = "保存中...";
      const saved = await api("/api/article", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
      $("status").textContent = `已保存：${saved.slug}`;
      notifyPublicSite();
      await load();
      await openArticle(saved.slug);
    };
    $("deleteSelectedBtn").onclick = async () => {
      const slugs = selectedSlugs();
      if (!slugs.length) return;
      if (!confirm(`确定删除选中的 ${slugs.length} 篇文章？此操作会删除 Markdown 文件并从列表移除。`)) return;
      $("status").textContent = "删除中...";
      for (const slug of slugs) {
        await api(`/api/article?slug=${encodeURIComponent(slug)}`, { method: "DELETE" });
      }
      notifyPublicSite();
      await load();
      if (current && slugs.includes(current.slug)) clearEditor(`已删除 ${slugs.length} 篇文章`);
      else $("status").textContent = `已删除 ${slugs.length} 篇文章`;
    };
    $("uploadBtn").onclick = () => $("uploadInput").click();
    $("uploadInput").onchange = (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        const text = ev.target.result;
        const { meta, body } = parseFrontmatter(text);
        const title = meta.title || extractH1(body) || file.name.replace(/\\.md$/, '');
        current = null;
        $("title").value = title;
        $("category").value = CATS.includes(meta.category) ? meta.category : 'ai-news';
        $("date").value = meta.date || today();
        $("slug").value = meta.slug ? slugify(meta.slug) : slugify(title);
        $("summary").value = meta.summary || '';
        $("body").value = body.trim();
        $("status").textContent = `已从文件加载：${file.name}`;
      };
      reader.readAsText(file, 'utf-8');
      e.target.value = '';
    };
    load().catch((error) => $("status").textContent = error.message);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        if not self._authenticated():
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Garvyn Labs Admin"')
            self.end_headers()
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/admin", "/admin/", "/api/articles"}:
            self.send_response(HTTPStatus.OK)
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authenticated():
            return self._auth_required()
        parsed = urlparse(self.path)
        if parsed.path in {"/admin", "/admin/"}:
            return self._send_html(ADMIN_HTML)
        if parsed.path == "/api/articles":
            return self._send_json(_load_manifest())
        if parsed.path == "/api/article":
            slug = parse_qs(parsed.query).get("slug", [""])[0]
            article = _find_article(slug)
            if not article:
                return self._send_error(HTTPStatus.NOT_FOUND, "article not found")
            body = _article_path(article).read_text(encoding="utf-8")
            return self._send_json({"body": _strip_frontmatter(body)})
        return self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if not self._authenticated():
            return self._auth_required()
        if urlparse(self.path).path != "/api/article":
            return self._send_error(HTTPStatus.NOT_FOUND, "not found")
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        saved = _save_article(payload)
        return self._send_json(saved)

    def do_DELETE(self) -> None:
        if not self._authenticated():
            return self._auth_required()
        parsed = urlparse(self.path)
        if parsed.path != "/api/article":
            return self._send_error(HTTPStatus.NOT_FOUND, "not found")
        slug = parse_qs(parsed.query).get("slug", [""])[0]
        deleted = _delete_article(slug)
        if not deleted:
            return self._send_error(HTTPStatus.NOT_FOUND, "article not found")
        return self._send_json(deleted)

    def _authenticated(self) -> bool:
        if not PASSWORD:
            return False
        header = self.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        user, _, password = raw.partition(":")
        return user == USERNAME and password == PASSWORD

    def _auth_required(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Garvyn Labs Admin"')
        self.end_headers()

    def _send_json(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _load_manifest() -> dict:
    data = {"articles": []}
    if MANIFEST_PATH.exists():
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return _merge_disk_articles(data)


def _write_manifest(data: dict) -> None:
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    data["updatedAt"] = int(time.time() * 1000)
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _find_article(slug: str) -> dict | None:
    for article in _load_manifest().get("articles", []):
        if article.get("slug") == slug:
            return article
    return None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$", text)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, match.group(2)


def _title_from_body(body: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _article_from_path(path: Path) -> dict | None:
    try:
        rel = path.resolve().relative_to(CONTENT_ROOT.resolve())
    except ValueError:
        return None
    if len(rel.parts) < 2:
        return None
    category = rel.parts[0]
    if category not in CATEGORIES or path.suffix.lower() != ".md":
        return None
    slug = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _parse_frontmatter(text)
    return {
        "slug": _safe_slug(meta.get("slug") or slug),
        "category": meta.get("category") if meta.get("category") in CATEGORIES else category,
        "title": meta.get("title") or _title_from_body(body, slug),
        "summary": meta.get("summary", ""),
        "date": meta.get("date", ""),
        "file": f"/content/{category}/{path.name}",
    }


def _merge_disk_articles(data: dict) -> dict:
    articles_by_slug = {
        article.get("slug"): article
        for article in data.get("articles", [])
        if article.get("slug")
    }
    if CONTENT_ROOT.exists():
        for category in CATEGORIES:
            category_dir = CONTENT_ROOT / category
            if not category_dir.exists():
                continue
            for path in sorted(category_dir.glob("*.md")):
                article = _article_from_path(path)
                if article:
                    articles_by_slug[article["slug"]] = {**articles_by_slug.get(article["slug"], {}), **article}
    merged = dict(data)
    merged["articles"] = sorted(
        articles_by_slug.values(),
        key=lambda article: (
            article.get("category", ""),
            str(article.get("date", "")),
            str(article.get("title", "")).lower(),
        ),
    )
    return merged


def _safe_slug(value: str) -> str:
    slug = SLUG_RE.sub("-", str(value or "").lower()).strip("-_")
    return slug or "untitled"


def _unique_slug(slug: str, articles: list[dict], original_slug: str = "") -> str:
    existing = {
        article.get("slug")
        for article in articles
        if article.get("slug") and article.get("slug") != original_slug
    }
    if slug not in existing:
        return slug
    index = 2
    while f"{slug}-{index}" in existing:
        index += 1
    return f"{slug}-{index}"


def _article_path(article: dict) -> Path:
    file_path = str(article.get("file", "")).lstrip("/")
    path = SITE_ROOT / file_path
    if CONTENT_ROOT not in path.resolve().parents:
        raise ValueError("invalid article path")
    return path


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", text, count=1)


def _save_article(payload: dict) -> dict:
    category = payload.get("category", "ai-news")
    if category not in CATEGORIES:
        raise ValueError("invalid category")
    manifest = _load_manifest()
    articles = manifest.setdefault("articles", [])
    original_slug = payload.get("originalSlug") or ""
    slug = _safe_slug(payload.get("slug") or payload.get("title") or "untitled")
    slug = _unique_slug(slug, articles, original_slug=original_slug)
    body = payload.get("body", "")
    article = {
        "slug": slug,
        "category": category,
        "title": payload.get("title", slug),
        "summary": payload.get("summary", ""),
        "date": payload.get("date", ""),
        "file": f"/content/{category}/{slug}.md",
    }
    path = _article_path(article)
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"title: {article['title']}\n"
        f"date: {article['date']}\n"
        f"category: {category}\n"
        "---\n\n"
    )
    path.write_text(frontmatter + body.lstrip(), encoding="utf-8")

    replaced = False
    for index, existing in enumerate(articles):
        if original_slug and existing.get("slug") == original_slug:
            old_path = _article_path(existing)
            articles[index] = article
            replaced = True
            if old_path != path and old_path.exists():
                old_path.unlink()
            break
    if not replaced:
        articles.append(article)
    _write_manifest(manifest)
    return article


def _delete_article(slug: str) -> dict | None:
    manifest = _load_manifest()
    articles = manifest.setdefault("articles", [])
    for index, article in enumerate(articles):
        if article.get("slug") != slug:
            continue
        path = _article_path(article)
        if path.exists():
            path.unlink()
        deleted = articles.pop(index)
        _write_manifest(manifest)
        return {"deleted": True, "slug": deleted.get("slug", slug)}
    return None


def main() -> None:
    if not PASSWORD:
        raise SystemExit("GARVYNLABS_ADMIN_PASSWORD is required")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Garvyn Labs admin listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
