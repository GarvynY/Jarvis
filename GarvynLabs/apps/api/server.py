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
SLUG_RE = re.compile(r"[^a-z0-9-]+")


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
    .list { display:grid; gap:8px; margin-top:12px; }
    .item { text-align:left; padding:10px; border:1px solid var(--line); border-radius:6px; background:#fff; }
    .item strong { display:block; }
    .item span { color:var(--muted); font-size:12px; }
    .grid { display:grid; grid-template-columns:1fr 190px 160px; gap:10px; margin-bottom:10px; }
    label { display:block; color:var(--muted); font-size:12px; margin:10px 0 5px; }
    input, select, textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; background:#fff; }
    textarea { min-height:54vh; resize:vertical; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height:1.55; }
    .toolbar { display:flex; gap:10px; align-items:center; margin-bottom:12px; }
    .status { color:var(--muted); font-size:13px; }
    @media (max-width: 920px) { main { grid-template-columns:1fr; } .grid { grid-template-columns:1fr; } aside { min-height:auto; } }
  </style>
</head>
<body>
  <header><h1>Garvyn Labs Admin</h1><a href="/" target="_blank">打开网站</a></header>
  <main>
    <aside>
      <div class="toolbar"><button class="primary" id="newBtn">新建笔记</button><button id="refreshBtn">刷新</button></div>
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

    function today() { return new Date().toISOString().slice(0, 10); }
    function slugify(value) {
      return String(value).toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    }
    async function api(path, options) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
    }
    async function load() {
      manifest = await api("/api/articles");
      $("articles").innerHTML = manifest.articles.map((a) => `<button class="item" data-slug="${a.slug}"><strong>${a.title}</strong><span>${a.date || ""} · ${a.category}</span></button>`).join("");
      document.querySelectorAll(".item").forEach((item) => item.onclick = () => openArticle(item.dataset.slug));
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
      await load();
      await openArticle(saved.slug);
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
    if not MANIFEST_PATH.exists():
        return {"articles": []}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _write_manifest(data: dict) -> None:
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _find_article(slug: str) -> dict | None:
    for article in _load_manifest().get("articles", []):
        if article.get("slug") == slug:
            return article
    return None


def _safe_slug(value: str) -> str:
    slug = SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or "untitled"


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
    slug = _safe_slug(payload.get("slug") or payload.get("title") or "untitled")
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

    manifest = _load_manifest()
    articles = manifest.setdefault("articles", [])
    original_slug = payload.get("originalSlug") or slug
    replaced = False
    for index, existing in enumerate(articles):
        if existing.get("slug") in {original_slug, slug}:
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


def main() -> None:
    if not PASSWORD:
        raise SystemExit("GARVYNLABS_ADMIN_PASSWORD is required")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Garvyn Labs admin listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
