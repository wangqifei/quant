from datetime import date, datetime, timezone

import pytest

from app.providers import (
    DemoProvider,
    ProviderError,
    StooqProvider,
    YahooProvider,
    _yahoo_range,
    build_providers,
)


def epoch(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, 20, 0, tzinfo=timezone.utc).timestamp())


YAHOO_PAYLOAD = {
    "chart": {
        "error": None,
        "result": [
            {
                "meta": {
                    "symbol": "^GSPC",
                    "currency": "USD",
                    "regularMarketPrice": 5450.0,
                    "chartPreviousClose": 5400.0,
                    "regularMarketTime": epoch(2025, 6, 4),
                },
                "timestamp": [epoch(2025, 6, 2), epoch(2025, 6, 3), epoch(2025, 6, 4)],
                "indicators": {
                    "quote": [
                        {
                            "open": [5300.0, 5350.0, 5400.0],
                            "high": [5320.0, 5380.0, 5430.0],
                            "low": [5280.0, 5330.0, 5390.0],
                            "close": [5310.0, 5400.0, 5420.0],
                            "volume": [1000, 1100, 1200],
                        }
                    ]
                },
            }
        ],
    }
}


def test_yahoo_parse_uses_live_price_and_prior_bar_close():
    series = YahooProvider()._parse("SPX", YAHOO_PAYLOAD)
    assert len(series.bars) == 3
    # The live print replaces the last daily close rather than appending a bar.
    assert series.bars[-1].close == 5450.0
    assert series.bars[-1].high == 5450.0  # widened past the stale 5430 high
    assert series.quote.price == 5450.0
    # The prior session's bar close - not meta.chartPreviousClose.
    assert series.quote.previous_close == 5400.0
    assert series.quote.change == pytest.approx(50.0)
    assert series.quote.as_of == date(2025, 6, 4)
    assert series.quote.source == "yahoo"


def test_stale_chart_previous_close_is_ignored():
    """Regression: a 2y range reported a +41.88% daily move.

    meta.chartPreviousClose is the close before the chart range starts, so on
    a long range it is years old. Using it made the quote disagree wildly with
    the bars it was built from.
    """
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": "^GSPC",
                        "currency": "USD",
                        "regularMarketPrice": 7673.52,
                        "chartPreviousClose": 5408.42,          # two years stale
                        "regularMarketPreviousClose": 7654.90,
                        "regularMarketTime": epoch(2026, 9, 8),
                    },
                    "timestamp": [epoch(2026, 9, 4), epoch(2026, 9, 5), epoch(2026, 9, 8)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [7600.0, 7640.0, 7666.99],
                                "high": [7650.0, 7680.0, 7717.81],
                                "low": [7590.0, 7620.0, 7666.99],
                                "close": [7640.0, 7654.90, 7670.0],
                                "volume": [1, 2, 3],
                            }
                        ]
                    },
                }
            ],
        }
    }
    quote = YahooProvider()._parse("SPX", payload).quote
    assert quote.previous_close == 7654.90
    assert quote.change_pct == pytest.approx(0.243, abs=0.01)
    assert abs(quote.change_pct) < 25  # no plausible SPX session moves this much


def test_quote_change_agrees_with_the_computed_1d_return():
    """The quote and the metrics must not tell different stories."""
    from app.analytics import summarize

    series = YahooProvider()._parse("SPX", YAHOO_PAYLOAD)
    metrics = summarize(series.bars)
    assert metrics["returns"]["1d"] == pytest.approx(series.quote.change_pct, abs=1e-9)


def test_meta_previous_close_used_only_when_there_is_no_prior_bar():
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"regularMarketPreviousClose": 5390.0},
                    "timestamp": [epoch(2025, 6, 4)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [5400.0],
                                "high": [5430.0],
                                "low": [5390.0],
                                "close": [5420.0],
                                "volume": [1],
                            }
                        ]
                    },
                }
            ],
        }
    }
    assert YahooProvider()._parse("SPX", payload).quote.previous_close == 5390.0


def test_yahoo_parse_skips_null_padded_sessions():
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {},
                    "timestamp": [epoch(2025, 6, 2), epoch(2025, 6, 3), epoch(2025, 6, 4)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [5300.0, None, 5400.0],
                                "high": [5320.0, None, 5430.0],
                                "low": [5280.0, None, 5390.0],
                                "close": [5310.0, None, 5420.0],
                                "volume": [1000, None, 1200],
                            }
                        ]
                    },
                }
            ],
        }
    }
    series = YahooProvider()._parse("SPX", payload)
    assert [b.close for b in series.bars] == [5310.0, 5420.0]


def test_yahoo_parse_raises_on_error_payload():
    with pytest.raises(ProviderError):
        YahooProvider()._parse("SPX", {"chart": {"error": {"code": "Not Found"}, "result": None}})
    with pytest.raises(ProviderError):
        YahooProvider()._parse("SPX", {"chart": {"result": []}})


STOOQ_CSV = """Date,Open,High,Low,Close,Volume
2025-06-02,5300,5320,5280,5310,1000
2025-06-03,5350,5380,5330,5400,1100
2025-06-04,5400,5430,5390,5420,1200
"""


def test_stooq_parse():
    series = StooqProvider()._parse("SPX", STOOQ_CSV, 3650)
    assert len(series.bars) == 3
    assert series.quote.price == 5420.0
    assert series.quote.previous_close == 5400.0
    assert series.quote.as_of == date(2025, 6, 4)
    assert series.quote.source == "stooq"


def test_stooq_parse_skips_junk_rows():
    junk = STOOQ_CSV + "<html>Exceeded the daily hits limit</html>\n"
    assert len(StooqProvider()._parse("SPX", junk, 3650).bars) == 3


def test_stooq_parse_raises_when_nothing_usable():
    with pytest.raises(ProviderError):
        StooqProvider()._parse("SPX", "<html>blocked</html>", 3650)


def test_stooq_lookback_trims_but_keeps_two_bars():
    series = StooqProvider()._parse("SPX", STOOQ_CSV, 1)
    assert len(series.bars) >= 2


def test_demo_provider_is_deterministic():
    a = DemoProvider().fetch("SPX", 200)
    b = DemoProvider().fetch("SPX", 200)
    assert [x.close for x in a.bars] == [x.close for x in b.bars]


def test_demo_etf_tracks_the_index():
    spx = DemoProvider().fetch("SPX", 300)
    spy = DemoProvider().fetch("SPY", 300)
    assert [b.date for b in spx.bars] == [b.date for b in spy.bars]
    ratio = spx.quote.price / spy.quote.price
    assert 9.9 < ratio < 10.2


def test_demo_bars_are_weekdays_only():
    for bar in DemoProvider().fetch("SPX", 60).bars:
        assert bar.date.weekday() < 5


def test_unknown_instrument_raises():
    with pytest.raises(KeyError):
        DemoProvider().fetch("TSLA", 30)


@pytest.mark.parametrize(
    "days,expected", [(5, "5d"), (30, "1mo"), (90, "3mo"), (180, "6mo"), (365, "1y"), (500, "2y"), (5000, "5y")]
)
def test_yahoo_range_mapping(days, expected):
    assert _yahoo_range(days) == expected


def test_build_providers_preserves_order_and_drops_unknown():
    assert [p.name for p in build_providers(["stooq", "nope", "demo"])] == ["stooq", "demo"]
