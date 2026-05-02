#!/usr/bin/env python3
"""
Generate a 2-day CNY/AUD exchange rate chart as PNG bytes.
Data: yfinance AUDCNY=X hourly (1 AUD = X CNY, no inversion needed).
"""
from __future__ import annotations

import io


def generate_2day_chart() -> bytes:
    """
    Fetch ~48 h of hourly AUD/CNY data and return a polished PNG chart.
    Falls back to 5-day daily data if hourly is unavailable.
    Raises RuntimeError on failure.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.ticker as mticker
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: {exc}. Run: pip install matplotlib"
        ) from exc

    # AUDCNY=X gives 1 AUD = X CNY directly — no inversion needed, more accurate
    ticker = yf.Ticker("AUDCNY=X")
    hist = ticker.history(period="2d", interval="1h")
    period_label = "Last 48h · Hourly"
    if hist.empty:
        hist = ticker.history(period="5d", interval="1d")
        period_label = "Last 5d · Daily"
    if hist.empty:
        raise RuntimeError("No data from yfinance AUDCNY=X")

    closes = hist["Close"].dropna()
    times = list(closes.index.to_pydatetime())
    prices = [round(float(v), 4) for v in closes]

    latest  = prices[-1]
    high    = max(prices)
    low     = min(prices)
    open_p  = prices[0]
    change  = latest - open_p
    chg_pct = change / open_p * 100
    chg_str = f"{change:+.4f}  ({chg_pct:+.2f}%)"
    color_chg = "#26A69A" if change >= 0 else "#EF5350"   # teal / red

    # ── Figure setup ─────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(11, 5.5))
    fig.patch.set_facecolor("#FAFAFA")
    ax.set_facecolor("#FFFFFF")

    # ── Line + fill ───────────────────────────────────────────────────────────
    line_color = "#1565C0"
    ax.plot(times, prices, color=line_color, linewidth=1.8, zorder=3)
    ax.fill_between(times, prices, low * 0.9995,
                    alpha=0.12, color=line_color, zorder=2)

    # ── Current price dashed line ─────────────────────────────────────────────
    ax.axhline(latest, color=color_chg, linewidth=0.9,
               linestyle="--", alpha=0.8, zorder=4)

    # ── High / Low markers ────────────────────────────────────────────────────
    hi_idx = prices.index(high)
    lo_idx = prices.index(low)
    ax.scatter([times[hi_idx]], [high], color="#26A69A",
               s=60, zorder=5, label=f"High {high:.4f}")
    ax.scatter([times[lo_idx]], [low],  color="#EF5350",
               s=60, zorder=5, label=f"Low  {low:.4f}")
    ax.annotate(f"▲ {high:.4f}", xy=(times[hi_idx], high),
                xytext=(5, 6), textcoords="offset points",
                fontsize=8.5, color="#26A69A", fontweight="bold")
    ax.annotate(f"▼ {low:.4f}", xy=(times[lo_idx], low),
                xytext=(5, -13), textcoords="offset points",
                fontsize=8.5, color="#EF5350", fontweight="bold")

    # ── Current price label on right edge ────────────────────────────────────
    ax.annotate(
        f"  {latest:.4f}",
        xy=(times[-1], latest),
        xytext=(4, 0), textcoords="offset points",
        fontsize=9.5, color=color_chg, fontweight="bold",
        va="center",
    )

    # ── Title & subtitle ──────────────────────────────────────────────────────
    period_en = period_label.replace("近48小时 (小时线)", "Last 48h · Hourly").replace("近5日 (日线)", "Last 5d · Daily")
    ax.set_title(
        f"AUD / CNY  (1 AUD = ? CNY)   {period_en}",
        fontsize=13, fontweight="bold", pad=12, color="#212121",
    )
    fig.text(
        0.5, 0.91,
        f"Change: {chg_str}     High: {high:.4f}     Low: {low:.4f}",
        ha="center", fontsize=9, color=color_chg,
    )

    # ── Axes formatting ───────────────────────────────────────────────────────
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f"))
    plt.xticks(rotation=40, ha="right", fontsize=8, color="#555555")
    plt.yticks(fontsize=8.5, color="#555555")

    ax.set_ylabel("CNY per AUD", fontsize=9, color="#555555")
    ax.set_xlim(times[0], times[-1])

    # ── Grid ─────────────────────────────────────────────────────────────────
    ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5, color="#BBBBBB")
    ax.grid(axis="x", linestyle=":",  linewidth=0.4, alpha=0.4, color="#CCCCCC")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#CCCCCC")

    # ── Legend + watermark ────────────────────────────────────────────────────
    ax.legend(loc="upper left", fontsize=8, framealpha=0.6,
              edgecolor="#CCCCCC", handlelength=1)
    fig.text(0.99, 0.01, "Data: yfinance AUDCNY=X",
             ha="right", fontsize=7, color="#AAAAAA")

    plt.tight_layout(rect=[0, 0.02, 1, 0.90])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=160, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


if __name__ == "__main__":
    import sys
    sys.stdout.buffer.write(generate_2day_chart())
