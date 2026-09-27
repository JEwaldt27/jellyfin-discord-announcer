"""Announcements and maintenance notices.

Both the slash commands and the HTTP endpoint go through here, so a notice
posted from the shell looks identical to one posted from Discord and the two
paths cannot drift apart.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import discord

from db import Database, utcnow

log = logging.getLogger(__name__)

COLOR_ANNOUNCE = discord.Color(0x5865F2)     # blurple
COLOR_MAINTENANCE = discord.Color(0xE67E22)  # amber
COLOR_RESOLVED = discord.Color(0x57F287)     # green

# discord.Embed caps the description at 4096.
MESSAGE_LIMIT = 3500
TITLE_LIMIT = 250

_DURATION_RE = re.compile(r"^(\d+)\s*([mhd])?$", re.IGNORECASE)
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}

MAX_DURATION_MINUTES = 30 * 24 * 60


class AnnounceError(Exception):
    """Something the caller got wrong - safe to show them verbatim."""


def parse_duration(text: str) -> timedelta:
    """'30m' / '2h' / '1d' / bare minutes -> timedelta.

    Deliberately not bot.parse_interval: that one enforces a 15-minute floor
    because it governs how often Jellyfin is polled. A five-minute restart is
    a perfectly reasonable maintenance window.
    """
    match = _DURATION_RE.match(text.strip().lower())
    if not match:
        raise AnnounceError(
            "Use a number followed by m, h, or d — for example `20m`, `2h`, `1d`."
        )
    minutes = int(match.group(1)) * _UNIT_MINUTES[match.group(2) or "m"]
    if minutes <= 0:
        raise AnnounceError("That duration is zero. Give it at least a minute.")
    if minutes > MAX_DURATION_MINUTES:
        raise AnnounceError("That's longer than 30 days.")
    return timedelta(minutes=minutes)


def humanise(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"
    minutes, _ = divmod(total // 60, 1)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts) or "under a minute"


def _clean(text: str | None, limit: int, *, field: str) -> str:
    if text is None:
        raise AnnounceError(f"{field} is required.")
    text = text.strip()
    if not text:
        raise AnnounceError(f"{field} is empty.")
    if len(text) > limit:
        raise AnnounceError(f"{field} is too long ({len(text)} chars, max {limit}).")
    return text


def _stamp(embed: discord.Embed, author: str | None) -> discord.Embed:
    embed.timestamp = utcnow()
    if author:
        embed.set_footer(text=f"Posted by {author}")
    return embed


def build_announcement(
    message: str, *, title: str | None = None, author: str | None = None
) -> discord.Embed:
    message = _clean(message, MESSAGE_LIMIT, field="message")
    embed = discord.Embed(
        title=_clean(title, TITLE_LIMIT, field="title") if title else "📢 Announcement",
        description=message,
        colour=COLOR_ANNOUNCE,
    )
    return _stamp(embed, author)


def build_maintenance(
    reason: str, *, back_at: datetime | None = None, author: str | None = None
) -> discord.Embed:
    reason = _clean(reason, MESSAGE_LIMIT, field="reason")
    embed = discord.Embed(
        title="🔧 Maintenance starting",
        description=reason,
        colour=COLOR_MAINTENANCE,
    )
    if back_at:
        # A Discord timestamp renders in each reader's own timezone, which a
        # hardcoded "back at 9pm" cannot.
        embed.add_field(
            name="Expected back",
            value=f"<t:{int(back_at.timestamp())}:t> (<t:{int(back_at.timestamp())}:R>)",
            inline=False,
        )
    return _stamp(embed, author)


def build_resolved(
    *,
    note: str | None = None,
    started_at: datetime | None = None,
    reason: str | None = None,
    jump_url: str | None = None,
    author: str | None = None,
) -> discord.Embed:
    description = note.strip() if note and note.strip() else "Everything is back up."
    embed = discord.Embed(
        title="✅ Maintenance complete",
        description=description[:MESSAGE_LIMIT],
        colour=COLOR_RESOLVED,
    )
    if started_at:
        embed.add_field(
            name="Downtime", value=humanise(utcnow() - started_at), inline=True
        )
    if reason:
        embed.add_field(name="Was for", value=reason[:1000], inline=True)
    if jump_url:
        embed.add_field(name="Started", value=f"[original notice]({jump_url})", inline=True)
    return _stamp(embed, author)


def resolve_channel(guild: discord.Guild, channel_id: int | None):
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


async def announce_channel_for(db: Database, guild: discord.Guild):
    """The announcement channel, or None if it isn't set or has gone away."""
    cfg = await db.get_config(guild.id)
    return resolve_channel(guild, cfg.announce_channel_id)


async def send(channel, embed: discord.Embed) -> discord.Message:
    """Post an embed, converting Discord's failures into AnnounceError.

    The HTTP endpoint needs a reportable error rather than an exception that
    would surface to a shell script as an opaque 500.
    """
    try:
        return await channel.send(embed=embed)
    except discord.Forbidden as exc:
        raise AnnounceError(
            f"Missing permission to post in #{getattr(channel, 'name', '?')}."
        ) from exc
    except discord.HTTPException as exc:
        raise AnnounceError(f"Discord rejected the message: {exc}") from exc
