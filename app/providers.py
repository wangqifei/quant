"""Market data providers.

Two live sources are supported, neither of which needs an API key:

* ``YahooProvider``  - the Yahoo Finance chart endpoint (primary). Mints a
  cookie/crumb pair, sends browser-like headers, alternates between the
  query1/query2 hosts and backs off on throttling.
* ``StooqProvider``  - Stooq daily CSV (fallback, end-of-day only).

``DemoProvider`` produces a deterministic synthetic series so the UI and the
test-suite work with no network access at all.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from datetime import date as Date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .models import Bar, Quote, Series

log = logging.getLogger(__name__)

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

# Yahoo rejects requests that do not look like they came from a browser, so
# send the same header set a real page load would.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
    "Connection": "keep-alive",
}

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


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
    """Yahoo Finance ``/v8/finance/chart`` - intraday-accurate, no API key.

    Yahoo throttles and blocks requests that do not look like a browser, so
    this provider:

    * reuses one :class:`httpx.Client` so the session cookie persists,
    * mints a cookie/crumb pair once and re-mints it if a request is rejected,
    * alternates between the ``query1``/``query2`` hosts across attempts, and
    * backs off exponentially on throttling and server errors.

    A non-retryable status (a bad symbol, say) fails immediately rather than
    burning the retry budget.
    """

    name = "yahoo"
    HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    CHART_PATH = "/v8/finance/chart/{symbol}"
    CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
    COOKIE_URL = "https://fc.yahoo.com/"

    def __init__(
        self,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self._client = client
        self._owns_client = client is None
        # None = not yet attempted; "" = attempted and unavailable.
        self._crumb: str | None = None

    # -- session ---------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers=BROWSER_HEADERS, timeout=self.timeout, follow_redirects=True
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _crumb_value(self) -> str | None:
        """The cached crumb, minting one on first use."""
        if self._crumb is None:
            self._crumb = self._mint_crumb() or ""
        return self._crumb or None

    def _mint_crumb(self) -> str:
        """Seed the session cookie, then exchange it for a crumb.

        Best-effort: the chart endpoint often works without a crumb, so a
        failure here is logged and ignored rather than raised.
        """
        try:
            self.client.get(self.COOKIE_URL)  # sets A1/A3; a 404 still sets them
        except httpx.HTTPError as exc:
            log.debug("yahoo cookie bootstrap failed: %s", exc)
        try:
            resp = self.client.get(self.CRUMB_URL)
        except httpx.HTTPError as exc:
            log.debug("yahoo crumb request failed: %s", exc)
            return ""
        crumb = resp.text.strip() if resp.status_code == 200 else ""
        # An HTML body means we were served an error/consent page, not a crumb.
        if not crumb or "<" in crumb or len(crumb) > 64:
            return ""
        return crumb

    # -- fetching --------------------------------------------------------

    def fetch(self, key: str, lookback_days: int) -> Series:
        symbol = INSTRUMENTS[key]["yahoo"]
        params = {"range": _yahoo_range(lookback_days), "interval": "1d"}
        return self._parse(key, self._get_chart(symbol, params))

    def _get_chart(self, symbol: str, params: dict[str, str]) -> dict:
        errors: list[str] = []
        delay = self.retry_delay

        for attempt in range(self.max_attempts):
            host = self.HOSTS[attempt % len(self.HOSTS)]
            retryable = True
            try:
                resp = self._request(host, symbol, params)
            except httpx.HTTPError as exc:
                errors.append(f"{host}: {type(exc).__name__}: {exc}")
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        errors.append(f"{host}: malformed JSON: {exc}")
                else:
                    errors.append(f"{host}: HTTP {resp.status_code}")
                    if resp.status_code in (401, 403):
                        self._crumb = None  # stale crumb: re-mint next attempt
                    elif resp.status_code not in RETRYABLE_STATUS:
                        retryable = False

            if not retryable:
                break
            if attempt < self.max_attempts - 1 and delay:
                time.sleep(delay)
                delay *= 2

        raise ProviderError(f"chart request failed for {symbol} ({'; '.join(errors)})")

    def _request(self, host: str, symbol: str, params: dict[str, str]) -> httpx.Response:
        query = dict(params)
        crumb = self._crumb_value()
        if crumb:
            query["crumb"] = crumb
        url = f"https://{host}{self.CHART_PATH.format(symbol=quote(symbol, safe=''))}"
        return self.client.get(url, params=query)

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
