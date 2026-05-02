---
name: fx_monitor
description: >
  Monitor CNY/AUD (人民币/澳元) exchange rate with Chinese bank spot buy/sell
  quotes for student exchange decisions, market fallback data, 90-day history,
  statistical trend analysis, and threshold breach alerts. Use when: user asks about
  CNY/AUD or RMB-to-Australian-dollar exchange rate, wants daily market analysis,
  needs rate monitoring with alerts, or requests currency trend/prediction.
  NOT for: other currency pairs, stock prices, crypto, or futures.
dependencies: yfinance, pandas, numpy
metadata:
  emoji: "💱"
---

# CNY/AUD Exchange Rate Monitor

Chinese-bank CNY/AUD (人民币/澳元) quote monitoring, historical analysis, and
threshold alert system. For student tuition/living-cost exchange, the operational
reference is bank `spot_sell_rate` because the customer pays CNY to buy AUD.

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
- Bank spot buy/sell quotes for AUD
- Student exchange reference: lowest available bank spot sell rate (1 AUD = ? CNY)
- Market mid-rate fallback (1 AUD = ? CNY)
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

1. Run `fetch_rate.py --period 90d --format json` to get bank quote context and historical data
2. Use `web_search` with `topic="news"` to fetch:
   - US-Iran geopolitical developments (affects oil → AUD)
   - Australian economic data (RBA rates, CPI, trade balance)
   - Chinese monetary policy and CNY basket moves
   - Global risk sentiment (USD strength, commodity prices)
3. Synthesize: explain current bank spot sell level, bank buy/sell spread, recent market trend, and key drivers
4. Give short-term outlook (bullish/bearish/sideways for CNY vs AUD)
5. Note key risks and events to watch

### 3. Monitor geopolitical news via Google News RSS (free, zero API credits)

```bash
# Check for new articles using default keywords (US-Iran, AUD drivers)
python {skill_path}/news_monitor.py

# Custom keywords
python {skill_path}/news_monitor.py --keywords "Iran Hormuz" "US Iran ceasefire" "RBA rate"

# Dry run (don't mark articles as seen, for use in daily report)
python {skill_path}/news_monitor.py --no-mark-seen --format json
```

**Default keywords monitored:**
- `US Iran ceasefire`, `Iran Hormuz strait`, `Iran nuclear deal`, `Middle East oil disruption`
- `RBA interest rate decision`, `Australia dollar AUD`, `China Australia trade`

**State file:** `~/.pythonclaw/context/news_monitor_state.json`

## Notes

- Bank quote source: Chinese bank FX boards, best effort. Bank boards quote CNY per 100 AUD; scripts normalize to CNY per 1 AUD.
- Student CNY -> AUD decisions should use bank `spot_sell_rate`; AUD -> CNY decisions should use bank `spot_buy_rate`.
- Market fallback source: `open.er-api.com` (free, ~1 min delay, no key needed)
- Historical data: yfinance `CNYAUD=X` (daily closes, free)
- News monitoring: Google News RSS (free, no API key, zero Tavily credits)
- Reserve Tavily (`web_search`) for daily report depth analysis only (~60 credits/month)

## Resources

| File | Description |
|------|-------------|
| `fetch_rate.py` | Bank spot buy/sell quotes + market fallback + 90-day history (yfinance) |
| `monitor_alert.py` | Threshold alert vs saved baseline, using bank spot sell when available — 0 Tavily credits |
| `news_monitor.py` | Google News RSS keyword monitor — 0 Tavily credits |
| `jobs.example.yaml` | Sample cron job definitions |
