"""
Trader — Executes trades on Polymarket via the CLOB API.

Handles order creation, submission, and status tracking.
Uses the py-clob-client-v2 SDK for authentication and signing.
"""

import logging
from typing import Optional

try:
    from py_clob_client_v2 import (
        ApiCreds,
        ClobClient,
        MarketOrderArgs,
        OrderPayload,
        OrderType,
        PartialCreateOrderOptions,
        Side as ClobSide,
        TradeParams,
    )
except ImportError:  # Allows dry-run tests without CLOB dependencies installed.
    ApiCreds = None
    ClobClient = None
    MarketOrderArgs = None
    OrderPayload = None
    OrderType = None
    PartialCreateOrderOptions = None
    ClobSide = None
    TradeParams = None

from core.models import (
    Market, Outcome, Position, ProbabilityAssessment, Side as TradeSide, Trade
)
from core.money import dec, price as quantized_price, shares, usdc
from core.sizing import SizingConfig, SizingInputs, compute_position_size

logger = logging.getLogger(__name__)


class Trader:
    """
    Executes trades on Polymarket's CLOB (Central Limit Order Book).

    The CLOB API requires authentication via API keys derived from
    your Polygon wallet. Orders are signed locally before submission.
    """

    def __init__(self, config: dict):
        poly_config = config.get("polymarket", {})
        risk_config = config.get("risk", {})

        self.private_key = poly_config.get("private_key", "")
        self.funder_address = poly_config.get("funder_address", "")
        self.signature_type = poly_config.get("signature_type", 1)
        self.clob_url = poly_config.get("clob_url", "https://clob.polymarket.com")
        self.chain_id = poly_config.get("chain_id", 137)
        self.clob_api_key = poly_config.get("clob_api_key", "")
        self.clob_api_secret = poly_config.get("clob_api_secret", "")
        self.clob_api_passphrase = poly_config.get("clob_api_passphrase", "")
        self.tick_size = self._normalize_tick_size(poly_config.get("tick_size", "0.01"))
        self.order_type = poly_config.get("order_type", "FOK")
        self.reconcile_fills = poly_config.get("reconcile_fills", True)

        # Risk parameters
        self.max_position_pct = risk_config.get("max_position_pct", 10.0) / 100
        self.min_edge = risk_config.get("min_edge", 5.0) / 100
        self.slippage_bps = risk_config.get("market_order_slippage_bps", 150)

        # Sizing parameters (confidence/spread-aware fractional Kelly)
        self.use_kelly = risk_config.get("use_kelly_sizing", True)
        self.kelly_fraction = risk_config.get("kelly_fraction", 0.25)

        # Dry run mode — log trades but don't execute
        self.dry_run = False

        # Initialize CLOB client
        self.client: Optional[object] = None
        self._initialized = False

    def initialize(self):
        """
        Initialize the CLOB client and derive API credentials.

        This involves:
        1. Creating a ClobClient with your private key
        2. Deriving API credentials (key, secret, passphrase)
        3. Reinitializing the client with those credentials

        Must be called before any trading operations.
        """
        if not self.private_key or self.private_key == "YOUR_PRIVATE_KEY_HERE":
            logger.warning("Private key not configured — running in read-only mode")
            self.dry_run = True
            return
        if ClobClient is None:
            logger.error("py-clob-client-v2 is not installed — running in dry-run mode")
            self.dry_run = True
            return

        try:
            # Step 1: Create initial client to derive API keys
            temp_client = ClobClient(
                host=self.clob_url,
                chain_id=self.chain_id,
                key=self.private_key,
                signature_type=self.signature_type,
                funder=self.funder_address or None,
            )

            # Step 2: Use configured L2 credentials, or derive them via L1 auth.
            if self.clob_api_key and self.clob_api_secret and self.clob_api_passphrase:
                api_creds = ApiCreds(
                    api_key=self.clob_api_key,
                    api_secret=self.clob_api_secret,
                    api_passphrase=self.clob_api_passphrase,
                )
                logger.info("Using configured Polymarket API credentials")
            else:
                api_creds = temp_client.create_or_derive_api_key()
                logger.info("Successfully derived Polymarket API credentials")

            # Step 3: Create authenticated client
            self.client = ClobClient(
                host=self.clob_url,
                chain_id=self.chain_id,
                key=self.private_key,
                creds=api_creds,
                signature_type=self.signature_type,
                funder=self.funder_address or None,
            )

            self._initialized = True
            logger.info("CLOB client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            logger.warning("Running in dry-run mode")
            self.dry_run = True

    def execute_trade(
        self,
        market: Market,
        assessment: ProbabilityAssessment,
        available_capital: float,
        spread: float = 0.0,
    ) -> Optional[Trade]:
        """
        Execute a trade based on the probability assessment.

        Decision logic:
        - If AI probability > market price → BUY YES tokens
        - If AI probability < market price → BUY NO tokens
        - Position size = max_position_pct * available_capital

        Returns a Trade record, or None if trade wasn't executed.
        """
        # Validate edge meets minimum threshold
        if assessment.abs_edge < self.min_edge:
            logger.debug(
                f"Edge too small for {market.question}: "
                f"{assessment.abs_edge:.1%} < {self.min_edge:.1%}"
            )
            return None

        # Determine trade direction and the fair probability for THAT side.
        if assessment.edge > 0:
            # AI thinks YES is more likely than market does → BUY YES
            token_id = market.yes_token_id
            outcome = Outcome.YES
            side = TradeSide.BUY
            price = market.yes_price
            fair_prob = assessment.estimated_probability
        else:
            # AI thinks NO is more likely → BUY NO
            token_id = market.no_token_id
            outcome = Outcome.NO
            side = TradeSide.BUY
            price = market.no_price
            # Fair probability of NO is the complement of the YES estimate.
            fair_prob = 1.0 - assessment.estimated_probability

        if price <= 0:
            logger.warning(f"Invalid token price ${price:.3f}, skipping")
            return None

        # Calculate position size — confidence/spread-aware fractional Kelly,
        # clamped to the max-position ceiling and available capital.
        max_spend = compute_position_size(
            SizingInputs(
                available_capital=available_capital,
                entry_price=price,
                fair_probability=fair_prob,
                confidence=assessment.confidence,
                spread=spread,
            ),
            SizingConfig(
                max_position_pct=self.max_position_pct,
                kelly_fraction=self.kelly_fraction,
                min_trade_usd=1.0,
                use_kelly=self.use_kelly,
            ),
        )

        if max_spend <= 0:
            logger.info(
                f"Sizing returned $0 for '{market.question}' "
                f"(edge {assessment.abs_edge:.1%}, conf {assessment.confidence:.0%}, "
                f"spread {spread:.1%}) — skipping"
            )
            return None

        # Size = how many shares we can buy at current price
        # Each share pays $1 if correct, costs $price
        size = shares(dec(max_spend) / dec(price)) if price > 0 else 0
        total_cost = usdc(dec(size) * dec(price))

        if total_cost > available_capital:
            logger.warning(
                f"Trade cost ${total_cost:.2f} exceeds available capital "
                f"${available_capital:.2f}, skipping"
            )
            return None

        if total_cost < 1.0:
            logger.warning(f"Position too small (${total_cost:.2f}), skipping")
            return None

        logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}"
            f"Executing: {side.value} {outcome.value} on '{market.question}' | "
            f"Size: {size:.1f} shares @ ${price:.3f} = ${total_cost:.2f} | "
            f"Edge: {assessment.abs_edge:.1%}"
        )

        # Execute or dry-run
        trade = Trade(
            market_condition_id=market.condition_id,
            token_id=token_id,
            side=side,
            outcome=outcome,
            price=price,
            size=size,
            total_cost=total_cost,
            edge_at_trade=assessment.abs_edge,
            ai_probability=assessment.estimated_probability,
            market_price_at_trade=market.yes_price,
        )

        if self.dry_run:
            trade.order_id = "DRY_RUN"
            trade.success = True
            logger.info(f"[DRY RUN] Trade logged but not executed")
            return trade

        # Place the actual order via CLOB API
        try:
            if not self._initialized or not self.client:
                logger.error("CLOB client not initialized")
                trade.success = False
                trade.error_message = "Client not initialized"
                return trade

            # Use market order (Fill-or-Kill) for immediate execution
            response = self._post_market_order(
                token_id=token_id,
                amount=total_cost,
                side=TradeSide.BUY,
                reference_price=price,
            )

            fill = self._reconcile_fill(
                response=response,
                token_id=token_id,
                side=TradeSide.BUY,
                fallback_price=price,
                fallback_size=size,
                fallback_total=total_cost,
            )
            if fill:
                trade.price = fill["price"]
                trade.size = fill["size"]
                trade.total_cost = fill["total"]

            trade.order_id = self._extract_order_id(response)
            trade.success = True
            logger.info(f"Order placed successfully: {trade.order_id}")

        except Exception as e:
            trade.success = False
            trade.error_message = str(e)
            logger.error(f"Order execution failed: {e}")

        return trade

    def close_position(self, position: Position) -> Trade:
        """
        Close an open position by selling its outcome token.

        The portfolio should only mark the position closed after this method
        returns a successful trade.

        The pre-fill price estimate applies the same slippage band used for live
        orders as a conservative haircut. Midpoint/last price overstates what a
        market SELL actually clears in a thin book, which would make dry-run and
        paper-trading P&L look better than reality. When trading live, the real
        fill from _reconcile_fill() overrides this estimate.
        """
        reference = quantized_price(position.current_price or position.entry_price)
        # Haircut the estimate to the worst-acceptable SELL price.
        price = self._price_limit(reference, TradeSide.SELL)
        proceeds = usdc(dec(position.size) * dec(price))
        trade = Trade(
            market_condition_id=position.market_condition_id,
            token_id=position.token_id,
            side=TradeSide.SELL,
            outcome=position.outcome,
            price=price,
            size=position.size,
            total_cost=proceeds,
            realized_pnl=usdc(dec(proceeds) - dec(position.cost_basis)),
            market_price_at_trade=reference,
        )

        logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}"
            f"Closing: SELL {position.outcome.value} | "
            f"Size: {position.size:.1f} shares @ ${price:.3f} = ${proceeds:.2f}"
        )

        if self.dry_run:
            trade.order_id = "DRY_RUN_CLOSE"
            trade.success = True
            return trade

        try:
            if not self._initialized or not self.client:
                trade.success = False
                trade.error_message = "Client not initialized"
                return trade

            response = self._post_market_order(
                token_id=position.token_id,
                amount=position.size,  # SELL market orders use shares, not USDC.
                side=TradeSide.SELL,
                reference_price=price,
            )
            fill = self._reconcile_fill(
                response=response,
                token_id=position.token_id,
                side=TradeSide.SELL,
                fallback_price=price,
                fallback_size=position.size,
                fallback_total=proceeds,
            )
            if fill:
                trade.price = fill["price"]
                trade.size = fill["size"]
                trade.total_cost = fill["total"]
                trade.realized_pnl = usdc(dec(trade.total_cost) - dec(position.cost_basis))
            trade.order_id = self._extract_order_id(response)
            trade.success = True
        except Exception as e:
            trade.success = False
            trade.error_message = str(e)
            logger.error(f"Position close failed: {e}")

        return trade

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        if not self._initialized or not self.client:
            return []
        try:
            return self.client.get_open_orders() or []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if not self._initialized or not self.client:
            return False
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders (emergency stop).

        py-clob-client-v2 exposes cancel_all() which cancels every open order
        for the account in one call.
        """
        if not self._initialized or not self.client:
            return False
        try:
            self.client.cancel_all()
            logger.info("Cancelled all open orders")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    def _post_market_order(
        self,
        token_id: str,
        amount: float,
        side: TradeSide,
        reference_price: float,
    ) -> dict:
        """Create and post a CLOB V2 market order with slippage protection."""
        if not self.client:
            raise RuntimeError("CLOB client not initialized")

        order_type = self._clob_order_type()
        price_limit = self._price_limit(reference_price, side)

        # When the real CLOB types are unavailable (e.g. in tests against a
        # fake client), fall back to plain values so the call still exercises
        # the client method. In production all four symbols are present.
        clob_side = (
            (ClobSide.BUY if side == TradeSide.BUY else ClobSide.SELL)
            if ClobSide is not None
            else side.value
        )
        if MarketOrderArgs is not None:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=clob_side,
                price=price_limit,
                order_type=order_type,
            )
        else:
            order_args = {
                "token_id": token_id,
                "amount": amount,
                "side": clob_side,
                "price": price_limit,
                "order_type": order_type,
            }
        options = (
            PartialCreateOrderOptions(tick_size=self.tick_size)
            if PartialCreateOrderOptions is not None
            else {"tick_size": self.tick_size}
        )

        return self.client.create_and_post_market_order(
            order_args=order_args,
            options=options,
            order_type=order_type,
        )

    def _clob_order_type(self):
        if OrderType is None:
            return "FOK"
        return getattr(OrderType, self.order_type.upper(), OrderType.FOK)

    @staticmethod
    def _normalize_tick_size(value) -> str:
        """
        Coerce the configured tick size to one the CLOB API accepts.

        py-clob-client-v2 only allows '0.1', '0.01', '0.001', '0.0001'. Anything
        else is rejected at order time, so we validate up front and fall back to
        the safe default of '0.01' with a warning rather than failing mid-trade.
        """
        allowed = {"0.1", "0.01", "0.001", "0.0001"}
        text = str(value).strip()
        if text in allowed:
            return text
        logger.warning(f"Invalid tick_size '{value}', falling back to '0.01'")
        return "0.01"

    def _price_limit(self, reference_price: float, side: TradeSide) -> float:
        """
        Compute a slippage-protected worst-acceptable price for a market order.

        BUY: pay at most reference * (1 + slippage), capped one tick below 1.0.
        SELL: accept at least reference * (1 - slippage), floored one tick above 0.0.

        The cap/floor are kept one tick inside [0, 1] (rather than a hard 0.99 /
        0.01) so markets trading very near the bounds still leave room to fill.
        The computed limit is logged so unexpected non-fills are explainable.
        """
        slippage = dec(self.slippage_bps) / dec(10_000)
        tick = dec(self.tick_size) if self.tick_size else dec("0.01")
        ref = dec(reference_price)

        if side == TradeSide.BUY:
            ceiling = dec(1) - tick
            limit = min(ceiling, ref * (dec(1) + slippage))
        else:
            floor = tick
            limit = max(floor, ref * (dec(1) - slippage))

        limit = quantized_price(limit)
        logger.debug(
            f"Price limit for {side.value}: ref={reference_price:.4f} "
            f"slippage={self.slippage_bps}bps tick={self.tick_size} -> {limit:.4f}"
        )
        return limit

    def _reconcile_fill(
        self,
        response: dict,
        token_id: str,
        side: TradeSide,
        fallback_price: float,
        fallback_size: float,
        fallback_total: float,
    ) -> Optional[dict[str, float]]:
        """
        Reconcile fill details from the post response, order detail, or recent trades.

        CLOB response shapes can vary between order status and trade data. We parse
        all available candidates and fall back to the conservative local estimate
        when no fill detail is present.
        """
        candidates = [response]
        order_id = self._extract_order_id(response)

        if self.reconcile_fills and self.client and order_id:
            try:
                order = self.client.get_order(order_id)
                if order:
                    candidates.append(order)
            except Exception as e:
                logger.warning(f"Failed to fetch order details for {order_id}: {e}")

            try:
                trade = self._find_recent_order_trade(order_id, token_id)
                if trade:
                    candidates.append(trade)
            except Exception as e:
                logger.warning(f"Failed to fetch trade details for {order_id}: {e}")

        for candidate in candidates:
            fill = self._extract_fill_details(
                candidate,
                side=side,
                fallback_price=fallback_price,
                fallback_size=fallback_size,
                fallback_total=fallback_total,
            )
            if fill:
                return fill

        return {
            "price": quantized_price(fallback_price),
            "size": shares(fallback_size),
            "total": usdc(fallback_total),
        }

    def _find_recent_order_trade(self, order_id: str, token_id: str) -> Optional[dict]:
        if not self.client or TradeParams is None or not hasattr(self.client, "get_trades"):
            return None

        trades = self.client.get_trades(
            TradeParams(asset_id=token_id),
            only_first_page=True,
        ) or []

        for trade in trades:
            identifiers = {
                str(trade.get(key, ""))
                for key in (
                    "order_id",
                    "orderId",
                    "orderID",
                    "maker_order_id",
                    "makerOrderId",
                    "taker_order_id",
                    "takerOrderId",
                )
                if isinstance(trade, dict)
            }
            if order_id in identifiers:
                return trade
        return None

    @staticmethod
    def _extract_order_id(response: dict) -> str:
        if not isinstance(response, dict):
            return ""
        return (
            response.get("orderID")
            or response.get("orderId")
            or response.get("id")
            or response.get("order_id")
            or ""
        )

    @staticmethod
    def _extract_fill_details(
        response: dict,
        side: TradeSide,
        fallback_price: float,
        fallback_size: float,
        fallback_total: float,
    ) -> Optional[dict[str, float]]:
        if not isinstance(response, dict):
            return None

        price_value = Trader._first_numeric(
            response,
            "avgPrice",
            "average_price",
            "averagePrice",
            "filled_price",
            "filledPrice",
            "price",
        )
        size_value = Trader._first_numeric(
            response,
            "size_matched",
            "sizeMatched",
            "matched_size",
            "matchedSize",
            "filled_size",
            "filledSize",
            "size",
            "shares",
        )
        total_value = Trader._first_numeric(
            response,
            "total",
            "total_cost",
            "totalCost",
            "filled_amount",
            "filledAmount",
            "value",
            "cost",
            "proceeds",
        )

        if price_value is None and size_value is None and total_value is None:
            return None

        price_value = dec(price_value if price_value is not None else fallback_price)

        if size_value is None and total_value is not None and price_value > 0:
            size_value = dec(total_value) / price_value
        elif size_value is None:
            size_value = dec(fallback_size)
        else:
            size_value = dec(size_value)

        if total_value is None:
            total_value = size_value * price_value
        else:
            total_value = dec(total_value)

        # BUY market order responses may report the requested USDC amount
        # separately from matched shares; keep the actual spend when known.
        if side == TradeSide.BUY and total_value == 0:
            total_value = dec(fallback_total)

        return {
            "price": quantized_price(price_value),
            "size": shares(size_value),
            "total": usdc(total_value),
        }

    @staticmethod
    def _first_numeric(response: dict, *keys: str) -> Optional[float]:
        for key in keys:
            value = response.get(key)
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None
