# Jarvis — 隐私可控的多 Agent 金融研究系统

Jarvis 最初是一个面向 CNY/AUD 的实时汇率监控 Agent，用于追踪汇率波动、相关新闻和用户自定义提醒阈值，并通过 Telegram 向用户推送高价值提醒。随着项目演进，Jarvis 已经从单一的汇率监控工具，逐步升级为一个面向金融研究场景的 privacy-aware multi-agent research system。

Jarvis 的核心定位不是“预测汇率”或“替用户做投资决策”，而是辅助用户完成金融信息搜集、证据整理、风险识别和研究简报生成。系统通过显式偏好、隐式偏好、反馈聚合和短期行为日志的分层设计，实现用户个性化能力；同时通过 Safe Context Builder 控制 LLM 只能访问安全白名单字段，避免 raw logs、完整 memory 或敏感信息进入模型上下文。用户可以通过 `/my_profile`、`/privacy`、`/delete_profile` 等命令查看、理解和删除自己的个性化数据。

## 当前能力

- **CNY/AUD 汇率监控**：追踪人民币/澳元汇率变化，支持阈值提醒和波动告警。
- **金融新闻监控**：通过新闻源持续扫描相关事件，并用 LLM 做相关性判断和摘要。
- **每日研究简报**：按固定时间生成 CNY/AUD 相关的市场观察、新闻摘要、风险提示和短期关注点。
- **Telegram Bot 交互**：支持私聊问答、命令式查询、偏好管理、反馈收集和提醒推送。
- **隐私感知个性化**：结构化保存显式偏好、推断偏好、反馈事件和短期行为事件。
- **多 Agent 研究工作流**：将研究任务拆解为 FX、News、Macro、MarketDrivers、PolicySignal、Risk 等无状态专家 Agent，再由 Supervisor 汇总。
- **证据优先的研究输出**：Agent 输出 evidence、confidence、source、missing_data，Evidence Store 再进行 source metadata、category taxonomy、scoring、routing 和 ContextPack 选择，避免只给不可审计的结论。

## 系统定位

Jarvis 面向的是 AI-assisted financial research workflow：

- 帮助用户搜集和筛选金融信息。
- 整理来自汇率、新闻、宏观和风险侧的证据。
- 生成结构化研究简报，而不是给出投资指令。
- 记录用户偏好，但不把敏感 raw logs 直接暴露给 LLM。
- 支持未来从 CNY/AUD 扩展到股票、行业、宏观主题、法律监管和用户自定义研究方向。

## 架构概览

```text
Telegram / Scheduled Jobs
        |
        v
Jarvis Orchestrator
        |
        +-- Safe Context Builder
        |     `-- explicit preferences / inferred preferences / feedback summary
        |
        +-- Research Preset
        |     `-- cny_aud, future stock, sector, macro, regulatory presets
        |
        +-- Parallel Expert Agents
        |     +-- FX Agent
        |     +-- News Agent
        |     +-- Macro Agent
        |     +-- MarketDrivers Agent
        |     +-- PolicySignal Agent
        |     `-- Risk Agent
        |
        +-- Evidence Store / Runtime Micro-RAG
        |
        `-- Supervisor
              `-- auditable research brief
```

## Phase 9：Preset-driven 并行研究工作流

在 Phase 9 中，Jarvis 引入 preset-driven 的并行研究工作流。CNY/AUD 不再是写死在系统里的唯一主题，而是第一个 research preset。每个研究任务会被拆解为多个无状态专家 Agent，例如 FX Agent、News Agent、Macro Agent、MarketDrivers Agent、PolicySignal Agent 和 Risk Agent。

各 Agent 独立运行并输出结构化 evidence、confidence、source、missing_data，再由 Supervisor 汇总为可审计的研究简报。该设计使 Jarvis 能在保留当前汇率 MVP 的基础上，未来扩展到股票、行业研究、宏观主题、法律监管分析和用户自定义研究方向。

## Runtime Micro-RAG / Dynamic Evidence Store

为了应对金融行研场景中的长上下文和多来源信息问题，Jarvis 已经引入 Runtime Micro-RAG / Dynamic Evidence Store。系统不再让上游 Agent 把长篇原文和分析全文传给 Supervisor，而是将证据切块并写入带强元数据的 Evidence Store，只向下游传递 chunk_id、summary、category、entity、importance 等引用信息。

Supervisor 再通过 metadata-first hybrid retrieval 和 context pack builder 按需检索相关证据，从而降低 token 成本、减少上下文折损，并实现研究报告的证据级溯源。

当前 Evidence Store 重点能力：

| 能力 | 说明 |
| --- | --- |
| source metadata | 记录 domain、source_type、source_tier、quality_reason 等来源质量信息 |
| category taxonomy | 显式区分 `fx_price`、`news_event`、`macro`、`policy_signal`、`market_driver`、`commodity_trade`、`risk`、`data_gap` 等证据类型 |
| attention-inspired routing | 根据 section hint、category、composite score、source quality、recency、conflict value 和 user relevance 选择 ContextPack |
| Policy/Market 平衡 | PolicySignalAgent 与 MarketDriversAgent 同时开启时，宏观 section 为有效 policy_signal 保留位置，同时保留有效 market_driver/commodity 证据 |
| debug/baseline | `/api/debug/fx_research` 和 baseline recorder 保留 selected chunks、score breakdown、policy candidates、conflict breakdown 和 retrieval traces |

## Phase 10.6H：Policy/Market 路由平衡

Phase 10.6H-fix2 解决了 PolicySignalAgent 产出存在但无法被评分/选中的问题。根因是 policy source label 过长，导致 policy chunks 的 `token_estimate` 超过 `max_chunk_tokens=1200`，在评分前被过滤。修复后，policy source label 会被压缩，policy chunks 能进入标准 EvidenceScorer，并在有效时参与宏观 section 选择。

有效 policy_signal 标准：

- `confidence >= 0.5` 或 `evidence_score >= 0.6`
- `source_tier <= 3`
- `evidence_basis != insufficient_evidence`
- direction 不是必需字段，但 neutral/None 结果需要有明确政策相关性

用户报告层不直接展示 raw conflict count，例如“识别出 N 组方向冲突”。raw/unique/reportable count 保留在 debug 和 baseline 中，用户侧使用解释性方向分歧描述。

当前阶段文档：

- `Jarvis/DEVELOPMENT.md`
- `Jarvis/docs/README.md`
- `Jarvis/docs/phase_10_6h_summary.md`
- `Jarvis/docs/baseline_073c7ec6.md`

## 隐私与个性化

Jarvis 的个性化设计遵循以下原则：

- 不依赖非结构化 LLM memory 存储用户偏好。
- 个性化数据存储在 SQLite 的结构化表中。
- LLM 个性化上下文只接收白名单字段。
- raw events 只作为短期反馈信号，带 TTL 上限并可清理。
- 用户可通过 `/my_profile` 查看资料，通过 `/privacy` 理解数据使用方式，通过 `/delete_profile` 删除结构化个性化数据。
- Safe Context Builder 不向 LLM 暴露完整 memory、raw logs、daily logs 或敏感字段。

当前结构化数据包括：

| 数据层 | 说明 |
| --- | --- |
| explicit preferences | 用户明确设置或确认的用途、提醒阈值、关注主题和摘要风格 |
| inferred preferences | 系统轻量推断出的内容偏好 |
| feedback events | useful、not useful、not interested 等反馈 |
| raw events | 短期行为事件，不直接进入 LLM 个性化上下文 |

Jarvis 不会主动要求或用于个性化存储银行卡、账户余额、身份证/护照、确切地址或详细个人财务压力等敏感信息；检测到这类内容时会尽量拒绝写入个性化资料。

## Telegram 命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 启动 Bot 或查看欢迎信息 |
| `/status` | 查看当前 Agent 状态 |
| `/my_profile` | 查看当前结构化个性化资料，不展示 raw logs |
| `/privacy` | 查看 Jarvis 隐私设计说明 |
| `/update_profile` | 进入问答式流程，逐项更新明确偏好 |
| `/feedback useful` | 记录提醒反馈 |
| `/delete_profile` | 查看删除影响范围，并在确认后删除结构化个性化数据 |

中文别名包括 `/我的资料`、`/隐私`、`/修改资料`、`/删除资料`、`/反馈`。Telegram 命令菜单仍使用英文命令名，中文别名由 Jarvis 在消息入口映射处理。

## 代码结构

```text
Jarvis/
  monitor_daemon.py                         独立监控守护进程
  pythonclaw/
    channels/telegram_bot.py                Telegram 命令与消息入口
    core/personalization/                   隐私感知个性化资料存储
    core/llm/                               OpenAI-compatible / Gemini / Anthropic provider
    core/retrieval/                         RAG 检索组件
    templates/skills/data/fx_monitor/       CNY/AUD 汇率监控与研究技能
      fetch_rate.py
      monitor_alert.py
      news_monitor.py
      research/
        agents/                             FX / News / Macro / MarketDrivers / PolicySignal / Risk Agent
        coordinator.py
        evidence_store.py
        baseline_recorder.py
        supervisor.py
        runner.py
        schema.py
```

## 运行与部署

Jarvis 是 Python 项目，依赖配置见 `Jarvis/pyproject.toml` 和 `Jarvis/pythonclaw.example.json`。

```bash
cd Jarvis
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

生产部署通常由 systemd 管理主 Agent 和监控守护进程：

```text
jarvis-agent
jarvis-monitor
```

密钥、Telegram token、LLM provider API key、Tavily key 等不进入代码仓库，应放在本机配置或部署环境变量中。

## 未来架构展望

长期来看，Jarvis 的目标是成为一个可扩展的 AI-assisted Financial Research Workflow。未来规划包括：

- **attention-inspired evidence routing**：根据实体、风险、时间敏感度和用户偏好动态分配证据注意力。
- **official policy source priority**：优先接入 RBA、PBoC、Fed 的官方政策来源。
- **dynamic conflict summary**：根据 conflict breakdown 动态生成用户可理解的方向分歧摘要。
- **queue-based agent workers**：将专家 Agent 变成可水平扩展的异步 worker。
- **serverless-style elastic agent execution**：按研究任务复杂度弹性启动 Agent 执行单元。
- **legal/regulatory RAG**：支持监管、法律、合规文本的专门检索与引用。
- **evaluation dashboard**：评估研究质量、引用覆盖率、成本、延迟和用户反馈。
- **custom research presets**：允许用户定义股票、行业、宏观主题或监管主题的研究 preset。

## 风险声明

Jarvis 输出仅用于信息整理和研究辅助，不构成投资建议、法律建议或财务决策依据。汇率、新闻和市场数据可能存在延迟、缺失或第三方错误，用户应自行核验关键来源。
