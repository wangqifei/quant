# Quant Dashboard — S&P 500 / SPY

Step one of a quantitative trading toolkit: a local dashboard that pulls the
S&P 500 index (SPX) and the SPY ETF, computes a standard metric set from the
daily history, and gives you a query panel on the right to ask about the price
action or record context the analysis should take into account.

![layout](docs/layout.png)

## Requirements

**Python 3.10 or newer.** Check with `python3 --version`.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh                      # → http://127.0.0.1:8000
```

That is the whole install. The dashboard and the query panel both work with
nothing else — no API key, no account.

### Optional: Claude-powered answers

The query panel ships with a built-in engine that answers from the computed
metrics. To also allow free-form questions, install the optional dependency
and set a key:

```bash
pip install -r requirements-assistant.txt   # needs Python 3.10+
export ANTHROPIC_API_KEY='<paste your real key here>'
./run.sh
```

Get the key from <https://console.anthropic.com/settings/keys>. It is a long
string beginning `sk-ant-api03-`; if what you exported is short, you have
pasted a placeholder rather than the key itself.

Skip this and everything still runs; the panel just stays on the local engine.

## What's on screen

**Left — market.** Live quote cards for SPX and SPY (last, change, previous
close, session range, YTD, distance from the 52-week high), an interactive
price chart with 1M/3M/6M/1Y/Max ranges and a hover readout of the OHLC for
any session, and a side-by-side metrics table.

**Right — query panel.** Two tabs:

- **Ask** — questions about the loaded snapshot: *"how far is SPX from its
  200-day?"*, *"what's realized vol?"*, *"compare SPX and SPY year to date"*.
- **Context** — notes you save (positions, theses, constraints). They persist
  to `data/context.json` and are sent with every question.

### Two answer engines

Pick one with the **Auto / Claude / Local** selector under the composer:

| Mode | Behaviour |
|---|---|
| **Auto** (default) | Ask Claude when it is configured; fall back to local metrics with a note if not, or if the call fails. |
| **Claude** | Always ask the model. If it is not configured or the call fails, you get an error saying why — never a local answer dressed up as a model answer. |
| **Local** | Answer from the computed metrics. No API call, no key, works offline. Every number is reproducible from the same snapshot. |

Answers are labelled with the engine, the model and how long the call took.

**To enable the Claude engine** (this is the "server model"):

```bash
pip install -r requirements-assistant.txt   # needs Python 3.10+
export ANTHROPIC_API_KEY='<paste your real key here>'   # in the shell that runs ./run.sh
./run.sh
```

The key is read by the **server** process, not the browser, so it never
reaches the page. Check it took effect at `/api/health`: `claude_available`
should be `true`, and `claude_unavailable_reason` tells you what is missing
when it is not.

#### The panel says the key was rejected (HTTP 401)

```bash
python -m app.diagnose --probe-model
```

Checks the key's shape for the usual paste damage (kept quotes, a trailing
newline, truncation), then authenticates against the models endpoint — which
validates credentials without generating any tokens — and confirms the
configured model is available to your account. It never prints the whole key.

#### Which model am I querying?

`claude-opus-5` by default. Override with `ANTHROPIC_MODEL`.

```bash
curl -sS localhost:8000/api/health          # the model the app will request
```

Every answer is labelled with the model that **actually served it**, read
from the API response rather than from configuration. These can differ:
server-side refusal fallbacks are enabled, so a declined request may be
answered by a different model. When that happens the reply says so instead of
crediting the answer to the model that declined it.

## Metrics computed

Returns (1d/1w/1m/3m/6m/1y/YTD) · SMA 20/50/200 and distance from each · trend
classification · annualised realized volatility (20d, 60d) · Wilder RSI(14) ·
52-week high/low and distance from each · max drawdown · SPX↔SPY price ratio
and tracking gap · next-session expected range.

**Expected range** answers "how high/low could it go tomorrow" by scaling
recent realized volatility down to a one-day horizon and reporting 1σ (~68%)
and 2σ (~95%) bands around the last close. It is a *dispersion* estimate with
no directional view, it assumes normally distributed log returns, and it
therefore understates gap risk around news — the answer says so every time.

All of it lives in `app/analytics.py` as pure functions over a bar list, so
each number is unit-testable and reproducible.

## Data sources

Tried in order; the first that returns usable data wins.

1. **Yahoo Finance** (`query{1,2}.finance.yahoo.com/v8/finance/chart`) —
   intraday accurate, no API key. Hardened against Yahoo's bot defences:
   persistent session, cookie/crumb minting, browser headers, host
   alternation and exponential backoff on throttling.
2. **Stooq** (daily CSV) — end-of-day fallback, no API key.
3. **Demo** — a seeded synthetic random walk so the app runs fully offline.
   SPY is derived from the index series so the two stay coherent. The UI
   labels this **DEMO DATA** in amber and every answer says so.

Neither live source is an official market feed. Treat the data as indicative,
not as a system of record.

See **[docs/data-sources.md](docs/data-sources.md)** for the full evaluation,
including why Futu/moomoo is not wired in yet and what would justify it.

### Diagnosing the data feed

If the dashboard shows the amber **DEMO DATA** badge, both live providers
failed. To see which one and why:

```bash
python -m app.diagnose          # add -v for debug logging
```

It tries every provider against both instruments and prints the price, bar
count and latency on success, or the exact error on failure.

**If Yahoo returns `HTTP 429`**, Yahoo has recognised the client by its TLS
fingerprint — headers alone cannot disguise it. Install the impersonating
transport (curl_cffi only, no pandas):

```bash
pip install -r requirements-yahoo.txt
python -m app.diagnose --probe-yahoo    # per-step handshake report
```

If it still fails with impersonation, the limit is genuinely on your IP. A
Futu/moomoo account sidesteps it entirely — an authenticated broker feed is
not rate-limited the way public endpoints are:

```bash
pip install -r requirements-futu.txt
# start FutuOpenD and log in, then:
export QUANT_PROVIDERS=futu,yahoo,stooq,demo
python -m app.diagnose               # confirms the link and prints your quota
```

`docs/data-sources.md` covers setup, the index-code lookup
(`python -m app.diagnose --futu-codes`) and the alternatives
(yfinance, akshare, tushare, key-based APIs).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `QUANT_PROVIDERS` | `yahoo,stooq,demo` | Provider chain, in priority order (`futu` also available) |
| `YAHOO_IMPERSONATE` | `auto` | `auto` / `off` / a curl_cffi target like `chrome` |
| `FUTU_HOST` / `FUTU_PORT` | `127.0.0.1` / `11111` | FutuOpenD gateway address |
| `FUTU_SECURITY_FIRM` | `FUTUSECURITIES` | `FUTUSECURITIES`, `FUTUINC` or `FUTUSG` |
| `FUTU_CODE_SPX` / `FUTU_CODE_SPY` | `US.SPX` / `US.SPY` | Futu ticker spellings |
| `QUANT_LOOKBACK_DAYS` | `420` | Calendar days of history to request |
| `QUANT_CACHE_TTL` | `60` | Seconds before a symbol is refetched |
| `ANTHROPIC_API_KEY` | — | Enables the Claude engine |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Model the assistant requests |
| `ANTHROPIC_MAX_TOKENS` | `16000` | Response cap (thinking tokens count toward it) |
| `PORT` | `8000` | Server port |

Force offline mode with `QUANT_PROVIDERS=demo ./run.sh`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Status, provider chain, assistant availability |
| `GET` | `/api/market?refresh=&history=` | Quotes, metrics and bars for both instruments |
| `GET` | `/api/market/{symbol}` | One instrument (`spx` or `spy`) |
| `POST` | `/api/ask` | `{question, history?, engine?}` → answer |
| `GET`/`POST` | `/api/context` | List / add a context note |
| `DELETE` | `/api/context/{id}` | Remove a note |

## Layout

```
app/
  config.py      environment-driven settings
  models.py      Bar / Quote / Series
  providers.py   Yahoo, Stooq and demo data sources
  analytics.py   pure metric functions
  market.py      provider fallback, caching, snapshot assembly
  assistant.py   context store, local engine, Claude engine
  main.py        FastAPI routes + static hosting
  diagnose.py    `python -m app.diagnose` provider connectivity check
web/             dashboard (no build step, no external JS)
tests/           pytest suite
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Without the optional `anthropic` package installed, the two tests that exercise
the Claude client skip and the rest still run.

Coverage includes the metric maths against hand-computed values, provider
parsing from captured payload fixtures (including Yahoo's null-padded holiday
sessions and Stooq's throttle pages), provider fallback and stale-cache
behaviour, question routing, and the Claude request shape verified at the wire
level against a local mock server — no live API calls, no key needed.

## Scope and caveats

- Prices are indicative; SPY returns here exclude dividends, so the tracking
  gap against the index is expected to be slightly negative.
- The assistant describes what the data shows. It is not investment advice and
  does not issue buy/sell recommendations.
- Context notes are stored unencrypted in `data/context.json` (gitignored).

## Next steps

Natural extensions from here: intraday bars, additional instruments, a
backtesting harness over the same `Bar` series, signal generation, and
position/PnL tracking wired into the context panel.
