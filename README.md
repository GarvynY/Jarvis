# AUD Rate Agent

一个基于 [PythonClaw](https://github.com/agentic-ai/pythonclaw) 开发的专属汇率监控 AI Agent，通过 Telegram Bot 实时追踪人民币/澳元（CNY/AUD）汇率，结合中东地缘政治新闻（美伊局势）和澳洲经济基本面进行综合分析与预测。

## 功能

- **每日早报**：每天墨尔本时间 09:00 自动推送 CNY/AUD 汇率分析 + 短期预测
- **实时汇率告警**：每 30 分钟检测汇率波动，超过阈值（默认 ±0.3%）立即推送 Telegram
- **中东突发新闻监控**：每 20 分钟通过 Google News RSS 扫描关键词（霍尔木兹、美伊停火等），有新消息立即推送
- **Telegram 对话**：可直接向 Bot 提问汇率走势、换汇建议、新闻分析等

## 技术架构

| 模块 | 说明 |
|------|------|
| AI 框架 | [PythonClaw](https://github.com/agentic-ai/pythonclaw) — 自主 AI Agent 框架 |
| LLM | Claude Haiku（Anthropic） |
| 实时汇率 | open.er-api.com（免费，无需 API Key） |
| 历史数据 | Yahoo Finance via yfinance |
| 新闻监控 | Google News RSS（免费，无需 API Key，零 Tavily 消耗） |
| 深度新闻搜索 | Tavily（仅用于每日早报，约 60 credits/月） |
| 推送渠道 | Telegram Bot |
| 定时调度 | APScheduler（内置于 PythonClaw） |

## 目录结构

```
AUDRateAgent/
├── PythonClaw/                          # PythonClaw 框架（含自定义扩展）
│   └── pythonclaw/
│       └── templates/skills/data/
│           └── cnyaud_monitor/          # 核心自定义技能
│               ├── SKILL.md             # 技能定义（Agent 自动发现）
│               ├── fetch_rate.py        # 实时汇率 + 历史趋势分析
│               ├── monitor_alert.py     # 汇率阈值监控
│               └── news_monitor.py      # Google News RSS 关键词监控
└── README.md
```

## 部署

### 1. 依赖安装

```bash
cd PythonClaw
pip install -e .
```

### 2. 配置文件

创建 `~/.pythonclaw/pythonclaw.json`：

```json
{
  "llm": {
    "provider": "claude",
    "claude": {
      "apiKey": "sk-ant-...",
      "model": "claude-haiku-4-5-20251001"
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

### 3. Cron 任务

将 `PythonClaw/pythonclaw/templates/skills/data/cnyaud_monitor/jobs.example.yaml` 复制到 `~/.pythonclaw/context/cron/jobs.yaml`，填入你的 Telegram Chat ID。

### 4. 启动

```bash
pythonclaw start
```

## Cron 任务说明

| 任务 | 时间 | Tavily 消耗 | 说明 |
|------|------|------------|------|
| 每日早报 | 墨尔本 09:00 | 2 次/天 | 汇率分析 + 预测报告 |
| 汇率阈值告警 | 每 30 分钟 | 0 | 波动超阈值才推送 |
| 中东新闻告警 | 每 20 分钟 | 0 | RSS 扫描，有新文章才推送 |

月均 Tavily 消耗约 60 credits（1000 credits/月额度）。

## 数据来源声明

- 汇率数据仅供参考，存在一定延迟，不构成投资建议
- 新闻来源为 Google News RSS，内容由第三方媒体提供
- 所有分析结果由 AI 生成，不构成专业金融建议
