"""Descriptive statistics computed from a daily bar series.

Everything here is pure and deterministic: given the same bars it returns the
same numbers. That matters because these values are what the assistant panel
quotes back, so they have to be reproducible and auditable.
"""

from __future__ import annotations

import math
from datetime import date as Date
from typing import Any

from .models import Bar

TRADING_DAYS = 252

# (label, lookback in trading sessions)
RETURN_WINDOWS: list[tuple[str, int]] = [
    ("1d", 1),
    ("1w", 5),
    ("1m", 21),
    ("3m", 63),
    ("6m", 126),
    ("1y", TRADING_DAYS),
]

SMA_WINDOWS = (20, 50, 200)


def sma(closes: list[float], window: int) -> float | None:
    """Simple moving average of the last ``window`` closes."""
    if len(closes) < window or window <= 0:
        return None
    return sum(closes[-window:]) / window


def pct_change(current: float, past: float) -> float | None:
    if past == 0:
        return None
    return (current / past - 1.0) * 100.0


def log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def realized_vol(closes: list[float], window: int) -> float | None:
    """Annualised standard deviation of daily log returns, in percent."""
    rets = log_returns(closes)[-window:]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS) * 100.0


def rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder's RSI. Returns ``None`` until there are enough observations."""
    if len(closes) <= window:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    for g, l in zip(gains[window:], losses[window:]):
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + l) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def max_drawdown(closes: list[float]) -> float | None:
    """Largest peak-to-trough decline in the window, as a negative percent."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak > 0:
            worst = min(worst, (close / peak - 1.0) * 100.0)
    return worst * 1.0


def ytd_return(bars: list[Bar]) -> float | None:
    """Return from the last close of the prior year to the latest close."""
    if not bars:
        return None
    year = bars[-1].date.year
    prior = [b for b in bars if b.date.year < year]
    base = prior[-1].close if prior else bars[0].close
    return pct_change(bars[-1].close, base)


def summarize(bars: list[Bar]) -> dict[str, Any]:
    """Full metric block for one instrument."""
    if not bars:
        return {}
    closes = [b.close for b in bars]
    last = closes[-1]

    returns: dict[str, float | None] = {}
    for label, window in RETURN_WINDOWS:
        returns[label] = pct_change(last, closes[-window - 1]) if len(closes) > window else None
    returns["ytd"] = ytd_return(bars)

    moving_averages: dict[str, dict[str, float | None]] = {}
    for window in SMA_WINDOWS:
        value = sma(closes, window)
        moving_averages[f"sma{window}"] = {
            "value": value,
            "distance_pct": pct_change(last, value) if value else None,
        }

    window_bars = bars[-TRADING_DAYS:]
    high = max(b.high for b in window_bars)
    low = min(b.low for b in window_bars)

    return {
        "last": last,
        "as_of": bars[-1].date.isoformat(),
        "sessions": len(bars),
        "returns": returns,
        "moving_averages": moving_averages,
        "volatility": {
            "realized_20d": realized_vol(closes, 20),
            "realized_60d": realized_vol(closes, 60),
        },
        "rsi_14": rsi(closes, 14),
        "range_52w": {
            "high": high,
            "low": low,
            "pct_from_high": pct_change(last, high),
            "pct_from_low": pct_change(last, low),
            "sessions_used": len(window_bars),
        },
        "max_drawdown_pct": max_drawdown([b.close for b in window_bars]),
        "trend": _trend_label(last, moving_averages),
    }


def _trend_label(last: float, moving_averages: dict[str, dict[str, float | None]]) -> str:
    sma50 = moving_averages.get("sma50", {}).get("value")
    sma200 = moving_averages.get("sma200", {}).get("value")
    if sma50 is None or sma200 is None:
        return "insufficient history"
    if last > sma50 > sma200:
        return "uptrend"
    if last < sma50 < sma200:
        return "downtrend"
    return "mixed"


def compare(spx: list[Bar], spy: list[Bar]) -> dict[str, Any]:
    """SPY-vs-index relationship: the implied multiple and tracking gap."""
    if not spx or not spy:
        return {}
    aligned = _align(spx, spy)
    ratio = spx[-1].close / spy[-1].close if spy[-1].close else None
    result: dict[str, Any] = {
        "index_to_etf_ratio": ratio,
        "aligned_sessions": len(aligned),
    }
    if len(aligned) > 1:
        first_spx, first_spy = aligned[0][1], aligned[0][2]
        spx_ret = pct_change(aligned[-1][1], first_spx)
        spy_ret = pct_change(aligned[-1][2], first_spy)
        result["window_return_spx_pct"] = spx_ret
        result["window_return_spy_pct"] = spy_ret
        if spx_ret is not None and spy_ret is not None:
            # SPY trails the index by roughly its expense ratio plus dividend timing.
            result["tracking_gap_pct"] = spy_ret - spx_ret
    return result


def _align(spx: list[Bar], spy: list[Bar]) -> list[tuple[Date, float, float]]:
    spy_by_date = {b.date: b.close for b in spy}
    return [(b.date, b.close, spy_by_date[b.date]) for b in spx if b.date in spy_by_date]
