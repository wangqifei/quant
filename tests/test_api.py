import pytest
from fastapi.testclient import TestClient

from app import main
from app.assistant import Assistant, ContextStore
from app.config import Settings
from app.market import MarketService
from app.providers import DemoProvider


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(providers=["demo"], context_path=tmp_path / "context.json")
    monkeypatch.setattr(main, "market", MarketService(settings, providers=[DemoProvider()]))
    monkeypatch.setattr(main, "assistant", Assistant(settings=settings, store=ContextStore(settings.context_path)))
    return TestClient(main.app)


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["instruments"] == ["SPX", "SPY"]
    assert body["providers"] == ["demo"]


def test_index_and_static_are_served(client):
    assert client.get("/").status_code == 200
    assert "S&amp;P 500" in client.get("/").text or "S&P 500" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_market_snapshot(client):
    body = client.get("/api/market?history=5").json()
    assert set(body["instruments"]) == {"SPX", "SPY"}
    assert len(body["instruments"]["SPY"]["bars"]) == 5
    quote = body["instruments"]["SPY"]["quote"]
    assert quote["price"] > 0
    assert "change_pct" in quote
    assert body["live"] is False


def test_market_history_is_clamped(client):
    assert len(client.get("/api/market?history=0").json()["instruments"]["SPX"]["bars"]) == 2


def test_single_symbol_endpoint(client):
    body = client.get("/api/market/spy?history=3").json()
    assert body["quote"]["symbol"] == "SPY"
    assert len(body["bars"]) == 3
    assert body["metrics"]["last"] > 0


def test_unknown_symbol_404(client):
    assert client.get("/api/market/tsla").status_code == 404


def test_ask_returns_a_local_answer(client):
    body = client.post("/api/ask", json={"question": "what is SPY trading at"}).json()
    assert body["engine"] == "local"
    assert "SPY" in body["answer"]


def test_ask_rejects_an_empty_question(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_ask_rejects_an_unknown_engine(client):
    assert client.post("/api/ask", json={"question": "hi", "engine": "gpt"}).status_code == 422


def test_context_crud(client):
    assert client.get("/api/context").json() == {"notes": []}

    note = client.post("/api/context", json={"text": "Long 200 SPY at 551"}).json()
    assert note["text"] == "Long 200 SPY at 551"
    assert client.get("/api/context").json()["notes"][0]["id"] == note["id"]

    assert client.delete(f"/api/context/{note['id']}").status_code == 200
    assert client.get("/api/context").json() == {"notes": []}


def test_delete_unknown_note_404(client):
    assert client.delete("/api/context/nope").status_code == 404


def test_context_rejects_blank_text(client):
    assert client.post("/api/context", json={"text": "  "}).status_code == 400
    assert client.post("/api/context", json={"text": ""}).status_code == 422
