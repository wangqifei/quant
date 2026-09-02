"""Market data providers.

Two live sources are supported, neither of which needs an API key:

* ``YahooProvider``  - the Yahoo Finance chart endpoint (primary).
* ``StooqProvider``  - Stooq daily CSV (fallback, end-of-day only).

``DemoProvider`` produces a deterministic synthetic series so the UI and the
test-suite work with no network access at all.
"""

from __future__ import annotations

import csv
import io
import math
import random
from abc import ABC, abstractmethod
from datetime import date as Date, datetime, timedelta, timezone

import httpx

from .models import Bar, Quote, Series

# Instruments the dashboard tracks. ``yahoo``/``stooq`` are the per-provider
# ticker spellings for the same instrument.
INSTRUMENTS: dict[str, dict[str, str]] = {
    "SPX": {"name": "S&P 500 Index", "yahoo": "^GSPC", "stooq": "^spx"},
    "SPY": {"name": "SPDR S&P 500 ETF", "yahoo": "SPY", "stooq": "spy.us"},
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce usable data."""


class Provider(ABC):
    """Fetches a daily series for one of the keys in :data:`INSTRUMENTS`."""

    name: str

    @abstractmethod
    def fetch(self, key: str, lookback_days: int) -> Series:
        """Return a :class:`Series` for ``key`` covering ~``lookback_days``."""


def _quote_from_bars(key: str, bars: list[Bar], source: str, **overrides) -> Quote:
    """Build a Quote from the tail of a bar series.

    Error messages here carry no provider prefix - MarketService adds the
    name of whichever provider raised.
    """
    if not bars:
        raise ProviderError(f"no bars returned for {key}")
    last = bars[-1]
    prev_close = bars[-2].close if len(bars) > 1 else last.open
    fields = {
        "symbol": key,
        "name": INSTRUMENTS[key]["name"],
        "price": last.close,
        "previous_close": prev_close,
        "as_of": last.date,
        "source": source,
        "day_open": last.open,
        "day_high": last.high,
        "day_low": last.low,
        "volume": last.volume,
    }
    fields.update({k: v for k, v in overrides.items() if v is not None})
    return Quote(**fields)


class YahooProvider(Provider):
    """Yahoo Finance ``/v8/finance/chart`` - intraday-accurate, no API key."""

    name = "yahoo"
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def fetch(self, key: str, lookback_days: int) -> Series:
        symbol = INSTRUMENTS[key]["yahoo"]
        params = {"range": _yahoo_range(lookback_days), "interval": "1d"}
        try:
            resp = httpx.get(
                self.BASE.format(symbol=symbol),
                params=params,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"request failed for {symbol}: {exc}") from exc
        return self._parse(key, payload)

    def _parse(self, key: str, payload: dict) -> Series:
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise ProviderError(f"upstream error: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            raise ProviderError(f"empty result for {key}")
        result = results[0]
        meta = result.get("meta") or {}
        stamps = result.get("timestamp") or []
        quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]

        bars: list[Bar] = []
        for i, ts in enumerate(stamps):
            close = _at(quote_block.get("close"), i)
            if close is None:
                continue  # Yahoo pads holidays/halts with nulls
            bars.append(
                Bar(
                    date=datetime.fromtimestamp(ts, tz=timezone.utc).date(),
                    open=_at(quote_block.get("open"), i, close),
                    high=_at(quote_block.get("high"), i, close),
                    low=_at(quote_block.get("low"), i, close),
                    close=close,
                    volume=int(_at(quote_block.get("volume"), i, 0) or 0),
                )
            )
        if not bars:
            raise ProviderError(f"no usable bars for {key}")

        # meta carries the live (possibly intraday) print; prefer it over the
        # last daily bar's close.
        live = meta.get("regularMarketPrice")
        market_time = meta.get("regularMarketTime")
        as_of = (
            datetime.fromtimestamp(market_time, tz=timezone.utc).date()
            if market_time
            else bars[-1].date
        )
        prev_close = meta.get("chartPreviousClose")
        if live is not None and as_of == bars[-1].date:
            # Replace the stale final close with the live print.
            last = bars[-1]
            bars[-1] = Bar(last.date, last.open, max(last.high, live), min(last.low, live), float(live), last.volume)
        elif live is not None:
            bars.append(Bar(as_of, float(live), float(live), float(live), float(live), 0))

        quote = _quote_from_bars(
            key,
            bars,
            self.name,
            previous_close=float(prev_close) if prev_close else None,
            currency=meta.get("currency") or "USD",
        )
        return Series(quote=quote, bars=bars)


class StooqProvider(Provider):
    """Stooq daily CSV - end-of-day only, used when Yahoo is unavailable."""

    name = "stooq"
    BASE = "https://stooq.com/q/d/l/"

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def fetch(self, key: str, lookback_days: int) -> Series:
        symbol = INSTRUMENTS[key]["stooq"]
        try:
            resp = httpx.get(
                self.BASE,
                params={"s": symbol, "i": "d"},
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            text = resp.text
        except httpx.HTTPError as exc:
            raise ProviderError(f"request failed for {symbol}: {exc}") from exc
        return self._parse(key, text, lookback_days)

    def _parse(self, key: str, text: str, lookback_days: int) -> Series:
        rows = list(csv.DictReader(io.StringIO(text)))
        bars: list[Bar] = []
        for row in rows:
            try:
                bars.append(
                    Bar(
                        date=Date.fromisoformat(row["Date"]),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=int(float(row.get("Volume") or 0)),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue  # Stooq emits an HTML error page when throttled
        if not bars:
            raise ProviderError(f"no usable rows for {key}")
        bars.sort(key=lambda b: b.date)
        cutoff = bars[-1].date - timedelta(days=lookback_days)
        bars = [b for b in bars if b.date >= cutoff] or bars[-2:]
        return Series(quote=_quote_from_bars(key, bars, self.name), bars=bars)


class DemoProvider(Provider):
    """Deterministic synthetic data, so the app runs with no network.

    The index is a seeded geometric random walk anchored near recent real
    levels; the ETF is derived from it so the two instruments stay coherent
    (SPY tracks the index at roughly 1/10th its level). Output is clearly
    labelled in the UI and must never be mistaken for a live quote.
    """

    name = "demo"
    INDEX_ANCHOR = 5600.0
    ETF_DIVISOR = 10.05
    DAILY_VOL = 0.0085

    def fetch(self, key: str, lookback_days: int) -> Series:
        sessions = _session_dates(lookback_days)
        bars = self._index_bars(sessions, lookback_days)
        if key == "SPY":
            bars = self._derive_etf(bars, lookback_days)
        return Series(quote=_quote_from_bars(key, bars, self.name), bars=bars)

    def _index_bars(self, sessions: list[Date], lookback_days: int) -> list[Bar]:
        rng = random.Random(f"index:{lookback_days}")
        price = self.INDEX_ANCHOR * math.exp(-0.00025 * len(sessions))
        bars: list[Bar] = []
        for day in sessions:
            open_ = price
            close = open_ * math.exp(rng.gauss(0.0003, self.DAILY_VOL))
            high = max(open_, close) * (1 + abs(rng.gauss(0, 0.0025)))
            low = min(open_, close) * (1 - abs(rng.gauss(0, 0.0025)))
            bars.append(
                Bar(day, round(open_, 2), round(high, 2), round(low, 2), round(close, 2), int(rng.uniform(2.5e9, 4.5e9)))
            )
            price = close
        return bars

    def _derive_etf(self, index_bars: list[Bar], lookback_days: int) -> list[Bar]:
        rng = random.Random(f"etf:{lookback_days}")
        out: list[Bar] = []
        for bar in index_bars:
            noise = 1 + rng.gauss(0, 0.0002)  # small tracking difference
            scale = noise / self.ETF_DIVISOR
            out.append(
                Bar(
                    bar.date,
                    round(bar.open * scale, 2),
                    round(bar.high * scale, 2),
                    round(bar.low * scale, 2),
                    round(bar.close * scale, 2),
                    int(rng.uniform(4e7, 9e7)),
                )
            )
        return out


def _session_dates(lookback_days: int) -> list[Date]:
    """Weekday dates ending today, spanning ``lookback_days`` calendar days."""
    today = Date.today()
    days = [today - timedelta(days=i) for i in range(lookback_days)]
    return sorted(d for d in days if d.weekday() < 5)


def _yahoo_range(lookback_days: int) -> str:
    for days, label in ((5, "5d"), (31, "1mo"), (93, "3mo"), (186, "6mo"), (370, "1y"), (740, "2y")):
        if lookback_days <= days:
            return label
    return "5y"


def _at(seq, index: int, default=None):
    if not seq or index >= len(seq):
        return default
    value = seq[index]
    return default if value is None else value


def build_providers(names: list[str]) -> list[Provider]:
    """Instantiate providers in priority order, ignoring unknown names."""
    registry = {"yahoo": YahooProvider, "stooq": StooqProvider, "demo": DemoProvider}
    return [registry[n]() for n in names if n in registry]
