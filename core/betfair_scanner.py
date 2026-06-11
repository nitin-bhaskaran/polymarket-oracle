"""
Betfair market scanner — discovers markets and builds BetfairMarket objects.

Flow:
  1. listMarketCatalogue with a filter (event types, in-play flag, time window)
     to find candidate markets and their runner metadata.
  2. listMarketBook for those market IDs to get current back/lay prices, depth,
     traded volume, and in-play status.
  3. Merge catalogue (names) + book (prices/state) into BetfairMarket models.

Scope is config-driven. By default we target ALL sports + politics, PRE-EVENT
only (in_play_enabled=false), because a slow reasoning loop cannot compete on
in-play speed. Setting in_play_enabled=true includes in-play markets — enabled
deliberately for the paper-trading validation, where the honest fill simulator
and delayed data make in-play results pessimistic-but-informative rather than
misleadingly rosy.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.betfair_client import BetfairClient
from core.betfair_models import (
    BetfairMarket, MarketPhase, PriceLevel, Runner, RunnerStatus,
)

logger = logging.getLogger("betfair.scanner")


class BetfairScanner:
    def __init__(self, config: dict, client: Optional[BetfairClient] = None):
        sc = config.get("scanner", {})
        self.client = client or BetfairClient(config)

        # Universe
        self.event_type_ids = sc.get("event_type_ids", [])  # empty => all
        self.in_play_enabled = sc.get("in_play_enabled", False)
        self.max_markets = sc.get("max_markets_per_scan", 20)
        self.catalogue_fetch = sc.get("catalogue_fetch", 200)
        self.book_batch_size = sc.get("book_batch_size", 10)
        self.min_total_matched = sc.get("min_total_matched", 1000.0)
        # Overround sanity band. A genuine, liquid win-market sits near 1.0
        # (roughly 0.85-1.20 depending on back/lay midpoint and book width).
        # Markets far outside this are either not win-markets (handicaps, lines,
        # totals — e.g. a Handicap showing overround 147) or too thin/wide to
        # price meaningfully (novelty/outright specials e.g. "Next James Bond"
        # at 15.7, a stage race at 5.4). The overround-adjustment maths assumes
        # implied probs sum to ~1, so these would yield huge phantom edges on
        # every runner. Exclude them before assessment.
        self.min_overround = sc.get("min_overround", 0.85)
        self.max_overround = sc.get("max_overround", 1.20)
        # Pre-event lookahead window (hours). Markets starting beyond this are
        # ignored as too far out to assess usefully.
        self.max_hours_ahead = sc.get("max_hours_ahead", 72.0)
        # Resolution-horizon ceiling: exclude markets that won't resolve within
        # this many days. A market resolving in 2028/2029 produces no settled
        # data in a multi-week validation window AND tends to show large,
        # unfalsifiable edges. Markets resolve ~when they start (matches) or at
        # the listed start time (elections/outrights), so marketStartTime is the
        # resolution proxy. This is the upper bound; min_hours_ahead is the lower.
        self.max_resolution_days = sc.get("max_resolution_days", 30.0)
        # Pre-event buffer: skip anything starting within this many hours, so the
        # model always assesses a stable, searchable pre-event state (not a
        # near-live or in-play market where its information is stale).
        self.min_hours_ahead = sc.get("min_hours_ahead", 3.0)
        # Market type codes to include. Empty list = all types (lets political
        # and novelty markets through, which don't use MATCH_ODDS). Default
        # covers the main sports win-markets plus common outright codes.
        self.market_type_codes = sc.get("market_type_codes", [])
        # Skip markets starting within this many minutes (too close / going live).
        self.default_commission = sc.get("commission_rate", 0.05)

        self._market_cache: dict[str, BetfairMarket] = {}

    def scan(self) -> list[BetfairMarket]:
        """Discover and price markets, returning the top ones by liquidity."""
        if not self.client.ensure_session():
            logger.error("No Betfair session; scan aborted")
            return []

        # 1. Build catalogue filter. Upper time bound = resolution horizon
        # (the widest we'd ever consider); precise lower/upper bounds applied
        # per-market below.
        now = datetime.now(timezone.utc)
        horizon_hours = max(self.max_hours_ahead, self.max_resolution_days * 24.0)
        market_filter = {
            "marketStartTime": {
                "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": (now + timedelta(hours=horizon_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }
        # Only constrain market types if configured; empty = all types (so
        # politics/specials aren't filtered out).
        if self.market_type_codes:
            market_filter["marketTypeCodes"] = self.market_type_codes
        if self.event_type_ids:
            market_filter["eventTypeIds"] = [str(e) for e in self.event_type_ids]
        if not self.in_play_enabled:
            market_filter["inPlayOnly"] = False
            market_filter["turnInPlayEnabled"] = True  # markets that CAN go in-play, but caught pre-event

        try:
            catalogue = self.client.list_market_catalogue(
                market_filter, max_results=self.catalogue_fetch, sort="MAXIMUM_TRADED"
            )
        except Exception as e:
            logger.error(f"listMarketCatalogue failed: {e}")
            return []

        logger.info(f"Catalogue returned {len(catalogue)} markets")
        if not catalogue:
            return []

        # Map id -> catalogue entry for name merging. The catalogue is sorted by
        # MAXIMUM_TRADED (most liquid first); we only need books for the top
        # slice we'll actually consider, not all of catalogue_fetch — fetching
        # books for 200 markets is slow and unnecessary.
        cat_by_id = {c["marketId"]: c for c in catalogue if "marketId" in c}
        book_limit = max(self.max_markets * 3, self.book_batch_size)
        market_ids = list(cat_by_id.keys())[:book_limit]

        # 2. Fetch books in batches. Betfair weights listMarketBook by data
        # requested; even best-offers trips TOO_MUCH_DATA above ~10 markets, so
        # batch small. Configurable via scanner.book_batch_size.
        books: dict[str, dict] = {}
        BATCH = self.book_batch_size
        for i in range(0, len(market_ids), BATCH):
            chunk = market_ids[i:i + BATCH]
            try:
                for book in self.client.list_market_book(chunk):
                    books[book["marketId"]] = book
            except Exception as e:
                logger.warning(f"listMarketBook batch failed: {e}")

        # 3. Merge into BetfairMarket models
        markets: list[BetfairMarket] = []
        for mid, cat in cat_by_id.items():
            book = books.get(mid)
            if not book:
                continue
            market = self._build_market(cat, book)
            if market is None:
                continue
            # Filters
            if market.total_matched < self.min_total_matched:
                continue
            # Overround sanity: drop markets whose book doesn't behave like a
            # win-market (handicaps/lines/totals, or ultra-thin specials). These
            # would produce phantom edges under overround adjustment.
            ovr = market.overround
            if ovr < self.min_overround or ovr > self.max_overround:
                logger.debug(f"Skipping {market.market_id} ({market.market_name}): "
                             f"overround {ovr:.2f} outside "
                             f"[{self.min_overround}, {self.max_overround}]")
                continue
            if not self.in_play_enabled and market.in_play:
                continue
            if market.phase == MarketPhase.PRE_EVENT:
                if market.hours_to_start < self.min_hours_ahead:
                    continue
                # Exclude markets resolving beyond the validation horizon — they
                # produce no settled data in-window and tend to show
                # unfalsifiable edges.
                if market.hours_to_start > self.max_resolution_days * 24.0:
                    continue
            markets.append(market)

        markets.sort(key=lambda m: m.total_matched, reverse=True)
        result = markets[:self.max_markets]
        self._market_cache = {m.market_id: m for m in markets}
        logger.info(f"Built {len(markets)} markets, returning top {len(result)}")
        return result

    def _build_market(self, cat: dict, book: dict) -> Optional[BetfairMarket]:
        try:
            event = cat.get("event", {}) or {}
            comp = cat.get("competition", {}) or {}
            desc = cat.get("description", {}) or {}

            start_str = cat.get("marketStartTime") or event.get("openDate")
            start_time = None
            if start_str:
                start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))

            in_play = bool(book.get("inplay", False))
            status = book.get("status", "OPEN")
            if status == "SETTLED":
                phase = MarketPhase.SETTLED
            elif status == "CLOSED":
                phase = MarketPhase.CLOSED
            elif in_play:
                phase = MarketPhase.IN_PLAY
            else:
                phase = MarketPhase.PRE_EVENT

            # Runner metadata (names) from catalogue
            names = {r["selectionId"]: r.get("runnerName", "")
                     for r in cat.get("runners", [])}

            runners = []
            for rb in book.get("runners", []):
                sid = rb.get("selectionId")
                ex = rb.get("ex", {}) or {}
                backs = [PriceLevel(price=p["price"], size=p["size"])
                         for p in ex.get("availableToBack", [])]
                lays = [PriceLevel(price=p["price"], size=p["size"])
                        for p in ex.get("availableToLay", [])]
                rstatus = rb.get("status", "ACTIVE")
                try:
                    rstatus_enum = RunnerStatus(rstatus)
                except ValueError:
                    rstatus_enum = RunnerStatus.ACTIVE
                runners.append(Runner(
                    selection_id=sid,
                    name=names.get(sid, str(sid)),
                    status=rstatus_enum,
                    available_to_back=backs,
                    available_to_lay=lays,
                    last_price_traded=rb.get("lastPriceTraded"),
                    total_matched=rb.get("totalMatched", 0.0) or 0.0,
                ))

            return BetfairMarket(
                market_id=cat["marketId"],
                event_id=event.get("id", ""),
                event_name=event.get("name", ""),
                market_name=cat.get("marketName", ""),
                competition=comp.get("name", ""),
                domain=(cat.get("eventType") or {}).get("name", "")
                       or str(event.get("eventTypeId", "")),
                sport=desc.get("marketType", "") or event.get("eventTypeId", ""),
                start_time=start_time,
                phase=phase,
                in_play=in_play,
                total_matched=book.get("totalMatched", 0.0) or 0.0,
                commission_rate=self.default_commission,
                runners=runners,
            )
        except Exception as e:
            logger.warning(f"Failed to build market {cat.get('marketId')}: {e}")
            return None

    def get_market(self, market_id: str) -> Optional[BetfairMarket]:
        return self._market_cache.get(market_id)

    def refresh_book(self, market_id: str) -> Optional[BetfairMarket]:
        """Re-fetch a single market's book (for position monitoring / passive fills)."""
        try:
            books = self.client.list_market_book([market_id])
            if not books:
                return None
            cached = self._market_cache.get(market_id)
            cat = {"marketId": market_id, "marketName": cached.market_name if cached else "",
                   "event": {"name": cached.event_name if cached else ""},
                   "runners": [{"selectionId": r.selection_id, "runnerName": r.name}
                               for r in (cached.runners if cached else [])]}
            market = self._build_market(cat, books[0])
            if market:
                self._market_cache[market_id] = market
            return market
        except Exception as e:
            logger.warning(f"refresh_book failed for {market_id}: {e}")
            return None
