"""
Market Scanner — Fetches and filters markets from Polymarket's Gamma API.

The Gamma API provides market metadata: titles, descriptions, outcomes,
token IDs, prices, volume, and liquidity. No authentication needed for reads.
"""

import logging
import json
from datetime import datetime
from typing import Optional

import httpx

from core.models import Market, MarketStatus

logger = logging.getLogger(__name__)


class MarketScanner:
    """
    Scans Polymarket for tradeable markets using the Gamma API.
    
    The Gamma API (gamma-api.polymarket.com) is the read-only market
    metadata API. It returns events (groups of related markets) and
    individual markets with their current prices and volumes.
    """
    
    def __init__(self, config: dict):
        self.gamma_url = config.get("polymarket", {}).get(
            "gamma_url", "https://gamma-api.polymarket.com"
        )
        self.clob_url = config.get("polymarket", {}).get(
            "clob_url", "https://clob.polymarket.com"
        )
        self.min_liquidity = config.get("risk", {}).get("min_liquidity", 5000.0)
        self.min_volume_24h = config.get("risk", {}).get("min_volume_24h", 1000.0)
        self.min_hours_to_expiry = config.get("risk", {}).get("min_hours_to_expiry", 2)
        self.max_markets = config.get("scanner", {}).get("max_markets_per_scan", 20)
        self.include_categories = config.get("scanner", {}).get("include_categories", [])
        self.exclude_categories = config.get("scanner", {}).get("exclude_categories", [])
        
        # HTTP client with timeout and retry
        self.client = httpx.Client(timeout=30.0)
    
    def fetch_active_events(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """
        Fetch active events from Gamma API, ordered by 24h volume.
        
        Events contain multiple related markets. For example, 
        "US Presidential Election" contains markets for each candidate.
        
        Returns raw JSON list of event objects.
        """
        try:
            response = self.client.get(
                f"{self.gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                    "order": "volume_24hr",
                    "ascending": "false",
                }
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch events from Gamma API: {e}")
            return []
    
    def fetch_market_by_id(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by its condition ID."""
        try:
            response = self.client.get(
                f"{self.gamma_url}/markets",
                params={"condition_id": condition_id}
            )
            response.raise_for_status()
            data = response.json()
            return data[0] if data else None
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch market {condition_id}: {e}")
            return None
    
    @staticmethod
    def _parse_json_list(value) -> list:
        """Gamma sometimes returns list-like fields as JSON strings."""
        if isinstance(value, list):
            return value
        if not value:
            return []
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []

    @staticmethod
    def _as_float(value, default: float = 0.0) -> float:
        try:
            return float(value or default)
        except (TypeError, ValueError):
            return default

    def parse_market(self, raw_market: dict, event_context: dict = None) -> Optional[Market]:
        """
        Parse a raw Gamma API market object into our Market model.
        
        The Gamma API returns markets with fields like:
        - clobTokenIds: JSON string like '["token_yes_id", "token_no_id"]'
        - outcomePrices: JSON string like '["0.65", "0.35"]'
        - outcomes: JSON string like '["Yes", "No"]'
        """
        try:
            # Extract token IDs for YES and NO
            clob_token_ids = self._parse_json_list(raw_market.get("clobTokenIds", []))
            if len(clob_token_ids) < 2:
                return None
            
            # Extract current prices
            outcome_prices = self._parse_json_list(raw_market.get("outcomePrices", []))
            yes_price = self._as_float(outcome_prices[0]) if outcome_prices else 0.0
            no_price = self._as_float(outcome_prices[1]) if len(outcome_prices) > 1 else 1.0 - yes_price
            
            # Parse end date
            end_date = None
            end_date_str = raw_market.get("endDate")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
            
            # Determine status
            status = MarketStatus.ACTIVE
            if not raw_market.get("active", True):
                status = MarketStatus.CLOSED
            if raw_market.get("closed"):
                status = MarketStatus.CLOSED
            if raw_market.get("resolved"):
                status = MarketStatus.RESOLVED
            
            # Build Market object
            market = Market(
                condition_id=raw_market.get("conditionId", ""),
                question=raw_market.get("question", ""),
                slug=raw_market.get("slug", ""),
                market_id=raw_market.get("id", ""),
                yes_token_id=clob_token_ids[0],
                no_token_id=clob_token_ids[1],
                yes_price=yes_price,
                no_price=no_price,
                description=raw_market.get("description", ""),
                category=(
                    raw_market.get("category")
                    or raw_market.get("groupItemTitle", "")
                    or (event_context or {}).get("category", "")
                ),
                end_date=end_date,
                liquidity=self._as_float(raw_market.get("liquidityNum", raw_market.get("liquidity", 0))),
                volume_24h=self._as_float(raw_market.get("volume24hr", raw_market.get("volume_24hr", 0))),
                volume_total=self._as_float(raw_market.get("volumeNum", raw_market.get("volume", 0))),
                status=status,
                event_title=event_context.get("title", "") if event_context else "",
                event_slug=event_context.get("slug", "") if event_context else "",
            )
            
            return market
            
        except Exception as e:
            logger.warning(f"Failed to parse market: {e}")
            return None
    
    def scan(self) -> list[Market]:
        """
        Main scan method — fetches all active markets and applies filters.
        
        Returns a list of Market objects that pass all filter criteria,
        sorted by 24h volume (highest first), capped at max_markets.
        """
        logger.info("Starting market scan...")
        
        all_markets: list[Market] = []
        
        # Fetch events (which contain their child markets)
        # Paginate through to get a broad set
        for offset in range(0, 200, 50):
            events = self.fetch_active_events(limit=50, offset=offset)
            if not events:
                break
            
            for event in events:
                event_markets = event.get("markets", [])
                for raw_market in event_markets:
                    market = self.parse_market(raw_market, event_context=event)
                    if market:
                        all_markets.append(market)
        
        logger.info(f"Fetched {len(all_markets)} total markets")
        
        # Apply filters
        filtered = self._apply_filters(all_markets)
        logger.info(f"After filtering: {len(filtered)} tradeable markets")
        
        # Sort by volume and cap
        filtered.sort(key=lambda m: m.volume_24h, reverse=True)
        return filtered[:self.max_markets]
    
    def _apply_filters(self, markets: list[Market]) -> list[Market]:
        """
        Apply all filter criteria to the market list.
        
        Filters:
        1. Must be active (not closed/resolved)
        2. Must have sufficient liquidity
        3. Must have sufficient 24h volume
        4. Must not be expiring too soon
        5. Price must not be too extreme (>0.95 or <0.05)
        6. Category filters (include/exclude)
        """
        filtered = []
        
        for market in markets:
            # Must be active
            if market.status != MarketStatus.ACTIVE:
                continue
            
            # Liquidity check
            if market.liquidity < self.min_liquidity:
                continue
            
            # Volume check
            if market.volume_24h < self.min_volume_24h:
                continue
            
            # Expiry check
            if market.hours_to_expiry < self.min_hours_to_expiry:
                continue
            
            # Price extremes — no edge in near-certain outcomes
            if market.yes_price > 0.95 or market.yes_price < 0.05:
                continue
            
            # Category filters
            if self.include_categories:
                if not market.category:
                    continue
                if market.category.lower() not in [c.lower() for c in self.include_categories]:
                    continue
            if self.exclude_categories and market.category:
                if market.category.lower() in [c.lower() for c in self.exclude_categories]:
                    continue
            
            filtered.append(market)
        
        return filtered
    
    def get_current_price(self, token_id: str) -> Optional[float]:
        """
        Get the current midpoint price for a specific token.
        
        Uses the CLOB API endpoint for real-time price.
        """
        try:
            response = self.client.get(
                f"{self.clob_url}/midpoint",
                params={"token_id": token_id}
            )
            response.raise_for_status()
            data = response.json()
            return float(data.get("mid", 0))
        except Exception as e:
            logger.warning(f"Failed to get price for {token_id}: {e}")
            return None
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()


# CLI mode — list current markets
if __name__ == "__main__":
    import yaml
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    # Load config if available, otherwise use defaults
    config = {}
    try:
        with open("config/config.yaml") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.info("No config.yaml found, using defaults")
    
    scanner = MarketScanner(config)
    markets = scanner.scan()
    
    logger.info("%s", "=" * 80)
    logger.info("Found %s tradeable markets", len(markets))
    logger.info("%s", "=" * 80)

    for i, m in enumerate(markets, 1):
        logger.info(
            "%3d. [%s] %s | Event: %s | Liquidity: $%.0f | 24h Vol: $%.0f | Expires: %s",
            i,
            f"{m.yes_price:.0%}",
            m.question,
            m.event_title,
            m.liquidity,
            m.volume_24h,
            m.end_date or "N/A",
        )
    
    scanner.close()
