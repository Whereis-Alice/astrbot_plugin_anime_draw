"""Shared storage scope, platform replies and short-lived collection actions."""

import asyncio
import datetime
import secrets
import time

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from . import qq_official as qq
from .storage import group_scope

PLUGIN_NAME = "astrbot_plugin_anime_draw"
GAME_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))
DRAW_MSG_TTL = 45
OFFICIAL_ACTION_TTL = 120
DRAW_MSG_INDEX_MAX = 300
ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"


class GameRuntime:
    def _scope(self, event: AstrMessageEvent) -> str:
        return group_scope(event.get_platform_id(), event.get_group_id())

    def _game_enabled(self, event: AstrMessageEvent) -> bool:
        return (
            self.config.get("gameplay_enabled", True)
            and bool(event.get_group_id())
            and event.get_platform_name() in qq.OFFICIAL_PLATFORMS | {"aiocqhttp"}
        )

    async def get_kv_data(self, key: str, default=None):
        return await self.store.get(key, default)

    async def put_kv_data(self, key: str, value) -> None:
        await self.store.put(key, value)

    async def delete_kv_data(self, key: str) -> None:
        await self.store.delete(key)

    async def get_group_cfg(self, gid: str) -> dict:
        return await self.store.get(f"{gid}:config", {}) or {}

    async def update_group_cfg(self, gid: str, key: str, value) -> None:
        async with self._get_group_lock(gid), self.store.transaction():
            cfg = await self.get_group_cfg(gid)
            if value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value
            await self.store.put(f"{gid}:config", cfg)

    async def get_user_list(self, gid: str) -> set[str]:
        return {str(uid) for uid in await self.store.get(f"{gid}:user_list", [])}

    async def _remember(self, event: AstrMessageEvent, gid: str) -> None:
        uid = str(event.get_sender_id())
        async with self._get_group_lock(gid), self.store.transaction():
            users = await self.get_user_list(gid)
            if uid not in users:
                await self.store.put(f"{gid}:user_list", sorted(users | {uid}))
            sender = getattr(event.message_obj, "sender", None)
            name = str(getattr(sender, "nickname", "") or "").strip()[:100]
            if name and name != uid:
                await self.store.put(f"{gid}:{uid}:display_name", name)

    async def _user_label(self, gid: str, uid: str) -> str:
        cached = await self.store.get(f"{gid}:{uid}:display_name")
        return cached or (uid if uid.isdecimal() else f"玩家 {uid[-6:]}")

    async def get_group_role(self, event: AstrMessageEvent) -> str | None:
        if event.get_platform_name() in qq.OFFICIAL_PLATFORMS:
            return "owner" if event.is_admin() else None
        try:
            response = await event.bot.api.call_action(
                "get_group_member_info", group_id=event.get_group_id(), user_id=event.get_sender_id()
            )
            return response.get("role")
        except Exception as exc:
            logger.debug("二次元抽抽乐读取群权限失败：%s", type(exc).__name__)
            return None

    def _get_group_lock(self, gid: str) -> asyncio.Lock:
        lock = self.group_locks.get(gid)
        if lock is None:
            lock = self.group_locks[gid] = asyncio.Lock()
        return lock

    @staticmethod
    def _image_component(image: str) -> Comp.Image:
        if image.startswith(("http://", "https://")):
            return Comp.Image.fromURL(image)
        return Comp.Image.fromFileSystem(image)

    def _custom_path(self, value: str):
        if not isinstance(value, str) or not value.startswith("img/") or "/" in value[4:] or "\\" in value:
            return None
        root = (self.data_dir / "img").resolve()
        path = (self.data_dir / value).resolve()
        return path if path.parent == root and path.is_file() else None

    async def _images(self, gid: str, char: dict) -> list[str]:
        images = [
            url for url in char.get("image", [])
            if isinstance(url, str) and url.startswith(("https://", "http://"))
        ]
        paths = await self.store.get(f"{gid}:{char['id']}:custom_images", [])
        images.extend(str(path) for value in paths if (path := self._custom_path(value)))
        return images

    async def _render(self, event, title: str, body: str, image=None, actions=(), *, button_owner=None):
        if await qq.send(
            event, title, body, image=image, actions=actions, button_owner=button_owner,
            use_markdown=self.config.get("official_markdown", True),
            use_buttons=self.config.get("official_buttons", True),
        ):
            return
        chain = [Comp.Plain(f"{title}\n{body}" if title else body)]
        if image and event.get_platform_name() not in qq.OFFICIAL_PLATFORMS:
            chain.append(self._image_component(image))
        yield event.chain_result(chain)

    def _new_action(self, gid: str, kind: str, ttl: int, **data) -> tuple[str, dict]:
        group = self._actions.setdefault(gid, {})
        now = time.monotonic()
        keep = {key: item for key, item in group.items() if item["until"] > now and not item.get("used")}
        group = self._actions[gid] = dict(list(keep.items())[-(DRAW_MSG_INDEX_MAX - 1):])
        code = secrets.token_hex(4).upper()
        while code in group:
            code = secrets.token_hex(4).upper()
        record = {"kind": kind, "until": now + ttl, **data}
        group[code] = record
        return code, record

    def _get_action(self, gid: str, kind: str, reference: str | int) -> dict | None:
        record = self._actions.get(gid, {}).get(str(reference).upper())
        if record and record["kind"] == kind and record["until"] > time.monotonic() and not record.get("used"):
            return record
        return None

    async def _onebot_send(self, event, chain: list) -> str | int | None:
        try:
            response = await event.bot.api.call_action("send_group_msg", group_id=event.get_group_id(), message=chain)
            return response.get("message_id") if isinstance(response, dict) else None
        except Exception as exc:
            logger.warning("二次元抽抽乐 OneBot 发消息失败：%s", type(exc).__name__)
            return None

    async def _onebot_react(self, event, message_id: str | int, emoji_id: int) -> None:
        try:
            await event.bot.api.call_action("set_msg_emoji_like", message_id=message_id, emoji_id=emoji_id, set=True)
        except Exception as exc:
            logger.debug("二次元抽抽乐 OneBot 贴表情失败：%s", type(exc).__name__)
