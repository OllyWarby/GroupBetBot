import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import ACCUMULATORS_FILE, CONFIG_FILE, MATCH_STATE_FILE, STANDINGS_FILE, ACC_SLOTS, TEAMS_PER_ACC
from services.espn_client import EspnClientError
from services.event_detector import next_fixture_from_team_profile
from services.poller import STATUS_LABELS
from services.team_resolver import resolve_team, TeamNotFoundError, AmbiguousTeamError
import storage

DEFAULT_ACCUMULATORS = {
    "1": {"teams": [], "created_at": None},
    "2": {"teams": [], "created_at": None},
}


class AccumulatorCommands(commands.GroupCog, name="acc"):
    def __init__(self, bot: commands.Bot, espn_client, poller):
        self.bot = bot
        self.espn_client = espn_client
        self.poller = poller
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

        accumulators = await storage.read_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        accumulators[slot] = {
            "teams": resolved,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
        await storage.write_json(ACCUMULATORS_FILE, accumulators)

        names = ", ".join(t["name"] for t in resolved)
        fixture_lines = await self._kickoff_lines(resolved)
        await interaction.followup.send(
            f"Accumulator {slot} set: {names}\n\n**Kick-off times:**\n" + "\n".join(fixture_lines)
        )

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
                if entry.get("home") == team_name or entry.get("away") == team_name:
                    opponent = entry["away"] if entry["home"] == team_name else entry["home"]
                    return {"opponent": opponent, "score": entry.get("score"), "status": entry.get("status")}
            return None

        archived_slots = {}
        for slot, acc in accumulators.items():
            archived_slots[slot] = {
                "teams": [{**team, "result": _result_for(team["name"])} for team in acc.get("teams", [])],
                "created_at": acc.get("created_at"),
            }

        history = await storage.read_json(STANDINGS_FILE, [])
        history.append(
            {
                "archived_at": datetime.datetime.utcnow().isoformat(),
                "slots": archived_slots,
            }
        )
        await storage.write_json(STANDINGS_FILE, history)

        await storage.write_json(ACCUMULATORS_FILE, DEFAULT_ACCUMULATORS)
        await storage.write_json(MATCH_STATE_FILE, {})
        self.poller.clear_snapshots()

        await interaction.response.send_message(
            "Archived this bet's results and cleared both slots — set up the next one with `/acc set` "
            "whenever you're ready (works for a Saturday round or an adhoc midweek/cup bet alike)."
        )

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


async def setup(bot: commands.Bot, espn_client, poller):
    await bot.add_cog(AccumulatorCommands(bot, espn_client, poller))
