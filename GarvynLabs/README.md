# Garvyn Labs

Garvyn Labs 是用于发布 AI Agent、产品思考、工程实践和 Jarvis 开发记录的个人网站。它和生产 Jarvis 解耦：Jarvis 负责 Agent 与研究系统本身，Garvyn Labs 负责公开展示文章、项目记录和可读化文档。

当前实现是一个轻量、低依赖的网站系统：

- 前台是 framework-free static site。
- Admin 是 Python 标准库实现的 Basic Auth 后台。
- 内容以 Markdown 为主，支持 PDF 上传后生成网页正文。
- 部署到旧 VPS，由 nginx 提供静态资源和 admin 反向代理。

## 功能

### 前台网站

- 首页展示 Garvyn Labs 和四个主要栏目。
- 栏目包括：
  - `AI动态`
  - `AI产品思考`
  - `AI产品技术`
  - `Jarvis`
- Jarvis 栏目支持二级分类：
  - `修复更新`
  - `产品迭代`
  - `产品分析`
- 文章页支持 Markdown 渲染、代码块、表格、引用、任务列表、加粗、高亮和图片。
- 文章页右侧会根据 `h2 / h3 / h4` 自动生成悬浮目录，方便快速定位。
- 前台拉取 `manifest.json` 和文章文件时使用 no-store 与时间戳，保证 admin 保存后尽快刷新。

### Admin 后台

Admin 位于 `/admin/`，由 `apps/api/server.py` 提供。

当前支持：

- 新建 Markdown 文档。
- 上传 `.md` 文档。
- 上传 `.pdf`，在浏览器中提取 PDF 文本并保存为网页正文。
- 编辑 Markdown 正文并保存。
- 从本地选择图片并插入到 Markdown。
- 多选文章并删除。
- 按栏目组织左侧文章列表。
- Jarvis 栏目选择二级分类。

图片上传会保存到 `/content/assets/`，不会作为文章出现在列表中。如果图片从 Markdown 中删除，且超过 1 天没有被任何文章引用，后端会自动清理。

## 内容模型

内容目录位于：

```text
apps/web/public/content/
  manifest.json
  assets/
  ai-news/
  ai-thinking/
  ai-technology/
  jarvis/
```

文章通过 frontmatter 描述元数据：

```md
---
title: Jarvis Phase 9 Research Workflow
date: 2026-05-08
category: jarvis
subcategory: product-iteration
summary: Jarvis 从汇率监控演进到多 Agent 金融研究系统。
---

# 正文标题

文章内容...
```

`subcategory` 只对 `category: jarvis` 生效：

| 值 | 显示名称 |
| --- | --- |
| `fix-updates` | 修复更新 |
| `product-iteration` | 产品迭代 |
| `product-analysis` | 产品分析 |

旧 Jarvis 文章如果没有 `subcategory`，前台默认归入 `产品迭代`。

## 项目结构

```text
GarvynLabs/
  apps/
    api/
      server.py              Admin 页面与内容 API
    web/
      public/
        index.html
        app.js               前台路由、Markdown 渲染、目录生成
        styles.css           网站和文章阅读样式
        content/             公开内容
  deploy/
    nginx/
      garvynlabs.conf        nginx 配置
    scripts/
      deploy-static.sh       静态站点和 admin 部署脚本
  docs/
    deployment.md
```

## 本地预览

前台可以直接用静态服务器预览：

```bash
cd GarvynLabs/apps/web/public
python3 -m http.server 8080
```

然后访问：

```text
http://127.0.0.1:8080/
```

Admin 需要设置环境变量后启动：

```bash
cd GarvynLabs
GARVYNLABS_SITE_ROOT="$PWD/apps/web/public" \
GARVYNLABS_ADMIN_USER=garvyn \
GARVYNLABS_ADMIN_PASSWORD=change-me \
python3 apps/api/server.py
```

默认监听：

```text
http://127.0.0.1:8090/admin/
```

## API

Admin 后端提供少量内部 API：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/api/articles` | GET | 读取 manifest，并合并磁盘上的 `.md/.pdf` 文件 |
| `/api/article?slug=...` | GET | 读取 Markdown 正文 |
| `/api/article` | POST | 保存 Markdown 文章 |
| `/api/article?slug=...` | DELETE | 删除文章及 PDF sidecar |
| `/api/pdf` | POST | 保存 PDF 原件和生成的网页正文 |
| `/api/asset` | POST | 上传 Markdown 内嵌图片 |

这些 API 只给 admin 使用，受 Basic Auth 保护。

## 部署

部署脚本读取仓库根目录的 `.env.deploy`，同步前台静态文件和 admin 服务到 VPS，并重启 `garvynlabs-admin`。
部署默认不会同步 `apps/web/public/content/` 里的样本文章，避免生产服务器上已删除的样本内容在后续部署时被重新补回。首次空服务器只会创建空的 `content/manifest.json`，之后文章应通过 admin 后台管理。

```bash
cd GarvynLabs
bash deploy/scripts/deploy-static.sh
```

部署脚本会执行：

- 同步 `apps/web/public/` 中除 `content/` 以外的前台静态文件到 `/var/www/garvynlabs`。
- 如果远端没有 `content/manifest.json`，创建空 manifest；不会补种样本文章。
- 同步 `apps/api/server.py` 到远端 admin 目录。
- 写入或更新 systemd 服务。
- `systemctl restart garvynlabs-admin`。
- `nginx -t` 并 reload nginx。

nginx 针对 `/index.html`、`/app.js`、`/content/` 设置 no-cache，减少文章保存后的前台延迟。

## 安全边界

- 不把生产 Jarvis token、用户 raw logs、profile 数据或私有 session logs 放入 Garvyn Labs。
- Admin 使用 Basic Auth，并通过 nginx 暴露。
- 图片资源只作为文章资产保存，不进入文章列表。
- PDF 原件可以保留下载入口，但前台主要展示从 PDF 提取出的网页正文。

## 设计取向

Garvyn Labs 是一个偏阅读和工程记录的网站，不是营销落地页。前台重点是让文章可读、可快速定位、可按主题组织；后台重点是让日常写作和上传资料足够轻量。
