# Market data sources

Findings from evaluating Yahoo, Stooq, Futu/moomoo, tushare, akshare and
yfinance as the price feed for this project, and why the chain is what it is.

## Current chain

`QUANT_PROVIDERS=yahoo,stooq,demo` — tried in order, first usable wins.

Run `python -m app.diagnose` to see which of them actually works from your
machine, and the exact error when one doesn't.

## Yahoo Finance (primary)

Endpoint: `https://query{1,2}.finance.yahoo.com/v8/finance/chart/{symbol}`

Free, no account, no key, and it carries both the index (`^GSPC`) and the ETF
(`SPY`) with intraday-accurate last prices. It is not an official feed and
Yahoo may change or throttle it without notice.

Yahoo actively discourages non-browser clients, so `YahooProvider` does the
following rather than issuing a bare GET:

| Measure | Why |
|---|---|
| One reused `httpx.Client` | Keeps the session cookie across requests |
| Cookie + crumb mint (`fc.yahoo.com` → `/v1/test/getcrumb`) | Some endpoints reject requests without a crumb |
| Browser headers (UA, Accept, Accept-Language, Origin, Referer) | Bare clients get 403s |
| `query1` ↔ `query2` alternation per attempt | The two hosts throttle independently |
| Exponential backoff on 429/5xx | Throttling is usually transient |
| Re-mint the crumb on 401/403 | Crumbs expire with the session |
| Fail fast on other 4xx | A bad symbol shouldn't burn the retry budget |

**Known remaining limitation.** Yahoo also fingerprints TLS handshakes. If you
are throttled persistently (repeated `HTTP 429` from `app.diagnose`) rather
than intermittently, a plain HTTP client cannot fix it — `yfinance` works
around this with `curl_cffi` TLS impersonation. Adopting that means taking on
pandas, numpy, curl_cffi and roughly a dozen transitive dependencies, which is
why it is not the default. It remains the escape hatch if throttling becomes a
real problem.

## Stooq (fallback)

Endpoint: `https://stooq.com/q/d/l/?s={symbol}&i=d` → daily CSV.

End-of-day only, so no intraday last price, but it is stable, needs no
account, and covers `^spx` and `spy.us`. Good enough to keep the dashboard
populated when Yahoo is throttling. It serves an HTML page instead of CSV when
rate-limited, which the parser detects and rejects.

## Futu / moomoo — supported (`futu` provider)

Futu Holdings runs both brands: **moomoo** internationally, **Futu Niuniu** in
HK/CN. One shared OpenAPI. **A personal account is enough.**

This is the recommended fix if the public endpoints are throttling you: it is
an *authenticated broker feed reached over localhost*, so it is not subject to
the IP-based rate limiting that makes Yahoo return 429.

Verified against `futu-api` 10.10.7008:

- **Local gateway, not a REST API.** You run **FutuOpenD**, which holds the
  session; your code talks to it on `127.0.0.1:11111`. There is no
  keys-only/serverless path — the daemon must be running.
- **Historical bars are quota-metered.** `get_history_kl_quota()` reports
  used/remaining; `app.diagnose` prints it after a successful check.
- **Symbols** are `MARKET.CODE` (`US.SPY`). The SDK documents no US *index*
  code, so `FUTU_CODE_SPX` defaults to `US.SPX` and is overridable —
  `python -m app.diagnose --futu-codes` asks your gateway what it actually
  offers, rather than guessing.
- **Bar fields**: `time_key, open, close, high, low, volume, last_close`.

### Setup

1. Download and run **FutuOpenD**, log in with your Futu/moomoo credentials.
2. `pip install -r requirements-futu.txt`
3. `export QUANT_PROVIDERS=futu,yahoo,stooq,demo`
4. `python -m app.diagnose` — confirms the connection and prints your quota.

If the S&P 500 index fails but SPY works, your account uses a different index
code: run `python -m app.diagnose --futu-codes` and
`export FUTU_CODE_SPX=<code>`.

Settings: `FUTU_HOST`, `FUTU_PORT`, `FUTU_SECURITY_FIRM`
(`FUTUSECURITIES` / `FUTUINC` / `FUTUSG` — must match your broker entity),
`FUTU_CODE_SPX`, `FUTU_CODE_SPY`.

The provider probes the TCP port before constructing the SDK context, because
`OpenQuoteContext` retries a dead gateway indefinitely and would otherwise
hang the dashboard.

## Other sources evaluated

### tushare — wrong tool for US markets

`tushare` 1.4.29's Pro client is a **generic dispatcher**: `__getattr__`
forwards any method name to `POST api.waditu.com/dataapi`, so the package
itself declares no endpoints and coverage is decided server-side by your
token's points (积分) balance. The bundled modules are China-focused (shibor,
A-share billboard/fundamentals, domestic macro).

It is a good source **for A-shares**, which this project does not track. For
SPX/SPY it is the wrong tool. Requires `ts.set_token(...)` and enough points
for whichever endpoint you call. *Not verified against the live API — the
host is unreachable from the dev sandbox.*

### akshare — viable, but Python 3.11+

`akshare` 1.18.94 needs **no token** and does carry US data on infrastructure
completely independent of Yahoo (Sina / EastMoney):

- `index_us_stock_sina(symbol=".INX")` — the S&P 500 index
- `stock_us_daily(symbol="SPY")` — US daily bars

Two catches: it declares `Requires-Python: >=3.11`, and the Sina index feed
returns a JS-obfuscated blob that needs `py_mini_racer` (a JS engine) to
decode. Worth revisiting as a no-account fallback if you move to 3.11+.

### yfinance — the no-account answer to Yahoo 429

`yfinance` 1.7.0 does two things a plain HTTP client cannot: it mints Yahoo's
cookie/crumb pair, and uses **`curl_cffi` to impersonate a browser TLS
fingerprint**. If Yahoo is throttling by fingerprint, this defeats it; if the
block is purely per-IP, it will not. Costs ~12 transitive dependencies
(pandas, numpy, curl_cffi, peewee, lxml, bs4, websockets).

### Key-based APIs (not evaluated in depth)

Alpha Vantage, Finnhub, Twelve Data, Polygon, Tiingo and FRED all offer free
tiers covering US equities, and none is subject to Yahoo's throttling. FRED in
particular publishes a daily S&P 500 close series and is extremely reliable,
though it carries the close only — no OHLC, and no SPY. Their free-tier limits
change often, so check current terms before depending on one; they were not
reachable from the dev sandbox to verify.

## Recommendation

| Situation | Use |
|---|---|
| Yahoo works | `yahoo,stooq,demo` (the default) |
| Yahoo returns 429 and you have a Futu/moomoo account | **`futu,yahoo,stooq,demo`** |
| Yahoo returns 429, no broker account | Try `yfinance`; otherwise a key-based API |
| A-shares later | `tushare` or `akshare` |
| No network at all | `demo` |
