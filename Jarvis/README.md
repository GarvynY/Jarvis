# Jarvis — 隐私可控的多 Agent 金融研究系统

Jarvis 最初是一个面向 CNY/AUD 的实时汇率监控 Agent，用于追踪汇率波动、相关新闻和用户自定义提醒阈值，并通过 Telegram 向用户推送高价值提醒。随着项目演进，Jarvis 已经从单一的汇率监控工具，逐步升级为一个面向金融研究场景的 privacy-aware multi-agent research system。

Jarvis 的核心定位不是“预测汇率”或“替用户做投资决策”，而是辅助用户完成金融信息搜集、证据整理、风险识别和研究简报生成。系统通过显式偏好、隐式偏好、反馈聚合和短期行为日志的分层设计，实现用户个性化能力；同时通过 Safe Context Builder 控制 LLM 只能访问安全白名单字段，避免 raw logs、完整 memory 或敏感信息进入模型上下文。

## 当前能力

- CNY/AUD 汇率监控、阈值提醒和波动告警。
- 金融新闻监控、相关性判断和摘要推送。
- Telegram Bot 交互、用户反馈和隐私命令。
- 隐私感知个性化资料管理。
- Preset-driven 的并行研究工作流。
- FX、News、Macro、Risk 等无状态专家 Agent。
- Supervisor 汇总 evidence、confidence、source、missing_data，生成可审计研究简报。

## 架构

```text
Research Preset
      |
      v
Parallel Expert Agents
      +-- FX Agent
      +-- News Agent
      +-- Macro Agent
      `-- Risk Agent
      |
      v
Runtime Evidence Store / Micro-RAG
      |
      v
Supervisor
      |
      v
Auditable Research Brief
```

## Phase 9：Preset-driven 研究工作流

在 Phase 9 中，CNY/AUD 不再是写死在系统里的唯一主题，而是第一个 research preset。每个研究任务会被拆解为多个无状态专家 Agent。各 Agent 独立运行并输出结构化 evidence / confidence / source / missing_data，再由 Supervisor 汇总为可审计的研究简报。

该设计使 Jarvis 能在保留当前汇率 MVP 的基础上，未来扩展到股票、行业研究、宏观主题、法律监管分析和用户自定义研究方向。

## Runtime Micro-RAG / Dynamic Evidence Store

Jarvis 正在引入 Runtime Micro-RAG / Dynamic Evidence Store。系统不再让上游 Agent 把长篇原文和分析全文传给 Supervisor，而是将证据切块并写入带强元数据的 Evidence Store，只向下游传递 chunk_id、summary、category、entity、importance 等引用信息。

Supervisor 再通过 metadata-first hybrid retrieval 和 context pack builder 按需检索相关证据，从而降低 token 成本、减少上下文折损，并实现研究报告的证据级溯源。

## 隐私设计

Jarvis 通过 Safe Context Builder 控制 LLM 上下文，只允许白名单字段进入模型：

- 显式偏好：用途、目标汇率、提醒阈值、摘要风格、关注主题。
- 隐式偏好：轻量推断出的内容偏好。
- 反馈聚合：useful、not useful、not interested 等统计信号。
- 短期事件摘要：带 TTL 的行为摘要，不暴露完整 raw logs。

用户可通过 `/my_profile`、`/privacy`、`/delete_profile` 查看、理解和删除自己的个性化数据。

## 目录结构

```text
monitor_daemon.py
pythonclaw/
  channels/telegram_bot.py
  core/personalization/
  core/retrieval/
  templates/skills/data/fx_monitor/
    fetch_rate.py
    monitor_alert.py
    news_monitor.py
    research/
      agents/
      coordinator.py
      supervisor.py
      runner.py
      schema.py
```

## 未来展望

长期来看，Jarvis 的目标是成为一个可扩展的 AI-assisted Financial Research Workflow。未来规划包括 attention-inspired evidence routing、queue-based agent workers、serverless-style elastic agent execution、legal/regulatory RAG，以及用于评估研究质量、引用覆盖率、成本、延迟和用户反馈的一套 evaluation dashboard。

## 声明

Jarvis 输出仅用于信息整理和研究辅助，不构成投资建议、法律建议或财务决策依据。
