"""
Read-only manual Betfair recommendations.

This mode reuses the paper trader's scanner, two-stage assessment, cost
governor, sleeve policy, coherence checks, and odds filters. It never calls an
order endpoint. Recommendations are refreshed against the latest available
book, expire quickly, and are sized under a small fixed liability budget.
"""

import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.betfair_models import (
    BetSide, ManualRecommendation, MarketPhase, RunnerStatus,
)
from core.betfair_sizing import (
    OddsSizingConfig, OddsSizingInputs, compute_bet_size,
)

logger = logging.getLogger("betfair.recommend")


class BetfairRecommendationEngine:
    def __init__(self, config: dict, scanner, signal_trader):
        self.config = config
        self.scanner = scanner
        self.signal_trader = signal_trader
        rc = config.get("recommendations", {})

        self.bankroll = float(rc.get("bankroll_gbp", 10.0))
        self.min_stake = float(rc.get("min_stake_gbp", 1.0))
        self.use_kelly = bool(rc.get("use_kelly_sizing", False))
        self.kelly_fraction = float(rc.get("kelly_fraction", 0.25))
        self.max_liability_per_bet = float(
            rc.get("max_liability_per_bet_gbp", 2.0)
        )
        self.max_total_liability = float(
            rc.get("max_total_liability_gbp", 3.0)
        )
        self.max_recommendations = int(rc.get("max_recommendations", 2))
        self.max_markets_to_assess = int(rc.get("max_markets_to_assess", 8))
        self.min_hours_ahead = float(rc.get("min_hours_ahead", 0.5))
        self.max_hours_ahead = float(rc.get("max_hours_ahead", 24.0))
        self.valid_for_minutes = float(rc.get("valid_for_minutes", 10.0))
        self.min_edge = float(rc.get("min_edge", 0.08))
        self.max_edge = float(rc.get("max_edge", 0.12))
        self.min_confidence = float(rc.get("min_confidence", 0.50))
        self.max_confidence = float(rc.get("max_confidence", 0.75))
        self.allowed_sides = {
            str(side).upper()
            for side in rc.get("allowed_sides", ["LAY"])
        }
        self.output_path = Path(
            rc.get(
                "output_path",
                "data/manual_recommendations_latest.json",
            )
        )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.price_data_mode = str(
            config.get("betfair", {}).get("app_key_mode", "delayed")
        ).lower()

    def _signal_rejection(self, assessment) -> str:
        side = assessment.recommended_side
        if side is None or side.value not in self.allowed_sides:
            return f"side {side.value if side else 'none'} not enabled"
        if not self.min_edge <= assessment.abs_edge <= self.max_edge:
            return (
                f"edge {assessment.abs_edge:.1%} outside "
                f"{self.min_edge:.0%}-{self.max_edge:.0%}"
            )
        if not self.min_confidence <= assessment.confidence <= self.max_confidence:
            return (
                f"confidence {assessment.confidence:.0%} outside "
                f"{self.min_confidence:.0%}-{self.max_confidence:.0%}"
            )
        return ""

    def _refresh_assessment(self, market, assessment):
        refreshed = self.scanner.refresh_book(market.market_id)
        if not refreshed:
            return None, "fresh book unavailable"
        if refreshed.in_play or refreshed.phase != MarketPhase.PRE_EVENT:
            return None, "market is no longer pre-event"
        refreshed.event_name = refreshed.event_name or market.event_name
        refreshed.market_name = refreshed.market_name or market.market_name
        refreshed.competition = refreshed.competition or market.competition
        refreshed.domain = refreshed.domain or market.domain
        refreshed.sport = refreshed.sport or market.sport
        refreshed.start_time = refreshed.start_time or market.start_time

        runner = next(
            (
                item for item in refreshed.runners
                if item.selection_id == assessment.selection_id
                and item.status == RunnerStatus.ACTIVE
            ),
            None,
        )
        if not runner:
            return None, "selection is no longer active"
        fair = refreshed.fair_implied_prob(runner)
        if fair is None:
            return None, "fresh fair probability unavailable"

        assessment.best_back = runner.best_back
        assessment.best_lay = runner.best_lay
        assessment.market_fair_prob = fair
        assessment.commission_rate = refreshed.commission_rate
        assessment.calculate_edge()
        if not self.signal_trader._odds_in_band(assessment):
            return None, "fresh odds are outside the tradeable band"
        return refreshed, ""

    def _size(self, assessment, remaining_liability: float) -> dict:
        side = assessment.recommended_side
        odds = (
            assessment.best_back if side == BetSide.BACK
            else assessment.best_lay
        )
        if not odds or odds <= 1.0:
            return {"stake": 0.0, "liability": 0.0}

        maximum = min(self.max_liability_per_bet, remaining_liability)
        if maximum <= 0:
            return {"stake": 0.0, "liability": 0.0}
        decision = compute_bet_size(
            OddsSizingInputs(
                available_capital=self.bankroll,
                odds=odds,
                fair_probability=assessment.estimated_probability,
                confidence=assessment.confidence,
                side=side,
                commission_rate=assessment.commission_rate,
            ),
            OddsSizingConfig(
                max_position_pct=self.max_liability_per_bet / self.bankroll,
                kelly_fraction=self.kelly_fraction,
                min_stake=self.min_stake,
                use_kelly=self.use_kelly,
            ),
        )
        liability = min(float(decision["liability"]), maximum)
        stake = (
            liability if side == BetSide.BACK
            else liability / (odds - 1.0)
        )
        stake = round(stake, 2)
        liability = round(liability, 2)
        if stake < self.min_stake or liability <= 0:
            return {"stake": 0.0, "liability": 0.0}
        return {"stake": stake, "liability": liability}

    @staticmethod
    def _price_rule(side: BetSide, odds: float) -> str:
        if side == BetSide.BACK:
            return f"BACK only at {odds:.2f} or higher"
        return f"LAY only at {odds:.2f} or lower"

    def _ticket(self, market, assessment, sleeve, sizing):
        now = datetime.now(timezone.utc)
        odds = (
            assessment.best_back
            if assessment.recommended_side == BetSide.BACK
            else assessment.best_lay
        )
        return ManualRecommendation(
            recommendation_id=str(uuid.uuid4()),
            generated_at=now,
            valid_until=now + timedelta(minutes=self.valid_for_minutes),
            market_id=market.market_id,
            event_name=market.event_name,
            market_name=market.market_name,
            competition=market.competition,
            start_time=market.start_time,
            selection_id=assessment.selection_id,
            runner_name=assessment.runner_name,
            side=assessment.recommended_side,
            quoted_odds=odds,
            price_rule=self._price_rule(assessment.recommended_side, odds),
            stake=sizing["stake"],
            liability=sizing["liability"],
            estimated_probability=assessment.estimated_probability,
            market_fair_prob=assessment.market_fair_prob,
            edge=assessment.edge,
            confidence=assessment.confidence,
            reasoning=assessment.reasoning,
            sleeve=sleeve,
            assessment_provider=assessment.assessment_provider,
            assessment_model=assessment.assessment_model,
            price_data_mode=self.price_data_mode,
        )

    def run_once(self) -> list[ManualRecommendation]:
        markets = self.scanner.scan()
        candidates = []
        assessed = 0
        rejections = Counter()

        for market in markets:
            if assessed >= self.max_markets_to_assess:
                break
            if market.in_play or market.phase != MarketPhase.PRE_EVENT:
                rejections["in_play_or_closed"] += 1
                continue
            if not (
                self.min_hours_ahead
                <= market.hours_to_start
                <= self.max_hours_ahead
            ):
                rejections["outside_time_window"] += 1
                continue
            policy, policy_rejection = self.signal_trader._market_policy(market)
            if policy_rejection:
                rejections[f"policy: {policy_rejection}"] += 1
                continue

            assessed += 1
            assessments = self.signal_trader._assess(market)
            if not assessments:
                rejections["no_deep_assessment_output"] += 1
            best_for_market = None
            for assessment in assessments:
                refreshed, refresh_rejection = self._refresh_assessment(
                    market, assessment
                )
                if refresh_rejection:
                    rejections[f"refresh: {refresh_rejection}"] += 1
                    continue
                rejection = self._signal_rejection(assessment)
                if rejection:
                    rejections[f"signal: {rejection}"] += 1
                    continue
                score = assessment.abs_edge * assessment.confidence
                candidate = (
                    score,
                    refreshed,
                    assessment,
                    policy.get("name", "general"),
                )
                if best_for_market is None or score > best_for_market[0]:
                    best_for_market = candidate
            if best_for_market:
                candidates.append(best_for_market)

        candidates.sort(key=lambda item: item[0], reverse=True)
        recommendations = []
        total_liability = 0.0
        for _, market, assessment, sleeve in candidates:
            remaining = self.max_total_liability - total_liability
            sizing = self._size(assessment, remaining)
            if sizing["stake"] <= 0:
                rejections["sizing_below_minimum_or_budget"] += 1
                continue
            ticket = self._ticket(market, assessment, sleeve, sizing)
            recommendations.append(ticket)
            total_liability += ticket.liability
            if len(recommendations) >= self.max_recommendations:
                break

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "price_data_mode": self.price_data_mode,
            "markets_scanned": len(markets),
            "markets_assessed": assessed,
            "total_recommended_liability": round(total_liability, 2),
            "rejection_summary": dict(rejections.most_common()),
            "recommendations": [
                recommendation.model_dump(mode="json")
                for recommendation in recommendations
            ],
        }
        self.output_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        self._log(recommendations, assessed, rejections)
        return recommendations

    def _log(self, recommendations, assessed, rejections):
        if self.price_data_mode != "live":
            logger.warning(
                "Prices are from a DELAYED app key. Recheck the Betfair screen "
                "and obey each ticket's price rule before placing manually."
            )
        if not recommendations:
            logger.warning(
                "NO MANUAL BET RECOMMENDED after assessing %s market(s). "
                "Rejections: %s",
                assessed, dict(rejections.most_common(5)),
            )
            return
        for index, ticket in enumerate(recommendations, 1):
            start = (
                ticket.start_time.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                if ticket.start_time else "unknown"
            )
            logger.warning(
                "MANUAL TICKET %s: %s %s | %s / %s | %s | "
                "stake £%.2f, max liability £%.2f | edge %+.1f%%, "
                "confidence %.0f%% | starts %s | expires %s",
                index,
                ticket.side.value,
                ticket.runner_name,
                ticket.event_name,
                ticket.market_name,
                ticket.price_rule,
                ticket.stake,
                ticket.liability,
                ticket.edge * 100,
                ticket.confidence * 100,
                start,
                ticket.valid_until.strftime("%H:%M:%S UTC"),
            )
