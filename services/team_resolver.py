"""Resolves a human-typed team name (e.g. "Arsenal", "man utd") to the
ESPN team ID + league slug we need for polling, and caches the result so
we don't re-search the API every time.
"""

import logging

from config import LEAGUE_SLUGS, TEAM_CACHE_FILE
from services.espn_client import EspnClient, EspnClientError
import storage

logger = logging.getLogger(__name__)


class TeamNotFoundError(Exception):
    pass


class AmbiguousTeamError(Exception):
    def __init__(self, matches):
        self.matches = matches
        super().__init__(f"Ambiguous team name, {len(matches)} matches")


async def _load_cache() -> dict:
    return await storage.read_json(TEAM_CACHE_FILE, {})


async def _save_cache(cache: dict):
    await storage.write_json(TEAM_CACHE_FILE, cache)


def _normalize(name: str) -> str:
    return name.strip().lower()


async def resolve_team(client: EspnClient, team_name: str) -> dict:
    """Resolve a team name across all supported leagues.

    Returns a dict: {"name": display_name, "espn_id": ..., "league": slug}.
    Raises TeamNotFoundError or AmbiguousTeamError if it can't confidently
    resolve one team.
    """
    cache = await _load_cache()
    key = _normalize(team_name)
    if key in cache:
        return cache[key]

    matches = []
    for league_key, slug in LEAGUE_SLUGS.items():
        try:
            payload = await client.get_teams(slug)
        except EspnClientError as e:
            logger.warning("Could not fetch teams for %s: %s", slug, e)
            continue

        for sport in payload.get("sports", []):
            for league in sport.get("leagues", []):
                for entry in league.get("teams", []):
                    team = entry.get("team", {})
                    candidates = {
                        _normalize(team.get("displayName", "")),
                        _normalize(team.get("shortDisplayName", "")),
                        _normalize(team.get("name", "")),
                        _normalize(team.get("abbreviation", "")),
                    }
                    if key in candidates:
                        matches.append(
                            {
                                "name": team.get("displayName"),
                                "espn_id": team.get("id"),
                                "league": slug,
                                "league_key": league_key,
                            }
                        )

    if not matches:
        raise TeamNotFoundError(team_name)
    if len(matches) > 1:
        # Same/similar name across leagues (rare, but possible) - let the
        # caller decide rather than silently guessing.
        raise AmbiguousTeamError(matches)

    result = matches[0]
    cache[key] = result
    await _save_cache(cache)
    return result
