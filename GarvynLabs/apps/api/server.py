#!/usr/bin/env python3
"""Small authenticated Markdown editor for Garvyn Labs.

The public site is static. This service only powers /admin/ and /api/* behind
HTTP Basic Auth and writes Markdown files plus content/manifest.json.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SITE_ROOT = Path(os.environ.get("GARVYNLABS_SITE_ROOT", "/var/www/garvynlabs"))
CONTENT_ROOT = SITE_ROOT / "content"
ASSET_ROOT = CONTENT_ROOT / "assets"
MANIFEST_PATH = CONTENT_ROOT / "manifest.json"
ANALYTICS_PATH = SITE_ROOT / "analytics-events.jsonl"
USERNAME = os.environ.get("GARVYNLABS_ADMIN_USER", "garvyn")
PASSWORD = os.environ.get("GARVYNLABS_ADMIN_PASSWORD", "")
HOST = os.environ.get("GARVYNLABS_ADMIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("GARVYNLABS_ADMIN_PORT", "8090"))

CATEGORIES = {"ai-news", "ai-thinking", "ai-technology", "jarvis"}
JARVIS_SUBCATEGORIES = {
    "fix-updates": "修复更新",
    "product-iteration": "产品迭代",
    "product-analysis": "产品分析",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ASSET_TTL_SECONDS = 24 * 60 * 60
ANALYTICS_MAX_EVENTS = 20000
ANALYTICS_LOCK = threading.Lock()
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
    header { position:sticky; top:0; z-index:3; padding:16px 28px; display:flex; justify-content:space-between; align-items:center; gap:18px; border-bottom:1px solid var(--line); background:rgba(255,255,255,.9); backdrop-filter:blur(18px); }
    h1 { margin:0; font-size:20px; }
    .admin-nav { display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:flex-end; }
    .admin-nav button.active { background:var(--teal); color:#fff; border-color:var(--teal); }
    .admin-nav a { color:var(--teal); font-size:14px; text-decoration:none; padding:9px 0; }
    main { padding:18px; }
    .view.hidden { display:none; }
    .content-view { display:grid; grid-template-columns:320px minmax(0,1fr); gap:18px; }
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
    .group-title { margin:8px 0 0; padding-bottom:6px; border-bottom:1px solid var(--line); font-size:13px; color:var(--muted); font-weight:700; cursor:pointer; display:flex; align-items:center; gap:6px; user-select:none; }
    .group-title::before { content:"▼"; font-size:10px; transition:transform .2s; }
    .group.collapsed .group-title::before { transform:rotate(-90deg); }
    .group.collapsed .item { display:none; }
    .item { display:grid; grid-template-columns:24px minmax(0,1fr); gap:8px; align-items:start; text-align:left; padding:10px; border:1px solid var(--line); border-radius:6px; background:#fff; }
    .item input { width:auto; margin-top:3px; }
    .item strong { display:block; }
    .item span { color:var(--muted); font-size:12px; }
    .pill { display:inline-flex; margin-left:6px; padding:1px 5px; border:1px solid var(--line); border-radius:999px; color:var(--teal); font-size:11px; }
    .grid { display:grid; grid-template-columns:1fr 190px 160px; gap:10px; margin-bottom:10px; }
    .subgrid { display:grid; grid-template-columns:190px minmax(0,1fr); gap:10px; margin-bottom:10px; }
    .hidden { display:none; }
    label { display:block; color:var(--muted); font-size:12px; margin:10px 0 5px; }
    input, select, textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; background:#fff; }
    textarea { min-height:54vh; resize:vertical; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height:1.55; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }
    .status { color:var(--muted); font-size:13px; }
    .analytics { min-height:calc(100vh - 118px); }
    .analytics-head { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; }
    .analytics-head h2 { margin:0; font-size:24px; }
    .metric-grid { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }
    .metric { padding:14px; border:1px solid var(--line); border-radius:8px; background:#fff; }
    .metric span { display:block; color:var(--muted); font-size:12px; }
    .metric strong { display:block; margin-top:8px; font-size:26px; line-height:1; }
    .analytics-grid { display:grid; grid-template-columns:1.2fr .8fr; gap:14px; margin-top:14px; }
    .analytics-card { padding:14px; border:1px solid var(--line); border-radius:8px; background:#fff; }
    .analytics-card h3 { margin:0 0 10px; font-size:14px; }
    .analytics-table { width:100%; border-collapse:collapse; font-size:13px; }
    .analytics-table th, .analytics-table td { padding:8px 6px; border-bottom:1px solid #edf0f5; text-align:left; vertical-align:top; }
    .analytics-table th { color:var(--muted); font-weight:700; }
    .analytics-path { max-width:460px; overflow-wrap:anywhere; }
    @media (max-width: 920px) { header { align-items:flex-start; flex-direction:column; } .admin-nav { justify-content:flex-start; } .content-view { grid-template-columns:1fr; } .grid { grid-template-columns:1fr; } aside { min-height:auto; } }
    @media (max-width: 920px) { .metric-grid, .analytics-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Garvyn Labs Admin</h1>
    <div class="admin-nav">
      <button id="contentTab" class="active" type="button">内容管理</button>
      <button id="analyticsTab" type="button">流量分析</button>
      <a href="/" target="_blank">打开网站</a>
    </div>
  </header>
  <main>
    <div class="view content-view" id="contentView">
      <aside>
        <div class="toolbar"><button class="primary" id="newBtn">新建笔记</button><button id="uploadBtn" style="color:var(--teal);border-color:var(--teal)">上传 MD/PDF</button><button class="danger" id="deleteSelectedBtn" disabled>删除选中</button><button id="refreshBtn">刷新</button></div>
        <input type="file" id="uploadInput" accept=".md,.pdf,application/pdf,text/markdown,text/plain" style="display:none">
        <div class="list" id="articles"></div>
      </aside>
      <section>
        <div class="grid">
          <div><label>标题</label><input id="title"></div>
          <div><label>栏目</label><select id="category"><option value="ai-news">AI动态</option><option value="ai-thinking">AI产品思考</option><option value="ai-technology">AI产品技术</option><option value="jarvis">Jarvis</option></select></div>
          <div><label>日期</label><input id="date" type="date"></div>
        </div>
        <label>Slug</label><input id="slug" placeholder="article-slug">
        <div class="subgrid hidden" id="subcategoryRow">
          <div><label>Jarvis 二级栏目</label><select id="subcategory"><option value="fix-updates">修复更新</option><option value="product-iteration">产品迭代</option><option value="product-analysis">产品分析</option></select></div>
        </div>
        <label>摘要</label><input id="summary" placeholder="列表页显示的简介">
        <label id="bodyLabel">Markdown</label><textarea id="body" spellcheck="false"></textarea>
        <div class="toolbar"><button class="primary" id="saveBtn">保存</button><button id="imageBtn" type="button">插入图片</button><input type="file" id="imageInput" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none"><span class="status" id="status"></span></div>
      </section>
    </div>
    <section class="view analytics hidden" id="analyticsView">
      <div class="analytics-head"><h2>网站流量分析</h2><button id="analyticsRefreshBtn">刷新统计</button></div>
      <div class="metric-grid" id="analyticsMetrics"></div>
      <div class="analytics-grid">
        <div class="analytics-card"><h3>近 14 天访问趋势</h3><div id="analyticsDays"></div></div>
        <div class="analytics-card"><h3>热门页面（近 30 天）</h3><div id="analyticsTopPaths"></div></div>
      </div>
    </section>
  </main>
  <script src="https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js"></script>
  <script>
    let manifest = { articles: [] };
    let current = null;
    let pendingPdf = null;
    let imageInsertState = null;
    const $ = (id) => document.getElementById(id);
    const CATS = ['ai-news', 'ai-thinking', 'ai-technology', 'jarvis'];
    const CAT_LABELS = { 'jarvis': 'Jarvis', 'ai-news': 'AI动态', 'ai-thinking': 'AI产品思考', 'ai-technology': 'AI产品技术' };
    const JARVIS_SUBCATS = ['fix-updates', 'product-iteration', 'product-analysis'];
    const JARVIS_SUBCAT_LABELS = { 'fix-updates': '修复更新', 'product-iteration': '产品迭代', 'product-analysis': '产品分析' };

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
    function normalizeJarvisSubcategory(value) {
      return JARVIS_SUBCATS.includes(value) ? value : "product-iteration";
    }
    function syncSubcategoryVisibility() {
      const isJarvis = $("category").value === "jarvis";
      $("subcategoryRow").classList.toggle("hidden", !isJarvis);
      if (isJarvis) $("subcategory").value = normalizeJarvisSubcategory($("subcategory").value);
    }
    function subcategoryValue() {
      return $("category").value === "jarvis" ? normalizeJarvisSubcategory($("subcategory").value) : "";
    }
    function clearEditor(status) {
      current = null;
      pendingPdf = null;
      $("title").value = "";
      $("category").value = "ai-news";
      $("date").value = today();
      $("slug").value = "";
      $("summary").value = "";
      $("subcategory").value = "product-iteration";
      syncSubcategoryVisibility();
      $("body").value = "";
      $("body").disabled = false;
      $("bodyLabel").textContent = "Markdown";
      $("status").textContent = status || "";
    }
    function setEditorMode(kind, message) {
      const isPdf = kind === "pdf";
      $("body").disabled = isPdf;
      $("bodyLabel").textContent = isPdf ? "PDF" : "Markdown";
      if (message !== undefined) $("body").value = message;
    }
    function readAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
    }
    function fileAltText(fileName) {
      return String(fileName || "image").replace(/\\.[^.]+$/, "").replace(/[-_]+/g, " ").trim() || "image";
    }
    function captureEditorPosition() {
      const editor = $("body");
      return {
        start: editor.selectionStart ?? editor.value.length,
        end: editor.selectionEnd ?? editor.value.length,
        editorScrollTop: editor.scrollTop,
        windowScrollX: window.scrollX,
        windowScrollY: window.scrollY
      };
    }
    function restoreEditorScroll(state) {
      if (!state) return;
      const editor = $("body");
      editor.scrollTop = state.editorScrollTop;
      window.scrollTo(state.windowScrollX, state.windowScrollY);
      requestAnimationFrame(() => {
        editor.scrollTop = state.editorScrollTop;
        window.scrollTo(state.windowScrollX, state.windowScrollY);
      });
    }
    function insertAtCursor(text, positionState) {
      const editor = $("body");
      const state = positionState || captureEditorPosition();
      const start = state.start ?? editor.value.length;
      const end = state.end ?? editor.value.length;
      const before = editor.value.slice(0, start);
      const after = editor.value.slice(end);
      const prefix = before && !before.endsWith("\\n") ? "\\n\\n" : "";
      const suffix = after && !after.startsWith("\\n") ? "\\n\\n" : "";
      editor.value = before + prefix + text + suffix + after;
      const next = (before + prefix + text).length;
      editor.focus();
      editor.setSelectionRange(next, next);
      restoreEditorScroll(state);
    }
    async function extractPdfText(file) {
      const pdfjs = window.pdfjsLib;
      if (!pdfjs?.getDocument) throw new Error("PDF 解析库加载失败，请刷新页面后重试。");
      pdfjs.GlobalWorkerOptions.workerSrc = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
      const data = new Uint8Array(await file.arrayBuffer());
      const pdf = await pdfjs.getDocument({ data }).promise;
      const pages = [];
      for (let pageNo = 1; pageNo <= pdf.numPages; pageNo += 1) {
        const page = await pdf.getPage(pageNo);
        const content = await page.getTextContent();
        const text = content.items.map((item) => item.str || "").join(" ").replace(/\\s+/g, " ").trim();
        if (text) pages.push(text);
      }
      return pages.join("\\n\\n");
    }
    async function api(path, options) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
    }
    function formatError(error) {
      const message = error?.message || String(error || "未知错误");
      if (message.includes("Request Entity Too Large") || message.includes("413")) return "保存失败：文件太大，服务器拒绝上传。";
      return `保存失败：${message}`;
    }
    function renderTable(rows, columns, emptyText) {
      if (!rows.length) return `<div class="status">${emptyText}</div>`;
      return `<table class="analytics-table">
        <thead><tr>${columns.map((column) => `<th>${column.label}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td class="${column.className || ""}">${column.render(row)}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>`;
    }
    function metric(label, value) {
      return `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`;
    }
    function showView(name) {
      const isAnalytics = name === "analytics";
      $("contentView").classList.toggle("hidden", isAnalytics);
      $("analyticsView").classList.toggle("hidden", !isAnalytics);
      $("contentTab").classList.toggle("active", !isAnalytics);
      $("analyticsTab").classList.toggle("active", isAnalytics);
      location.hash = isAnalytics ? "analytics" : "content";
      if (isAnalytics) loadAnalytics();
    }
    async function loadAnalytics() {
      try {
        const data = await api("/api/analytics");
        const summary = data.summary || {};
        $("analyticsMetrics").innerHTML = [
          metric("总浏览量", summary.totalViews || 0),
          metric("今日浏览", summary.todayViews || 0),
          metric("总访客", summary.totalVisitors || 0),
          metric("近 7 天访客", summary.visitors7d || 0),
          metric("近 30 天浏览", summary.views30d || 0)
        ].join("");
        $("analyticsDays").innerHTML = renderTable(data.days || [], [
          { label: "日期", render: (row) => escapeHtml(row.date) },
          { label: "浏览", render: (row) => row.views },
          { label: "访客", render: (row) => row.visitors }
        ], "暂无访问数据。");
        $("analyticsTopPaths").innerHTML = renderTable(data.topPaths || [], [
          { label: "页面", className: "analytics-path", render: (row) => escapeHtml(row.path) },
          { label: "浏览", render: (row) => row.views },
          { label: "访客", render: (row) => row.visitors }
        ], "暂无热门页面。");
      } catch (error) {
        $("analyticsMetrics").innerHTML = `<div class="status">${escapeHtml(error.message || "统计加载失败")}</div>`;
      }
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
                  <strong>${escapeHtml(a.title)}${a.kind === "pdf" ? '<span class="pill">PDF</span>' : ''}</strong><span>${escapeHtml(a.date || "")} · ${escapeHtml(a.slug)}</span>
                  ${a.category === "jarvis" ? `<span>${escapeHtml(JARVIS_SUBCAT_LABELS[normalizeJarvisSubcategory(a.subcategory)] || "")}</span>` : ""}
                </button>
              </div>`).join("")}
          </div>`;
      }).join("");
      $("articles").innerHTML = groups || `<div class="empty">还没有 Markdown 文档。</div>`;
      document.querySelectorAll("[data-open]").forEach((item) => item.onclick = () => openArticle(item.dataset.open));
      document.querySelectorAll(".article-check").forEach((item) => item.onchange = updateDeleteSelectedState);
      document.querySelectorAll(".group-title").forEach((title) => title.onclick = () => title.parentElement.classList.toggle("collapsed"));
      updateDeleteSelectedState();
    }
    async function openArticle(slug) {
      current = manifest.articles.find((a) => a.slug === slug);
      $("title").value = current.title || "";
      $("category").value = current.category || "ai-news";
      $("subcategory").value = normalizeJarvisSubcategory(current.subcategory);
      syncSubcategoryVisibility();
      $("date").value = current.date || today();
      $("slug").value = current.slug || "";
      $("summary").value = current.summary || "";
      pendingPdf = null;
      if (current.kind === "pdf") {
        setEditorMode("pdf", `PDF 文件不可在线修改。\n\n文件路径：${current.file || ""}`);
      } else {
        const data = await api(`/api/article?slug=${encodeURIComponent(slug)}`);
        setEditorMode("md", data.body || "");
      }
      $("status").textContent = "已加载";
    }
    $("newBtn").onclick = () => {
      current = null;
      pendingPdf = null;
      $("title").value = "";
      $("category").value = "ai-news";
      $("date").value = today();
      $("slug").value = "";
      $("summary").value = "";
      $("subcategory").value = "product-iteration";
      syncSubcategoryVisibility();
      setEditorMode("md", "# 新笔记\\n\\n");
      $("status").textContent = "新建中";
    };
    $("refreshBtn").onclick = load;
    $("analyticsRefreshBtn").onclick = loadAnalytics;
    $("contentTab").onclick = () => showView("content");
    $("analyticsTab").onclick = () => showView("analytics");
    $("category").addEventListener("change", syncSubcategoryVisibility);
    $("title").addEventListener("input", () => { if (!current && !$("slug").value) $("slug").value = slugify($("title").value); });
    $("saveBtn").onclick = async () => {
      $("status").textContent = "保存中...";
      try {
        let saved;
        if (pendingPdf || current?.kind === "pdf") {
          if (pendingPdf && !pendingPdf.data) throw new Error("PDF 还在读取中，请稍后再保存。");
          const payload = {
            originalSlug: current?.slug || "",
            title: $("title").value,
            category: $("category").value,
            subcategory: subcategoryValue(),
            date: $("date").value,
            slug: $("slug").value || slugify($("title").value),
            summary: $("summary").value,
            fileName: pendingPdf?.name || current?.file?.split("/").pop() || "",
            fileData: pendingPdf?.data || "",
            existingFile: current?.file || "",
            body: $("body").value
          };
          saved = await api("/api/pdf", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
        } else {
          const payload = {
            originalSlug: current?.slug || "",
            title: $("title").value,
            category: $("category").value,
            subcategory: subcategoryValue(),
            date: $("date").value,
            slug: $("slug").value || slugify($("title").value),
            summary: $("summary").value,
            body: $("body").value
          };
          saved = await api("/api/article", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
        }
        $("status").textContent = `已保存：${saved.slug}`;
        pendingPdf = null;
        notifyPublicSite();
        await load();
        await openArticle(saved.slug);
      } catch (error) {
        $("status").textContent = formatError(error);
      }
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
      if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
        const title = file.name.replace(/\\.pdf$/i, "");
        pendingPdf = { name: file.name, data: "" };
        current = null;
        $("title").value = title;
        $("category").value = "ai-news";
        $("subcategory").value = "product-iteration";
        syncSubcategoryVisibility();
        $("date").value = today();
        $("slug").value = slugify(title);
        $("summary").value = "";
        setEditorMode("md", "正在从 PDF 提取网页正文...");
        $("status").textContent = "正在读取 PDF...";
        Promise.all([readAsDataUrl(file), extractPdfText(file)]).then(([dataUrl, text]) => {
          pendingPdf.data = dataUrl.split(",", 2)[1] || "";
          const body = text ? `# ${title}\n\n${text}` : `# ${title}\n\nPDF 文本提取为空，请在这里补充网页正文。`;
          $("bodyLabel").textContent = "PDF 生成的网页正文";
          $("body").disabled = false;
          $("body").value = body;
          $("status").textContent = `已准备上传 PDF：${file.name}`;
        }).catch((error) => {
          pendingPdf = null;
          $("status").textContent = error.message || "PDF 读取失败";
        });
        e.target.value = '';
        return;
      }
        const reader = new FileReader();
      reader.onload = (ev) => {
        const text = ev.target.result;
        const { meta, body } = parseFrontmatter(text);
        const title = meta.title || extractH1(body) || file.name.replace(/\\.md$/, '');
        current = null;
        pendingPdf = null;
        $("title").value = title;
        $("category").value = CATS.includes(meta.category) ? meta.category : 'ai-news';
        $("subcategory").value = normalizeJarvisSubcategory(meta.subcategory);
        syncSubcategoryVisibility();
        $("date").value = meta.date || today();
        $("slug").value = meta.slug ? slugify(meta.slug) : slugify(title);
        $("summary").value = meta.summary || '';
        setEditorMode("md", body.trim());
        $("status").textContent = `已从文件加载：${file.name}`;
      };
      reader.readAsText(file, 'utf-8');
      e.target.value = '';
    };
    $("imageBtn").onclick = () => {
      if ($("body").disabled) {
        $("status").textContent = "PDF 条目不能插入图片";
        return;
      }
      imageInsertState = captureEditorPosition();
      $("imageInput").click();
    };
    $("imageInput").onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      $("status").textContent = "图片上传中...";
      try {
        const dataUrl = await readAsDataUrl(file);
        const saved = await api("/api/asset", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            fileName: file.name,
            fileData: dataUrl.split(",", 2)[1] || "",
            alt: fileAltText(file.name)
          })
        });
        insertAtCursor(saved.markdown || `![${fileAltText(file.name)}](${saved.url})`, imageInsertState);
        $("status").textContent = "图片已插入，请保存文章";
      } catch (error) {
        $("status").textContent = formatError(error);
      } finally {
        imageInsertState = null;
        e.target.value = "";
      }
    };
    syncSubcategoryVisibility();
    load().catch((error) => $("status").textContent = error.message);
    showView(location.hash === "#analytics" ? "analytics" : "content");
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/track":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if not self._authenticated():
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Garvyn Labs Admin"')
            self.end_headers()
            return
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
        if parsed.path == "/api/analytics":
            return self._send_json(_analytics_summary())
        if parsed.path == "/api/article":
            slug = parse_qs(parsed.query).get("slug", [""])[0]
            article = _find_article(slug)
            if not article:
                return self._send_error(HTTPStatus.NOT_FOUND, "article not found")
            if article.get("kind") == "pdf" or str(article.get("file", "")).lower().endswith(".pdf"):
                return self._send_json({"body": "", "kind": "pdf", "file": article.get("file", "")})
            body = _article_path(article).read_text(encoding="utf-8")
            return self._send_json({"body": _strip_frontmatter(body)})
        return self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/track":
            return self._handle_track()
        if not self._authenticated():
            return self._auth_required()
        if path not in {"/api/article", "/api/pdf", "/api/asset"}:
            return self._send_error(HTTPStatus.NOT_FOUND, "not found")
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if path == "/api/asset":
            saved = _save_asset(payload)
        else:
            saved = _save_pdf(payload) if path == "/api/pdf" else _save_article(payload)
        return self._send_json(saved)

    def _handle_track(self) -> None:
        length = min(int(self.headers.get("content-length", "0")), 8192)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except json.JSONDecodeError:
            payload = {}
        _record_pageview(payload, self.headers)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

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
    _cleanup_unreferenced_assets()
    data = {"articles": []}
    if MANIFEST_PATH.exists():
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return _merge_disk_articles(data)


def _write_manifest(data: dict) -> None:
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    data["updatedAt"] = int(time.time() * 1000)
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_pageview(payload: dict, headers) -> None:
    path = _clean_path(payload.get("path") or "/")
    if path.startswith("/admin") or path.startswith("/api"):
        return
    now = int(time.time())
    visitor_seed = str(payload.get("visitorId") or "")
    session_seed = str(payload.get("sessionId") or "")
    ip = _client_ip(headers)
    event = {
        "ts": now,
        "day": time.strftime("%Y-%m-%d", time.localtime(now)),
        "path": path,
        "title": _trim(payload.get("title"), 160),
        "referrer": _clean_referrer(payload.get("referrer")),
        "visitor": _hash_value(visitor_seed or ip or str(headers.get("user-agent", ""))),
        "session": _hash_value(session_seed or f"{ip}:{headers.get('user-agent', '')}:{now // 1800}"),
        "ua": _trim(headers.get("user-agent", ""), 240),
        "ipHash": _hash_value(ip),
    }
    with ANALYTICS_LOCK:
        ANALYTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ANALYTICS_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        _compact_analytics_if_needed()


def _analytics_summary() -> dict:
    events = _read_analytics_events()
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    cutoff_7d = now - 7 * 24 * 60 * 60
    cutoff_30d = now - 30 * 24 * 60 * 60
    events_7d = [event for event in events if int(event.get("ts", 0)) >= cutoff_7d]
    events_30d = [event for event in events if int(event.get("ts", 0)) >= cutoff_30d]
    today_events = [event for event in events if event.get("day") == today]
    days = []
    for offset in range(13, -1, -1):
        day_ts = now - offset * 24 * 60 * 60
        day = time.strftime("%Y-%m-%d", time.localtime(day_ts))
        day_events = [event for event in events if event.get("day") == day]
        days.append({
            "date": day,
            "views": len(day_events),
            "visitors": _unique_count(day_events, "visitor"),
        })
    return {
        "summary": {
            "totalViews": len(events),
            "todayViews": len(today_events),
            "totalVisitors": _unique_count(events, "visitor"),
            "visitors7d": _unique_count(events_7d, "visitor"),
            "views30d": len(events_30d),
        },
        "days": days,
        "topPaths": _top_paths(events_30d),
        "updatedAt": int(time.time() * 1000),
    }


def _read_analytics_events() -> list[dict]:
    if not ANALYTICS_PATH.exists():
        return []
    events = []
    with ANALYTICS_LOCK:
        lines = ANALYTICS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-ANALYTICS_MAX_EVENTS:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("path") and event.get("ts"):
            events.append(event)
    return events


def _compact_analytics_if_needed() -> None:
    try:
        lines = ANALYTICS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    if len(lines) <= ANALYTICS_MAX_EVENTS * 1.2:
        return
    ANALYTICS_PATH.write_text("\n".join(lines[-ANALYTICS_MAX_EVENTS:]) + "\n", encoding="utf-8")


def _top_paths(events: list[dict], limit: int = 10) -> list[dict]:
    grouped: dict[str, dict] = {}
    for event in events:
        path = str(event.get("path") or "/")
        item = grouped.setdefault(path, {"path": path, "views": 0, "visitors": set()})
        item["views"] += 1
        if event.get("visitor"):
            item["visitors"].add(event["visitor"])
    rows = [
        {"path": item["path"], "views": item["views"], "visitors": len(item["visitors"])}
        for item in grouped.values()
    ]
    return sorted(rows, key=lambda row: (-row["views"], row["path"]))[:limit]


def _unique_count(events: list[dict], key: str) -> int:
    return len({event.get(key) for event in events if event.get(key)})


def _client_ip(headers) -> str:
    forwarded = str(headers.get("x-forwarded-for", ""))
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return str(headers.get("x-real-ip", "")).strip()


def _hash_value(value: str) -> str:
    if not value:
        return ""
    secret = (PASSWORD or "garvynlabs").encode("utf-8")
    return hmac.new(secret, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _clean_path(value: str) -> str:
    path = str(value or "/").strip()[:240]
    if not path.startswith("/"):
        path = "/" + path
    return path


def _clean_referrer(value: str) -> str:
    referrer = _trim(value, 240)
    if "garvynlabs.com" in referrer:
        return ""
    return referrer


def _trim(value: object, limit: int) -> str:
    return str(value or "").replace("\n", " ").strip()[:limit]


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


def _jarvis_subcategory(value: str) -> str:
    return value if value in JARVIS_SUBCATEGORIES else "product-iteration"


def _article_subcategory(category: str, payload_or_meta: dict) -> str:
    return _jarvis_subcategory(str(payload_or_meta.get("subcategory", ""))) if category == "jarvis" else ""


def _article_from_path(path: Path) -> dict | None:
    try:
        rel = path.resolve().relative_to(CONTENT_ROOT.resolve())
    except ValueError:
        return None
    if len(rel.parts) < 2:
        return None
    category = rel.parts[0]
    suffix = path.suffix.lower()
    if category not in CATEGORIES or suffix not in {".md", ".pdf"}:
        return None
    slug = path.stem
    if suffix == ".pdf":
        body_path = path.with_suffix(".md")
        meta = {}
        body = ""
        if body_path.exists():
            try:
                meta, body = _parse_frontmatter(body_path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                meta, body = _parse_frontmatter(body_path.read_text(encoding="utf-8", errors="replace"))
        return {
            "slug": _safe_slug(meta.get("slug") or slug),
            "kind": "pdf",
            "category": meta.get("category") if meta.get("category") in CATEGORIES else category,
            "subcategory": _article_subcategory(meta.get("category") if meta.get("category") in CATEGORIES else category, meta),
            "title": meta.get("title") or _title_from_body(body, slug),
            "summary": meta.get("summary", ""),
            "date": meta.get("date", ""),
            "file": f"/content/{category}/{path.name}",
            "bodyFile": f"/content/{category}/{body_path.name}" if body_path.exists() else "",
        }
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _parse_frontmatter(text)
    return {
        "slug": _safe_slug(meta.get("slug") or slug),
        "kind": "md",
        "category": meta.get("category") if meta.get("category") in CATEGORIES else category,
        "subcategory": _article_subcategory(meta.get("category") if meta.get("category") in CATEGORIES else category, meta),
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
            for path in sorted([*category_dir.glob("*.md"), *category_dir.glob("*.pdf")]):
                article = _article_from_path(path)
                if article:
                    existing = articles_by_slug.get(article["slug"], {})
                    if article.get("kind") == "pdf":
                        articles_by_slug[article["slug"]] = {**article, **existing, "kind": "pdf", "file": article["file"]}
                    else:
                        articles_by_slug[article["slug"]] = {**existing, **article}
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
        "kind": "md",
        "category": category,
        "subcategory": _article_subcategory(category, payload),
        "title": payload.get("title", slug),
        "summary": payload.get("summary", ""),
        "date": payload.get("date", ""),
        "file": f"/content/{category}/{slug}.md",
    }
    path = _article_path(article)
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = [
        "---",
        f"title: {article['title']}",
        f"date: {article['date']}",
        f"category: {category}",
    ]
    if category == "jarvis":
        frontmatter_lines.append(f"subcategory: {article['subcategory']}")
    frontmatter_lines.extend([f"summary: {article['summary']}", "---", ""])
    frontmatter = "\n".join(frontmatter_lines) + "\n"
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


def _save_pdf(payload: dict) -> dict:
    category = payload.get("category", "ai-news")
    if category not in CATEGORIES:
        raise ValueError("invalid category")
    manifest = _load_manifest()
    articles = manifest.setdefault("articles", [])
    original_slug = payload.get("originalSlug") or ""
    slug = _safe_slug(payload.get("slug") or payload.get("title") or Path(payload.get("fileName", "document")).stem)
    slug = _unique_slug(slug, articles, original_slug=original_slug)
    existing_file = str(payload.get("existingFile") or "")
    file_data = str(payload.get("fileData") or "")
    article = {
        "slug": slug,
        "kind": "pdf",
        "category": category,
        "subcategory": _article_subcategory(category, payload),
        "title": payload.get("title", slug),
        "summary": payload.get("summary", ""),
        "date": payload.get("date", ""),
        "file": f"/content/{category}/{slug}.pdf",
        "bodyFile": f"/content/{category}/{slug}.md",
    }
    path = _article_path(article)
    body_path = _article_path({"file": article["bodyFile"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    if file_data:
        path.write_bytes(base64.b64decode(file_data))
    elif existing_file:
        old_article = {"file": existing_file}
        old_path = _article_path(old_article)
        if old_path.exists() and old_path != path:
            path.write_bytes(old_path.read_bytes())
            old_path.unlink()
        elif not old_path.exists():
            raise ValueError("existing PDF file not found")
    else:
        raise ValueError("PDF file data is required")
    body = str(payload.get("body") or "").lstrip()
    frontmatter_lines = [
        "---",
        f"title: {article['title']}",
        f"date: {article['date']}",
        f"category: {category}",
    ]
    if category == "jarvis":
        frontmatter_lines.append(f"subcategory: {article['subcategory']}")
    frontmatter_lines.extend([
        f"summary: {article['summary']}",
        "kind: pdf",
        f"pdf: {article['file']}",
        "---",
        "",
    ])
    frontmatter = "\n".join(frontmatter_lines) + "\n"
    body_path.write_text(frontmatter + body, encoding="utf-8")

    replaced = False
    for index, existing in enumerate(articles):
        if original_slug and existing.get("slug") == original_slug:
            old_path = _article_path(existing)
            old_body_path = _article_path({"file": existing.get("bodyFile", "")}) if existing.get("bodyFile") else None
            articles[index] = article
            replaced = True
            if old_path != path and old_path.exists():
                old_path.unlink()
            if old_body_path and old_body_path != body_path and old_body_path.exists():
                old_body_path.unlink()
            break
    if not replaced:
        articles.append(article)
    _write_manifest(manifest)
    return article


def _save_asset(payload: dict) -> dict:
    file_name = str(payload.get("fileName") or "image")
    ext = Path(file_name).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError("unsupported image type")
    file_data = str(payload.get("fileData") or "")
    if not file_data:
        raise ValueError("image data is required")
    stem = _safe_slug(Path(file_name).stem or "image")
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    path = _unique_asset_path(stem, ext)
    path.write_bytes(base64.b64decode(file_data))
    url = f"/content/assets/{path.name}"
    alt = _clean_alt_text(payload.get("alt") or Path(file_name).stem or "image")
    return {"url": url, "markdown": f"![{alt}]({url})"}


def _unique_asset_path(stem: str, ext: str) -> Path:
    timestamp = int(time.time() * 1000)
    candidate = ASSET_ROOT / f"{stem}-{timestamp}{ext}"
    index = 2
    while candidate.exists():
        candidate = ASSET_ROOT / f"{stem}-{timestamp}-{index}{ext}"
        index += 1
    resolved = candidate.resolve()
    if ASSET_ROOT.resolve() not in resolved.parents:
        raise ValueError("invalid asset path")
    return candidate


def _clean_alt_text(value: str) -> str:
    return str(value or "image").replace("[", "").replace("]", "").replace("\n", " ").strip() or "image"


def _cleanup_unreferenced_assets() -> None:
    if not ASSET_ROOT.exists():
        return
    referenced = _referenced_assets()
    cutoff = time.time() - ASSET_TTL_SECONDS
    for path in ASSET_ROOT.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        if f"/content/assets/{path.name}" in referenced:
            continue
        path.unlink()


def _referenced_assets() -> set[str]:
    references: set[str] = set()
    if not CONTENT_ROOT.exists():
        return references
    for category in CATEGORIES:
        category_dir = CONTENT_ROOT / category
        if not category_dir.exists():
            continue
        for path in category_dir.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="replace")
            references.update(re.findall(r"/content/assets/[^)\s\"']+", text))
    return references


def _delete_article(slug: str) -> dict | None:
    manifest = _load_manifest()
    articles = manifest.setdefault("articles", [])
    for index, article in enumerate(articles):
        if article.get("slug") != slug:
            continue
        path = _article_path(article)
        if path.exists():
            path.unlink()
        if article.get("bodyFile"):
            body_path = _article_path({"file": article["bodyFile"]})
            if body_path.exists():
                body_path.unlink()
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
