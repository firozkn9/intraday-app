"""Build the market-data service from the environment (engine only: it holds the keys)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.env import EnvSettings
from intraday.core.safety import normalize_base_url, paper_mode_problems
from intraday.marketdata.alpaca.clients import AlpacaDataClient, AlpacaReferenceClient
from intraday.marketdata.alpaca.http import DATA_BASE_URL, AlpacaHttp, Credentials, TokenBucket
from intraday.marketdata.alpaca.stream import AlpacaBarStream
from intraday.marketdata.service import MarketDataService

RATE_LIMIT_HEADROOM = 0.9


def build_market_data(env: EnvSettings, sessionmaker: async_sessionmaker[AsyncSession], *,
                      feed: str) -> tuple[MarketDataService | None, str | None]:
    """Returns (service, None) or (None, reason it is disabled)."""
    if paper_mode_problems(env.trading_mode, env.alpaca_base_url):
        return None, "paper guard failed; Alpaca clients are not constructed"
    if not env.alpaca_keys_present:
        return None, "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set"
    if env.alpaca_api_key_id is None or env.alpaca_api_secret_key is None:  # pragma: no cover
        return None, "Alpaca keys missing"
    creds = Credentials(env.alpaca_api_key_id.get_secret_value(),
                        env.alpaca_api_secret_key.get_secret_value())
    data = AlpacaDataClient(AlpacaHttp(
        DATA_BASE_URL, creds,
        limiter=TokenBucket(int(env.alpaca_data_rate_limit * RATE_LIMIT_HEADROOM))))
    reference = AlpacaReferenceClient(
        AlpacaHttp(normalize_base_url(env.alpaca_base_url), creds,
                   limiter=TokenBucket(int(env.alpaca_trading_rate_limit * RATE_LIMIT_HEADROOM))),
        trading_mode=env.trading_mode)
    stream = AlpacaBarStream(creds, feed)
    return MarketDataService(sessionmaker, data, reference, stream, feed=feed,
                             stream_limit=env.alpaca_stream_symbol_limit), None
