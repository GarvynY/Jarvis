---
title: 外部接口并发墙与 Provider 级限流
date: 2026-05-07
category: ai-technology
---

## 问题

多 Agent 并发会放大外部 API 请求数量。如果 4 个 Agent 同时运行，每个 Agent 内部又触发多个 HTTP 请求，系统很容易撞到 `429 Too Many Requests`。

## 当前预埋

Jarvis 已经把外部调用收敛到 provider 级限流层：

- `deepseek`
- `tavily`
- `google_news`
- `telegram`
- `fx_data`

核心策略：

1. Provider 级并发上限
2. 429 / 408 / 5xx / timeout 指数退避
3. 优先尊重 `Retry-After`
4. 保留未来 Redis distributed limiter 替换点

```python
with limiter.acquire("deepseek"):
    call_llm()
```

==短期用本地 semaphore，长期替换为 Redis-backed limiter。==
