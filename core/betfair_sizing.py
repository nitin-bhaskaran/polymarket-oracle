"""
Position sizing for Betfair decimal-odds bets — fractional Kelly, back & lay,
commission-aware.

Kelly on decimal odds for a BACK bet at odds O with true win prob p:
    b = O - 1                      (net odds received per unit staked)
    f* = (b*p - (1-p)) / b = (O*p - 1) / (O - 1)
Stake fraction of bankroll = f* * kelly_fraction * confidence, clamped.

For a LAY bet we are effectively backing the complement. Laying a runner at
odds O is profitable when its true win prob p is BELOW the implied 1/O. The
Kelly-optimal lay is the back-Kelly applied to "the field" (prob 1-p) at the
lay's effective odds. We compute the equivalent and express the result as a
BACKER'S STAKE we accept (Betfair lay sizing is in backer's-stake terms); the
liability is stake*(O-1), which is what actually gets risked and what we clamp
against capital.

Commission reduces winnings, so it shrinks the effective edge; we fold a simple
commission haircut into the win payoff before computing the fraction.
"""

from dataclasses import dataclass
from decimal import Decimal

from core.betfair_models import BetSide
from core.money import dec, usdc


@dataclass
class OddsSizingInputs:
    available_capital: float
    odds: float                # decimal odds we'd get filled at
    fair_probability: float    # AI P(runner wins) for a BACK; same for LAY (we invert inside)
    confidence: float
    side: BetSide
    commission_rate: float = 0.05


@dataclass
class OddsSizingConfig:
    max_position_pct: float        # ceiling as fraction of capital (risk(=liability) based)
    kelly_fraction: float = 0.25
    min_stake: float = 1.0         # Betfair min is typically £1-£2
    use_kelly: bool = True


def _back_kelly_fraction(p: float, odds: float, commission: float) -> float:
    """Full-Kelly fraction for a back bet, with commission haircut on winnings."""
    b = (odds - 1.0) * (1.0 - commission)  # net winnings per unit, after commission
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


def compute_bet_size(inp: OddsSizingInputs, cfg: OddsSizingConfig) -> dict:
    """
    Return a sizing decision dict:
        {"stake": float, "liability": float}  (both 0.0 => skip)

    'stake' is the backer's stake (for LAY this is the stake we accept from the
    backer). 'liability' is what we actually risk and is clamped to
    max_position_pct of capital and to available capital.
    """
    capital = dec(inp.available_capital)
    ceiling = dec(cfg.max_position_pct)
    if inp.odds <= 1.0 or capital <= 0:
        return {"stake": 0.0, "liability": 0.0}

    p = inp.fair_probability

    if inp.side == BetSide.BACK:
        if cfg.use_kelly:
            frac = _back_kelly_fraction(p, inp.odds, inp.commission_rate)
            frac = frac * cfg.kelly_fraction * inp.confidence
        else:
            frac = cfg.max_position_pct * inp.confidence
        frac = max(0.0, min(frac, cfg.max_position_pct, 1.0))
        # For a back, liability == stake.
        liability = usdc(dec(frac) * capital)
        stake = liability
        if stake < cfg.min_stake:
            return {"stake": 0.0, "liability": 0.0}
        return {"stake": float(min(dec(stake), capital)),
                "liability": float(min(dec(liability), capital))}

    # LAY: we profit if the runner LOSES. Our "win prob" is (1 - p).
    # Effective odds for the layer's stake-at-risk: laying at O risks (O-1) to
    # win 1 unit of backer stake. Treat as a back on the field at odds
    # O/(O-1) with win prob (1-p).
    lay_win_prob = 1.0 - p
    eff_odds = inp.odds / (inp.odds - 1.0)  # >1
    if cfg.use_kelly:
        frac = _back_kelly_fraction(lay_win_prob, eff_odds, inp.commission_rate)
        frac = frac * cfg.kelly_fraction * inp.confidence
    else:
        frac = cfg.max_position_pct * inp.confidence
    frac = max(0.0, min(frac, cfg.max_position_pct, 1.0))

    # 'frac' is the fraction of capital to put AT RISK (the liability).
    liability = usdc(dec(frac) * capital)
    if liability < cfg.min_stake:
        return {"stake": 0.0, "liability": 0.0}
    liability = float(min(dec(liability), capital))
    # Convert liability back to backer's stake: liability = stake*(O-1).
    stake = usdc(dec(liability) / dec(inp.odds - 1.0))
    return {"stake": float(stake), "liability": liability}
