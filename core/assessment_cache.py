"""
Assessment cache + daily budget governor — the cost-control core.

Two jobs:

1. Assess-once with change-triggered re-assessment. Pre-event markets barely
   move; re-assessing every cycle wastes money. We cache each market's deep
   assessment keyed by market_id and only allow a re-assessment when:
     - it has aged past reassess_after_hours, OR
     - the market's odds have moved more than reassess_on_move (fractional) on
       the assessed runner since last time.
   A market that never moves is paid for once, ever.

2. Hard daily budget cap on EXPENSIVE (web-search) assessments. A counter,
   reset at UTC midnight, that the orchestrator checks before spending. When
   the cap is hit, scanning/monitoring/settlement continue but no new deep
   assessments are run until tomorrow. This is an in-code backstop independent
   of the Anthropic-side spend cap.

Persisted to disk so restarts don't reset the budget or lose the cache.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("betfair.cache")


class AssessmentGovernor:
    def __init__(self, config: dict):
        paper = config.get("paper", {})
        self.reassess_after_hours = paper.get("reassess_after_hours", 6.0)
        self.reassess_on_move = paper.get("reassess_on_move", 0.05)  # 5% odds move
        self.daily_deep_budget = paper.get("daily_deep_assessment_budget", 50)
        self.state_path = Path(paper.get("governor_state_path", "data/governor.json"))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        # market_id -> {"assessed_at": iso, "odds": {selection_id: odds}}
        self._cache: dict = {}
        self._deep_today = 0
        self._day = self._today()
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self._cache = data.get("cache", {})
            self._deep_today = data.get("deep_today", 0)
            self._day = data.get("day", self._today())
            # Reset counter if the saved day is stale.
            if self._day != self._today():
                self._deep_today = 0
                self._day = self._today()
        except Exception as e:
            logger.warning(f"Failed to load governor state: {e}")

    def _flush(self):
        try:
            self.state_path.write_text(json.dumps({
                "cache": self._cache,
                "deep_today": self._deep_today,
                "day": self._day,
            }, indent=2))
        except Exception as e:
            logger.warning(f"Failed to flush governor state: {e}")

    def _roll_day_if_needed(self):
        today = self._today()
        if today != self._day:
            logger.info(f"New day {today}: resetting deep-assessment budget "
                        f"(used {self._deep_today} yesterday)")
            self._deep_today = 0
            self._day = today
            self._flush()

    # ── budget ──

    def deep_budget_remaining(self) -> int:
        self._roll_day_if_needed()
        return max(0, self.daily_deep_budget - self._deep_today)

    def can_deep_assess(self) -> bool:
        return self.deep_budget_remaining() > 0

    def record_deep_assessment(self):
        self._roll_day_if_needed()
        self._deep_today += 1
        self._flush()

    # ── change-triggered re-assessment ──

    def needs_assessment(self, market) -> bool:
        """
        True if this market should be (re)assessed now: never seen, aged out,
        or odds moved materially on any runner since last assessment.
        """
        entry = self._cache.get(market.market_id)
        if entry is None:
            return True

        # Age check
        try:
            last = datetime.fromisoformat(entry["assessed_at"])
        except Exception:
            return True
        age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if age_h >= self.reassess_after_hours:
            return True

        # Movement check — compare best-back odds per runner.
        prev = entry.get("odds", {})
        for r in market.runners:
            now_odds = r.best_back
            old = prev.get(str(r.selection_id))
            if now_odds and old:
                if abs(now_odds - old) / old >= self.reassess_on_move:
                    return True
        return False

    def record_assessment(self, market):
        """Cache the fact that we assessed this market and its current odds."""
        self._cache[market.market_id] = {
            "assessed_at": datetime.now(timezone.utc).isoformat(),
            "odds": {str(r.selection_id): r.best_back
                     for r in market.runners if r.best_back},
        }
        self._flush()
