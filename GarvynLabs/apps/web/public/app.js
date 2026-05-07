const CATEGORY_META = {
  "ai-news": {
    title: "AI动态",
    label: "AI News",
    description: "持续追踪 AI 最新动态、模型发布、产品变化和行业信号。",
    homeText: "AI 最新动态追踪，记录模型、应用、公司和生态变化。"
  },
  "ai-thinking": {
    title: "AI产品思考",
    label: "Product Thinking",
    description: "关于 AI 产品体验、用户价值、交互范式和商业化的观察。",
    homeText: "从产品视角记录 AI 工具、用户体验和真实需求。"
  },
  "ai-technology": {
    title: "AI产品技术",
    label: "Product Technology",
    description: "面向 AI 产品落地的工程架构、Agent、评测、成本和安全边界。",
    homeText: "记录 Agent 工程、系统设计、评测、安全和部署实践。"
  },
  jarvis: {
    title: "Jarvis",
    label: "Jarvis",
    description: "个人 AI Agent 系统的开发过程、架构演进与实验记录。",
    homeText: "Jarvis 是我的个人 AI Agent 系统，也是很多工程实验的起点。"
  }
};

let manifest = { articles: [] };

const app = document.getElementById("app");
const canvas = document.getElementById("field");
const ctx = canvas.getContext("2d");

function resizeCanvas() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(window.innerWidth * dpr);
  canvas.height = Math.floor(window.innerHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawField(time = 0) {
  const width = window.innerWidth;
  const height = window.innerHeight;
  ctx.clearRect(0, 0, width, height);
  const points = 52;
  for (let i = 0; i < points; i += 1) {
    const x = (width * ((i * 37) % points)) / points + Math.sin(time / 1700 + i) * 16;
    const y = (height * ((i * 19) % points)) / points + Math.cos(time / 2100 + i) * 12;
    const r = 1.1 + ((i % 4) * 0.28);
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = i % 3 === 0 ? "rgba(8,145,178,.22)" : "rgba(37,99,235,.14)";
    ctx.fill();
    if (i % 4 === 0) {
      const x2 = x + Math.sin(i) * 92;
      const y2 = y + Math.cos(i) * 72;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x2, y2);
      ctx.strokeStyle = "rgba(15,118,110,.07)";
      ctx.stroke();
    }
  }
  requestAnimationFrame(drawField);
}

function currentRoute() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return { type: "home" };
  if (path === "/article") {
    return { type: "article", slug: new URLSearchParams(window.location.search).get("slug") || "" };
  }
  const key = path.slice(1);
  if (CATEGORY_META[key]) return { type: key === "jarvis" ? "jarvis" : "category", category: key };
  return { type: "home" };
}

function setActiveNav(route) {
  document.querySelectorAll(".nav a").forEach((link) => {
    const key = link.getAttribute("href").replaceAll("/", "");
    link.classList.toggle("active", route.category === key || route.type === key);
  });
}

async function loadManifest() {
  try {
    const res = await fetch(`/content/manifest.json?v=${Date.now()}`, { cache: "no-store" });
    if (res.ok) manifest = await res.json();
  } catch (error) {
    manifest = { articles: [] };
  }
}

function articlesFor(category) {
  return (manifest.articles || [])
    .filter((article) => article.category === category)
    .sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
}

function articleCard(article) {
  return `
    <a class="article-card" href="/article/?slug=${encodeURIComponent(article.slug)}">
      <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}</span>
      <h2>${escapeHtml(article.title)}</h2>
      <p>${escapeHtml(article.summary || "")}</p>
    </a>
  `;
}

function renderHome() {
  app.innerHTML = `
    <section class="hero">
      <h1 class="hero-title">Garvyn Labs</h1>
      <div class="scroll-cue">Scroll</div>
    </section>
    <section class="section">
      <div class="section-inner intro-section">
        <span class="eyebrow intro-tagline">AI agents, Product thinking, Engineering notes</span>
        <div class="intro-grid">
          ${Object.entries(CATEGORY_META).map(([key, item], index) => `
            <a class="intro-card" href="/${key}/">
              <span class="card-index">0${index + 1}</span>
              <h2>${item.title}</h2>
              <p>${item.homeText}</p>
            </a>
          `).join("")}
        </div>
      </div>
    </section>
  `;
}

function renderCategory(category) {
  const meta = CATEGORY_META[category];
  const articles = articlesFor(category);
  app.innerHTML = `
    <section class="page-hero">
      <div class="section-inner">
        <span class="eyebrow">${meta.label}</span>
        <h1>${meta.title}</h1>
        <p>${meta.description}</p>
      </div>
    </section>
    <section class="section">
      <div class="section-inner">
        <div class="articles">
          ${articles.length ? articles.map(articleCard).join("") : `<div class="empty">这里还没有发布的笔记。</div>`}
        </div>
      </div>
    </section>
  `;
}

function renderJarvis() {
  const articles = articlesFor("jarvis");
  app.innerHTML = `
    <section class="jarvis-hero">
      <div class="jarvis-panel">
        <span class="eyebrow">Personal AI Agent System</span>
        <h1>Jarvis</h1>
        <p>Jarvis 是一个在真实使用场景中持续演进的个人 AI Agent 系统，覆盖 Telegram Bot、CNY/AUD 汇率研究、新闻监控、个性化上下文、安全边界、成本控制和云端迁移。</p>
      </div>
    </section>
    <section class="section">
      <div class="section-inner">
        <div class="articles">
          ${articles.length ? articles.map(articleCard).join("") : `<div class="empty">Jarvis 的专题笔记结构还在整理中。</div>`}
        </div>
      </div>
    </section>
  `;
}

async function renderArticle(slug) {
  const article = (manifest.articles || []).find((item) => item.slug === slug);
  if (!article) {
    app.innerHTML = `<section class="article-shell"><article class="article"><h1>文章不存在</h1><p>没有找到这篇笔记。</p></article></section>`;
    return;
  }
  try {
    const res = await fetch(`${article.file}?v=${Date.now()}`, { cache: "no-store" });
    const raw = res.ok ? await res.text() : "# 加载失败\n\n这篇笔记暂时无法读取。";
    const body = renderMarkdown(raw);
    app.innerHTML = `
      <section class="article-shell">
        <article class="article">
          <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}</span>
          <h1>${escapeHtml(article.title)}</h1>
          <div class="article-body">${body}</div>
        </article>
      </section>
    `;
    const highlighter = window.hljs;
    if (highlighter?.highlightElement) {
      document.querySelectorAll("pre code").forEach((block) => highlighter.highlightElement(block));
    }
  } catch (err) {
    app.innerHTML = `
      <section class="article-shell">
        <article class="article">
          <h1>渲染出错</h1>
          <pre style="white-space:pre-wrap;color:#dc2626">${escapeHtml(err.message)}\n${escapeHtml(err.stack || "")}</pre>
        </article>
      </section>
    `;
  }
}

function renderMarkdown(raw) {
  const source = raw
    .replace(/^---[\s\S]*?---\s*/, "")
    .replace(/==(.+?)==/g, "<mark>$1</mark>")
    .replace(/^(\s*[-*+]) \[x\] /gim, '$1 <input type="checkbox" checked disabled class="task-cb"> ')
    .replace(/^(\s*[-*+]) \[ \] /gim, '$1 <input type="checkbox" disabled class="task-cb"> ');
  const md = window.markdownit({
    html: true,
    linkify: true,
    typographer: true,
    highlight(code, lang) {
      const highlighter = window.hljs;
      if (lang && highlighter?.getLanguage?.(lang) && highlighter?.highlight) {
        return `<pre><code class="hljs language-${escapeHtml(lang)}">${highlighter.highlight(code, { language: lang }).value}</code></pre>`;
      }
      return `<pre><code class="hljs">${md.utils.escapeHtml(code)}</code></pre>`;
    }
  });
  const html = md.render(source);
  return DOMPurify.sanitize(html, {
    ADD_TAGS: ["mark", "input"],
    ADD_ATTR: ["class", "target", "rel", "type", "checked", "disabled"]
  });
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function route() {
  const next = currentRoute();
  await loadManifest();
  setActiveNav(next);
  if (next.type === "home") renderHome();
  if (next.type === "category") renderCategory(next.category);
  if (next.type === "jarvis") renderJarvis();
  if (next.type === "article") await renderArticle(next.slug);
}

window.addEventListener("popstate", route);
document.addEventListener("click", (event) => {
  const link = event.target.closest("a");
  if (!link) return;
  const url = new URL(link.href);
  if (url.origin !== window.location.origin) return;
  event.preventDefault();
  history.pushState({}, "", url.pathname + url.search);
  route();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

resizeCanvas();
window.addEventListener("resize", resizeCanvas);
requestAnimationFrame(drawField);
loadManifest().then(route);
