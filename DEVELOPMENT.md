# Jarvis — 开发全记录

基于 PythonClaw 框架开发的 CNY/AUD 汇率监控 AI Agent，从零到云端部署的完整流程。

---

## 一、需求分析

**目标**：构建一个自动化汇率监控机器人，通过 Telegram 推送：
- 每日早报：结合中东地缘政治（美伊局势）+ 澳洲经济基本面的 CNY/AUD 分析
- 实时告警：汇率波动超阈值时推送
- 新闻监控：关键词（霍尔木兹、美伊停火等）出现时推送

**约束**：
- Tavily API 仅 1000 credits/月，需严格控制
- Anthropic API 按 token 计费，监控类任务不能频繁调 LLM
- 代码开源但密钥不能提交 git

---

## 二、技术选型

| 组件 | 选择 | 原因 |
|------|------|------|
| AI 框架 | PythonClaw | 支持技能系统、Cron 调度、Telegram 多通道 |
| LLM | DeepSeek (`deepseek-chat`) | 比 Claude Haiku 便宜 3-5 倍，OpenAI 兼容接口 |
| 实时汇率 | open.er-api.com | 免费、~1 分钟延迟、无需 API Key |
| 历史数据 | yfinance (`CNYAUD=X`) | 免费日线数据 |
| 新闻监控 | Google News RSS | 免费、无需 API Key、零 Tavily 消耗 |
| 深度搜索 | Tavily | 仅早报使用，约 60 credits/月 |
| 推送渠道 | Telegram Bot | PythonClaw 原生支持 |

---

## 三、项目结构

```
AUDRateAgent/                        # Git 仓库根目录
├── Jarvis/                          # PythonClaw 框架（自定义版本）
│   ├── pythonclaw/
│   │   ├── core/
│   │   │   └── llm/
│   │   │       ├── anthropic_client.py   # Anthropic provider（已修复多处 bug）
│   │   │       ├── openai_compatible.py  # DeepSeek/OpenAI provider
│   │   │       └── base.py
│   │   ├── templates/skills/data/
│   │   │   └── cnyaud_monitor/      # 核心技能（同步到 ~/.pythonclaw/context/skills/）
│   │   │       ├── SKILL.md         # 技能定义（Agent 自动发现）
│   │   │       ├── fetch_rate.py    # 实时汇率 + 历史趋势
│   │   │       ├── monitor_alert.py # 阈值告警
│   │   │       └── news_monitor.py  # Google News RSS 关键词监控
│   │   ├── daemon.py                # Windows 兼容性修复
│   │   ├── main.py                  # 入口 + LLM provider 工厂
│   │   └── web/app.py               # FastAPI（WebSocket 方法名修复）
│   └── monitor_daemon.py            # 独立监控守护进程（零 LLM 常态监控）
├── README.md
└── DEVELOPMENT.md                   # 本文档

~/.pythonclaw/                       # 运行时配置（不进 git，含密钥）
├── pythonclaw.json                  # LLM / Telegram / Tavily 密钥
└── context/
    ├── cron/jobs.yaml               # 定时任务
    ├── soul/SOUL.md                 # Bot 人设
    ├── persona/persona.md           # Bot 行为规范
    └── skills/cnyaud_monitor/       # 技能脚本副本（sandbox 可访问路径）
```

---

## 四、核心功能开发

### 4.1 技能系统

PythonClaw 的技能通过 `SKILL.md` 定义，Agent 自动发现并调用。

**`fetch_rate.py`**
- 主数据源：`open.er-api.com/v6/latest/CNY`（免费实时）
- 备用：yfinance `CNYAUD=X`（仅历史）
- 输出：当前汇率双向展示 + 90 天统计（涨跌幅、高低点、波动率、线性回归）

**`monitor_alert.py`**
- 对比保存的基线汇率（`~/.pythonclaw/context/cnyaud_state.json`）
- 超过阈值返回 `alert: true` + JSON 告警数据

**`news_monitor.py`**
- Google News RSS 关键词组扫描
- 状态文件去重（`~/.pythonclaw/context/news_monitor_state.json`）
- `--no-mark-seen` 参数供早报干跑使用（不影响实时告警去重）
- 返回 `has_breaking: bool` + 新文章列表

### 4.2 Cron 调度

`~/.pythonclaw/context/cron/jobs.yaml` 配置定时任务，由 APScheduler 驱动：

| 任务 | 触发时间 | 说明 |
|------|---------|------|
| `cnyaud_morning_report` | UTC 23:00（墨尔本 09:00） | 早报，使用 Tavily 2 次 |
| `mideast_news_alert` | ~~每 20 分钟~~ 已禁用 | 迁移到 monitor_daemon |
| `cnyaud_realtime_alert` | ~~每 30 分钟~~ 已禁用 | 迁移到 monitor_daemon |

### 4.3 Bot 人设

通过 Soul + Persona 文件将默认 PythonClaw 行为改造为专属 Jarvis：

- `soul/SOUL.md`：定义核心职责和准确性优先原则
- `persona/persona.md`：规定名称、回复语言（中文）、固定报价格式、拒绝非汇率话题

---

## 五、架构演进：成本优化

### 阶段一：初始架构（问题：API 消耗过快）

```
mideast_news_alert（每20分钟） ──→ LLM 判断 ──→ Telegram
cnyaud_realtime_alert（每30分钟）──→ LLM 判断 ──→ Telegram
cnyaud_morning_report（每天）   ──→ LLM 分析 ──→ Telegram

日均 LLM 调用：121 次
日均成本（Haiku）：~$3 AUD
```

**问题根因**：监控任务 99% 的结果是"无告警"，但每次都走完整 LLM 推理。

### 阶段二：独立监控守护进程（`monitor_daemon.py`）

```
monitor_daemon.py（独立 Python 进程）
├── 每30分钟：直接运行 monitor_alert.py → 解析 JSON → if alert: 发 Telegram
├── 每20分钟：直接运行 news_monitor.py  → 解析 JSON → if breaking: 发 Telegram
└── 零 LLM 调用

cnyaud_morning_report（每天）──→ LLM 分析 ──→ Telegram（保留）

日均 LLM 调用：1 次
```

### 阶段三：联合告警（精准 LLM 调用）

```
触发条件：突发新闻 AND 汇率从48小时最高点下跌 ≥ 0.8%

monitor_daemon.py
├── 常态监控：零 LLM
├── 单项告警（汇率/新闻）：零 LLM，发模板消息
└── 联合告警：调用 DeepSeek 生成3句中文分析
    ├── 2小时冷却避免重复推送
    └── 日均触发 0-2 次

月均成本（DeepSeek）：< $1 AUD
```

### 阶段四：切换 LLM 至 DeepSeek

PythonClaw `main.py` 原生支持 DeepSeek（OpenAI 兼容接口），只需修改配置：

```json
// ~/.pythonclaw/pythonclaw.json
{
  "llm": {
    "provider": "deepseek",
    "deepseek": {
      "apiKey": "sk-xxx",
      "model": "deepseek-chat"
    }
  }
}
```

---

## 六、关键 Bug 修复记录

### Bug 1：GitHub 仓库文件夹无法点开

**现象**：PythonClaw 文件夹在 GitHub 显示为灰色，点击无效。  
**原因**：该目录下有 `.git`，被识别为未注册的 git submodule。  
**修复**：
```bash
git rm --cached PythonClaw
rm -rf PythonClaw/.git
git add PythonClaw/
git push
```

### Bug 2：FastAPI 启动报错

**现象**：`'FastAPI' object has no attribute 'add_websocket_route'`  
**原因**：新版 FastAPI 将方法重命名。  
**修复**：`web/app.py` 中改为 `add_api_websocket_route`。

### Bug 3：Windows 守护进程崩溃

**现象**：`SystemError: WinError 87` / `OSError`  
**原因**：`os.kill(pid, 0)` 在 Windows 对已死进程行为不同。  
**修复**：`daemon.py` 中捕获 `Exception`，停止时用 `taskkill` 替代 SIGTERM。

### Bug 4：Anthropic API 400 错误（assistant content 格式）

**现象**：`messages.7.content: Input should be a valid list`  
**原因**：新版 Anthropic SDK 要求 assistant 消息的 `content` 必须是 list，不能是字符串。  
**修复**：`anthropic_client.py` `_prepare_request` 中添加分支，将字符串内容包装为 `[{"type": "text", "text": content}]`。

### Bug 5：Anthropic API 400 错误（_ts 字段）

**现象**：`messages.0._ts: Extra inputs are not permitted`  
**原因**：`SessionStore` 恢复历史消息时注入 `_ts` 时间戳字段，被原样传给 API。  
**修复**：`_prepare_request` 开头过滤内部字段：
```python
_INTERNAL_KEYS = {"_ts"}
messages = [{k: v for k, v in m.items() if k not in _INTERNAL_KEYS} for m in messages]
```

### Bug 6：汇率数据不准确

**现象**：yfinance 返回 `1 AUD = 5 CNY`，实际约 4.89。  
**原因**：yfinance 的 `CNYAUD=X` 数据有延迟且精度差。  
**修复**：实时汇率改用 `open.er-api.com`（免费，~1 分钟延迟），yfinance 仅保留历史日线。

### Bug 7：技能脚本路径解析失败

**现象**：Cron 任务报告找不到 `news_monitor.py`。  
**原因**：项目目录从 `PythonClaw` 改名为 `Jarvis` 后，技能路径在 sandbox 内解析失败。  
**修复**：将技能文件复制到 `~/.pythonclaw/context/skills/cnyaud_monitor/`（始终在 sandbox 内）：
```bash
cp Jarvis/pythonclaw/templates/skills/data/cnyaud_monitor/* \
   ~/.pythonclaw/context/skills/cnyaud_monitor/
```

---

## 七、云端部署（RackNerd VPS）

### 环境信息

| 项目 | 值 |
|------|-----|
| 服务器 | RackNerd VPS |
| OS | Ubuntu 24.04 LTS |
| Python | 3.12 |
| 虚拟环境 | `/opt/jarvis-venv/` |
| 代码路径 | `/opt/Jarvis/` |
| 配置路径 | `/root/.pythonclaw/` |

### 部署步骤概览

```
1. 购买 VPS → 获取 IP、用户名、密码
2. 加 Swap（内存不足时）
3. apt install git python3-venv python3-pip
4. 本地打包代码 → scp 上传 → 解压到 /opt/Jarvis/
5. 创建虚拟环境 + pip install
6. 手动创建 ~/.pythonclaw/ 配置文件（含密钥，不在 git 中）
7. 创建两个 systemd 服务
8. systemctl enable + start
9. 停止本地实例（避免 Telegram bot 冲突）
```

详细步骤见 `DEPLOYMENT.md`（迁移 Workflow 文档）。

### systemd 服务

**`jarvis-agent.service`**：PythonClaw 主进程（Telegram 对话 + 早报）  
**`jarvis-monitor.service`**：独立监控守护进程（汇率/新闻/联合告警）

```bash
# 常用运维命令
systemctl status jarvis-agent jarvis-monitor   # 查看状态
systemctl restart jarvis-agent jarvis-monitor  # 重启（更新代码后）
tail -f /root/.pythonclaw/pythonclaw.log        # 实时日志
tail -f /root/.pythonclaw/monitor_daemon.log
```

### 更新代码流程

```bash
# 本地
cd /项目根目录
tar czf deploy.tar.gz Jarvis --exclude='*/__pycache__' --exclude='*.pyc'
scp deploy.tar.gz root@<IP>:/root/

# 服务器
ssh root@<IP>
cd /opt && tar xzf /root/deploy.tar.gz
/opt/jarvis-venv/bin/pip install -q -e /opt/Jarvis/.
systemctl restart jarvis-agent jarvis-monitor
```

---

## 八、成本对比

| 阶段 | 架构 | 日均 LLM 调用 | 月费用估算 |
|------|------|-------------|-----------|
| 初始 | 全部走 LLM Cron | 121 次 | ~$90 AUD（Haiku） |
| 优化后 | monitor_daemon + LLM 仅早报 | 1 次 | ~$2-3 AUD（Haiku） |
| 切换 DeepSeek | 同上 | 1 次 | **< $1 AUD** |

DeepSeek 定价（参考）：
- Input cache hit：$0.028 / 1M tokens
- Input cache miss：$0.28 / 1M tokens
- Output：$0.42 / 1M tokens

---

## 九、安全事项

- 所有 API Key 存放在 `~/.pythonclaw/pythonclaw.json`，不进 git
- `.gitignore` 已排除：`.venv/`、`__pycache__/`、`CLAUDE.md`、`.idea/`、`jarvis_deploy.tar.gz`
- 服务器密码建议部署完成后修改（`passwd root`）
- 建议配置 SSH Key 登录并禁用密码登录（`PasswordAuthentication no`）
