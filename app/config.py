"""Runtime configuration, all overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # Provider chain, tried in order until one succeeds. "demo" is the
    # offline synthetic source and should stay last.
    providers: list[str] = field(
        default_factory=lambda: [
            p.strip()
            for p in os.environ.get("QUANT_PROVIDERS", "yahoo,stooq,demo").split(",")
            if p.strip()
        ]
    )
    lookback_days: int = _int("QUANT_LOOKBACK_DAYS", 420)
    cache_ttl_seconds: int = _int("QUANT_CACHE_TTL", 60)
    context_path: Path = ROOT / "data" / "context.json"

    # Yahoo transport. "auto" uses curl_cffi's browser TLS impersonation when
    # installed (needed when Yahoo 429s a plain client), "off" forces httpx,
    # or name a curl_cffi target such as "chrome" / "safari".
    yahoo_impersonate: str = os.environ.get("YAHOO_IMPERSONATE", "auto")

    # Futu / moomoo OpenAPI (used only when "futu" is in the provider chain).
    # Requires the FutuOpenD gateway running locally and logged in.
    futu_host: str = os.environ.get("FUTU_HOST", "127.0.0.1")
    futu_port: int = _int("FUTU_PORT", 11111)
    futu_security_firm: str = os.environ.get("FUTU_SECURITY_FIRM", "FUTUSECURITIES")
    # Futu's own ticker spellings. Override if your account uses different
    # index codes - `python -m app.diagnose --futu-codes` lists what it offers.
    futu_codes: dict[str, str] = field(
        default_factory=lambda: {
            "SPX": os.environ.get("FUTU_CODE_SPX", "US.SPX"),
            "SPY": os.environ.get("FUTU_CODE_SPY", "US.SPY"),
        }
    )

    # Assistant
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
    anthropic_max_tokens: int = _int("ANTHROPIC_MAX_TOKENS", 16000)

    @property
    def has_api_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


settings = Settings()
