import pytest

from app import diagnose
from app.providers import DemoProvider, ProviderError, Provider
from app.models import Series


class Broken(Provider):
    name = "broken"

    def fetch(self, key, lookback_days):
        raise ProviderError("upstream refused the connection")


class Exploding(Provider):
    name = "exploding"

    def fetch(self, key, lookback_days):
        raise ValueError("unexpected payload")


def test_check_reports_success():
    ok, detail = diagnose.check(DemoProvider(), "SPX", 200)
    assert ok is True
    assert "bars" in detail and "ms" in detail


def test_check_reports_provider_error():
    ok, detail = diagnose.check(Broken(), "SPX", 200)
    assert ok is False
    assert "upstream refused" in detail


def test_check_catches_unexpected_exceptions():
    ok, detail = diagnose.check(Exploding(), "SPX", 200)
    assert ok is False
    assert detail.startswith("ValueError:")


def test_main_returns_1_when_only_demo_works(capsys):
    assert diagnose.main(["--providers", "demo"]) == 1
    out = capsys.readouterr().out
    assert "No live provider is reachable" in out
    assert "SPX" in out and "SPY" in out


def test_main_rejects_unknown_providers(capsys):
    assert diagnose.main(["--providers", "nonsense"]) == 2
    assert "No known providers" in capsys.readouterr().err


def test_main_reports_success_for_a_live_provider(capsys, monkeypatch):
    class FakeLive(DemoProvider):
        name = "yahoo"

    monkeypatch.setattr(diagnose, "build_providers", lambda names: [FakeLive()])
    assert diagnose.main(["--providers", "yahoo"]) == 0
    assert "Live data available from: yahoo" in capsys.readouterr().out


def test_environment_reports_python_and_optional_packages():
    text = diagnose.environment()
    assert "Python" in text
    assert "curl_cffi" in text and "futu-api" in text


def test_probe_yahoo_reports_each_step(capsys):
    # Network is unreachable in CI, so every step errors - the point is that
    # the probe reports per-step outcomes instead of one opaque failure.
    rc = diagnose.probe_yahoo()
    out = capsys.readouterr().out
    assert "Transport:" in out
    assert "cookie" in out and "crumb" in out and "chart SPY" in out
    assert "cookies held:" in out
    assert rc in (0, 1)
