"""The single place a broker is constructed (docs/design/03 §3, 08 §2).

Only the paper broker can be built for real data. SimBroker is allowed only
together with replayed data (ENGINE_DATA=replay), never alongside a live feed.
"""

from __future__ import annotations

import os

from intraday.core.env import EnvSettings
from intraday.core.safety import assert_paper_mode, normalize_base_url
from intraday.marketdata.alpaca.http import AlpacaHttp, Credentials, TokenBucket
from intraday.trading.broker.alpaca_paper import AlpacaPaperBroker
from intraday.trading.broker.base import Broker
from intraday.trading.broker.sim_broker import SimBroker

RATE_LIMIT_HEADROOM = 0.9


class BrokerConfigError(RuntimeError):
    pass


def build_broker(env: EnvSettings) -> Broker:
    kind = os.environ.get("ENGINE_BROKER", "alpaca_paper")
    data = os.environ.get("ENGINE_DATA", "live")
    if kind == "sim":
        if data != "replay":
            raise BrokerConfigError("ENGINE_BROKER=sim is only allowed with ENGINE_DATA=replay")
        return SimBroker()
    if kind != "alpaca_paper":
        raise BrokerConfigError(f"unknown ENGINE_BROKER {kind!r}")
    assert_paper_mode(env.trading_mode, env.alpaca_base_url)   # second, independent guard
    if not env.alpaca_keys_present or env.alpaca_api_key_id is None \
            or env.alpaca_api_secret_key is None:
        raise BrokerConfigError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set")
    creds = Credentials(env.alpaca_api_key_id.get_secret_value(),
                        env.alpaca_api_secret_key.get_secret_value())
    http = AlpacaHttp(normalize_base_url(env.alpaca_base_url), creds,
                      limiter=TokenBucket(int(env.alpaca_trading_rate_limit * RATE_LIMIT_HEADROOM)))
    return AlpacaPaperBroker(http, creds, trading_mode=env.trading_mode)
