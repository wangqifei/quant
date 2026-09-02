from datetime import date, timedelta

import pytest

from app import analytics
from app.models import Bar


def make_bars(closes: list[float], start: date = date(2025, 1, 1)) -> list[Bar]:
    return [
        Bar(date=start + timedelta(days=i), open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_sma_needs_full_window():
    assert analytics.sma([1, 2, 3], 5) is None
    assert analytics.sma([1, 2, 3, 4], 4) == 2.5


def test_pct_change():
    assert analytics.pct_change(110, 100) == pytest.approx(10.0)
    assert analytics.pct_change(90, 100) == pytest.approx(-10.0)
    assert analytics.pct_change(100, 0) is None


def test_rsi_bounds():
    assert analytics.rsi([100 + i for i in range(30)]) == 100.0
    assert analytics.rsi([100 - i for i in range(30)]) == 0.0
    assert analytics.rsi([100, 101, 102]) is None  # not enough observations


def test_rsi_mid_range():
    closes = [100 + (2 if i % 2 == 0 else -1) for i in range(40)]
    value = analytics.rsi(closes)
    assert 0 < value < 100


def test_max_drawdown():
    # 100 -> 120 -> 90: worst decline is 90/120 - 1 = -25%
    assert analytics.max_drawdown([100, 120, 90, 110]) == pytest.approx(-25.0)
    assert analytics.max_drawdown([100, 110, 120]) == pytest.approx(0.0)
    assert analytics.max_drawdown([100]) is None


def test_realized_vol_zero_for_flat_series():
    assert analytics.realized_vol([100.0] * 30, 20) == pytest.approx(0.0)
    assert analytics.realized_vol([100.0], 20) is None


def test_realized_vol_is_annualised():
    # Alternating +1%/-1% daily moves: daily stdev ~1%, annualised ~ 1% * sqrt(252).
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    vol = analytics.realized_vol(closes, 60)
    assert 14 < vol < 17


def test_ytd_uses_prior_year_close():
    bars = make_bars([100, 110], start=date(2024, 12, 30))  # 2024-12-30, 2024-12-31
    bars += make_bars([132], start=date(2025, 1, 2))
    assert analytics.ytd_return(bars) == pytest.approx(20.0)  # 132 vs 110


def test_ytd_falls_back_to_first_bar_without_prior_year():
    bars = make_bars([100, 125], start=date(2025, 3, 1))
    assert analytics.ytd_return(bars) == pytest.approx(25.0)


def test_summarize_shape_and_trend():
    closes = [100 + i for i in range(300)]  # steady uptrend
    result = analytics.summarize(make_bars(closes))
    assert result["last"] == 399
    assert result["sessions"] == 300
    assert result["trend"] == "uptrend"
    assert result["returns"]["1d"] == pytest.approx(analytics.pct_change(399, 398))
    assert result["moving_averages"]["sma200"]["value"] is not None
    assert result["range_52w"]["sessions_used"] == 252


def test_summarize_short_history_degrades_gracefully():
    result = analytics.summarize(make_bars([100, 101, 102]))
    assert result["returns"]["1y"] is None
    assert result["moving_averages"]["sma200"]["value"] is None
    assert result["trend"] == "insufficient history"


def test_summarize_empty():
    assert analytics.summarize([]) == {}


def test_compare_aligns_on_shared_dates():
    spx = make_bars([1000, 1100])
    spy = make_bars([100, 110])
    result = analytics.compare(spx, spy)
    assert result["index_to_etf_ratio"] == pytest.approx(10.0)
    assert result["aligned_sessions"] == 2
    assert result["tracking_gap_pct"] == pytest.approx(0.0)


def test_compare_ignores_unmatched_dates():
    spx = make_bars([1000, 1100], start=date(2025, 1, 1))
    spy = make_bars([100, 110], start=date(2025, 6, 1))
    assert analytics.compare(spx, spy)["aligned_sessions"] == 0
