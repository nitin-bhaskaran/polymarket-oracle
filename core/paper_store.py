"""
Paper-bet store — persistence and lifecycle for the validation instrument.

Holds every PaperBet across runs (JSONL on disk so weeks of data accumulate
and survive restarts), and drives the lifecycle:
  PENDING  -> FILLED/UNFILLED   (via the fill simulator)
  FILLED   -> SETTLED           (when the market resolves)

Settlement needs the market outcome. For a paper-trader on resolved markets we
learn the winner from listMarketBook once the market is SETTLED (runner status
WINNER/LOSER). The store exposes the set of market IDs with open (FILLED,
unsettled) bets so the orchestrator knows which markets to keep polling for
resolution.

The on-disk format is JSON Lines: one PaperBet per line, rewritten on update.
At validation scale (hundreds–low thousands of bets) a full rewrite is fine and
keeps the format trivially analysable.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from core.betfair_models import PaperBet, PaperBetStatus

logger = logging.getLogger("betfair.paperstore")


class PaperBetStore:
    def __init__(self, path: str = "data/paper_bets.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bets: dict[str, PaperBet] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    bet = PaperBet(**json.loads(line))
                    self._bets[bet.bet_id] = bet
            logger.info(f"Loaded {len(self._bets)} paper bets from {self.path}")
        except Exception as e:
            logger.error(f"Failed to load paper bets: {e}")

    def _flush(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                for bet in self._bets.values():
                    f.write(bet.model_dump_json() + "\n")
            tmp.replace(self.path)
        except Exception as e:
            logger.error(f"Failed to flush paper bets: {e}")

    def add(self, bet: PaperBet):
        self._bets[bet.bet_id] = bet
        self._flush()

    def update(self, bet: PaperBet):
        self._bets[bet.bet_id] = bet
        self._flush()

    def all(self) -> list[PaperBet]:
        return list(self._bets.values())

    def pending(self) -> list[PaperBet]:
        return [b for b in self._bets.values() if b.status == PaperBetStatus.PENDING]

    def filled_unsettled(self) -> list[PaperBet]:
        return [b for b in self._bets.values() if b.status == PaperBetStatus.FILLED]

    def open_market_ids(self) -> set[str]:
        """Markets with FILLED-but-unsettled bets — keep polling these for resolution."""
        return {b.market_id for b in self.filled_unsettled()}

    def has_open_position(self, market_id: str, selection_id: int) -> bool:
        """True if we already hold a filled/pending bet on this selection (avoid dupes)."""
        for b in self._bets.values():
            if (b.market_id == market_id and b.selection_id == selection_id
                    and b.status in (PaperBetStatus.PENDING, PaperBetStatus.FILLED)):
                return True
        return False

    def count_by_status(self) -> dict:
        out: dict = {}
        for b in self._bets.values():
            out[b.status.value] = out.get(b.status.value, 0) + 1
        return out
