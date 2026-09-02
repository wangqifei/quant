"""Market data service: provider fallback, caching, and snapshot assembly."""

from __future__ import annotations

import logging
import time
from typing import Any

from . import analytics
from .config import Settings, settings as default_settings
from .models import Series
from .providers import INSTRUMENTS, Provider, ProviderError, build_providers

log = logging.getLogger(__name__)


class MarketService:
    """Fetches and caches daily series for the tracked instruments.

    Providers are tried in configured order; the first one that returns
    usable data for a symbol wins. Failures are recorded so the UI can show
    which source is actually live.
    """

    def __init__(self, settings: Settings | None = None, providers: list[Provider] | None = None) -> None:
        self.settings = settings or default_settings
        self.providers = providers if providers is not None else build_providers(self.settings.providers)
        self._cache: dict[str, tuple[float, Series]] = {}

    def series(self, key: str, *, force: bool = False) -> tuple[Series, list[str]]:
        """Return the series for ``key`` plus any provider errors seen."""
        if key not in INSTRUMENTS:
            raise KeyError(f"unknown instrument: {key}")

        cached = self._cache.get(key)
        if cached and not force and time.time() - cached[0] < self.settings.cache_ttl_seconds:
            return cached[1], []

        errors: list[str] = []
        for provider in self.providers:
            try:
                result = provider.fetch(key, self.settings.lookback_days)
            except Exception as exc:  # noqa: BLE001 - never let one provider kill the chain
                errors.append(f"{provider.name}: {exc}")
                log.warning("provider %s failed for %s: %s", provider.name, key, exc)
                continue
            self._cache[key] = (time.time(), result)
            return result, errors

        if cached:
            # Every provider failed; serve the last good data rather than nothing.
            errors.append("all providers failed - serving cached data")
            return cached[1], errors
        raise ProviderError(f"no provider could supply {key}: {'; '.join(errors) or 'none configured'}")

    def snapshot(self, *, force: bool = False, history: int = 260) -> dict[str, Any]:
        """Quotes, metrics and history for every tracked instrument."""
        instruments: dict[str, Any] = {}
        errors: list[str] = []
        raw: dict[str, Series] = {}

        for key in INSTRUMENTS:
            try:
                series, errs = self.series(key, force=force)
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            errors.extend(errs)
            raw[key] = series
            instruments[key] = {
                **series.to_dict(history=history),
                "metrics": analytics.summarize(series.bars),
            }

        payload: dict[str, Any] = {
            "instruments": instruments,
            "errors": errors,
            "sources": {k: v.quote.source for k, v in raw.items()},
            "live": all(v.quote.source != "demo" for v in raw.values()) if raw else False,
            "fetched_at": time.time(),
        }
        if "SPX" in raw and "SPY" in raw:
            payload["comparison"] = analytics.compare(raw["SPX"].bars, raw["SPY"].bars)
        return payload
