"""SQLite archive of collected messages."""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    author_name TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_author_guild
    ON messages (author_id, guild_id);
"""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime

    @property
    def jump_url(self) -> str:
        """Deep link to the original message in the Discord client."""
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


def _filter(author_id: int | None, guild_id: int | None) -> tuple[str, list[int]]:
    """Build a SQL WHERE clause for the optional author/guild filters."""
    clauses: list[str] = []
    params: list[int] = []

    if author_id is not None:
        clauses.append("author_id = ?")
        params.append(author_id)
    if guild_id is not None:
        clauses.append("guild_id = ?")
        params.append(guild_id)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _row_to_message(row: aiosqlite.Row) -> StoredMessage:
    return StoredMessage(
        message_id=row["message_id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class MessageStore:
    """Small wrapper around a single SQLite connection.

    One connection is enough for a bot of this size; aiosqlite serialises access
    through a background thread.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("MessageStore is not open")
        return self._conn

    async def add_many(self, messages: Iterable[StoredMessage]) -> int:
        """Insert messages, ignoring duplicates. Returns the number newly stored."""
        payload = [
            (
                m.message_id,
                m.guild_id,
                m.channel_id,
                m.author_id,
                m.author_name,
                m.content,
                m.created_at.astimezone(UTC).isoformat(),
            )
            for m in messages
        ]
        if not payload:
            return 0

        cursor = await self.conn.executemany(
            "INSERT OR IGNORE INTO messages"
            " (message_id, guild_id, channel_id, author_id, author_name, content, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        await self.conn.commit()
        return cursor.rowcount

    async def count(self, author_id: int | None = None, guild_id: int | None = None) -> int:
        """Count archived messages, optionally for one author and/or one guild."""
        where, params = _filter(author_id, guild_id)
        async with self.conn.execute(f"SELECT COUNT(*) FROM messages{where}", params) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def author_count(self, guild_id: int | None = None) -> int:
        """How many distinct users have archived messages."""
        where, params = _filter(None, guild_id)
        async with self.conn.execute(
            f"SELECT COUNT(DISTINCT author_id) FROM messages{where}", params
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def random_for(
        self, author_id: int | None = None, guild_id: int | None = None
    ) -> StoredMessage | None:
        """Pick a random message, optionally restricted to one author/guild."""
        total = await self.count(author_id, guild_id)
        if total == 0:
            return None

        where, params = _filter(author_id, guild_id)
        params.append(random.randrange(total))

        async with self.conn.execute(
            f"SELECT * FROM messages{where} ORDER BY message_id LIMIT 1 OFFSET ?", params
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_message(row) if row else None
