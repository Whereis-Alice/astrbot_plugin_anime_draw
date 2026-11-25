"""Find related Bangumi works for catalog entries with confirmed character IDs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

MAX_WORKS = 3
WORK_TYPES = frozenset({1, 2, 4, 6})
IMAGE_HOSTS = frozenset({"lain.bgm.tv", "bgmlain.waterbee.cc"})
IMAGE_PATH = re.compile(r"^/pic/(?:crt|photos/character)/.*/([0-9]+)_")
USER_AGENT = "Whereis-Alice/astrbot_plugin_anime_draw (https://github.com/Whereis-Alice/astrbot_plugin_anime_draw)"


def has_bangumi_id(character: dict) -> bool:
    """Confirm the catalog ID in a Bangumi character image path or its mirror."""
    character_id = str(character.get("id"))
    for url in character.get("image") or ():
        if not isinstance(url, str):
            continue
        try:
            resource = urlsplit(url)
        except ValueError:
            continue
        if (
            resource.hostname in IMAGE_HOSTS
            and (match := IMAGE_PATH.match(resource.path))
            and match.group(1) == character_id
        ):
            return True
    return False


def work_title(subject: dict) -> str:
    for candidate in (subject.get("name_cn"), subject.get("name")):
        if isinstance(candidate, str) and (title := " ".join(candidate.split())):
            return title
    return ""


def take_titles(sorted_titles: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(title for title in sorted_titles if title))[:MAX_WORKS]


class WorkSources:
    """Load the bundled index and perform small, optional exact-ID lookups."""

    def __init__(self) -> None:
        self._index: dict[str, list[str]] | None = None
        self._online: dict[int, tuple[float, tuple[str, ...]]] = {}
        self._online_lock = asyncio.Lock()
        self._retry_at = 0.0

    def get(self, character_id: int | str) -> list[str]:
        if self._index is None:
            path = Path(__file__).with_name("character_works.json")
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                index = document["works"]
                self._index = index if isinstance(index, dict) else {}
            except (OSError, ValueError, KeyError, TypeError):
                self._index = {}
        works = self._index.get(str(character_id))
        if isinstance(works, list):
            return [title for title in works if isinstance(title, str)]
        if str(character_id).isascii() and str(character_id).isdigit():
            cached = self._online.get(int(character_id))
            if cached is not None and time.monotonic() < cached[0]:
                return list(cached[1])
        return []

    async def lookup(self, character: dict) -> list[str]:
        character_id = int(character["id"])
        works = self.get(character_id)
        if works or not has_bangumi_id(character):
            return works

        now = time.monotonic()
        cached = self._online.get(character_id)
        if cached is not None and now < cached[0]:
            return list(cached[1])
        if now < self._retry_at or self._online_lock.locked():
            return []
        async with self._online_lock:
            try:
                titles = await self._request(character_id)
            except (aiohttp.ClientError, ValueError, TypeError, TimeoutError):
                self._retry_at = time.monotonic() + 30
                return []
            self._retry_at = time.monotonic() + 1
            lifetime = 7 * 86400 if titles else 3600
            self._online.pop(character_id, None)
            if len(self._online) >= 512:
                del self._online[next(iter(self._online))]
            self._online[character_id] = (time.monotonic() + lifetime, titles)
            return list(titles)

    @staticmethod
    async def _request(character_id: int) -> tuple[str, ...]:
        timeout = aiohttp.ClientTimeout(total=2.5)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as session:
            url = f"https://api.bgm.tv/v0/characters/{character_id}/subjects"
            async with session.get(url, allow_redirects=False) as response:
                if response.status == 404:
                    return ()
                response.raise_for_status()
                rows = await response.json()
        if not isinstance(rows, list):
            raise TypeError("unexpected Bangumi character subject response")
        subjects = [
            row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("id"), int)
            and isinstance(row.get("type"), int)
            and row["type"] in WORK_TYPES
        ]
        roles = {"主角": 1, "配角": 2, "客串": 3}
        subjects.sort(key=lambda row: (roles.get(str(row.get("staff")), 4), row.get("id", 0)))
        return take_titles(work_title(row) for row in subjects)
