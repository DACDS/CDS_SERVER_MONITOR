import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.txt"
CHANNELS_FILE = BASE_DIR / "channels.txt"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_channels() -> set[int]:
    if not CHANNELS_FILE.exists():
        return set()

    return {
        int(line.strip())
        for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_channels(channels: set[int]) -> None:
    # Replace the old file only after the new contents are written.
    temp_file = CHANNELS_FILE.with_suffix(".tmp")
    contents = "".join(f"{channel_id}\n" for channel_id in sorted(channels))
    temp_file.write_text(contents, encoding="utf-8")
    temp_file.replace(CHANNELS_FILE)


class ChannelBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.enabled_channels = load_channels()
        self.channels_lock = asyncio.Lock()

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print(f"Enabled channels: {len(self.enabled_channels)}")


bot = ChannelBot()


async def update_channel(
    interaction: discord.Interaction,
    enable: bool,
) -> None:
    channel_id = interaction.channel_id

    if interaction.guild_id is None or channel_id is None:
        await interaction.response.send_message(
            "Run this command in a server channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    async with bot.channels_lock:
        already_enabled = channel_id in bot.enabled_channels

        if already_enabled == enable:
            message = (
                "This channel is already enabled."
                if enable
                else "This channel is already disabled."
            )
        else:
            updated = bot.enabled_channels.copy()

            if enable:
                updated.add(channel_id)
            else:
                updated.discard(channel_id)

            try:
                await asyncio.to_thread(save_channels, updated)
            except OSError:
                logger.exception("Could not save the channel list")
                await interaction.followup.send(
                    "I couldn't save the channel list. "
                    "Check the bot's folder permissions.",
                    ephemeral=True,
                )
                return

            bot.enabled_channels = updated
            message = (
                "Enabled this channel and saved its ID."
                if enable
                else "Disabled this channel and removed its ID."
            )

    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="enable", description="Save this channel for later.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def enable(interaction: discord.Interaction):
    await update_channel(interaction, enable=True)


@bot.tree.command(name="disable", description="Remove this saved channel.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def disable(interaction: discord.Interaction):
    await update_channel(interaction, enable=False)


@bot.tree.error
async def on_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need Manage Channels permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "Run this command in a server channel."
    else:
        logger.error(
            "Command failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Something went wrong. Check the bot's terminal."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN_FILE.exists():
        raise SystemExit("Create token.txt beside bot.py and put your token in it.")

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if not token:
        raise SystemExit("token.txt is empty.")

    bot.run(token)