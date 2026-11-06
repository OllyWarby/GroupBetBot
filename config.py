import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None

# When on, the bot uses services.fake_espn_client.FakeEspnClient instead of
# the real ESPN client (see bot.py) - no network calls are made, and a
# scripted match plays out over ~90s as soon as an accumulator slot is
# filled with 4 (fake) teams. Data is also isolated into DATA_DIR below so
# testing never touches real accumulators/history.
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in ("1", "true", "yes", "on")

if TEST_MODE:
    # Single fake league the FakeEspnClient responds to.
    LEAGUE_SLUGS = {"test": "test.sim"}

    # Fast enough to track a ~90s simulated match end-to-end and to notice
    # a freshly-set accumulator within a few seconds rather than minutes.
    LIVE_POLL_INTERVAL = 2
    PRE_MATCH_WINDOW_SECONDS = 10
    PRE_MATCH_POLL_INTERVAL = 2
    FAR_OUT_POLL_INTERVAL = 3
else:
    # ESPN league slugs we support, in priority order for team search.
    LEAGUE_SLUGS = {
        "epl": "eng.1",
        "championship": "eng.2",
        "league_one": "eng.3",
        "league_two": "eng.4",
        "national_league": "eng.5",
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

# Live updates are posted to the text channel with this name (looked up in
# every guild the bot is in), so there's nothing to configure at startup.
UPDATE_CHANNEL_NAME = "test-channel" if TEST_MODE else "live-updates"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data_test" if TEST_MODE else "data")
ACCUMULATORS_FILE = os.path.join(DATA_DIR, "accumulators.json")
TEAM_CACHE_FILE = os.path.join(DATA_DIR, "team_cache.json")
MATCH_STATE_FILE = os.path.join(DATA_DIR, "match_state.json")
STANDINGS_FILE = os.path.join(DATA_DIR, "standings.json")
ROSTER_FILE = os.path.join(DATA_DIR, "roster.json")
PEOPLE_FILE = os.path.join(DATA_DIR, "people.json")

ACC_SLOTS = ("1", "2")
TEAMS_PER_ACC = 4

# The Odds API (the-odds-api.com) - free tier used for match-winner (h2h)
# prices. See services/odds_client.py for the provider-isolation rationale.
#
# Master switch: off by default. While this is false, odds_client makes no
# network calls at all (not even to check a key) - /acc set just shows
# "odds unavailable" everywhere, same as if the feature didn't exist. Flip
# ODDS_ENABLED=true in .env once you've got a free API key to actually use.
ODDS_ENABLED = os.getenv("ODDS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "uk"
ODDS_MARKET = "h2h"
ODDS_FORMAT = "decimal"

# The Odds API doesn't publish stable sport_key names for every league we
# track, and League One/Two coverage isn't confirmed at all - so instead of
# hardcoding guessed keys, services/odds_client.py looks up the sport list
# by matching these title keywords (case-insensitive substring match).
ODDS_LEAGUE_TITLE_HINTS = {
    "epl": ("premier league",),
    "championship": ("championship",),
    "league_one": ("league 1", "league one"),
    "league_two": ("league 2", "league two"),
}

# Other countries/competitions can collide with the hints above (Scotland
# also has a "Championship", Wales a "Premier League", etc). If a title
# matches a hint but also matches one of these, skip it rather than risk
# silently binding to the wrong country's competition.
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

# How long (seconds) to reuse a league's fetched odds board before asking
# the API again. One /acc set call can involve several teams from the same
# league (e.g. two League One picks) - without this, each of those re-fetches
# the whole board, burning free-tier credits on data already in hand.
ODDS_BOARD_CACHE_SECONDS = 300
