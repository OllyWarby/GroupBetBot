# GroupBetBot

Discord bot that tracks two 4-team football accumulators (EPL down to the National League) and posts live goals, half-time, full-time and red cards to a channel. Match data comes from ESPN's unofficial site API (no key needed).

## Setup

```bash
python -m venv venv
source venv/bin/activate  # venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env      # add your DISCORD_TOKEN
python bot.py
```

Invite the bot with the `bot` and `applications.commands` scopes, and create a text channel called `#live-updates`. Updates are posted there automatically.

Optional: set `ODDS_ENABLED=true` and `ODDS_API_KEY` (free key from [the-odds-api.com](https://the-odds-api.com)) to show odds in `/acc set`. League One/Two coverage isn't confirmed; missing odds show as "unavailable".

## Test mode

Set `TEST_MODE=true` to run against a fake ESPN client with made-up teams (`Test Alpha` ... `Test Hotel`). Data goes to `data_test/` and updates go to `#test-channel`, so create that channel too.

Fill a slot with four test teams (`/acc set 1`, then enter Test Alpha, Test Beta, Test Gamma and Test Delta in the boxes) and a ~75s scripted match plays out: kick-off, goals, half-time, a red card, an early-payout lock that is then undone by a disallowed goal, and full-time. The bet is then archived and cleared automatically, so you can `/acc set` again straight away.

## Commands

- `/acc set slot` - opens a form with one box per name from `ACC_NAMES` in `config.py` (e.g. Liv, Cal, Dave, Matt); enter each person's team and it posts kick-off times, names and odds
- `/acc check slot` - check each team resolves against ESPN (run before matchday)
- `/acc show` - show current teams and next kick-offs
- `/acc clear slot` - clear a slot
- `/acc newbet` - archive both slots' results and reset them for the next bet
- `/acc history count` - show recent archived bets (default 5)
- `/person link name user` - link a name to a Discord member so they're @-mentioned
- `/person history name count` - show a person's win/loss record (default 10)

Slots are `1` or `2`. Data is stored as JSON under `data/`.

## Notes

The ESPN API is unofficial and can change or go down without warning. ESPN logic lives in `services/espn_client.py` and odds logic in `services/odds_client.py`, so either can be swapped out.
