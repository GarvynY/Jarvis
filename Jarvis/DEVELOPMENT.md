# Jarvis Development Notes

更新时间：2026-05-18

本文档记录 Jarvis 当前开发、测试、部署和质量基准流程。历史完整开发记录仍保留在仓库根目录的 `DEVELOPMENT.md`。

## 当前架构状态

Jarvis 当前是一个 privacy-aware multi-agent financial research system。核心运行链路：

```text
Telegram / Debug API / Scheduled Task
        |
        v
Safe Context Builder
        |
        v
Research Preset
        |
        v
Parallel Expert Agents
        +-- FXAgent
        +-- NewsAgent
        +-- MacroAgent
        +-- MarketDriversAgent
        +-- PolicySignalAgent
        `-- RiskAgent
        |
        v
Evidence Store / EvidenceScorer / ContextPack
        |
        v
Supervisor / Telegram formatter
```

## 关键模块

| 模块 | 作用 |
|---|---|
| `pythonclaw/templates/skills/data/fx_monitor/research/schema.py` | Finding、AgentOutput、EvidenceChunk 等结构化模型 |
| `pythonclaw/templates/skills/data/fx_monitor/research/source_metadata.py` | source metadata 解析、source tier 和质量评分 |
| `pythonclaw/templates/skills/data/fx_monitor/research/evidence_store.py` | EvidenceStore、chunk ingest、scoring、ContextPack routing |
| `pythonclaw/templates/skills/data/fx_monitor/research/baseline_recorder.py` | 质量基准指标持久化 |
| `pythonclaw/templates/skills/data/fx_monitor/research/agents/` | FX、News、Macro、MarketDrivers、PolicySignal、Risk agents |
| `pythonclaw/web/fx_research_debug.py` | debug payload 组装 |
| `pythonclaw/web/app.py` | `/api/debug/fx_research` 与浏览器 debug 页面 |
| `pythonclaw/channels/_telegram_helpers.py` | Telegram brief 格式化和用户侧表达 |

## Phase 10.6 当前约束

### Category taxonomy

当前主要 category：

- `fx_price`
- `news_event`
- `macro`
- `policy_signal`
- `market_driver`
- `commodity_trade`
- `risk`
- `data_gap`
- `unknown`

`market_driver` 默认归属宏观信号，不再作为“汇率事实”的默认候选，避免在默认四段 ContextPack 中被 FX section 先消耗。

### Enhanced Finding fields

Finding 兼容旧输出，同时支持以下可选字段：

- `subcategory`
- `entities`
- `direction_for_aud`
- `direction_for_cny`
- `direction_for_pair`
- `time_horizon`
- `time_sensitivity`
- `evidence_basis`

Agent 应优先输出显式 `category` / `subcategory`。EvidenceStore 仍保留 agent 默认类别 fallback，用于兼容旧输出。

### Policy/Market balance

当 PolicySignalAgent 和 MarketDriversAgent 同时开启：

- 有效 policy_signal 应进入标准 scoring 路径，获得 `score_breakdown`、`composite_score` 和 `score_reason`。
- 宏观 section 有有效 policy_signal 时至少保留 1 个 policy chunk，正常上限 1 或 2。
- 有效 market_driver / commodity_trade 仍至少保留 1 个。
- 不强制选择 tier 4、低置信度或 `insufficient_evidence` 的 policy chunk。
- Google News policy 来源保持 tier 3，不重标为官方来源。

有效 policy_signal 标准：

- `confidence >= 0.5` 或 `evidence_score >= 0.6`
- `source_tier <= 3`
- `evidence_basis != insufficient_evidence`

### Conflict reporting

内部 debug/baseline 保留 raw counts 和 breakdown：

- `raw_conflict_count`
- `unique_conflict_count`
- `reportable_conflict_count`
- `news_internal`
- `news_vs_fx`
- `news_vs_market_driver`
- `policy_vs_fx`
- `policy_vs_market_driver`
- `policy_internal`

用户报告层不直接暴露 raw count，改为解释性方向分歧描述。

## Baseline 与质量记录

当前 baseline 文档入口：

- `docs/README.md`
- `docs/phase_10_6h_summary.md`
- `docs/baseline_4bee1175.md`
- `docs/baseline_073c7ec6.md`

质量提升阶段建议记录：

- agent findings / selected 分布
- selected chunk IDs 和 section composition
- score summary 与 score breakdown
- source metadata 和 source tier
- policy candidates
- conflict breakdown
- retrieval traces
- fallback_count
- stability notes

Baseline 数据应优先存为结构化字段；Markdown 报告只在手动需要分析时生成，避免每次调用自动写大量文档。

## 本地开发

```bash
cd Jarvis
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

如果本机没有 pytest：

```bash
python3 -m pip install pytest
```

常用测试：

```bash
python3 -m pytest pythonclaw/templates/skills/data/fx_monitor/research/test_evidence_store.py
python3 -m pytest pythonclaw/templates/skills/data/fx_monitor/research/test_source_metadata.py
python3 -m pytest pythonclaw/web/test_app_phase10_debug.py
python3 -m pytest pythonclaw/channels/test_format_brief.py
python3 -m pytest tests
```

语法检查：

```bash
python3 -m py_compile \
  pythonclaw/templates/skills/data/fx_monitor/research/evidence_store.py \
  pythonclaw/templates/skills/data/fx_monitor/research/baseline_recorder.py \
  pythonclaw/channels/_telegram_helpers.py
```

## 部署

部署连接信息在仓库根目录 `.env.deploy`，该文件不应提交公开仓库。

当前生产服务由 systemd 管理：

```text
jarvis-agent.service
jarvis-monitor.service
```

部署前应确认：

- 本地测试通过。
- 没有误改或提交密钥。
- 重要运行数据已有备份。
- 新功能的 feature flag 或默认行为符合预期。

部署后应检查：

- `systemctl is-active jarvis-agent.service jarvis-monitor.service`
- `/api/debug/fx_research` 是否能返回最新 debug payload。
- Telegram `/fx_research` 或 debug API 单次运行是否生成稳定 ContextPack。
- 相关日志中没有 `Traceback`、`ERROR`、`database is locked`、`telegram.error`。

## 当前已知边界

- PolicySignalAgent 仍可能依赖 Google News tier 3 聚合来源；后续应优先接入 RBA、PBoC、Fed 官方来源。
- 用户侧 conflict summary 当前是固定归纳表达，后续可基于 breakdown 动态生成更精确摘要。
- MacroAgent 个别 source label 过长时仍可能导致 chunk 被过滤，可复用 policy source label compaction 的策略。
- Policy reserve 解决的是 ContextPack 平衡，不等于完整政策推理层。

