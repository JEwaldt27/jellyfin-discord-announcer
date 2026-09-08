"""Scan logic: diff Jellyfin against what we've already announced, then post."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import discord

import embeds
from db import Database
from jellyfin import EPISODE_FIELDS, MOVIE_FIELDS, SERIES_FIELDS, JellyfinClient, JellyfinError

log = logging.getLogger(__name__)

# Seconds to wait between messages so a big batch doesn't trip Discord's limiter.
POST_DELAY = 1.0

# How deep a single scan will page before giving up on finding the boundary
# between new and already-seen items.
HARD_CAP = 3000
PAGE_SIZE = 200


@dataclass
class ScanResult:
    baseline: bool = False
    baseline_count: int = 0
    movies_posted: int = 0
    seasons_posted: int = 0
    episodes_posted: int = 0
    extra_movies: int = 0
    extra_episodes: int = 0
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def posted_anything(self) -> bool:
        return bool(self.movies_posted or self.seasons_posted)

    def status_line(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if self.baseline:
            return f"baseline ({self.baseline_count} items recorded)"
        return (
            f"ok ({self.movies_posted} movies, {self.seasons_posted} seasons"
            f"/{self.episodes_posted} episodes)"
        )

    def summary(self) -> str:
        if self.error:
            return f"❌ Scan failed: {self.error}"

        if self.baseline:
            return (
                f"📋 **First scan complete.** Recorded {self.baseline_count:,} existing "
                "items as your starting point — nothing was posted.\n"
                "From now on, anything new gets announced."
            )

        lines = []
        if self.movies_posted:
            lines.append(
                f"🎬 {self.movies_posted} movie{'s' if self.movies_posted != 1 else ''}"
            )
        if self.seasons_posted:
            lines.append(
                f"📺 {self.episodes_posted} episode"
                f"{'s' if self.episodes_posted != 1 else ''} across "
                f"{self.seasons_posted} season{'s' if self.seasons_posted != 1 else ''}"
            )

        if lines:
            out = "✅ Posted " + " and ".join(lines) + "."
        else:
            out = "✅ Scan complete — nothing new since the last one."

        if self.extra_movies or self.extra_episodes:
            out += (
                f"\n⚠️ Post cap reached; {self.extra_movies} movies and "
                f"{self.extra_episodes} episodes were recorded but not posted."
            )
        for note in self.skipped:
            out += f"\n⚠️ {note}"
        return out


async def _collect_new(
    jf: JellyfinClient,
    db: Database,
    guild_id: int,
    *,
    include_types: str,
    fields: str,
) -> list[dict]:
    """Walk newest-first until a whole page is already known, then stop.

    Stopping on a *fully* seen page rather than the first seen item matters:
    a library rescan can rewrite DateCreated and float an old item back to the
    top, which would otherwise cut the walk short and hide genuinely new media.
    """
    found: list[dict] = []
    start = 0
    while start < HARD_CAP:
        page, total = await jf.items_page(
            include_types=include_types, fields=fields, start=start, limit=PAGE_SIZE
        )
        if not page:
            break

        ids = [item["Id"] for item in page if item.get("Id")]
        unseen = await db.filter_unseen(guild_id, ids)
        found.extend(item for item in page if item.get("Id") in unseen)

        if not unseen:
            break

        start += len(page)
        if start >= total or len(page) < PAGE_SIZE:
            break

    return found


async def _run_baseline(jf: JellyfinClient, db: Database, guild_id: int) -> ScanResult:
    """Record the whole library as seen without posting anything."""
    total = 0
    batch: list[dict] = []
    for item_type in ("Movie", "Episode"):
        async for item in jf.iter_all_items(item_type):
            batch.append(item)
            if len(batch) >= 500:
                total += await db.mark_seen(guild_id, batch)
                batch.clear()
    if batch:
        total += await db.mark_seen(guild_id, batch)

    await db.set_baselined(guild_id, True)
    log.info("Guild %s baselined with %d items", guild_id, total)
    return ScanResult(baseline=True, baseline_count=total)


async def _send(channel: discord.abc.Messageable, embed, files) -> bool:
    try:
        await channel.send(embed=embed, files=files)
        return True
    except discord.Forbidden:
        log.warning("Missing permission to post in #%s", getattr(channel, "name", "?"))
    except discord.HTTPException as exc:
        log.warning("Discord rejected a post: %s", exc)
    return False


def _resolve_channel(guild: discord.Guild, channel_id: int | None):
    """Return a postable channel, or None if it's unset, deleted, or not text."""
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    if channel_id:
        log.warning("Configured channel %s is missing or not a text channel", channel_id)
    return None


def _group_episodes(episodes: list[dict]) -> dict[tuple[str, int | None], list[dict]]:
    groups: dict[tuple[str, int | None], list[dict]] = {}
    for ep in episodes:
        series_id = ep.get("SeriesId")
        if not series_id:
            continue  # an episode with no parent series can't be grouped or linked
        groups.setdefault((series_id, ep.get("ParentIndexNumber")), []).append(ep)
    return groups


async def run_scan(
    *,
    guild: discord.Guild,
    jf: JellyfinClient,
    db: Database,
    max_posts: int,
    public_url: str | None,
    server_id: str | None,
) -> ScanResult:
    cfg = await db.get_config(guild.id)

    try:
        if not cfg.baselined:
            return await _run_baseline(jf, db, guild.id)

        new_movies = await _collect_new(
            jf, db, guild.id, include_types="Movie", fields=MOVIE_FIELDS
        )
        new_episodes = await _collect_new(
            jf, db, guild.id, include_types="Episode", fields=EPISODE_FIELDS
        )
    except JellyfinError as exc:
        log.error("Scan for guild %s failed: %s", guild.id, exc)
        return ScanResult(error=str(exc))

    result = ScanResult()
    movie_channel = _resolve_channel(guild, cfg.movie_channel_id)
    show_channel = _resolve_channel(guild, cfg.show_channel_id)

    if new_movies and movie_channel is None:
        result.skipped.append(
            f"{len(new_movies)} new movie(s) held back — no movie channel set "
            "(`/channels movies`)."
        )
        new_movies = []

    groups = _group_episodes(new_episodes)
    if groups and show_channel is None:
        result.skipped.append(
            f"{len(new_episodes)} new episode(s) held back — no show channel set "
            "(`/channels shows`)."
        )
        groups = {}

    budget = max_posts

    # ------------------------------------------------------------- movies
    # Oldest first so the channel reads in the order things were added.
    for movie in reversed(new_movies):
        if budget <= 0:
            result.extra_movies += 1
            await db.mark_seen(guild.id, [movie])
            continue
        try:
            embed, files = await embeds.build_movie_embed(
                jf, movie, public_url=public_url, server_id=server_id
            )
        except JellyfinError as exc:
            log.warning("Skipping movie %s: %s", movie.get("Name"), exc)
            continue

        if await _send(movie_channel, embed, files):
            await db.mark_seen(guild.id, [movie])
            result.movies_posted += 1
            budget -= 1
            await asyncio.sleep(POST_DELAY)

    # -------------------------------------------------------------- shows
    series_cache: dict[str, dict | None] = {}
    ordered_groups = sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1] if kv[0][1] is not None else -1)
    )

    for (series_id, season), episode_list in ordered_groups:
        if budget <= 0:
            result.extra_episodes += len(episode_list)
            await db.mark_seen(guild.id, episode_list)
            continue

        if series_id not in series_cache:
            try:
                series_cache[series_id] = await jf.get_item(series_id, SERIES_FIELDS)
            except JellyfinError as exc:
                log.warning("Could not load series %s: %s", series_id, exc)
                series_cache[series_id] = None

        try:
            embed, files = await embeds.build_season_embed(
                jf,
                series_cache[series_id],
                season,
                episode_list,
                public_url=public_url,
                server_id=server_id,
            )
        except JellyfinError as exc:
            log.warning("Skipping season %s of %s: %s", season, series_id, exc)
            continue

        if await _send(show_channel, embed, files):
            await db.mark_seen(guild.id, episode_list)
            result.seasons_posted += 1
            result.episodes_posted += len(episode_list)
            budget -= 1
            await asyncio.sleep(POST_DELAY)

    # ------------------------------------------------------------ overflow
    if result.extra_movies and movie_channel is not None:
        await _send(movie_channel, embeds.build_overflow_embed(result.extra_movies, 0), [])
    if result.extra_episodes and show_channel is not None:
        await _send(show_channel, embeds.build_overflow_embed(0, result.extra_episodes), [])

    return result
