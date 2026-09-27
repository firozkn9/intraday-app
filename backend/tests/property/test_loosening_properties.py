"""Property tests for the loosen/tighten comparator."""

from hypothesis import given
from hypothesis import strategies as st

from intraday.core.runtime_config import FIELD_SPECS, is_loosening

DIRECTIONAL = [s for s in FIELD_SPECS.values() if s.loosen in ("higher", "lower")]


@given(spec=st.sampled_from(DIRECTIONAL), a=st.integers(0, 10_000), b=st.integers(0, 10_000))
def test_loosening_is_antisymmetric(spec, a, b) -> None:  # type: ignore[no-untyped-def]
    assert not (is_loosening(spec, a, b) and is_loosening(spec, b, a))
    assert is_loosening(spec, a, a) is False


@given(spec=st.sampled_from(DIRECTIONAL), a=st.integers(0, 10_000), b=st.integers(0, 10_000))
def test_exactly_one_direction_loosens_when_values_differ(spec, a, b) -> None:  # type: ignore[no-untyped-def]
    if a != b:
        assert is_loosening(spec, a, b) != is_loosening(spec, b, a)


@given(old=st.sets(st.sampled_from(["earnings", "fda", "merger_acquisition", "trading_halt"])),
       new=st.sets(st.sampled_from(["earnings", "fda", "merger_acquisition", "trading_halt"])))
def test_removing_block_tags_loosens(old, new) -> None:  # type: ignore[no-untyped-def]
    spec = FIELD_SPECS["news.block_event_tags"]
    assert is_loosening(spec, sorted(old), sorted(new)) == bool(old - new)
