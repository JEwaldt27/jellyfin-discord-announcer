"""Jellyfin -> Discord new media announcer.

Polls a Jellyfin server on a configurable schedule and posts an embed for every
movie and TV season that has appeared since the last scan.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shlex
import sys
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from db import (
    DEFAULT_SCAN_MINUTES,
    MAX_SCAN_MINUTES,
    MIN_SCAN_MINUTES,
    Database,
    utcnow,
)
from jellyfin import JellyfinClient, JellyfinError
from scanner import run_scan

try:  # convenience when running outside Docker
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("announcer")

TICK_SECONDS = 60
RETRY_DELAY = timedelta(minutes=15)

# /imdb runs this as a subprocess rather than importing it: the script is a
# synchronous stdlib CLI that calls sys.exit() and does blocking network and
# filesystem I/O. A subprocess keeps all of that off the event loop and means
# a crash in it can never take the bot down.
IMDB_SCRIPT = Path(__file__).resolve().parent / "jellyfin_imdb_rename.py"

# Generous: the script waits for a full Jellyfin library scan before it starts,
# and a rename pass over a large library on a slow share is not quick. Discord
# lets us follow up on a deferred interaction for 15 minutes, so stay under it.
IMDB_TIMEOUT = 13 * 60

# discord.Embed hard limit is 4096; leave room for the code fence and a note.
EMBED_DESC_LIMIT = 3800

# Everything that changes state is gated on this role: /imdb (renames files on
# the media share), /rebaseline (wipes the record of everything announced),
# /scanrate and /channels (bot configuration). /start, /status and /help stay
# open to any member. Enforced by the bot itself,
# which means it holds even for server admins and cannot be undone from
# Discord's Integrations UI. Override with ADMIN_ROLE in .env; a restart
# picks it up.
ADMIN_ROLE = os.environ.get("ADMIN_ROLE", "").strip() or "Media Admin"

_INTERVAL_RE = re.compile(r"^(\d+)\s*([mhd])?$", re.IGNORECASE)
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}


# --------------------------------------------------------------------- utils


def parse_interval(text: str) -> int:
    """'30m' / '6h' / '24h' / '7d' / bare minutes -> minutes."""
    match = _INTERVAL_RE.match(text.strip().lower())
    if not match:
        raise ValueError(
            "Use a number followed by m, h, or d — for example `30m`, `6h`, `24h`, `7d`."
        )
    amount = int(match.group(1))
    minutes = amount * _UNIT_MINUTES[match.group(2) or "m"]
    if minutes < MIN_SCAN_MINUTES:
        raise ValueError(f"That's too frequent. The minimum is {MIN_SCAN_MINUTES} minutes.")
    if minutes > MAX_SCAN_MINUTES:
        raise ValueError("That's too long. The maximum is 30 days.")
    return minutes


def format_interval(minutes: int) -> str:
    days, remainder = divmod(minutes, 1440)
    hours, mins = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts) or "0 minutes"


def relative(when: datetime | None) -> str:
    return f"<t:{int(when.timestamp())}:R>" if when else "—"


def missing_permissions(guild: discord.Guild, channel: discord.abc.GuildChannel) -> list[str]:
    perms = channel.permissions_for(guild.me)
    required = {
        "View Channel": perms.view_channel,
        "Send Messages": perms.send_messages,
        "Embed Links": perms.embed_links,
        "Attach Files": perms.attach_files,
    }
    return [name for name, granted in required.items() if not granted]


def collapse_carriage_returns(raw: str) -> str:
    """
    Flatten a captured console stream into what a terminal would have shown.

    jellyfin_imdb_rename.py draws its progress ticker with `end="\r"`, so it
    rewrites one line in place. Captured through a pipe those rewrites all
    survive, and a 400-movie run turns into 400 lines of noise. Keeping only
    the last segment of each line reproduces the final on-screen state.
    """
    out = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.split("\r")[-1].rstrip()
        if line.strip():
            out.append(line)
    return "\n".join(out)


async def run_imdb_script(
    *, apply_changes: bool = False, limit: int = 0, timeout: float = IMDB_TIMEOUT
) -> tuple[int | None, str, float]:
    """
    Run jellyfin_imdb_rename.py and capture its console output.

    Returns (returncode, output, elapsed_seconds). A returncode of None means
    it hit the timeout and was killed.
    """
    if not IMDB_SCRIPT.exists():
        return 1, f"{IMDB_SCRIPT.name} is not present in the image.", 0.0

    argv = [sys.executable, "-u", str(IMDB_SCRIPT)]
    if apply_changes:
        argv.append("--apply")
    if limit:
        argv += ["--limit", str(limit)]

    log.info("running: %s", shlex.join(argv))
    started = asyncio.get_running_loop().time()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(IMDB_SCRIPT.parent),
    )

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        elapsed = asyncio.get_running_loop().time() - started
        return None, f"Timed out after {int(elapsed)}s and was stopped.", elapsed

    elapsed = asyncio.get_running_loop().time() - started
    return proc.returncode, collapse_carriage_returns(stdout.decode("utf-8", "replace")), elapsed


def imdb_summary_fields(output: str) -> dict[str, str]:
    """Pull the script's SUMMARY block out into {label: value} for embed fields."""
    fields: dict[str, str] = {}
    in_summary = False
    for line in output.split("\n"):
        if line.strip() == "SUMMARY":
            in_summary = True
            continue
        if in_summary:
            if not line.startswith("  ") or line.startswith("  DRY RUN"):
                break
            label, sep, value = line.partition(":")
            if sep and value.strip():
                fields[label.strip()] = value.strip()
    return fields


def command_required_role(command: app_commands.Command) -> str | None:
    """
    The role name an app command is gated on, or None if it is open.

    Read off the command's own checks rather than kept in a second list, so
    /help can never drift from what is actually enforced. app_commands.checks
    .has_role stores the role in its predicate's closure; that is an internal
    detail, so a failure to read it degrades to "gated, role unknown" instead
    of taking the command down.
    """
    for check in getattr(command, "checks", []):
        closure = getattr(check, "__closure__", None) or []
        for cell in closure:
            value = cell.cell_contents
            if isinstance(value, str):
                return value
        if closure:
            return "?"
    return None


def walk_commands(tree: app_commands.CommandTree):
    """Yield (qualified_name, description, required_role) for every command."""
    def visit(cmd, prefix=""):
        name = f"{prefix}{cmd.name}"
        if isinstance(cmd, app_commands.Group):
            for sub in cmd.commands:
                yield from visit(sub, prefix=f"{name} ")
        else:
            yield name, cmd.description, command_required_role(cmd)

    for cmd in sorted(tree.get_commands(), key=lambda c: c.name):
        yield from visit(cmd)


# Longer explanations for /help. Anything without an entry falls back to the
# command's own description, so a new command still shows up here on its own.
HELP_DETAIL = {
    "start": "Run a scan right now instead of waiting for the schedule. New "
             "movies and TV seasons get announced in the configured channels.",
    "status": "Current settings, when the last and next scans are, how many "
              "items are tracked, and whether Jellyfin is reachable.",
    "scanrate": "How often to scan automatically — `30m`, `6h`, `24h`, `7d`. "
                "Minimum 15 minutes, default 24 hours.",
    "channels movies": "Channel where new movie announcements are posted.",
    "channels shows": "Channel where new TV announcements are posted.",
    "imdb": "Looks up an IMDb ID for every movie Jellyfin has none for, then "
            "renames the files to `Title (Year) [imdbid-tt…]`. Scans the "
            "Jellyfin library first, then reports what it found. **Dry run "
            "unless you pass `apply: True`** — it renames real files on the "
            "media share.",
    "rebaseline": "Forgets everything announced so far and re-records the "
                  "library as the new starting point. Nothing is posted for "
                  "existing media. Needs `confirm: True`.",
    "help": "This message.",
}


# ----------------------------------------------------------------- the client


class Announcer(discord.Client):
    def __init__(
        self,
        *,
        db: Database,
        jf: JellyfinClient,
        max_posts: int,
        public_url: str | None,
    ):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = db
        self.jf = jf
        self.max_posts = max_posts
        self.public_url = public_url
        self.server_id: str | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._synced: set[int] = set()
        # The rename script mutates files on the share. One at a time, server
        # wide — it operates on the whole Jellyfin library, not per guild.
        self._imdb_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.ticker.start()

    async def close(self) -> None:
        await super().close()
        await self.jf.close()
        await self.db.close()

    async def on_ready(self) -> None:
        log.info("Connected to Discord as %s", self.user)
        try:
            info = await self.jf.system_info()
            self.server_id = info.get("Id")
            log.info(
                "Jellyfin reachable: '%s' (version %s)",
                info.get("ServerName"),
                info.get("Version"),
            )
        except JellyfinError as exc:
            log.error("Jellyfin check failed at startup: %s", exc)

        for guild in self.guilds:
            await self._sync_guild(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined guild %s (%s)", guild.name, guild.id)
        await self._sync_guild(guild)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        if guild.id in self._synced:
            return
        target = discord.Object(id=guild.id)
        self.tree.copy_global_to(guild=target)
        try:
            await self.tree.sync(guild=target)
            self._synced.add(guild.id)
            log.info("Slash commands registered in %s", guild.name)
            # A typo'd role name fails closed and silently — nobody can run
            # the gated commands and the error just says they lack a role that
            # isn't there. Say so once at startup instead.
            if not discord.utils.get(guild.roles, name=ADMIN_ROLE):
                log.warning(
                    "%s has no '%s' role — nobody can use /imdb or /rebaseline "
                    "there until it exists",
                    guild.name,
                    ADMIN_ROLE,
                )
        except discord.HTTPException as exc:
            log.error("Could not register commands in %s: %s", guild.name, exc)

    def is_scanning(self, guild_id: int) -> bool:
        lock = self._locks.get(guild_id)
        return lock is not None and lock.locked()

    async def scan_guild(self, guild: discord.Guild):
        """Run one scan, then schedule the next based on the outcome."""
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            cfg = await self.db.get_config(guild.id)
            result = await run_scan(
                guild=guild,
                jf=self.jf,
                db=self.db,
                max_posts=self.max_posts,
                public_url=self.public_url,
                server_id=self.server_id,
            )
            interval = timedelta(minutes=cfg.scan_interval_min)
            # A failed scan retries soon instead of waiting out the full cadence.
            delay = interval if result.ok else min(interval, RETRY_DELAY)
            await self.db.record_scan(
                guild.id, status=result.status_line(), next_scan_at=utcnow() + delay
            )
            log.info("Scan of %s finished: %s", guild.name, result.status_line())
            return result

    @tasks.loop(seconds=TICK_SECONDS)
    async def ticker(self) -> None:
        for guild in list(self.guilds):
            try:
                cfg = await self.db.get_config(guild.id)
                if not cfg.is_due() or self.is_scanning(guild.id):
                    continue
                log.info("Scheduled scan starting for %s", guild.name)
                await self.scan_guild(guild)
            except Exception:  # a bad guild must not kill the scheduler
                log.exception("Scheduled scan for %s blew up", guild.id)

    @ticker.before_loop
    async def _before_ticker(self) -> None:
        await self.wait_until_ready()


# -------------------------------------------------------------- app commands


def register_commands(bot: Announcer) -> None:
    tree = bot.tree

    @tree.command(name="help", description="What every command does and who can use it")
    @app_commands.guild_only()
    async def help_(interaction: discord.Interaction) -> None:
        member = interaction.user
        held = {r.name for r in getattr(member, "roles", [])}

        embed = discord.Embed(
            title="Finster — Jellyfin announcer",
            description=(
                "Posts an embed when new movies or TV seasons appear in Jellyfin. "
                "Replies are only visible to you."
            ),
            colour=discord.Color.blurple(),
        )

        gated_roles: set[str] = set()
        for name, description, role in walk_commands(tree):
            detail = HELP_DETAIL.get(name, description)
            if role:
                gated_roles.add(role)
                mark = "✅" if role in held else "🔒"
                detail = f"{detail}\n*Needs the **{role}** role.* {mark}"
            embed.add_field(name=f"/{name}", value=detail, inline=False)

        if gated_roles:
            missing = sorted(r for r in gated_roles if r not in held)
            if missing:
                footer = "🔒 you don't have: " + ", ".join(missing)
            else:
                footer = "✅ you have every role these commands need"
        else:
            footer = "Every command is open to all members."
        embed.set_footer(text=footer)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="start", description="Scan Jellyfin for new media right now")
    @app_commands.guild_only()
    async def start(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if bot.is_scanning(guild.id):
            await interaction.response.send_message(
                "A scan is already running. Give it a moment.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot.scan_guild(guild)
        await interaction.followup.send(result.summary(), ephemeral=True)

    @tree.command(name="scanrate", description="Set how often Jellyfin is scanned")
    @app_commands.describe(interval="How often to scan, e.g. 30m, 6h, 24h, 7d")
    @app_commands.guild_only()
    @app_commands.checks.has_role(ADMIN_ROLE)
    async def scanrate(interaction: discord.Interaction, interval: str) -> None:
        try:
            minutes = parse_interval(interval)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        cfg = await bot.db.set_interval(interaction.guild.id, minutes)
        await interaction.response.send_message(
            f"✅ Scanning every **{format_interval(minutes)}**.\n"
            f"Next scan {relative(cfg.next_scan_at)}.",
            ephemeral=True,
        )

    @tree.command(name="status", description="Show the announcer's current settings")
    @app_commands.guild_only()
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        cfg = await bot.db.get_config(guild.id)
        movies_seen, episodes_seen = await bot.db.seen_count(guild.id)

        try:
            info = await bot.jf.system_info()
            jellyfin_state = f"🟢 `{info.get('ServerName')}` (v{info.get('Version')})"
        except JellyfinError as exc:
            jellyfin_state = f"🔴 {exc}"

        embed = discord.Embed(title="Jellyfin announcer", color=discord.Color.blurple())
        embed.add_field(name="Jellyfin", value=jellyfin_state, inline=False)
        embed.add_field(
            name="Movie channel",
            value=f"<#{cfg.movie_channel_id}>" if cfg.movie_channel_id else "*not set*",
            inline=True,
        )
        embed.add_field(
            name="Show channel",
            value=f"<#{cfg.show_channel_id}>" if cfg.show_channel_id else "*not set*",
            inline=True,
        )
        embed.add_field(
            name="Scan rate", value=format_interval(cfg.scan_interval_min), inline=True
        )
        embed.add_field(name="Last scan", value=relative(cfg.last_scan_at), inline=True)
        embed.add_field(
            name="Next scan",
            value="running now" if bot.is_scanning(guild.id) else relative(cfg.next_scan_at),
            inline=True,
        )
        embed.add_field(
            name="Tracked",
            value=f"{movies_seen:,} movies · {episodes_seen:,} episodes",
            inline=True,
        )
        if cfg.last_scan_status:
            embed.add_field(name="Last result", value=cfg.last_scan_status, inline=False)
        if not cfg.baselined:
            embed.set_footer(text="Not baselined yet - the next scan records your library.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(
        name="imdb",
        description="Match movies to IMDb IDs and rename the files to match",
    )
    @app_commands.describe(
        apply="Set to True to actually rename files. Default is a dry run.",
        limit="Only process the first N movies. 0 = the whole library.",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_role(ADMIN_ROLE)
    async def imdb(
        interaction: discord.Interaction, apply: bool = False, limit: int = 0
    ) -> None:
        if bot._imdb_lock.locked():
            await interaction.response.send_message(
                "An IMDb run is already in progress. Give it a moment.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with bot._imdb_lock:
            code, output, elapsed = await run_imdb_script(
                apply_changes=apply, limit=max(0, limit)
            )

        if code == 0:
            colour = discord.Color.green() if apply else discord.Color.blurple()
        elif code is None:
            colour = discord.Color.orange()
        else:
            colour = discord.Color.red()

        embed = discord.Embed(
            title="IMDb rename — applied" if apply else "IMDb rename — dry run",
            colour=colour,
        )

        for label, value in imdb_summary_fields(output).items():
            embed.add_field(name=label, value=value, inline=True)

        if code is None:
            embed.add_field(name="Result", value="🟠 Timed out", inline=False)
        elif code != 0:
            embed.add_field(name="Result", value=f"🔴 Exited with code {code}", inline=False)

        note = None
        if len(output) <= EMBED_DESC_LIMIT:
            embed.description = f"```\n{output}\n```"
        else:
            # Too long to inline. Show the tail — the summary and anything that
            # went wrong live at the end — and attach the whole thing.
            tail = output[-EMBED_DESC_LIMIT:]
            tail = tail.split("\n", 1)[1] if "\n" in tail else tail
            embed.description = f"```\n...\n{tail}\n```"
            note = discord.File(
                io.BytesIO(output.encode("utf-8")), filename="imdb_rename.log"
            )

        embed.set_footer(
            text=f"took {elapsed:.0f}s"
            + ("" if apply else " · nothing was changed — run with apply: True to commit")
        )

        if note:
            await interaction.followup.send(embed=embed, file=note, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(
        name="rebaseline",
        description="Forget everything announced so far and re-record the library",
    )
    @app_commands.describe(confirm="Set to True to confirm - the next scan posts nothing")
    @app_commands.guild_only()
    @app_commands.checks.has_role(ADMIN_ROLE)
    async def rebaseline(interaction: discord.Interaction, confirm: bool = False) -> None:
        if not confirm:
            await interaction.response.send_message(
                "This clears the record of everything already announced and re-reads "
                "your whole library as the new starting point. Nothing is posted for "
                "existing media. Run it again with `confirm: True` if that's what you want.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.db.clear_seen(interaction.guild.id)
        await bot.db.set_baselined(interaction.guild.id, False)
        result = await bot.scan_guild(interaction.guild)
        await interaction.followup.send(result.summary(), ephemeral=True)

    channels = app_commands.Group(
        name="channels",
        description="Choose where new media announcements are posted",
        guild_only=True,
    )

    async def _set_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        kind: str,
    ) -> None:
        missing = missing_permissions(interaction.guild, channel)
        if missing:
            await interaction.response.send_message(
                f"❌ I can't post in {channel.mention} — missing: "
                + ", ".join(f"**{m}**" for m in missing),
                ephemeral=True,
            )
            return

        if kind == "movies":
            await bot.db.set_movie_channel(interaction.guild.id, channel.id)
        else:
            await bot.db.set_show_channel(interaction.guild.id, channel.id)

        await interaction.response.send_message(
            f"✅ New {kind} will be announced in {channel.mention}.", ephemeral=True
        )

    @channels.command(name="movies", description="Where new movies are announced")
    @app_commands.describe(channel="Channel to post movie announcements in")
    @app_commands.checks.has_role(ADMIN_ROLE)
    async def channels_movies(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await _set_channel(interaction, channel, "movies")

    @channels.command(name="shows", description="Where new TV episodes are announced")
    @app_commands.describe(channel="Channel to post TV announcements in")
    @app_commands.checks.has_role(ADMIN_ROLE)
    async def channels_shows(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await _set_channel(interaction, channel, "shows")

    tree.add_command(channels)

    @tree.error
    async def on_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (app_commands.MissingRole, app_commands.MissingAnyRole)):
            # Without this branch a denied /imdb would fall through to the
            # generic handler below and read as a bug — "Something went wrong"
            # plus a logged traceback — rather than a refusal.
            message = f"You need the **{ADMIN_ROLE}** role to use this."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You don't have permission to use this."
        else:
            log.exception("Command failed", exc_info=error)
            message = f"Something went wrong: `{error}`"

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


# ---------------------------------------------------------------------- main


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    token = _require("DISCORD_TOKEN")
    jellyfin_url = _require("JELLYFIN_URL")
    api_key = _require("JELLYFIN_API_KEY")

    public_url = os.environ.get("JELLYFIN_PUBLIC_URL", "").strip() or None
    if public_url and not public_url.lower().startswith(("http://", "https://")):
        public_url = "https://" + public_url
        log.warning(
            "JELLYFIN_PUBLIC_URL had no scheme - assuming %s. "
            "Set it explicitly in .env if that's wrong.",
            public_url,
        )
    db_path = os.environ.get("DB_PATH", "/data/bot.db")
    try:
        max_posts = max(1, int(os.environ.get("MAX_POSTS_PER_SCAN", "20")))
    except ValueError:
        max_posts = 20

    log.info(
        "Starting announcer (jellyfin=%s, db=%s, cap=%d/scan, default rate=%s)",
        jellyfin_url,
        db_path,
        max_posts,
        format_interval(DEFAULT_SCAN_MINUTES),
    )

    bot = Announcer(
        db=Database(db_path),
        jf=JellyfinClient(jellyfin_url, api_key),
        max_posts=max_posts,
        public_url=public_url,
    )
    register_commands(bot)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
