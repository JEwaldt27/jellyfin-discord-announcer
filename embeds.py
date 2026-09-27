"""Builds the Discord embeds for newly added movies and TV seasons."""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

import discord

from jellyfin import JellyfinClient

log = logging.getLogger(__name__)

COLOR_MOVIE = discord.Color(0x00A4DC)  # Jellyfin blue
COLOR_SHOW = discord.Color(0xAA5CC3)   # Jellyfin purple

POSTER_WIDTH = 400
BACKDROP_WIDTH = 1280

OVERVIEW_LIMIT = 350
MAX_EPISODES_LISTED = 8

# Jellyfin emits 7-digit fractional seconds, which datetime.fromisoformat rejects.
_FRACTION = re.compile(r"\.(\d+)")


def parse_jellyfin_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6], value.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def truncate(text: str | None, limit: int = OVERVIEW_LIMIT) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def format_runtime(ticks: int | None) -> str | None:
    """Jellyfin stores runtime in 100-nanosecond ticks."""
    if not ticks:
        return None
    minutes = int(ticks / 10_000_000 / 60)
    if minutes <= 0:
        return None
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def detail_url(public_url: str | None, item_id: str, server_id: str | None) -> str | None:
    if not public_url:
        return None
    base = public_url.rstrip("/")
    # Discord rejects the whole embed with a 400 if the url has no scheme, so a
    # malformed setting must cost us the link, never the post.
    if not base.lower().startswith(("http://", "https://")):
        log.warning("Ignoring JELLYFIN_PUBLIC_URL %r - it needs http:// or https://", base)
        return None
    url = f"{base}/web/index.html#!/details?id={item_id}"
    if server_id:
        url += f"&serverId={server_id}"
    return url


def _studio_names(item: dict, limit: int = 2) -> str | None:
    names = [s.get("Name") for s in (item.get("Studios") or []) if s.get("Name")]
    return ", ".join(names[:limit]) if names else None


async def _artwork(
    jf: JellyfinClient, embed: discord.Embed, art_item_id: str
) -> list[discord.File]:
    """Attach poster (as thumbnail) and backdrop (as banner) to an embed."""
    files: list[discord.File] = []

    poster = await jf.image_bytes(art_item_id, "Primary", POSTER_WIDTH)
    if poster:
        data, ext = poster
        name = f"poster.{ext}"
        files.append(discord.File(io.BytesIO(data), filename=name))
        embed.set_thumbnail(url=f"attachment://{name}")

    backdrop = await jf.image_bytes(art_item_id, "Backdrop", BACKDROP_WIDTH)
    if backdrop:
        data, ext = backdrop
        name = f"backdrop.{ext}"
        files.append(discord.File(io.BytesIO(data), filename=name))
        embed.set_image(url=f"attachment://{name}")

    return files


async def build_movie_embed(
    jf: JellyfinClient,
    item: dict,
    *,
    public_url: str | None = None,
    server_id: str | None = None,
) -> tuple[discord.Embed, list[discord.File]]:
    name = item.get("Name") or "Unknown title"
    year = item.get("ProductionYear")
    title = f"{name} ({year})" if year else name

    embed = discord.Embed(
        title=title,
        description=truncate(item.get("Overview")),
        color=COLOR_MOVIE,
        url=detail_url(public_url, item["Id"], server_id),
        timestamp=parse_jellyfin_date(item.get("DateCreated")),
    )
    embed.set_author(name="New movie added")

    runtime = format_runtime(item.get("RunTimeTicks"))
    if runtime:
        embed.add_field(name="Runtime", value=runtime, inline=True)

    rating = item.get("CommunityRating")
    if rating:
        embed.add_field(name="Rating", value=f"⭐ {rating:.1f}", inline=True)

    if item.get("OfficialRating"):
        embed.add_field(name="Rated", value=item["OfficialRating"], inline=True)

    genres = item.get("Genres") or []
    if genres:
        embed.add_field(name="Genres", value=", ".join(genres[:4]), inline=False)

    studio = _studio_names(item)
    if studio:
        embed.set_footer(text=f"{studio} • Added to Jellyfin")
    else:
        embed.set_footer(text="Added to Jellyfin")

    files = await _artwork(jf, embed, item["Id"])
    return embed, files


def _season_label(season: int | None) -> str:
    if season is None:
        return "New episodes"
    if season == 0:
        return "Specials"
    return f"Season {season}"


def _episode_line(ep: dict, season: int | None) -> str:
    number = ep.get("IndexNumber")
    if season is not None and number is not None:
        code = f"S{season:02d}E{number:02d}"
    elif number is not None:
        code = f"E{number:02d}"
    else:
        code = "—"
    return f"`{code}` {ep.get('Name') or 'Untitled'}"


async def build_season_embed(
    jf: JellyfinClient,
    series: dict | None,
    season: int | None,
    episodes: list[dict],
    *,
    public_url: str | None = None,
    server_id: str | None = None,
) -> tuple[discord.Embed, list[discord.File]]:
    """One embed covering every new episode of a single season."""
    series = series or {}
    first = episodes[0]
    series_name = series.get("Name") or first.get("SeriesName") or "Unknown series"
    series_id = series.get("Id") or first.get("SeriesId") or first["Id"]

    ordered = sorted(
        episodes,
        key=lambda e: (e.get("IndexNumber") is None, e.get("IndexNumber") or 0),
    )
    count = len(ordered)
    header = f"**{count} new episode{'s' if count != 1 else ''}**"

    lines = [_episode_line(ep, season) for ep in ordered[:MAX_EPISODES_LISTED]]
    hidden = count - len(lines)
    if hidden > 0:
        lines.append(f"…and {hidden} more")

    overview = truncate(series.get("Overview"), 220)
    description = header + "\n" + "\n".join(lines)
    if overview:
        description += f"\n\n{overview}"

    embed = discord.Embed(
        title=f"{series_name} — {_season_label(season)}",
        description=description,
        color=COLOR_SHOW,
        url=detail_url(public_url, series_id, server_id),
        timestamp=parse_jellyfin_date(first.get("DateCreated")),
    )
    embed.set_author(name="New episodes added")

    rating = series.get("CommunityRating")
    if rating:
        embed.add_field(name="Rating", value=f"⭐ {rating:.1f}", inline=True)

    if series.get("OfficialRating"):
        embed.add_field(name="Rated", value=series["OfficialRating"], inline=True)

    network = _studio_names(series, limit=1)
    if network:
        embed.add_field(name="Network", value=network, inline=True)

    genres = series.get("Genres") or []
    if genres:
        embed.add_field(name="Genres", value=", ".join(genres[:4]), inline=False)

    embed.set_footer(text="Added to Jellyfin")

    files = await _artwork(jf, embed, series_id)
    return embed, files


def build_overflow_embed(extra_movies: int, extra_episodes: int) -> discord.Embed:
    """Shown when a bulk import blows past the per-scan post cap."""
    parts = []
    if extra_movies:
        parts.append(f"**{extra_movies}** movie{'s' if extra_movies != 1 else ''}")
    if extra_episodes:
        parts.append(f"**{extra_episodes}** episode{'s' if extra_episodes != 1 else ''}")
    return discord.Embed(
        title="…and more",
        description=(
            "Also added this scan: " + " and ".join(parts) + ".\n"
            "They were not posted individually to avoid flooding the channel."
        ),
        color=discord.Color.dark_grey(),
    )
