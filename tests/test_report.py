import pytest

from app import report

pd = pytest.importorskip("pandas")


def test_every_indicator_agrees_with_the_independent_implementation(capsys):
    assert report.main(["--providers", "demo", "--symbol", "all"]) == 0
    out = capsys.readouterr().out
    assert "ALL INDICATORS AGREE" in out
    assert "MISMATCH" not in out
    assert "SYNTHETIC DEMO DATA" in out  # never passes demo data off as the market


def test_custom_windows_are_reported(capsys):
    report.main(["--providers", "demo", "--symbol", "SPY", "--windows", "7,33"])
    out = capsys.readouterr().out
    assert "SMA 7" in out and "SMA 33" in out and "EMA 33" in out


def test_mismatch_is_detected():
    assert report._agree(100.0, 100.0) == "ok"
    assert report._agree(100.0, 101.0) == "MISMATCH"
    assert report._agree(None, None) == "n/a"
    assert report._agree(100.0, None) == "MISMATCH"
