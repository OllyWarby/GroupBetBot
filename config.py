import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None

TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in ("1", "true", "yes", "on")

if TEST_MODE:
    # Single fake league the FakeEspnClient responds to.
    LEAGUE_SLUGS = {"test": "test.sim"}

    LIVE_POLL_INTERVAL = 2
    PRE_MATCH_WINDOW_SECONDS = 10
    PRE_MATCH_POLL_INTERVAL = 2
    FAR_OUT_POLL_INTERVAL = 3
else:
    LEAGUE_SLUGS = {
        "epl": "eng.1",
        "championship": "eng.2",
        "league_one": "eng.3",
        "league_two": "eng.4",
        "national_league": "eng.5",
    }

    LIVE_POLL_INTERVAL = 20

    PRE_MATCH_WINDOW_SECONDS = 5 * 60
    PRE_MATCH_POLL_INTERVAL = 15

    FAR_OUT_POLL_INTERVAL = 600

UPDATE_CHANNEL_NAME = "test-channel" if TEST_MODE else "live-updates"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data_test" if TEST_MODE else "data")
ACCUMULATORS_FILE = os.path.join(DATA_DIR, "accumulators.json")
TEAM_CACHE_FILE = os.path.join(DATA_DIR, "team_cache.json")
MATCH_STATE_FILE = os.path.join(DATA_DIR, "match_state.json")
STANDINGS_FILE = os.path.join(DATA_DIR, "standings.json")
PEOPLE_FILE = os.path.join(DATA_DIR, "people.json")

ACC_SLOTS = ("1", "2")
TEAMS_PER_ACC = 4

# Each name becomes a labelled box in the /acc set form, and is stored against that box's team.
ACC_NAMES = {
    "1": ("Liv", "Cal", "Dave", "Matt"),
    "2": ("Olly", "Joe", "Luke", "Niamh"),
}

ODDS_ENABLED = os.getenv("ODDS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "uk"
ODDS_MARKET = "h2h"
ODDS_FORMAT = "decimal"

ODDS_LEAGUE_TITLE_HINTS = {
    "epl": ("premier league",),
    "championship": ("championship",),
    "league_one": ("league 1", "league one"),
    "league_two": ("league 2", "league two"),
}

ODDS_LEAGUE_EXCLUDE_HINTS = (
    "scotland",
    "scottish",
    "wales",
    "welsh",
    "ireland",
    "irish",
    "women",
    "u21",
    "u23",
    "youth",
)

ODDS_BOARD_CACHE_SECONDS = 300
