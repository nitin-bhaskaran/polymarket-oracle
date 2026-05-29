from core.models import Market, Outcome, Position, ProbabilityAssessment, Side
from core.trader import Trader


def make_market(**overrides) -> Market:
    defaults = {
        "condition_id": "condition-1",
        "question": "Will the test pass?",
        "slug": "will-the-test-pass",
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "yes_price": 0.60,
        "no_price": 0.40,
        "liquidity": 10_000,
        "volume_24h": 5_000,
    }
    defaults.update(overrides)
    return Market(**defaults)


def make_assessment(probability: float, market_price: float = 0.60) -> ProbabilityAssessment:
    assessment = ProbabilityAssessment(
        market_condition_id="condition-1",
        question="Will the test pass?",
        estimated_probability=probability,
        confidence=0.75,
        reasoning="Test reasoning",
        market_price=market_price,
    )
    assessment.calculate_edge()
    return assessment


def test_execute_trade_buys_no_token_when_probability_is_below_market():
    trader = Trader(
        {
            "risk": {"max_position_pct": 10.0, "min_edge": 5.0},
            "polymarket": {"private_key": "YOUR_PRIVATE_KEY_HERE"},
        }
    )
    trader.initialize()

    trade = trader.execute_trade(
        market=make_market(),
        assessment=make_assessment(probability=0.40),
        available_capital=100.0,
    )

    assert trade.success is True
    assert trade.order_id == "DRY_RUN"
    assert trade.side == Side.BUY
    assert trade.outcome == Outcome.NO
    assert trade.token_id == "no-token"
    assert trade.total_cost == 10.0
    assert trade.size == 25.0


def test_execute_trade_never_spends_more_than_available_capital():
    trader = Trader(
        {
            "risk": {"max_position_pct": 200.0, "min_edge": 5.0},
            "polymarket": {"private_key": "YOUR_PRIVATE_KEY_HERE"},
        }
    )
    trader.initialize()

    trade = trader.execute_trade(
        market=make_market(yes_price=0.50),
        assessment=make_assessment(probability=0.70, market_price=0.50),
        available_capital=42.0,
    )

    assert trade is not None
    assert trade.total_cost == 42.0


def test_close_position_returns_realized_pnl_in_dry_run():
    trader = Trader({"polymarket": {"private_key": "YOUR_PRIVATE_KEY_HERE"}})
    trader.initialize()
    position = Position(
        market_condition_id="condition-1",
        question="Will the test pass?",
        token_id="yes-token",
        side=Side.BUY,
        outcome=Outcome.YES,
        entry_price=0.60,
        size=10,
        cost_basis=6.0,
        current_price=0.30,
        current_value=3.0,
        unrealized_pnl=-3.0,
    )

    trade = trader.close_position(position)

    assert trade.success is True
    assert trade.order_id == "DRY_RUN_CLOSE"
    assert trade.side == Side.SELL
    assert trade.total_cost == 3.0
    assert trade.realized_pnl == -3.0
