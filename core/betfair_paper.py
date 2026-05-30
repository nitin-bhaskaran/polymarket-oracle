"""
Betfair paper-trading orchestrator — the validation loop.

Per cycle:
  1. Scan markets (all sports + politics; pre-event + optionally in-play).
  2. Assess each market's runners with Claude -> edges vs overround-adjusted prob.
  3. For actionable edges, SIZE a back/lay bet and place it as a PaperBet:
       - pre-event: CROSS style (take available price), simulate fill vs depth.
       - in-play:   PASSIVE style (rest at our price), fill only if market
                    later trades through it.
  4. Re-evaluate resting PASSIVE bets against fresh books; expire stale ones.
  5. Poll markets with open filled bets for resolution; settle them.
  6. Persist everything (tagged) and write a heartbeat.

No real orders are ever placed. This is a measurement instrument: every bet is
tagged by phase/sport/edge-band/confidence-band/side/style so the analysis
script can later say which features to keep, kill, or revise.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from core.betfair_models import (
    BetSide, MarketPhase, OrderStyle, PaperBet, PaperBetStatus, RunnerStatus,
)
from core.betfair_fills import (
    simulate_cross_fill, update_passive, expire_if_stale, settle,
)
from core.betfair_sizing import OddsSizingInputs, OddsSizingConfig, compute_bet_size
from core.paper_store import PaperBetStore

logger = logging.getLogger("betfair.paper")


class BetfairPaperTrader:
    def __init__(self, config, scanner, assessor, store=None, alerts=None):
        self.config = config
        self.scanner = scanner
        self.assessor = assessor
        self.store = store or PaperBetStore(
            config.get("paper", {}).get("store_path", "data/paper_bets.jsonl")
        )
        self.alerts = alerts

        risk = config.get("risk", {})
        self.starting_capital = risk.get("starting_capital", 130.0)
        self.max_position_pct = risk.get("max_position_pct", 10.0) / 100
        self.kelly_fraction = risk.get("kelly_fraction", 0.25)
        self.use_kelly = risk.get("use_kelly_sizing", True)
        self.min_stake = risk.get("min_stake", 1.0)

        bf = config.get("betfair_assessor", {})
        self.min_edge = bf.get("min_edge", 0.05)

        paper = config.get("paper", {})
        self.passive_timeout_s = paper.get("passive_timeout_seconds", 1800)
        self.max_bets_per_cycle = paper.get("max_bets_per_cycle", 5)

    # ── capital accounting (paper) ──

    def _deployed(self) -> float:
        """Sum liabilities of filled, unsettled bets."""
        return sum(b.liability for b in self.store.filled_unsettled())

    def _available_capital(self) -> float:
        realised = sum((b.net_pnl or 0.0) for b in self.store.all()
                       if b.status == PaperBetStatus.SETTLED)
        return self.starting_capital + realised - self._deployed()

    # ── one cycle ──

    def run_cycle(self):
        logger.info("Paper cycle: scanning...")
        markets = self.scanner.scan()
        logger.info(f"Scanning returned {len(markets)} markets")

        placed = 0
        for market in markets:
            if placed >= self.max_bets_per_cycle:
                break
            assessments = self.assessor.assess_market(market)
            for a in assessments:
                if placed >= self.max_bets_per_cycle:
                    break
                if a.abs_edge < self.min_edge:
                    continue
                if self.store.has_open_position(a.market_id, a.selection_id):
                    continue
                bet = self._place_paper_bet(market, a)
                if bet:
                    placed += 1

        # Re-evaluate resting passive orders and expire stale ones.
        self._update_passive_orders()
        # Settle resolved markets.
        self._settle_resolved()

        self._heartbeat()
        counts = self.store.count_by_status()
        logger.info(f"Paper cycle done. Placed {placed}. Status counts: {counts}")
        return placed

    def _place_paper_bet(self, market, assessment):
        side = assessment.recommended_side
        # Choose the price/odds for the side.
        if side == BetSide.BACK:
            odds = assessment.best_back
        else:
            odds = assessment.best_lay
        if not odds or odds <= 1.0:
            return None

        sizing = compute_bet_size(
            OddsSizingInputs(
                available_capital=self._available_capital(),
                odds=odds,
                fair_probability=assessment.estimated_probability,
                confidence=assessment.confidence,
                side=side,
                commission_rate=assessment.commission_rate,
            ),
            OddsSizingConfig(
                max_position_pct=self.max_position_pct,
                kelly_fraction=self.kelly_fraction,
                min_stake=self.min_stake,
                use_kelly=self.use_kelly,
            ),
        )
        if sizing["stake"] <= 0:
            return None

        style = OrderStyle.PASSIVE if market.in_play else OrderStyle.CROSS
        bet = PaperBet(
            bet_id=str(uuid.uuid4()),
            market_id=market.market_id,
            selection_id=assessment.selection_id,
            runner_name=assessment.runner_name,
            side=side,
            style=style,
            requested_odds=odds,
            stake=sizing["stake"],
            liability=sizing["liability"],
            commission_rate=assessment.commission_rate,
            ai_probability=assessment.estimated_probability,
            market_fair_prob=assessment.market_fair_prob,
            edge_at_placement=assessment.edge,
            confidence=assessment.confidence,
            phase=market.phase,
            sport=market.sport,
            edge_band=PaperBet.band_edge(assessment.abs_edge),
            confidence_band=PaperBet.band_confidence(assessment.confidence),
            strategy="value",
        )

        # CROSS fills immediately against current depth; PASSIVE rests.
        if style == OrderStyle.CROSS:
            bet = simulate_cross_fill(bet, market)

        self.store.add(bet)
        if bet.status == PaperBetStatus.FILLED and self.alerts:
            self.alerts.send_message_sync(
                f"📝 Paper {side.value} {assessment.runner_name} @ {bet.filled_odds} "
                f"(edge {assessment.edge:+.1%}, {market.market_name})"
            )
        return bet

    def _update_passive_orders(self):
        for bet in self.store.pending():
            market = self.scanner.refresh_book(bet.market_id) or self.scanner.get_market(bet.market_id)
            if market:
                runner = next((r for r in market.runners
                               if r.selection_id == bet.selection_id), None)
                if runner:
                    before = bet.status
                    bet = update_passive(bet, runner)
                    if bet.status != before:
                        self.store.update(bet)
                        continue
            bet = expire_if_stale(bet, self.passive_timeout_s)
            self.store.update(bet)

    def _settle_resolved(self):
        for market_id in self.store.open_market_ids():
            market = self.scanner.refresh_book(market_id)
            if not market or market.phase != MarketPhase.SETTLED:
                continue
            # Determine winners from runner status.
            winners = {r.selection_id for r in market.runners
                       if r.status == RunnerStatus.WINNER}
            for bet in self.store.filled_unsettled():
                if bet.market_id != market_id:
                    continue
                runner_won = bet.selection_id in winners
                # For a BACK we win if our runner won; for a LAY we win if it lost.
                our_bet_won = runner_won if bet.side == BetSide.BACK else not runner_won
                bet = settle(bet, won=our_bet_won)
                self.store.update(bet)
                if self.alerts:
                    self.alerts.send_message_sync(
                        f"✅ Settled paper {bet.side.value} {bet.runner_name}: "
                        f"net {bet.net_pnl:+.2f}"
                    )

    def _heartbeat(self):
        import json
        from pathlib import Path
        try:
            counts = self.store.count_by_status()
            health = {
                "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                "available_capital": self._available_capital(),
                "deployed": self._deployed(),
                "bet_counts": counts,
            }
            p = Path("data/health.json")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(health, indent=2, default=str))
        except Exception as e:
            logger.warning(f"heartbeat failed: {e}")
