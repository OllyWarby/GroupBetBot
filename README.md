# Discord Football Accumulator Bot

Tracks two 4-team football accumulators (EPL / Championship / League One / League Two)
and posts live updates (goals, half-time, full-time, red cards) to a Discord channel.

Data source: ESPN's public (unofficial) site API — free, no API key required.
See `services/espn_client.py` for details and caveats.

## Setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your bot token:

   ```bash
   cp .env.example .env
   ```

   You'll need a Discord bot application with the `applications.commands` and
   `bot` scopes, and the "Message Content Intent" is NOT required since this
   bot only uses slash commands.

   Odds are off by default. To turn them on, set `ODDS_ENABLED=true` and add a
   free API key from [the-odds-api.com](https://the-odds-api.com) as
   `ODDS_API_KEY`. While disabled (or if a league isn't covered — see Notes
   below), `/acc set` just shows odds as unavailable; nothing else changes.

3. Run the bot:

   ```bash
   python bot.py
   ```

4. In Discord, run `/acc setchannel` in the channel you want live updates posted to.

## Commands

- `/acc set slot:<1|2> teams:<comma,separated,team,names>` — set an accumulator's 4 teams
  (also looks up and posts each team's next kick-off time, assigned name, and odds)
- `/acc setnames slot:<1|2> names:<comma,separated,4,names>` — predefine which name goes with
  which team position for a slot (name #1 ↔ team #1, etc.); persists across `/acc newbet`
- `/acc names slot:<1|2>` — show the names currently set for a slot
- `/acc check slot:<1|2>` — verify each team resolves against ESPN's data (run this before matchday!)
- `/acc show` — show current teams and each team's next kick-off time from ESPN
- `/acc clear slot:<1|2>` — clear an accumulator
- `/acc newbet` — archive both slots' results into `standings.json`, record each named leg's
  result into `people.json`, and reset both slots for the next bet (a following Saturday round,
  or an adhoc midweek/cup one — same 2-slot format either way)
- `/acc history count:<n>` — show the `n` most recently archived bets (default 5)
- `/acc setchannel` — set the channel for live match updates (run in the target channel)
- `/person link name:<name> user:<@member>` — link a predefined name to a Discord member, so
  they're @-mentioned in future `/acc set` summaries
- `/person history name:<name> count:<n>` — show a person's long-term win/loss record (default 10
  most recent entries)

## Data storage

Plain JSON files under `data/`:

- `accumulators.json` — current team selections, including each team's assigned name and
  odds captured when `/acc set` ran
- `roster.json` — the predefined names per slot (via `/acc setnames`), independent of the
  weekly team resets
- `people.json` — long-term per-person history: each archived leg's result (win/loss, or the
  match's status if it never finished) and the odds captured at bet time, plus an optional
  linked Discord user ID
- `team_cache.json` — resolved team name → ESPN team ID / league mappings
- `match_state.json` — last-seen score/status per tracked fixture (used to diff for events)
- `config.json` — the update channel ID
- `standings.json` — history of archived bets (via `/acc newbet`), each with both slots' teams
  and final results; raw data for now, ready for a scoring rule to be computed from once decided

## Notes

- The ESPN API is unofficial and undocumented by ESPN. It's free and has no
  published daily cap, but there's no SLA — it can change shape or go down
  without warning. All ESPN-specific logic is isolated in
  `services/espn_client.py` so swapping providers later is a one-file change.
- The poller only calls the *scoreboard* endpoint (one call per league in
  play) on the regular tick. It only calls the heavier *summary* (events)
  endpoint for a specific match when a score or status change is detected,
  to keep request volume low.
- Standings/points scoring is deliberately left as a stub — the plan is to
  get live tracking solid first and decide the scoring rule later.
- Odds are off by default (`ODDS_ENABLED=false`) and come from
  [The Odds API](https://the-odds-api.com)'s free tier (500 credits/month)
  once turned on, isolated in `services/odds_client.py` so it can be swapped
  later. EPL and EFL Championship coverage is confirmed; **League One/Two
  coverage is not confirmed** — those teams may just show "odds unavailable"
  once enabled. It looks up each league's sport key by title rather than a
  hardcoded key, since not every competition's key is documented, and
  degrades to "unavailable" rather than erroring when a league or fixture
  can't be matched.
