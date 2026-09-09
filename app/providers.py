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
import socket
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
    "SPX": {"name": "S&P 500 Index", "yahoo": "^GSPC", "stooq": "^spx", "futu": "US.SPX"},
    "SPY": {"name": "SPDR S&P 500 ETF", "yahoo": "SPY", "stooq": "spy.us", "futu": "US.SPY"},
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

# Yahoo also fingerprints the TLS handshake, which no pure-Python client can
# disguise. curl_cffi speaks with a real browser's fingerprint; it is optional,
# and its absence just means we fall back to httpx.
DEFAULT_IMPERSONATE = "chrome"

try:  # pragma: no cover - depends on whether the extra is installed
    from curl_cffi.requests.exceptions import RequestException as _CurlError

    TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, _CurlError)
    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    TRANSPORT_ERRORS = (httpx.HTTPError,)
    HAVE_CURL_CFFI = False


def make_yahoo_session(impersonate: str = "auto", timeout: float = 10.0):
    """Build the HTTP session Yahoo requests go through.

    ``impersonate`` is ``"auto"`` (use curl_cffi when installed), ``"off"``
    (always httpx), or a curl_cffi browser target such as ``"chrome"``.
    Returns ``(session, description)``.
    """
    if impersonate != "off":
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            if impersonate != "auto":
                raise ProviderError(
                    f"impersonate={impersonate!r} needs curl_cffi - "
                    "`pip install -r requirements-yahoo.txt` (Python 3.10+)"
                ) from None
        else:
            target = DEFAULT_IMPERSONATE if impersonate == "auto" else impersonate
            # No custom headers: impersonation supplies a browser-consistent set,
            # and overriding pieces of it weakens the disguise.
            return curl_requests.Session(impersonate=target), f"curl_cffi/{target}"

    return (
        httpx.Client(headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=True),
        "httpx",
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
        client=None,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
        impersonate: str = "auto",
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.impersonate = impersonate
        self._client = client
        self._owns_client = client is None
        self.transport = "injected" if client is not None else "?"
        # None = not yet attempted; "" = attempted and unavailable.
        self._crumb: str | None = None

    # -- session ---------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            self._client, self.transport = make_yahoo_session(self.impersonate, self.timeout)
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
        except TRANSPORT_ERRORS as exc:
            log.debug("yahoo cookie bootstrap failed: %s", exc)
        try:
            resp = self.client.get(self.CRUMB_URL)
        except TRANSPORT_ERRORS as exc:
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
            except TRANSPORT_ERRORS as exc:
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

        raise ProviderError(
            f"chart request failed for {symbol} via {self.transport} ({'; '.join(errors)})"
        )

    def _request(self, host: str, symbol: str, params: dict[str, str]):
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
        # NOT meta.chartPreviousClose: that is the close before the *chart
        # range* begins, so on a 2y range it is two years stale and yields an
        # absurd daily change. Derive the prior close from the bars instead,
        # which also keeps quote.change_pct consistent with the 1d figure in
        # metrics.returns. Only fall back to meta when there is no prior bar.
        prev_close = None
        if len(bars) < 2:
            prev_close = meta.get("regularMarketPreviousClose") or meta.get("previousClose")

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
            # Stooq answers 200 with an HTML page or a plain-text limit notice
            # when it throttles, so show what actually arrived.
            preview = " ".join(text.split())[:120] or "(empty response)"
            raise ProviderError(f"no usable rows for {key}; response began: {preview!r}")
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


class FutuProvider(Provider):
    """Futu / moomoo OpenAPI, via a locally running FutuOpenD gateway.

    Unlike the public scrapers this is an authenticated broker feed, so it is
    not subject to the IP throttling that makes Yahoo return 429. It does need
    the FutuOpenD daemon running and logged in - see docs/data-sources.md.

    Historical bars are quota-metered per account; ``quota()`` reports what is
    left. Nothing here is imported at module load, so ``futu-api`` stays an
    optional dependency.
    """

    name = "futu"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        security_firm: str = "FUTUSECURITIES",
        codes: dict[str, str] | None = None,
        context_factory=None,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.security_firm = security_firm
        self.connect_timeout = connect_timeout
        self.codes = codes or {k: v["futu"] for k, v in INSTRUMENTS.items()}
        self._context_factory = context_factory
        self._ctx = None

    # -- connection ------------------------------------------------------

    def _context(self):
        if self._ctx is None:
            self._ctx = (self._context_factory or self._open_context)()
        return self._ctx

    def _assert_gateway_listening(self) -> None:
        """Fail fast if FutuOpenD is not up.

        OpenQuoteContext retries a dead gateway indefinitely, which would hang
        the dashboard and the diagnostic, so probe the port first.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=self.connect_timeout):
                return
        except OSError as exc:
            raise ProviderError(
                f"nothing is listening on {self.host}:{self.port} ({exc}). "
                "Start the FutuOpenD gateway and log in, then retry."
            ) from exc

    def _open_context(self):
        try:
            from futu import OpenQuoteContext, SecurityFirm
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "futu-api is not installed - `pip install -r requirements-futu.txt`"
            ) from exc

        self._assert_gateway_listening()
        firm = getattr(SecurityFirm, self.security_firm, None)
        if firm is None:
            valid = [n for n in dir(SecurityFirm) if n.isupper()]
            raise ProviderError(f"unknown security_firm {self.security_firm!r}; expected one of {valid}")
        try:
            return OpenQuoteContext(host=self.host, port=self.port, security_firm=firm)
        except Exception as exc:  # noqa: BLE001 - surfaces as "is FutuOpenD running?"
            raise ProviderError(
                f"cannot reach FutuOpenD at {self.host}:{self.port} ({exc}). "
                "Start the FutuOpenD gateway and log in first."
            ) from exc

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
            self._ctx = None

    # -- queries ---------------------------------------------------------

    def quota(self) -> tuple[int, int]:
        """``(used, remaining)`` historical-K-line quota for this account."""
        ret, data = _futu_unpack(self._context().get_history_kl_quota(get_detail=False))
        used, remaining = data[0], data[1]
        return used, remaining

    def fetch(self, key: str, lookback_days: int) -> Series:
        code = self.codes.get(key) or INSTRUMENTS[key]["futu"]
        end = Date.today()
        start = end - timedelta(days=lookback_days)
        rows = self._paged_kline(code, start.isoformat(), end.isoformat())
        bars = _bars_from_futu_rows(rows)
        if not bars:
            raise ProviderError(f"no bars returned for {code}")
        prev_close = rows[-1].get("last_close") if rows else None
        return Series(
            quote=_quote_from_bars(
                key, bars, self.name, previous_close=_as_float(prev_close)
            ),
            bars=bars,
        )

    def _paged_kline(self, code: str, start: str, end: str) -> list[dict]:
        # Open the context first: it raises the actionable "futu-api is not
        # installed" / "is FutuOpenD running?" error before this import can
        # surface a bare ModuleNotFoundError.
        ctx = self._context()
        from futu import AuType, KLType

        rows: list[dict] = []
        page_key = None
        for _ in range(20):  # bounded: 20 x 1000 bars is far more than we ask for
            ret, frame, page_key = _futu_unpack_kline(
                ctx.request_history_kline(
                    code,
                    start=start,
                    end=end,
                    ktype=KLType.K_DAY,
                    autype=AuType.QFQ,
                    max_count=1000,
                    page_req_key=page_key,
                )
            )
            rows.extend(frame)
            if not page_key:
                break
        return rows


def _futu_unpack(result):
    """Futu returns ``(ret, data)`` where data is an error string on failure."""
    ret, data = result
    if ret != 0:  # futu.RET_OK
        raise ProviderError(str(data))
    return ret, data


def _futu_unpack_kline(result) -> tuple[int, list[dict], object]:
    ret, data, page_key = result
    if ret != 0:
        raise ProviderError(str(data))
    rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data)
    return ret, rows, page_key


def _bars_from_futu_rows(rows: list[dict]) -> list[Bar]:
    bars: list[Bar] = []
    for row in rows:
        close = _as_float(row.get("close"))
        stamp = str(row.get("time_key") or "")[:10]
        if close is None or not stamp:
            continue
        try:
            day = Date.fromisoformat(stamp)
        except ValueError:
            continue
        bars.append(
            Bar(
                date=day,
                open=_as_float(row.get("open")) or close,
                high=_as_float(row.get("high")) or close,
                low=_as_float(row.get("low")) or close,
                close=close,
                volume=int(_as_float(row.get("volume")) or 0),
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def _as_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # reject NaN


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
    registry = {
        "yahoo": YahooProvider,
        "stooq": StooqProvider,
        "futu": FutuProvider,
        "demo": DemoProvider,
    }
    return [_build(registry[n]) for n in names if n in registry]


def _build(cls: type[Provider]) -> Provider:
    """Instantiate a provider, wiring in any settings it needs."""
    if cls is YahooProvider:
        from .config import settings

        return YahooProvider(impersonate=settings.yahoo_impersonate)
    if cls is FutuProvider:
        from .config import settings

        return FutuProvider(
            host=settings.futu_host,
            port=settings.futu_port,
            security_firm=settings.futu_security_firm,
            codes=settings.futu_codes,
        )
    return cls()
