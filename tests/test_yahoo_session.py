"""Resilience behaviour of the Yahoo provider.

Every request is served by an httpx MockTransport, so these assert on what
the provider actually puts on the wire - headers, crumb, host rotation and
retry policy - without touching the network.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers import BROWSER_HEADERS, ProviderError, YahooProvider
from tests.test_providers import YAHOO_PAYLOAD


class Recorder:
    """Serves canned responses and records the requests that produced them."""

    def __init__(self, responses):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404, text="not found")
        if str(request.url).startswith(YahooProvider.CRUMB_URL):
            return self._crumb_response()
        return self._responses.pop(0) if self._responses else httpx.Response(200, json=YAHOO_PAYLOAD)

    def _crumb_response(self):
        return httpx.Response(200, text="abc123crumb")

    @property
    def chart_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/v8/finance/chart/" in str(r.url)]


def make_provider(recorder: Recorder, **kwargs) -> YahooProvider:
    client = httpx.Client(
        transport=httpx.MockTransport(recorder),
        headers=BROWSER_HEADERS,
        follow_redirects=True,
    )
    kwargs.setdefault("retry_delay", 0)  # no real sleeping in tests
    return YahooProvider(client=client, **kwargs)


def test_sends_browser_headers_and_crumb():
    rec = Recorder([httpx.Response(200, json=YAHOO_PAYLOAD)])
    series = make_provider(rec).fetch("SPX", 30)

    assert series.quote.price == 5450.0
    chart = rec.chart_requests[0]
    assert chart.headers["User-Agent"].startswith("Mozilla/5.0")
    assert chart.headers["Referer"] == "https://finance.yahoo.com/"
    assert chart.url.params["crumb"] == "abc123crumb"
    assert chart.url.params["interval"] == "1d"


def test_symbol_is_url_encoded():
    rec = Recorder([httpx.Response(200, json=YAHOO_PAYLOAD)])
    make_provider(rec).fetch("SPX", 30)
    # ^GSPC must not travel as a raw caret.
    assert "%5EGSPC" in str(rec.chart_requests[0].url)


def test_crumb_is_minted_once_across_calls():
    rec = Recorder([httpx.Response(200, json=YAHOO_PAYLOAD)] * 3)
    provider = make_provider(rec)
    provider.fetch("SPX", 30)
    provider.fetch("SPY", 30)

    crumb_calls = [r for r in rec.requests if str(r.url).startswith(YahooProvider.CRUMB_URL)]
    assert len(crumb_calls) == 1


def test_retries_on_429_then_succeeds():
    rec = Recorder([httpx.Response(429, text="slow down"), httpx.Response(200, json=YAHOO_PAYLOAD)])
    series = make_provider(rec).fetch("SPX", 30)

    assert series.quote.price == 5450.0
    assert len(rec.chart_requests) == 2


def test_alternates_hosts_across_attempts():
    rec = Recorder([httpx.Response(503), httpx.Response(200, json=YAHOO_PAYLOAD)])
    make_provider(rec).fetch("SPX", 30)

    hosts = [r.url.host for r in rec.chart_requests]
    assert hosts == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def test_403_remints_the_crumb():
    rec = Recorder([httpx.Response(403), httpx.Response(200, json=YAHOO_PAYLOAD)])
    make_provider(rec).fetch("SPX", 30)

    crumb_calls = [r for r in rec.requests if str(r.url).startswith(YahooProvider.CRUMB_URL)]
    assert len(crumb_calls) == 2  # re-minted after the rejection


def test_gives_up_after_max_attempts():
    rec = Recorder([httpx.Response(429)] * 5)
    with pytest.raises(ProviderError, match="chart request failed"):
        make_provider(rec, max_attempts=3).fetch("SPX", 30)
    assert len(rec.chart_requests) == 3


def test_non_retryable_status_fails_immediately():
    rec = Recorder([httpx.Response(404, text="No data found")] * 5)
    with pytest.raises(ProviderError, match="HTTP 404"):
        make_provider(rec, max_attempts=3).fetch("SPX", 30)
    assert len(rec.chart_requests) == 1  # no retry budget wasted


def test_transport_errors_are_retried_and_reported():
    def boom(request):
        if request.url.host in ("fc.yahoo.com", "query1.finance.yahoo.com") and "chart" not in str(request.url):
            return httpx.Response(404)
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    provider = YahooProvider(client=client, max_attempts=2, retry_delay=0)
    with pytest.raises(ProviderError, match="ConnectError"):
        provider.fetch("SPX", 30)


def test_missing_crumb_endpoint_still_allows_the_chart_call():
    class NoCrumb(Recorder):
        def _crumb_response(self):
            return httpx.Response(500, text="<html>error</html>")

    rec = NoCrumb([httpx.Response(200, json=YAHOO_PAYLOAD)])
    series = make_provider(rec).fetch("SPX", 30)

    assert series.quote.price == 5450.0
    assert "crumb" not in rec.chart_requests[0].url.params


def test_html_crumb_body_is_rejected():
    class HtmlCrumb(Recorder):
        def _crumb_response(self):
            return httpx.Response(200, text="<!doctype html><html>consent</html>")

    rec = HtmlCrumb([httpx.Response(200, json=YAHOO_PAYLOAD)])
    make_provider(rec).fetch("SPX", 30)
    assert "crumb" not in rec.chart_requests[0].url.params


# -- transport selection -------------------------------------------------

def test_auto_uses_curl_cffi_when_available(monkeypatch):
    from app import providers

    session, description = providers.make_yahoo_session("auto")
    if providers.HAVE_CURL_CFFI:
        assert description.startswith("curl_cffi/")
    else:
        assert description == "httpx"
    if hasattr(session, "close"):
        session.close()


def test_off_always_uses_httpx():
    from app import providers

    session, description = providers.make_yahoo_session("off")
    assert description == "httpx"
    assert isinstance(session, httpx.Client)
    session.close()


def test_named_target_without_curl_cffi_is_an_actionable_error(monkeypatch):
    import builtins

    from app import providers

    real_import = builtins.__import__

    def no_curl(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError("no curl_cffi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_curl)
    with pytest.raises(ProviderError, match="requirements-yahoo"):
        providers.make_yahoo_session("chrome")


def test_auto_falls_back_to_httpx_without_curl_cffi(monkeypatch):
    import builtins

    from app import providers

    real_import = builtins.__import__

    def no_curl(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError("no curl_cffi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_curl)
    session, description = providers.make_yahoo_session("auto")
    assert description == "httpx"
    session.close()


def test_failure_message_names_the_transport():
    rec = Recorder([httpx.Response(429)] * 5)
    with pytest.raises(ProviderError, match="via injected"):
        make_provider(rec, max_attempts=2).fetch("SPX", 30)
