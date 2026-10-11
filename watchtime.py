"""Weekly watch-time leaderboard, built from what Jellyfin core actually keeps.

Accuracy note, because this is an estimate and the embed says so:

Jellyfin core stores no log of playback events. All it keeps per user per item
is Played, PlayCount, PlaybackPositionTicks and LastPlayedDate - and
LastPlayedDate is only the *most recent* play. So:

  - Something watched three times this week counts once. PlayCount is
    all-time, with no dates attached, so it cannot correct for that.
  - Anything marked played counts its full runtime, even if it was abandoned
    after ten minutes or marked watched in bulk.
  - Part-watched items contribute their real position instead, which is the
    one place the number is exact.

The Playback Reporting plugin would give true per-play durations. It is not
installed on this server, and this works without it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from jellyfin import WATCH_FIELDS, JellyfinClient

log = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000
PAGE = 100

# A user with a huge library could otherwise be paged through indefinitely.
# Sorted newest-first, the walk normally stops on the first page.
MAX_PAGES = 40

DEFAULT_DAYS = 7
MIN_DAYS = 1
MAX_DAYS = 90

MEDALS = ("🥇", "🥈", "🥉")

# Jellyfin emits 7-digit fractional seconds, which fromisoformat rejects.
_FRACTION = re.compile(r"\.(\d+)")


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6], value.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_duration(seconds: int) -> str:
    minutes = int(seconds // 60)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


@dataclass
class UserWatch:
    name: str
    user_id: str
    seconds: int = 0
    movies: int = 0
    episodes: int = 0
    partial: int = 0
    last_played: datetime | None = None

    @property
    def items(self) -> int:
        return self.movies + self.episodes

    def summary(self) -> str:
        bits = []
        if self.movies:
            bits.append(f"{self.movies} film{'s' if self.movies != 1 else ''}")
        if self.episodes:
            bits.append(f"{self.episodes} episode{'s' if self.episodes != 1 else ''}")
        if self.partial and not bits:
            bits.append(f"{self.partial} part-watched")
        return ", ".join(bits) or "nothing finished"


async def _collect_user(
    jf: JellyfinClient, name: str, user_id: str, cutoff: datetime
) -> UserWatch:
    watch = UserWatch(name=name, user_id=user_id)
    start = 0

    for _ in range(MAX_PAGES):
        items, total = await jf.items_page(
            include_types="Movie,Episode",
            fields=WATCH_FIELDS,
            start=start,
            limit=PAGE,
            sort_by="DatePlayed",
            sort_order="Descending",
            user_id=user_id,
            filters="IsPlayed",
        )
        if not items:
            break

        reached_cutoff = False
        for item in items:
            played_at = parse_date((item.get("UserData") or {}).get("LastPlayedDate"))
            if played_at is None:
                # No date to judge; keep walking rather than stopping early.
                continue
            if watch.last_played is None or played_at > watch.last_played:
                watch.last_played = played_at
            if played_at < cutoff:
                # Sorted newest-first, so everything below is older too.
                reached_cutoff = True
                break

            watch.seconds += int((item.get("RunTimeTicks") or 0) / TICKS_PER_SECOND)
            if item.get("Type") == "Movie":
                watch.movies += 1
            else:
                watch.episodes += 1

        start += len(items)
        if reached_cutoff or len(items) < PAGE or start >= total:
            break

    # Part-watched items contribute their actual position. Skip anything
    # already counted above, or a finished item still carrying a stale resume
    # position would be counted twice.
    for item in await jf.resume_items(user_id):
        data = item.get("UserData") or {}
        if data.get("Played"):
            continue
        played_at = parse_date(data.get("LastPlayedDate"))
        if played_at is None or played_at < cutoff:
            continue
        position = int((data.get("PlaybackPositionTicks") or 0) / TICKS_PER_SECOND)
        if position <= 0:
            continue
        watch.seconds += position
        watch.partial += 1
        if watch.last_played is None or played_at > watch.last_played:
            watch.last_played = played_at

    return watch


async def collect(jf: JellyfinClient, *, days: int = DEFAULT_DAYS) -> list[UserWatch]:
    """Watch time per user over the window, busiest first, idle users dropped."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = []
    for user in await jf.users():
        name, user_id = user.get("Name"), user.get("Id")
        if not name or not user_id:
            continue
        watch = await _collect_user(jf, name, user_id, cutoff)
        if watch.seconds > 0:
            results.append(watch)

    results.sort(key=lambda w: w.seconds, reverse=True)
    return results


def build_embed(rows: list[UserWatch], *, days: int, total_users: int) -> discord.Embed:
    window = "the last 7 days" if days == DEFAULT_DAYS else f"the last {days} days"

    if not rows:
        return discord.Embed(
            title="📺 Watch time",
            description=f"Nobody watched anything in {window}.",
            colour=discord.Color.greyple(),
        )

    lines = []
    for index, row in enumerate(rows):
        rank = MEDALS[index] if index < len(MEDALS) else f"`{index + 1}.`"
        lines.append(
            f"{rank} **{row.name}** — {format_duration(row.seconds)}\n"
            f" {row.summary()}"
        )

    total = sum(row.seconds for row in rows)
    embed = discord.Embed(
        title=f"📺 Watch time — {window}",
        description="\n".join(lines),
        colour=discord.Color(0x00A4DC),
    )
    embed.add_field(name="Total", value=format_duration(total), inline=True)
    embed.add_field(name="Watchers", value=f"{len(rows)} of {total_users}", inline=True)
    embed.set_footer(
        text="Estimated — Jellyfin records when something was last played, "
             "not every play, so repeats within the window count once."
    )
    return embed
