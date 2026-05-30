"""
Paper-bet fill simulator — the honesty layer of the validation instrument.

The single biggest way to fool yourself when paper-trading is to assume every
order fills at the price you wanted. That flatters exactly the strategies that
would fail live (in-play passive orders that never actually trade through).
This module models fills conservatively so the weeks of paper data mean
something.

Fill rules:

  CROSS (take available price now):
    A back fills against available_to_back depth; a lay against
    available_to_lay depth. Fills up to the size available at acceptable
    prices. If the touch price is worse than requested, partial/no fill.
    This is the realistic "I cross the spread now" case, used pre-event.

  PASSIVE (rest a limit order, wait):
    The order does NOT fill at placement. It only fills if the market
    subsequently TRADES THROUGH the requested price. We check that against
    later observed prices fed in via update_passive(). A back at odds O fills
    only if the market's best-back later rises to >= O (someone offered at
    least our price) OR last-traded passed through it; a lay at O fills only
    if best-lay later falls to <= O. Until then it stays PENDING; after a
    configurable timeout it becomes UNFILLED (and contributes nothing to P&L).

This conservative treatment means: if a passive in-play strategy looks
profitable in paper, it's because orders genuinely would have been hit — not
because we assumed free fills.
"""

from datetime import datetime, timezone
from typing import Optional

from core.betfair_models import (
    BetfairMarket, BetSide, OrderStyle, PaperBet, PaperBetStatus, Runner,
)


def _find_runner(market: BetfairMarket, selection_id: int) -> Optional[Runner]:
    for r in market.runners:
        if r.selection_id == selection_id:
            return r
    return None


def simulate_cross_fill(bet: PaperBet, market: BetfairMarket) -> PaperBet:
    """
    Simulate an immediate (CROSS) fill against current book depth.

    For a BACK we consume available_to_back; for a LAY, available_to_lay. We
    fill only at prices no worse than requested. Sets status FILLED (with a
    volume-weighted filled_odds) or UNFILLED if nothing acceptable is available.
    """
    runner = _find_runner(market, bet.selection_id)
    if runner is None:
        bet.status = PaperBetStatus.UNFILLED
        return bet

    if bet.side == BetSide.BACK:
        levels = runner.available_to_back        # we take offered back prices
        acceptable = lambda price: price >= bet.requested_odds  # higher is better for backer
    else:
        levels = runner.available_to_lay
        acceptable = lambda price: price <= bet.requested_odds  # lower is better for layer

    remaining = bet.stake
    cost_weighted = 0.0
    filled = 0.0
    for level in levels:
        if remaining <= 0:
            break
        if not acceptable(level.price):
            break  # book is sorted; once unacceptable, stop
        take = min(remaining, level.size)
        cost_weighted += take * level.price
        filled += take
        remaining -= take

    if filled <= 0:
        bet.status = PaperBetStatus.UNFILLED
        return bet

    bet.filled_odds = cost_weighted / filled
    bet.stake = filled  # may be a partial fill
    if bet.side == BetSide.LAY:
        bet.liability = bet.stake * (bet.filled_odds - 1.0)
    else:
        bet.liability = bet.stake
    bet.status = PaperBetStatus.FILLED
    bet.filled_at = datetime.now(timezone.utc)
    return bet


def update_passive(bet: PaperBet, runner: Runner) -> PaperBet:
    """
    Re-evaluate a resting PASSIVE order against a fresh observation of the book.

    Fills only if the market has traded through the requested price:
      BACK at O fills if best-back >= O (someone is now offering at least O),
        or last_price_traded passed >= O.
      LAY at O fills if best-lay <= O, or last_price_traded passed <= O.
    Leaves the bet PENDING otherwise.
    """
    if bet.status != PaperBetStatus.PENDING:
        return bet

    ltp = runner.last_price_traded
    if bet.side == BetSide.BACK:
        hit = (runner.best_back is not None and runner.best_back >= bet.requested_odds) \
              or (ltp is not None and ltp >= bet.requested_odds)
    else:
        hit = (runner.best_lay is not None and runner.best_lay <= bet.requested_odds) \
              or (ltp is not None and ltp <= bet.requested_odds)

    if hit:
        bet.filled_odds = bet.requested_odds  # passive fills at our limit
        if bet.side == BetSide.LAY:
            bet.liability = bet.stake * (bet.filled_odds - 1.0)
        else:
            bet.liability = bet.stake
        bet.status = PaperBetStatus.FILLED
        bet.filled_at = datetime.now(timezone.utc)
    return bet


def expire_if_stale(bet: PaperBet, max_age_seconds: float) -> PaperBet:
    """Mark a still-PENDING passive order UNFILLED once it's older than the timeout."""
    if bet.status != PaperBetStatus.PENDING:
        return bet
    age = (datetime.now(timezone.utc) - bet.placed_at).total_seconds()
    if age >= max_age_seconds:
        bet.status = PaperBetStatus.UNFILLED
    return bet


def settle(bet: PaperBet, won: bool) -> PaperBet:
    """
    Settle a FILLED bet given the outcome, computing gross and net (post-
    commission) P&L. Commission is charged on net winnings only (Betfair model).

    BACK win:  profit = stake * (filled_odds - 1); loss = -stake
    LAY win (runner loses): profit = stake (the backer's stake we accepted)
    LAY loss (runner wins):  loss = -liability = -stake*(filled_odds - 1)
    """
    if bet.status != PaperBetStatus.FILLED or bet.filled_odds is None:
        return bet

    odds = bet.filled_odds
    if bet.side == BetSide.BACK:
        gross = bet.stake * (odds - 1.0) if won else -bet.stake
    else:  # LAY
        if won:  # we wanted the runner to LOSE; 'won' here = OUR bet won
            gross = bet.stake
        else:
            gross = -(bet.stake * (odds - 1.0))

    # Commission only on positive net winnings.
    commission = bet.commission_rate * gross if gross > 0 else 0.0
    bet.gross_pnl = gross
    bet.net_pnl = gross - commission
    bet.won = won
    bet.status = PaperBetStatus.SETTLED
    bet.settled_at = datetime.now(timezone.utc)
    return bet
