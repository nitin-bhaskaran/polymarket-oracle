"""
Betfair API-NG client — interactive login + JSON-RPC betting calls.

Auth flow (interactive, no client certificate — simplest for personal use):
  POST https://identitysso.betfair.com/api/login
    headers: X-Application: <app_key>, Accept: application/json
    form:    username, password
  -> { "token": <session>, "status": "SUCCESS" }

All betting calls go to the JSON-RPC endpoint:
  POST https://api.betfair.com/exchange/betting/json-rpc/v1
    headers: X-Application: <app_key>, X-Authentication: <session>, content-type json

Betfair recommends the non-interactive (certificate) login for unattended bots.
We start with interactive for validation; a cert-based login can be added later
without changing the call layer. The session token is kept alive and re-fetched
on expiry.

Using the DELAYED application key, listMarketBook returns delayed prices and no
matched-volume data — fine for validation, and the safe (pessimistic) direction
for any in-play results.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("betfair.client")

IDENTITY_URL = "https://identitysso.betfair.com/api/login"
KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"
LOGOUT_URL = "https://identitysso.betfair.com/api/logout"
BETTING_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ACCOUNT_URL = "https://api.betfair.com/exchange/account/json-rpc/v1"


class BetfairError(Exception):
    pass


class BetfairClient:
    def __init__(self, config: dict):
        bf = config.get("betfair", {})
        self.app_key = bf.get("app_key", "")
        self.username = bf.get("username", "")
        self.password = bf.get("password", "")
        self.locale = bf.get("locale", "en")
        self.currency = bf.get("currency", "GBP")

        self.session_token: Optional[str] = None
        self._client = httpx.Client(timeout=30.0)
        self._rpc_id = 0

    # ── Auth ──

    def login(self) -> bool:
        """Interactive login → session token. Returns True on success."""
        if not (self.app_key and self.username and self.password):
            logger.error("Betfair credentials missing (app_key/username/password)")
            return False
        try:
            resp = self._client.post(
                IDENTITY_URL,
                headers={
                    "X-Application": self.app_key,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"username": self.username, "password": self.password},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "SUCCESS":
                logger.error(f"Betfair login failed: {data.get('status')} / {data.get('error')}")
                return False
            self.session_token = data["token"]
            logger.info("Betfair login successful")
            return True
        except Exception as e:
            logger.error(f"Betfair login error: {e}")
            return False

    def keep_alive(self) -> bool:
        if not self.session_token:
            return False
        try:
            resp = self._client.post(
                KEEPALIVE_URL,
                headers={
                    "X-Application": self.app_key,
                    "X-Authentication": self.session_token,
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json().get("status") == "SUCCESS"
        except Exception as e:
            logger.warning(f"Betfair keepAlive failed: {e}")
            return False

    def ensure_session(self) -> bool:
        """Ensure we have a live session, logging in if needed."""
        if self.session_token and self.keep_alive():
            return True
        return self.login()

    # ── JSON-RPC ──

    def _rpc(self, method: str, params: dict, endpoint: str = BETTING_URL) -> dict:
        """
        Make a JSON-RPC call. method is e.g. 'SportsAPING/v1.0/listMarketCatalogue'.
        Returns the 'result' payload. Raises BetfairError on RPC error.
        """
        if not self.session_token:
            if not self.login():
                raise BetfairError("Not authenticated")

        self._rpc_id += 1
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._rpc_id,
        }
        resp = self._client.post(
            endpoint,
            headers={
                "X-Application": self.app_key,
                "X-Authentication": self.session_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise BetfairError(f"RPC error on {method}: {data['error']}")
        return data.get("result", {})

    # ── Betting calls ──

    def list_event_types(self) -> list[dict]:
        """List sport groups (Soccer=1, Tennis=2, ... Politics=2378961)."""
        return self._rpc(
            "SportsAPING/v1.0/listEventTypes",
            {"filter": {}},
        )

    def list_market_catalogue(self, filter_: dict, max_results: int = 100,
                              market_projection: Optional[list] = None,
                              sort: str = "MAXIMUM_TRADED") -> list[dict]:
        """
        Find markets matching a filter. market_projection controls what's
        returned (EVENT, COMPETITION, MARKET_START_TIME, RUNNER_DESCRIPTION, ...).
        """
        if market_projection is None:
            market_projection = [
                "EVENT", "COMPETITION", "MARKET_START_TIME",
                "RUNNER_DESCRIPTION", "MARKET_DESCRIPTION",
            ]
        return self._rpc(
            "SportsAPING/v1.0/listMarketCatalogue",
            {
                "filter": filter_,
                "maxResults": max_results,
                "marketProjection": market_projection,
                "sort": sort,
                "locale": self.locale,
            },
        )

    def list_market_book(self, market_ids: list[str],
                         price_data: Optional[list] = None) -> list[dict]:
        """
        Get live (delayed, on the delayed key) prices/state for markets.

        Betfair weights each listMarketBook request; requesting rich price data
        across many markets trips TOO_MUCH_DATA. We therefore request only
        EX_BEST_OFFERS by default, cap best-offers depth to 1 rung (the touch),
        and rely on small batches (see scanner). EX_TRADED is omitted by default
        because it adds significant weight and we don't need traded ladders for
        pre-event value assessment.
        """
        if price_data is None:
            price_data = ["EX_BEST_OFFERS"]
        return self._rpc(
            "SportsAPING/v1.0/listMarketBook",
            {
                "marketIds": market_ids,
                "priceProjection": {
                    "priceData": price_data,
                    "exBestOffersOverrides": {
                        "bestPricesDepth": 1,
                        "rollupModel": "STAKE",
                        "rollupLimit": 0,
                    },
                    "virtualise": True,
                },
                "currencyCode": self.currency,
                "locale": self.locale,
            },
        )

    def close(self):
        self._client.close()
