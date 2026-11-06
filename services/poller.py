import asyncio
import logging
from datetime import datetime, timezone

import discord

from config import (
    LEAGUE_SLUGS,
    ACCUMULATORS_FILE,
    MATCH_STATE_FILE,
    UPDATE_CHANNEL_NAME,
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
    is_winning_leg,
    team_with_early_payout_lead,
    EARLY_PAYOUT_GOAL_MARGIN,
)
import storage

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "STATUS_SCHEDULED": "not started",
    "STATUS_FIRST_HALF": "LIVE",
    "STATUS_SECOND_HALF": "LIVE",
    "STATUS_HALFTIME": "HT",
    "STATUS_FULL_TIME": "FT",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "cancelled",
}

IN_PLAY_STATUSES = {"STATUS_FIRST_HALF", "STATUS_SECOND_HALF"}

LIVE_OR_PAUSED_STATUSES = IN_PLAY_STATUSES | {"STATUS_HALFTIME"}


def _team_name(entry) -> str:
    return entry["name"] if isinstance(entry, dict) else entry


def _embed_color(events: list[str]) -> discord.Color:
    joined = " ".join(events)
    if "⚽" in joined:  # goal
        return discord.Color.green()
    if "\U0001F6AB" in joined:  # goal disallowed
        return discord.Color.red()
    if "\U0001F512" in joined:  # early payout locked in
        return discord.Color.gold()
    if "\U0001F7E2" in joined:  # kick-off
        return discord.Color.blue()
    if "FT" in joined:  # full-time
        return discord.Color.dark_grey()
    if "HT" in joined:  # half-time
        return discord.Color.light_grey()
    return discord.Color.blurple()


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
        self._snapshots.clear()

    async def _load_state_from_disk(self):
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
                seen_incident_ids=set(entry.get("seen_incidents", [])),
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
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=UPDATE_CHANNEL_NAME)
            if channel is not None:
                return channel
        return None

    def _find_snapshot_by_team(self, team_name: str):
        for snapshot in self._snapshots.values():
            if snapshot.home_name == team_name or snapshot.away_name == team_name:
                return snapshot
        return None

    def _format_accumulator_status(self, acc: dict) -> str:
        lines = []
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

            is_home = snapshot.home_name == name
            team_score = snapshot.home_score if is_home else snapshot.away_score
            opp_score = snapshot.away_score if is_home else snapshot.home_score
            opponent = snapshot.away_name if is_home else snapshot.home_name

            label = STATUS_LABELS.get(snapshot.status, snapshot.status)
            if snapshot.status in IN_PLAY_STATUSES and snapshot.display_clock:
                label = f"LIVE {snapshot.display_clock}"

            if locked:
                result_icon = "\U0001F512✅"
                label = f"{label}, early payout"
            else:
                # A draw counts as a loss for accumulator purposes.
                result_icon = "✅" if is_winning_leg(team_score, opp_score, False) else "❌"
            lines.append(
                f"{result_icon} {name} {team_score}-{opp_score} {opponent} ({label})"
            )
        return "\n".join(lines)

    async def _release_early_payouts(self, old: MatchSnapshot, snapshot: MatchSnapshot) -> list[str]:
        sides = (
            (snapshot.home_name, old.home_score, snapshot.home_score, snapshot.away_score),
            (snapshot.away_name, old.away_score, snapshot.away_score, snapshot.home_score),
        )
        # A team's own score dropped and it no longer has a qualifying lead.
        candidates = {
            name
            for name, old_score, new_score, opp_score in sides
            if new_score < old_score and new_score - opp_score < EARLY_PAYOUT_GOAL_MARGIN
        }
        if not candidates:
            return []

        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        released = set()
        for acc in accumulators.values():
            for team in acc.get("teams", []):
                if not isinstance(team, dict) or team.get("name") not in candidates:
                    continue
                if not team.get("locked_win"):
                    continue
                team.pop("locked_win", None)
                team.pop("locked_score", None)
                released.add(team["name"])

        if not released:
            return []

        await storage.write_json(ACCUMULATORS_FILE, accumulators)
        return [
            f"\U0001F513 {name} early payout removed - goal disallowed"
            for name in sorted(released)
        ]

    async def _lock_in_early_payouts(self, snapshot: MatchSnapshot) -> list[str]:
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

    async def _post_accumulator_updates(
        self, channel: discord.abc.Messageable | None, events_this_tick: list[tuple]
    ):
        if channel is None:
            logger.info("Event(s) detected but no update channel set: %s", events_this_tick)
            return

        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        for slot, acc in accumulators.items():
            team_names = {_team_name(t) for t in acc.get("teams", [])}
            if not team_names:
                continue

            slot_events = [
                line
                for snapshot, text_events in events_this_tick
                for line in text_events
                if snapshot.home_name in team_names or snapshot.away_name in team_names
            ]
            if not slot_events:
                continue

            embed = discord.Embed(
                title=f"\U0001F4CA Accumulator {slot} update",
                description="\n".join(slot_events),
                color=_embed_color(slot_events),
            )
            embed.add_field(name="Status", value=self._format_accumulator_status(acc), inline=False)
            try:
                await channel.send(embed=embed)
            except Exception:
                logger.exception("Failed to post update for accumulator %s", slot)

    async def _post_red_cards(self, channel: discord.abc.Messageable | None, lines: list[str]):
        if channel is None:
            logger.info("Red card(s) detected but no update channel set: %s", lines)
            return

        for line in lines:
            try:
                await channel.send(line)
            except Exception:
                logger.exception("Failed to post red card line")

    async def _post_halftime_updates(
        self, channel: discord.abc.Messageable | None, halftime_matches: list[MatchSnapshot]
    ):
        if channel is None:
            logger.info("Half-time(s) detected but no update channel set: %s", halftime_matches)
            return

        for snapshot in halftime_matches:
            line = (
                f"\U0001F3C1 HT {snapshot.home_name} {snapshot.home_score}-"
                f"{snapshot.away_score} {snapshot.away_name}"
            )
            try:
                await channel.send(line)
            except Exception:
                logger.exception("Failed to post half-time line for %s", snapshot.event_id)

        accumulators = await storage.read_json(ACCUMULATORS_FILE, {})
        for slot, acc in accumulators.items():
            team_names = [_team_name(t) for t in acc.get("teams", [])]
            if not team_names:
                continue

            just_arrived = any(
                s.home_name in team_names or s.away_name in team_names for s in halftime_matches
            )
            if not just_arrived:
                continue

            all_at_halftime = all(
                (snap := self._find_snapshot_by_team(name)) is not None
                and snap.status == "STATUS_HALFTIME"
                for name in team_names
            )
            if not all_at_halftime:
                continue

            embed = discord.Embed(
                title=f"\U0001F4CA Accumulator {slot} halftime update",
                description=self._format_accumulator_status(acc),
                color=discord.Color.light_grey(),
            )
            try:
                await channel.send(embed=embed)
            except Exception:
                logger.exception("Failed to post half-time update for accumulator %s", slot)

    async def _tick(self) -> float:
        tracked_names = await self._tracked_team_names()
        if not tracked_names:
            return FAR_OUT_POLL_INTERVAL

        channel = await self._get_channel()
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y%m%d")

        state_dump = {}
        this_tick_matches = []
        events_this_tick: list[tuple] = []
        halftime_matches: list[MatchSnapshot] = []  # matches that just reached half-time
        red_card_lines: list[str] = []

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
                if (
                    old is not None
                    and old.status != "STATUS_HALFTIME"
                    and snapshot.status == "STATUS_HALFTIME"
                ):
                    halftime_matches.append(snapshot)

                self._snapshots[snapshot.event_id] = snapshot
                this_tick_matches.append(snapshot)
                state_dump[snapshot.event_id] = {
                    "league": snapshot.league,
                    "home": snapshot.home_name,
                    "away": snapshot.away_name,
                    "score": f"{snapshot.home_score}-{snapshot.away_score}",
                    "status": snapshot.status,
                }

                if old is not None and (
                    snapshot.home_score < old.home_score or snapshot.away_score < old.away_score
                ):
                    try:
                        text_events.extend(await self._release_early_payouts(old, snapshot))
                    except Exception:
                        logger.exception(
                            "Failed to release early payout for event %s", snapshot.event_id
                        )

                if old is None or text_events:
                    try:
                        text_events.extend(await self._lock_in_early_payouts(snapshot))
                    except Exception:
                        logger.exception(
                            "Failed to check early payout for event %s", snapshot.event_id
                        )

                if snapshot.state == "in":
                    try:
                        summary = await self.client.get_summary(slug, snapshot.event_id)
                        red_card_lines.extend(extract_new_card_events(summary, snapshot))
                    except EspnClientError as e:
                        logger.warning("Summary fetch failed for event %s: %s", snapshot.event_id, e)
                    except Exception:
                        logger.exception(
                            "Failed to enrich event(s) for %s with summary detail", snapshot.event_id
                        )

                state_dump[snapshot.event_id]["seen_incidents"] = sorted(snapshot.seen_incident_ids)

                if text_events:
                    events_this_tick.append((snapshot, text_events))

        await storage.write_json(MATCH_STATE_FILE, state_dump)

        if red_card_lines:
            await self._post_red_cards(channel, red_card_lines)

        if events_this_tick:
            await self._post_accumulator_updates(channel, events_this_tick)

        if halftime_matches:
            await self._post_halftime_updates(channel, halftime_matches)

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
