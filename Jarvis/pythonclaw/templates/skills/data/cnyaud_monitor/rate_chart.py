#!/usr/bin/env python3
"""
Generate a 2-day CNY/AUD exchange rate chart as PNG bytes.
Uses yfinance hourly data (free, no API key needed).
"""
from __future__ import annotations

import io


def generate_2day_chart() -> bytes:
    """
    Fetch 2 days of hourly CNY/AUD data and return a PNG chart as bytes.
    Falls back to 5-day daily data if hourly is unavailable.
    Raises RuntimeError on failure.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: {exc}. Run: pip install matplotlib"
        ) from exc

    ticker = yf.Ticker("CNYAUD=X")
    hist = ticker.history(period="2d", interval="1h")
    period_label = "2-Day (Hourly)"
    if hist.empty:
        hist = ticker.history(period="5d", interval="1d")
        period_label = "5-Day (Daily fallback)"
    if hist.empty:
        raise RuntimeError("No data available from yfinance for CNYAUD=X")

    # CNYAUD=X = 1 CNY in AUD; invert to get 1 AUD in CNY (user-facing direction)
    closes = hist["Close"].dropna()
    times = closes.index.to_pydatetime()
    cny_per_aud = [round(1.0 / float(v), 4) for v in closes]

    latest = cny_per_aud[-1]
    high_val = max(cny_per_aud)
    low_val = min(cny_per_aud)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(times, cny_per_aud, color="#1976D2", linewidth=1.8, marker="o", markersize=3)
    ax.fill_between(times, cny_per_aud, low_val * 0.9998, alpha=0.08, color="#1976D2")

    # Current price line
    ax.axhline(y=latest, color="#E53935", linestyle="--", linewidth=0.9, alpha=0.7)
    ax.annotate(
        f"Now: {latest:.4f}",
        xy=(times[-1], latest),
        xytext=(-95, 10),
        textcoords="offset points",
        fontsize=9,
        color="#E53935",
        fontweight="bold",
    )

    ax.set_title(
        f"CNY/AUD  ({period_label})   High: {high_val:.4f}   Low: {low_val:.4f}",
        fontsize=12,
    )
    ax.set_ylabel("CNY per 1 AUD")
    ax.set_xlabel("Time (UTC)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.xticks(rotation=45, fontsize=8)
    ax.grid(True, alpha=0.3, linestyle="--")
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


if __name__ == "__main__":
    import sys
    png = generate_2day_chart()
    sys.stdout.buffer.write(png)
