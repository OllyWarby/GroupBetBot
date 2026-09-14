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

3. Run the bot:

   ```bash
   python bot.py
   ```

4. In Discord, run `/acc setchannel` in the channel you want live updates posted to.

## Commands

- `/acc set slot:<1|2> teams:<comma,separated,team,names>` — set an accumulator's 4 teams
  (also looks up and posts each team's next kick-off time from ESPN)
- `/acc check slot:<1|2>` — verify each team resolves against ESPN's data (run this before matchday!)
- `/acc show` — show current teams and each team's next kick-off time from ESPN
- `/acc clear slot:<1|2>` — clear an accumulator
- `/acc newbet` — archive both slots' results into `standings.json` history and reset them for the
  next bet (a following Saturday round, or an adhoc midweek/cup one — same 2-slot format either way)
- `/acc history count:<n>` — show the `n` most recently archived bets (default 5)
- `/acc setchannel` — set the channel for live match updates (run in the target channel)

## Data storage

Plain JSON files under `data/`:

- `accumulators.json` — current team selections
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
