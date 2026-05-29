"""
Position sizing — confidence- and spread-aware fractional Kelly.

The original bot sized every trade at a flat ``max_position_pct`` of capital
regardless of how strong the signal was. That treats a 6% edge at 30%
confidence identically to a 6% edge at 95% confidence, and ignores the spread
you have to cross to enter. This module computes a size that scales with the
quality of the opportunity, then clamps it to the configured risk ceiling.

The maths, kept deliberately simple and conservative:

  A binary outcome token bought at price ``p`` pays 1.0 if correct, 0 if not.
  If the true probability of the outcome we are buying is ``q`` (the AI's
  estimate for that side), the Kelly-optimal fraction of bankroll for a bet
  that pays net odds ``b = (1 - p) / p`` per unit staked is:

      f* = (b * q - (1 - q)) / b
         = q - (1 - q) / b
         = (q - p) / (1 - p)

  That ``f*`` is the *full* Kelly fraction. Full Kelly is famously too
  aggressive and assumes your probability estimate is exact, so we:

    1. multiply by a configurable Kelly fraction (default 0.25 — "quarter
       Kelly", a common conservative choice),
    2. multiply by the model's stated confidence (so low-confidence edges are
       sized down), and
    3. cap the result at ``max_position_pct`` so a single position can never
       exceed the existing hard risk ceiling.

  We also subtract the half-spread from the edge before sizing, because you
  pay roughly half the bid-ask spread to enter at the mid. If the edge does
  not survive the spread, the size is zero and the trade is skipped.
"""

from dataclasses import dataclass
from decimal import Decimal

from core.money import dec, usdc


@dataclass
class SizingInputs:
    """Everything needed to size one position. All prices are 0..1."""
    available_capital: float
    entry_price: float          # price of the token we are buying (YES or NO)
    fair_probability: float     # AI's probability for the side we are buying
    confidence: float           # 0..1 model confidence
    spread: float = 0.0         # bid-ask spread of the token (0..1), optional


@dataclass
class SizingConfig:
    max_position_pct: float     # hard ceiling, fraction (e.g. 0.10)
    kelly_fraction: float = 0.25
    min_trade_usd: float = 1.0
    use_kelly: bool = True


def compute_position_size(inp: SizingInputs, cfg: SizingConfig) -> float:
    """
    Return the USDC amount to spend on this position (0.0 means "skip").

    The returned value is already clamped to both the max-position ceiling and
    available capital, and rounded to USDC precision.
    """
    p = dec(inp.entry_price)
    q = dec(inp.fair_probability)
    capital = dec(inp.available_capital)
    ceiling = dec(cfg.max_position_pct)

    if p <= 0 or p >= 1 or capital <= 0:
        return 0.0

    # Edge for the side we are buying, net of the half-spread cost to enter.
    half_spread = dec(inp.spread) / dec(2)
    net_edge = (q - p) - half_spread
    if net_edge <= 0:
        return 0.0

    if cfg.use_kelly:
        # f* = (q - p) / (1 - p), using the spread-adjusted edge in the
        # numerator so the cost of entry reduces the bet.
        denom = dec(1) - p
        if denom <= 0:
            return 0.0
        full_kelly = net_edge / denom
        fraction = full_kelly * dec(cfg.kelly_fraction) * dec(inp.confidence)
    else:
        # Non-Kelly fallback: scale the ceiling linearly by confidence.
        fraction = ceiling * dec(inp.confidence)

    # Clamp to the hard ceiling and to 1.0 of capital.
    fraction = max(Decimal(0), min(fraction, ceiling, Decimal(1)))

    spend = usdc(fraction * capital)
    if spend < cfg.min_trade_usd:
        return 0.0
    # Never exceed available capital.
    return usdc(min(dec(spend), capital))
