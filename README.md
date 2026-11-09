# CDS Server Monitor
This project is not an official Discord, Minecraft, Pterodactyl, Spark, or DURW product.

### Notice: project uses HTTP thus should only be used local (for now)

A Discord bot for monitoring and controlling a Pterodactyl-managed Minecraft server.

The bot displays a dashboard that updates in place instead of posting every Minecraft log line as a separate message. All Discord commands require Administrator permission.

## Features

- Automatically edited dashboard, refreshed approximately every 15 seconds.
- Server status: running, starting, stopping, offline, or unavailable.
- Crash indicators based on Minecraft log evidence and unexpected offline transitions.
- CPU and memory usage from Pterodactyl.
- Network totals and calculated transfer rates.
- Player count from Minecraft's `list` command.
- TPS samples from `spark tps`.
- DURW update counts and recently listed updates.
- Warning and error counts with recent messages.
- Start and graceful-stop commands.
- Persistent dashboard channel and message settings.
- Separate files for the Discord token and Pterodactyl client API key.
- Docker Compose deployment with a read-only Minecraft log mount.

## AI Use Notice

This project was developed with **AI-assisted** code generation, documentation, and troubleshooting. however was reviewed by a human.
(but you know still be careful random niche repos)

## Requirements

- A Linux host running the Minecraft server's Pterodactyl Wings node.
- Docker Engine and Docker Compose.
- A Discord bot application and its bot token.
- A Pterodactyl client API key belonging to an account with access to the server.
- Read access to the Minecraft server's `logs` directory.
- Spark installed on Minecraft for TPS reporting.
- DURW installed and writing update results to `latest.log` for update reporting.

The current deployment expects the bot and Minecraft server files to be on the same host.

The Pterodactyl API account needs permission to:

- Read server resource usage.
- Start and stop the server.
- Send Minecraft console commands.

The bot does not require the Docker socket.

## Repository Files

The repository's root must contain:

```text
bot.py
Dockerfile
requirements.txt
.dockerignore
.gitignore
README.md
```

### requirements.txt

```text
discord.py>=2.0,<3.0
aiohttp>=3.9,<4.0
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
```

The bot uses `tail`, supplied by `coreutils`, to follow the Minecraft log.

### .gitignore and .dockerignore

Include these entries in both files:

```text
.git/
.env
token.txt
ptero_api_key.txt
channels.txt
data/
__pycache__/
*.pyc
```

Never commit secrets. Ignore rules do not remove secrets that have already been committed; revoke and replace any exposed credentials.

## Installation

### 1. Create the deployment directory

Run on the Wings host:

```bash
mkdir -p ~/cds-monitor/data
cd ~/cds-monitor
chmod 700 .
chmod 700 data
```

The deployment directory will contain:

```text
cds-monitor/
├── compose.yaml
├── .env
├── token.txt
├── ptero_api_key.txt
└── data/
    └── dashboard.json
```

`dashboard.json` is created automatically.

### 2. Create and invite the Discord bot

In the Discord Developer Portal:

1. Create or select an application.
2. Open its Bot settings.
3. Generate/reset the bot token and copy it.
4. Generate an invitation using the `bot` and `applications.commands` scopes.
5. Invite the bot to your Discord server.

Give the bot access to its dashboard channel with:

- View Channel.
- Send Messages.
- Embed Links.
- Send Messages in Threads, if using a thread.

The bot does not need Discord Administrator permission.

Its users do need Administrator permission to run commands.

Save the token:

```bash
nano token.txt
```

Paste only the token, without quotes or a `Bot` prefix.

```bash
chmod 600 token.txt
```

### 3. Create a Pterodactyl client API key

Sign in to your Pterodactyl user account and open:

```text
Account Settings → API Credentials
```

This is normally available at:

```text
http://YOUR-PANEL/account/api
```

Create a client API key and save it:

```bash
nano ptero_api_key.txt
chmod 600 ptero_api_key.txt
```

Paste only the key, without quotes or a `Bearer` prefix.

Do not use:

- A Discord token.
- A Wings node token.
- An Application API key from the administrator area.

Prefer a dedicated Pterodactyl user limited to the monitored server and necessary permissions.

### 4. Locate the Minecraft log

On a standard Wings installation, try:

```bash
sudo find /var/lib/pterodactyl/volumes \
  -type f -path '*/logs/latest.log'
```

An example result:

```text
/var/lib/pterodactyl/volumes/SERVER-UUID/logs/latest.log
```

Use the directory ending in `/logs` for the mount, not the individual file.

If Wings uses a custom server-data location, search that location instead.

### 5. Create .env

```bash
nano .env
```

Example:

```dotenv
MINECRAFT_LOG_DIR=/var/lib/pterodactyl/volumes/YOUR-SERVER-UUID/logs

PTERO_URL=http://YOUR-PANEL-ADDRESS
PTERO_SERVER_ID=YOUR-SHORT-SERVER-ID
```

For a server page such as:

```text
http://192.168.68.85/server/0189b057
```

use:

```dotenv
PTERO_URL=http://192.168.68.85
PTERO_SERVER_ID=0189b057
```

Do not include `/server/...` or `/api` in `PTERO_URL`.

Use the actual panel protocol and port. Do not point the bot at Wings' API port instead of the panel.

No Discord server ID is required.

```bash
chmod 600 .env
```

### 6. Create compose.yaml

```yaml
services:
  monitor:
    build:
      context: [https://github.com/DACDS/CDS_SERVER_MONITOR.git](https://github.com/DACDS/CDS_SERVER_MONITOR.git)
      dockerfile: Dockerfile

    image: cds-server-monitor:local
    restart: unless-stopped
    init: true

    environment:
      TOKEN_FILE: /run/secrets/discord_token
      CHANNELS_FILE: /data/channels.txt
      MINECRAFT_LOG: /minecraft-logs/latest.log
      PTERO_URL: ${PTERO_URL:?Set PTERO_URL in .env}
      PTERO_SERVER_ID: ${PTERO_SERVER_ID:?Set PTERO_SERVER_ID in .env}
      PTERO_API_KEY_FILE: /run/secrets/ptero_api_key

    volumes:
      - type: bind
        source: ./token.txt
        target: /run/secrets/discord_token
        read_only: true
        bind:
          create_host_path: false

      - type: bind
        source: ./ptero_api_key.txt
        target: /run/secrets/ptero_api_key
        read_only: true
        bind:
          create_host_path: false

      - type: bind
        source: ./data
        target: /data

      - type: bind
        source: ${MINECRAFT_LOG_DIR:?Set MINECRAFT_LOG_DIR in .env}
        target: /minecraft-logs
        read_only: true
        bind:
          create_host_path: false

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

This builds from the GitHub repository's default branch. It does not download a prebuilt image.

If using a fork, change `build.context`.

```bash
chmod 600 compose.yaml
```

### 7. Build and start

```bash
unset PTERO_URL

docker compose --env-file .env config --quiet

docker compose --env-file .env build --no-cache --pull monitor

docker compose --env-file .env up -d monitor

docker compose logs -f --tail=100 monitor
```

The `unset` prevents an exported `PTERO_URL` in your shell from overriding the value in `.env`.

Press `Ctrl+C` to stop following logs. The detached container continues running.

### 8. Enable the dashboard

As a Discord administrator, run this in the desired channel:

```text
/enable
```

The bot creates one dashboard message and edits it automatically.

Run `/enable` in additional channels to create additional dashboards.

## Commands

| Command | Description | Required permission |
|---|---|---|
| `/enable` | Enable the dashboard in the current channel | Administrator |
| `/disable` | Stop this channel's dashboard updates | Administrator |
| `/status` | Show a private copy of the latest dashboard | Administrator |
| `/start` | Request Minecraft startup | Administrator |
| `/stop confirm:True` | Request graceful shutdown | Administrator |

Role-based access and `/setrole` are not included.

Members without Administrator may still read the dashboard if they can view its channel.

Administrators in any Discord server where you install the bot can control the same configured Minecraft server.

## Dashboard Details

### Status and crash indicators

| Indicator | Meaning |
|---|---|
| 🟢 Running | Pterodactyl reports the process running |
| 🟡 Starting | Pterodactyl reports startup |
| 🟠 Stopping | Pterodactyl reports shutdown |
| 🔴 Offline | Offline without detected crash evidence |
| ⚪ Unknown | Panel state is unavailable |
| 💥 Crash detected | Offline with explicit Minecraft crash-log evidence |
| 💥 Possible crash / unexpected stop | Observed offline transition without a recognized graceful shutdown |

Crash detection is heuristic. It does not inspect Wings' exit codes or out-of-memory events. A fast crash/restart between polls or a host failure may be missed.

A running process is not a guarantee that Minecraft has finished loading.

### DURW updates

Shows DURW's latest completed update result found in `latest.log`.

The bot does not independently check update services or install updates. If no matching result exists, it reports that the result is unavailable.

### CPU, memory, and network

These are measurements for the Minecraft container, not the Discord bot or entire host.

CPU uses Pterodactyl's reported units and is not normalized by this bot.

Network transfer rates are calculated between consecutive counter samples. The first sample cannot provide a rate.

### Players and TPS

While running, the bot requests these console commands approximately every 30 seconds:

```text
list
spark tps
```

Player and TPS values become unavailable when responses are missing or stale.

The parsers expect supported English console output. Mod or translation changes may require parser updates.

### Warnings and errors

Counts matching `WARN`, `ERROR`, and `FATAL` log entries and shows recent messages.

The bot replays the existing current log at startup to recover information, but does not post raw log lines.

Counts do not represent unresolved issues. Multiline stack traces are not counted as separate errors.

## Updating

Push changes to the GitHub branch used by the build, then run:

```bash
cd ~/cds-monitor

unset PTERO_URL

docker compose --env-file .env build --no-cache --pull monitor

docker compose --env-file .env up -d --force-recreate monitor

docker compose logs -f --tail=100 monitor
```

Because the build context points to GitHub, editing an unrelated local `bot.py` does not update the image.

For `.env` or mounted secret-file changes, recreate the container; an image rebuild is unnecessary:

```bash
docker compose --env-file .env up -d --force-recreate monitor
```

## Persistent Data

Dashboard channel and message IDs are stored in:

```text
data/dashboard.json
```

If present on the first run, the previous `data/channels.txt` is imported. Afterward, `dashboard.json` is authoritative.

Back up the `data` directory. Do not delete it unless you intend to reset dashboard settings.

## Troubleshooting

### Panel connection failure

Check the configured address:

```bash
docker compose exec -T monitor printenv PTERO_URL
```

Confirm the protocol is correct:

```text
http://192.168.68.85
```

is different from:

```text
[https://192.168.68.85](https://192.168.68.85)
```

Inside a normally networked container, `localhost` refers to the bot container, not the host.

### HTTP 401 or 403

Check that the mounted file contains the correct client API key and that its account can access the server and perform the requested action.

Never paste keys into public troubleshooting messages.

### Dashboard fails to send or edit

Check the bot's access to the channel, its Embed Links permission, and applicable thread permissions.

Review logs:

```bash
docker compose logs --tail=100 monitor
```

### Missing TPS or player count

Check that the API user can send console commands and that `list` and `spark tps` produce the expected output in `latest.log`.

### Log permission errors

Confirm the host log directory is correct and readable by the container.

Do not use `chmod -R 777` on Minecraft files. Rootless containers and SELinux systems may need additional configuration; do not blindly relabel Wings' live server directories.

### Voice dependency warnings

Warnings about PyNaCl or davey concern optional voice functionality. This bot does not use voice.

## Security Notes

- Keep tokens, API keys, `.env`, and persistent data out of Git.
- Revoke and replace exposed secrets.
- Use HTTPS for panel API traffic where possible. HTTP sends API credentials without TLS protection.
- Do not disable TLS verification as a permanent workaround.
- Do not mount the Docker socket into the bot.
- Keep dashboard channels private if warnings or errors contain sensitive details.
- A private Discord bot limits installation; it does not itself authorize commands.
- Review changes before rebuilding from the repository's moving default branch.
- For controlled deployments, pin the build context to a reviewed commit.

## Limitations

This project is a convenience dashboard, not a high-availability monitoring platform.

It does not provide guaranteed alert delivery, a historical metrics database, automatic mod updates, arbitrary Linux shell execution, or definitive crash accounting.

Keep independent backups and monitoring for important servers.
