"""
Betfair assessor — gets AI win-probabilities for runners and computes edge.

Bridges the venue-agnostic Claude probability engine to Betfair's multi-runner
markets. For each active runner it asks Claude "what is P(this runner wins this
market?)", then compares to the overround-adjusted market implied probability to
produce a BetfairAssessment with signed edge and a BACK/LAY recommendation.

Reuses the existing engine's hardened machinery: the JSON-robust parser, the
retry/backoff Claude call, and the concurrency pool. We build a per-runner
prompt rather than reusing the binary YES/NO prompt, because a runner in a
3-way market is not a YES/NO question.

Calibration note: in a coherent market the AI's per-runner probabilities should
roughly sum to 1.0 across runners. We optionally normalise them so the edge
isn't an artifact of the model's probabilities not summing to 1 — configurable,
because normalising can also mask genuine disagreement. Off by default; the
analysis script tracks both raw and normalised.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import anthropic

from core.betfair_models import BetfairAssessment, BetfairMarket, Runner, RunnerStatus

logger = logging.getLogger("betfair.assessor")


RUNNER_SYSTEM_PROMPT = """You are a professional sports and political betting analyst. \
You estimate the TRUE probability that a specific selection wins a specific market, \
independent of the current betting odds.

Be calibrated: when you say 30%, it should happen ~30% of the time. Consider base \
rates, form, head-to-head, context, and what you may not know given your knowledge \
cutoff. Avoid extreme probabilities unless strongly justified.

Reply with ONLY a raw JSON object, no preamble, no markdown fences:
{
    "probability": 0.XX,
    "confidence": 0.XX,
    "reasoning": "1-2 sentences",
    "key_factors": ["factor 1", "factor 2"]
}"""


class BetfairAssessor:
    def __init__(self, config: dict, claude_engine=None):
        ac = config.get("anthropic", {})
        self.model = ac.get("model", "claude-sonnet-4-6")
        self.max_tokens = ac.get("max_tokens", 1024)
        self.max_workers = ac.get("max_concurrent_assessments", 4)
        self.max_retries = ac.get("max_retries", 3)
        self.retry_base_delay = ac.get("retry_base_delay_seconds", 2.0)

        bf = config.get("betfair_assessor", {})
        self.min_edge = bf.get("min_edge", 0.05)
        self.normalise_probs = bf.get("normalise_probabilities", False)
        self.max_runners = bf.get("max_runners_per_market", 8)

        # Reuse the hardened engine's Claude client + helpers if provided.
        self.engine = claude_engine
        if self.engine is not None:
            self.client = self.engine.client
        else:
            self.client = anthropic.Anthropic(api_key=ac.get("api_key", ""))

    # ── Per-runner Claude call (reuses engine helpers when available) ──

    def _assess_runner_prob(self, market: BetfairMarket, runner: Runner) -> Optional[dict]:
        prompt = (
            f"MARKET: {market.market_name} — {market.event_name}\n"
            f"COMPETITION: {market.competition}\n"
            f"SELECTION: {runner.name}\n"
            f"TODAY: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
            f"What is the TRUE probability that '{runner.name}' wins this market?"
        )
        try:
            if self.engine is not None:
                # Reuse hardened retry + parse path with our system prompt.
                resp = self._call_with_engine_retry(prompt)
            else:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=self.max_tokens,
                    system=RUNNER_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
            text = self._extract_text(resp)
            data = self._parse_json(text)
            return data
        except anthropic.APIError as e:
            logger.error(f"Claude error assessing {runner.name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Assessment failed for {runner.name}: {e}")
            return None

    def _call_with_engine_retry(self, prompt: str):
        import time
        delay = self.retry_base_delay
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.client.messages.create(
                    model=self.model, max_tokens=self.max_tokens,
                    system=RUNNER_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
            except (anthropic.RateLimitError, anthropic.APITimeoutError,
                    anthropic.APIConnectionError) as e:
                if attempt == self.max_retries:
                    raise
                logger.warning(f"Claude transient error (attempt {attempt}), retrying in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2

    @staticmethod
    def _extract_text(response) -> str:
        content = getattr(response, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                return text.strip()
        return ""

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        if not text:
            return None
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    def _parse_json(self, text: str) -> Optional[dict]:
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            obj = self._extract_json_object(text)
            return json.loads(obj) if obj else None

    # ── Public: assess a whole market ──

    def assess_market(self, market: BetfairMarket) -> list[BetfairAssessment]:
        active = [r for r in market.runners if r.status == RunnerStatus.ACTIVE][:self.max_runners]
        if not active:
            return []

        # Assess runners concurrently.
        raw: dict[int, dict] = {}
        workers = max(1, min(self.max_workers, len(active)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._assess_runner_prob, market, r): r for r in active}
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    data = fut.result()
                except Exception as e:
                    logger.error(f"runner task failed {r.name}: {e}")
                    continue
                if data:
                    raw[r.selection_id] = data

        if not raw:
            return []

        # Optional normalisation so AI probs sum to ~1 across runners.
        probs = {sid: max(0.01, min(0.99, float(d.get("probability", 0.5))))
                 for sid, d in raw.items()}
        if self.normalise_probs:
            total = sum(probs.values())
            if total > 0:
                probs = {sid: p / total for sid, p in probs.items()}

        assessments = []
        for r in active:
            if r.selection_id not in raw:
                continue
            d = raw[r.selection_id]
            fair = market.fair_implied_prob(r)
            if fair is None:
                continue
            a = BetfairAssessment(
                market_id=market.market_id,
                selection_id=r.selection_id,
                runner_name=r.name,
                question=f"Will {r.name} win {market.market_name}?",
                estimated_probability=probs[r.selection_id],
                confidence=max(0.0, min(1.0, float(d.get("confidence", 0.5)))),
                reasoning=d.get("reasoning", ""),
                key_factors=d.get("key_factors", []),
                market_fair_prob=fair,
                best_back=r.best_back,
                best_lay=r.best_lay,
                commission_rate=market.commission_rate,
            )
            a.calculate_edge()
            assessments.append(a)

        assessments.sort(key=lambda x: x.abs_edge, reverse=True)
        return assessments
