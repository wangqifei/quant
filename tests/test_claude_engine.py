"""Wire-level checks for the Claude engine.

These run a local HTTP server, point the Anthropic SDK at it via
``ANTHROPIC_BASE_URL``, and assert on the request the engine actually sends.
No real API calls, no key required.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.assistant import FALLBACK_BETA, AssistantError, ClaudeEngine
from app.config import Settings
from app.market import MarketService
from app.providers import DemoProvider

anthropic = pytest.importorskip("anthropic")


def message_body(text: str = "SPY is 581.67.", stop_reason: str = "end_turn") -> dict:
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    if stop_reason == "refusal":
        body["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "nope"}
    return body


class MockAPI:
    """Captures requests and replays canned responses."""

    def __init__(self, responses: list[tuple[int, dict]]):
        self.requests: list[dict] = []
        self._responses = list(responses)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(self.rfile.read(length) or b"{}"),
                    }
                )
                status, payload = outer._responses.pop(0) if outer._responses else (200, message_body())
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):  # silence the server's stderr logging
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def snapshot():
    return MarketService(Settings(), providers=[DemoProvider()]).snapshot(history=40)


@pytest.fixture
def engine(monkeypatch):
    def _make(mock: MockAPI) -> ClaudeEngine:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        # No retries, so a non-200 surfaces immediately instead of backing off.
        eng = ClaudeEngine(Settings())
        eng._client = anthropic.Anthropic(base_url=mock.url, api_key="sk-ant-test", max_retries=0)
        return eng

    return _make


def test_request_shape(engine, snapshot):
    with MockAPI([(200, message_body())]) as mock:
        reply = engine(mock).answer("what is SPY at", snapshot, "- Long 200 SPY at 551", [])

    assert reply.text == "SPY is 581.67."
    assert reply.model == "claude-opus-5"  # from the response body, not config
    request = mock.requests[0]
    body = request["body"]

    assert body["model"] == "claude-opus-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["max_tokens"] > 0
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in request["headers"]["anthropic-beta"]
    assert "not a licensed advisor" in body["system"]

    # The prompt carries metrics and the user's context, not the raw bar history.
    content = body["messages"][-1]["content"]
    assert "MARKET SNAPSHOT" in content
    assert "Long 200 SPY at 551" in content
    assert "what is SPY at" in content
    assert '"recent_closes"' in content
    assert '"bars"' not in content


def test_history_is_replayed(engine, snapshot):
    history = [
        {"role": "user", "content": "what is SPX at"},
        {"role": "assistant", "content": "5,846.80"},
    ]
    with MockAPI([(200, message_body())]) as mock:
        engine(mock).answer("and SPY?", snapshot, "(none saved)", history)

    messages = mock.requests[0]["body"]["messages"]
    assert len(messages) == 3
    assert messages[0] == history[0]
    assert messages[1] == history[1]
    assert messages[2]["role"] == "user"


def test_refusal_is_surfaced_not_returned_as_text(engine, snapshot):
    with MockAPI([(200, message_body(text="", stop_reason="refusal"))]) as mock:
        with pytest.raises(AssistantError, match="declined"):
            engine(mock).answer("q", snapshot, "", [])


def test_retries_once_without_fallbacks_when_the_beta_is_rejected(engine, snapshot):
    rejection = {"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks is not supported"}}
    with MockAPI([(400, rejection), (200, message_body())]) as mock:
        eng = engine(mock)
        assert eng.answer("q", snapshot, "", []).text == "SPY is 581.67."

    assert len(mock.requests) == 2
    assert mock.requests[0]["body"]["fallbacks"] == "default"
    assert "fallbacks" not in mock.requests[1]["body"]      # dropped on the retry
    assert eng._send_fallbacks is False                      # and stays off


def test_unrelated_bad_request_is_not_retried(engine, snapshot):
    rejection = {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens too large"}}
    with MockAPI([(400, rejection)]) as mock:
        with pytest.raises(anthropic.BadRequestError):
            engine(mock).answer("q", snapshot, "", [])
    assert len(mock.requests) == 1


def test_invalid_api_key_is_reported_as_a_config_fault(engine, snapshot):
    """A 401 means the key is wrong - say so instead of retrying forever."""
    from app.assistant import AssistantError

    body = {"type": "error", "error": {"type": "authentication_error", "message": "API key is invalid."}}
    with MockAPI([(401, body)]) as mock:
        eng = engine(mock)
        with pytest.raises(AssistantError, match="ANTHROPIC_API_KEY was rejected"):
            eng.answer("q", snapshot, "", [])

    # And it sticks: the engine now reports itself unavailable with that reason,
    # so later questions fail fast instead of re-hitting a key we know is bad.
    assert "rejected" in eng.unavailable_reason()
    assert len(mock.requests) == 1


def test_auth_failure_does_not_leak_the_key(engine, snapshot):
    from app.assistant import AssistantError

    body = {"type": "error", "error": {"type": "authentication_error", "message": "API key is invalid."}}
    with MockAPI([(401, body)]) as mock:
        eng = engine(mock)
        with pytest.raises(AssistantError) as caught:
            eng.answer("q", snapshot, "", [])
    assert "sk-ant-test" not in str(caught.value)


def test_serving_model_is_read_from_the_response(engine, snapshot):
    """With refusal fallbacks on, another model may serve the request."""
    body = message_body()
    body["model"] = "claude-opus-4-8"
    with MockAPI([(200, body)]) as mock:
        reply = engine(mock).answer("q", snapshot, "", [])
    assert reply.model == "claude-opus-4-8"
