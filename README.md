# Jarvis — CNY/AUD 汇率监控 Agent

基于 [PythonClaw](https://github.com/agentic-ai/pythonclaw) 开发的专属汇率监控 AI Agent，通过 Telegram Bot 实时追踪人民币/澳元（CNY/AUD）汇率，结合中东地缘政治新闻和澳洲经济基本面进行分析与预测。

## 功能

- **每日早报**：墨尔本时间 09:00 自动推送 CNY/AUD 汇率分析 + 短期预测（含 Tavily 深度搜索）
- **新闻监控**：每 20 分钟扫描 Google News RSS，DeepSeek 自动判断相关性，有实质影响才推送简析
- **汇率告警**：每 30 分钟检测波动，超过阈值（默认 ±0.3%）推送告警
- **联合告警**：突发新闻 + 汇率从 48 小时高点下跌 ≥ 0.8% 同时触发时，推送带 AI 分析的深度告警
- **Telegram 对话**：可直接向 Bot 提问汇率走势、换汇建议、新闻分析等
- **隐私感知个性化**：结构化保存用户偏好，支持 `/my_profile`、`/privacy`、`/delete_profile`

## 技术架构

| 模块 | 说明 |
|------|------|
| AI 框架 | [PythonClaw](https://github.com/agentic-ai/pythonclaw) — 自主 AI Agent 框架 |
| LLM | DeepSeek (`deepseek-chat`) — 比 Claude Haiku 便宜 3-5 倍，OpenAI 兼容接口 |
| 实时汇率 | open.er-api.com（免费，~1 分钟延迟，无需 API Key） |
| 历史数据 | Yahoo Finance via yfinance（免费日线） |
| 新闻监控 | Google News RSS（免费，无需 API Key，零 Tavily 消耗） |
| 深度新闻搜索 | Tavily（仅每日早报使用，约 60 credits/月） |
| 推送渠道 | Telegram Bot |
| 定时调度 | APScheduler（内置于 PythonClaw）+ 独立监控守护进程 |
| 个性化数据 | SQLite（`~/.pythonclaw/context/personalization/user_profiles.sqlite3`） |
| 部署 | RackNerd VPS，Ubuntu 24.04，systemd 管理 |

## 监控架构

```
PythonClaw 主进程（jarvis-agent）
└── 每天 09:00 早报：fetch_rate + news_monitor + Tavily 搜索 → DeepSeek 综合分析

monitor_daemon.py（jarvis-monitor）
├── 每 20 分钟：Google News RSS 扫描
│   └── 有新文章 → DeepSeek 判断相关性
│       ├── "影响有限" → 静默跳过
│       └── 有实质影响 → 📰 推送标题 + 3 句分析
├── 每 30 分钟：open.er-api.com 实时汇率
│   └── 波动 > 0.3% → ⚠️ 推送告警（无 LLM）
└── 联合检测：新闻触发 AND 汇率从 48h 高点跌 ≥ 0.8%
    └── 🔴 推送：汇率数据 + 新闻 + DeepSeek 深度分析（2小时冷却）
```

## 月均成本估算

| 调用类型 | 次数/月 | 月费用 |
|---------|---------|--------|
| 每日早报（含 Tavily 2次） | 30 | ~$0.34 USD |
| 新闻相关性分析（过滤后） | ~180 | ~$0.04 USD |
| 联合告警 | ~10 | ~$0.003 USD |
| **合计** | | **~$0.38 USD ≈ $0.60 AUD/月** |

## 目录结构

```
AUDRateAgent/
├── Jarvis/                              # PythonClaw 框架（自定义版本）
│   ├── pythonclaw/
│   │   ├── core/llm/
│   │   │   ├── openai_compatible.py     # DeepSeek/OpenAI provider
│   │   │   └── anthropic_client.py      # Anthropic provider（含兼容性修复）
│   │   ├── core/personalization/
│   │   │   └── user_profile_store.py    # Phase 8 结构化用户资料 SQLite 存储
│   │   ├── channels/telegram_bot.py      # Telegram 命令与消息处理
│   │   └── templates/skills/data/
│   │       └── cnyaud_monitor/          # 核心自定义技能
│   │           ├── SKILL.md             # 技能定义（Agent 自动发现）
│   │           ├── fetch_rate.py        # 实时汇率 + 历史趋势分析
│   │           ├── monitor_alert.py     # 汇率阈值监控
│   │           └── news_monitor.py      # Google News RSS 关键词监控
│   └── monitor_daemon.py                # 独立监控守护进程
├── README.md
└── DEVELOPMENT.md                       # 完整开发记录
```

## 部署

### 1. 依赖安装

```bash
cd Jarvis
python3 -m venv /opt/jarvis-venv
/opt/jarvis-venv/bin/pip install -e .
/opt/jarvis-venv/bin/pip install yfinance pandas numpy openai
```

### 2. 配置文件

创建 `~/.pythonclaw/pythonclaw.json`：

```json
{
  "llm": {
    "provider": "deepseek",
    "deepseek": {
      "apiKey": "sk-...",
      "model": "deepseek-chat"
    }
  },
  "tavily": { "apiKey": "tvly-..." },
  "channels": {
    "telegram": {
      "token": "YOUR_BOT_TOKEN",
      "allowedUsers": [YOUR_TELEGRAM_USER_ID]
    }
  }
}
```

### 3. 技能文件

```bash
mkdir -p ~/.pythonclaw/context/skills/cnyaud_monitor
cp Jarvis/pythonclaw/templates/skills/data/cnyaud_monitor/* \
   ~/.pythonclaw/context/skills/cnyaud_monitor/
```

### 4. Cron 任务

将 `Jarvis/pythonclaw/templates/skills/data/cnyaud_monitor/jobs.example.yaml` 复制到 `~/.pythonclaw/context/cron/jobs.yaml`，填入 Telegram Chat ID。

### 5. 启动（systemd）

```bash
# 创建服务文件后
systemctl enable jarvis-agent jarvis-monitor
systemctl start jarvis-agent jarvis-monitor
```

详细部署流程见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 监控任务说明

| 任务 | 时间 | LLM 调用 | 说明 |
|------|------|---------|------|
| 每日早报 | 墨尔本 09:00 | 是（DeepSeek） | 汇率分析 + Tavily 搜索 |
| 新闻监控 | 每 20 分钟 | 有新文章时（DeepSeek） | 相关性过滤 + 简析 |
| 汇率告警 | 每 30 分钟 | 否 | 波动超阈值才推送 |
| 联合告警 | 实时触发 | 是（DeepSeek） | 新闻 + 汇率双触发 |

## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 显示欢迎信息 |
| `/status` | 查看当前 Agent 会话状态 |
| `/my_profile` | 查看当前结构化个性化资料，不展示 raw logs |
| `/privacy` | 查看 Jarvis 第 8 阶段隐私设计说明 |
| `/delete_profile` | 显示删除影响范围与确认方式 |
| `/delete_profile confirm` | 删除当前 Telegram 用户的结构化个性化数据 |

`/delete_profile confirm` 只删除结构化个性化数据：明确偏好、推断偏好、反馈事件和短期 raw events。它不会删除系统运行日志、Telegram 对话历史或 legacy memory 文件。

## 隐私与个性化

Phase 8 的个性化设计遵循以下原则：

- 不依赖非结构化 LLM memory 存储用户偏好。
- 个性化数据存储在 SQLite 的结构化表中。
- LLM 个性化上下文只接收白名单字段，不接收完整 `MEMORY.md`、daily logs、raw events 或 `history_detail.jsonl`。
- 原始行为事件只作为短期反馈信号，带 TTL 上限并可清理。
- 用户可通过 `/my_profile` 查看资料，通过 `/delete_profile confirm` 删除结构化个性化数据。

当前结构化数据表：

| 表 | 用途 |
|----|------|
| `users` | Telegram 用户 profile 根记录 |
| `explicit_preferences` | 用户明确设置或确认的偏好 |
| `inferred_preferences` | 轻量推断出的内容偏好 |
| `feedback_events` | 有用/无用/不感兴趣等反馈 |
| `raw_events` | 短期原始事件，不进入 LLM 个性化上下文 |

Jarvis 不会主动要求或用于个性化存储银行卡、账户余额、身份证/护照、确切地址或详细个人财务压力等敏感信息；检测到这类内容时会尽量拒绝写入个性化资料。

## 数据来源声明

- 汇率数据仅供参考，存在一定延迟，不构成投资建议
- 新闻来源为 Google News RSS，内容由第三方媒体提供
- 所有分析结果由 AI 生成，不构成专业金融建议
