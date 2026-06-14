"""
Two-stage Betfair assessor: cheap triage, then grounded deep assessment.

Both stages make one coherent call per market and estimate a probability
distribution over all runners. Providers are tried in configured order.
Gemini can handle cheap/free triage and Google Search-grounded assessment;
Anthropic remains an automatic fallback.

Provider and model are attached to every result so paper trading can compare
calibration and ROI instead of assuming the cheaper route is equivalent.
"""

import json
import logging
from typing import Optional

import anthropic
import httpx

from core.betfair_models import (
    BetfairAssessment, BetfairMarket, Runner, RunnerStatus,
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
Be calibrated and honest about uncertainty.

CRITICAL OUTPUT RULE: Keep any commentary to at most two short sentences. You MUST end \
your response with the JSON object below and it must be complete. Do not write long \
research summaries; put a brief justification inside the "reasoning" field, not before \
the JSON. The final thing in your reply must be:
{"probabilities": {"<selection_id>": 0.XX, ...}, "confidence": 0.XX, "reasoning": "brief"}"""


class TwoStageAssessor:
    def __init__(self, config: dict):
        ac = config.get("anthropic", {})
        gc = config.get("gemini", {})
        bf = config.get("betfair_assessor", {})

        self.triage_model = bf.get("triage_model", "claude-haiku-4-5-20251001")
        self.deep_model = bf.get(
            "deep_model", ac.get("model", "claude-sonnet-4-6")
        )
        self.gemini_triage_model = gc.get(
            "triage_model", "gemini-2.5-flash-lite"
        )
        self.gemini_deep_model = gc.get("deep_model", "gemini-2.5-flash")
        self.gemini_api_key = gc.get("api_key", "")
        self.gemini_base_url = gc.get(
            "base_url", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self.gemini_timeout = gc.get("timeout_seconds", 45.0)

        self.provider_order = bf.get("provider_order", ["anthropic"])
        if isinstance(self.provider_order, str):
            self.provider_order = [self.provider_order]

        self.max_tokens = ac.get("max_tokens", 1500)
        self.deep_max_tokens = bf.get("deep_max_tokens", 4000)
        self.triage_edge = bf.get("triage_edge", 0.04)
        self.min_edge = bf.get("min_edge", 0.05)
        self.web_search_max_uses = bf.get("web_search_max_uses", 3)
        self.max_runners = bf.get("max_runners_per_market", 8)

        self.anthropic_api_key = ac.get("api_key", "")
        self.client = anthropic.Anthropic(api_key=self.anthropic_api_key)
        self.allow_paid_deep_fallback = True
        self.paid_deep_used = False

    @staticmethod
    def _extract_text(response) -> str:
        parts = []
        for block in (getattr(response, "content", None) or []):
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _all_json_objects(text: str) -> list[dict]:
        """Return all balanced, parseable top-level JSON objects in text."""
        objects = []
        i = 0
        while i < len(text):
            if text[i] != "{":
                i += 1
                continue
            depth = 0
            in_string = False
            escaped = False
            j = i
            while j < len(text):
                char = text[j]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                else:
                    if char == '"':
                        in_string = True
                    elif char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                objects.append(json.loads(text[i:j + 1]))
                            except json.JSONDecodeError:
                                pass
                            break
                j += 1
            i = j + 1
        return objects

    def _extract_json(self, text: str) -> Optional[dict]:
        """Prefer the final JSON object containing a probabilities field."""
        if not text:
            return None
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines)

        objects = self._all_json_objects(text)
        if not objects:
            return None
        for obj in reversed(objects):
            if isinstance(obj, dict) and "probabilities" in obj:
                return obj
        return objects[-1]

    def _market_prompt(self, market: BetfairMarket, active: list[Runner]) -> str:
        lines = [
            f"MARKET: {market.market_name} - {market.event_name}",
            f"COMPETITION: {market.competition}",
            f"SPORT/TYPE: {market.sport}",
            "RUNNERS (with current exchange best-back odds):",
        ]
        for runner in active:
            lines.append(
                f"  - selection_id {runner.selection_id}: "
                f"{runner.name} @ {runner.best_back}"
            )
        from datetime import datetime, timezone
        lines.append(
            f"TODAY: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}"
        )
        lines.append(
            "Estimate P(win) for each selection_id as a distribution summing to 1.0."
        )
        return "\n".join(lines)

    def _to_assessments(
        self, market, active, data, provider: str = "", model: str = ""
    ) -> list[BetfairAssessment]:
        probabilities = data.get("probabilities", {}) if data else {}
        confidence = float(data.get("confidence", 0.5)) if data else 0.5
        reasoning = data.get("reasoning", "") if data else ""
        assessments = []
        for runner in active:
            probability = probabilities.get(str(runner.selection_id))
            if probability is None:
                continue
            probability = max(0.01, min(0.99, float(probability)))
            fair = market.fair_implied_prob(runner)
            if fair is None:
                continue
            assessment = BetfairAssessment(
                market_id=market.market_id,
                selection_id=runner.selection_id,
                runner_name=runner.name,
                question=f"Will {runner.name} win {market.market_name}?",
                estimated_probability=probability,
                confidence=max(0.0, min(1.0, confidence)),
                reasoning=reasoning,
                market_fair_prob=fair,
                best_back=runner.best_back,
                best_lay=runner.best_lay,
                commission_rate=market.commission_rate,
                assessment_provider=provider,
                assessment_model=model,
            )
            assessment.calculate_edge()
            assessments.append(assessment)
        return assessments

    @staticmethod
    def _configured_key(value: str) -> bool:
        return bool(value and not value.startswith("YOUR_"))

    def _provider_available(self, provider: str) -> bool:
        if provider == "gemini":
            return self._configured_key(self.gemini_api_key)
        if provider == "anthropic":
            return self._configured_key(self.anthropic_api_key)
        return False

    def configured_providers(self) -> list[str]:
        """Providers in routing order that currently have usable credentials."""
        return [
            str(provider).lower()
            for provider in self.provider_order
            if self._provider_available(str(provider).lower())
        ]

    def _gemini_call(
        self, *, model: str, system: str, prompt: str, max_tokens: int,
        grounded: bool,
    ) -> tuple[str, int, object]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
            },
        }
        if grounded:
            body["tools"] = [{"google_search": {}}]
        else:
            body["generationConfig"]["responseMimeType"] = "application/json"

        response = httpx.post(
            f"{self.gemini_base_url}/models/{model}:generateContent",
            headers={
                "x-goog-api-key": self.gemini_api_key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.gemini_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError("Gemini returned no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "\n".join(
            part.get("text", "") for part in parts if part.get("text")
        ).strip()
        grounding = candidates[0].get("groundingMetadata", {})
        searches = len(grounding.get("webSearchQueries") or [])
        return text, searches, payload

    def _anthropic_call(
        self, *, model: str, system: str, prompt: str, max_tokens: int,
        grounded: bool,
    ) -> tuple[str, int, object]:
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if grounded:
            kwargs["tools"] = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": self.web_search_max_uses,
            }]
        response = self.client.messages.create(**kwargs)
        searches = sum(
            1 for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", "") == "server_tool_use"
        )
        return self._extract_text(response), searches, response

    def _call_stage(
        self, *, stage: str, system: str, prompt: str, max_tokens: int,
        grounded: bool,
    ) -> tuple[Optional[dict], str, str, str, int, object]:
        errors = []
        last_result = ("", "", "", 0, None)
        for configured_provider in self.provider_order:
            provider = str(configured_provider).lower()
            if not self._provider_available(provider):
                continue
            if provider == "gemini":
                model = (
                    self.gemini_triage_model if stage == "triage"
                    else self.gemini_deep_model
                )
            elif provider == "anthropic":
                if stage == "deep" and not self.allow_paid_deep_fallback:
                    logger.info(
                        "Skipping Anthropic deep fallback: daily paid budget exhausted"
                    )
                    continue
                model = self.triage_model if stage == "triage" else self.deep_model
            else:
                logger.warning("Unknown assessment provider %r; skipping", provider)
                continue

            try:
                if provider == "gemini":
                    text, searches, raw_response = self._gemini_call(
                        model=model,
                        system=system,
                        prompt=prompt,
                        max_tokens=max_tokens,
                        grounded=grounded,
                    )
                else:
                    if stage == "deep":
                        self.paid_deep_used = True
                    text, searches, raw_response = self._anthropic_call(
                        model=model,
                        system=system,
                        prompt=prompt,
                        max_tokens=max_tokens,
                        grounded=grounded,
                    )
                last_result = (provider, model, text, searches, raw_response)
                data = self._extract_json(text)
                if data and "probabilities" in data:
                    return data, provider, model, text, searches, raw_response
                errors.append(f"{provider}/{model}: no parseable probabilities")
            except Exception as exc:
                errors.append(f"{provider}/{model}: {exc}")
                logger.warning(
                    "%s assessment failed via %s/%s; trying fallback: %s",
                    stage.capitalize(), provider, model, exc,
                )

        if errors:
            logger.error(
                "%s assessment exhausted providers: %s",
                stage, "; ".join(errors),
            )
        else:
            logger.error(
                "%s assessment has no configured provider. Set GEMINI_API_KEY "
                "or ANTHROPIC_API_KEY.",
                stage,
            )
        provider, model, text, searches, raw_response = last_result
        return None, provider, model, text, searches, raw_response

    def triage(self, market: BetfairMarket) -> tuple[float, list[BetfairAssessment]]:
        """Return (best_abs_edge, assessments) from a cheap no-search pass."""
        active = [
            runner for runner in market.runners
            if runner.status == RunnerStatus.ACTIVE and runner.best_back
        ][:self.max_runners]
        if not active:
            return 0.0, []

        data, provider, model, _, _, _ = self._call_stage(
            stage="triage",
            system=TRIAGE_SYSTEM,
            prompt=self._market_prompt(market, active),
            max_tokens=self.max_tokens,
            grounded=False,
        )
        if not data:
            return 0.0, []
        assessments = self._to_assessments(
            market, active, data, provider=provider, model=model
        )
        best = max((assessment.abs_edge for assessment in assessments), default=0.0)
        logger.info(
            "Triage %s via %s/%s: best edge %.1f%%",
            market.event_name, provider, model, best * 100,
        )
        return best, assessments

    def deep_assess(self, market: BetfairMarket) -> list[BetfairAssessment]:
        self.paid_deep_used = False
        active = [
            runner for runner in market.runners
            if runner.status == RunnerStatus.ACTIVE and runner.best_back
        ][:self.max_runners]
        if not active:
            return []

        data, provider, model, text, searches, raw_response = self._call_stage(
            stage="deep",
            system=DEEP_SYSTEM,
            prompt=self._market_prompt(market, active),
            max_tokens=self.deep_max_tokens,
            grounded=True,
        )
        stop = getattr(raw_response, "stop_reason", None)

        if not data and text:
            logger.info(
                "Deep assess %s: no JSON (stop=%s); running salvage extraction call",
                market.event_name, stop,
            )
            data, salvage_provider, salvage_model = self._salvage_json(
                market, active, text
            )
            if data:
                provider = f"{provider}+{salvage_provider}"
                model = f"{model}+{salvage_model}"

        if not data or "probabilities" not in data:
            logger.error(
                "Deep assess for %s (%s) returned no parseable probabilities "
                "after %s search(es) (stop_reason=%s). Response head: %r",
                market.market_id, market.event_name, searches, stop, text[:300],
            )
            return []

        logger.info(
            "Deep assess %s via %s/%s: %s search(es), parsed OK",
            market.event_name, provider, model, searches,
        )
        return self._to_assessments(
            market, active, data, provider=provider, model=model
        )

    def _salvage_json(self, market, active, findings_text):
        """Convert a deep response's prose findings into the required JSON."""
        ids = ", ".join(str(runner.selection_id) for runner in active)
        prompt = (
            "Below are research findings for a betting market. Convert them into "
            "the required JSON ONLY; no other text.\n\n"
            f"Selection IDs to include: {ids}\n\n"
            f"FINDINGS:\n{findings_text[:6000]}\n\n"
            'Reply with ONLY: {"probabilities": {"<selection_id>": 0.XX, ...}, '
            '"confidence": 0.XX, "reasoning": "brief"}; probabilities must sum to 1.0.'
        )
        data, provider, model, _, _, _ = self._call_stage(
            stage="triage",
            system="Convert supplied research into the requested JSON only.",
            prompt=prompt,
            max_tokens=self.max_tokens,
            grounded=False,
        )
        return data, provider, model
