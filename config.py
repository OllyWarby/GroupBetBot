import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None

# ESPN league slugs we support, in priority order for team search.
LEAGUE_SLUGS = {
    "epl": "eng.1",
    "championship": "eng.2",
    "league_one": "eng.3",
    "league_two": "eng.4",
}

# How often (seconds) to poll a league's scoreboard while any tracked
# fixture in it is live. ESPN's site API is unofficial and doesn't publish
# a rate limit; this stays low-risk because we call it once per LEAGUE in
# play, not once per team - for a single Saturday 3pm kickoff that's
# likely 1-2 calls per tick regardless of how many of your 8 teams are on.
LIVE_POLL_INTERVAL = 20

# How long before kickoff to start ramping up polling, and how fast to
# poll during that window, so we catch the STATUS_IN_PROGRESS transition
# (and any early card/goal) within seconds rather than minutes.
PRE_MATCH_WINDOW_SECONDS = 5 * 60
PRE_MATCH_POLL_INTERVAL = 15

# How often (seconds) to check for upcoming kickoffs when nothing is
# imminent. This is just a fallback ceiling - the poller actually
# schedules itself to wake up exactly PRE_MATCH_WINDOW_SECONDS before the
# next known kickoff rather than blindly polling on this cadence.
FAR_OUT_POLL_INTERVAL = 600

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ACCUMULATORS_FILE = os.path.join(DATA_DIR, "accumulators.json")
TEAM_CACHE_FILE = os.path.join(DATA_DIR, "team_cache.json")
MATCH_STATE_FILE = os.path.join(DATA_DIR, "match_state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STANDINGS_FILE = os.path.join(DATA_DIR, "standings.json")

ACC_SLOTS = ("1", "2")
TEAMS_PER_ACC = 4
