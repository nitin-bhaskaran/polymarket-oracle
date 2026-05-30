"""
Two-stage Betfair assessor — cheap triage, then web-search deep dive.

Stage 1 (triage, cheap): a single coherent call per market on a cheap model
(Haiku by default), NO web search, asking for a probability distribution over
all runners that sums to 1. Produces a rough edge. Markets whose best rough
edge clears triage_edge get promoted to stage 2. On an efficient exchange most
markets don't clear it, so the expensive stage runs rarely.

Stage 2 (deep, expensive): a single coherent call per market on a stronger
model (Sonnet) WITH the web_search tool enabled, so the model gathers current
form/news/injury context before estimating. Also returns a coherent
distribution. Gated by the AssessmentGovernor's daily budget.

Both stages make ONE call per market (all runners + current odds in the prompt),
which fixes the "probabilities don't sum to 1" incoherence from the old
per-runner approach AND cuts call volume ~Nx.

Cost levers wired here:
  - cheap triage filters out most markets before any web search
  - coherent single call (not N calls)
  - daily deep-assessment budget (via governor)
  - model split: Haiku triage, Sonnet deep
"""

import json
import logging
from typing import Optional

import anthropic

from core.betfair_models import (
    BetfairAssessment, BetfairMarket, BetSide, Runner, RunnerStatus,
)

logger = logging.getLogger("betfair.assessor2")


TRIAGE_SYSTEM = """You are a betting analyst. Given a market and its runners with \
current exchange odds, estimate the TRUE probability of each runner winning, as a \
distribution that sums to 1.0. Use only your existing knowledge; do not be \
overconfident when you lack current information. Reply ONLY with raw JSON:
{"probabilities": {"<selection_id>": 0.XX, ...}, "confidence": 0.XX}"""

DEEP_SYSTEM = """You are a professional betting analyst with web search. For the given \
market, SEARCH for current, relevant information (recent form, team news, injuries, \
lineups, head-to-head, anything affecting the outcome) BEFORE estimating. Then \
estimate the TRUE probability of each runner winning as a distribution summing to 1.0. \
Be calibrated and honest about uncertainty. After searching, reply with ONLY a raw \
JSON object (no prose outside it):
{"probabilities": {"<selection_id>": 0.XX, ...}, "confidence": 0.XX, "reasoning": "what the search found and your logic"}"""


class TwoStageAssessor:
    def __init__(self, config: dict):
        ac = config.get("anthropic", {})
        bf = config.get("betfair_assessor", {})

        self.triage_model = bf.get("triage_model", "claude-haiku-4-5-20251001")
        self.deep_model = bf.get("deep_model", ac.get("model", "claude-sonnet-4-6"))
        self.max_tokens = ac.get("max_tokens", 1500)
        self.triage_edge = bf.get("triage_edge", 0.04)   # promote to deep if rough edge >= this
        self.min_edge = bf.get("min_edge", 0.05)         # actionable edge after deep
        self.web_search_max_uses = bf.get("web_search_max_uses", 3)
        self.max_runners = bf.get("max_runners_per_market", 8)

        self.client = anthropic.Anthropic(api_key=ac.get("api_key", ""))

    # ── helpers ──

    @staticmethod
    def _extract_text(response) -> str:
        parts = []
        for block in (getattr(response, "content", None) or []):
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
        return "\n".join(parts).strip()

    @staticmethod
    def _all_json_objects(text: str) -> list[dict]:
        """
        Find ALL balanced top-level JSON objects in text (web-search responses
        interleave narration with the answer, and the model may emit braces in
        prose before the real JSON). Returns every parseable object, in order.
        """
        objs = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] != "{":
                i += 1
                continue
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                objs.append(json.loads(text[i:j + 1]))
                            except json.JSONDecodeError:
                                pass
                            break
                j += 1
            i = j + 1
        return objs

    def _extract_json(self, text: str) -> Optional[dict]:
        """
        Extract the answer object. Prefer the LAST object that contains a
        'probabilities' key (the model's final answer after any search
        narration). Falls back to the last parseable object, then None.
        """
        if not text:
            return None
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines)

        objs = self._all_json_objects(text)
        if not objs:
            return None
        for obj in reversed(objs):
            if isinstance(obj, dict) and "probabilities" in obj:
                return obj
        return objs[-1]  # last resort

    def _market_prompt(self, market: BetfairMarket, active: list[Runner]) -> str:
        lines = [
            f"MARKET: {market.market_name} — {market.event_name}",
            f"COMPETITION: {market.competition}",
            f"SPORT/TYPE: {market.sport}",
            "RUNNERS (with current exchange best-back odds):",
        ]
        for r in active:
            lines.append(f"  - selection_id {r.selection_id}: {r.name} @ {r.best_back}")
        from datetime import datetime, timezone
        lines.append(f"TODAY: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}")
        lines.append("Estimate P(win) for each selection_id as a distribution summing to 1.0.")
        return "\n".join(lines)

    def _to_assessments(self, market, active, data) -> list[BetfairAssessment]:
        probs = data.get("probabilities", {}) if data else {}
        conf = float(data.get("confidence", 0.5)) if data else 0.5
        reasoning = data.get("reasoning", "") if data else ""
        out = []
        for r in active:
            p = probs.get(str(r.selection_id))
            if p is None:
                continue
            p = max(0.01, min(0.99, float(p)))
            fair = market.fair_implied_prob(r)
            if fair is None:
                continue
            a = BetfairAssessment(
                market_id=market.market_id, selection_id=r.selection_id,
                runner_name=r.name, question=f"Will {r.name} win {market.market_name}?",
                estimated_probability=p, confidence=max(0.0, min(1.0, conf)),
                reasoning=reasoning, market_fair_prob=fair,
                best_back=r.best_back, best_lay=r.best_lay,
                commission_rate=market.commission_rate,
            )
            a.calculate_edge()
            out.append(a)
        return out

    # ── stage 1: triage (cheap, no search) ──

    def triage(self, market: BetfairMarket) -> tuple[float, list[BetfairAssessment]]:
        """Return (best_abs_edge, assessments) from a cheap no-search pass."""
        active = [r for r in market.runners
                  if r.status == RunnerStatus.ACTIVE and r.best_back][:self.max_runners]
        if not active:
            return 0.0, []
        try:
            resp = self.client.messages.create(
                model=self.triage_model, max_tokens=self.max_tokens,
                system=TRIAGE_SYSTEM,
                messages=[{"role": "user", "content": self._market_prompt(market, active)}],
            )
            data = self._extract_json(self._extract_text(resp))
        except Exception as e:
            logger.warning(f"Triage failed for {market.market_id}: {e}")
            return 0.0, []
        assessments = self._to_assessments(market, active, data)
        best = max((a.abs_edge for a in assessments), default=0.0)
        return best, assessments

    # ── stage 2: deep (web search) ──

    def deep_assess(self, market: BetfairMarket) -> list[BetfairAssessment]:
        active = [r for r in market.runners
                  if r.status == RunnerStatus.ACTIVE and r.best_back][:self.max_runners]
        if not active:
            return []
        try:
            resp = self.client.messages.create(
                model=self.deep_model, max_tokens=self.max_tokens,
                system=DEEP_SYSTEM,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": self.web_search_max_uses,
                }],
                messages=[{"role": "user", "content": self._market_prompt(market, active)}],
            )
        except Exception as e:
            logger.error(f"Deep assess API call failed for {market.market_id} "
                         f"({market.event_name}): {e}")
            return []

        # Count web searches actually performed (for cost visibility).
        searches = sum(
            1 for b in (getattr(resp, "content", None) or [])
            if getattr(b, "type", "") == "server_tool_use"
        )
        text = self._extract_text(resp)
        data = self._extract_json(text)
        if not data or "probabilities" not in data:
            # Loud failure: we paid for this (incl. web searches) and got nothing.
            logger.error(
                f"Deep assess for {market.market_id} ({market.event_name}) returned "
                f"no parseable probabilities after {searches} search(es). "
                f"Response head: {text[:300]!r}"
            )
            return []
        logger.info(f"Deep assess {market.event_name}: {searches} search(es), parsed OK")
        return self._to_assessments(market, active, data)
