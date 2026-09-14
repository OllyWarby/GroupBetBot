"""Background task: figures out which fixtures involve our tracked teams,
polls their scores on a schedule that ramps up as kickoff approaches, and
posts events + an accumulator status summary to the configured Discord
channel when something changes.

Scheduling behaviour:
- Far out from any known kickoff: sleeps in FAR_OUT_POLL_INTERVAL chunks,
  but actually computes the exact wake-up time for PRE_MATCH_WINDOW_SECONDS
  before the next kickoff rather than blindly polling on a fixed cadence.
- Inside the pre-match window: polls every PRE_MATCH_POLL_INTERVAL seconds,
  fast enough to catch kickoff (and an early goal) within seconds.
- Once any tracked match is live: polls every LIVE_POLL_INTERVAL seconds.

Request volume stays low throughout because each poll is one scoreboard
call per LEAGUE in play, not per team. The heavier `summary` (incidents)
endpoint is only called for a specific match when a score/status change
was just detected on it.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord

from config import (
    LEAGUE_SLUGS,
    ACCUMULATORS_FILE,
    MATCH_STATE_FILE,
    CONFIG_FILE,
    LIVE_POLL_INTERVAL,
    PRE_MATCH_WINDOW_SECONDS,
    PRE_MATCH_POLL_INTERVAL,
    FAR_OUT_POLL_INTERVAL,
)
from services.espn_client import EspnClient, EspnClientError
from services.event_detector import (
    MatchSnapshot,
    snapshot_from_scoreboard_event,
    diff_snapshots,
    extract_new_card_events,
    team_with_early_payout_lead,
)
import storage

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "STATUS_SCHEDULED": "not started",
    "STATUS_FIRST_HALF": "LIVE",
    "STATUS_SECOND_HALF": "LIVE",
    "STATUS_HALFTIME": "HT",
    "STATUS_FULL_TIME": "FT",
}

# Statuses where the match clock is actually ticking (not half-time, not a
# stoppage) - used to decide whether to show "LIVE <clock>" instead of a
# plain phase label.
IN_PLAY_STATUSES = {"STATUS_FIRST_HALF", "STATUS_SECOND_HALF"}

# Statuses where a tracked match has started but isn't finished, including
# breaks in play like half-time. Used (alongside `state == "in"`) to decide
# whether polling should stay on the fast live cadence - ESPN's generic
# `state` bucket isn't reliably "in" while the clock is stopped at
# half-time, so without this a match could fall back to the slow far-out
# poll interval right when it goes to the break and fail to pick the
# second half back up promptly.
LIVE_OR_PAUSED_STATUSES = IN_PLAY_STATUSES | {"STATUS_HALFTIME"}


def _team_name(entry) -> str:
    return entry["name"] if isinstance(entry, dict) else entry


class Poller:
    def __init__(self, bot: discord.Client, client: EspnClient):
        self.bot = bot
        self.client = client
        self._snapshots: dict[str, object] = {}  # event_id -> MatchSnapshot
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever())

    def stop(self):
        if self._task:
            self._task.cancel()

    def clear_snapshots(self):
        """Drop all in-memory match snapshots. Call this after archiving a
        bet so a stale finished-match snapshot can't be shown against a
        same-named team's next fixture before the next real poll refreshes it.
        """
        self._snapshots.clear()

    async def _load_state_from_disk(self):
        """Restore last-known match snapshots from match_state.json so a
        restart doesn't lose the diffing baseline.

        Trade-offs to know about: this restores score/status so a genuine
        change that happened while the bot was down still gets detected
        and posted on the next tick - but it collapses whatever happened
        during the downtime into a single diff (e.g. 0-0 -> 2-1 while
        offline is posted as one "goal" update showing the final score,
        not two separate goals). It also doesn't restore which red cards
        were already announced, so a card from just before a restart could
        in rare cases be announced twice. Neither matters for the common
        case of "bot was already running before and during kickoff."
        """
        state_dump = await storage.read_json(MATCH_STATE_FILE, {})
        restored = 0
        for event_id, entry in state_dump.items():
            try:
                home_score, away_score = (int(x) for x in entry["score"].split("-"))
            except (KeyError, ValueError):
                continue
            self._snapshots[event_id] = MatchSnapshot(
                event_id=event_id,
                league=entry.get("league", ""),
                home_name=entry.get("home", ""),
                away_name=entry.get("away", ""),
                home_score=home_score,
                away_score=away_score,
                status=entry.get("status", "STATUS_SCHEDULED"),
            )
            restored += 1
        if restored:
            logger.info("Restored %d match snapshot(s) from match_state.json", restored)

    async def _run_forever(self):
        await self._load_state_from_disk()
        while True:
            try:
                sleep_for = await self._tick()
            except Exception:
                logger.exception("Poller tick failed")
                sleep_for = FAR_OUT_POLL_INTERVAL
            logger.info("Next poll in %.0fs", sleep_for)
            await asyncio.sleep(sleep_for)

    async def _tracked_team_names(self) -> set[str]:
        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        names = set()
        for acc in accumulators.values():
            for team in acc.get("teams", []):
                names.add(_team_name(team))
        return names

    async def _get_channel(self) -> discord.abc.Messageable | None:
        cfg = await storage.read_json(CONFIG_FILE, {})
        channel_id = cfg.get("update_channel_id")
        if not channel_id:
            return None
        return self.bot.get_channel(int(channel_id))

    def _find_snapshot_by_team(self, team_name: str):
        for snapshot in self._snapshots.values():
            if snapshot.home_name == team_name or snapshot.away_name == team_name:
                return snapshot
        return None

    def _format_accumulator_status(self, slot: str, acc: dict) -> str:
        lines = [f"\U0001F4CA **Acc {slot}**"]
        for team in acc.get("teams", []):
            name = _team_name(team)
            locked = isinstance(team, dict) and team.get("locked_win")
            snapshot = self._find_snapshot_by_team(name)

            if snapshot is None:
                if locked:
                    lines.append(f"\U0001F512 {name} {team.get('locked_score', '')}: early payout")
                else:
                    lines.append(f"❔ {name}: no data")
                continue

            if snapshot.status == "STATUS_SCHEDULED":
                lines.append(f"⏳ {name}: not started")
                continue

            # Show the tracked team's own score first (not always the home
            # score) so an away team's line doesn't read backwards.
            is_home = snapshot.home_name == name
            team_score = snapshot.home_score if is_home else snapshot.away_score
            opp_score = snapshot.away_score if is_home else snapshot.home_score
            opponent = snapshot.away_name if is_home else snapshot.home_name

            label = STATUS_LABELS.get(snapshot.status, snapshot.status)
            if snapshot.status in IN_PLAY_STATUSES and snapshot.display_clock:
                label = f"LIVE {snapshot.display_clock}"

            if locked:
                # 2+ goals clear (at any point) auto-settles this leg as a
                # win with our provider - it stays a win even if the team
                # concedes and the score above later says otherwise.
                result_icon = "\U0001F512✅"
                label = f"{label}, early payout"
            else:
                # A draw counts as a loss for accumulator purposes.
                result_icon = "✅" if team_score > opp_score else "❌"
            lines.append(
                f"{result_icon} {name} {team_score}-{opp_score} {opponent} ({label})"
            )
        return "\n".join(lines)

    async def _lock_in_early_payouts(self, snapshot: MatchSnapshot) -> list[str]:
        """If either side of this match now has an early-payout-qualifying
        lead, mark that team's accumulator leg(s) as a locked-in win in
        ACCUMULATORS_FILE - our bet provider auto-settles a leg as a winner
        once a team goes 2 goals clear, and it stays a winner even if that
        team later draws or loses. The lock is sticky (checked via
        `locked_win`) so this only fires once per team, and cheap to call
        repeatedly since it no-ops when nothing currently qualifies.
        """
        lead = team_with_early_payout_lead(snapshot)
        if lead is None:
            return []
        team_name, team_score, opp_score, opponent = lead

        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        newly_locked = False
        for acc in accumulators.values():
            for team in acc.get("teams", []):
                if not isinstance(team, dict) or team.get("name") != team_name:
                    continue
                if team.get("locked_win"):
                    continue
                team["locked_win"] = True
                team["locked_score"] = f"{team_score}-{opp_score}"
                newly_locked = True

        if not newly_locked:
            return []

        await storage.write_json(ACCUMULATORS_FILE, accumulators)
        return [f"\U0001F512 {team_name} {team_score}-{opp_score} {opponent} - early payout locked in!"]

    async def _accumulators_for_team(self, team_name: str) -> list[tuple[str, dict]]:
        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        matches = []
        for slot, acc in accumulators.items():
            names = {_team_name(t) for t in acc.get("teams", [])}
            if team_name in names:
                matches.append((slot, acc))
        return matches

    async def _tick(self) -> float:
        """Poll everything relevant once. Returns how many seconds to sleep
        before the next tick.
        """
        tracked_names = await self._tracked_team_names()
        if not tracked_names:
            return FAR_OUT_POLL_INTERVAL

        channel = await self._get_channel()
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y%m%d")

        state_dump = {}
        this_tick_matches = []  # snapshots for tracked teams found this tick

        for slug in LEAGUE_SLUGS.values():
            try:
                payload = await self.client.get_scoreboard(slug, date=today_str)
            except EspnClientError as e:
                logger.warning("Scoreboard fetch failed for %s: %s", slug, e)
                continue

            for event in payload.get("events", []):
                snapshot = snapshot_from_scoreboard_event(event, slug)
                if snapshot is None:
                    continue
                if snapshot.home_name not in tracked_names and snapshot.away_name not in tracked_names:
                    continue  # not one of our teams

                old = self._snapshots.get(snapshot.event_id)
                if old is not None:
                    snapshot.seen_incident_ids = old.seen_incident_ids  # carry over card dedup

                text_events = diff_snapshots(old, snapshot)

                # Persist the new baseline *before* the best-effort enrichment
                # below. If fetching/parsing the summary feed ever throws
                # (bad shape, unexpected incident, etc.) it must not leave us
                # diffing against a stale pre-halftime snapshot on every
                # future tick - that would silently swallow every later
                # goal/status change for this match, including the restart,
                # for the rest of the game.
                self._snapshots[snapshot.event_id] = snapshot
                this_tick_matches.append(snapshot)
                state_dump[snapshot.event_id] = {
                    "league": snapshot.league,
                    "home": snapshot.home_name,
                    "away": snapshot.away_name,
                    "score": f"{snapshot.home_score}-{snapshot.away_score}",
                    "status": snapshot.status,
                }

                if old is None or text_events:
                    # Check on every score change, and also on first sighting
                    # so a team that's already 2+ clear when the bot starts
                    # watching (e.g. after a restart) still gets locked in.
                    try:
                        text_events.extend(await self._lock_in_early_payouts(snapshot))
                    except Exception:
                        logger.exception(
                            "Failed to check early payout for event %s", snapshot.event_id
                        )

                if text_events and snapshot.state == "in":
                    # Best-effort only (who scored / red cards) - never let a
                    # bad or unexpected shape in this feed stop the core
                    # score/status update above from being posted.
                    try:
                        summary = await self.client.get_summary(slug, snapshot.event_id)
                        text_events.extend(extract_new_card_events(summary, snapshot))
                    except EspnClientError as e:
                        logger.warning("Summary fetch failed for event %s: %s", snapshot.event_id, e)
                    except Exception:
                        logger.exception(
                            "Failed to enrich event(s) for %s with summary detail", snapshot.event_id
                        )

                if text_events:
                    if channel is None:
                        logger.info("Event(s) detected but no update channel set: %s", text_events)
                    else:
                        try:
                            await channel.send("\n".join(text_events))
                            # Follow-up: post the overall standing of any
                            # accumulator this match's teams belong to.
                            posted_slots = set()
                            for team_name in (snapshot.home_name, snapshot.away_name):
                                for slot, acc in await self._accumulators_for_team(team_name):
                                    if slot in posted_slots:
                                        continue
                                    posted_slots.add(slot)
                                    await channel.send(self._format_accumulator_status(slot, acc))
                        except Exception:
                            logger.exception("Failed to post update(s) for event %s", snapshot.event_id)

        await storage.write_json(MATCH_STATE_FILE, state_dump)

        any_live = any(
            s.state == "in" or s.status in LIVE_OR_PAUSED_STATUSES for s in this_tick_matches
        )
        if any_live:
            return LIVE_POLL_INTERVAL

        upcoming = [s.start_time for s in this_tick_matches if s.status == "STATUS_SCHEDULED" and s.start_time]
        if not upcoming:
            return FAR_OUT_POLL_INTERVAL

        nearest = min(upcoming)
        seconds_until_kickoff = (nearest - now).total_seconds()
        if seconds_until_kickoff <= PRE_MATCH_WINDOW_SECONDS:
            return PRE_MATCH_POLL_INTERVAL

        # Wake up right as we enter the pre-match window, not before.
        return max(1, min(FAR_OUT_POLL_INTERVAL, seconds_until_kickoff - PRE_MATCH_WINDOW_SECONDS))
