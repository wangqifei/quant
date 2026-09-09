"""Provider connectivity check.

Run ``python -m app.diagnose`` to see, per provider, whether it can actually
reach its upstream and what it returned. Use it when the dashboard shows the
amber DEMO DATA badge and you want to know which source failed and why.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import settings
from .providers import (
    HAVE_CURL_CFFI,
    INSTRUMENTS,
    TRANSPORT_ERRORS,
    FutuProvider,
    Provider,
    ProviderError,
    YahooProvider,
    build_providers,
)

OK, FAIL = "  OK  ", " FAIL "


def check(provider: Provider, key: str, lookback_days: int) -> tuple[bool, str]:
    started = time.perf_counter()
    try:
        series = provider.fetch(key, lookback_days)
    except ProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - report anything a provider throws
        return False, f"{type(exc).__name__}: {exc}"

    elapsed = (time.perf_counter() - started) * 1000
    quote = series.quote
    return True, (
        f"{quote.price:,.2f} ({quote.change_pct:+.2f}%) as of {quote.as_of} "
        f"| {len(series.bars)} bars | {elapsed:.0f} ms"
    )


def environment() -> str:
    """The facts needed to interpret everything below."""
    import platform

    optional = []
    for module, label in (("curl_cffi", "curl_cffi"), ("futu", "futu-api"), ("anthropic", "anthropic")):
        try:
            __import__(module)
            optional.append(f"{label} yes")
        except ImportError:
            optional.append(f"{label} NO")
    return (
        f"Python {platform.python_version()} on {platform.system()} {platform.machine()}\n"
        f"Optional packages: {', '.join(optional)}"
    )


def probe_yahoo() -> int:
    """Walk Yahoo's handshake one step at a time and report each status.

    The chain check only says "429". This says which request got the 429,
    what Yahoo sent back, and whether TLS impersonation is in play - which is
    what distinguishes a throttled IP from a recognisable client.
    """
    print(environment(), "\n")

    provider = YahooProvider(impersonate=settings.yahoo_impersonate)
    session = provider.client
    print(f"Transport: {provider.transport}")
    if not HAVE_CURL_CFFI:
        print("  curl_cffi is NOT installed - requests carry a Python TLS fingerprint,")
        print("  which Yahoo throttles. `pip install -r requirements-yahoo.txt` (needs 3.10+).")
    print()

    # Only the chart call decides the verdict. fc.yahoo.com always answers 404
    # - it exists to set the A1/A3 cookies, not to serve a page - and the crumb
    # is best-effort, since the chart endpoint usually works without one.
    steps = [
        ("cookie   ", YahooProvider.COOKIE_URL, None, False),
        ("crumb    ", YahooProvider.CRUMB_URL, None, False),
        (
            "chart SPY",
            f"https://{YahooProvider.HOSTS[0]}/v8/finance/chart/SPY",
            {"range": "5d", "interval": "1d"},
            True,
        ),
    ]
    chart_ok = False
    for label, url, params, decisive in steps:
        note = "" if decisive else "   (informational)"
        try:
            resp = session.get(url, params=params) if params else session.get(url)
        except TRANSPORT_ERRORS as exc:
            print(f"  {label}  ERROR  {type(exc).__name__}: {exc}{note}")
            continue

        body = (resp.text or "")[:110].replace("\n", " ")
        print(f"  {label}  HTTP {resp.status_code}  {body!r}{note}")
        for header in ("retry-after", "x-ratelimit-remaining", "content-type"):
            value = resp.headers.get(header)
            if value:
                print(f"    {header}: {value}")
        if decisive and resp.status_code == 200:
            chart_ok = True

    cookies = getattr(session, "cookies", None)
    names = sorted(getattr(cookies, "keys", lambda: [])())
    print(f"\n  cookies held: {names or 'none'}")
    provider.close()

    print()
    if chart_ok:
        print(
            "WORKING - Yahoo returned chart data over "
            f"{provider.transport}.\n"
            "Run `python -m app.diagnose` to confirm the whole chain, then start\n"
            "the app: the badge should read `live` instead of `demo data`."
        )
        return 0
    if not HAVE_CURL_CFFI:
        print("Install curl_cffi and re-run: pip install -r requirements-yahoo.txt")
    else:
        print(
            "Still blocked with browser impersonation, so the limit is on your IP\n"
            "rather than the client. Options: wait it out, try another network or\n"
            "VPN exit, or use a source that is not Yahoo (see docs/data-sources.md)."
        )
    return 1


def list_futu_codes() -> int:
    """Print the US index codes the connected Futu account can see.

    Futu's code for the S&P 500 index is not documented in the SDK, so this
    asks the gateway directly rather than guessing.
    """
    provider = FutuProvider(
        host=settings.futu_host,
        port=settings.futu_port,
        security_firm=settings.futu_security_firm,
    )
    try:
        from futu import Market, SecurityType
    except ImportError:
        print("futu-api is not installed - pip install -r requirements-futu.txt", file=sys.stderr)
        return 2
    try:
        ctx = provider._context()
        ret, data = ctx.get_stock_basicinfo(Market.US, SecurityType.IDX)
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        provider.close()

    if ret != 0:
        print(f"Futu returned an error: {data}", file=sys.stderr)
        return 2

    rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data)
    print(f"{len(rows)} US index codes available:\n")
    for row in rows:
        print(f"  {str(row.get('code','')):16} {row.get('name','')}")
    print("\nSet the S&P 500 one with:  export FUTU_CODE_SPX=<code>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check each market data provider.")
    parser.add_argument("--verbose", "-v", action="store_true", help="show debug logging")
    parser.add_argument(
        "--providers",
        help="comma-separated provider names to test (default: the configured chain)",
    )
    parser.add_argument(
        "--probe-yahoo",
        action="store_true",
        help="walk Yahoo's cookie/crumb/chart handshake and report each status",
    )
    parser.add_argument(
        "--futu-codes",
        action="store_true",
        help="list the US index codes your Futu account exposes, then exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.probe_yahoo:
        return probe_yahoo()
    if args.futu_codes:
        return list_futu_codes()

    names = [p.strip() for p in args.providers.split(",")] if args.providers else settings.providers
    providers = build_providers(names)
    if not providers:
        print(f"No known providers in {names!r}.", file=sys.stderr)
        return 2

    print(environment())
    print(f"Lookback: {settings.lookback_days} days   Chain: {', '.join(p.name for p in providers)}\n")

    working: list[str] = []
    for provider in providers:
        print(f"{provider.name}")
        provider_ok = True
        for key in INSTRUMENTS:
            ok, detail = check(provider, key, settings.lookback_days)
            provider_ok &= ok
            print(f"  [{OK if ok else FAIL}] {key:4} {detail}")
        if isinstance(provider, FutuProvider) and provider_ok:
            try:
                used, remaining = provider.quota()
                print(f"  history quota: {used} used, {remaining} remaining")
            except Exception as exc:  # noqa: BLE001
                print(f"  history quota: unavailable ({exc})")
        if provider_ok and provider.name != "demo":
            working.append(provider.name)
        if hasattr(provider, "close"):
            provider.close()
        print()

    if working:
        print(f"Live data available from: {', '.join(working)}")
        return 0
    print(
        "No live provider is reachable - the dashboard will show synthetic DEMO DATA.\n"
        "Common causes: a firewall or VPN blocking the host, Yahoo throttling this IP\n"
        "(the errors above will show HTTP 429), or no outbound internet access."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
