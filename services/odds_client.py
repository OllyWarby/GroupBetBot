"""Thin async client for The Odds API (the-odds-api.com) - free tier used
for pre-match head-to-head (match winner) prices.

Coverage caveat: the free tier is confirmed for EPL and EFL Championship,
but League One/Two coverage is NOT confirmed, and the provider doesn't
publish stable sport_key names for every competition. Rather than hardcode
guessed keys, this looks up the sport list by title (see
config.ODDS_LEAGUE_TITLE_HINTS) and returns None - treated by callers as
"odds unavailable" - when a league or fixture can't be matched.

All provider-specific knowledge lives in this one file on purpose, so
swapping to a different odds provider later is a one-file change, same
pattern as services/espn_client.py.
"""

import asyncio
import logging

import aiohttp

from config import (
    ODDS_ENABLED,
    ODDS_API_KEY,
    ODDS_API_BASE_URL,
    ODDS_REGIONS,
    ODDS_MARKET,
    ODDS_FORMAT,
    ODDS_LEAGUE_TITLE_HINTS,
)

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MAX_RETRIES = 1
_RETRY_BACKOFF_SECONDS = 2


class OddsClientError(Exception):
    pass


def _normalize(name: str) -> str:
    return name.strip().lower()


class OddsClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        # league_key -> sport_key, or None if we've already checked and
        # this provider doesn't cover that league. Cached for the process
        # lifetime since the sport list rarely changes.
        self._sport_key_cache: dict[str, str | None] = {}
        self._sports_list_cache: list[dict] | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict):
        if not ODDS_API_KEY:
            raise OddsClientError("ODDS_API_KEY is not set")

        session = await self._get_session()
        url = f"{ODDS_API_BASE_URL}{path}"
        request_params = {**params, "apiKey": ODDS_API_KEY}
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with session.get(url, params=request_params) as resp:
                    if resp.status == 401:
                        raise OddsClientError("Odds API rejected the API key (401)")
                    if resp.status == 429:
                        raise OddsClientError("Odds API rate/credit limit hit (429)")
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, OddsClientError, asyncio.TimeoutError) as e:
                last_error = e
                logger.warning(
                    "Odds API request failed (attempt %d/%d) %s: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    url,
                    e,
                )
                if attempt < _MAX_RETRIES and not isinstance(e, OddsClientError):
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                elif isinstance(e, OddsClientError):
                    break  # no point retrying a bad key / quota hit
        raise OddsClientError(f"Failed to fetch {path}: {last_error}")

    async def _get_sports_list(self) -> list[dict]:
        if self._sports_list_cache is None:
            self._sports_list_cache = await self._get("/sports", {})
        return self._sports_list_cache

    async def _sport_key_for_league(self, league_key: str) -> str | None:
        if league_key in self._sport_key_cache:
            return self._sport_key_cache[league_key]

        hints = ODDS_LEAGUE_TITLE_HINTS.get(league_key, ())
        sport_key = None
        try:
            sports = await self._get_sports_list()
            for sport in sports:
                title = _normalize(sport.get("title", ""))
                if sport.get("group") == "Soccer" and any(hint in title for hint in hints):
                    sport_key = sport.get("key")
                    break
        except OddsClientError as e:
            logger.warning("Could not fetch odds sports list: %s", e)

        self._sport_key_cache[league_key] = sport_key
        return sport_key

    async def get_best_price(self, league_key: str, team_name: str) -> tuple[float, str] | None:
        """Best (highest) decimal price for `team_name` to win their next
        fixture, and the bookmaker offering it.

        Returns None if odds are disabled (ODDS_ENABLED=false), this
        provider doesn't cover the league, the fixture/outcome can't be
        matched, or the request fails - callers should treat all of that
        as "odds unavailable", not an error.
        """
        if not ODDS_ENABLED:
            return None

        sport_key = await self._sport_key_for_league(league_key)
        if sport_key is None:
            return None

        try:
            events = await self._get(
                f"/sports/{sport_key}/odds",
                {"regions": ODDS_REGIONS, "markets": ODDS_MARKET, "oddsFormat": ODDS_FORMAT},
            )
        except OddsClientError as e:
            logger.warning("Could not fetch odds for %s: %s", sport_key, e)
            return None

        target = _normalize(team_name)
        for event in events:
            home = _normalize(event.get("home_team", ""))
            away = _normalize(event.get("away_team", ""))
            if target != home and target != away:
                continue

            best_price = None
            best_bookmaker = None
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != ODDS_MARKET:
                        continue
                    for outcome in market.get("outcomes", []):
                        if _normalize(outcome.get("name", "")) != target:
                            continue
                        price = outcome.get("price")
                        if price is not None and (best_price is None or price > best_price):
                            best_price = price
                            best_bookmaker = bookmaker.get("title", "")
            return (best_price, best_bookmaker) if best_price is not None else None

        return None  # no fixture found for this team on the current odds board
