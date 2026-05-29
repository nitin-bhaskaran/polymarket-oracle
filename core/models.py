"""
Data models for Polymarket Oracle.
Defines the core data structures used throughout the system.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Side(str, Enum):
    """Trade side — buying YES or NO outcome tokens."""
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    """Binary outcome."""
    YES = "Yes"
    NO = "No"


class MarketStatus(str, Enum):
    """Market lifecycle status."""
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"


class Market(BaseModel):
    """
    A single Polymarket prediction market.
    
    Maps to a Gamma API market object. Each market is a binary YES/NO
    question within a broader event (e.g., "Will X happen by Y date?").
    """
    # Identifiers
    condition_id: str
    question: str
    slug: str
    market_id: Optional[str] = None
    
    # Token IDs for YES and NO outcomes (needed for CLOB trading)
    yes_token_id: str
    no_token_id: str
    
    # Current prices (0.0 to 1.0, representing probability)
    yes_price: float = 0.0
    no_price: float = 0.0
    
    # Market metadata
    description: str = ""
    category: str = ""
    end_date: Optional[datetime] = None
    
    # Liquidity & volume
    liquidity: float = 0.0
    volume_24h: float = 0.0
    volume_total: float = 0.0
    
    # Status
    status: MarketStatus = MarketStatus.ACTIVE
    
    # Event context (parent event)
    event_title: str = ""
    event_slug: str = ""
    
    @property
    def hours_to_expiry(self) -> float:
        """Hours until market closes. Returns 999999 if no end date."""
        if not self.end_date:
            return 999999.0
        end_date = self.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        delta = end_date - datetime.now(timezone.utc)
        return max(0, delta.total_seconds() / 3600)
    
    @property
    def midpoint(self) -> float:
        """Midpoint price (average of YES price)."""
        return self.yes_price


class ProbabilityAssessment(BaseModel):
    """
    AI-generated probability assessment for a market.
    
    Contains the Claude API's assessment of the true probability,
    along with reasoning and confidence level.
    """
    market_condition_id: str
    question: str
    
    # AI's estimated probability (0.0 to 1.0)
    estimated_probability: float
    
    # How confident the AI is in its estimate (0.0 to 1.0)
    confidence: float
    
    # Brief reasoning (1-2 sentences)
    reasoning: str
    
    # Key news/facts that informed the assessment
    key_factors: list[str] = Field(default_factory=list)
    
    # Edge calculation
    market_price: float  # Current YES price on Polymarket
    edge: float = 0.0    # estimated_probability - market_price (signed)
    abs_edge: float = 0.0  # Absolute edge
    
    # Recommended action
    recommended_side: Optional[Side] = None  # BUY YES or BUY NO
    
    # Timestamp
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    def calculate_edge(self):
        """Calculate the edge between AI estimate and market price."""
        self.edge = self.estimated_probability - self.market_price
        self.abs_edge = abs(self.edge)
        
        # The trade side is BUY for both outcomes; the outcome token decides YES vs NO.
        self.recommended_side = Side.BUY


class Position(BaseModel):
    """
    An open position in a market.
    
    Tracks entry price, current value, and P&L.
    """
    # Market reference
    market_condition_id: str
    question: str
    token_id: str  # The specific token (YES or NO) we hold
    
    # Position details
    side: Side  # Whether we bought YES or NO
    outcome: Outcome  # Which outcome token we hold
    entry_price: float  # Price we paid per share
    size: float  # Number of shares
    cost_basis: float  # Total cost (entry_price * size)
    
    # Current state
    current_price: float = 0.0
    current_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    
    # Timestamps
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    
    # Assessment that triggered this trade
    edge_at_entry: float = 0.0
    ai_probability_at_entry: float = 0.0
    
    # Status
    is_open: bool = True
    
    def update_pnl(self, current_price: float):
        """Update P&L based on current market price."""
        self.current_price = current_price
        self.current_value = self.size * current_price
        self.unrealized_pnl = self.current_value - self.cost_basis
        if self.cost_basis > 0:
            self.unrealized_pnl_pct = (self.unrealized_pnl / self.cost_basis) * 100


class Trade(BaseModel):
    """Record of an executed trade."""
    # Identifiers
    trade_id: str = ""
    order_id: str = ""
    market_condition_id: str
    token_id: str
    
    # Trade details
    side: Side
    outcome: Outcome
    price: float
    size: float
    total_cost: float
    fees: float = 0.0
    
    # Context
    edge_at_trade: float = 0.0
    ai_probability: float = 0.0
    market_price_at_trade: float = 0.0
    
    # Result (filled when position closes)
    realized_pnl: Optional[float] = None
    
    # Timestamps
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Status
    success: bool = True
    error_message: str = ""


class PortfolioSnapshot(BaseModel):
    """Point-in-time snapshot of the entire portfolio."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Capital
    total_capital: float  # Starting capital
    available_capital: float  # Cash not in positions
    deployed_capital: float  # Cash in open positions
    
    # P&L
    total_unrealized_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    
    # Positions
    open_positions: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    
    @property
    def win_rate(self) -> float:
        """Win rate as percentage."""
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100
    
    @property
    def total_value(self) -> float:
        """Total portfolio value."""
        return self.available_capital + self.deployed_capital + self.total_unrealized_pnl
