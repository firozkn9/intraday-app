"""Asset eligibility E1-E7 (CLAUDE.md "Tradable assets"; docs/design/07 §2a)."""

from datetime import UTC, datetime, timedelta

import pytest

from intraday.core.assets import AlpacaAsset, SymbolMeta, evaluate, is_eligible_common_stock

NOW = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)


def asset(symbol: str, name: str, exchange: str = "NASDAQ", cls: str = "us_equity",
          status: str = "active", tradable: bool = True, age_days: int = 0) -> AlpacaAsset:
    return AlpacaAsset(symbol, name, cls, exchange, status, tradable,
                       NOW - timedelta(days=age_days))


def stock(symbol: str, country: str = "US") -> SymbolMeta:
    return SymbolMeta(symbol, "TRADABLE", "COMMON_STOCK", country)


def test_plain_us_common_stock_is_eligible() -> None:
    e = evaluate(stock("AAPL"), asset("AAPL", "Apple Inc. Common Stock"), NOW)
    assert e.eligible and e.failed == []
    assert set(e.rules) == {"E1", "E2", "E3", "E4", "E5", "E6", "E7"}


def test_share_class_suffix_allowed() -> None:
    assert is_eligible_common_stock(
        stock("BRK.B"), asset("BRK.B", "Berkshire Hathaway Inc. Class B", "NYSE"), NOW)


@pytest.mark.parametrize(("meta", "alp", "failing"), [
    # Context ETFs are never tradable, whatever else is true.
    (SymbolMeta("SPY", "CONTEXT", "ETF", "US"),
     asset("SPY", "SPDR S&P 500 ETF Trust", "ARCA"), {"E1", "E2", "E4", "E7"}),
    # Leveraged ETF entered as if it were stock: heuristics and exchange still reject.
    (stock("TQQQ"), asset("TQQQ", "ProShares UltraPro QQQ 3x Leveraged ETF"), {"E7"}),
    # Preferred share suffix.
    (stock("BAC.PRL"), asset("BAC.PRL", "Bank of America 7.25% Preferred Series L", "NYSE"),
     {"E6", "E7"}),
    # Warrant.
    (stock("ACMEW"), asset("ACMEW", "Acme Corp Warrant"), {"E7"}),
    # ADR (non-U.S. issuer).
    (stock("TSM", "TW"), asset("TSM", "Taiwan Semiconductor Manufacturing Company Ltd ADR",
                               "NYSE"), {"E3", "E7"}),
    # Foreign ordinary shares listed directly (D5: U.S. issuers only).
    (stock("SHOP", "CA"), asset("SHOP", "Shopify Inc. Class A Subordinate Voting Shares",
                                "NYSE"), {"E3"}),
    # OTC listing.
    (stock("ABCDF"), asset("ABCDF", "Some Company Ordinary Shares", "OTC"), {"E4"}),
    # NYSE Arca / AMEX are not NYSE/Nasdaq.
    (stock("XYZ"), asset("XYZ", "Xyz Holdings Common Stock", "AMEX"), {"E4"}),
    # Non-equity asset classes.
    (stock("BTCUSD"), asset("BTCUSD", "Bitcoin", "CRYPTO", cls="crypto"), {"E4", "E6"}),
    # Halted / inactive / not tradable.
    (stock("OLD"), asset("OLD", "Old Co Common Stock", status="inactive"), {"E5"}),
    (stock("NOPE"), asset("NOPE", "Nope Inc Common Stock", tradable=False), {"E5"}),
])
def test_ineligible(meta: SymbolMeta, alp: AlpacaAsset, failing: set[str]) -> None:
    e = evaluate(meta, alp, NOW)
    assert not e.eligible
    assert set(e.failed) == failing


def test_missing_or_stale_asset_data_fails_closed() -> None:
    assert evaluate(stock("AAPL"), None, NOW).failed == ["E4", "E5", "E7"]
    stale = evaluate(stock("AAPL"), asset("AAPL", "Apple Inc.", age_days=8), NOW)
    assert stale.failed == ["E4", "E5", "E7"]
    assert evaluate(stock("AAPL"), asset("AAPL", "Apple Inc.", age_days=6), NOW).eligible


def test_heuristic_word_boundaries() -> None:
    """'UnitedHealth' must not trip the 'Unit' rule; 'Fundamental' not 'Fund'."""
    for name in ("UnitedHealth Group Incorporated", "Fundamental Global Inc",
                 "Indexed Tech Corp Common Stock"):
        assert evaluate(stock("UNH"), asset("UNH", name, "NYSE"), NOW).eligible, name


def test_json_shape() -> None:
    out = evaluate(stock("AAPL"), None, NOW).to_json()
    assert out["eligible"] is False
    assert out["rules"]["E4"] == {"passed": False, "detail": "Alpaca asset data not synced yet"}


def test_from_api_parses_documented_fields() -> None:
    data = {"id": "b0b6dd9d", "symbol": "AAPL", "name": "Apple Inc. Common Stock",
            "class": "us_equity", "exchange": "NASDAQ", "status": "active", "tradable": True,
            "attributes": ["has_options"]}
    a = AlpacaAsset.from_api(data, NOW)
    assert (a.asset_class, a.exchange, a.tradable) == ("us_equity", "NASDAQ", True)
