"""Tests for the Betfair pivot foundation: models, fills, sizing."""

from core.betfair_models import (
    BetfairMarket, BetSide, OrderStyle, PaperBet, PaperBetStatus,
    PriceLevel, Runner,
)
from core.betfair_fills import (
    simulate_cross_fill, update_passive, expire_if_stale, settle,
)
from core.betfair_sizing import OddsSizingInputs, OddsSizingConfig, compute_bet_size


def make_market() -> BetfairMarket:
    return BetfairMarket(
        market_id="1.1", market_name="Match Odds", sport="Soccer",
        runners=[
            Runner(selection_id=1, name="Home",
                   available_to_back=[PriceLevel(price=2.0, size=100)],
                   available_to_lay=[PriceLevel(price=2.04, size=100)]),
            Runner(selection_id=2, name="Draw",
                   available_to_back=[PriceLevel(price=3.4, size=100)],
                   available_to_lay=[PriceLevel(price=3.5, size=100)]),
            Runner(selection_id=3, name="Away",
                   available_to_back=[PriceLevel(price=4.0, size=100)],
                   available_to_lay=[PriceLevel(price=4.2, size=100)]),
        ],
    )


# ── Overround / fair probability ──

def test_overround_above_one():
    m = make_market()
    assert m.overround > 1.0


def test_fair_probs_sum_to_one():
    m = make_market()
    total = sum(m.fair_implied_prob(r) for r in m.runners)
    assert abs(total - 1.0) < 1e-9


def test_fair_prob_below_raw():
    m = make_market()
    home = m.runners[0]
    assert m.fair_implied_prob(home) < home.raw_implied_prob


# ── Edge direction ──

def test_back_edge_when_ai_above_market():
    from core.betfair_models import BetfairAssessment
    m = make_market()
    home = m.runners[0]
    a = BetfairAssessment(market_id="1.1", selection_id=1, runner_name="Home",
                          question="?", estimated_probability=0.58, confidence=0.8,
                          market_fair_prob=m.fair_implied_prob(home))
    a.calculate_edge()
    assert a.edge > 0 and a.recommended_side == BetSide.BACK


def test_lay_edge_when_ai_below_market():
    from core.betfair_models import BetfairAssessment
    m = make_market()
    home = m.runners[0]
    a = BetfairAssessment(market_id="1.1", selection_id=1, runner_name="Home",
                          question="?", estimated_probability=0.30, confidence=0.8,
                          market_fair_prob=m.fair_implied_prob(home))
    a.calculate_edge()
    assert a.edge < 0 and a.recommended_side == BetSide.LAY


# ── Sizing ──

def cfg(**kw) -> OddsSizingConfig:
    base = dict(max_position_pct=0.10, kelly_fraction=0.25, min_stake=1.0, use_kelly=True)
    base.update(kw)
    return OddsSizingConfig(**base)


def test_back_size_scales_with_edge():
    small = compute_bet_size(OddsSizingInputs(1000, 2.0, 0.52, 1.0, BetSide.BACK), cfg())
    large = compute_bet_size(OddsSizingInputs(1000, 2.0, 0.65, 1.0, BetSide.BACK), cfg())
    assert 0 < small["stake"] < large["stake"]


def test_back_no_bet_when_no_edge():
    # fair prob below break-even for the odds -> no Kelly stake
    r = compute_bet_size(OddsSizingInputs(1000, 2.0, 0.45, 1.0, BetSide.BACK), cfg())
    assert r["stake"] == 0.0


def test_lay_liability_clamped_to_ceiling():
    r = compute_bet_size(OddsSizingInputs(1000, 2.0, 0.10, 1.0, BetSide.LAY),
                         cfg(max_position_pct=0.10))
    assert r["liability"] <= 100.0  # 10% of 1000


def test_min_stake_skips_dust():
    r = compute_bet_size(OddsSizingInputs(5, 2.0, 0.55, 0.3, BetSide.BACK),
                         cfg(min_stake=1.0))
    assert r["stake"] == 0.0


def test_invalid_odds_returns_zero():
    r = compute_bet_size(OddsSizingInputs(1000, 1.0, 0.7, 1.0, BetSide.BACK), cfg())
    assert r["stake"] == 0.0


# ── Cross fills ──

def test_cross_back_fills_at_acceptable_price():
    m = make_market()
    bet = PaperBet(bet_id="b", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.CROSS, requested_odds=2.0, stake=50)
    bet = simulate_cross_fill(bet, m)
    assert bet.status == PaperBetStatus.FILLED
    assert bet.filled_odds == 2.0


def test_cross_back_unfilled_when_price_worse():
    m = make_market()
    # demand odds of 2.5 but only 2.0 available -> no fill
    bet = PaperBet(bet_id="b", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.CROSS, requested_odds=2.5, stake=50)
    bet = simulate_cross_fill(bet, m)
    assert bet.status == PaperBetStatus.UNFILLED


def test_cross_partial_fill_limited_by_depth():
    m = make_market()
    m.runners[0].available_to_back = [PriceLevel(price=2.0, size=10)]
    bet = PaperBet(bet_id="b", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.CROSS, requested_odds=2.0, stake=50)
    bet = simulate_cross_fill(bet, m)
    assert bet.status == PaperBetStatus.FILLED
    assert bet.stake == 10  # only the available depth filled


# ── Passive fills ──

def test_passive_stays_pending_until_traded_through():
    r = Runner(selection_id=1, available_to_back=[PriceLevel(price=2.0, size=50)])
    bet = PaperBet(bet_id="p", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.PASSIVE, requested_odds=2.10, stake=20)
    bet = update_passive(bet, r)
    assert bet.status == PaperBetStatus.PENDING


def test_passive_fills_at_limit_when_market_reaches_it():
    r = Runner(selection_id=1, available_to_back=[PriceLevel(price=2.15, size=50)])
    bet = PaperBet(bet_id="p", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.PASSIVE, requested_odds=2.10, stake=20)
    bet = update_passive(bet, r)
    assert bet.status == PaperBetStatus.FILLED
    assert bet.filled_odds == 2.10  # our limit, not the better 2.15


def test_passive_expires_when_stale():
    from datetime import datetime, timezone, timedelta
    bet = PaperBet(bet_id="p", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.PASSIVE, requested_odds=2.10, stake=20)
    bet.placed_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    bet = expire_if_stale(bet, max_age_seconds=60)
    assert bet.status == PaperBetStatus.UNFILLED


# ── Settlement ──

def test_back_win_applies_commission():
    bet = PaperBet(bet_id="b", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.CROSS, requested_odds=2.0, stake=100,
                   filled_odds=2.0, status=PaperBetStatus.FILLED, commission_rate=0.05)
    bet = settle(bet, won=True)
    assert bet.gross_pnl == 100.0          # 100 * (2.0 - 1)
    assert abs(bet.net_pnl - 95.0) < 1e-6  # commission on winnings


def test_back_loss_loses_stake():
    bet = PaperBet(bet_id="b", market_id="1.1", selection_id=1, side=BetSide.BACK,
                   style=OrderStyle.CROSS, requested_odds=2.0, stake=100,
                   filled_odds=2.0, status=PaperBetStatus.FILLED)
    bet = settle(bet, won=False)
    assert bet.gross_pnl == -100.0
    assert bet.net_pnl == -100.0  # no commission on losses


def test_lay_loss_equals_liability():
    bet = PaperBet(bet_id="L", market_id="1.1", selection_id=1, side=BetSide.LAY,
                   style=OrderStyle.CROSS, requested_odds=3.0, stake=50,
                   liability=100, filled_odds=3.0, status=PaperBetStatus.FILLED)
    bet = settle(bet, won=False)  # runner won -> lay loses liability
    assert bet.gross_pnl == -100.0   # 50 * (3.0 - 1)


def test_attribution_bands():
    assert PaperBet.band_edge(0.03) == "<5%"
    assert PaperBet.band_edge(0.06) == "5-8%"
    assert PaperBet.band_edge(0.10) == "8-12%"
    assert PaperBet.band_edge(0.20) == ">12%"
    assert PaperBet.band_confidence(0.4) == "low"
    assert PaperBet.band_confidence(0.6) == "med"
    assert PaperBet.band_confidence(0.9) == "high"
