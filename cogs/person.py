import discord
from discord import app_commands
from discord.ext import commands

import services.people_store as people_store

RESULT_LABELS = {
    "win": "✅ win",
    "loss": "❌ loss",
    "unknown": "❔ unknown",
    "no_data": "❔ no data",
}


def _label_for(result: str) -> str:
    return RESULT_LABELS.get(result, result)


class PersonCommands(commands.GroupCog, name="person"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(description="Link a name (as used in /acc setnames) to a Discord member")
    @app_commands.describe(
        name="The name as it appears in /acc setnames",
        user="The Discord member this name belongs to",
    )
    async def link(self, interaction: discord.Interaction, name: str, user: discord.Member):
        people = await people_store.load_people()
        key = people_store.find_person_key(people, name)
        entry = people.setdefault(key, people_store.new_person_entry())
        entry["discord_user_id"] = user.id
        await people_store.save_people(people)

        await interaction.response.send_message(
            f"Linked **{key}** to {user.mention} — future accumulator summaries will @-mention them."
        )

    @app_commands.command(description="Show a person's long-term win/loss history")
    @app_commands.describe(
        name="The name as it appears in /acc setnames",
        count="How many recent entries to show (default 10)",
    )
    async def history(self, interaction: discord.Interaction, name: str, count: int = 10):
        people = await people_store.load_people()
        key = people_store.find_person_key(people, name)
        entry = people.get(key)

        if not entry or not entry.get("history"):
            await interaction.response.send_message(
                f"No history recorded for **{name}** yet — this fills in after `/acc newbet`.",
                ephemeral=True,
            )
            return

        history = entry["history"]
        wins = sum(1 for h in history if h.get("result") == "win")
        losses = sum(1 for h in history if h.get("result") == "loss")
        pending = len(history) - wins - losses

        count = max(1, min(count, len(history)))
        recent = list(reversed(history[-count:]))

        lines = [f"**{key}** — {wins}W-{losses}L" + (f" ({pending} unresolved)" if pending else "")]
        for h in recent:
            score = h.get("score") or "?"
            opponent = h.get("opponent") or "?"
            odds = h.get("odds")
            odds_part = f", odds {odds:.2f}" if odds else ""
            lines.append(
                f"- {h.get('archived_at', 'unknown time')}: {h.get('team')} {score} vs {opponent} "
                f"({_label_for(h.get('result'))}{odds_part})"
            )

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1900] + "\n… (truncated, try a smaller `count`)"
        await interaction.response.send_message(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(PersonCommands(bot))
