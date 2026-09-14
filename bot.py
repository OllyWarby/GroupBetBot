import asyncio
import logging

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, DEV_GUILD_ID
from services.espn_client import EspnClient
from services.odds_client import OddsClient
from services.poller import Poller
import cogs.commands as commands_cog
import cogs.person as person_cog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Slash commands only - no message content needed.
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)
espn_client = EspnClient()
odds_client = OddsClient()
poller = Poller(bot, espn_client)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

    if DEV_GUILD_ID:
        guild = discord.Object(id=int(DEV_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        logger.info("Synced %d command(s) to dev guild %s", len(synced), DEV_GUILD_ID)
    else:
        synced = await bot.tree.sync()
        logger.info("Synced %d command(s) globally (may take up to an hour to appear)", len(synced))

    poller.start()


async def main():
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set - copy .env.example to .env and fill it in.")

    async with bot:
        await commands_cog.setup(bot, espn_client, poller, odds_client)
        await person_cog.setup(bot)
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            await espn_client.close()
            await odds_client.close()


if __name__ == "__main__":
    asyncio.run(main())
