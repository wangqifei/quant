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


class _StubResponse:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _StubSession:
    """Replays a scripted Yahoo handshake."""

    def __init__(self, by_url):
        self._by_url = by_url
        self.cookies = {"A1": "x", "A1S": "y", "A3": "z"}

    def get(self, url, params=None):
        for fragment, response in self._by_url.items():
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL {url}")

    def close(self):
        pass


def _patch_session(monkeypatch, session):
    from app import providers

    monkeypatch.setattr(providers, "make_yahoo_session", lambda *a, **k: (session, "curl_cffi/chrome"))


def test_probe_succeeds_when_only_the_chart_call_matters(capsys, monkeypatch):
    """The real-world success case: fc.yahoo.com 404s but the chart works.

    fc.yahoo.com always answers 404 - it only sets cookies - so counting it as
    a failure previously made a working setup report "Still blocked".
    """
    _patch_session(monkeypatch, _StubSession({
        "fc.yahoo.com": _StubResponse(404, "<!DOCTYPE html>", {"content-type": "text/html"}),
        "getcrumb": _StubResponse(200, "1HOCiTjsztx", {"content-type": "text/plain"}),
        "/v8/finance/chart/": _StubResponse(
            200, '{"chart":{"result":[{"meta":{"symbol":"SPY"}}]}}', {"content-type": "application/json"}
        ),
    }))
    rc = diagnose.probe_yahoo()
    out = capsys.readouterr().out

    assert rc == 0
    assert "WORKING" in out
    assert "Still blocked" not in out
    assert "(informational)" in out  # the 404 is labelled, not counted


def test_probe_fails_when_the_chart_call_is_throttled(capsys, monkeypatch):
    _patch_session(monkeypatch, _StubSession({
        "fc.yahoo.com": _StubResponse(404, "<!DOCTYPE html>"),
        "getcrumb": _StubResponse(200, "abc"),
        "/v8/finance/chart/": _StubResponse(429, "Too Many Requests", {"retry-after": "60"}),
    }))
    rc = diagnose.probe_yahoo()
    out = capsys.readouterr().out

    assert rc == 1
    assert "WORKING" not in out
    assert "retry-after: 60" in out


def test_probe_fails_when_crumb_works_but_chart_does_not(capsys, monkeypatch):
    _patch_session(monkeypatch, _StubSession({
        "fc.yahoo.com": _StubResponse(200, "ok"),
        "getcrumb": _StubResponse(200, "abc"),
        "/v8/finance/chart/": _StubResponse(403, "Forbidden"),
    }))
    assert diagnose.probe_yahoo() == 1


# ---------------------------------------------------------- key shape checks

def test_key_shape_accepts_a_clean_key():
    assert diagnose._key_shape("sk-ant-api03-" + "a" * 60) == []


def test_key_shape_flags_surrounding_quotes():
    problems = diagnose._key_shape("'sk-ant-api03-" + "a" * 60 + "'")
    assert any("wrapped in quotes" in p for p in problems)


def test_key_shape_flags_trailing_newline():
    problems = diagnose._key_shape("sk-ant-api03-" + "a" * 60 + "\n")
    assert any("whitespace or a newline" in p for p in problems)


def test_key_shape_flags_internal_whitespace():
    problems = diagnose._key_shape("sk-ant-api03-" + "a" * 30 + " " + "a" * 30)
    assert any("internal space" in p for p in problems)


def test_key_shape_flags_truncation_and_wrong_prefix():
    problems = diagnose._key_shape("nope-123")
    assert any("truncated" in p for p in problems)
    assert any("sk-ant-" in p for p in problems)


def test_probe_model_reports_a_missing_key(capsys, monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert diagnose.probe_model() == 2
    assert "not set in THIS shell" in capsys.readouterr().out


def test_probe_model_never_prints_the_whole_key(capsys, monkeypatch):
    pytest.importorskip("anthropic")
    secret = "sk-ant-api03-" + "z" * 60
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    diagnose.probe_model()
    out = capsys.readouterr().out
    assert secret not in out
    assert "sk-ant-api" in out  # the prefix alone is fine for identification


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-...",              # the literal placeholder from the docs
        "<your-api-key>",
        "YOUR_API_KEY",
        "sk-ant-api03-REPLACE_ME",
        "'sk-ant-...'",            # placeholder that also kept its quotes
    ],
)
def test_key_shape_calls_out_placeholder_text(value):
    problems = diagnose._key_shape(value)
    assert any("placeholder text" in p for p in problems)
    # The placeholder message stands alone - length/prefix noise would bury it.
    assert len(problems) == 1


def test_a_real_looking_key_is_not_called_a_placeholder():
    assert diagnose._key_shape("sk-ant-api03-" + "a" * 90) == []
