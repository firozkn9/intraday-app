"""Environment loading: .env / .env.local precedence and placeholder detection."""

from pathlib import Path

import pytest

from intraday.api.main import build_context
from intraday.core import env as env_module
from intraday.core.env import EnvSettings, get_env, is_placeholder


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    base, local = tmp_path / ".env", tmp_path / ".env.local"
    monkeypatch.setattr(env_module, "ENV_FILES", (base, local))
    monkeypatch.delenv("INTRADAY_SKIP_ENV_FILES", raising=False)
    for var in ("DATABASE_URL", "DATABASE_URL_OWNER", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    get_env.cache_clear()
    yield base, local
    get_env.cache_clear()


def test_env_local_overrides_env_and_real_env_wins(files: tuple[Path, Path],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    base, local = files
    base.write_text("DATABASE_URL_OWNER=postgresql+asyncpg://u:p@postgres:5432/intraday\n"
                    "LOG_LEVEL=WARNING\n", encoding="utf-8")
    local.write_text("DATABASE_URL_OWNER=postgresql+asyncpg://u:p@localhost:5432/intraday\n",
                     encoding="utf-8")
    env = get_env()
    assert env.database_url_owner is not None and "@localhost:" in env.database_url_owner
    assert env.log_level == "WARNING"  # from .env, not overridden

    get_env.cache_clear()
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert get_env().log_level == "DEBUG"  # real environment beats both files


def test_missing_files_are_fine(files: tuple[Path, Path]) -> None:
    assert get_env().database_url_owner is None


def test_skip_flag_ignores_files(files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    files[0].write_text("LOG_LEVEL=ERROR\n", encoding="utf-8")
    monkeypatch.setenv("INTRADAY_SKIP_ENV_FILES", "1")
    assert get_env().log_level == "INFO"


@pytest.mark.parametrize(("value", "placeholder"), [
    ("CHANGE_ME_generate_a_long_random_value", True), ("PK_PLACEHOLDER", True),
    ("change_me", True), ("s3cr3t-Very-Random-0123456789", False)])
def test_is_placeholder(value: str, placeholder: bool) -> None:
    assert is_placeholder(value) is placeholder


def test_placeholder_alpaca_keys_count_as_missing() -> None:
    env = EnvSettings.model_validate({"ALPACA_API_KEY_ID": "PK_PLACEHOLDER",
                                      "ALPACA_API_SECRET_KEY": "CHANGE_ME"})
    assert env.alpaca_keys_present is False
    real = EnvSettings.model_validate({"ALPACA_API_KEY_ID": "PKABCDEF",
                                       "ALPACA_API_SECRET_KEY": "abcdef123"})
    assert real.alpaca_keys_present is True


def test_api_refuses_placeholder_session_secret() -> None:
    env = EnvSettings.model_validate({"SESSION_SECRET": "CHANGE_ME_generate_a_long_random_value"})
    with pytest.raises(RuntimeError, match="placeholder"):
        build_context(env)
