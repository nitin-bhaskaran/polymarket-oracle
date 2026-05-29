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
            "risk": {
                "max_position_pct": 10.0,
                "min_edge": 5.0,
                "use_kelly_sizing": False,
            },
            "polymarket": {"private_key": "YOUR_PRIVATE_KEY_HERE"},
        }
    )
    trader.initialize()

    trade = trader.execute_trade(
        market=make_market(),
        assessment=make_assessment(probability=0.40),
        available_capital=100.0,
    )

    # Non-Kelly sizing scales the 10% ceiling by confidence (0.75) -> $7.50.
    assert trade.success is True
    assert trade.order_id == "DRY_RUN"
    assert trade.side == Side.BUY
    assert trade.outcome == Outcome.NO
    assert trade.token_id == "no-token"
    assert trade.total_cost == 7.5
    assert trade.size == 18.75  # 7.50 / 0.40 NO price


def test_execute_trade_never_spends_more_than_available_capital():
    trader = Trader(
        {
            "risk": {
                "max_position_pct": 200.0,
                "min_edge": 5.0,
                "use_kelly_sizing": False,
            },
            "polymarket": {"private_key": "YOUR_PRIVATE_KEY_HERE"},
        }
    )
    trader.initialize()

    trade = trader.execute_trade(
        market=make_market(yes_price=0.50),
        assessment=make_assessment(probability=0.70, market_price=0.50),
        available_capital=42.0,
    )

    # Even with a 200% ceiling, the fraction is clamped to 1.0 of capital.
    assert trade is not None
    assert trade.total_cost == 42.0


def test_execute_trade_reconciles_fill_from_order_details():
    class FakeClient:
        def create_and_post_market_order(self, order_args, options, order_type):
            return {"orderID": "order-1"}

        def get_order(self, order_id):
            assert order_id == "order-1"
            return {"price": "0.625", "size_matched": "16"}

        def get_trades(self, params, only_first_page=False):
            return []

    trader = Trader(
        {
            "risk": {"max_position_pct": 10.0, "min_edge": 5.0},
            "polymarket": {"private_key": "not-used", "tick_size": "0.001"},
        }
    )
    trader.client = FakeClient()
    trader._initialized = True

    trade = trader.execute_trade(
        market=make_market(yes_price=0.60),
        assessment=make_assessment(probability=0.80, market_price=0.60),
        available_capital=100.0,
    )

    assert trade.order_id == "order-1"
    assert trade.price == 0.625
    assert trade.size == 16.0
    assert trade.total_cost == 10.0


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
    # Estimate applies the default 150bps SELL haircut: 0.30 * 0.985 = 0.2955.
    # Proceeds = 10 shares * 0.2955 = 2.955; realized = 2.955 - 6.0 cost basis.
    assert trade.price == 0.2955
    assert trade.total_cost == 2.955
    assert trade.realized_pnl == -3.045
