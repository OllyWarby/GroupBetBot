"""Thin async client for ESPN's public (unofficial) soccer site API.

This is NOT an officially documented or supported ESPN product — it's the
same JSON API that powers espn.com, reverse-engineered by the developer
community. It's free, needs no API key, and has no published daily cap,
but there's no SLA: it can change shape or go down without notice.

All ESPN-specific knowledge (URLs, response shape) lives in this one file
on purpose, so if the provider ever needs to change, this is the only
file that has to.
"""

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Be a polite, cache-friendly citizen of an API we don't have a contract
# with: short timeout, small retry budget, no hammering on failure.
_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 3


class EspnClientError(Exception):
    pass


class EspnClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, url: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        raise EspnClientError(f"Rate limited (429) on {url}")
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, EspnClientError, asyncio.TimeoutError) as e:
                last_error = e
                logger.warning(
                    "ESPN request failed (attempt %d/%d) %s: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    url,
                    e,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        raise EspnClientError(f"Failed to fetch {url}: {last_error}")

    async def get_scoreboard(self, league_slug: str, date: str | None = None) -> dict:
        """Today's (or `date`, format YYYYMMDD) fixtures for a league.

        Returns the raw ESPN payload; `data["events"]` is the list of
        fixtures, each with a `status` and `competitions[0].competitors`
        (home/away teams + scores).
        """
        url = f"{BASE_URL}/{league_slug}/scoreboard"
        params = {"dates": date} if date else None
        return await self._get(url, params=params)

    async def get_summary(self, league_slug: str, event_id: str) -> dict:
        """Full match detail for one fixture, including the events/incidents
        feed (goals, cards, subs) under `data["details"]` (may vary by
        match/competition, so callers should defensively check for keys).
        """
        url = f"{BASE_URL}/{league_slug}/summary"
        return await self._get(url, params={"event": event_id})

    async def get_teams(self, league_slug: str) -> dict:
        """Team list for a league, used to resolve a team name to an ID."""
        url = f"{BASE_URL}/{league_slug}/teams"
        return await self._get(url)

    async def get_team(self, league_slug: str, team_id: str) -> dict:
        """Single team's profile, including `data["team"]["nextEvent"]` — a
        (usually one-item) list with its next scheduled fixture. Used to show
        kick-off time right after a team is set on an accumulator.
        """
        url = f"{BASE_URL}/{league_slug}/teams/{team_id}"
        return await self._get(url)
