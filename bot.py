import asyncio
import contextlib
import logging
import os
from pathlib import Path

import discord
from discord import app_commands

BASE_DIR = Path(__file__).resolve().parent

TOKEN_FILE = Path(
    os.getenv("TOKEN_FILE", str(BASE_DIR / "token.txt"))
)
CHANNELS_FILE = Path(
    os.getenv("CHANNELS_FILE", str(BASE_DIR / "channels.txt"))
)
LOG_FILE = os.getenv(
    "MINECRAFT_LOG",
    "/minecraft-logs/latest.log",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minecraft_bot")


def load_channels():
    if not CHANNELS_FILE.exists():
        return set()

    return {
        int(line.strip())
        for line in CHANNELS_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    }


def save_channels(channels):
    CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CHANNELS_FILE.with_suffix(".tmp")

    temporary.write_text(
        "".join(f"{channel_id}\n" for channel_id in sorted(channels)),
        encoding="utf-8",
    )
    temporary.replace(CHANNELS_FILE)


class ChannelBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.tree = app_commands.CommandTree(self)
        self.enabled_channels = load_channels()
        self.channels_lock = asyncio.Lock()

        # Bounded memory buffer, not permanent storage.
        self.log_queue = asyncio.Queue(maxsize=10000)
        self.background_tasks = []

    async def setup_hook(self):
        await self.tree.sync()

        self.background_tasks = [
            asyncio.create_task(self.read_log()),
            asyncio.create_task(self.forward_logs()),
        ]

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)
        logger.info("Watching %s", LOG_FILE)
        logger.info("Saved channels: %s", len(self.enabled_channels))

    async def read_log(self):
        await self.wait_until_ready()

        while not self.is_closed():
            process = None

            try:
                process = await asyncio.create_subprocess_exec(
                    "tail",
                    "-n", "0",
                    "-F",
                    LOG_FILE,
                    stdout=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,
                )

                while True:
                    raw_line = await process.stdout.readline()

                    if not raw_line:
                        raise RuntimeError("Log reader exited")

                    line = raw_line.decode(
                        "utf-8",
                        errors="replace",
                    ).rstrip("\r\n")

                    print(f"[Minecraft] {line}", flush=True)

                    if not self.enabled_channels:
                        continue

                    # Prevent log contents from closing the code block.
                    line = line.replace("```", "'''")

                    # Split unusually long lines into safe message pieces.
                    text = line + "\n"

                    for start in range(0, len(text), 1800):
                        await self.log_queue.put(
                            text[start:start + 1800]
                        )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Log reader failed; retrying")
            finally:
                if process is not None and process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    await process.wait()

            await asyncio.sleep(5)

    async def forward_logs(self):
        await self.wait_until_ready()
        pending = None

        while True:
            batch = (
                pending
                if pending is not None
                else await self.log_queue.get()
            )
            pending = None

            # Collect nearby log lines into fewer Discord messages.
            await asyncio.sleep(2)
            await self.wait_until_ready()

            while not self.log_queue.empty():
                piece = self.log_queue.get_nowait()

                if len(batch) + len(piece) > 1800:
                    pending = piece
                    break

                batch += piece

            for channel_id in list(self.enabled_channels):
                # Respect /disable while earlier sends are in progress.
                if channel_id not in self.enabled_channels:
                    continue

                try:
                    channel = self.get_channel(channel_id)

                    if channel is None:
                        channel = await self.fetch_channel(channel_id)

                    if not isinstance(
                        channel,
                        (discord.TextChannel, discord.Thread),
                    ):
                        continue

                    await channel.send(
                        f"```text\n{batch}\n```",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

                except discord.Forbidden:
                    logger.warning(
                        "Missing access/send permission: %s",
                        channel_id,
                    )
                except discord.NotFound:
                    logger.warning(
                        "Channel no longer exists: %s",
                        channel_id,
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not send log batch to %s",
                        channel_id,
                    )

    async def close(self):
        for task in self.background_tasks:
            task.cancel()

        await asyncio.gather(
            *self.background_tasks,
            return_exceptions=True,
        )

        await super().close()


bot = ChannelBot()


async def update_channel(interaction, enable):
    channel_id = interaction.channel_id

    if interaction.guild_id is None or channel_id is None:
        await interaction.response.send_message(
            "Run this command in a server channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    async with bot.channels_lock:
        updated = bot.enabled_channels.copy()

        if enable:
            updated.add(channel_id)
        else:
            updated.discard(channel_id)

        try:
            await asyncio.to_thread(save_channels, updated)
        except OSError:
            logger.exception("Could not save channels")
            await interaction.followup.send(
                "Couldn't save channels. Check folder permissions.",
                ephemeral=True,
            )
            return

        bot.enabled_channels = updated

    await interaction.followup.send(
        "Minecraft log forwarding enabled here."
        if enable
        else "Minecraft log forwarding disabled here.",
        ephemeral=True,
    )


@bot.tree.command(
    name="enable",
    description="Forward Minecraft logs to this channel.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def enable(interaction: discord.Interaction):
    await update_channel(interaction, True)


@bot.tree.command(
    name="disable",
    description="Stop forwarding Minecraft logs here.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def disable(interaction: discord.Interaction):
    await update_channel(interaction, False)


@bot.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need Manage Channels permission."
    else:
        logger.error(
            "Command failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Command failed. Check the bot's terminal."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


if __name__ == "__main__":
    if not TOKEN_FILE.exists():
        raise SystemExit(f"Missing token file: {TOKEN_FILE}")

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if not token:
        raise SystemExit("Token file is empty.")

    bot.run(token)