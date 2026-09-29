"""Print every indicator the app computes, cross-checked independently.

    python -m app.report                      # both instruments, default windows
    python -m app.report --symbol SPX --windows 5,10,30,100

Each value is recomputed with pandas - a separate implementation of the same
definitions - and the two must agree. It exists so the numbers on the
dashboard can be audited rather than taken on trust, and so they can be
compared against a charting tool on the same data.
"""

from __future__ import annotations

import argparse
import math
import sys

from . import analytics
from .config import settings
from .market import MarketService
from .providers import INSTRUMENTS, ProviderError, build_providers

TOLERANCE = 1e-6  # relative


# -- independent reference implementations (pandas) ------------------------

def _pandas():
    try:
        import pandas as pd
    except ImportError:
        return None
    return pd


def ref_sma(pd, closes, w):
    if len(closes) < w:
        return None
    return float(pd.Series(closes).rolling(w).mean().iloc[-1])


def ref_ema(pd, closes, w):
    if len(closes) < w:
        return None
    # Same definition as the app: seed with the SMA of the first window,
    # then recurse with alpha = 2 / (w + 1).
    seeded = [sum(closes[:w]) / w] + list(closes[w:])
    return float(pd.Series(seeded).ewm(alpha=2 / (w + 1), adjust=False).mean().iloc[-1])


def ref_vol(pd, closes, w):
    rets = pd.Series(closes).apply(math.log).diff().dropna().tail(w)
    if len(rets) < 2:
        return None
    return float(rets.std(ddof=1) * math.sqrt(analytics.TRADING_DAYS) * 100)


def ref_rsi(pd, closes, n):
    if len(closes) <= n:
        return None
    delta = pd.Series(closes).diff().dropna()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
    # Wilder: seed with the simple mean of the first n, then alpha = 1/n.
    g = pd.Series([gain.iloc[:n].mean()] + list(gain.iloc[n:])).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    l = pd.Series([loss.iloc[:n].mean()] + list(loss.iloc[n:])).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    if l == 0:
        return 100.0
    return float(100 - 100 / (1 + g / l))


def ref_return(pd, closes, sessions):
    if len(closes) <= sessions:
        return None
    return float(pd.Series(closes).pct_change(sessions).iloc[-1] * 100)


# -- report -----------------------------------------------------------------

def _agree(a, b) -> str:
    if a is None and b is None:
        return "n/a"
    if a is None or b is None:
        return "MISMATCH"
    scale = max(abs(a), abs(b), 1e-12)
    return "ok" if abs(a - b) / scale <= TOLERANCE else "MISMATCH"


def _num(v, unit=""):
    return "insufficient data" if v is None else f"{v:,.2f}{unit}"


def report(symbol: str, bars, windows: list[int], pd) -> int:
    closes = [b.close for b in bars]
    last = closes[-1]
    rows: list[tuple[str, float | None, float | None]] = []

    for w in windows:
        rows.append((f"SMA {w}", analytics.sma(closes, w), pd and ref_sma(pd, closes, w)))
    for w in windows:
        rows.append((f"EMA {w}", analytics.ema(closes, w), pd and ref_ema(pd, closes, w)))
    for w in sorted({10, 20, 30, 60, 90} | set(windows)):
        rows.append((f"Realized vol {w}d %", analytics.realized_vol(closes, w), pd and ref_vol(pd, closes, w)))
    for n in (7, 14, 21):
        rows.append((f"RSI {n}", analytics.rsi(closes, n), pd and ref_rsi(pd, closes, n)))
    for label, n in analytics.RETURN_WINDOWS:
        rows.append((f"Return {label} %", analytics.pct_change(last, closes[-n - 1]) if len(closes) > n else None,
                     pd and ref_return(pd, closes, n)))

    print(f"\n=== {symbol} - {INSTRUMENTS[symbol]['name']} ===")
    print(f"{len(bars)} daily bars, {bars[0].date} -> {bars[-1].date}, last close {last:,.2f}")
    header = f"  {'indicator':22} {'app':>18} {'independent':>18}  check"
    print(header)
    print("  " + "-" * (len(header) - 2))
    mismatches = 0
    for name, mine, theirs in rows:
        verdict = _agree(mine, theirs) if pd else "-"
        mismatches += verdict == "MISMATCH"
        print(f"  {name:22} {_num(mine):>18} {(_num(theirs) if pd else 'pandas n/a'):>18}  {verdict}")

    window = bars[-analytics.TRADING_DAYS:]
    print(f"\n  52-week high / low     {max(b.high for b in window):,.2f} / {min(b.low for b in window):,.2f}")
    print(f"  Max drawdown (52w)     {_num(analytics.max_drawdown([b.close for b in window]), '%')}")
    band = analytics.expected_range(closes)
    if band:
        for b in band["bands"]:
            print(f"  Next-session {b['sigma']:.0f}σ       {b['low']:,.2f} - {b['high']:,.2f}  (~{b['probability_pct']:.0f}%)")
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print and cross-check every indicator.")
    parser.add_argument("--symbol", default="all", help="SPX, SPY or all")
    parser.add_argument("--windows", default="5,10,20,30,50,100,200", help="moving-average windows")
    parser.add_argument("--providers", help="override the provider chain, e.g. demo")
    args = parser.parse_args(argv)

    windows = sorted({int(w) for w in args.windows.split(",") if w.strip()})
    names = [p.strip() for p in args.providers.split(",")] if args.providers else settings.providers
    service = MarketService(settings, providers=build_providers(names))
    symbols = list(INSTRUMENTS) if args.symbol.lower() == "all" else [args.symbol.upper()]

    pd = _pandas()
    if pd is None:
        print("pandas not installed - showing app values without the independent check "
              "(pip install pandas to enable it)")

    total = 0
    for sym in symbols:
        try:
            series, errors = service.series(sym)
        except (ProviderError, KeyError) as exc:
            print(f"{sym}: {exc}", file=sys.stderr)
            return 2
        src = series.quote.source
        print(f"\nsource: {src}" + ("   <-- SYNTHETIC DEMO DATA, not the market" if src == "demo" else ""))
        total += report(sym, series.bars, windows, pd)

    if pd:
        print(f"\n{'ALL INDICATORS AGREE' if total == 0 else f'{total} MISMATCH(ES)'} with the independent implementation.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
