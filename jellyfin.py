"""Async Jellyfin REST client - only the endpoints the announcer needs."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import aiohttp

log = logging.getLogger(__name__)

CLIENT_NAME = "jellyfin-discord-bot"

# Only real ItemFields enum values belong here - Jellyfin 10.10+ rejects unknown
# names with a 400. Name, ProductionYear, RunTimeTicks, CommunityRating,
# OfficialRating, SeriesId/SeriesName and the episode/season numbers are all
# part of the default BaseItemDto payload and must not be requested explicitly.
MOVIE_FIELDS = "Overview,Genres,Studios,DateCreated,ProviderIds"
EPISODE_FIELDS = "Overview,DateCreated"
SERIES_FIELDS = "Overview,Genres,Studios"

# Content types Jellyfin can hand back for artwork, mapped to a file extension
# because Discord picks the renderer from the attachment filename.
_IMAGE_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class JellyfinError(RuntimeError):
    """Any failure talking to Jellyfin: unreachable, auth rejected, bad status."""


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                f'MediaBrowser Token="{self.api_key}", Client="{CLIENT_NAME}", '
                f'Device="docker", DeviceId="{CLIENT_NAME}-01", Version="1.0"'
            ),
            "Accept": "application/json",
        }

    async def _session_for(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers=self._headers()
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def get_json(self, path: str, **params) -> dict:
        session = await self._session_for()
        clean = {k: str(v) for k, v in params.items() if v is not None and v != ""}
        url = f"{self.base}{path}"
        try:
            async with session.get(url, params=clean) as resp:
                if resp.status == 401:
                    raise JellyfinError(
                        "Jellyfin rejected the API key (401) - check JELLYFIN_API_KEY"
                    )
                if resp.status >= 400:
                    raise JellyfinError(f"Jellyfin returned HTTP {resp.status} for {path}")
                return await resp.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise JellyfinError(f"Jellyfin timed out on {path}") from exc
        except aiohttp.ClientError as exc:
            raise JellyfinError(f"Cannot reach Jellyfin at {self.base}: {exc}") from exc

    # ------------------------------------------------------------------ meta

    async def system_info(self) -> dict:
        return await self.get_json("/System/Info")

    # ----------------------------------------------------------------- items

    async def items_page(
        self,
        *,
        include_types: str,
        fields: str = "",
        start: int = 0,
        limit: int = 200,
        sort_by: str = "DateCreated",
        sort_order: str = "Descending",
    ) -> tuple[list[dict], int]:
        data = await self.get_json(
            "/Items",
            recursive="true",
            includeItemTypes=include_types,
            # Jellyfin 12 collapses movies into their collections by default,
            # which returns BoxSets in place of the movies they contain. Those
            # then get announced as if they were new films. Always off.
            collapseBoxSetItems="false",
            fields=fields or None,
            enableImages="false",
            startIndex=start,
            limit=limit,
            sortBy=sort_by,
            sortOrder=sort_order,
        )
        return data.get("Items") or [], int(data.get("TotalRecordCount") or 0)

    async def iter_all_items(
        self, include_types: str, page: int = 500
    ) -> AsyncIterator[dict]:
        """Page through every item of the given types. Used only for baselining."""
        start = 0
        while True:
            items, total = await self.items_page(
                include_types=include_types, start=start, limit=page
            )
            if not items:
                return
            for item in items:
                yield item
            start += len(items)
            if start >= total:
                return

    async def get_item(self, item_id: str, fields: str = "") -> dict | None:
        """Fetch one item by id.

        Uses the ids= filter rather than /Items/{id} because the latter moved
        between Jellyfin releases, while this form works on 10.8 and 10.9+.
        """
        data = await self.get_json(
            "/Items", ids=item_id, recursive="true", fields=fields or None
        )
        items = data.get("Items") or []
        return items[0] if items else None

    # ---------------------------------------------------------------- images

    async def image_bytes(
        self, item_id: str, image_type: str = "Primary", max_width: int = 600
    ) -> tuple[bytes, str] | None:
        """Download artwork as raw bytes so it can be attached to a Discord post.

        Jellyfin on a LAN address is not reachable by Discord's image fetchers,
        so artwork has to travel with the message instead of as a URL.
        """
        session = await self._session_for()
        url = f"{self.base}/Items/{item_id}/Images/{image_type}"
        try:
            async with session.get(
                url, params={"maxWidth": str(max_width), "quality": "90"}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if not data:
                    return None
                ext = _IMAGE_EXT.get((resp.content_type or "").lower(), "jpg")
                return data, ext
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.debug("Could not fetch %s image for %s", image_type, item_id)
            return None
