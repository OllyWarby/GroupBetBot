import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    ACCUMULATORS_FILE,
    CONFIG_FILE,
    MATCH_STATE_FILE,
    STANDINGS_FILE,
    ROSTER_FILE,
    ACC_SLOTS,
    TEAMS_PER_ACC,
)
from services.espn_client import EspnClientError
from services.event_detector import next_fixture_from_team_profile, is_winning_leg
from services.odds_client import OddsClientError
from services.poller import STATUS_LABELS
from services.team_resolver import resolve_team, TeamNotFoundError, AmbiguousTeamError
import services.people_store as people_store
import storage

DEFAULT_ACCUMULATORS = {
    "1": {"teams": [], "created_at": None},
    "2": {"teams": [], "created_at": None},
}

DEFAULT_ROSTER = {
    "1": [],
    "2": [],
}


class AccumulatorCommands(commands.GroupCog, name="acc"):
    def __init__(self, bot: commands.Bot, espn_client, poller, odds_client):
        self.bot = bot
        self.espn_client = espn_client
        self.poller = poller
        self.odds_client = odds_client
        super().__init__()

    @app_commands.command(description="Set the 4 teams for an accumulator slot")
    @app_commands.describe(
        slot="Which accumulator (1 or 2)",
        teams="4 team names, comma-separated, e.g. Arsenal, Leeds, Bolton, Salford",
    )
    async def set(self, interaction: discord.Interaction, slot: str, teams: str):
        if slot not in ACC_SLOTS:
            await interaction.response.send_message(f"Slot must be one of {ACC_SLOTS}.", ephemeral=True)
            return

        team_names = [t.strip() for t in teams.split(",") if t.strip()]
        if len(team_names) != TEAMS_PER_ACC:
            await interaction.response.send_message(
                f"Please provide exactly {TEAMS_PER_ACC} team names, got {len(team_names)}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        resolved = []
        problems = []
        for name in team_names:
            try:
                result = await resolve_team(self.espn_client, name)
                resolved.append(result)
            except TeamNotFoundError:
                problems.append(f"- **{name}**: not found")
            except AmbiguousTeamError as e:
                options = ", ".join(f"{m['name']} ({m['league_key']})" for m in e.matches)
                problems.append(f"- **{name}**: ambiguous, matches: {options}")

        if problems:
            await interaction.followup.send(
                "Couldn't set the accumulator — some teams didn't resolve cleanly:\n"
                + "\n".join(problems)
                + "\n\nTry a more specific name (e.g. the full club name)."
            )
            return

        roster = await storage.read_json(ROSTER_FILE, DEFAULT_ROSTER)
        slot_roster = roster.get(slot, [])
        for i, team in enumerate(resolved):
            # Snapshot the person for this position now, rather than
            # re-reading the roster later - if /acc setnames is run again
            # mid-bet, this accumulator should keep who was set when it
            # was set, not silently pick up the new roster.
            team["person"] = slot_roster[i] if i < len(slot_roster) else None

        # Independent per-team lookups - run them concurrently rather than
        # one at a time, so a 4-team accumulator isn't paying for 4
        # sequential network round-trips before it can respond.
        odds_results = await asyncio.gather(*(self._fetch_odds(team) for team in resolved))
        for team, (odds, bookmaker) in zip(resolved, odds_results):
            team["odds"], team["odds_bookmaker"] = odds, bookmaker

        accumulators = await storage.read_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        accumulators[slot] = {
            "teams": resolved,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
        await storage.write_json(ACCUMULATORS_FILE, accumulators)

        names = ", ".join(t["name"] for t in resolved)
        summary_lines = await self._team_summary_lines(resolved)
        await interaction.followup.send(
            f"Accumulator {slot} set: {names}\n\n" + "\n".join(summary_lines)
        )

    async def _fetch_odds(self, team: dict) -> tuple[float | None, str | None]:
        try:
            result = await self.odds_client.get_best_price(team["league_key"], team["name"])
        except OddsClientError:
            return None, None
        if result is None:
            return None, None
        return result

    @staticmethod
    def _person_label(people: dict, person_name: str | None) -> str:
        if not person_name:
            return "no name set"
        key = people_store.find_person_key(people, person_name)
        discord_user_id = people.get(key, {}).get("discord_user_id")
        if discord_user_id:
            return f"<@{discord_user_id}>"
        return person_name

    async def _fetch_fixture(self, team: dict):
        try:
            profile = await self.espn_client.get_team(team["league"], team["espn_id"])
        except EspnClientError:
            return None
        return next_fixture_from_team_profile(profile, team["name"])

    async def _team_summary_lines(self, teams: list[dict]) -> list[str]:
        """For each resolved team: who it's assigned to, its odds, and its
        next fixture from ESPN (Discord timestamp markup so it renders in
        each viewer's own timezone).
        """
        people = await people_store.load_people()
        fixtures = await asyncio.gather(*(self._fetch_fixture(team) for team in teams))

        lines = []
        for team, fixture in zip(teams, fixtures):
            person_label = self._person_label(people, team.get("person"))

            if team.get("odds"):
                odds_part = f"odds {team['odds']:.2f}"
                if team.get("odds_bookmaker"):
                    odds_part += f" ({team['odds_bookmaker']})"
            else:
                odds_part = "odds unavailable"

            if fixture is None:
                lines.append(f"- **{person_label}** — {team['name']}: no upcoming fixture found, {odds_part}")
                continue

            kickoff, opponent, is_home = fixture
            ts = int(kickoff.timestamp())
            vs_or_at = "vs" if is_home else "@"
            lines.append(
                f"- **{person_label}** — {team['name']} {vs_or_at} {opponent}: "
                f"<t:{ts}:F> (<t:{ts}:R>), {odds_part}"
            )
        return lines

    async def _kickoff_lines(self, teams: list[dict]) -> list[str]:
        """For each resolved team, fetch its next fixture from ESPN and
        return one human-readable line per team (Discord timestamp markup
        so it renders in each viewer's own timezone).
        """
        lines = []
        for team in teams:
            try:
                profile = await self.espn_client.get_team(team["league"], team["espn_id"])
            except EspnClientError:
                lines.append(f"- {team['name']}: couldn't fetch fixture info right now")
                continue

            fixture = next_fixture_from_team_profile(profile, team["name"])
            if fixture is None:
                lines.append(f"- {team['name']}: no upcoming fixture found")
                continue

            kickoff, opponent, is_home = fixture
            ts = int(kickoff.timestamp())
            vs_or_at = "vs" if is_home else "@"
            lines.append(f"- {team['name']} {vs_or_at} {opponent}: <t:{ts}:F> (<t:{ts}:R>)")
        return lines

    @app_commands.command(description="Check that 4 team names resolve, without saving them")
    @app_commands.describe(teams="4 team names, comma-separated")
    async def check(self, interaction: discord.Interaction, teams: str):
        team_names = [t.strip() for t in teams.split(",") if t.strip()]
        await interaction.response.defer(thinking=True)

        lines = []
        for name in team_names:
            try:
                result = await resolve_team(self.espn_client, name)
                lines.append(f"✅ **{name}** → {result['name']} ({result['league_key']})")
            except TeamNotFoundError:
                lines.append(f"❌ **{name}**: not found on any supported league")
            except AmbiguousTeamError as e:
                options = ", ".join(f"{m['name']} ({m['league_key']})" for m in e.matches)
                lines.append(f"⚠️ **{name}**: ambiguous — {options}")

        await interaction.followup.send("\n".join(lines))

    @app_commands.command(description="Set the 4 names to attribute a slot's teams to, in order")
    @app_commands.describe(
        slot="Which accumulator (1 or 2)",
        names="4 names, comma-separated, in the same order teams are given to /acc set",
    )
    async def setnames(self, interaction: discord.Interaction, slot: str, names: str):
        if slot not in ACC_SLOTS:
            await interaction.response.send_message(f"Slot must be one of {ACC_SLOTS}.", ephemeral=True)
            return

        name_list = [n.strip() for n in names.split(",") if n.strip()]
        if len(name_list) != TEAMS_PER_ACC:
            await interaction.response.send_message(
                f"Please provide exactly {TEAMS_PER_ACC} names, got {len(name_list)}.",
                ephemeral=True,
            )
            return

        roster = await storage.read_json(ROSTER_FILE, DEFAULT_ROSTER)
        roster[slot] = name_list
        await storage.write_json(ROSTER_FILE, roster)

        await interaction.response.send_message(
            f"Names for accumulator {slot} set: {', '.join(name_list)}\n"
            "These stick around across `/acc newbet` — the next `/acc set` for this slot will "
            "match team #1 to name #1, and so on."
        )

    @app_commands.command(description="Show the names currently set for an accumulator slot")
    @app_commands.describe(slot="Which accumulator (1 or 2)")
    async def names(self, interaction: discord.Interaction, slot: str):
        if slot not in ACC_SLOTS:
            await interaction.response.send_message(f"Slot must be one of {ACC_SLOTS}.", ephemeral=True)
            return

        roster = await storage.read_json(ROSTER_FILE, DEFAULT_ROSTER)
        slot_roster = roster.get(slot, [])
        if not slot_roster:
            await interaction.response.send_message(
                f"No names set for accumulator {slot} yet — use `/acc setnames`.", ephemeral=True
            )
            return

        await interaction.response.send_message(f"Accumulator {slot} names: {', '.join(slot_roster)}")

    @app_commands.command(description="Clear an accumulator slot")
    @app_commands.describe(slot="Which accumulator (1 or 2)")
    async def clear(self, interaction: discord.Interaction, slot: str):
        if slot not in ACC_SLOTS:
            await interaction.response.send_message(f"Slot must be one of {ACC_SLOTS}.", ephemeral=True)
            return

        accumulators = await storage.read_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        accumulators[slot] = {"teams": [], "created_at": None}
        await storage.write_json(ACCUMULATORS_FILE, accumulators)
        await interaction.response.send_message(f"Accumulator {slot} cleared.")

    @app_commands.command(description="Show current accumulator teams and their next kick-off times")
    async def show(self, interaction: discord.Interaction):
        accumulators = await storage.read_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        await interaction.response.defer(thinking=True)

        blocks = []
        for slot in ACC_SLOTS:
            acc = accumulators.get(slot, {"teams": []})
            teams = acc.get("teams", [])
            if not teams:
                blocks.append(f"**Accumulator {slot}**: (empty)")
                continue

            names = ", ".join(t["name"] for t in teams)
            fixture_lines = await self._kickoff_lines(teams)
            blocks.append(f"**Accumulator {slot}**: {names}\n" + "\n".join(fixture_lines))

        await interaction.followup.send("\n\n".join(blocks))

    @app_commands.command(
        description="Archive this bet's results and reset both slots, ready for the next one"
    )
    async def newbet(self, interaction: discord.Interaction):
        accumulators = await storage.read_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        if not any(acc.get("teams") for acc in accumulators.values()):
            await interaction.response.send_message(
                "Nothing to archive — both slots are already empty.", ephemeral=True
            )
            return

        match_state = await storage.read_json(MATCH_STATE_FILE, {})

        def _result_for(team_name: str):
            for entry in match_state.values():
                home, away = entry.get("home"), entry.get("away")
                if team_name not in (home, away):
                    continue
                opponent = away if home == team_name else home
                return {
                    "opponent": opponent,
                    "score": entry.get("score"),
                    "status": entry.get("status"),
                    "is_home": home == team_name,
                }
            return None

        archived_slots = {}
        for slot, acc in accumulators.items():
            archived_slots[slot] = {
                "teams": [{**team, "result": _result_for(team["name"])} for team in acc.get("teams", [])],
                "created_at": acc.get("created_at"),
            }

        archived_at = datetime.datetime.utcnow().isoformat()
        history = await storage.read_json(STANDINGS_FILE, [])
        history.append(
            {
                "archived_at": archived_at,
                "slots": archived_slots,
            }
        )
        await storage.write_json(STANDINGS_FILE, history)

        await self._archive_people_history(accumulators, _result_for, archived_at)

        await storage.write_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        await storage.write_json(MATCH_STATE_FILE, {})
        self.poller.clear_snapshots()

        await interaction.response.send_message(
            "Archived this bet's results and cleared both slots — set up the next one with `/acc set` "
            "whenever you're ready (works for a Saturday round or an adhoc midweek/cup bet alike)."
        )

    @staticmethod
    def _leg_result(result: dict | None, locked_win: bool) -> str:
        """Win/loss (or the raw match status if it never finished) for one
        leg, given the {opponent, score, status, is_home} dict `_result_for`
        already resolved for it. Uses the same is_winning_leg rule the
        poller's live status display uses (a draw counts as a loss, an
        early-payout lock counts as a win regardless of the final score),
        so the two can't drift apart.
        """
        if result is None:
            return "no_data"
        if result["status"] != "STATUS_FULL_TIME":
            return result["status"]

        try:
            left, right = (int(x) for x in result["score"].split("-"))
        except (AttributeError, ValueError, KeyError):
            return "unknown"

        team_score, opp_score = (left, right) if result["is_home"] else (right, left)
        return "win" if is_winning_leg(team_score, opp_score, locked_win) else "loss"

    async def _archive_people_history(self, accumulators: dict, result_for, archived_at: str):
        """Record each named team's leg into data/people.json, reusing the
        same {opponent, score, status} lookup `newbet` already ran against
        match_state.json for the standings.json archive. Teams with no
        name assigned (roster wasn't set when /acc set ran) are skipped -
        there's nobody to attribute them to.
        """
        people = await people_store.load_people()
        changed = False

        for slot, acc in accumulators.items():
            for team in acc.get("teams", []):
                person_name = team.get("person")
                if not person_name:
                    continue

                result = result_for(team["name"])
                key = people_store.find_person_key(people, person_name)
                entry = people.setdefault(key, people_store.new_person_entry())
                entry["history"].append(
                    {
                        "archived_at": archived_at,
                        "slot": slot,
                        "team": team["name"],
                        "opponent": result["opponent"] if result else None,
                        "score": result["score"] if result else None,
                        "result": self._leg_result(result, team.get("locked_win", False)),
                        "odds": team.get("odds"),
                    }
                )
                changed = True

        if changed:
            await people_store.save_people(people)

    @app_commands.command(description="Show recently archived bets")
    @app_commands.describe(count="How many recent bets to show (default 5)")
    async def history(self, interaction: discord.Interaction, count: int = 5):
        history_entries = await storage.read_json(STANDINGS_FILE, [])
        if not history_entries:
            await interaction.response.send_message(
                "No archived bets yet — use `/acc newbet` to archive one.", ephemeral=True
            )
            return

        count = max(1, min(count, len(history_entries)))
        recent = list(reversed(history_entries[-count:]))

        lines = []
        for entry in recent:
            lines.append(f"**Bet archived {entry.get('archived_at', 'unknown time')}**")
            for slot in ACC_SLOTS:
                slot_data = entry.get("slots", {}).get(slot, {"teams": []})
                teams = slot_data.get("teams", [])
                if not teams:
                    lines.append(f"  Accumulator {slot}: (empty)")
                    continue

                team_lines = []
                for team in teams:
                    result = team.get("result")
                    if result:
                        label = STATUS_LABELS.get(result.get("status"), result.get("status"))
                        team_lines.append(
                            f"{team['name']} {result.get('score')} vs {result['opponent']} ({label})"
                        )
                    else:
                        team_lines.append(f"{team['name']} (no data)")
                lines.append(f"  Accumulator {slot}: " + "; ".join(team_lines))
            lines.append("")

        message = "\n".join(lines).strip()
        if len(message) > 1900:
            message = message[:1900] + "\n… (truncated, try a smaller `count`)"
        await interaction.response.send_message(message)

    @app_commands.command(description="Set this channel as the destination for live match updates")
    async def setchannel(self, interaction: discord.Interaction):
        cfg = await storage.read_json(CONFIG_FILE, {})
        cfg["update_channel_id"] = interaction.channel_id
        await storage.write_json(CONFIG_FILE, cfg)
        await interaction.response.send_message(f"Live updates will be posted in {interaction.channel.mention}.")


async def setup(bot: commands.Bot, espn_client, poller, odds_client):
    await bot.add_cog(AccumulatorCommands(bot, espn_client, poller, odds_client))
