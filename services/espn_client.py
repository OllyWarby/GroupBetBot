import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"

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
        url = f"{BASE_URL}/{league_slug}/scoreboard"
        params = {"dates": date} if date else None
        return await self._get(url, params=params)

    async def get_summary(self, league_slug: str, event_id: str) -> dict:
        url = f"{BASE_URL}/{league_slug}/summary"
        return await self._get(url, params={"event": event_id})

    async def get_teams(self, league_slug: str) -> dict:
        url = f"{BASE_URL}/{league_slug}/teams"
        return await self._get(url)

    async def get_team(self, league_slug: str, team_id: str) -> dict:
        url = f"{BASE_URL}/{league_slug}/teams/{team_id}"
        return await self._get(url)
