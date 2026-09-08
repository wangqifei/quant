"""FutuProvider behaviour, driven through an injected fake gateway context.

A real check needs FutuOpenD running and an account, so these exercise the
parts that are ours: DataFrame conversion, pagination, error unpacking and
the fail-fast guards.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import FutuProvider, ProviderError, _as_float, _bars_from_futu_rows


def row(day: str, px: float, **over):
    """One kline record in the shape request_history_kline returns."""
    base = {
        "code": "US.SPY",
        "time_key": f"{day} 00:00:00",
        "open": px - 1,
        "close": px,
        "high": px + 2,
        "low": px - 2,
        "volume": 1_000_000,
        "last_close": px - 0.5,
    }
    base.update(over)
    return base


class FakeFrame:
    """Stands in for the pandas DataFrame the SDK returns."""

    def __init__(self, records):
        self._records = records

    def to_dict(self, _orient):
        return self._records


class FakeContext:
    def __init__(self, pages, quota=(10, 90)):
        self._pages = list(pages)
        self._quota = quota
        self.requests = []
        self.closed = False

    def request_history_kline(self, code, **kwargs):
        self.requests.append({"code": code, **kwargs})
        return self._pages.pop(0)

    def get_history_kl_quota(self, get_detail=False):
        return 0, (self._quota[0], self._quota[1], [])

    def close(self):
        self.closed = True


def provider_with(ctx) -> FutuProvider:
    return FutuProvider(context_factory=lambda: ctx)


# -- conversion ----------------------------------------------------------

def test_bars_from_rows():
    bars = _bars_from_futu_rows([row("2025-06-02", 500.0), row("2025-06-03", 505.0)])
    assert [b.date for b in bars] == [date(2025, 6, 2), date(2025, 6, 3)]
    assert bars[1].close == 505.0
    assert bars[1].high == 507.0


def test_rows_are_sorted_by_date():
    bars = _bars_from_futu_rows([row("2025-06-05", 510.0), row("2025-06-02", 500.0)])
    assert [b.date for b in bars] == [date(2025, 6, 2), date(2025, 6, 5)]


def test_unparseable_rows_are_skipped():
    rows = [
        row("2025-06-02", 500.0),
        row("not-a-date", 501.0),           # unparseable date
        row("2025-06-03", 502.0, close=None),  # missing close
        row("2025-06-04", 503.0, time_key=""),  # missing timestamp
    ]
    assert len(_bars_from_futu_rows(rows)) == 1


def test_as_float_rejects_nan_and_junk():
    assert _as_float("12.5") == 12.5
    assert _as_float(None) is None
    assert _as_float("abc") is None
    assert _as_float(float("nan")) is None


# -- fetching ------------------------------------------------------------

def test_fetch_builds_a_series():
    ctx = FakeContext([(0, FakeFrame([row("2025-06-02", 500.0), row("2025-06-03", 505.0)]), None)])
    series = provider_with(ctx).fetch("SPY", 30)

    assert series.quote.symbol == "SPY"
    assert series.quote.price == 505.0
    assert series.quote.previous_close == 504.5  # from last_close
    assert series.quote.source == "futu"
    assert len(series.bars) == 2


def test_fetch_uses_the_configured_code():
    ctx = FakeContext([(0, FakeFrame([row("2025-06-02", 500.0)]), None)])
    FutuProvider(context_factory=lambda: ctx, codes={"SPX": "US.CUSTOM"}).fetch("SPX", 30)
    assert ctx.requests[0]["code"] == "US.CUSTOM"


def test_pagination_follows_the_page_key():
    ctx = FakeContext([
        (0, FakeFrame([row("2025-06-02", 500.0)]), "page2"),
        (0, FakeFrame([row("2025-06-03", 505.0)]), None),
    ])
    series = provider_with(ctx).fetch("SPY", 30)

    assert len(series.bars) == 2
    assert ctx.requests[0]["page_req_key"] is None
    assert ctx.requests[1]["page_req_key"] == "page2"


def test_futu_error_becomes_provider_error():
    ctx = FakeContext([(-1, "Quota exceeded", None)])
    with pytest.raises(ProviderError, match="Quota exceeded"):
        provider_with(ctx).fetch("SPY", 30)


def test_empty_result_raises():
    ctx = FakeContext([(0, FakeFrame([]), None)])
    with pytest.raises(ProviderError, match="no bars returned"):
        provider_with(ctx).fetch("SPY", 30)


def test_quota_is_reported():
    assert provider_with(FakeContext([], quota=(7, 93))).quota() == (7, 93)


def test_close_is_idempotent_and_closes_the_context():
    ctx = FakeContext([])
    provider = provider_with(ctx)
    provider._context()
    provider.close()
    provider.close()
    assert ctx.closed is True


# -- guards --------------------------------------------------------------

def test_dead_gateway_fails_fast_with_guidance():
    # Port 1 is never listening; this must not hang.
    provider = FutuProvider(host="127.0.0.1", port=1, connect_timeout=0.25)
    with pytest.raises(ProviderError, match="Start the FutuOpenD gateway"):
        provider.fetch("SPY", 30)


def test_unknown_security_firm_is_rejected():
    pytest.importorskip("futu")
    provider = FutuProvider(security_firm="NOT_A_FIRM", port=1, connect_timeout=0.25)
    # The gateway probe fires first; that is the more actionable error.
    with pytest.raises(ProviderError):
        provider.fetch("SPY", 30)
