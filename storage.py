"""SQLite storage shared by chat, migration and the AstrBot dashboard."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import aiosqlite


def group_scope(platform_id: str, group_id: str) -> str:
    return f"{quote(str(platform_id), safe='')}|{quote(str(group_id), safe='')}"


class GameStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._owner = None
        self._db = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS game (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    @asynccontextmanager
    async def _connection(self):
        if self._owner is asyncio.current_task():
            yield self._db
            return
        async with self._lock:
            if self._db is None:
                raise RuntimeError("游戏数据库尚未打开")
            yield self._db

    @asynccontextmanager
    async def transaction(self):
        async with self._connection() as db:
            if self._owner is asyncio.current_task():
                yield self
                return
            self._owner = asyncio.current_task()
            try:
                await db.execute("BEGIN IMMEDIATE")
                yield self
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            finally:
                self._owner = None

    async def get(self, key: str, default=None):
        async with self._connection() as db:
            async with db.execute("SELECT value FROM game WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
        return default if row is None else json.loads(row[0])

    async def put(self, key: str, value) -> None:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        async with self._connection() as db:
            await db.execute(
                "INSERT INTO game(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, encoded),
            )

    async def delete(self, key: str) -> None:
        async with self._connection() as db:
            await db.execute("DELETE FROM game WHERE key = ?", (key,))

    async def snapshot(self) -> dict:
        async with self._connection() as db:
            async with db.execute("SELECT key, value FROM game ORDER BY key") as cursor:
                rows = await cursor.fetchall()
        return {key: json.loads(value) for key, value in rows}
