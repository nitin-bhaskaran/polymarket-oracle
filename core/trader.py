"""
Trader — Executes trades on Polymarket via the CLOB API.

Handles order creation, submission, and status tracking.
Uses the official py-clob-client SDK for authentication and signing.
"""

import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from core.models import (
    Market, Outcome, Position, ProbabilityAssessment, Side, Trade
)

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
        
        # Risk parameters
        self.max_position_pct = risk_config.get("max_position_pct", 10.0) / 100
        self.min_edge = risk_config.get("min_edge", 5.0) / 100
        
        # Dry run mode — log trades but don't execute
        self.dry_run = False
        
        # Initialize CLOB client
        self.client: Optional[ClobClient] = None
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
        
        try:
            # Step 1: Create initial client to derive API keys
            temp_client = ClobClient(
                self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
            )
            
            # Step 2: Derive API credentials
            api_creds = temp_client.create_or_derive_api_creds()
            logger.info("Successfully derived Polymarket API credentials")
            
            # Step 3: Create authenticated client
            self.client = ClobClient(
                self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
                creds=api_creds,
                signature_type=self.signature_type,
                funder=self.funder_address,
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
        
        # Determine trade direction
        if assessment.edge > 0:
            # AI thinks YES is more likely than market does → BUY YES
            token_id = market.yes_token_id
            outcome = Outcome.YES
            side = Side.BUY
            price = market.yes_price
        else:
            # AI thinks NO is more likely → BUY NO
            token_id = market.no_token_id
            outcome = Outcome.NO
            side = Side.BUY
            price = market.no_price
        
        # Calculate position size
        max_spend = available_capital * self.max_position_pct
        # Size = how many shares we can buy at current price
        # Each share pays $1 if correct, costs $price
        size = max_spend / price if price > 0 else 0
        total_cost = size * price
        
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
            market_order = MarketOrderArgs(
                token_id=token_id,
                amount=total_cost,  # Amount in USDC to spend
            )
            
            signed_order = self.client.create_market_order(market_order)
            response = self.client.post_order(signed_order, OrderType.FOK)
            
            trade.order_id = response.get("orderID", "")
            trade.success = True
            logger.info(f"Order placed successfully: {trade.order_id}")
            
        except Exception as e:
            trade.success = False
            trade.error_message = str(e)
            logger.error(f"Order execution failed: {e}")
        
        return trade
    
    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        if not self._initialized or not self.client:
            return []
        try:
            return self.client.get_orders() or []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if not self._initialized or not self.client:
            return False
        try:
            self.client.cancel(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    def cancel_all_orders(self) -> bool:
        """Cancel all open orders (emergency stop)."""
        if not self._initialized or not self.client:
            return False
        try:
            self.client.cancel_all()
            logger.info("Cancelled all open orders")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False
