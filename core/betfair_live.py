"""
Gated real-money Betfair execution.

This reuses the paper trader's scanning, assessment, policy, sizing, dedupe,
and settlement logic, but replaces simulated placement with one synchronous
FILL_OR_KILL Exchange order. Live mode is intentionally difficult to arm:

  1. run with --live
  2. set live.enabled: true
  3. declare betfair.app_key_mode: live
  4. set BETFAIR_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK

The free delayed application key cannot be used for live betting.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.betfair_models import (
    BetSide, MarketPhase, OrderStyle, PaperBet, PaperBetStatus,
)
from core.betfair_paper import BetfairPaperTrader
from core.betfair_sizing import (
    OddsSizingConfig, OddsSizingInputs, compute_bet_size,
)

logger = logging.getLogger("betfair.live")

LIVE_ACK = "I_ACCEPT_REAL_MONEY_RISK"


class LiveConfigurationError(ValueError):
    pass


def validate_live_config(config: dict, environ=None) -> dict:
    """Validate all independent arming gates and return the live block."""
    environ = os.environ if environ is None else environ
    live = config.get("live", {})
    betfair = config.get("betfair", {})

    errors = []
    if not live.get("enabled", False):
        errors.append("live.enabled must be true")
    if str(betfair.get("app_key_mode", "delayed")).lower() != "live":
        errors.append(
            "betfair.app_key_mode must be 'live' (the delayed key cannot place bets)"
        )
    ack_name = live.get("acknowledgement_env", "BETFAIR_LIVE_ACK")
    if environ.get(ack_name) != LIVE_ACK:
        errors.append(f"{ack_name} must equal {LIVE_ACK}")

    bankroll = float(live.get("bankroll_gbp", 0))
    single = float(live.get("max_liability_per_bet_gbp", 0))
    total = float(live.get("max_total_liability_gbp", 0))
    if bankroll <= 0:
        errors.append("live.bankroll_gbp must be positive")
    if single <= 0 or single > bankroll:
        errors.append(
            "live.max_liability_per_bet_gbp must be positive and <= bankroll_gbp"
        )
    if total <= 0 or total > bankroll:
        errors.append(
            "live.max_total_liability_gbp must be positive and <= bankroll_gbp"
        )
    if single > total:
        errors.append(
            "live.max_liability_per_bet_gbp must be <= max_total_liability_gbp"
        )
    if int(live.get("daily_order_limit", 0)) <= 0:
        errors.append("live.daily_order_limit must be positive")
    if int(live.get("max_bets_per_cycle", 0)) <= 0:
        errors.append("live.max_bets_per_cycle must be positive")

    if errors:
        raise LiveConfigurationError("; ".join(errors))
    return live


def verify_live_app_key(client) -> dict:
    """Confirm the configured key is active and receives non-delayed data."""
    applications = client.get_developer_app_keys()
    for application in applications:
        for version in application.get("appVersions", []):
            key = version.get("applicationKey") or version.get("appKey")
            if key != client.app_key:
                continue
            if not version.get("active", False):
                raise LiveConfigurationError(
                    "configured Betfair application key is not active"
                )
            if version.get("delayData", True):
                raise LiveConfigurationError(
                    "configured Betfair application key is the delayed version"
                )
            return version
    raise LiveConfigurationError(
        "configured Betfair application key was not found in getDeveloperAppKeys"
    )


class BetfairLiveTrader(BetfairPaperTrader):
    """The paper signal pipeline with fail-closed Exchange execution."""

    def __init__(
        self, config, scanner, assessor, client, store=None, alerts=None,
        two_stage=None, governor=None,
    ):
        super().__init__(
            config, scanner, assessor, store=store, alerts=alerts,
            two_stage=two_stage, governor=governor,
        )
        self.client = client
        live = config.get("live", {})
        self.execution_label = "LIVE"
        self.health_path = live.get("health_path", "data/live_health.json")

        self.starting_capital = float(live.get("bankroll_gbp", 10.0))
        self.use_kelly = bool(live.get("use_kelly_sizing", False))
        self.kelly_fraction = float(
            live.get("kelly_fraction", self.kelly_fraction)
        )
        self.max_position_pct = float(
            live.get("max_liability_per_bet_gbp", 2.0)
        ) / self.starting_capital
        self.min_stake = float(live.get("min_stake_gbp", 1.0))
        self.max_bets_per_cycle = int(live.get("max_bets_per_cycle", 1))
        self.max_open_bets = int(live.get("max_open_bets", 2))
        self.max_open_bets_per_market = int(
            live.get("max_open_bets_per_market", 1)
        )
        self.max_total_liability = float(
            live.get("max_total_liability_gbp", 3.0)
        )
        self.max_liability_per_bet = float(
            live.get("max_liability_per_bet_gbp", 2.0)
        )
        self.daily_order_limit = int(live.get("daily_order_limit", 2))
        self.max_loss = float(live.get("max_loss_gbp", 5.0))
        self.live_min_edge = float(live.get("min_edge", 0.08))
        self.live_max_edge = float(live.get("max_edge", 0.12))
        self.min_confidence = float(live.get("min_confidence", 0.50))
        self.max_confidence = float(live.get("max_confidence", 0.75))
        self.allowed_sides = {
            str(side).upper() for side in live.get("allowed_sides", ["LAY"])
        }
        self.strategy_ref = str(live.get("strategy_ref", "oracle-live"))[:15]
        self.audit_path = Path(
            live.get("audit_path", "data/live_order_attempts.jsonl")
        )
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._funds_cache: tuple[float, float] | None = None
        self._funds_cache_at = 0.0

    def _journal_attempt(self, payload: dict):
        """Append an immutable record of every real-money API attempt."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        try:
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.critical("Failed to write LIVE order audit: %s", exc)

    def _market_policy(self, market) -> tuple[dict, str]:
        if market.in_play or market.phase == MarketPhase.IN_PLAY:
            return {"name": "live"}, "live execution rejects in-play markets"
        return super()._market_policy(market)

    def _live_funds(self, force: bool = False) -> tuple[float, float]:
        if (
            not force
            and self._funds_cache is not None
            and time.monotonic() - self._funds_cache_at < 30.0
        ):
            return self._funds_cache
        funds = self.client.get_account_funds()
        available = float(funds.get("availableToBetBalance", 0.0) or 0.0)
        exposure = abs(float(funds.get("exposure", 0.0) or 0.0))
        self._funds_cache = (available, exposure)
        self._funds_cache_at = time.monotonic()
        return self._funds_cache

    def _orders_today(self) -> int:
        today = datetime.now(timezone.utc).date()
        return sum(
            1 for bet in self.store.all()
            if bet.execution_mode == "live"
            and bet.placed_at.astimezone(timezone.utc).date() == today
        )

    def _realised_live_pnl(self) -> float:
        return sum(
            float(bet.net_pnl or 0.0)
            for bet in self.store.all()
            if bet.execution_mode == "live"
            and bet.status == PaperBetStatus.SETTLED
        )

    def _exposure_capacity(
        self, market, policy: dict, force_funds: bool = False,
    ) -> tuple[float, str]:
        if self._orders_today() >= self.daily_order_limit:
            return 0.0, f"daily live order limit reached ({self.daily_order_limit})"
        if self._realised_live_pnl() <= -self.max_loss:
            return 0.0, f"live loss stop reached (£{self.max_loss:.2f})"
        if self.max_open_bets and len(self.store.open_bets()) >= self.max_open_bets:
            return 0.0, f"max live open bets reached ({self.max_open_bets})"
        market_open = self.store.open_count(market_id=market.market_id)
        if (
            self.max_open_bets_per_market
            and market_open >= self.max_open_bets_per_market
        ):
            return 0.0, (
                f"market already has {market_open} live bet(s); "
                f"limit {self.max_open_bets_per_market}"
            )

        try:
            available, account_exposure = self._live_funds(force=force_funds)
        except Exception as exc:
            return 0.0, f"account funds check failed: {exc}"

        total_remaining = self.max_total_liability - account_exposure
        capacity = min(
            available,
            total_remaining,
            self.max_liability_per_bet,
        )
        if capacity <= 0:
            return 0.0, (
                "live exposure capacity exhausted "
                f"(available £{available:.2f}, account exposure £{account_exposure:.2f})"
            )
        return capacity, ""

    def _live_signal_allowed(self, assessment) -> tuple[bool, str]:
        side = assessment.recommended_side
        if side is None or side.value not in self.allowed_sides:
            return False, f"side {side.value if side else 'none'} not enabled"
        if not self.live_min_edge <= assessment.abs_edge <= self.live_max_edge:
            return False, (
                f"edge {assessment.abs_edge:.1%} outside live band "
                f"{self.live_min_edge:.0%}-{self.live_max_edge:.0%}"
            )
        if not self.min_confidence <= assessment.confidence <= self.max_confidence:
            return False, (
                f"confidence {assessment.confidence:.0%} outside live band "
                f"{self.min_confidence:.0%}-{self.max_confidence:.0%}"
            )
        return True, ""

    def _place_paper_bet(
        self, market, assessment, entry_index: int = 1,
        entry_reason: str = "initial",
    ):
        allowed, reason = self._live_signal_allowed(assessment)
        if not allowed:
            logger.info(
                "Skipping LIVE %s / %s: %s",
                market.event_name, assessment.runner_name, reason,
            )
            return None

        policy, policy_rejection = self._market_policy(market)
        if policy_rejection:
            logger.info(
                "Skipping LIVE %s / %s: %s",
                market.event_name, market.market_name, policy_rejection,
            )
            return None
        capacity, capacity_rejection = self._exposure_capacity(
            market, policy, force_funds=True
        )
        if capacity_rejection:
            logger.info(
                "Skipping LIVE %s / %s: %s",
                market.event_name, market.market_name, capacity_rejection,
            )
            return None

        side = assessment.recommended_side
        odds = (
            assessment.best_back if side == BetSide.BACK
            else assessment.best_lay
        )
        if not odds or odds <= 1.0:
            return None

        sizing = compute_bet_size(
            OddsSizingInputs(
                available_capital=self.starting_capital,
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
        sizing = self._cap_sizing(sizing, odds, side, capacity)
        stake = round(float(sizing["stake"]), 2)
        liability = round(float(sizing["liability"]), 2)
        if stake < self.min_stake or liability <= 0:
            logger.info(
                "Skipping LIVE %s: calculated stake £%.2f is below minimum £%.2f",
                assessment.runner_name, stake, self.min_stake,
            )
            return None
        if liability > self.max_liability_per_bet + 0.001:
            logger.error(
                "Refusing LIVE order: liability £%.2f exceeds £%.2f cap",
                liability, self.max_liability_per_bet,
            )
            return None

        local_id = str(uuid.uuid4())
        order_ref = f"oracle-{local_id}"[:32]
        response = None
        request_audit = {
            "local_id": local_id,
            "market_id": market.market_id,
            "event_name": market.event_name,
            "market_name": market.market_name,
            "selection_id": assessment.selection_id,
            "runner_name": assessment.runner_name,
            "side": side.value,
            "price": odds,
            "size": stake,
            "liability": liability,
            "edge": assessment.edge,
            "confidence": assessment.confidence,
            "provider": assessment.assessment_provider,
            "model": assessment.assessment_model,
        }
        try:
            response = self.client.place_limit_order(
                market_id=market.market_id,
                selection_id=assessment.selection_id,
                side=side.value,
                price=odds,
                size=stake,
                customer_ref=local_id.replace("-", ""),
                customer_order_ref=order_ref,
                strategy_ref=self.strategy_ref,
                market_version=market.market_version,
            )
        except Exception as exc:
            self._journal_attempt({
                **request_audit,
                "outcome": "api_error",
                "error": str(exc),
            })
            logger.error(
                "LIVE order API failure for %s / %s: %s",
                market.event_name, assessment.runner_name, exc,
            )
            return None

        reports = response.get("instructionReports") or []
        report = reports[0] if reports else {}
        report_status = str(report.get("status", "FAILURE"))
        overall_status = str(response.get("status", "FAILURE"))
        error_code = str(
            report.get("errorCode") or response.get("errorCode") or ""
        )
        matched = round(float(report.get("sizeMatched", 0.0) or 0.0), 2)
        average_price = float(report.get("averagePriceMatched", 0.0) or 0.0)
        exchange_bet_id = str(report.get("betId", ""))
        self._journal_attempt({
            **request_audit,
            "outcome": "response",
            "response": response,
        })

        if overall_status != "SUCCESS" or report_status != "SUCCESS":
            logger.error(
                "LIVE order rejected: status=%s/%s error=%s response=%r",
                overall_status, report_status, error_code, response,
            )
            return None

        actual_liability = (
            matched if side == BetSide.BACK
            else round(matched * (average_price - 1.0), 2)
        )
        status = (
            PaperBetStatus.FILLED if matched > 0
            else PaperBetStatus.UNFILLED
        )
        bet = PaperBet(
            bet_id=local_id,
            market_id=market.market_id,
            selection_id=assessment.selection_id,
            runner_name=assessment.runner_name,
            side=side,
            style=OrderStyle.CROSS,
            requested_odds=odds,
            stake=matched if matched > 0 else stake,
            liability=actual_liability if matched > 0 else 0.0,
            commission_rate=assessment.commission_rate,
            status=status,
            filled_odds=average_price if matched > 0 else None,
            filled_at=datetime.now(timezone.utc) if matched > 0 else None,
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
            sleeve=policy.get("name", "general"),
            entry_index=entry_index,
            entry_reason=entry_reason,
            edge_band=PaperBet.band_edge(assessment.abs_edge),
            confidence_band=PaperBet.band_confidence(assessment.confidence),
            strategy="llm_value_live",
            assessment_provider=assessment.assessment_provider or "unknown",
            assessment_model=assessment.assessment_model or "unknown",
            execution_mode="live",
            exchange_bet_id=exchange_bet_id,
            exchange_order_status=str(report.get("orderStatus", "")),
            exchange_error_code=error_code,
        )
        self.store.add(bet)

        if matched and abs(matched - stake) > 0.01:
            logger.critical(
                "FILL_OR_KILL returned partial match £%.2f of £%.2f; "
                "recorded actual exposure and stopping further orders this cycle",
                matched, stake,
            )
            self.max_bets_per_cycle = 0

        logger.warning(
            "LIVE %s %s @ %.2f: matched £%.2f, liability £%.2f, betId=%s",
            side.value, assessment.runner_name, odds, matched,
            bet.liability, exchange_bet_id,
        )
        return bet

    def _update_passive_orders(self):
        """Live mode never leaves passive orders resting."""

    def run_cycle(self):
        logger.warning("LIVE cycle starting: REAL MONEY execution is armed")
        return super().run_cycle()
