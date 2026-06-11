"""
Data models for the Betfair Exchange pivot.

These are deliberately separate from the original Polymarket models (core.models)
so the venue swap doesn't break anything already validated. The key conceptual
differences from Polymarket, encoded here:

  * Prices are DECIMAL ODDS, not 0..1 probabilities. Odds of 2.50 mean a £1
    backer's stake returns £2.50 (including stake) if it wins. Implied
    probability is 1/odds, BEFORE removing the book's overround.

  * The book has an OVERROUND: summing 1/odds across all runners gives >1.0.
    That excess is the market's margin. Edge must be computed against the
    overround-ADJUSTED implied probability, or every market shows phantom edge.

  * Two order directions: BACK (bet FOR a runner, risk = stake) and LAY (bet
    AGAINST a runner, risk = liability = stake*(odds-1)). Sizing differs.

  * COMMISSION is charged on net winnings (market-dependent), a real drag the
    edge must clear.

  * Orders can be PASSIVE limit orders (rest at a price) rather than crossing
    the spread. The paper fill simulator treats these differently.

Everything here is venue-specific; the sizing/exit/assessment logic consumes it
through small adapters rather than depending on these shapes directly.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BetSide(str, Enum):
    """Direction of a bet on the exchange."""
    BACK = "BACK"   # bet FOR the runner; risk = stake
    LAY = "LAY"     # bet AGAINST the runner; risk = stake * (odds - 1)


class OrderStyle(str, Enum):
    """How the order is placed into the book."""
    CROSS = "CROSS"     # take available price now (market-like)
    PASSIVE = "PASSIVE" # rest a limit order at our price, wait for a fill


class MarketPhase(str, Enum):
    """Where the market is in its lifecycle."""
    PRE_EVENT = "pre_event"
    IN_PLAY = "in_play"
    CLOSED = "closed"
    SETTLED = "settled"


class RunnerStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WINNER = "WINNER"
    LOSER = "LOSER"
    REMOVED = "REMOVED"


class PriceLevel(BaseModel):
    """A single rung of the order book: price (decimal odds) and available size."""
    price: float
    size: float


class Runner(BaseModel):
    """
    A single selection within a Betfair market (e.g. one football team, one
    candidate). Carries the best available back/lay prices and book depth.
    """
    selection_id: int
    name: str = ""
    status: RunnerStatus = RunnerStatus.ACTIVE

    # Best available prices (decimal odds). back[0]/lay[0] are the touch.
    available_to_back: list[PriceLevel] = Field(default_factory=list)
    available_to_lay: list[PriceLevel] = Field(default_factory=list)
    last_price_traded: Optional[float] = None
    total_matched: float = 0.0

    @property
    def best_back(self) -> Optional[float]:
        return self.available_to_back[0].price if self.available_to_back else None

    @property
    def best_lay(self) -> Optional[float]:
        return self.available_to_lay[0].price if self.available_to_lay else None

    @property
    def mid_odds(self) -> Optional[float]:
        """Midpoint of best back and lay, in odds space."""
        b, l = self.best_back, self.best_lay
        if b is None or l is None:
            return None
        return (b + l) / 2.0

    @property
    def raw_implied_prob(self) -> Optional[float]:
        """1/mid_odds — implied probability BEFORE overround adjustment."""
        mid = self.mid_odds
        if mid is None or mid <= 1.0:
            # use whichever side we have if no mid
            single = self.best_back or self.best_lay
            if not single or single <= 1.0:
                return None
            return 1.0 / single
        return 1.0 / mid


class BetfairMarket(BaseModel):
    """
    A Betfair Exchange market — a set of mutually exclusive runners under an
    event (e.g. 'Match Odds' with Home/Draw/Away).
    """
    market_id: str               # e.g. "1.234567890"
    event_id: str = ""
    event_name: str = ""
    market_name: str = ""        # e.g. "Match Odds"
    competition: str = ""
    domain: str = ""             # event type group (Soccer, Politics, ...)
    sport: str = ""              # legacy field: Betfair market type code

    start_time: Optional[datetime] = None
    phase: MarketPhase = MarketPhase.PRE_EVENT
    in_play: bool = False

    total_matched: float = 0.0   # liquidity proxy
    commission_rate: float = 0.05  # fraction of net winnings (market default)

    runners: list[Runner] = Field(default_factory=list)

    @property
    def hours_to_start(self) -> float:
        if not self.start_time:
            return 999999.0
        st = self.start_time
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        return max(0.0, (st - datetime.now(timezone.utc)).total_seconds() / 3600)

    @property
    def overround(self) -> float:
        """
        Sum of raw implied probabilities across active runners. >1.0 means the
        book has a margin; the excess over 1.0 is the overround.
        """
        total = 0.0
        for r in self.runners:
            if r.status != RunnerStatus.ACTIVE:
                continue
            p = r.raw_implied_prob
            if p:
                total += p
        return total

    def fair_implied_prob(self, runner: Runner) -> Optional[float]:
        """
        Overround-adjusted implied probability for a runner: normalise its raw
        implied probability by the book total so the runners sum to 1.0. This
        is the market's *true* probability estimate, stripped of margin, and the
        correct thing to compare an AI estimate against.
        """
        raw = runner.raw_implied_prob
        book = self.overround
        if raw is None or book <= 0:
            return None
        return raw / book


# ── Probability assessment (odds-aware) ─────────────────────────────

class BetfairAssessment(BaseModel):
    """
    AI probability assessment for one runner in a Betfair market, with edge
    computed against the overround-adjusted implied probability.
    """
    market_id: str
    selection_id: int
    runner_name: str
    question: str  # human-readable "Will <runner> win <market>?"

    estimated_probability: float   # AI P(this runner wins)
    confidence: float
    reasoning: str = ""
    key_factors: list[str] = Field(default_factory=list)

    # Market side
    market_fair_prob: float = 0.0  # overround-adjusted implied prob
    best_back: Optional[float] = None
    best_lay: Optional[float] = None
    commission_rate: float = 0.05

    # Edge (signed): AI prob minus market fair prob
    edge: float = 0.0
    abs_edge: float = 0.0
    recommended_side: Optional[BetSide] = None

    assessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def calculate_edge(self):
        """
        Edge = AI probability - market fair (overround-adjusted) probability.

        Positive edge → the runner is underpriced → BACK it.
        Negative edge → the runner is overpriced → LAY it.
        """
        self.edge = self.estimated_probability - self.market_fair_prob
        self.abs_edge = abs(self.edge)
        self.recommended_side = BetSide.BACK if self.edge > 0 else BetSide.LAY


# ── Paper bet record (the validation instrument) ────────────────────

class PaperBetStatus(str, Enum):
    PENDING = "pending"       # order placed, awaiting (simulated) fill
    FILLED = "filled"         # simulated fill occurred
    UNFILLED = "unfilled"     # passive order never traded through; expired
    SETTLED = "settled"       # market resolved, P&L known
    CANCELLED = "cancelled"


class PaperBet(BaseModel):
    """
    A simulated bet — the core record of the validation instrument.

    Every paper bet is tagged with the feature dimensions needed to slice
    results later (market phase, sport, edge band, confidence band, order
    style, side) so weeks of data can tell us what to retain, kill, or revise.
    Fills are simulated honestly: a PASSIVE order is only marked FILLED if the
    market later traded through its requested price.
    """
    # Identity
    bet_id: str
    market_id: str
    selection_id: int
    runner_name: str = ""

    # Order
    side: BetSide
    style: OrderStyle
    requested_odds: float          # the price we asked for
    stake: float                   # backer's stake / lay backer's stake
    liability: float = 0.0         # for lay: stake*(odds-1); for back: == stake
    commission_rate: float = 0.05

    # Fill (simulated)
    status: PaperBetStatus = PaperBetStatus.PENDING
    filled_odds: Optional[float] = None
    filled_at: Optional[datetime] = None

    # Settlement
    won: Optional[bool] = None
    gross_pnl: Optional[float] = None   # before commission
    net_pnl: Optional[float] = None     # after commission
    settled_at: Optional[datetime] = None

    # Context at placement
    ai_probability: float = 0.0
    market_fair_prob: float = 0.0
    edge_at_placement: float = 0.0
    confidence: float = 0.0

    # ── Attribution tags (for slicing results) ──
    phase: MarketPhase = MarketPhase.PRE_EVENT
    domain: str = ""
    sport: str = ""              # legacy field: Betfair market type code
    event_name: str = ""
    market_name: str = ""
    competition: str = ""
    sleeve: str = "legacy"
    edge_band: str = ""        # e.g. "5-8%", "8-12%", ">12%"
    confidence_band: str = ""  # e.g. "low", "med", "high"
    strategy: str = "value"    # "value" | "fade_overreaction" | ...

    placed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def band_edge(abs_edge: float) -> str:
        if abs_edge < 0.05:
            return "<5%"
        if abs_edge < 0.08:
            return "5-8%"
        if abs_edge < 0.12:
            return "8-12%"
        return ">12%"

    @staticmethod
    def band_confidence(conf: float) -> str:
        if conf < 0.5:
            return "low"
        if conf < 0.75:
            return "med"
        return "high"
