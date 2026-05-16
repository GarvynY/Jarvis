const CATEGORY_META = {
  "ai-news": {
    title: "AI最新动态",
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
  },
};

const JARVIS_SUBCATEGORIES = [
  { key: "fix-updates", title: "修复更新" },
  { key: "product-iteration", title: "产品迭代" },
  { key: "product-analysis", title: "产品分析" }
];

const THINKING_SUBCATEGORIES = [
  { key: "general", title: "AI产品思考" },
  { key: "retro-general", title: "Jarvis 深度复盘" },
  { key: "retro-part1", title: "Part 1：产品定位与架构演进" },
  { key: "retro-part2", title: "Part 2：证据系统与可信输出" },
  { key: "retro-part3", title: "Part 3：Token 与上下文治理" },
  { key: "retro-part4", title: "Part 4：数据质量与垂直金融场景" },
  { key: "retro-part5", title: "Part 5：用户信任与个性化" },
  { key: "retro-part6", title: "Part 6：审计、评估与系统进化" }
];

const RETRO_PARTS = THINKING_SUBCATEGORIES.filter((s) => s.key.startsWith("retro-") && s.key !== "retro-general");

const PROJECT_ITEMS = [
  {
    title: "Realbrain",
    label: "Project",
    description: "项目介绍正在整理中。"
  },
  {
    slug: "cloud-platform",
    title: "云服务数据平台",
    label: "Platform",
    description: "基于 Kubernetes、Fission、Redis + RQ、Elasticsearch 构建的云原生社交媒体情绪分析平台，用于采集、处理和可视化澳大利亚大选相关公开数据。",
    file: "/content/projects/cloud-platform.md"
  }
];

let manifest = { articles: [] };
let manifestSignature = "";

const app = document.getElementById("app");
const canvas = document.getElementById("field");
const ctx = canvas.getContext("2d");
const analyticsState = {
  sessionId: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
  lastPath: ""
};

function visitorId() {
  const key = "garvynlabs-visitor-id";
  let id = localStorage.getItem(key);
  if (!id) {
    id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(key, id);
  }
  return id;
}

function trackPageview() {
  const path = `${location.pathname}${location.search}`;
  if (path === analyticsState.lastPath || path.startsWith("/admin") || path.startsWith("/api")) return;
  analyticsState.lastPath = path;
  const payload = {
    path,
    title: document.title,
    referrer: document.referrer,
    visitorId: visitorId(),
    sessionId: analyticsState.sessionId
  };
  const body = JSON.stringify(payload);
  if (navigator.sendBeacon) {
    navigator.sendBeacon("/api/track", new Blob([body], { type: "application/json" }));
    return;
  }
  fetch("/api/track", { method: "POST", headers: { "content-type": "application/json" }, body, keepalive: true }).catch(() => {});
}

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
  if (path === "/about") return { type: "about" };
  if (path === "/projects") return { type: "projects" };
  if (path.startsWith("/projects/")) {
    return { type: "project-detail", slug: path.split("/")[2] };
  }
  const key = path.slice(1);
  if (key === "jarvis") return { type: "jarvis", category: "jarvis" };
  if (key === "ai-thinking") return { type: "ai-thinking", category: "ai-thinking" };
  if (CATEGORY_META[key]) return { type: "category", category: key };
  return { type: "home" };
}

function setActiveNav(route) {
  document.querySelectorAll(".nav a").forEach((link) => {
    const href = link.getAttribute("href");
    const key = href.replaceAll("/", "");
    const isHome = href === "/" && route.type === "home";
    link.classList.toggle("active", isHome || route.category === key || route.type === key);
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

function thinkingSubcategoryKey(article) {
  return THINKING_SUBCATEGORIES.some((item) => item.key === article.subcategory) ? article.subcategory : "general";
}

function thinkingSubcategoryLabel(article) {
  const key = thinkingSubcategoryKey(article);
  if (key === "general") return "AI产品思考";
  if (key === "retro-general") return "Jarvis 深度复盘";
  const part = RETRO_PARTS.find((item) => item.key === key);
  return part ? `Jarvis 深度复盘 · ${part.title}` : "AI产品思考";
}

function subcategoryLabelFor(article) {
  if (article.category === "jarvis") return jarvisSubcategoryLabel(article);
  if (article.category === "ai-thinking") return thinkingSubcategoryLabel(article);
  return "";
}

function articleCard(article) {
  const isPdf = article.kind === "pdf" || String(article.file || "").toLowerCase().endsWith(".pdf");
  return `
    <a class="article-card" href="/article/?slug=${encodeURIComponent(article.slug)}">
      <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}${subcategoryLabelFor(article) ? ` · ${subcategoryLabelFor(article)}` : ""}${isPdf ? " · PDF" : ""}</span>
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

function renderAbout() {
  app.innerHTML = `
    <section class="page-hero">
      <div class="section-inner">
        <span class="eyebrow">About</span>
        <h1>关于我</h1>
        <p>我是一名具有计算机与数据科学背景的 AI 产品与 Agent 系统方向实践者，关注 AI Agent、大模型应用、可信个性化、金融研究工作流与专业场景中的智能系统设计。</p>
      </div>
    </section>
    <section class="section about-section">
      <div class="section-inner">
        <div class="about-layout">
          <article class="about-panel about-focus">
            <span class="about-label">Focus</span>
            <h2>我关注的方向</h2>
            <div class="about-tags">
              <span>AI Agent</span>
              <span>大模型应用</span>
              <span>可信个性化</span>
              <span>金融研究工作流</span>
              <span>人机决策边界</span>
            </div>
          </article>

          <article class="about-panel about-wide">
            <span class="about-label">Questions</span>
            <h2>我关注的问题</h2>
            <p>当大模型从“回答问题”走向“参与工作流”时，产品应该如何设计它的能力边界、上下文边界和责任边界。在我看来，AI 产品的核心不只是模型能力，而是如何把模型嵌入真实任务中：它应该看到哪些信息，不应该看到哪些信息；它应该什么时候自动执行，什么时候提醒人工判断；它的结论如何被追溯、验证和修正；它如何在成本、可靠性和用户信任之间取得平衡。</p>
            <ul class="about-list">
              <li>AI Agent 如何从单轮问答走向真实业务工作流</li>
              <li>个性化能力和用户隐私边界的平衡</li>
              <li>多 Agent 系统如何避免上下文爆炸、幻觉和不可追溯</li>
              <li>在金融、医药、企业智能等高风险场景中，AI 的辅助定位而不是越界决策</li>
              <li>AI 产品建立可评估、可复查、可持续迭代的反馈闭环</li>
            </ul>
          </article>

          <article class="about-panel">
            <span class="about-label">Core Project</span>
            <h2>我的核心项目</h2>
            <p>实践是验证思考的最佳方式，Jarvis 最初是一个人民币兑澳元汇率监控 Agent，用于追踪汇率变化、相关新闻和用户自定义提醒。随着项目迭代，我将其扩展为一个隐私可控的多智能体金融研究工作流：系统能够拆解研究任务，调用不同专家 Agent 进行分析，通过安全上下文保护用户数据，并以结构化证据和可审计简报的形式输出研究结果。</p>
          </article>

          <article class="about-panel">
            <span class="about-label">Practice</span>
            <h2>其他实践项目</h2>
            <p>除了 Jarvis，我也参与过企业级 AI 分析平台、数据产品、云原生服务架构和数据科学相关项目。这些经历让我意识到，AI 产品能否真正落地，往往取决于模型之外的部分：数据结构、系统可靠性、用户反馈、可解释性、合规边界和持续迭代机制。</p>
          </article>

          <article class="about-panel">
            <span class="about-label">Site</span>
            <h2>关于本站</h2>
            <p>这个网站将长期记录我对 AI 产品和 Agent 系统的开发实践与思考。在这里，我会持续整理行业动态、产品思考、技术实践、项目复盘，以及对未来 AI 工作流的探索。</p>
          </article>
        </div>
        <div class="about-contact">
          <span class="about-label">Contact</span>
          <h2>联系方式</h2>
          <div class="contact-links">
            <a href="mailto:yuan.gao.2@student.unimelb.edu.au">yuan.gao.2@student.unimelb.edu.au</a>
            <a href="https://www.linkedin.com/in/yuan-gao-garvyn0922" target="_blank" rel="noopener">LinkedIn</a>
            <a href="https://github.com/GarvynY/Jarvis" target="_blank" rel="noopener">GitHub</a>
            <span class="resume-placeholder">Resume · 点击下载（待上传）</span>
          </div>
        </div>
      </div>
    </section>
  `;
}

function renderProjects() {
  app.innerHTML = `
    <section class="page-hero">
      <div class="section-inner">
        <span class="eyebrow">Projects</span>
        <h1>其他项目作品</h1>
        <p>这里将用于展示 Jarvis 之外的产品、平台和实验项目。</p>
      </div>
    </section>
    <section class="section">
      <div class="section-inner">
        <div class="project-grid">
          ${PROJECT_ITEMS.map((project) => project.slug ? `
            <a class="project-card project-card-link" href="/projects/${project.slug}/">
              <span>${project.label}</span>
              <h2>${project.title}</h2>
              <p>${project.description}</p>
            </a>
          ` : `
            <article class="project-card">
              <span>${project.label}</span>
              <h2>${project.title}</h2>
              <p>${project.description}</p>
            </article>
          `).join("")}
        </div>
      </div>
    </section>
  `;
}

async function renderProjectDetail(slug) {
  const project = PROJECT_ITEMS.find((p) => p.slug === slug);
  if (!project || !project.file) {
    app.innerHTML = `<section class="article-shell"><article class="article"><h1>项目不存在</h1><p>没有找到这个项目。</p></article></section>`;
    return;
  }
  try {
    const res = await fetch(`${project.file}?v=${Date.now()}`, { cache: "no-store" });
    const raw = res.ok ? await res.text() : "# 加载失败\n\n项目内容暂时无法读取。";
    const body = renderMarkdown(raw);
    app.innerHTML = `
      <section class="article-shell">
        <aside class="article-toc" id="articleToc" aria-label="文章目录"></aside>
        <article class="article">
          <span class="article-meta">${project.label} · 其他项目作品</span>
          <h1>${escapeHtml(project.title)}</h1>
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
          <pre style="white-space:pre-wrap;color:#dc2626">${escapeHtml(err.message)}</pre>
        </article>
      </section>
    `;
  }
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
        <p>Jarvis 是一个从人民币兑澳元汇率监控场景起步、逐步演进为隐私可控型多智能体金融研究工作流的人工智能系统，旨在支持多源信息检索、结构化证据聚合、风险识别与可审计研究简报生成。</p>
      </div>
    </section>
    <section class="section jarvis-vision">
      <div class="section-inner">
        <span class="eyebrow">System Direction</span>
        <h2>Jarvis 和最终形态展望</h2>
        <div class="jarvis-vision-copy">
          <article class="jarvis-vision-card">
            <span>Current</span>
            <p>Jarvis 当前以人民币兑澳元实时汇率监控作为首个落地场景，已具备汇率异常监测、相关新闻追踪、自动提醒、模型辅助分析、用户偏好管理与安全上下文控制等能力。系统的定位不是替代用户直接做金融决策，而是作为研究辅助工具，帮助用户更高效地获取信息、理解影响因素、识别潜在风险，并形成可追溯的研究结论。</p>
          </article>
          <article class="jarvis-vision-card">
            <span>Workflow</span>
            <p>在后续演进中，Jarvis 将从单一汇率监控工具扩展为面向金融信息研究的多智能体工作流。系统会将一个研究问题拆解为多个专业视角，例如汇率走势、新闻事件、宏观政策、央行信号、行业信息和风险校验等。不同智能体独立完成各自任务，并输出统一格式的证据、置信度、来源和缺失信息，最终由总控模块进行汇总，生成包含结论、依据、风险和不确定性的研究简报。</p>
          </article>
          <article class="jarvis-vision-card">
            <span>Evidence</span>
            <p>为了支持更复杂的行业研究和大量资料检索，Jarvis 正在引入动态证据库机制。系统不会将所有原文和中间分析一次性传给大模型，而是将重要证据切分、标注并存储为可检索的证据片段。每个证据片段都会带有主题类别、相关实体、信息来源、时间戳、重要性等结构化标签。总控模块在生成报告时，会先根据这些标签筛选相关证据，再提取必要内容，从而降低上下文成本，减少无关信息干扰，并提升结论的可追溯性。</p>
          </article>
          <article class="jarvis-vision-card">
            <span>Long Term</span>
            <p>长期来看，Jarvis 将继续扩展证据加权、动态深挖、法律与监管信息检索、任务队列化执行、弹性智能体调度和研究质量评估等能力。系统未来不仅可以服务于汇率监控，也可以扩展到行业研究、公司分析、宏观专题、政策监管和用户自定义研究方向，形成一个更通用、更可靠、可复查的金融研究辅助平台。</p>
          </article>
        </div>
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
    <section class="section jarvis-repo-section">
      <div class="section-inner">
        <a class="jarvis-repo-link" href="https://github.com/GarvynY/Jarvis" target="_blank" rel="noopener">
          <span class="jarvis-repo-label">GitHub Repository</span>
          <strong>GarvynY / Jarvis</strong>
          <span class="jarvis-repo-url">github.com/GarvynY/Jarvis</span>
        </a>
      </div>
    </section>
  `;
}

function renderThinking() {
  const meta = CATEGORY_META["ai-thinking"];
  const articles = articlesFor("ai-thinking");
  const generalArticles = articles.filter((a) => thinkingSubcategoryKey(a) === "general");
  const retroIntroArticles = articles.filter((a) => thinkingSubcategoryKey(a) === "retro-general");
  const retroGroups = RETRO_PARTS.map((part) => {
    const grouped = articles.filter((a) => thinkingSubcategoryKey(a) === part.key);
    return { ...part, articles: grouped };
  }).filter((g) => g.articles.length);
  const retroTotal = retroIntroArticles.length + retroGroups.reduce((n, g) => n + g.articles.length, 0);
  const hasRetro = retroTotal > 0;
  const hasGeneral = generalArticles.length > 0;
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
        ${hasGeneral ? `
          <div class="articles" style="margin-bottom:36px">
            ${generalArticles.map(articleCard).join("")}
          </div>
        ` : ""}
        ${hasRetro ? `
          <div class="jarvis-groups">
            <section class="jarvis-group">
              <div class="jarvis-group-heading">
                <h2>Jarvis 深度复盘系列</h2>
                <span>${retroTotal}</span>
              </div>
              ${retroIntroArticles.length ? `
                <div class="articles">
                  ${retroIntroArticles.map(articleCard).join("")}
                </div>
              ` : ""}
              <div class="retro-parts">
                ${retroGroups.map((group) => `
                  <section class="jarvis-group" style="margin-left:16px">
                    <div class="jarvis-group-heading">
                      <h3>${group.title}</h3>
                      <span>${group.articles.length}</span>
                    </div>
                    <div class="articles">
                      ${group.articles.map(articleCard).join("")}
                    </div>
                  </section>
                `).join("")}
              </div>
            </section>
          </div>
        ` : ""}
        ${!hasGeneral && !hasRetro ? `<div class="empty">这里还没有发布的笔记。</div>` : ""}
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
          <span class="article-meta">${article.date || ""} · ${CATEGORY_META[article.category]?.title || ""}${subcategoryLabelFor(article) ? ` · ${subcategoryLabelFor(article)}` : ""}</span>
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
  if (next.type === "about") renderAbout();
  if (next.type === "projects") renderProjects();
  if (next.type === "project-detail") await renderProjectDetail(next.slug);
  if (next.type === "category") renderCategory(next.category);
  if (next.type === "jarvis") renderJarvis();
  if (next.type === "ai-thinking") renderThinking();
  if (next.type === "article") await renderArticle(next.slug);
  trackPageview();
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
