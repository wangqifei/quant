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

import re

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


def _key_shape(raw: str) -> list[str]:
    """Report suspicious characteristics of the key without printing it.

    A 401 from a key that "looks right" pasted into a terminal is usually one
    of these: surrounding quotes the shell kept, a trailing newline, or a
    truncated copy.
    """
    problems = []
    stripped_early = raw.strip().strip("\"'")
    # Placeholder text copied out of documentation instead of a real key.
    if (
        "..." in stripped_early
        or stripped_early.lower() in {"sk-ant-", "your-key", "your_api_key", "xxx", "changeme"}
        or re.fullmatch(r"[<{\[].*[>}\]]", stripped_early)
        or re.search(r"(your[_-]?(api[_-]?)?key|replace[_-]?me|paste[_-]?here)", stripped_early, re.I)
    ):
        problems.append(
            "is placeholder text from the docs, not a real key - copy the actual "
            "key from https://console.anthropic.com/settings/keys"
        )
        return problems  # no point reporting length or prefix on a placeholder

    if raw != raw.strip():
        problems.append("has leading/trailing whitespace or a newline - re-export without it")
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        problems.append("is wrapped in quotes - the shell kept them as part of the value")
    if any(c in stripped for c in " \t\n\r"):
        problems.append("contains an internal space or newline - it was probably line-wrapped on paste")
    if not stripped.startswith("sk-ant-"):
        problems.append(f"does not start with 'sk-ant-' (starts with {stripped[:7]!r})")
    if len(stripped) < 40:
        problems.append(f"is only {len(stripped)} characters - looks truncated")
    return problems


def probe_model() -> int:
    """Check the assistant's credentials without spending tokens.

    Uses the models endpoint, which authenticates but generates nothing, then
    confirms the configured model is actually available to the account.
    """
    import os

    print(environment(), "\n")

    try:
        import anthropic
    except ImportError:
        print("The `anthropic` package is not installed.")
        print("  pip install -r requirements-assistant.txt   (needs Python 3.10+)")
        return 2

    raw = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    has_profile = settings.has_oauth_profile

    # Credential precedence: API key, then auth token, then an `ant` profile.
    # A set key wins even when empty, which silently shadows a working profile.
    if raw is not None:
        source = "ANTHROPIC_API_KEY"
    elif token is not None:
        source = "ANTHROPIC_AUTH_TOKEN"
    elif has_profile:
        source = "ant auth login profile"
    else:
        source = None

    print(f"OAuth profile ({settings.anthropic_profile_dir}): {'present' if has_profile else 'none'}")
    print(f"Credential the SDK will use: {source or 'NONE'}\n")

    if source is None:
        print("No Anthropic credentials found. Two options:")
        print("  1. Sign in with your Anthropic account (no key to manage):")
        print("       ant auth login")
        print("  2. Or export an API key in the shell that runs ./run.sh:")
        print("       export ANTHROPIC_API_KEY='<paste the real key>'")
        return 2

    if raw is not None and has_profile:
        print("WARNING: ANTHROPIC_API_KEY is set, so your `ant auth login` profile is")
        print("  being ignored. To use the profile instead, unset the key entirely:")
        print("    unset ANTHROPIC_API_KEY")
        print("  (an empty value still wins its slot - it must be unset, not blank)\n")
    if raw is not None and not raw.strip():
        print("WARNING: ANTHROPIC_API_KEY is set but EMPTY. That still takes precedence")
        print("  and authenticates with an empty key. Run: unset ANTHROPIC_API_KEY\n")
    if raw and token:
        print("WARNING: both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are set. The SDK")
        print("  sends both and the API rejects that. Unset one.\n")

    if raw:
        stripped = raw.strip()
        print(f"ANTHROPIC_API_KEY: set, {len(stripped)} chars, "
              f"starts {stripped[:10]!r}, ends {stripped[-4:]!r}")
        problems = _key_shape(raw)
        for problem in problems:
            print(f"  PROBLEM: the value {problem}")
        if not problems:
            print("  shape looks normal")
    print(f"Configured model: {settings.anthropic_model}\n")

    try:
        # Construction can fail on its own (e.g. a bad ANTHROPIC_CONFIG_DIR),
        # so it belongs inside the same guard as the call.
        client = anthropic.Anthropic()
        client.models.list(limit=1)
    except anthropic.AuthenticationError:
        print("AUTH FAILED (401) - the API rejected this key.")
        print("  The key is not valid for this account. Common causes:")
        print("   - it was revoked, or belongs to a different/deleted workspace")
        print("   - it was copied incompletely, or with quotes/whitespace (see above)")
        print("   - an old key is still exported in this shell, shadowing a new one")
        print("  If you have a Claude subscription, `ant auth login` signs in with your")
        print("  account instead of a purchased key - then `unset ANTHROPIC_API_KEY`.")
        print("  Get a key at https://console.anthropic.com/settings/keys - it is a")
        print("  long string starting 'sk-ant-api03-'. Then, in the SAME shell:")
        print("    export ANTHROPIC_API_KEY='<paste the real key>'")
        print("    ./run.sh")
        return 1
    except anthropic.PermissionDeniedError as exc:
        print(f"PERMISSION DENIED (403) - the key authenticated but lacks access: {exc}")
        return 1
    except anthropic.APIConnectionError as exc:
        print(f"NETWORK ERROR - could not reach the API: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - report anything else verbatim
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}")
        return 1

    print("AUTH OK - the key is valid.")

    try:
        model = client.models.retrieve(settings.anthropic_model)
    except Exception as exc:  # noqa: BLE001
        print(f"\nBut the configured model is not available: {exc}")
        print(f"  Pick another with: export ANTHROPIC_MODEL=<id>")
        return 1

    print(f"MODEL OK - {getattr(model, 'id', settings.anthropic_model)} is available to this account.")
    print("\nRestart the server and the panel will answer with the model.")
    return 0


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
        "--probe-model",
        action="store_true",
        help="check the assistant's API key and model access, without spending tokens",
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

    if args.probe_model:
        return probe_model()
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
