from datetime import datetime, timedelta, timezone

from config import ACCUMULATORS_FILE, ACC_SLOTS
import storage

FAKE_LEAGUE_SLUG = "test.sim"

ROSTER = [
    {"name": "Test Alpha", "id": "test-1"},
    {"name": "Test Beta", "id": "test-2"},
    {"name": "Test Gamma", "id": "test-3"},
    {"name": "Test Delta", "id": "test-4"},
    {"name": "Test Echo", "id": "test-5"},
    {"name": "Test Foxtrot", "id": "test-6"},
    {"name": "Test Golf", "id": "test-7"},
    {"name": "Test Hotel", "id": "test-8"},
]

KICKOFF_DELAY_SECONDS = 5
HALFTIME_AT = 30
SECOND_HALF_AT = 40
FULL_TIME_AT = 70

GOAL_SCHEDULES = [
    [(15, "home"), (50, "home")],
    [(25, "away")],
]


class FakeEspnClient:
    def __init__(self):
        self._kickoffs: dict[str, datetime] = {}

    async def close(self):
        pass

    def _kickoff_for(self, key: str) -> datetime:
        if key not in self._kickoffs:
            self._kickoffs[key] = datetime.now(timezone.utc) + timedelta(seconds=KICKOFF_DELAY_SECONDS)
        return self._kickoffs[key]

    def _match_state(self, key: str, pair_index: int, home: str, away: str) -> dict:
        kickoff = self._kickoff_for(key)
        elapsed = (datetime.now(timezone.utc) - kickoff).total_seconds()

        if elapsed < 0:
            status, state, clock = "STATUS_SCHEDULED", "pre", ""
        elif elapsed < HALFTIME_AT:
            status, state, clock = "STATUS_FIRST_HALF", "in", f"{int(elapsed)}'"
        elif elapsed < SECOND_HALF_AT:
            status, state, clock = "STATUS_HALFTIME", "in", "HT"
        elif elapsed < FULL_TIME_AT:
            status, state, clock = "STATUS_SECOND_HALF", "in", f"{45 + int(elapsed - SECOND_HALF_AT)}'"
        else:
            status, state, clock = "STATUS_FULL_TIME", "post", "FT"

        schedule = GOAL_SCHEDULES[pair_index % len(GOAL_SCHEDULES)]
        home_score = sum(1 for t, side in schedule if side == "home" and elapsed >= t)
        away_score = sum(1 for t, side in schedule if side == "away" and elapsed >= t)

        return {
            "id": key,
            "home": home,
            "away": away,
            "home_score": home_score,
            "away_score": away_score,
            "status": status,
            "state": state,
            "clock": clock,
            "kickoff": kickoff,
        }

    async def _simulated_matches(self) -> list[dict]:
        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        matches = []
        for slot in ACC_SLOTS:
            teams = accumulators.get(slot, {}).get("teams", [])
            names = [t["name"] if isinstance(t, dict) else t for t in teams]
            if len(names) < 4:
                continue
            for pair_index, (home, away) in enumerate([(names[0], names[1]), (names[2], names[3])]):
                key = f"{slot}:{pair_index}"
                matches.append(self._match_state(key, pair_index, home, away))
        return matches

    @staticmethod
    def _event_json(match: dict) -> dict:
        return {
            "id": match["id"],
            "date": match["kickoff"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": {
                "type": {"name": match["status"], "state": match["state"]},
                "displayClock": match["clock"],
            },
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": str(match["home_score"]),
                            "team": {"displayName": match["home"]},
                        },
                        {
                            "homeAway": "away",
                            "score": str(match["away_score"]),
                            "team": {"displayName": match["away"]},
                        },
                    ]
                }
            ],
        }

    async def get_scoreboard(self, league_slug: str, date: str | None = None) -> dict:
        matches = await self._simulated_matches()
        return {"events": [self._event_json(m) for m in matches]}

    async def get_summary(self, league_slug: str, event_id: str) -> dict:
        return {"details": []}

    async def get_teams(self, league_slug: str) -> dict:
        return {
            "sports": [
                {
                    "leagues": [
                        {
                            "teams": [
                                {
                                    "team": {
                                        "displayName": t["name"],
                                        "shortDisplayName": t["name"],
                                        "name": t["name"],
                                        "abbreviation": t["id"].upper(),
                                        "id": t["id"],
                                    }
                                }
                                for t in ROSTER
                            ]
                        }
                    ]
                }
            ]
        }

    async def get_team(self, league_slug: str, team_id: str) -> dict:
        team_name = next((t["name"] for t in ROSTER if t["id"] == team_id), None)
        if team_name is None:
            return {"team": {"nextEvent": []}}

        for match in await self._simulated_matches():
            if match["home"] == team_name or match["away"] == team_name:
                return {"team": {"nextEvent": [self._event_json(match)]}}
        return {"team": {"nextEvent": []}}
