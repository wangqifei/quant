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
from .providers import INSTRUMENTS, Provider, ProviderError, build_providers

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check each market data provider.")
    parser.add_argument("--verbose", "-v", action="store_true", help="show debug logging")
    parser.add_argument(
        "--providers",
        help="comma-separated provider names to test (default: the configured chain)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    names = [p.strip() for p in args.providers.split(",")] if args.providers else settings.providers
    providers = build_providers(names)
    if not providers:
        print(f"No known providers in {names!r}.", file=sys.stderr)
        return 2

    print(f"Lookback: {settings.lookback_days} days   Chain: {', '.join(p.name for p in providers)}\n")

    working: list[str] = []
    for provider in providers:
        print(f"{provider.name}")
        provider_ok = True
        for key in INSTRUMENTS:
            ok, detail = check(provider, key, settings.lookback_days)
            provider_ok &= ok
            print(f"  [{OK if ok else FAIL}] {key:4} {detail}")
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
