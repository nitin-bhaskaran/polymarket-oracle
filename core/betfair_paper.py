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
    def __init__(self, config, scanner, assessor, store=None, alerts=None,
                 two_stage=None, governor=None):
        self.config = config
        self.scanner = scanner
        self.assessor = assessor
        self.two_stage = two_stage      # optional TwoStageAssessor
        self.governor = governor        # optional AssessmentGovernor
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
        # Sanity bounds that suppress phantom edges on near-certain markets:
        # an implausibly large triage edge is treated as triage confusion, and
        # bets are only placed when the side's odds sit in a tradeable band
        # (not extreme longshots/near-locks).
        self.max_triage_edge = bf.get("max_triage_edge", 0.40)
        self.min_odds = bf.get("min_odds", 1.20)
        self.max_odds = bf.get("max_odds", 21.0)
        # Favourite-floor: reject a market assessment if the AI gives the
        # market's favourite less than this fraction of its market-implied
        # probability (catches broken multi-runner distributions like a
        # tournament favourite assessed at 1%). 0 disables.
        self.favourite_floor_fraction = bf.get("favourite_floor_fraction", 0.5)

        # Minimum seconds between deep (web-search) assessments, to stay under
        # the org input-tokens-per-minute rate limit. Deep calls are
        # token-heavy; spacing them avoids hard 429s that drop assessments.
        self.deep_min_interval_s = config.get("paper", {}).get("deep_min_interval_seconds", 20.0)
        self._last_deep_at = 0.0

        paper = config.get("paper", {})
        self.health_path = paper.get("health_path", "data/health.json")
        self.passive_timeout_s = paper.get("passive_timeout_seconds", 1800)
        self.max_bets_per_cycle = paper.get("max_bets_per_cycle", 5)
        self.max_open_bets = paper.get("max_open_bets", 0)
        self.max_total_exposure_pct = paper.get(
            "max_total_exposure_pct", 100.0
        ) / 100
        self.max_open_bets_per_market = paper.get(
            "max_open_bets_per_market", 0
        )
        self.default_sleeve = paper.get(
            "default_sleeve", {"name": "general"}
        )
        self.sleeves = paper.get("sleeves", [])
        scale_in = paper.get("scale_in", {})
        self.scale_in_enabled = scale_in.get("enabled", True)
        self.max_entries_per_selection = scale_in.get(
            "max_entries_per_selection", 3
        )
        self.scale_in_min_hours = scale_in.get(
            "min_hours_between_entries", 6.0
        )
        self.scale_in_min_odds_improvement = scale_in.get(
            "min_odds_improvement_pct", 0.03
        )
        self.scale_in_min_edge_improvement = scale_in.get(
            "min_edge_improvement", 0.02
        )
        self.scale_in_size_multiplier = scale_in.get(
            "size_multiplier", 0.5
        )

    # ── capital accounting (paper) ──

    def _deployed(self) -> float:
        """Sum liabilities reserved by filled and pending bets."""
        return self.store.open_liability()

    def _available_capital(self) -> float:
        realised = sum((b.net_pnl or 0.0) for b in self.store.all()
                       if b.status == PaperBetStatus.SETTLED)
        return self.starting_capital + realised - self._deployed()

    # ── strategy sleeves + exposure controls ──

    @staticmethod
    def _pct_limit(policy: dict, key: str, capital: float) -> float:
        value = policy.get(key)
        if value is None:
            return float("inf")
        return capital * float(value) / 100

    def _market_policy(self, market) -> tuple[dict, str]:
        """
        Select a strategy sleeve for a market.

        A matching sleeve may restrict its own market types without narrowing
        the rest of the scanner. The reason is non-empty when the sleeve blocks
        this market.
        """
        haystack = " | ".join([
            market.event_name or "",
            market.competition or "",
            market.market_name or "",
        ]).lower()
        market_type = (market.sport or "").upper()

        for raw in self.sleeves:
            policy = dict(raw)
            keywords = [
                str(value).lower()
                for value in policy.get("match_any", [])
                if value
            ]
            if keywords and not any(keyword in haystack for keyword in keywords):
                continue

            allowed = {
                str(value).upper()
                for value in policy.get("allowed_market_types", [])
            }
            if allowed and market_type not in allowed:
                return policy, (
                    f"market type {market_type or 'unknown'} is outside sleeve "
                    f"{policy.get('name', 'unnamed')} "
                    f"({', '.join(sorted(allowed))})"
                )
            if not policy.get("enabled", True):
                return policy, (
                    f"sleeve {policy.get('name', 'unnamed')} is disabled"
                )
            return policy, ""

        return dict(self.default_sleeve), ""

    def _exposure_capacity(self, market, policy: dict) -> tuple[float, str]:
        """Return remaining liability capacity and a rejection reason."""
        open_bets = self.store.open_bets()
        sleeve = policy.get("name", "general")

        if self.max_open_bets and len(open_bets) >= self.max_open_bets:
            return 0.0, f"max open bets reached ({self.max_open_bets})"

        market_max = policy.get(
            "max_open_bets_per_market", self.max_open_bets_per_market
        )
        market_open = self.store.open_count(market_id=market.market_id)
        if market_max and market_open >= int(market_max):
            return 0.0, (
                f"market already has {market_open} open bet(s); "
                f"limit {market_max}"
            )

        event_max = policy.get("max_open_bets_per_event", 0)
        event_open = self.store.open_count(event_name=market.event_name)
        if market.event_name and event_max and event_open >= int(event_max):
            return 0.0, (
                f"event already has {event_open} open bet(s); "
                f"limit {event_max}"
            )

        total_remaining = (
            self.starting_capital * self.max_total_exposure_pct
            - self.store.open_liability()
        )
        sleeve_remaining = (
            self._pct_limit(
                policy, "max_exposure_pct", self.starting_capital
            )
            - self.store.open_liability(sleeve=sleeve)
        )
        market_remaining = (
            self._pct_limit(
                policy, "max_market_exposure_pct", self.starting_capital
            )
            - self.store.open_liability(market_id=market.market_id)
        )
        event_remaining = (
            self._pct_limit(
                policy, "max_event_exposure_pct", self.starting_capital
            )
            - self.store.open_liability(event_name=market.event_name)
        )
        capacity = min(
            self._available_capital(),
            total_remaining,
            sleeve_remaining,
            market_remaining,
            event_remaining,
        )
        if capacity <= 0:
            return 0.0, (
                f"exposure capacity exhausted for sleeve={sleeve} "
                f"(total deployed £{self.store.open_liability():.2f})"
            )
        return capacity, ""

    def _cap_sizing(self, sizing: dict, odds: float, side: BetSide,
                    capacity: float) -> dict:
        """Clamp a Kelly decision to remaining exposure capacity."""
        liability = min(float(sizing["liability"]), max(0.0, capacity))
        if side == BetSide.BACK:
            stake = liability
        else:
            stake = liability / (odds - 1.0)
        if stake < self.min_stake or liability <= 0:
            return {"stake": 0.0, "liability": 0.0}
        return {"stake": stake, "liability": liability}

    def _entry_decision(self, assessment) -> tuple[bool, int, str]:
        """
        Decide whether an assessment is a new position or a valid scale-in.

        Repeated same-selection entries require a fresh material improvement:
        better executable odds for our side or at least the configured increase
        in assessed edge. This prevents periodic re-assessments from blindly
        stacking identical exposure.
        """
        existing = self.store.open_selection_bets(
            assessment.market_id, assessment.selection_id
        )
        if not existing:
            return True, 1, "initial"
        if not self.scale_in_enabled:
            return False, len(existing) + 1, "scale-ins disabled"
        if len(existing) >= self.max_entries_per_selection:
            return False, len(existing) + 1, (
                f"selection already has {len(existing)} entries; "
                f"limit {self.max_entries_per_selection}"
            )

        latest = existing[-1]
        if latest.side != assessment.recommended_side:
            return False, len(existing) + 1, (
                f"signal flipped from {latest.side.value} to "
                f"{assessment.recommended_side.value}; not stacking opposing bets"
            )

        placed_at = latest.placed_at
        if placed_at.tzinfo is None:
            placed_at = placed_at.replace(tzinfo=timezone.utc)
        age_hours = (
            datetime.now(timezone.utc) - placed_at
        ).total_seconds() / 3600
        if age_hours < self.scale_in_min_hours:
            return False, len(existing) + 1, (
                f"last entry is {age_hours:.1f}h old; "
                f"minimum {self.scale_in_min_hours:.1f}h"
            )

        odds = (
            assessment.best_back
            if assessment.recommended_side == BetSide.BACK
            else assessment.best_lay
        )
        prior_odds = latest.filled_odds or latest.requested_odds
        odds_improvement = 0.0
        if odds and prior_odds:
            if assessment.recommended_side == BetSide.BACK:
                odds_improvement = (odds - prior_odds) / prior_odds
            else:
                odds_improvement = (prior_odds - odds) / prior_odds

        edge_improvement = (
            assessment.abs_edge - abs(latest.edge_at_placement)
        )
        if (
            odds_improvement < self.scale_in_min_odds_improvement
            and edge_improvement < self.scale_in_min_edge_improvement
        ):
            return False, len(existing) + 1, (
                f"no material improvement: odds {odds_improvement:+.1%}, "
                f"edge {edge_improvement:+.1%}"
            )

        reasons = []
        if odds_improvement >= self.scale_in_min_odds_improvement:
            reasons.append(f"odds improved {odds_improvement:+.1%}")
        if edge_improvement >= self.scale_in_min_edge_improvement:
            reasons.append(f"edge improved {edge_improvement:+.1%}")
        return True, len(existing) + 1, "; ".join(reasons)

    # ── assessment routing (single-stage or two-stage + governor) ──

    def _assess(self, market):
        """
        Return assessments for a market.

        If a two-stage assessor + governor are configured, use the cost-
        controlled path: skip markets the governor says don't need
        reassessment; run cheap triage; only promote to the expensive
        web-search deep assessment when the rough edge clears triage_edge AND
        the daily deep-assessment budget allows. Otherwise fall back to the
        single-stage assessor.
        """
        if not (self.two_stage and self.governor):
            return self.assessor.assess_market(market)

        # Skip if cached and unchanged.
        if not self.governor.needs_assessment(market):
            return []

        # Stage 1: cheap triage (no web search).
        best_edge, triage_assessments = self.two_stage.triage(market)
        self.governor.record_assessment(market)  # mark seen regardless

        if best_edge < self.two_stage.triage_edge:
            return []  # not worth a deep look

        # Upper guard: an absurdly large triage edge means the cheap, no-info
        # triage strongly disagrees with a market the exchange prices
        # confidently — that's a sign the triage is confused (near-certain
        # market, e.g. a 0.3%-priced "replaced by tomorrow" market), not a real
        # opportunity. Don't burn deep budget chasing it.
        if best_edge > self.max_triage_edge:
            logger.info(
                f"Triage edge {best_edge:.1%} on {market.market_id} exceeds sanity "
                f"cap ({self.max_triage_edge:.0%}); treating as triage confusion, "
                f"skipping deep assess. ({market.event_name} / {market.market_name})"
            )
            return []

        # Stage 2: deep web-search assessment, budget permitting.
        if not self.governor.can_deep_assess():
            logger.info(
                f"Daily deep-assessment budget exhausted "
                f"({self.governor.daily_deep_budget}); skipping deep assess of "
                f"{market.market_id}. Triage flagged edge {best_edge:.1%}."
            )
            return []  # do NOT act on triage-only edges; they're uninformed

        logger.info(f"Triage flagged {market.market_id} (edge {best_edge:.1%}); deep-assessing")
        # Space deep assessments apart: each pulls web-search results into
        # context (token-heavy), and several within a minute trip the org's
        # input-tokens-per-minute rate limit (hard 429s that drop assessments).
        # A deliberate gap keeps us under the limit with clean, gap-free data.
        self._respect_deep_spacing()
        deep = self.two_stage.deep_assess(market)
        self._last_deep_at = time.monotonic()
        self.governor.record_deep_assessment()

        # Favourite-floor coherence check. On a multi-runner market, the clearest
        # distribution-construction failure is assigning the market's favourite an
        # implausibly low probability (e.g. world #1 tennis favourite at 1%). When
        # that happens the WHOLE distribution is misallocated — the probability
        # stripped from the favourite inflates the other runners, so the
        # mid-runner "edges" are just the flip side of the same error. So we
        # reject the entire market assessment, not just the favourite's leg.
        if deep and not self._favourite_plausible(market, deep):
            return []

        # Drop assessments on extreme-priced runners: backing/laying a near-lock
        # (very long or very short odds) is where phantom edges and poor fills
        # concentrate, and Kelly on extreme odds is unreliable.
        deep = [a for a in deep if self._odds_in_band(a)]
        return deep

    def _respect_deep_spacing(self):
        """Sleep so deep assessments are at least deep_min_interval_s apart."""
        if self.deep_min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_deep_at
        wait = self.deep_min_interval_s - elapsed
        if wait > 0:
            logger.info(f"Spacing deep assessments: sleeping {wait:.0f}s to respect rate limit")
            time.sleep(wait)

    def _favourite_plausible(self, market, assessments) -> bool:
        """
        False if the AI assigned the market's favourite (shortest odds) a
        probability far below its market-implied probability — a signature of a
        broken multi-runner distribution. The floor is a fraction of the
        favourite's overround-adjusted market probability.
        """
        # Identify favourite by highest market-fair prob among assessed runners.
        fav = max(assessments, key=lambda a: a.market_fair_prob, default=None)
        if fav is None or fav.market_fair_prob <= 0:
            return True
        floor = self.favourite_floor_fraction * fav.market_fair_prob
        if fav.estimated_probability < floor:
            logger.info(
                f"Rejecting {market.market_id} ({market.event_name} / "
                f"{market.market_name}): favourite {fav.runner_name} assessed at "
                f"{fav.estimated_probability:.0%} vs market {fav.market_fair_prob:.0%} "
                f"(floor {floor:.0%}) — implausible distribution, skipping whole market."
            )
            return False
        return True

    def _odds_in_band(self, assessment) -> bool:
        """True if the side's odds are within the tradeable band (not a near-lock)."""
        side_odds = (assessment.best_back if assessment.recommended_side == BetSide.BACK
                     else assessment.best_lay)
        if not side_odds:
            return False
        return self.min_odds <= side_odds <= self.max_odds

    # ── one cycle ──

    def run_cycle(self):
        logger.info("Paper cycle: scanning...")
        markets = self.scanner.scan()
        logger.info(f"Scanning returned {len(markets)} markets")

        placed = 0
        funnel = {
            "scanned": len(markets),
            "policy_blocked": 0,
            "exposure_blocked": 0,
            "cached": 0,
            "eligible": 0,
            "no_assessment_output": 0,
            "assessments": 0,
            "below_edge": 0,
            "scale_in_blocked": 0,
        }
        for market in markets:
            if placed >= self.max_bets_per_cycle:
                break

            policy, policy_rejection = self._market_policy(market)
            if policy_rejection:
                funnel["policy_blocked"] += 1
                logger.info(
                    f"Skipping assessment of {market.event_name} / "
                    f"{market.market_name}: {policy_rejection}"
                )
                continue
            _, capacity_rejection = self._exposure_capacity(market, policy)
            if capacity_rejection:
                funnel["exposure_blocked"] += 1
                logger.info(
                    f"Skipping assessment of {market.event_name} / "
                    f"{market.market_name}: {capacity_rejection}"
                )
                continue

            funnel["eligible"] += 1
            if (
                self.two_stage
                and self.governor
                and not self.governor.needs_assessment(market)
            ):
                funnel["cached"] += 1
                continue
            assessments = self._assess(market)
            if not assessments:
                funnel["no_assessment_output"] += 1
            funnel["assessments"] += len(assessments)
            for a in assessments:
                if placed >= self.max_bets_per_cycle:
                    break
                if a.abs_edge < self.min_edge:
                    funnel["below_edge"] += 1
                    continue
                allowed, entry_index, entry_reason = self._entry_decision(a)
                if not allowed:
                    funnel["scale_in_blocked"] += 1
                    logger.info(
                        f"Skipping entry on {a.runner_name} in "
                        f"{market.event_name} / {market.market_name}: "
                        f"{entry_reason}"
                    )
                    continue
                bet = self._place_paper_bet(
                    market, a,
                    entry_index=entry_index,
                    entry_reason=entry_reason,
                )
                if bet:
                    placed += 1

        # Re-evaluate resting passive orders and expire stale ones.
        self._update_passive_orders()
        # Settle resolved markets.
        self._settle_resolved()

        self._heartbeat()
        counts = self.store.count_by_status()
        logger.info(
            "Cycle funnel: "
            f"scanned={funnel['scanned']} "
            f"eligible={funnel['eligible']} "
            f"policy_blocked={funnel['policy_blocked']} "
            f"exposure_blocked={funnel['exposure_blocked']} "
            f"cached={funnel['cached']} "
            f"no_assessment_output={funnel['no_assessment_output']} "
            f"assessments={funnel['assessments']} "
            f"below_edge={funnel['below_edge']} "
            f"scale_in_blocked={funnel['scale_in_blocked']} "
            f"placed={placed}"
        )
        logger.info(f"Paper cycle done. Placed {placed}. Status counts: {counts}")
        return placed

    def _place_paper_bet(self, market, assessment, entry_index: int = 1,
                         entry_reason: str = "initial"):
        policy, policy_rejection = self._market_policy(market)
        sleeve = policy.get("name", "general")
        if policy_rejection:
            logger.info(
                f"Skipping {market.event_name} / {market.market_name}: "
                f"{policy_rejection}"
            )
            return None

        capacity, capacity_rejection = self._exposure_capacity(market, policy)
        if capacity_rejection:
            logger.info(
                f"Skipping {market.event_name} / {market.market_name}: "
                f"{capacity_rejection}"
            )
            return None

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
        if entry_index > 1:
            multiplier = self.scale_in_size_multiplier ** (entry_index - 1)
            sizing = {
                "stake": sizing["stake"] * multiplier,
                "liability": sizing["liability"] * multiplier,
            }
        sizing = self._cap_sizing(sizing, odds, side, capacity)
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
            domain=market.domain,
            sport=market.sport,
            event_name=market.event_name,
            market_name=market.market_name,
            competition=market.competition,
            sleeve=sleeve,
            entry_index=entry_index,
            entry_reason=entry_reason,
            edge_band=PaperBet.band_edge(assessment.abs_edge),
            confidence_band=PaperBet.band_confidence(assessment.confidence),
            strategy="llm_value",
        )

        # CROSS fills immediately against current depth; PASSIVE rests.
        if style == OrderStyle.CROSS:
            bet = simulate_cross_fill(bet, market)

        self.store.add(bet)
        logger.info(
            f"PLACED paper {side.value} {assessment.runner_name} @ {bet.requested_odds} "
            f"(filled={bet.filled_odds if bet.status == PaperBetStatus.FILLED else 'no'}, "
            f"status={bet.status.value}, stake £{bet.stake:.2f}, liability £{bet.liability:.2f}, "
            f"sleeve={bet.sleeve}, entry={bet.entry_index} ({bet.entry_reason}), "
            f"edge {assessment.edge:+.1%}, AI {assessment.estimated_probability:.0%} vs "
            f"fair {assessment.market_fair_prob:.0%}) — {market.event_name} / {market.market_name}"
        )
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
            if not market or market.phase not in (
                MarketPhase.CLOSED, MarketPhase.SETTLED
            ):
                continue
            # Determine winners from runner status.
            winners = {r.selection_id for r in market.runners
                       if r.status == RunnerStatus.WINNER}
            if not winners:
                logger.info(
                    f"Market {market_id} is closed without an explicit winner; "
                    "leaving paper bets unresolved (void/suspended result)."
                )
                continue
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
                "max_total_exposure": (
                    self.starting_capital * self.max_total_exposure_pct
                ),
                "bet_counts": counts,
            }
            p = Path(self.health_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(health, indent=2, default=str))
        except Exception as e:
            logger.warning(f"heartbeat failed: {e}")
