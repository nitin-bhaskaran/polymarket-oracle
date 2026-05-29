"""
Probability Assessment — Uses Claude API to estimate true probabilities.

For each candidate market, this module:
1. Searches for recent news relevant to the market question
2. Sends the question + news context to Claude
3. Parses Claude's probability estimate and reasoning
4. Calculates edge vs. current market price
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic
import httpx

from core.models import Market, ProbabilityAssessment, Side

logger = logging.getLogger(__name__)


# System prompt for probability assessment
ASSESSMENT_SYSTEM_PROMPT = """You are a professional prediction market analyst. Your job is to estimate the TRUE probability of events, independent of what the market currently prices them at.

You will be given:
1. A YES/NO question from a prediction market
2. The market's description and resolution criteria
3. Recent news articles relevant to the question

Your task:
- Analyze all available evidence objectively
- Estimate the probability that the answer is YES
- Be calibrated: when you say 70%, events should happen ~70% of the time
- Consider base rates, historical precedents, and current evidence
- Account for uncertainty — extreme probabilities (>95% or <5%) should be rare
- Be aware of your knowledge cutoff and factor in what you might not know

RESPOND IN EXACTLY THIS JSON FORMAT (no other text):
{
    "probability": 0.XX,
    "confidence": 0.XX,
    "reasoning": "1-2 sentence explanation",
    "key_factors": ["factor 1", "factor 2", "factor 3"]
}

Where:
- probability: Your estimate that YES is correct (0.0 to 1.0)
- confidence: How confident you are in your estimate (0.0 to 1.0)
- reasoning: Brief explanation of your logic
- key_factors: 2-4 most important factors in your assessment"""


class ProbabilityEngine:
    """
    Estimates true probabilities for prediction market questions
    using Claude API + news context.
    """
    
    def __init__(self, config: dict):
        api_key = config.get("anthropic", {}).get("api_key", "")
        self.model = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")
        self.max_tokens = config.get("anthropic", {}).get("max_tokens", 1024)
        self.include_market_price = config.get("anthropic", {}).get(
            "include_market_price_in_prompt", False
        )
        news_config = config.get("news", {})
        self.news_enabled = news_config.get("enabled", True)
        self.news_provider = news_config.get("provider", "gdelt")
        self.news_max_articles = news_config.get("max_articles", 5)
        self.news_timeout = news_config.get("timeout_seconds", 10.0)
        self.news_language = news_config.get("language", "english")
        
        if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY_HERE":
            raise ValueError("Anthropic API key not configured. Set it in config.yaml")
        
        self.client = anthropic.Anthropic(api_key=api_key)
        self.news_client = httpx.Client(timeout=self.news_timeout)
    
    def assess_market(self, market: Market) -> Optional[ProbabilityAssessment]:
        """
        Generate a probability assessment for a single market.
        
        Steps:
        1. Fetch relevant news for context
        2. Build prompt with market question + news
        3. Call Claude API
        4. Parse response and calculate edge
        
        Returns ProbabilityAssessment or None if assessment fails.
        """
        logger.info(f"Assessing: {market.question}")
        
        try:
            # Step 1: Gather news context
            news_context = self._fetch_news_context(market)
            
            # Step 2: Build the assessment prompt
            user_prompt = self._build_prompt(market, news_context)
            
            # Step 3: Call Claude
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=ASSESSMENT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}]
            )
            
            # Step 4: Parse response
            response_text = response.content[0].text.strip()
            assessment = self._parse_response(response_text, market)
            
            if assessment:
                logger.info(
                    f"Assessment: {market.question} | "
                    f"AI: {assessment.estimated_probability:.1%} | "
                    f"Market: {market.yes_price:.1%} | "
                    f"Edge: {assessment.abs_edge:.1%} | "
                    f"Side: {assessment.recommended_side}"
                )
            
            return assessment
            
        except anthropic.APIError as e:
            logger.error(f"Claude API error assessing {market.question}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error assessing {market.question}: {e}")
            return None
    
    def _fetch_news_context(self, market: Market) -> str:
        """
        Fetch recent news relevant to the market question.

        The default source is GDELT's public document API because it does
        not require a key. Failures fall back to market context rather than
        blocking the trading loop.
        """
        base_context = (
            f"Event context: {market.event_title}\n"
            f"Question: {market.question}\n"
            f"Resolution criteria: {market.description or 'Not provided'}"
        )
        if not self.news_enabled:
            return base_context
        if self.news_provider.lower() != "gdelt":
            logger.warning(f"Unsupported news provider '{self.news_provider}', using base context")
            return base_context

        try:
            articles = self._fetch_gdelt_articles(market)
        except Exception as e:
            logger.warning(f"News fetch failed for {market.question}: {e}")
            return base_context

        if not articles:
            return base_context + "\nRecent news: No relevant articles found."

        lines = ["Recent news:"]
        for article in articles[: self.news_max_articles]:
            title = article.get("title") or "Untitled"
            source = article.get("sourceCountry") or article.get("domain") or "unknown source"
            date = article.get("seendate") or article.get("datetime") or "unknown date"
            url = article.get("url") or ""
            lines.append(f"- {title} ({source}, {date}) {url}".strip())

        return base_context + "\n" + "\n".join(lines)

    def _fetch_gdelt_articles(self, market: Market) -> list[dict]:
        query = self._build_news_query(market)
        response = self.news_client.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "sort": "hybridrel",
                "maxrecords": self.news_max_articles,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data.get("articles", []) or []

    def _build_news_query(self, market: Market) -> str:
        terms = []
        for text in (market.event_title, market.question):
            cleaned = self._clean_news_query(text)
            if cleaned:
                terms.append(f'"{cleaned}"')
        if not terms:
            terms.append(f'"{market.slug}"')
        return f"({' OR '.join(terms[:2])}) sourcelang:{self.news_language}"

    @staticmethod
    def _clean_news_query(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        text = re.sub(r"^(will|can|does|did|is|are)\s+", "", text, flags=re.IGNORECASE)
        text = text.strip(" ?.")
        return text[:160]
    
    def _build_prompt(self, market: Market, news_context: str) -> str:
        """Build the full assessment prompt for Claude."""
        
        # Calculate time remaining
        time_info = ""
        if market.end_date:
            hours = market.hours_to_expiry
            if hours < 24:
                time_info = f"This market closes in {hours:.0f} hours."
            elif hours < 720:
                time_info = f"This market closes in {hours/24:.0f} days."
            else:
                time_info = f"This market closes in {hours/720:.0f} months."
        
        market_price_context = ""
        if self.include_market_price:
            market_price_context = (
                f"- Current YES price: {market.yes_price:.2f} "
                f"(market implies {market.yes_price:.0%} probability)\n"
                f"- Current NO price: {market.no_price:.2f}\n"
            )

        prompt = f"""PREDICTION MARKET QUESTION:
{market.question}

MARKET DESCRIPTION:
{market.description or 'No additional description provided.'}

EVENT CONTEXT:
{market.event_title}

{time_info}

MARKET DATA:
{market_price_context}\
- 24h Volume: ${market.volume_24h:,.0f}
- Total Liquidity: ${market.liquidity:,.0f}

NEWS & CONTEXT:
{news_context}

TODAY'S DATE: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Based on all available information, what is the TRUE probability that the answer is YES?
Remember: respond ONLY with the JSON format specified."""

        return prompt
    
    def _parse_response(self, response_text: str, market: Market) -> Optional[ProbabilityAssessment]:
        """
        Parse Claude's JSON response into a ProbabilityAssessment.
        
        Handles cases where Claude wraps JSON in markdown code blocks
        or adds extra text.
        """
        try:
            # Clean up response — remove markdown code blocks if present
            text = response_text.strip()
            if text.startswith("```"):
                # Remove first and last lines (code block markers)
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])
            
            data = json.loads(text)
            
            # Validate probability is in range
            prob = float(data.get("probability", 0.5))
            prob = max(0.01, min(0.99, prob))  # Clamp to avoid extremes
            
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            
            assessment = ProbabilityAssessment(
                market_condition_id=market.condition_id,
                question=market.question,
                estimated_probability=prob,
                confidence=confidence,
                reasoning=data.get("reasoning", "No reasoning provided"),
                key_factors=data.get("key_factors", []),
                market_price=market.yes_price,
            )
            
            # Calculate edge
            assessment.calculate_edge()
            
            return assessment
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse Claude response: {e}\nResponse: {response_text[:200]}")
            return None
    
    def batch_assess(self, markets: list[Market]) -> list[ProbabilityAssessment]:
        """
        Assess multiple markets and return those with meaningful edge.
        
        Processes markets sequentially (Claude API is rate-limited anyway).
        """
        assessments = []
        
        for market in markets:
            assessment = self.assess_market(market)
            if assessment:
                assessments.append(assessment)
        
        # Sort by absolute edge, highest first
        assessments.sort(key=lambda a: a.abs_edge, reverse=True)
        
        logger.info(
            f"Assessed {len(markets)} markets, "
            f"found {len(assessments)} valid assessments"
        )
        
        return assessments
    
    def close(self):
        """Clean up resources."""
        self.news_client.close()
