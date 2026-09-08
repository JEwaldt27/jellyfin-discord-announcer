"""SQLite state: per-guild settings and the set of Jellyfin items already announced."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import aiosqlite

log = logging.getLogger(__name__)

DEFAULT_SCAN_MINUTES = 24 * 60
MIN_SCAN_MINUTES = 15
MAX_SCAN_MINUTES = 30 * 24 * 60

# SQLite caps host parameters per statement; stay well under the old 999 limit.
_CHUNK = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id          INTEGER PRIMARY KEY,
    movie_channel_id  INTEGER,
    show_channel_id   INTEGER,
    scan_interval_min INTEGER NOT NULL DEFAULT 1440,
    baselined         INTEGER NOT NULL DEFAULT 0,
    last_scan_at      TEXT,
    next_scan_at      TEXT,
    last_scan_status  TEXT
);

CREATE TABLE IF NOT EXISTS seen_items (
    guild_id      INTEGER NOT NULL,
    item_id       TEXT    NOT NULL,
    item_type     TEXT    NOT NULL,
    series_id     TEXT,
    season        INTEGER,
    first_seen_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_seen_guild_type ON seen_items (guild_id, item_type);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class GuildConfig:
    guild_id: int
    movie_channel_id: int | None = None
    show_channel_id: int | None = None
    scan_interval_min: int = DEFAULT_SCAN_MINUTES
    baselined: bool = False
    last_scan_at: datetime | None = None
    next_scan_at: datetime | None = None
    last_scan_status: str | None = None

    def is_due(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.next_scan_at is None or self.next_scan_at <= now


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was never awaited")
        return self._conn

    # ---------------------------------------------------------------- config

    async def get_config(self, guild_id: int) -> GuildConfig:
        """Fetch a guild's settings, creating the default row on first sight."""
        async with self.conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            await self.conn.execute(
                "INSERT INTO guild_config (guild_id, scan_interval_min) VALUES (?, ?)",
                (guild_id, DEFAULT_SCAN_MINUTES),
            )
            await self.conn.commit()
            return GuildConfig(guild_id=guild_id)

        return GuildConfig(
            guild_id=row["guild_id"],
            movie_channel_id=row["movie_channel_id"],
            show_channel_id=row["show_channel_id"],
            scan_interval_min=row["scan_interval_min"],
            baselined=bool(row["baselined"]),
            last_scan_at=from_iso(row["last_scan_at"]),
            next_scan_at=from_iso(row["next_scan_at"]),
            last_scan_status=row["last_scan_status"],
        )

    async def _update(self, guild_id: int, **columns) -> None:
        await self.get_config(guild_id)  # ensure the row exists
        assignments = ", ".join(f"{k} = ?" for k in columns)
        await self.conn.execute(
            f"UPDATE guild_config SET {assignments} WHERE guild_id = ?",
            (*columns.values(), guild_id),
        )
        await self.conn.commit()

    async def set_movie_channel(self, guild_id: int, channel_id: int) -> None:
        await self._update(guild_id, movie_channel_id=channel_id)

    async def set_show_channel(self, guild_id: int, channel_id: int) -> None:
        await self._update(guild_id, show_channel_id=channel_id)

    async def set_interval(self, guild_id: int, minutes: int) -> GuildConfig:
        """Change the scan cadence and re-anchor the next run to the last one."""
        cfg = await self.get_config(guild_id)
        anchor = cfg.last_scan_at or utcnow()
        next_at = anchor + timedelta(minutes=minutes)
        await self._update(
            guild_id, scan_interval_min=minutes, next_scan_at=to_iso(next_at)
        )
        return await self.get_config(guild_id)

    async def record_scan(
        self, guild_id: int, *, status: str, next_scan_at: datetime
    ) -> None:
        await self._update(
            guild_id,
            last_scan_at=to_iso(utcnow()),
            next_scan_at=to_iso(next_scan_at),
            last_scan_status=status,
        )

    async def set_baselined(self, guild_id: int, value: bool) -> None:
        await self._update(guild_id, baselined=1 if value else 0)

    async def all_guild_ids(self) -> list[int]:
        async with self.conn.execute("SELECT guild_id FROM guild_config") as cur:
            return [r["guild_id"] for r in await cur.fetchall()]

    # ------------------------------------------------------------ seen items

    async def filter_unseen(self, guild_id: int, item_ids: Sequence[str]) -> set[str]:
        """Return the subset of item_ids this guild has never announced."""
        remaining = set(item_ids)
        ids = list(remaining)
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            placeholders = ",".join("?" * len(chunk))
            async with self.conn.execute(
                "SELECT item_id FROM seen_items WHERE guild_id = ? "
                f"AND item_id IN ({placeholders})",
                (guild_id, *chunk),
            ) as cur:
                for row in await cur.fetchall():
                    remaining.discard(row["item_id"])
        return remaining

    async def mark_seen(self, guild_id: int, items: Iterable[dict]) -> int:
        """Record items as announced. Safe to call twice on the same item."""
        now = to_iso(utcnow())
        rows = [
            (
                guild_id,
                item["Id"],
                item.get("Type") or "Unknown",
                item.get("SeriesId"),
                item.get("ParentIndexNumber"),
                now,
            )
            for item in items
            if item.get("Id")
        ]
        if not rows:
            return 0
        await self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items "
            "(guild_id, item_id, item_type, series_id, season, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self.conn.commit()
        return len(rows)

    async def seen_count(self, guild_id: int) -> tuple[int, int]:
        async with self.conn.execute(
            "SELECT item_type, COUNT(*) AS n FROM seen_items "
            "WHERE guild_id = ? GROUP BY item_type",
            (guild_id,),
        ) as cur:
            counts = {r["item_type"]: r["n"] for r in await cur.fetchall()}
        return counts.get("Movie", 0), counts.get("Episode", 0)

    async def clear_seen(self, guild_id: int) -> None:
        await self.conn.execute("DELETE FROM seen_items WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()
