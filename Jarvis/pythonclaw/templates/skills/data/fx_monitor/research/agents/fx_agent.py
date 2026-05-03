"""
Phase 9 Step 3a — FXAgent

Wraps fetch_rate.py to produce structured AgentOutput for CNY/AUD.
No LLM calls. No raw memory, chat history, or full user profile reads.
Only ResearchTask fields and the existing fetch_rate tool are used.

Supported focus_pair: "CNY/AUD"
Returns status="partial" for unsupported or missing focus_pair.

Thread model
------------
fetch_rate() is a blocking I/O function. Each run uses a short-lived
ThreadPoolExecutor (NOT asyncio's default executor) so repeated tests and
serverless-style invocations do not share mutable executor state.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
from pathlib import Path
from typing import Any

# ── Skill-dir path so fetch_rate.py is importable ────────────────────────────
_SKILL_DIR = Path(__file__).parent.parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from fetch_rate import fetch_rate as _fetch_rate  # noqa: E402

try:
    from ..schema import AgentOutput, Finding, ResearchTask, SourceRef, now_iso
except ImportError:
    from schema import AgentOutput, Finding, ResearchTask, SourceRef, now_iso  # type: ignore[no-redef]

# ── Constants ─────────────────────────────────────────────────────────────────

_SUPPORTED_PAIRS: frozenset[str] = frozenset({"CNY/AUD"})

# Maximum length for pair strings echoed back in outputs (prompt-injection guard)
_MAX_PAIR_ECHO_LEN: int = 12

# Characters allowed in a pair string echoed into outputs
_PAIR_SAFE_CHARS: frozenset[str] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789/.-_"
)

# Banned terms: must never appear in any summary or finding
_BANNED: frozenset[str] = frozenset({
    "建议买入", "建议卖出", "换汇时机", "立即操作",
    "应该买", "应该卖", "最佳时机",
})

# Volatility threshold: σ of CNYAUD=X daily closes
_HIGH_VOLATILITY_STD: float = 0.003

# Confidence ceiling — bank scraping and real-time sources are not perfectly reliable
_MAX_CONFIDENCE: float = 0.85


def _sanitise_pair(pair: str) -> str:
    """
    Return a safe, short representation of pair for use in output strings.

    Strips non-whitelist characters and truncates to _MAX_PAIR_ECHO_LEN.
    Prevents arbitrary text from a user-supplied focus_pair reaching the
    supervisor LLM prompt via AgentOutput.summary or missing_data.
    """
    cleaned = "".join(c for c in pair if c in _PAIR_SAFE_CHARS)
    return cleaned[:_MAX_PAIR_ECHO_LEN]


class FXAgent:
    """
    FX rate research agent for CNY/AUD.

    Protocol:
        agent.agent_name       → str
        await agent.run(task)  → AgentOutput

    Each call owns a small ThreadPoolExecutor for offloading the blocking
    fetch_rate() call. This avoids polluting asyncio's default executor
    and keeps invocations independent.

    Usage:
        agent = FXAgent()
        output = await agent.run(task)
    """

    agent_name: str = "fx_agent"

    @classmethod
    def close_executor(cls) -> None:
        """Backward-compatible no-op; executors are per-run."""
        return None

    async def run(self, task: ResearchTask) -> AgentOutput:
        """Fetch live CNY/AUD rate data and return structured findings."""
        t0 = time.monotonic()

        # ── Guard: unsupported or missing focus_pair ──────────────────────────
        pair = (task.focus_pair or "").strip()
        if not pair:
            return AgentOutput(
                agent_name=self.agent_name,
                status="partial",
                summary="focus_pair 未指定",
                missing_data=["focus_pair"],
                latency_ms=int((time.monotonic() - t0) * 1000),
                token_usage={},
                regulatory_flags=[],
            )

        safe_pair = _sanitise_pair(pair)   # used in any output strings
        if pair not in _SUPPORTED_PAIRS:
            return AgentOutput(
                agent_name=self.agent_name,
                status="partial",
                summary=f"不支持的货币对（当前仅支持 CNY/AUD）",
                missing_data=[f"unsupported_pair:{safe_pair}"],
                latency_ms=int((time.monotonic() - t0) * 1000),
                token_usage={},
                regulatory_flags=[],
            )

        # ── Fetch via own executor — non-blocking, no default executor ────────
        try:
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="fx-agent",
            )
            data: dict[str, Any] = await loop.run_in_executor(
                executor, _fetch_rate, "90d"
            )
        except Exception as exc:
            return AgentOutput.make_error(
                self.agent_name,
                error=f"fetch_rate failed: {exc}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        finally:
            if "executor" in locals():
                executor.shutdown(wait=False, cancel_futures=True)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return _build_output(data, task, latency_ms, self.agent_name)


# ── Output builder (pure function — no I/O, no LLM) ──────────────────────────

def _build_output(
    data: dict[str, Any],
    task: ResearchTask,
    latency_ms: int,
    agent_name: str,
) -> AgentOutput:
    findings: list[Finding] = []
    sources: list[SourceRef] = []
    risks: list[str] = []
    missing: list[str] = []
    retrieved_at: str = data.get("fetched_at_utc") or now_iso()

    # ── 1. Current rate ───────────────────────────────────────────────────────
    if "error" in data:
        missing.append("current_rate")
    else:
        current_cny_per_aud = data.get("current_1_AUD_in_CNY")
        market_cny_per_aud  = data.get("market_1_AUD_in_CNY")
        basis               = data.get("current_rate_basis", "unknown")

        if current_cny_per_aud:
            parts = [f"1 AUD = {current_cny_per_aud:.4f} CNY（{basis}）"]
            if market_cny_per_aud and basis != "market_mid_fallback":
                spread = round(current_cny_per_aud - market_cny_per_aud, 4)
                parts.append(
                    f"市场中间价 {market_cny_per_aud:.4f} CNY/AUD，银行加价 +{spread:.4f}"
                )
            findings.append(Finding(
                key="current_rate",
                summary="；".join(parts),
                direction=None,
            ))

        # ── Sources ───────────────────────────────────────────────────────────
        rt_source   = data.get("market_rate_source", "")
        bank_source = data.get("bank_rate_source", "")
        if rt_source and rt_source != "unavailable":
            sources.append(SourceRef(
                title="CNY/AUD 市场实时汇率",
                url="https://open.er-api.com/v6/latest/CNY",
                source=rt_source,
                retrieved_at=retrieved_at,
            ))
        if bank_source and "unavailable" not in bank_source.lower():
            sources.append(SourceRef(
                title="中国银行 AUD 牌价（10家）",
                url="https://www.usdrate.top/",
                source=bank_source,
                retrieved_at=retrieved_at,
            ))

        # ── 2. Bank spread ────────────────────────────────────────────────────
        bank_summary = (data.get("bank_exchange_rates") or {}).get("summary")
        if bank_summary:
            spread_pct = bank_summary.get("median_bank_spread_pct")
            sell_mid   = bank_summary.get("median_spot_sell_rate")
            buy_mid    = bank_summary.get("median_spot_buy_rate")
            sell_lo    = bank_summary.get("lowest_spot_sell_rate")
            sell_hi    = bank_summary.get("highest_spot_sell_rate")
            n          = bank_summary.get("quote_count", 0)

            parts = [f"银行牌价样本 {n} 家（单位 CNY/AUD）"]
            if sell_lo and sell_hi:
                parts.append(f"现汇卖出区间 {sell_lo:.4f}–{sell_hi:.4f}")
            if sell_mid:
                parts.append(f"卖出中位 {sell_mid:.4f}")
            if buy_mid:
                parts.append(f"买入中位 {buy_mid:.4f}")
            if spread_pct is not None:
                parts.append(f"买卖价差 {spread_pct:.3f}%")
            findings.append(Finding(
                key="bank_spread",
                summary="，".join(parts),
                direction=None,
            ))
        else:
            missing.append("bank_quotes")

    # ── 3. Historical stats ───────────────────────────────────────────────────
    #
    # fetch_rate stats use yfinance CNYAUD=X (= AUD per CNY):
    #   period_change_pct  = (end_AUDperCNY / start_AUDperCNY - 1) * 100
    #     positive → AUD/CNY up → CNY gained vs AUD → AUD weakened (bearish_aud)
    #     negative → AUD/CNY down → AUD gained vs CNY            (bullish_aud)
    #   high_cny_aud = max(AUD/CNY) = cheapest AUD in CNY terms  (lowest CNY/AUD)
    #   low_cny_aud  = min(AUD/CNY) = most expensive AUD in CNY (highest CNY/AUD)
    #
    # All values displayed to students are converted to CNY/AUD for consistency.

    stats = data.get("stats")
    if stats:
        period_chg = stats.get("period_change_pct", 0.0)
        hi_aud_per_cny = stats.get("high_cny_aud")   # max AUD/CNY → min CNY/AUD
        lo_aud_per_cny = stats.get("low_cny_aud")    # min AUD/CNY → max CNY/AUD
        vol_std        = stats.get("volatility_std", 0.0)
        trend_dir      = stats.get("trend_direction", "")
        trend_7d       = stats.get("trend_7d_pct")

        # Direction: negative period_change_pct means AUD/CNY fell = AUD strengthened
        if period_chg < -0.5:
            direction = "bullish_aud"
        elif period_chg > 0.5:
            direction = "bearish_aud"
        else:
            direction = "neutral"

        parts = [f"{stats['period']} AUD 变动 {period_chg:+.2f}%（CNYAUD=X）"]

        # Convert range from AUD/CNY to student-facing CNY/AUD
        if hi_aud_per_cny and lo_aud_per_cny and hi_aud_per_cny > 0 and lo_aud_per_cny > 0:
            period_lo_cny_aud = round(1.0 / hi_aud_per_cny, 4)  # AUD was cheapest here
            period_hi_cny_aud = round(1.0 / lo_aud_per_cny, 4)  # AUD was most expensive here
            parts.append(f"区间 {period_lo_cny_aud:.4f}–{period_hi_cny_aud:.4f} CNY/AUD")

        if trend_7d is not None:
            parts.append(f"近7日 {trend_7d:+.2f}%（{trend_dir}）")
        if vol_std:
            # Explicit unit: σ of CNYAUD=X daily closes (price std dev, not return std dev)
            parts.append(f"日收盘价格波动σ={vol_std:.4f}（AUD/CNY）")

        findings.append(Finding(
            key="historical_trend",
            summary="；".join(parts),
            direction=direction,
        ))
        sources.append(SourceRef(
            title="CNY/AUD 历史数据（90日）",
            url="https://finance.yahoo.com/quote/CNYAUD=X/",
            source="yfinance CNYAUD=X",
            retrieved_at=retrieved_at,
        ))

        if vol_std > _HIGH_VOLATILITY_STD:
            risks.append(
                f"近期日收盘价格波动偏高（σ={vol_std:.4f} AUD/CNY），"
                "参考汇率时效性有限，建议使用银行实时牌价"
            )
    else:
        missing.append("historical_stats")

    # ── 4. Recent range (last 5 daily closes, all in CNY/AUD) ────────────────
    recent = data.get("recent_history", [])
    if len(recent) >= 2:
        tail = recent[-5:]
        vals = [pt["cny_per_aud"] for pt in tail if "cny_per_aud" in pt]
        if vals:
            r_hi    = max(vals)
            r_lo    = min(vals)
            r_range = round(r_hi - r_lo, 4)
            findings.append(Finding(
                key="recent_range",
                summary=(
                    f"近5个交易日区间：{r_lo:.4f}–{r_hi:.4f} CNY/AUD"
                    f"（振幅 {r_range:.4f}）"
                ),
                direction=None,
            ))
    else:
        missing.append("recent_range")

    # ── 5. Target rate gap (safe_user_context only — no raw profile) ──────────
    target       = task.safe_user_context.target_rate
    current_rate = data.get("current_1_AUD_in_CNY")
    if target and current_rate:
        gap     = round(current_rate - target, 4)
        gap_pct = round(gap / target * 100, 2)
        if abs(gap_pct) < 0.1:
            gap_desc = "当前汇率已非常接近目标汇率"
        elif gap > 0:
            gap_desc = f"当前汇率高于目标 +{gap:.4f}（+{gap_pct:.2f}%）"
        else:
            gap_desc = f"当前汇率低于目标 {gap:.4f}（{gap_pct:.2f}%）"
        findings.append(Finding(
            key="target_rate_gap",
            summary=f"目标汇率 {target:.4f} CNY/AUD：{gap_desc}",
            direction=None,
        ))

    # ── Confidence (capped at _MAX_CONFIDENCE = 0.85) ────────────────────────
    has_rate = any(f.key == "current_rate"    for f in findings)
    has_hist = any(f.key == "historical_trend" for f in findings)
    has_bank = any(f.key == "bank_spread"     for f in findings)
    if has_rate and has_hist and has_bank:
        confidence = _MAX_CONFIDENCE        # all three sources, cap at 0.85
    elif has_rate and (has_hist or has_bank):
        confidence = 0.65
    elif has_rate:
        confidence = 0.45
    else:
        confidence = 0.1

    # ── Status ────────────────────────────────────────────────────────────────
    if not findings:
        status = "error"
    elif missing:
        status = "partial"
    else:
        status = "ok"

    return AgentOutput(
        agent_name=agent_name,
        status=status,
        summary=findings[0].summary if findings else "无法获取汇率数据",
        findings=findings,
        sources=sources,
        as_of=retrieved_at,
        confidence=confidence,
        risks=risks,
        missing_data=missing,
        latency_ms=latency_ms,
        token_usage={},
        regulatory_flags=[],
    )
