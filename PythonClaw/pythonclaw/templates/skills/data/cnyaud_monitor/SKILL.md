---
name: cnyaud_monitor
description: >
  Monitor CNY/AUD (人民币/澳元) exchange rate with real-time data, 90-day history,
  statistical trend analysis, and threshold breach alerts. Use when: user asks about
  CNY/AUD or RMB-to-Australian-dollar exchange rate, wants daily market analysis,
  needs rate monitoring with alerts, or requests currency trend/prediction.
  NOT for: other currency pairs, stock prices, crypto, or futures.
dependencies: yfinance, pandas, numpy
metadata:
  emoji: "💱"
---

# CNY/AUD Exchange Rate Monitor

Real-time CNY/AUD (人民币/澳元) exchange rate monitoring, historical analysis, and
threshold alert system powered by Yahoo Finance.

## When to Use

✅ **USE this skill when:**
- "CNY/AUD 今天汇率多少？"
- "人民币兑澳元近3个月趋势如何？"
- "如果汇率涨跌超过0.5%，提醒我"
- "结合美伊局势分析 CNY/AUD 走势"
- "澳元最近相对人民币是升值还是贬值？"

❌ **DON'T use this skill when:**
- Other currency pairs → use the `finance` skill (EURUSD=X, etc.)
- Stock or crypto prices → use the `finance` skill
- Options/futures analysis → use specialized tools

## Usage/Commands

### 1. Fetch current rate + historical analysis

```bash
python {skill_path}/fetch_rate.py
python {skill_path}/fetch_rate.py --period 30d
python {skill_path}/fetch_rate.py --period 1y --format json
```

**Options:**
- `--period` — history window: `7d`, `30d`, `90d`, `1y`, `2y` (default: `90d`)
- `--format` — `text` (default) or `json`

**Output includes:**
- Current rate (1 CNY = ? AUD)
- Period start/end rates and % change
- High/low/mean/volatility (σ)
- 7-day trend vs prior 7 days
- Last 30 data points for charting

### 2. Check if rate has moved beyond threshold (alert)

```bash
# First run — saves baseline rate
python {skill_path}/monitor_alert.py --threshold 0.5

# Subsequent runs — compare to saved baseline
python {skill_path}/monitor_alert.py --threshold 0.5

# Force update baseline to current rate
python {skill_path}/monitor_alert.py --update

# JSON output for programmatic use
python {skill_path}/monitor_alert.py --threshold 0.5 --format json
```

**Options:**
- `--threshold` — alert if |change| ≥ this % (default: `0.5`)
- `--update` — reset the saved baseline to the current rate
- `--format` — `text` (default) or `json`

**State file:** `~/.pythonclaw/context/cnyaud_state.json`

## Analysis Workflow (for daily report)

When tasked with a daily CNY/AUD analysis:

1. Run `fetch_rate.py --period 90d --format json` to get historical data
2. Use `web_search` with `topic="news"` to fetch:
   - US-Iran geopolitical developments (affects oil → AUD)
   - Australian economic data (RBA rates, CPI, trade balance)
   - Chinese monetary policy and CNY basket moves
   - Global risk sentiment (USD strength, commodity prices)
3. Synthesize: explain current rate level, recent trend, key drivers
4. Give short-term outlook (bullish/bearish/sideways for CNY vs AUD)
5. Note key risks and events to watch

## Notes

- Yahoo Finance ticker for CNY/AUD: `CNYAUD=X` (1 CNY = ? AUD)
- Forex data may have small delays vs live trading feeds
- Baseline state persists in `~/.pythonclaw/context/cnyaud_state.json`
- No API key needed — Yahoo Finance is free

## Resources

| File | Description |
|------|-------------|
| `fetch_rate.py` | Fetch current rate + 90-day history with trend stats |
| `monitor_alert.py` | Check threshold breach vs saved baseline |
| `jobs.example.yaml` | Sample cron job definitions to copy to `~/.pythonclaw/context/cron/jobs.yaml` |
