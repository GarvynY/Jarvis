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

const JARVIS_SUBCATEGORIES = [
  { key: "fix-updates", title: "修复更新" },
  { key: "product-iteration", title: "产品迭代" },
  { key: "product-analysis", title: "产品分析" }
];

let manifest = { articles: [] };
let manifestSignature = "";

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
    if (!res.ok) return false;
    const nextManifest = await res.json();
    const nextSignature = signatureForManifest(nextManifest);
    const changed = Boolean(manifestSignature && nextSignature !== manifestSignature);
    manifest = nextManifest;
    manifestSignature = nextSignature;
    return changed;
  } catch (error) {
    manifest = { articles: [] };
    manifestSignature = "";
    return false;
  }
}

function signatureForManifest(data) {
  const articles = (data.articles || [])
    .map((article) => `${article.slug}:${article.category}:${article.subcategory || ""}:${article.title}:${article.summary}:${article.date}:${article.file}`)
    .sort()
    .join("|");
  return `${data.updatedAt || ""}:${articles}`;
}

function articlesFor(category) {
  return (manifest.articles || [])
    .filter((article) => article.category === category)
    .sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
}

function jarvisSubcategoryKey(article) {
  return JARVIS_SUBCATEGORIES.some((item) => item.key === article.subcategory) ? article.subcategory : "product-iteration";
}

function jarvisSubcategoryLabel(article) {
  return JARVIS_SUBCATEGORIES.find((item) => item.key === jarvisSubcategoryKey(article))?.title || "产品迭代";
}

function articleCard(article) {
  const isPdf = article.kind === "pdf" || String(article.file || "").toLowerCase().endsWith(".pdf");
  return `
    <a class="article-card" href="/article/?slug=${encodeURIComponent(article.slug)}">
      <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}${article.category === "jarvis" ? ` · ${jarvisSubcategoryLabel(article)}` : ""}${isPdf ? " · PDF" : ""}</span>
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
  const groups = JARVIS_SUBCATEGORIES.map((subcategory) => {
    const groupedArticles = articles.filter((article) => jarvisSubcategoryKey(article) === subcategory.key);
    return { ...subcategory, articles: groupedArticles };
  }).filter((group) => group.articles.length);
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
        ${groups.length ? `
          <div class="jarvis-groups">
            ${groups.map((group) => `
              <section class="jarvis-group">
                <div class="jarvis-group-heading">
                  <h2>${group.title}</h2>
                  <span>${group.articles.length}</span>
                </div>
                <div class="articles">
                  ${group.articles.map(articleCard).join("")}
                </div>
              </section>
            `).join("")}
          </div>
        ` : `<div class="empty">Jarvis 的专题笔记结构还在整理中。</div>`}
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
  if (article.kind === "pdf" || String(article.file || "").toLowerCase().endsWith(".pdf")) {
    await renderPdfArticle(article);
    return;
  }
  try {
    const res = await fetch(`${article.file}?v=${Date.now()}`, { cache: "no-store" });
    const raw = res.ok ? await res.text() : "# 加载失败\n\n这篇笔记暂时无法读取。";
    const body = renderMarkdown(raw);
    app.innerHTML = `
      <section class="article-shell">
        <aside class="article-toc" id="articleToc" aria-label="文章目录"></aside>
        <article class="article">
          <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}${article.category === "jarvis" ? ` · ${jarvisSubcategoryLabel(article)}` : ""}</span>
          <h1>${escapeHtml(article.title)}</h1>
          <div class="article-body">${body}</div>
        </article>
      </section>
    `;
    buildArticleToc();
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

async function renderPdfArticle(article) {
  const file = `${article.file}?v=${Date.now()}`;
  let body = "";
  if (article.bodyFile) {
    try {
      const res = await fetch(`${article.bodyFile}?v=${Date.now()}`, { cache: "no-store" });
      body = res.ok ? await res.text() : "";
    } catch (error) {
      body = "";
    }
  }
  const renderedBody = body ? renderMarkdown(body) : `<p>这份 PDF 暂时没有可渲染的网页正文。</p>`;
  app.innerHTML = `
    <section class="article-shell">
      <aside class="article-toc" id="articleToc" aria-label="文章目录"></aside>
      <article class="article pdf-article">
        <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}${article.category === "jarvis" ? ` · ${jarvisSubcategoryLabel(article)}` : ""} · PDF</span>
        <h1>${escapeHtml(article.title)}</h1>
        ${article.summary ? `<p class="pdf-summary">${escapeHtml(article.summary)}</p>` : ""}
        <div class="pdf-document">
          <div>
            <span class="pdf-label">PDF 原件</span>
            <p>下面是从 PDF 提取并生成的网页正文。</p>
          </div>
          <div class="pdf-actions">
            <a href="${file}" target="_blank" rel="noopener">打开 PDF</a>
            <a href="${file}" download>下载 PDF</a>
          </div>
        </div>
        <div class="article-body">${renderedBody}</div>
      </article>
    </section>
  `;
  buildArticleToc();
}

function renderMarkdown(raw) {
  const source = normalizeInlineMarkdown(raw)
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
    ADD_TAGS: ["mark", "input", "img"],
    ADD_ATTR: ["class", "target", "rel", "type", "checked", "disabled", "src", "alt", "title", "loading"]
  });
}

function normalizeInlineMarkdown(raw) {
  return String(raw || "")
    .split(/(```[\s\S]*?```)/g)
    .map((part) => {
      if (part.startsWith("```")) return part;
      return part
        .replace(/\*\*([^*\n][^*\n]*?)\*\*/g, "<strong>$1</strong>")
        .replace(/__([^_\n][^_\n]*?)__/g, "<strong>$1</strong>");
    })
    .join("");
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function slugForHeading(value, index) {
  const slug = String(value || "")
    .normalize("NFKC")
    .toLowerCase()
    .trim()
    .replace(/[^\p{Letter}\p{Number}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
  return slug || `heading-${index + 1}`;
}

function buildArticleToc() {
  const toc = document.getElementById("articleToc");
  const body = document.querySelector(".article-body");
  if (!toc || !body) return;
  const headings = [...body.querySelectorAll("h2, h3, h4")];
  if (headings.length < 2) {
    toc.remove();
    return;
  }
  const used = new Map();
  const items = headings.map((heading, index) => {
    const base = slugForHeading(heading.textContent, index);
    const count = used.get(base) || 0;
    used.set(base, count + 1);
    const id = count ? `${base}-${count + 1}` : base;
    heading.id = id;
    return {
      id,
      text: heading.textContent.trim(),
      level: heading.tagName.toLowerCase()
    };
  });
  toc.innerHTML = `
    <div class="toc-title">目录</div>
    <nav>
      ${items.map((item) => `<a class="toc-link toc-${item.level}" href="#${encodeURIComponent(item.id)}">${escapeHtml(item.text)}</a>`).join("")}
    </nav>
  `;
  const links = [...toc.querySelectorAll("a")];
  links.forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const id = decodeURIComponent(link.getAttribute("href").slice(1));
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
      history.replaceState({}, "", `${location.pathname}${location.search}#${encodeURIComponent(id)}`);
    });
  });
  if ("IntersectionObserver" in window) {
    const byId = new Map(links.map((link) => [decodeURIComponent(link.getAttribute("href").slice(1)), link]));
    const observer = new IntersectionObserver((entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (!visible) return;
      links.forEach((link) => link.classList.toggle("active", link === byId.get(visible.target.id)));
    }, { rootMargin: "-130px 0px -68% 0px", threshold: 0 });
    headings.forEach((heading) => observer.observe(heading));
  }
}

async function route(options = {}) {
  const next = currentRoute();
  if (!options.skipManifest) await loadManifest();
  setActiveNav(next);
  if (next.type === "home") renderHome();
  if (next.type === "category") renderCategory(next.category);
  if (next.type === "jarvis") renderJarvis();
  if (next.type === "article") await renderArticle(next.slug);
}

window.addEventListener("popstate", route);
window.addEventListener("storage", (event) => {
  if (event.key === "garvynlabs-content-updated") route();
});
window.addEventListener("focus", () => route());
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
setInterval(async () => {
  const changed = await loadManifest();
  if (changed) route({ skipManifest: true });
}, 1000);
route();
