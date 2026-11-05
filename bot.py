import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
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

STATE_FILE = Path(
    os.getenv(
        "DASHBOARD_STATE_FILE",
        str(CHANNELS_FILE.parent / "dashboard.json"),
    )
)

KEY_FILE = Path(
    os.getenv("PTERO_API_KEY_FILE", "/run/secrets/ptero_api_key")
)

LOG_FILE = os.getenv(
    "MINECRAFT_LOG",
    "/minecraft-logs/latest.log",
)

PANEL_URL = os.getenv("PTERO_URL", "").rstrip("/")
SERVER_ID = os.getenv("PTERO_SERVER_ID", "").strip()

REFRESH_SECONDS = 15
CONSOLE_SECONDS = 30
FRESH_SECONDS = 90
UNEXPECTED_STOP_DELAY = 20

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minecraft_bot")

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

HEADER = re.compile(
    r"^\[[^\]]+\] \[[^\]]+/(INFO|WARN|ERROR|FATAL)\]:\s?(.*)$"
)

UPDATE_SUMMARY = re.compile(
    r"Available\s+(\d+)\s+updates?\s+from\s+(\d+)\s+installed mods",
    re.IGNORECASE,
)

PLAYER_SUMMARY = re.compile(
    r"^There are (\d+) of a max of (\d+) players online",
    re.IGNORECASE,
)

CRASH_EVIDENCE = re.compile(
    r"^(?:"
    r"Encountered an unexpected exception|"
    r"Exception in server tick loop|"
    r"This crash report has been saved to|"
    r"Crash report saved to"
    r")",
    re.IGNORECASE,
)


# ---------------- Storage ----------------

def load_state():
    if STATE_FILE.exists():
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        state.setdefault("messages", {})

        # Remove the old role configuration.
        state.pop("roles", None)
        return state

    state = {"messages": {}}

    # Import previously enabled channels once.
    if CHANNELS_FILE.exists():
        for line in CHANNELS_FILE.read_text(
            encoding="utf-8"
        ).splitlines():
            line = line.strip()

            if line.isdigit():
                state["messages"][line] = None

    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


# ---------------- Formatting ----------------

def safe_text(text, limit=220):
    text = ANSI.sub("", str(text))
    text = re.sub(r"§.", "", text)
    text = text.replace("```", "'''")
    text = discord.utils.escape_mentions(text)
    text = discord.utils.escape_markdown(text)
    return text[:limit]


def size_text(value):
    value = float(value)

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"

        value /= 1024


class PanelError(Exception):
    pass


# ---------------- Bot ----------------

class MonitorBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.tree = app_commands.CommandTree(self)
        self.settings = load_state()

        # Save migrated settings without the old roles.
        save_state(self.settings)

        # Discord already uses self.http internally.
        self.panel_session = None

        self.jobs = []
        self.dashboard_lock = asyncio.Lock()
        self.power_lock = asyncio.Lock()

        self.server_state = "unknown"
        self.last_known_state = None
        self.resources = {}

        self.panel_error = "Waiting for the first panel check"
        self.console_error = None
        self.log_error = "Waiting for the log reader"

        self.network_previous = None
        self.rx_rate = None
        self.tx_rate = None

        self.last_console_poll = 0

        self.stop_requested_until = 0
        self.unexpected_offline_since = None

        self.reset_log_stats()

    def reset_log_stats(self):
        self.warning_count = 0
        self.error_count = 0

        self.recent_warnings = deque(maxlen=2)
        self.recent_errors = deque(maxlen=2)

        self.update_count = None
        self.mod_count = None
        self.update_items = deque(maxlen=5)
        self.collect_updates = False
        self.update_log_time = None

        self.players = None
        self.players_at = 0

        self.tps = None
        self.tps_at = 0
        self.expect_tps_line = False

        self.graceful_stop_seen = False
        self.crash_reason = None

    async def setup_hook(self):
        self.panel_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )

        # Replaces the registered global commands.
        # /setrole is no longer included.
        await self.tree.sync()

        self.jobs = [
            asyncio.create_task(self.watch_log()),
            asyncio.create_task(self.dashboard_loop()),
        ]

        for job in self.jobs:
            job.add_done_callback(self.job_finished)

    def job_finished(self, job):
        if job.cancelled():
            return

        error = job.exception()

        if error is not None:
            logger.error(
                "Background task stopped unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)
        logger.info("Administrator-only dashboard mode")
        logger.info("Reading: %s", LOG_FILE)

    # ---------------- Panel API ----------------

    async def panel_request(self, method, endpoint, payload=None):
        if not PANEL_URL or not SERVER_ID:
            raise PanelError(
                "Panel URL or server ID is missing."
            )

        try:
            key = KEY_FILE.read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            raise PanelError(
                "Cannot read the Pterodactyl API key file."
            ) from None

        if not key:
            raise PanelError(
                "Pterodactyl API key file is empty."
            )

        url = (
            f"{PANEL_URL}/api/client/servers/"
            f"{SERVER_ID}/{endpoint}"
        )

        try:
            async with self.panel_session.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
                json=payload,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    descriptions = {
                        401: "API key rejected.",
                        403: "API account lacks permission.",
                        404: "Server or endpoint not found.",
                        409: "Action unavailable in the current server state.",
                        422: "Request parameters rejected.",
                        429: "Panel rate limit reached.",
                    }

                    detail = descriptions.get(
                        response.status,
                        "Check panel configuration and availability.",
                    )

                    raise PanelError(
                        f"Panel HTTP {response.status}: {detail}"
                    )

                if method == "GET":
                    return await response.json()

                return None

        except asyncio.TimeoutError:
            raise PanelError(
                "Panel request timed out. "
                "An action may still have been accepted."
            ) from None

        except aiohttp.ClientError as error:
            logger.warning(
                "Panel connection failed: %s",
                error,
            )

            raise PanelError(
                f"Panel connection failed: "
                f"{type(error).__name__}"
            ) from None

    async def console_command(self, command):
        command = command.strip()

        if not command or "\n" in command or "\r" in command:
            raise ValueError(
                "Expected one nonempty console command."
            )

        await self.panel_request(
            "POST",
            "command",
            {"command": command},
        )

    async def power(self, signal):
        if signal not in {"start", "stop"}:
            raise ValueError("Unsupported power action.")

        async with self.power_lock:
            await self.panel_request(
                "POST",
                "power",
                {"signal": signal},
            )

            if signal == "stop":
                self.stop_requested_until = time.monotonic() + 180
                self.unexpected_offline_since = None

            elif signal == "start":
                self.stop_requested_until = 0

    # ---------------- Metrics ----------------

    async def refresh_metrics(self):
        try:
            result = await self.panel_request(
                "GET",
                "resources",
            )

            attributes = result["attributes"]
            new_state = attributes["current_state"]
            old_state = self.last_known_state

            self.server_state = new_state
            self.resources = attributes.get("resources", {})
            self.panel_error = None

            now = time.monotonic()

            if new_state == "offline":
                if (
                    old_state in {"running", "starting"}
                    and not self.graceful_stop_seen
                    and now > self.stop_requested_until
                ):
                    self.unexpected_offline_since = now

            elif new_state in {"running", "starting"}:
                self.unexpected_offline_since = None

                if old_state == "offline":
                    self.graceful_stop_seen = False

            if self.graceful_stop_seen:
                self.unexpected_offline_since = None

            self.last_known_state = new_state

            rx = self.resources.get("network_rx_bytes")
            tx = self.resources.get("network_tx_bytes")

            self.rx_rate = None
            self.tx_rate = None

            if rx is not None and tx is not None:
                previous = self.network_previous

                if (
                    previous is not None
                    and old_state == new_state == "running"
                    and rx >= previous[1]
                    and tx >= previous[2]
                ):
                    elapsed = now - previous[0]

                    if elapsed > 0:
                        self.rx_rate = (
                            rx - previous[1]
                        ) / elapsed

                        self.tx_rate = (
                            tx - previous[2]
                        ) / elapsed

                self.network_previous = (now, rx, tx)

            else:
                self.network_previous = None

            if new_state != "running":
                self.players = None
                self.tps = None
                self.console_error = None
                self.last_console_poll = 0

            elif now - self.last_console_poll >= CONSOLE_SECONDS:
                self.last_console_poll = now

                try:
                    await self.console_command("list")
                    await self.console_command("spark tps")
                    self.console_error = None

                except PanelError as error:
                    self.console_error = str(error)

        except PanelError as error:
            self.panel_error = str(error)
            self.server_state = "unknown"
            self.resources = {}

            self.network_previous = None
            self.rx_rate = None
            self.tx_rate = None

    # ---------------- Log parsing ----------------

    def process_log_line(self, raw):
        line = ANSI.sub("", raw)
        line = re.sub(r"§.", "", line)

        header = HEADER.match(line)

        if not header:
            return

        level, message = header.groups()

        if re.match(
            r"Loading Minecraft .+ with Fabric Loader",
            message,
        ):
            self.reset_log_stats()

        if level == "WARN":
            self.warning_count += 1
            self.recent_warnings.append(message)

        elif level in {"ERROR", "FATAL"}:
            self.error_count += 1
            self.recent_errors.append(message)

        # Explicit crash evidence, not ordinary WARN/ERROR entries.
        if CRASH_EVIDENCE.match(message):
            self.crash_reason = message

        if re.match(
            r"^Stopping (?:the )?server(?:$|[.!])",
            message,
            re.IGNORECASE,
        ):
            self.graceful_stop_seen = True
            self.unexpected_offline_since = None

        if message.startswith("Fetching updates from "):
            self.collect_updates = True
            self.update_items.clear()
            self.update_count = None

        item = re.match(
            r"\s*-\s+(.+)\[(.+ -> .+)\]\s*$",
            message,
        )

        if self.collect_updates and item:
            self.update_items.append(
                f"{item.group(1).strip()}: "
                f"{item.group(2).strip()}"
            )

        summary = UPDATE_SUMMARY.search(message)

        if summary:
            self.update_count = int(summary.group(1))
            self.mod_count = int(summary.group(2))
            self.collect_updates = False

            timestamp = re.match(
                r"^\[([^\]]+)\]",
                line,
            )

            self.update_log_time = (
                timestamp.group(1)
                if timestamp
                else "unknown"
            )

        players = PLAYER_SUMMARY.search(message)

        if players:
            self.players = (
                int(players.group(1)),
                int(players.group(2)),
            )
            self.players_at = time.monotonic()

        if "TPS from last" in message:
            tail = message.rsplit(":", 1)[-1].strip()
            self.expect_tps_line = True

            if self.parse_tps_values(tail):
                self.expect_tps_line = False

        elif self.expect_tps_line:
            self.expect_tps_line = False

            candidate = re.sub(
                r"^\[[^\]]*\]\s*",
                "",
                message,
            ).strip()

            self.parse_tps_values(candidate)

    def parse_tps_values(self, text):
        text = text.strip()

        if not re.fullmatch(r"[\d\s.,*]+", text):
            return False

        values = re.findall(
            r"\d+(?:\.\d+)?",
            text,
        )

        if len(values) < 3:
            return False

        self.tps = " / ".join(values[:5])
        self.tps_at = time.monotonic()

        return True

    async def watch_log(self):
        await self.wait_until_ready()

        while not self.is_closed():
            process = None

            try:
                if not Path(LOG_FILE).is_file():
                    raise FileNotFoundError(LOG_FILE)

                self.reset_log_stats()

                # Replay the current log to recover DURW results,
                # warning/error counts, and crash evidence.
                process = await asyncio.create_subprocess_exec(
                    "tail",
                    "-n", "+1",
                    "-F",
                    LOG_FILE,
                    stdout=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,
                )

                while True:
                    raw = await process.stdout.readline()

                    if not raw:
                        raise RuntimeError(
                            "Minecraft log reader exited."
                        )

                    self.log_error = None

                    self.process_log_line(
                        raw.decode(
                            "utf-8",
                            errors="replace",
                        ).rstrip("\r\n")
                    )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                self.log_error = (
                    f"Log reader: {type(error).__name__}"
                )
                logger.exception(
                    "Log reader failed; retrying"
                )

            finally:
                if (
                    process is not None
                    and process.returncode is None
                ):
                    with contextlib.suppress(
                        ProcessLookupError
                    ):
                        process.terminate()

                    await process.wait()

            await asyncio.sleep(5)

    # ---------------- Dashboard ----------------

    def crash_status(self):
        now = time.monotonic()

        if self.crash_reason:
            if self.server_state == "offline":
                return (
                    "💥 Crash detected — server offline",
                    discord.Color.dark_red(),
                )

            if self.server_state in {"running", "starting"}:
                return (
                    "💥 Crash evidence in current log",
                    discord.Color.orange(),
                )

        if (
            self.server_state == "offline"
            and self.unexpected_offline_since is not None
            and now - self.unexpected_offline_since
            >= UNEXPECTED_STOP_DELAY
            and not self.graceful_stop_seen
            and now > self.stop_requested_until
        ):
            return (
                "💥 Possible crash / unexpected stop",
                discord.Color.dark_red(),
            )

        return None

    def build_embed(self):
        now = time.monotonic()

        labels = {
            "running": (
                "🟢 Running",
                discord.Color.green(),
            ),
            "starting": (
                "🟡 Starting",
                discord.Color.gold(),
            ),
            "stopping": (
                "🟠 Stopping",
                discord.Color.orange(),
            ),
            "offline": (
                "🔴 Offline",
                discord.Color.red(),
            ),
        }

        state_label, color = labels.get(
            self.server_state,
            (
                "⚪ Unknown / panel unavailable",
                discord.Color.greyple(),
            ),
        )

        crash = self.crash_status()

        # Do not label a currently running server as offline.
        if crash and self.server_state == "offline":
            state_label, color = crash

        embed = discord.Embed(
            title="🎮 Minecraft Server Dashboard",
            description=(
                "Live monitoring • edited every 15 seconds\n"
                "Server controls: administrators only"
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="📡 Server status",
            value=state_label,
            inline=False,
        )

        if crash:
            crash_detail = crash[0]

            if self.crash_reason:
                crash_detail += (
                    "\n" + safe_text(self.crash_reason, 300)
                )
            else:
                crash_detail += (
                    "\nNo graceful shutdown was observed. "
                    "This is an inference, not a confirmed exit cause."
                )

            embed.add_field(
                name="💥 Crash indicator",
                value=crash_detail[:1024],
                inline=False,
            )

        if self.update_count is None:
            updates = (
                "⏳ No completed DURW result "
                "found in the current log."
            )

        else:
            icon = "🟠" if self.update_count else "🟢"

            updates = (
                f"{icon} {self.update_count} updates / "
                f"{self.mod_count} installed mods\n"
                f"Latest logged check: {self.update_log_time}"
            )

            if self.update_items:
                updates += "\n" + "\n".join(
                    f"• {safe_text(item, 110)}"
                    for item in self.update_items
                )

        embed.add_field(
            name="📦 DURW updates",
            value=updates[:1024],
            inline=False,
        )

        cpu = self.resources.get("cpu_absolute")
        memory = self.resources.get("memory_bytes")

        embed.add_field(
            name="⚙️ CPU usage",
            value=(
                f"{cpu:.1f}%"
                if cpu is not None
                else "Unavailable"
            ),
            inline=True,
        )

        embed.add_field(
            name="🧠 Memory usage",
            value=(
                size_text(memory)
                if memory is not None
                else "Unavailable"
            ),
            inline=True,
        )

        if self.server_state == "offline":
            players = "0 — server offline"

        elif self.server_state != "running":
            players = "Unavailable"

        elif (
            self.players is not None
            and now - self.players_at < FRESH_SECONDS
        ):
            players = (
                f"{self.players[0]} / {self.players[1]}"
            )

        else:
            players = "Waiting for fresh console output"

        embed.add_field(
            name="👥 Players",
            value=players,
            inline=True,
        )

        rx = self.resources.get("network_rx_bytes")
        tx = self.resources.get("network_tx_bytes")
        network = "Unavailable"

        if rx is not None and tx is not None:
            network = (
                f"⬇️ Received: {size_text(rx)}\n"
                f"⬆️ Sent: {size_text(tx)}"
            )

            if self.rx_rate is not None:
                network += (
                    f"\nRate: ↓ {size_text(self.rx_rate)}/s"
                    f" • ↑ {size_text(self.tx_rate)}/s"
                )

        embed.add_field(
            name="🌐 Network — game container",
            value=network,
            inline=False,
        )

        if (
            self.server_state == "running"
            and self.tps is not None
            and now - self.tps_at < FRESH_SECONDS
        ):
            tps = (
                f"{self.tps}\n"
                "Spark samples: shortest to longest interval"
            )

        elif self.server_state != "running":
            tps = "Unavailable while server is not running"

        else:
            tps = (
                "Waiting for Spark output; "
                "unavailable if its format differs"
            )

        embed.add_field(
            name="⏱️ TPS",
            value=tps,
            inline=False,
        )

        warnings = (
            f"{self.warning_count} WARN entries "
            "in the current log"
        )

        if self.recent_warnings:
            warnings += "\n" + "\n".join(
                f"• {safe_text(item)}"
                for item in self.recent_warnings
            )

        errors = (
            f"{self.error_count} ERROR/FATAL entries "
            "in the current log"
        )

        if self.recent_errors:
            errors += "\n" + "\n".join(
                f"• {safe_text(item)}"
                for item in self.recent_errors
            )

        embed.add_field(
            name="⚠️ Warnings",
            value=warnings[:1024],
            inline=False,
        )

        embed.add_field(
            name="❌ Errors",
            value=errors[:1024],
            inline=False,
        )

        problems = [
            message
            for message in (
                self.panel_error,
                self.console_error,
                self.log_error,
            )
            if message
        ]

        if problems:
            embed.add_field(
                name="🛠️ Monitoring notes",
                value="\n".join(
                    safe_text(item)
                    for item in problems
                )[:1024],
                inline=False,
            )

        embed.set_footer(
            text=(
                "DURW = last logged check • "
                "CPU uses panel units • "
                "Log counts are not unresolved-issue counts"
            )
        )

        return embed

    async def publish_dashboard(self, channel_id, embed):
        async with self.dashboard_lock:
            key = str(channel_id)

            if key not in self.settings["messages"]:
                return

            channel = self.get_channel(channel_id)

            if channel is None:
                channel = await self.fetch_channel(channel_id)

            if not isinstance(
                channel,
                (discord.TextChannel, discord.Thread),
            ):
                return

            message_id = self.settings["messages"][key]

            if message_id:
                try:
                    message = channel.get_partial_message(
                        int(message_id)
                    )

                    await message.edit(
                        content=None,
                        embed=embed,
                        allowed_mentions=(
                            discord.AllowedMentions.none()
                        ),
                    )
                    return

                except discord.NotFound:
                    pass

            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

            self.settings["messages"][key] = message.id
            save_state(self.settings)

    async def dashboard_loop(self):
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                await self.refresh_metrics()
                embed = self.build_embed()

                for key in list(self.settings["messages"]):
                    try:
                        await self.publish_dashboard(
                            int(key),
                            embed,
                        )

                    except (
                        discord.HTTPException,
                        OSError,
                    ):
                        logger.exception(
                            "Dashboard update failed: %s",
                            key,
                        )

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Dashboard cycle failed"
                )

            await asyncio.sleep(REFRESH_SECONDS)

    async def close(self):
        for job in self.jobs:
            job.cancel()

        await asyncio.gather(
            *self.jobs,
            return_exceptions=True,
        )

        if self.panel_session is not None:
            await self.panel_session.close()

        await super().close()


bot = MonitorBot()


# ---------------- Administrator-only commands ----------------

@bot.tree.command(
    name="enable",
    description="Administrator: enable the dashboard in this channel.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def enable(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    key = str(interaction.channel_id)
    bot.settings["messages"].setdefault(key, None)
    save_state(bot.settings)

    await bot.publish_dashboard(
        interaction.channel_id,
        bot.build_embed(),
    )

    await interaction.followup.send(
        "✅ Dashboard enabled here. "
        "It updates automatically.",
        ephemeral=True,
    )


@bot.tree.command(
    name="disable",
    description="Administrator: stop this channel's dashboard.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def disable(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with bot.dashboard_lock:
        key = str(interaction.channel_id)
        message_id = bot.settings["messages"].pop(key, None)
        save_state(bot.settings)

        if message_id:
            try:
                message = interaction.channel.get_partial_message(
                    int(message_id)
                )

                disabled = discord.Embed(
                    title="⏸️ Minecraft Dashboard Disabled",
                    description=(
                        "Automatic updates have stopped in this channel.\n"
                        "An administrator can run /enable to resume."
                    ),
                    color=discord.Color.greyple(),
                    timestamp=datetime.now(timezone.utc),
                )

                await message.edit(
                    content=None,
                    embed=disabled,
                    allowed_mentions=(
                        discord.AllowedMentions.none()
                    ),
                )

            except discord.HTTPException:
                logger.warning(
                    "Could not mark disabled dashboard: %s",
                    key,
                )

    await interaction.followup.send(
        "⏸️ Dashboard disabled here.",
        ephemeral=True,
    )


@bot.tree.command(
    name="status",
    description="Administrator: show the latest dashboard privately.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def status(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=bot.build_embed(),
        ephemeral=True,
    )


async def request_power(interaction, signal):
    await interaction.response.defer(ephemeral=True)

    try:
        await bot.power(signal)

    except PanelError as error:
        await interaction.followup.send(
            str(error),
            ephemeral=True,
        )
        return

    logger.info(
        "%s requested by Discord user %s in guild %s",
        signal.upper(),
        interaction.user.id,
        interaction.guild_id,
    )

    await interaction.followup.send(
        "✅ Start request accepted."
        if signal == "start"
        else "🛑 Graceful stop request accepted.",
        ephemeral=True,
    )


@bot.tree.command(
    name="start",
    description="Administrator: start the Minecraft server.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def start(interaction: discord.Interaction):
    await request_power(interaction, "start")


@bot.tree.command(
    name="stop",
    description="Administrator: gracefully stop Minecraft.",
)
@app_commands.describe(
    confirm="Set True to confirm shutdown."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def stop(
    interaction: discord.Interaction,
    confirm: bool,
):
    if not confirm:
        await interaction.response.send_message(
            "No change. Use /stop confirm:True "
            "to confirm shutdown.",
            ephemeral=True,
        )
        return

    await request_power(interaction, "stop")


@bot.tree.error
async def command_error(interaction, error):
    if isinstance(
        error,
        app_commands.MissingPermissions,
    ):
        message = (
            "🔒 Only members with Administrator "
            "permission can use this command."
        )

    elif isinstance(
        error,
        app_commands.NoPrivateMessage,
    ):
        message = (
            "Run this command inside a Discord server."
        )

    else:
        logger.error(
            "Command failed",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )

        message = (
            "Command failed. Check the bot logs "
            "and Discord channel permissions."
        )

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )

    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


# ---------------- Startup ----------------

if __name__ == "__main__":
    if not TOKEN_FILE.is_file():
        raise SystemExit(
            f"Missing Discord token file: {TOKEN_FILE}"
        )

    token = TOKEN_FILE.read_text(
        encoding="utf-8"
    ).strip()

    if not token:
        raise SystemExit(
            "Discord token file is empty."
        )

    bot.run(token)