import pytest

from app.assistant import (
    Assistant,
    ClaudeEngine,
    ContextStore,
    LocalEngine,
    _compact_snapshot,
    detect_intent,
    detect_symbols,
)
from app.config import Settings
from app.market import MarketService
from app.providers import DemoProvider


@pytest.fixture
def snapshot():
    return MarketService(Settings(), providers=[DemoProvider()]).snapshot(history=30)


@pytest.fixture
def store(tmp_path):
    return ContextStore(tmp_path / "context.json")


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize(
    "question,expected",
    [
        ("what is SPY trading at", ["SPY"]),
        ("how far is spx from its high", ["SPX"]),
        ("compare spy and the s&p", ["SPY", "SPX"]),
        ("how are things", ["SPX", "SPY"]),  # no symbol named -> both
    ],
)
def test_detect_symbols(question, expected):
    assert sorted(detect_symbols(question)) == sorted(expected)


@pytest.mark.parametrize(
    "question,intent",
    [
        ("what is the 200 day moving average", "moving_average"),
        ("what is realized vol", "volatility"),
        ("is it overbought", "rsi"),
        ("how far off the 52 week high", "range"),
        ("what was the max drawdown", "drawdown"),
        ("spx vs spy", "comparison"),
        ("ytd performance", "performance"),
        ("spy price", "price"),
        ("give me a read on the tape", "summary"),
    ],
)
def test_detect_intent(question, intent):
    assert detect_intent(question) == intent


# ------------------------------------------------------------ local engine

def test_local_engine_answers_price(snapshot):
    answer = LocalEngine().answer("what is SPY trading at", snapshot, "")
    price = snapshot["instruments"]["SPY"]["quote"]["price"]
    assert f"{price:,.2f}" in answer
    assert "SPX" not in answer  # question named only SPY


def test_local_engine_targets_the_requested_moving_average(snapshot):
    answer = LocalEngine().answer("how far is spx from its 200 day moving average", snapshot, "")
    assert "SMA200" in answer
    assert "SMA20 " not in answer


def test_local_engine_flags_demo_data(snapshot):
    assert "Synthetic demo data" in LocalEngine().answer("spy price", snapshot, "")


def test_local_engine_handles_empty_snapshot():
    assert "No market data" in LocalEngine().answer("spy price", {"instruments": {}}, "")


def test_local_engine_comparison_includes_tracking_gap(snapshot):
    answer = LocalEngine().answer("spx vs spy", snapshot, "")
    assert "tracking gap" in answer


def test_local_engine_rsi_zone(snapshot):
    answer = LocalEngine().answer("is it overbought", snapshot, "")
    assert "RSI" in answer
    assert any(word in answer for word in ("overbought", "oversold", "neutral"))


# ----------------------------------------------------------- context store

def test_context_store_roundtrip_persists(tmp_path):
    path = tmp_path / "context.json"
    store = ContextStore(path)
    note = store.add("Long 200 SPY at 551")
    assert [n.text for n in ContextStore(path).list()] == ["Long 200 SPY at 551"]
    assert store.remove(note.id) is True
    assert ContextStore(path).list() == []


def test_context_store_rejects_blank(store):
    with pytest.raises(ValueError):
        store.add("   ")


def test_context_store_remove_unknown_id(store):
    assert store.remove("nope") is False


def test_context_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("{not json")
    assert ContextStore(path).list() == []


def test_context_store_caps_note_count(store):
    for i in range(ContextStore.MAX_NOTES + 10):
        store.add(f"note {i}")
    notes = store.list()
    assert len(notes) == ContextStore.MAX_NOTES
    assert notes[-1].text == f"note {ContextStore.MAX_NOTES + 9}"  # newest kept


def test_context_store_prompt_text(store):
    assert store.as_prompt_text() == "(none saved)"
    store.add("Long SPY")
    assert store.as_prompt_text() == "- Long SPY"


# ---------------------------------------------------------------- assistant

def test_assistant_uses_local_when_claude_unavailable(snapshot, tmp_path):
    class NoClaude:
        def unavailable_reason(self):
            return "ANTHROPIC_API_KEY is not set in the server's environment"

    assistant = Assistant(settings=Settings(context_path=tmp_path / "c.json"), claude=NoClaude())
    result = assistant.ask("spy price", snapshot)
    assert result["engine"] == "local"
    assert result["answer"]
    assert "ANTHROPIC_API_KEY" in result["note"]


def test_claude_engine_unavailable_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert ClaudeEngine(Settings()).available is False


def test_claude_engine_reports_available_with_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    pytest.importorskip("anthropic")
    assert ClaudeEngine(Settings()).available is True


def test_assistant_falls_back_to_local_when_claude_errors(snapshot, tmp_path):
    class BrokenClaude:
        def unavailable_reason(self):
            return None

        def answer(self, *args, **kwargs):
            raise RuntimeError("api exploded")

    assistant = Assistant(settings=Settings(context_path=tmp_path / "c.json"), claude=BrokenClaude())
    result = assistant.ask("spy price", snapshot)
    assert result["engine"] == "local"
    assert "api exploded" in result["warning"]
    assert result["answer"]


def test_assistant_prefers_local_when_asked(snapshot, tmp_path):
    class LoudClaude:
        def unavailable_reason(self):
            raise AssertionError("should not be consulted for a local request")

        def answer(self, *args, **kwargs):
            raise AssertionError("should not be called")

    assistant = Assistant(settings=Settings(context_path=tmp_path / "c.json"), claude=LoudClaude())
    assert assistant.ask("spy price", snapshot, prefer="local")["engine"] == "local"


def test_assistant_passes_context_and_history_to_claude(snapshot, tmp_path):
    seen = {}

    class RecordingClaude:
        def unavailable_reason(self):
            return None

        def answer(self, question, snap, context, history):
            seen.update(question=question, context=context, history=history)
            return "ok"

    settings = Settings(context_path=tmp_path / "c.json")
    assistant = Assistant(settings=settings, claude=RecordingClaude())
    assistant.store.add("Long 200 SPY at 551")
    result = assistant.ask("spy price", snapshot, history=[{"role": "user", "content": "hi"}])

    assert result == {"answer": "ok", "engine": "claude", "model": settings.anthropic_model}
    assert "Long 200 SPY at 551" in seen["context"]
    assert seen["history"] == [{"role": "user", "content": "hi"}]


def test_assistant_rejects_empty_question(snapshot, tmp_path):
    with pytest.raises(ValueError):
        Assistant(settings=Settings(context_path=tmp_path / "c.json")).ask("  ", snapshot)


def test_compact_snapshot_drops_full_history(snapshot):
    compact = _compact_snapshot(snapshot)
    block = compact["instruments"]["SPX"]
    assert "bars" not in block
    assert len(block["recent_closes"]) == 20
    assert set(block["recent_closes"][0]) == {"date", "close"}
    assert block["metrics"]["last"] > 0


# ------------------------------------------------- explicit engine choice

class _Unavailable:
    def __init__(self, reason="no key configured"):
        self.reason = reason

    def unavailable_reason(self):
        return self.reason


class _Working:
    def __init__(self, text="Claude says hello"):
        self.text = text
        self.calls = 0

    def unavailable_reason(self):
        return None

    def answer(self, question, snap, context, history):
        self.calls += 1
        return self.text


def _assistant(tmp_path, claude):
    return Assistant(settings=Settings(context_path=tmp_path / "c.json"), claude=claude)


def test_engine_claude_raises_when_unavailable(snapshot, tmp_path):
    from app.assistant import EngineUnavailable

    assistant = _assistant(tmp_path, _Unavailable("ANTHROPIC_API_KEY is not set"))
    with pytest.raises(EngineUnavailable, match="ANTHROPIC_API_KEY"):
        assistant.ask("spy price", snapshot, prefer="claude")


def test_engine_claude_does_not_silently_fall_back_on_error(snapshot, tmp_path):
    from app.assistant import AssistantError

    class Broken(_Working):
        def answer(self, *a, **k):
            raise RuntimeError("rate limited")

    with pytest.raises(AssistantError, match="rate limited"):
        _assistant(tmp_path, Broken()).ask("spy price", snapshot, prefer="claude")


def test_engine_claude_returns_the_model_answer(snapshot, tmp_path):
    claude = _Working("SPX is extended versus its 200-day.")
    result = _assistant(tmp_path, claude).ask("read the tape", snapshot, prefer="claude")

    assert result["engine"] == "claude"
    assert result["answer"] == "SPX is extended versus its 200-day."
    assert claude.calls == 1


def test_engine_local_never_consults_claude(snapshot, tmp_path):
    class Exploding:
        def unavailable_reason(self):
            raise AssertionError("must not be consulted")

    result = _assistant(tmp_path, Exploding()).ask("spy price", snapshot, prefer="local")
    assert result["engine"] == "local"


def test_auto_carries_the_reason_as_a_note(snapshot, tmp_path):
    result = _assistant(tmp_path, _Unavailable("the `anthropic` package is not installed")).ask(
        "spy price", snapshot, prefer="auto"
    )
    assert result["engine"] == "local"
    assert "not installed" in result["note"]


def test_unknown_engine_is_rejected(snapshot, tmp_path):
    with pytest.raises(ValueError, match="unknown engine"):
        _assistant(tmp_path, _Working()).ask("spy price", snapshot, prefer="gpt")


def test_unavailable_reason_names_the_missing_key(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    reason = ClaudeEngine(Settings()).unavailable_reason()
    assert "ANTHROPIC_API_KEY" in reason
