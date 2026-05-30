"""Tests for the Betfair scanner's catalogue+book merge logic (mocked client)."""

from datetime import datetime, timedelta, timezone

from core.betfair_scanner import BetfairScanner
from core.betfair_models import MarketPhase, RunnerStatus


class FakeClient:
    """Stand-in for BetfairClient returning canned catalogue/book data."""
    def __init__(self, catalogue, books):
        self._catalogue = catalogue
        self._books = books

    def ensure_session(self):
        return True

    def list_market_catalogue(self, *a, **k):
        return self._catalogue

    def list_market_book(self, market_ids, *a, **k):
        return [b for b in self._books if b["marketId"] in market_ids]


def _start(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_scanner(in_play_enabled=False, min_matched=1000.0):
    cat = [{
        "marketId": "1.100",
        "marketName": "Match Odds",
        "marketStartTime": _start(5),
        "event": {"id": "e1", "name": "Home v Away"},
        "competition": {"name": "Premier League"},
        "description": {"marketType": "MATCH_ODDS"},
        "runners": [
            {"selectionId": 11, "runnerName": "Home"},
            {"selectionId": 12, "runnerName": "Away"},
            {"selectionId": 13, "runnerName": "Draw"},
        ],
    }]
    books = [{
        "marketId": "1.100",
        "status": "OPEN",
        "inplay": False,
        "totalMatched": 50000.0,
        "runners": [
            {"selectionId": 11, "status": "ACTIVE", "lastPriceTraded": 2.0,
             "totalMatched": 20000,
             "ex": {"availableToBack": [{"price": 2.0, "size": 500}],
                    "availableToLay": [{"price": 2.04, "size": 500}]}},
            {"selectionId": 12, "status": "ACTIVE", "lastPriceTraded": 4.0,
             "totalMatched": 15000,
             "ex": {"availableToBack": [{"price": 4.0, "size": 300}],
                    "availableToLay": [{"price": 4.2, "size": 300}]}},
            {"selectionId": 13, "status": "ACTIVE", "lastPriceTraded": 3.5,
             "totalMatched": 15000,
             "ex": {"availableToBack": [{"price": 3.4, "size": 300}],
                    "availableToLay": [{"price": 3.6, "size": 300}]}},
        ],
    }]
    config = {"scanner": {"in_play_enabled": in_play_enabled,
                          "min_total_matched": min_matched,
                          "max_markets_per_scan": 20}}
    return BetfairScanner(config, client=FakeClient(cat, books))


def test_scan_builds_market_with_runners():
    s = make_scanner()
    markets = s.scan()
    assert len(markets) == 1
    m = markets[0]
    assert m.market_id == "1.100"
    assert m.event_name == "Home v Away"
    assert m.competition == "Premier League"
    assert len(m.runners) == 3
    assert m.phase == MarketPhase.PRE_EVENT


def test_scan_prices_and_overround():
    s = make_scanner()
    m = s.scan()[0]
    home = m.runners[0]
    assert home.best_back == 2.0
    assert home.best_lay == 2.04
    assert m.overround > 1.0
    # fair probs sum to 1
    total = sum(m.fair_implied_prob(r) for r in m.runners)
    assert abs(total - 1.0) < 1e-9


def test_scan_filters_low_liquidity():
    s = make_scanner(min_matched=100000.0)  # above the 50k matched
    assert s.scan() == []


def test_get_market_from_cache():
    s = make_scanner()
    s.scan()
    assert s.get_market("1.100") is not None
    assert s.get_market("nope") is None


def test_in_play_excluded_by_default():
    s = make_scanner(in_play_enabled=False)
    # flip the book to in-play
    s.client._books[0]["inplay"] = True
    assert s.scan() == []


def test_in_play_included_when_enabled():
    s = make_scanner(in_play_enabled=True)
    s.client._books[0]["inplay"] = True
    markets = s.scan()
    assert len(markets) == 1
    assert markets[0].phase == MarketPhase.IN_PLAY


def test_excludes_markets_beyond_resolution_horizon():
    """A market resolving far in the future (e.g. 2028/29 politics) is dropped."""
    s = make_scanner()
    # Start time ~2 years out, well beyond the 30-day default horizon.
    s.client._catalogue[0]["marketStartTime"] = _start(24 * 365 * 2)
    assert s.scan() == []


def test_keeps_near_term_markets_within_horizon():
    """A market resolving in ~3 weeks (e.g. a by-election) is kept."""
    s = make_scanner()
    s.client._catalogue[0]["marketStartTime"] = _start(24 * 21)  # 21 days
    assert len(s.scan()) == 1
