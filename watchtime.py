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

from jellyfin import WATCH_FIELDS, JellyfinClient, JellyfinError, JellyfinNotFound

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


async def collect_from_core(
    jf: JellyfinClient, *, days: int = DEFAULT_DAYS
) -> list[UserWatch]:
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


# ------------------------------------------------------- Playback Reporting

QUERY_PATH = "/user_usage_stats/submit_custom_query"

# The plugin's response spells it "colums".
_RESULTS_KEY = "results"

_SQL_TIME = "%Y-%m-%d %H:%M:%S"


async def _query(jf: JellyfinClient, sql: str) -> list[list[str]]:
    payload = await jf.post_json(
        QUERY_PATH, {"CustomQueryString": sql, "ReplaceUserId": False}
    )
    return payload.get(_RESULTS_KEY) or []


def _parse_sql_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:19], _SQL_TIME)
    except ValueError:
        return None


@dataclass
class PluginWindow:
    """What history the plugin holds, and which clock it writes in."""

    rows: int
    earliest: datetime | None
    # SQLite datetime() arguments naming the clock the plugin writes in,
    # e.g. "'now'" or "'now','localtime'". Kept as arguments rather than a
    # finished expression so the window query reads datetime('now', '-7 days')
    # instead of nesting one datetime() call inside another.
    now_args: str

    def covers(self, now: datetime | None, days: int) -> bool:
        if not self.rows or self.earliest is None or now is None:
            return False
        return self.earliest <= now - timedelta(days=days)


async def plugin_window(jf: JellyfinClient) -> tuple[PluginWindow, datetime | None]:
    """Inspect the plugin's table, and work out whether it logs UTC or local.

    Playback Reporting has written DateCreated in local time in some releases
    and UTC in others. Rather than guess, compare the newest row against both
    clocks and keep whichever it sits closer to - a wrong guess would shift the
    window boundary by the server's UTC offset.
    """
    rows = await _query(
        jf,
        "SELECT COUNT(*), MIN(DateCreated), MAX(DateCreated), "
        "datetime('now'), datetime('now','localtime') FROM PlaybackActivity",
    )
    if not rows:
        return PluginWindow(0, None, "'now'"), None

    count, earliest, latest, utc_now, local_now = (list(rows[0]) + [None] * 5)[:5]
    count = int(count or 0)
    earliest_dt = _parse_sql_time(earliest)
    latest_dt = _parse_sql_time(latest)
    utc_dt = _parse_sql_time(utc_now)
    local_dt = _parse_sql_time(local_now)

    now_args, reference = "'now'", utc_dt
    if count and latest_dt and utc_dt and local_dt:
        if abs((latest_dt - local_dt).total_seconds()) < abs(
            (latest_dt - utc_dt).total_seconds()
        ):
            now_args, reference = "'now','localtime'", local_dt
            log.debug("Playback Reporting appears to log local time")

    return PluginWindow(count, earliest_dt, now_args), reference


async def collect_from_plugin(
    jf: JellyfinClient, *, days: int, window: PluginWindow
) -> list[UserWatch]:
    """Real per-play durations, grouped by user and item type."""
    names = {u.get("Id"): u.get("Name") for u in await jf.users() if u.get("Id")}

    # days is an int validated by the command before it ever reaches here.
    rows = await _query(
        jf,
        "SELECT UserId, ItemType, SUM(PlayDuration), MAX(DateCreated) "
        "FROM PlaybackActivity "
        f"WHERE DateCreated >= datetime({window.now_args}, '-{int(days)} days') "
        "GROUP BY UserId, ItemType",
    )

    by_user: dict[str, UserWatch] = {}
    for row in rows:
        user_id, item_type, seconds, last = (list(row) + [None] * 4)[:4]
        if not user_id:
            continue
        watch = by_user.get(user_id)
        if watch is None:
            watch = UserWatch(name=names.get(user_id) or "Unknown", user_id=user_id)
            by_user[user_id] = watch

        watch.seconds += int(float(seconds or 0))
        if item_type == "Movie":
            watch.movies += 1
        elif item_type == "Episode":
            watch.episodes += 1

        played_at = _parse_sql_time(last)
        if played_at and (watch.last_played is None
                          or played_at.replace(tzinfo=timezone.utc) > watch.last_played):
            watch.last_played = played_at.replace(tzinfo=timezone.utc)

    results = [w for w in by_user.values() if w.seconds > 0]
    results.sort(key=lambda w: w.seconds, reverse=True)
    return results


@dataclass
class Report:
    rows: list[UserWatch]
    exact: bool
    note: str | None = None


async def collect(jf: JellyfinClient, *, days: int = DEFAULT_DAYS) -> Report:
    """Prefer Playback Reporting, but only once it covers the whole window.

    The plugin does not backfill: its table starts empty at install time. Using
    it before it has `days` of history would silently report near-zero while
    the core estimate still has the real answer, so coverage is checked first.
    """
    try:
        window, now = await plugin_window(jf)
    except JellyfinNotFound:
        window, now = None, None
    except JellyfinError as exc:
        log.warning("Playback Reporting query failed, using core data: %s", exc)
        window, now = None, None

    if window is not None and window.covers(now, days):
        try:
            return Report(await collect_from_plugin(jf, days=days, window=window), True)
        except JellyfinError as exc:
            log.warning("Playback Reporting aggregation failed: %s", exc)

    note = None
    if window is not None and window.rows == 0:
        note = "Playback Reporting is installed but hasn't recorded anything yet."
    elif window is not None and window.earliest and now:
        have = max(0, (now - window.earliest).days)
        so_far = (
            "less than a day" if have == 0
            else f"{have} day{'s' if have != 1 else ''}"
        )
        note = (
            f"Playback Reporting has {so_far} of history so far; "
            f"figures become exact once it covers {days} days."
        )

    return Report(await collect_from_core(jf, days=days), False, note)


def build_embed(report: Report, *, days: int, total_users: int) -> discord.Embed:
    window = "the last 7 days" if days == DEFAULT_DAYS else f"the last {days} days"
    rows = report.rows

    if not rows:
        embed = discord.Embed(
            title="📺 Watch time",
            description=f"Nobody watched anything in {window}.",
            colour=discord.Color.greyple(),
        )
        if report.note:
            embed.set_footer(text=report.note)
        return embed

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

    if report.exact:
        footer = "Measured from Jellyfin's playback history."
    else:
        footer = ("Estimated — Jellyfin records when something was last played, "
                  "not every play, so repeats within the window count once.")
        if report.note:
            footer += f"\n{report.note}"
    embed.set_footer(text=footer)
    return embed
