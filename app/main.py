"""FastAPI application: JSON endpoints plus the static dashboard."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics
from .assistant import Assistant, AssistantError, EngineUnavailable
from .config import ROOT, settings
from .market import MarketService
from .providers import INSTRUMENTS, ProviderError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

WEB_DIR = ROOT / "web"

app = FastAPI(title="Quant Dashboard", version="0.1.0")
market = MarketService(settings)
assistant = Assistant(settings=settings)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    engine: str = Field(default="auto", pattern="^(auto|claude|local)$")


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "instruments": list(INSTRUMENTS),
        "providers": [p.name for p in market.providers],
        "assistant": _assistant_health(),
    }


def _assistant_health() -> dict:
    """Claude availability, derived from a single source of truth."""
    reason = assistant.claude.unavailable_reason()
    return {
        "claude_available": reason is None,
        "claude_unavailable_reason": reason,
        "model": settings.anthropic_model if reason is None else None,
    }


@app.get("/api/market")
def get_market(refresh: bool = False, history: int = 260) -> dict:
    try:
        return market.snapshot(force=refresh, history=max(2, min(history, 2000)))
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/market/{symbol}")
def get_symbol(symbol: str, refresh: bool = False, history: int = 260) -> dict:
    key = symbol.upper()
    if key not in INSTRUMENTS:
        raise HTTPException(status_code=404, detail=f"unknown instrument: {symbol}")
    try:
        series, errors = market.series(key, force=refresh)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        **series.to_dict(history=max(2, min(history, 2000))),
        "metrics": analytics.summarize(series.bars),
        "errors": errors,
    }


@app.post("/api/ask")
def ask(request: AskRequest) -> dict:
    try:
        snapshot = market.snapshot()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        return assistant.ask(
            request.question,
            snapshot,
            history=request.history,
            prefer=request.engine,
        )
    except EngineUnavailable as exc:
        # The caller asked for Claude specifically; say why it cannot run
        # rather than quietly answering with a different engine.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AssistantError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/context")
def list_context() -> dict:
    return {"notes": [n.to_dict() for n in assistant.store.list()]}


@app.post("/api/context")
def add_context(request: NoteRequest) -> dict:
    try:
        note = assistant.store.add(request.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return note.to_dict()


@app.delete("/api/context/{note_id}")
def delete_context(note_id: str) -> dict:
    if not assistant.store.remove(note_id):
        raise HTTPException(status_code=404, detail="note not found")
    return {"deleted": note_id}


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
