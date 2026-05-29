"""Tests for confidence/spread-aware fractional Kelly sizing."""

from core.sizing import SizingConfig, SizingInputs, compute_position_size


def cfg(**kw) -> SizingConfig:
    base = dict(max_position_pct=0.10, kelly_fraction=0.25, min_trade_usd=1.0, use_kelly=True)
    base.update(kw)
    return SizingConfig(**base)


def test_kelly_scales_with_edge():
    """A bigger edge yields a bigger position, all else equal."""
    small = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.50,
                     fair_probability=0.55, confidence=1.0),
        cfg(),
    )
    large = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.50,
                     fair_probability=0.70, confidence=1.0),
        cfg(),
    )
    assert 0 < small < large


def test_kelly_scales_with_confidence():
    """Lower confidence sizes the position down."""
    high = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.50,
                     fair_probability=0.65, confidence=0.9),
        cfg(),
    )
    low = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.50,
                     fair_probability=0.65, confidence=0.3),
        cfg(),
    )
    assert 0 < low < high


def test_size_clamped_to_max_position_ceiling():
    """A huge edge can never exceed max_position_pct of capital."""
    spend = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.10,
                     fair_probability=0.95, confidence=1.0),
        cfg(max_position_pct=0.10),
    )
    assert spend <= 100.0  # 10% of 1000


def test_no_trade_when_edge_does_not_survive_spread():
    """If the half-spread eats the edge, size is zero."""
    spend = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.50,
                     fair_probability=0.52, confidence=1.0, spread=0.06),
        cfg(),
    )
    assert spend == 0.0


def test_no_trade_when_below_min_trade():
    """Tiny capital or tiny fraction yields a skip, not a dust trade."""
    spend = compute_position_size(
        SizingInputs(available_capital=5, entry_price=0.50,
                     fair_probability=0.55, confidence=0.3),
        cfg(min_trade_usd=1.0),
    )
    assert spend == 0.0


def test_zero_when_fair_below_price():
    """Buying a token whose fair prob is below its price is never sized."""
    spend = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.60,
                     fair_probability=0.55, confidence=1.0),
        cfg(),
    )
    assert spend == 0.0


def test_invalid_prices_return_zero():
    for p in (0.0, 1.0, -0.1, 1.5):
        spend = compute_position_size(
            SizingInputs(available_capital=1000, entry_price=p,
                         fair_probability=0.7, confidence=1.0),
            cfg(),
        )
        assert spend == 0.0


def test_non_kelly_mode_scales_ceiling_by_confidence():
    spend = compute_position_size(
        SizingInputs(available_capital=1000, entry_price=0.50,
                     fair_probability=0.60, confidence=0.5),
        cfg(use_kelly=False, max_position_pct=0.10),
    )
    # 10% ceiling * 0.5 confidence * 1000 = 50
    assert spend == 50.0


def test_never_exceeds_available_capital():
    spend = compute_position_size(
        SizingInputs(available_capital=8, entry_price=0.50,
                     fair_probability=0.99, confidence=1.0),
        cfg(use_kelly=False, max_position_pct=5.0),  # absurd ceiling
    )
    assert spend <= 8.0
