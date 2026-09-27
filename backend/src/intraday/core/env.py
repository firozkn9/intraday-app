"""Process environment (secrets, safety switches, infrastructure).

Runtime trading settings live in the database (see ``runtime_config``); this
module only covers values that come from the process environment.

Precedence: real environment variables > ``.env.local`` > ``.env`` (repo root).
In Docker, Compose passes explicit variables and no env files exist in the image.
``.env.local`` is for local (non-Docker) runs, e.g. a ``localhost`` database.
Set INTRADAY_SKIP_ENV_FILES=1 to ignore both files (the test suite does).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_FILES = (_REPO_ROOT / ".env", _REPO_ROOT / ".env.local")
PLACEHOLDER_MARKERS = ("CHANGE_ME", "PLACEHOLDER")


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # Safety
    trading_mode: str = Field(default="", alias="TRADING_MODE")
    alpaca_base_url: str = Field(default="", alias="ALPACA_BASE_URL")

    # Broker / AI credentials (engine only; optional until Phase 7 / 9)
    alpaca_api_key_id: SecretStr | None = Field(default=None, alias="ALPACA_API_KEY_ID")
    alpaca_api_secret_key: SecretStr | None = Field(default=None, alias="ALPACA_API_SECRET_KEY")
    alpaca_data_feed_default: str = Field(default="iex", alias="ALPACA_DATA_FEED_DEFAULT")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    # Alpaca plan limits (free/Basic plan values, verified 2026-09-26).
    alpaca_stream_symbol_limit: int = Field(default=30, alias="ALPACA_STREAM_SYMBOL_LIMIT")
    alpaca_data_rate_limit: int = Field(default=200, alias="ALPACA_DATA_RATE_LIMIT")
    alpaca_trading_rate_limit: int = Field(default=200, alias="ALPACA_TRADING_RATE_LIMIT")

    # Infrastructure
    database_url: str = Field(
        default="postgresql+asyncpg://intraday:intraday@localhost:5432/intraday",
        alias="DATABASE_URL",
    )
    database_url_owner: str | None = Field(default=None, alias="DATABASE_URL_OWNER")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Web / auth (api only)
    session_secret: SecretStr = Field(default=SecretStr(""), alias="SESSION_SECRET")
    public_origin: str = Field(default="http://localhost:5173", alias="PUBLIC_ORIGIN")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")

    # Alert email (optional)
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: SecretStr = Field(default=SecretStr(""), alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="", alias="SMTP_FROM")
    alert_email_to: str = Field(default="", alias="ALERT_EMAIL_TO")

    # Misc
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    defaults_path: Path = Field(
        default=_REPO_ROOT / "config" / "defaults.yaml", alias="INTRADAY_DEFAULTS_PATH"
    )
    watchlist_seed_path: Path = Field(
        default=_REPO_ROOT / "config" / "watchlist.seed.yaml", alias="INTRADAY_WATCHLIST_SEED_PATH"
    )

    @property
    def alpaca_keys_present(self) -> bool:
        key = self.alpaca_api_key_id.get_secret_value() if self.alpaca_api_key_id else ""
        secret = self.alpaca_api_secret_key.get_secret_value() if self.alpaca_api_secret_key else ""
        return all(v and not is_placeholder(v) for v in (key, secret))

    @property
    def anthropic_key_present(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value())


def env_files() -> tuple[Path, ...]:
    if os.environ.get("INTRADAY_SKIP_ENV_FILES"):
        return ()
    return tuple(p for p in ENV_FILES if p.is_file())


def is_placeholder(value: str) -> bool:
    return any(m in value.upper() for m in PLACEHOLDER_MARKERS)


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    return EnvSettings(_env_file=env_files() or None)
