"""Runtime configuration, all overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Published list prices, USD per million tokens (input, output).
# Cached 2026-06-24 - prices change, so treat any cost figure derived from
# this as an estimate and check the console for actual billing. Override a
# model's rates with QUANT_PRICE_INPUT / QUANT_PRICE_OUTPUT.
MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
    price_input: float | None = _float("QUANT_PRICE_INPUT", None)
    price_output: float | None = _float("QUANT_PRICE_OUTPUT", None)

    def rates_for(self, model: str) -> tuple[float, float] | None:
        """(input, output) USD per million tokens, or None if unknown."""
        listed = MODEL_PRICING_USD_PER_MTOK.get(model)
        inp = self.price_input if self.price_input is not None else (listed[0] if listed else None)
        out = self.price_output if self.price_output is not None else (listed[1] if listed else None)
        if inp is None or out is None:
            return None
        return inp, out

    @property
    def has_api_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def anthropic_profile_dir(self) -> Path:
        """Where the `ant` CLI stores OAuth profiles."""
        configured = os.environ.get("ANTHROPIC_CONFIG_DIR")
        if configured:
            return Path(configured)
        if os.name == "nt":
            return Path(os.environ.get("APPDATA", "~")).expanduser() / "Anthropic"
        return Path.home() / ".config" / "anthropic"

    @property
    def has_oauth_profile(self) -> bool:
        """True when `ant auth login` has stored credentials the SDK can use.

        A bare ``Anthropic()`` picks these up with no environment variable, so
        an API key is not the only way to authenticate.
        """
        credentials = self.anthropic_profile_dir / "credentials"
        try:
            return credentials.is_dir() and any(credentials.glob("*.json"))
        except OSError:
            return False

    @property
    def has_credentials(self) -> bool:
        return self.has_api_key or self.has_oauth_profile


settings = Settings()
