"""Core data types shared across providers, analytics and the API layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date as Date
from typing import Any


@dataclass(frozen=True)
class Bar:
    """A single daily OHLCV bar."""

    date: Date
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d


@dataclass(frozen=True)
class Quote:
    """The latest observed price for one instrument."""

    symbol: str
    name: str
    price: float
    previous_close: float
    as_of: Date
    source: str
    currency: str = "USD"
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: int = 0

    @property
    def change(self) -> float:
        return self.price - self.previous_close

    @property
    def change_pct(self) -> float:
        if self.previous_close == 0:
            return 0.0
        return (self.price / self.previous_close - 1.0) * 100.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        d["change"] = self.change
        d["change_pct"] = self.change_pct
        return d


@dataclass
class Series:
    """A quote plus the daily history it was derived from."""

    quote: Quote
    bars: list[Bar] = field(default_factory=list)

    def to_dict(self, history: int | None = None) -> dict[str, Any]:
        bars = self.bars if history is None else self.bars[-history:]
        return {"quote": self.quote.to_dict(), "bars": [b.to_dict() for b in bars]}
