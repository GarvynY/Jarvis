# Jarvis — 开发全记录

基于 PythonClaw 框架开发的 CNY/AUD 汇率监控 AI Agent，从零到云端部署的完整流程。

---

## 一、需求分析

**目标**：构建一个自动化汇率监控机器人，通过 Telegram 推送：
- 每日早报：结合中东地缘政治（美伊局势）+ 澳洲经济基本面的 CNY/AUD 分析
- 实时告警：汇率波动超阈值时推送
- 新闻监控：关键词（霍尔木兹、美伊停火等）出现时推送并分析影响

**约束**：
- Tavily API 仅 1000 credits/月，需严格控制
- LLM 按 token 计费，监控类任务不能无差别调用
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
| 部署 | RackNerd VPS + systemd | 低成本 24/7 运行 |

---

## 三、项目结构

```
AUDRateAgent/                        # Git 仓库根目录
├── Jarvis/                          # PythonClaw 框架（自定义版本）
│   ├── pythonclaw/
│   │   ├── core/
│   │   │   ├── llm/
│   │   │   │   ├── anthropic_client.py   # Anthropic provider（含多处兼容性修复）
│   │   │   │   ├── openai_compatible.py  # DeepSeek/OpenAI provider
│   │   │   │   └── base.py
│   │   │   ├── memory/                   # legacy memory，Phase 8 后默认不写入个性化偏好
│   │   │   └── personalization/
│   │   │       └── user_profile_store.py # SQLite 结构化用户资料存储
│   │   ├── channels/
│   │   │   └── telegram_bot.py           # Telegram 命令与消息入口
│   │   ├── templates/skills/data/
│   │   │   └── cnyaud_monitor/      # 核心技能（同步到 ~/.pythonclaw/context/skills/）
│   │   │       ├── SKILL.md         # 技能定义（Agent 自动发现）
│   │   │       ├── fetch_rate.py    # 实时汇率 + 历史趋势
│   │   │       ├── monitor_alert.py # 阈值告警
│   │   │       └── news_monitor.py  # Google News RSS 关键词监控
│   │   ├── daemon.py                # Windows 兼容性修复
│   │   ├── main.py                  # 入口 + LLM provider 工厂
│   │   └── web/app.py               # FastAPI（WebSocket 方法名修复）
│   └── monitor_daemon.py            # 独立监控守护进程（含新闻 LLM 分析）
├── README.md
└── DEVELOPMENT.md                   # 本文档

~/.pythonclaw/                       # 运行时配置（不进 git，含密钥）
├── pythonclaw.json                  # LLM / Telegram / Tavily 密钥
└── context/
    ├── cron/jobs.yaml               # 定时任务
    ├── soul/SOUL.md                 # Bot 人设
    ├── persona/persona.md           # Bot 行为规范
    ├── personalization/
    │   └── user_profiles.sqlite3    # Phase 8 结构化个性化数据
    └── skills/cnyaud_monitor/       # 技能脚本副本（sandbox 可访问路径）
```

---

## 四、核心功能开发

### 4.1 技能系统

PythonClaw 的技能通过 `SKILL.md` 定义，Agent 自动发现并调用。

**`fetch_rate.py`**
- 主数据源：`open.er-api.com/v6/latest/CNY`（免费实时，~1 分钟延迟）
- 备用：yfinance `CNYAUD=X`（仅历史日线）
- 输出：当前汇率双向展示 + 90 天统计（涨跌幅、高低点、波动率、线性回归）

**`monitor_alert.py`**
- 对比保存的基线汇率（`~/.pythonclaw/context/cnyaud_state.json`）
- 超过阈值返回 `alert: true` + JSON 告警数据，exit code 1

**`news_monitor.py`**
- Google News RSS 关键词组扫描（霍尔木兹、美伊停火、RBA 利率等）
- 状态文件去重，保留最近 500 条 URL（`~/.pythonclaw/context/news_monitor_state.json`）
- `--no-mark-seen` 参数供早报干跑使用，不影响实时告警去重
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

- `soul/SOUL.md`：定义核心职责（CNY/AUD 专家）和准确性优先原则
- `persona/persona.md`：规定名称（Jarvis）、中文回复、固定报价格式、拒绝非汇率话题

---

## 五、架构演进：成本优化

### 阶段一：初始架构（问题：成本失控）

```
mideast_news_alert（每20分钟） ──→ LLM 判断 ──→ Telegram
cnyaud_realtime_alert（每30分钟）──→ LLM 判断 ──→ Telegram
cnyaud_morning_report（每天）   ──→ LLM 分析 ──→ Telegram

日均 LLM 调用：121 次
实际测试成本：~$5 AUD/晚（Claude Haiku）
```

**根本原因**：监控任务 99% 结果是"无告警"，但每次都走完整 LLM 推理。

---

### 阶段二：抽离独立监控守护进程

将监控逻辑从 PythonClaw Cron（走 LLM）迁移到独立 Python 进程，直接调脚本解析 JSON：

```
monitor_daemon.py
├── 每30分钟：monitor_alert.py → 解析 JSON → if alert: 发固定模板
├── 每20分钟：news_monitor.py  → 解析 JSON → if breaking: 发固定模板
└── 零 LLM 调用

cnyaud_morning_report（每天）──→ LLM 分析（保留）

日均 LLM 调用：1 次
```

---

### 阶段三：联合告警（首次引入精准 LLM）

```
触发条件：突发新闻 AND 汇率从 48 小时最高点下跌 ≥ 0.8%

monitor_daemon.py
├── 常态监控：零 LLM，固定模板告警
└── 联合告警：调用 DeepSeek，含汇率 + 新闻上下文
    ├── 2 小时冷却防重复
    └── 日均触发 0-2 次
```

---

### 阶段四：切换 LLM 至 DeepSeek

PythonClaw `main.py` 原生支持 DeepSeek，只需改配置，无需动代码：

```json
{
  "llm": {
    "provider": "deepseek",
    "deepseek": { "apiKey": "sk-...", "model": "deepseek-chat" }
  }
}
```

DeepSeek 定价较 Claude Haiku 便宜 3-5 倍，`monitor_daemon.py` 内的直接 API 调用也同步换成 `openai.OpenAI(base_url="https://api.deepseek.com/v1")`。

---

### 阶段五：新闻 LLM 相关性过滤 + 分析

每次新闻触发不再发固定模板，改为先让 DeepSeek 判断相关性：

```
每 20 分钟 RSS 扫描
    │
    ├─ 无新文章 → 静默
    │
    └─ 有新文章 → DeepSeek 判断
                    │
                    ├─ "影响有限，无需关注"
                    │   → 静默跳过（历史回顾、无关事件）
                    │
                    └─ 有实质影响
                        → 📰 标题 + 3 句分析

同时：新闻触发 AND 汇率从 48h 高点跌 ≥ 0.8%
    → 🔴 联合告警（含汇率上下文的增强分析，跳过相关性过滤）
```

**Prompt 关键设计**：

```
- 如果新闻与 CNY/AUD 汇率关联不明显，只回复'影响有限，无需关注'
- 如果有实质影响，用3句话分析：驱动逻辑、方向判断、换汇建议
```

---

### 阶段八准备：隐私防火墙 + 结构化个性化

Phase 8 的目标是加入隐私感知个性化，但不依赖 legacy Markdown memory 或非结构化 LLM memory。准备工作先完成“隐私防火墙”，再加入结构化 SQLite 数据层和 Telegram 管理命令。

核心原则：

- 用户个性化数据必须存储在结构化字段中。
- LLM 个性化上下文只接收安全白名单字段。
- 原始日志、每日日志、工具结果、完整 `MEMORY.md`、`history_detail.jsonl` 不进入 LLM prompt。
- 原始行为事件只作为短期反馈信号，不能长期保留或被 LLM 工具读取。
- 用户必须能够查看和删除结构化个性化数据。

#### 1. Legacy memory 写入开关

新增配置：

```json
{
  "personalization": {
    "legacyMemoryWriteEnabled": false
  }
}
```

默认关闭旧版 LLM 驱动的“事实/决策/偏好”自动提取和写入，避免继续把用户偏好写入 `MEMORY.md` 或 daily memory。

影响路径：

- `pythonclaw/core/agent.py`
- `pythonclaw/core/compaction.py`
- `pythonclaw/core/memory/storage.py`

#### 2. Safe boot context

`MemoryManager.boot_context()` 已重构为安全上下文入口，只返回白名单字段或默认空上下文，不再注入 daily logs、raw interactions、tool results、完整 `MEMORY.md` 或 `history_detail.jsonl`。

安全字段范围：

- language
- tone
- target_rate
- alert_threshold
- report_time
- risk preference label
- preferred_summary_style

#### 3. 工具访问限制

`pythonclaw/core/tools.py` 增加文件与 memory 工具沙箱：

- `read_file`、`send_file` 只允许读取安全白名单路径。
- 阻止 `.env`、`pythonclaw.json`、secret/key/token 文件、daily memory logs、`history_detail.jsonl`。
- `memory_get` 不再允许直接读取 `YYYY-MM-DD` daily logs。
- `recall("*")` 不再 dump legacy memory，改为安全个性化上下文。
- `run_command` 默认不暴露给 LLM；只允许本地可信调试显式开启。
- `list_files` 也套用 readable allowlist，避免泄露目录结构。

#### 4. Web memory/config/identity API 加固

`pythonclaw/web/app.py` 默认禁用 raw memory API，并保护敏感 dashboard API：

- `/api/memories` 默认禁用，不返回完整 raw memory。
- raw memory export 需要 `web.enableRawMemoryApi=true` 且通过 admin token。
- `/api/config`、`/api/identity`、`/api/memory/index` 需要 admin token。
- admin token 只从环境变量 `JARVIS_WEB_ADMIN_TOKEN` 读取，不通过 config UI 管理。
- 默认 Web host 建议绑定 `127.0.0.1`；若服务器绑定 `0.0.0.0`，敏感 API 仍需 token。

#### 5. SQLite 用户资料数据层

新增模块：

```text
pythonclaw/core/personalization/user_profile_store.py
```

默认数据库：

```text
~/.pythonclaw/context/personalization/user_profiles.sqlite3
```

表结构：

| 表 | 说明 |
|----|------|
| `users` | Telegram 用户根记录 |
| `explicit_preferences` | 明确偏好：语言、目标汇率、提醒阈值、用途、摘要风格、主题、隐私级别等 |
| `inferred_preferences` | 推断出的内容偏好：高/低兴趣话题、confidence |
| `feedback_events` | 有用、无用、不感兴趣等反馈 |
| `raw_events` | 短期原始事件，带 `expires_at`，不通过 profile API 返回 |

公开函数：

```python
init_db()
get_or_create_user(telegram_user_id)
get_user_profile(telegram_user_id)
update_explicit_preferences(telegram_user_id, updates)
update_inferred_preferences(telegram_user_id, updates)
delete_user_profile(telegram_user_id)
log_feedback_event(...)
log_raw_event(...)
purge_expired_raw_events()
```

隐私加固：

- 检查敏感 key 和字符串 value。
- 拦截银行卡/账户余额/身份证/护照/确切地址/详细财务压力等字段或内容。
- raw event payload 限制 16KB。
- raw event TTL 上限 14 天。
- 删除 profile 后执行 WAL checkpoint + VACUUM，减少物理残留。

#### 6. Telegram 个性化命令

新增命令：

| 命令 | 行为 |
|------|------|
| `/privacy` | 中文解释 Phase 8 数据分类、敏感信息处理和 LLM 个性化上下文边界 |
| `/my_profile` | 查看当前用户结构化 profile；不存在则创建默认 profile |
| `/update_profile` | 进入问答式流程，逐项更新明确偏好，不更新推断偏好 |
| `/update_profile 目标汇率=4.85 提醒阈值=0.3 用途=学费 风格=简短 主题=RBA,oil,CNY` | 高级用法：一次性更新明确偏好 |
| `/delete_profile` | 显示删除影响范围和确认方式 |
| `确定` | 在 `/delete_profile` 后确认删除当前 Telegram 用户的结构化个性化数据 |

新用户首次私聊 Jarvis，或 `users.onboarding_completed = 0` 时，会进入轻量 onboarding：

1. 用途：学费 / 生活费 / 投资 / 其他
2. 提醒偏好：目标汇率 / 波动率 / 重大新闻 / 晨报
3. 摘要风格：简短 / 普通 / 详细

回答保存到 `explicit_preferences`，完成状态保存在 `users.onboarding_completed` 和 `users.onboarding_completed_at`。用户可回复 `跳过引导` 使用默认 profile，跳过后不会反复询问。

`/delete_profile` 后回复 `确定` 的删除范围：

- explicit preferences
- inferred preferences
- feedback events
- raw events
- users 表中当前 Telegram user 对应的 profile row

不删除：

- Telegram 对话历史
- systemd/应用运行日志
- legacy `MEMORY.md`
- unrelated system files

#### 7. Phase 8 手动测试清单

服务状态：

```bash
systemctl status jarvis-agent jarvis-monitor
tail -80 ~/.pythonclaw/pythonclaw.log
tail -80 ~/.pythonclaw/monitor_daemon.log
```

Telegram：

```text
/start
学费
重大新闻
简短
/privacy
/my_profile
/update_profile
4.85
0.3
学费
简短
RBA，oil，CNY
zh-CN
标准
/update_profile 目标汇率=4.85 提醒阈值=0.3 用途=学费 风格=简短 主题=RBA,oil,CNY
/delete_profile
确定
/my_profile
```

问答式更新中可回复 `跳过` 跳过当前字段，或回复 `取消` 退出本次修改。

SQLite 检查：

```bash
sqlite3 ~/.pythonclaw/context/personalization/user_profiles.sqlite3
```

```sql
.tables
SELECT * FROM users;
SELECT * FROM explicit_preferences;
SELECT * FROM inferred_preferences;
SELECT * FROM feedback_events;
SELECT * FROM raw_events;
```

如服务器没有 `sqlite3` CLI，可使用 Python 内置 sqlite3：

```bash
cd /opt/Jarvis
/opt/jarvis-venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path

db = Path.home() / ".pythonclaw/context/personalization/user_profiles.sqlite3"
conn = sqlite3.connect(db)
for table in ["users", "explicit_preferences", "inferred_preferences", "feedback_events", "raw_events"]:
    print(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
conn.close()
PY
```

删除闭环通过标准：

- `/my_profile` 不展示 raw logs 或 JSON dump。
- `/delete_profile` 不会立即删除，必须再回复 `确定`。
- 删除后相关 profile 表记录被清空或当前用户相关行消失。
- 删除后 `/my_profile` 显示默认/暂无偏好状态。
- `/reset` 后再询问旧目标汇率，Jarvis 不应继续记得已删除偏好。

#### 8. 部署 checkpoint

2026-04-29 已部署到 RackNerd：

- `/opt/Jarvis`
- `jarvis-agent.service`
- `jarvis-monitor.service`

部署前现有 `jarvis-agent` 因 FastAPI `Request | None` 路由参数报错重启；已修复为 `_build_status(request=None)` + `_api_status(request: Request)`，部署后 Telegram bot 正常注册 8 个命令。

---

## 六、成本对比

| 阶段 | 架构 | 日均 LLM 调用 | 月费用估算 |
|------|------|-------------|-----------|
| 初始 | 全部走 PythonClaw Cron | 121 次 | ~$90 AUD（Haiku） |
| 阶段二 | monitor_daemon，仅早报用 LLM | 1 次 | ~$2-3 AUD（Haiku） |
| 阶段四 | 同上，切 DeepSeek | 1 次 | ~$0.5 AUD |
| 阶段五 | 新闻过滤分析 + 联合告警 | ~7-8 次 | ~$0.6 AUD |
| **阶段八准备（当前）** | 在阶段五基础上加入隐私防火墙 + 结构化 profile | 取决于用户交互 | 监控成本不变 |

DeepSeek 定价（参考）：
- Input cache hit：$0.028 / 1M tokens
- Input cache miss：$0.28 / 1M tokens
- Output：$0.42 / 1M tokens

---

## 七、关键 Bug 修复记录

### Bug 1：GitHub 仓库文件夹无法点开

**现象**：PythonClaw 文件夹在 GitHub 显示为灰色，点击无效。  
**原因**：该目录下有 `.git`，被识别为未注册的 git submodule。  
**修复**：
```bash
git rm --cached PythonClaw
rm -rf PythonClaw/.git
git add PythonClaw/ && git push
```

### Bug 2：FastAPI 启动报错

**现象**：`'FastAPI' object has no attribute 'add_websocket_route'`  
**原因**：新版 FastAPI 将方法重命名。  
**修复**：`web/app.py` 改为 `add_api_websocket_route`。

### Bug 3：Windows 守护进程崩溃

**现象**：`SystemError: WinError 87` / `OSError`  
**原因**：`os.kill(pid, 0)` 在 Windows 对已死进程行为不同。  
**修复**：`daemon.py` 捕获 `Exception`，停止时用 `taskkill` 替代 SIGTERM。

### Bug 4：Anthropic API 400（assistant content 格式）

**现象**：`messages.7.content: Input should be a valid list`  
**原因**：新版 Anthropic SDK 要求 assistant 消息 `content` 必须是 list。  
**修复**：`anthropic_client.py` 将字符串内容包装为 `[{"type": "text", "text": content}]`。

### Bug 5：Anthropic API 400（_ts 字段）

**现象**：`messages.0._ts: Extra inputs are not permitted`  
**原因**：`SessionStore` 恢复历史消息时注入 `_ts` 字段，被原样传给 API。  
**修复**：`_prepare_request` 开头过滤：
```python
_INTERNAL_KEYS = {"_ts"}
messages = [{k: v for k, v in m.items() if k not in _INTERNAL_KEYS} for m in messages]
```

### Bug 6：汇率数据不准确

**现象**：yfinance 返回 `1 AUD ≈ 5 CNY`，实际约 4.89。  
**原因**：yfinance `CNYAUD=X` 数据有延迟且精度差。  
**修复**：实时汇率改用 `open.er-api.com`，yfinance 仅保留历史日线。

### Bug 7：技能脚本路径解析失败

**现象**：Cron 任务报告找不到 `news_monitor.py`。  
**原因**：项目目录从 `PythonClaw` 改名为 `Jarvis` 后，技能路径在 sandbox 内解析失败。  
**修复**：将技能文件复制到 `~/.pythonclaw/context/skills/cnyaud_monitor/`。

### Bug 8：FastAPI Request 可选类型导致启动失败

**现象**：服务器 `jarvis-agent.service` 持续重启，日志显示：

```text
fastapi.exceptions.FastAPIError: Invalid args for response field
starlette.requests.Request | None is a valid Pydantic field type
```

**原因**：`/api/status` 路由函数参数写成 `request: Request | None = None`，当前 FastAPI/Pydantic 版本会尝试把该 union 当成 response/dependency 字段处理。

**修复**：

- 拆出 `_build_status(request: Request | None = None)` 作为内部 helper。
- FastAPI 路由函数改为 `_api_status(request: Request)`。
- WebSocket `/status` 使用 `_build_status()`。

---

## 八、云端部署（RackNerd VPS）

### 环境信息

| 项目 | 值 |
|------|-----|
| 服务器 | RackNerd VPS，172.245.147.100 |
| OS | Ubuntu 24.04 LTS |
| Python | 3.12 |
| 虚拟环境 | `/opt/jarvis-venv/` |
| 代码路径 | `/opt/Jarvis/` |
| 配置路径 | `/root/.pythonclaw/` |

### 部署步骤

```bash
# 1. 服务器初始化
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo "/swapfile none swap sw 0 0" >> /etc/fstab
apt update && apt install -y python3-venv python3-pip

# 2. 上传代码（仓库为私有，用 scp 直接传）
# 本地打包
tar czf deploy.tar.gz Jarvis --exclude='*/__pycache__' --exclude='*.pyc' --exclude='*.egg-info'
scp deploy.tar.gz root@<IP>:/root/
# 服务器解压
ssh root@<IP> "cd /opt && tar xzf /root/deploy.tar.gz"

# 3. 安装依赖
python3 -m venv /opt/jarvis-venv
/opt/jarvis-venv/bin/pip install -e /opt/Jarvis/.
/opt/jarvis-venv/bin/pip install yfinance pandas numpy openai

# 4. 创建配置目录
mkdir -p /root/.pythonclaw/context/{cron,soul,persona,skills/cnyaud_monitor}
# 手动创建 pythonclaw.json、jobs.yaml、SOUL.md、persona.md
cp /opt/Jarvis/pythonclaw/templates/skills/data/cnyaud_monitor/* \
   /root/.pythonclaw/context/skills/cnyaud_monitor/

# 5. 创建 systemd 服务（见下方）
systemctl daemon-reload
systemctl enable jarvis-agent jarvis-monitor
systemctl start jarvis-agent && sleep 5 && systemctl start jarvis-monitor
```

### systemd 服务文件

**`/etc/systemd/system/jarvis-agent.service`**
```ini
[Unit]
Description=Jarvis PythonClaw Agent
After=network-online.target

[Service]
User=root
WorkingDirectory=/opt/Jarvis
ExecStart=/opt/jarvis-venv/bin/pythonclaw start -f
Restart=always
RestartSec=15
StandardOutput=append:/root/.pythonclaw/pythonclaw.log
StandardError=append:/root/.pythonclaw/pythonclaw.log

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/jarvis-monitor.service`**
```ini
[Unit]
Description=Jarvis Monitor Daemon
After=network-online.target

[Service]
User=root
WorkingDirectory=/opt/Jarvis
ExecStart=/opt/jarvis-venv/bin/python monitor_daemon.py
Restart=always
RestartSec=15
StandardOutput=append:/root/.pythonclaw/monitor_daemon.log
StandardError=append:/root/.pythonclaw/monitor_daemon.log

[Install]
WantedBy=multi-user.target
```

### 常用运维命令

```bash
# 服务状态
systemctl status jarvis-agent jarvis-monitor

# 重启（更新代码后）
systemctl restart jarvis-agent jarvis-monitor

# 实时日志
tail -f /root/.pythonclaw/pythonclaw.log
tail -f /root/.pythonclaw/monitor_daemon.log
```

### 更新代码流程

```bash
# 本地打包上传
tar czf deploy.tar.gz Jarvis --exclude='*/__pycache__' --exclude='*.pyc'
scp deploy.tar.gz root@172.245.147.100:/root/

# 服务器更新
ssh root@172.245.147.100
cd /opt && tar xzf /root/deploy.tar.gz
/opt/jarvis-venv/bin/pip install -q -e /opt/Jarvis/.
systemctl restart jarvis-agent jarvis-monitor
```

### 注意事项

同一个 Telegram Bot Token 只能有一个实例轮询。迁移到服务器后**必须停止本地 PythonClaw**，否则两边抢 getUpdates 会报 `Conflict` 错误：

```powershell
# Windows PowerShell
Stop-Process -Name python -Force
```

---

## 九、安全事项

- 所有 API Key 存放在 `~/.pythonclaw/pythonclaw.json`，不进 git
- `.gitignore` 已排除：`.venv/`、`__pycache__/`、`CLAUDE.md`、`.idea/`、`jarvis_deploy.tar.gz`
- `.env.deploy` 只用于本地部署连接信息，不应提交公开仓库
- Phase 8 个性化数据存放在 `~/.pythonclaw/context/personalization/user_profiles.sqlite3`
- 生产默认关闭 legacy LLM memory 写入：`personalization.legacyMemoryWriteEnabled=false`
- Web dashboard 敏感 API 需使用 `JARVIS_WEB_ADMIN_TOKEN`，不要把 admin token 放入可通过 UI 修改的 config
- Telegram bot token 可能出现在 HTTP 客户端 debug 日志中，建议生产环境提高 `httpx` 日志级别
- 建议部署完成后修改服务器密码：`passwd root`
- 建议配置 SSH Key 登录并禁用密码认证：在 `/etc/ssh/sshd_config` 设置 `PasswordAuthentication no`
