import pytest

from app.config import Settings
from app.market import MarketService
from app.models import Series
from app.providers import DemoProvider, Provider, ProviderError


class BoomProvider(Provider):
    name = "boom"

    def __init__(self):
        self.calls = 0

    def fetch(self, key: str, lookback_days: int) -> Series:
        self.calls += 1
        raise ProviderError("upstream is down")


class CountingDemo(DemoProvider):
    def __init__(self):
        self.calls = 0

    def fetch(self, key, lookback_days):
        self.calls += 1
        return super().fetch(key, lookback_days)


def test_falls_through_to_the_next_provider():
    boom, demo = BoomProvider(), CountingDemo()
    service = MarketService(Settings(providers=["boom", "demo"]), providers=[boom, demo])
    series, errors = service.series("SPX")
    assert series.quote.source == "demo"
    assert boom.calls == 1
    assert "boom: upstream is down" in errors[0]


def test_raises_when_every_provider_fails_and_no_cache():
    service = MarketService(Settings(), providers=[BoomProvider()])
    with pytest.raises(ProviderError):
        service.series("SPX")


def test_serves_stale_cache_when_providers_go_down():
    demo = CountingDemo()
    service = MarketService(Settings(cache_ttl_seconds=0), providers=[demo])
    good, _ = service.series("SPX")

    service.providers = [BoomProvider()]
    stale, errors = service.series("SPX")
    assert stale.quote.price == good.quote.price
    assert any("serving cached data" in e for e in errors)


def test_cache_prevents_refetch_within_ttl():
    demo = CountingDemo()
    service = MarketService(Settings(cache_ttl_seconds=300), providers=[demo])
    service.series("SPX")
    service.series("SPX")
    assert demo.calls == 1


def test_force_bypasses_the_cache():
    demo = CountingDemo()
    service = MarketService(Settings(cache_ttl_seconds=300), providers=[demo])
    service.series("SPX")
    service.series("SPX", force=True)
    assert demo.calls == 2


def test_unknown_symbol():
    with pytest.raises(KeyError):
        MarketService(Settings(), providers=[DemoProvider()]).series("TSLA")


def test_snapshot_shape():
    service = MarketService(Settings(), providers=[DemoProvider()])
    snap = service.snapshot(history=10)
    assert set(snap["instruments"]) == {"SPX", "SPY"}
    assert len(snap["instruments"]["SPX"]["bars"]) == 10
    assert snap["instruments"]["SPX"]["metrics"]["last"] > 0
    assert snap["sources"] == {"SPX": "demo", "SPY": "demo"}
    assert snap["live"] is False
    assert "tracking_gap_pct" in snap["comparison"]


def test_snapshot_marks_live_when_no_demo_source():
    class FakeLive(DemoProvider):
        name = "yahoo"

        def fetch(self, key, lookback_days):
            series = super().fetch(key, lookback_days)
            object.__setattr__(series.quote, "source", "yahoo")
            return series

    snap = MarketService(Settings(), providers=[FakeLive()]).snapshot(history=5)
    assert snap["live"] is True
