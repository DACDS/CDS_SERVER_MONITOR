import asyncio
import contextlib
import logging
import os
from pathlib import Path

import aiohttp
import discord
from discord import app_commands


# ---------------- Configuration ----------------

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

PTERO_URL = os.getenv("PTERO_URL", "").rstrip("/")
PTERO_SERVER_ID = os.getenv("PTERO_SERVER_ID", "").strip()

PTERO_API_KEY_FILE = Path(
    os.getenv(
        "PTERO_API_KEY_FILE",
        "/run/secrets/ptero_api_key",
    )
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minecraft_bot")


# ---------------- Saved channels ----------------

def load_channels():
    if not CHANNELS_FILE.exists():
        return set()

    channels = set()

    for line in CHANNELS_FILE.read_text(
        encoding="utf-8"
    ).splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            channels.add(int(line))
        except ValueError:
            logger.warning("Ignoring invalid channel ID: %r", line)

    return channels


def save_channels(channels):
    CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary = CHANNELS_FILE.with_suffix(".tmp")

    temporary.write_text(
        "".join(f"{channel_id}\n" for channel_id in sorted(channels)),
        encoding="utf-8",
    )

    temporary.replace(CHANNELS_FILE)


class PanelError(Exception):
    pass


# ---------------- Bot ----------------

class ChannelBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.tree = app_commands.CommandTree(self)

        self.enabled_channels = load_channels()
        self.channels_lock = asyncio.Lock()
        self.power_lock = asyncio.Lock()

        self.log_queue = asyncio.Queue(maxsize=10000)
        self.background_tasks = []
        self.panel_session = None

    async def setup_hook(self):
        self.panel_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        )

        await self.tree.sync()

        self.background_tasks = [
            asyncio.create_task(self.read_log()),
            asyncio.create_task(self.forward_logs()),
        ]

        for task in self.background_tasks:
            task.add_done_callback(self.background_task_done)

    def background_task_done(self, task):
        if task.cancelled():
            return

        error = task.exception()

        if error is not None:
            logger.error(
                "Background task stopped unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)
        logger.info("Watching: %s", LOG_FILE)
        logger.info("Saved channels: %s", len(self.enabled_channels))

    # ---------------- Pterodactyl API ----------------

    async def panel_post(self, endpoint, payload):
        if not PTERO_URL or not PTERO_SERVER_ID:
            raise PanelError(
                "Set PTERO_URL and PTERO_SERVER_ID first."
            )

        try:
            api_key = PTERO_API_KEY_FILE.read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            raise PanelError(
                "Cannot read the Pterodactyl API key file."
            ) from None

        if not api_key:
            raise PanelError("The Pterodactyl API key file is empty.")

        url = (
            f"{PTERO_URL}/api/client/servers/"
            f"{PTERO_SERVER_ID}/{endpoint}"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

        try:
            async with self.panel_session.post(
                url,
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as response:
                if 200 <= response.status < 300:
                    return

                messages = {
                    401: "Pterodactyl rejected the API key.",
                    403: "The API account lacks permission for this action.",
                    404: "Server or API endpoint not found. Check the panel URL and server ID.",
                    409: "The server cannot perform that action in its current state.",
                    422: "Pterodactyl rejected the request parameters.",
                    429: "Pterodactyl rate limit reached. Try again later.",
                }

                raise PanelError(
                    messages.get(
                        response.status,
                        f"Pterodactyl returned HTTP {response.status}.",
                    )
                )

        except asyncio.TimeoutError:
            raise PanelError(
                "Request timed out. Check the panel before retrying; "
                "the action may already have been accepted."
            ) from None
        except aiohttp.ClientError:
            raise PanelError(
                "Could not connect to the panel. "
                "Check its URL, networking, and TLS certificate."
            ) from None

    async def set_power(self, signal):
        if signal not in {"start", "stop"}:
            raise ValueError("Unsupported power signal")

        async with self.power_lock:
            await self.panel_post("power", {"signal": signal})

    async def send_console_command(self, command):
        """Send a Minecraft console command, not a Linux shell command."""
        command = command.strip()

        if not command:
            raise ValueError("Console command cannot be empty")

        if "\n" in command or "\r" in command:
            raise ValueError("Only one console command is allowed")

        await self.panel_post("command", {"command": command})

    # ---------------- Log reader ----------------

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

                    # Prevent log text from closing the Discord code block.
                    text = line.replace("```", "'''") + "\n"

                    # Split long lines to keep Discord messages small.
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

            # Batch nearby lines rather than sending one message per line.
            await asyncio.sleep(2)
            await self.wait_until_ready()

            while not self.log_queue.empty():
                piece = self.log_queue.get_nowait()

                if len(batch) + len(piece) > 1800:
                    pending = piece
                    break

                batch += piece

            for channel_id in list(self.enabled_channels):
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
                        "Missing Discord channel permissions: %s",
                        channel_id,
                    )
                except discord.NotFound:
                    logger.warning(
                        "Discord channel no longer exists: %s",
                        channel_id,
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Failed sending logs to channel %s",
                        channel_id,
                    )

    async def close(self):
        for task in self.background_tasks:
            task.cancel()

        await asyncio.gather(
            *self.background_tasks,
            return_exceptions=True,
        )

        if self.panel_session is not None:
            await self.panel_session.close()

        await super().close()


bot = ChannelBot()


# ---------------- Enable / disable ----------------

async def update_channel(interaction, enable):
    if interaction.guild_id is None or interaction.channel_id is None:
        await interaction.response.send_message(
            "Run this command in a Discord server channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    async with bot.channels_lock:
        updated = bot.enabled_channels.copy()

        if enable:
            updated.add(interaction.channel_id)
        else:
            updated.discard(interaction.channel_id)

        try:
            await asyncio.to_thread(save_channels, updated)
        except OSError:
            logger.exception("Could not save channel list")
            await interaction.followup.send(
                "Could not save channels. Check folder permissions.",
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


# ---------------- Start / stop ----------------

async def request_power(interaction, signal):
    await interaction.response.defer(ephemeral=True)

    try:
        await bot.set_power(signal)
    except PanelError as error:
        await interaction.followup.send(
            str(error),
            ephemeral=True,
        )
        return

    logger.info(
        "%s requested by Discord user %s in server %s",
        signal.upper(),
        interaction.user.id,
        interaction.guild_id,
    )

    message = (
        "Start request accepted. Watch the logs for startup progress."
        if signal == "start"
        else "Graceful stop request accepted. Watch the logs for shutdown progress."
    )

    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(
    name="start",
    description="Start the Minecraft server.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def start(interaction: discord.Interaction):
    await request_power(interaction, "start")


@bot.tree.command(
    name="stop",
    description="Gracefully stop the Minecraft server.",
)
@app_commands.describe(
    confirm="Set to True to confirm stopping the Minecraft server."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def stop(interaction: discord.Interaction, confirm: bool):
    if not confirm:
        await interaction.response.send_message(
            "Nothing changed. Use /stop confirm:True to stop the server.",
            ephemeral=True,
        )
        return

    await request_power(interaction, "stop")


# ---------------- Command errors ----------------

@bot.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "Run this command in a Discord server."
    else:
        logger.error(
            "Discord command failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Command failed. Check the bot's container logs."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


# ---------------- Startup ----------------

if __name__ == "__main__":
    if not TOKEN_FILE.exists():
        raise SystemExit(f"Missing Discord token file: {TOKEN_FILE}")

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if not token:
        raise SystemExit("Discord token file is empty.")

    bot.run(token)
