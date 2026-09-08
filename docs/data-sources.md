# Market data sources

Findings from evaluating Yahoo Finance, Stooq and Futu/moomoo as the price
feed for this project, and why the current chain is what it is.

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

## Futu / moomoo — evaluated, not adopted (yet)

Futu Holdings operates both brands: **moomoo** internationally, **Futu
Niuniu** in HK/CN. They share one OpenAPI.

Verified against `futu-api` 10.10.7008 from PyPI:

- **Architecture is a local gateway, not a REST API.** You run **FutuOpenD**
  (a desktop daemon) which holds the authenticated session; your code talks to
  it over `127.0.0.1:11111`. Confirmed from the SDK:
  `OpenQuoteContext(host='127.0.0.1', port=11111, ..., security_firm=SecurityFirm.NONE)`.
  There is no keys-only, serverless path — the daemon must be running.
- **Account required**, and `security_firm` must match your broker entity
  (`FUTUSECURITIES` / `FUTUINC` / `FUTUSG`).
- **Historical bars are metered.** `request_history_kline(code, start, end,
  ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000, page_req_key=...)`
  returns a paginated pandas DataFrame, and the SDK ships
  `get_history_kl_quota()` specifically to report used/remaining quota.
  Quota depends on your account tier and market-data entitlements.
- **Realtime needs a subscription**: `subscribe(code_list, subtype_list)` then
  `get_stock_quote(...)` / `get_cur_kline(...)`. US realtime generally requires
  paid market data or qualifying assets/activity.
- **Symbol format** is `MARKET.CODE` — `US.SPY`, `HK.00700`, `SH.000001`.
- **Dependency footprint**: pandas, numpy, protobuf, pycryptodome, simplejson.

### When Futu becomes the right call

Not for daily-bar research — Yahoo is simpler and unmetered, and the quota
would work against backtesting, which re-reads history constantly.

It becomes clearly worth it when you want:

- intraday or L2/order-book depth,
- data consistent with the broker you actually trade through, or
- **order execution from the same API** (`OpenSecTradeContext`), so signal and
  execution share one session.

That last point is the real argument: it collapses the research-to-execution
gap into one integration. When the project reaches live order placement, a
`FutuProvider` alongside the existing chain is the natural step — the
`Provider` interface already accommodates it, and `futu-api` would go in an
optional requirements file so the core install stays light.

### If you want to try it manually

1. Download and run **FutuOpenD**, log in with your Futu/moomoo credentials.
2. `pip install futu-api`
3. ```python
   from futu import OpenQuoteContext, KLType, AuType

   ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
   print(ctx.get_history_kl_quota(get_detail=True))
   ret, data, page = ctx.request_history_kline(
       "US.SPY", start="2025-01-01", end="2025-12-31",
       ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000,
   )
   print(ret, data.head() if ret == 0 else data)
   ctx.close()
   ```

Check the quota call first — it tells you how much history you can pull before
you design anything around it.
