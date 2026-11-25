"""Data management through AstrBot's authenticated plugin pages."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger

from .migration import MigrationError

PLUGIN_NAME = "astrbot_plugin_anime_draw"
NO_STORE = {"Cache-Control": "no-store"}
PLAYER_FIELDS = {
    "display_name",
    "partners",
    "fav",
    "wish_list",
    "draw_status",
    "last_claim",
}
GROUP_FIELDS = {"config", "user_list"}
PLAYER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def object_value(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def ids(value: Any) -> list[str]:
    if not isinstance(value, (list, set)):
        return []
    return list(
        dict.fromkeys(
            str(item) for item in value if type(item) in {int, str} and item != ""
        )
    )


def number(value: Any) -> int | float | None:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def group_players(snapshot: dict[str, Any]) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    for key, value in snapshot.items():
        prefix, sep, field = key.rpartition(":")
        if sep and field in GROUP_FIELDS and "|" in prefix and ":" not in prefix:
            players = groups.setdefault(prefix, set())
            if field == "user_list":
                players.update(ids(value))
        elif sep and field in PLAYER_FIELDS:
            scope, id_sep, uid = prefix.rpartition(":")
            if id_sep and uid and "|" in scope and ":" not in scope:
                groups.setdefault(scope, set()).add(uid)
    return groups


class Dashboard:
    def __init__(self, plugin: Any) -> None:
        from astrbot.api.web import json_response, request

        self.plugin = plugin
        self.request = request
        self.json_response = json_response
        self._closed = False

    def register(self) -> None:
        for endpoint, handler, method, title in (
            ("overview", self.overview, "GET", "群聊数据概览"),
            ("players", self.players, "GET", "玩家和角色收藏"),
            ("reset", self.reset, "POST", "重置玩家的抽卡次数和结婚冷却"),
            ("migration", self.migration, "GET", "旧服存档迁移进度"),
            ("pending", self.pending, "GET", "待关联旧账号"),
            ("map", self.map_group, "POST", "关联新旧群聊"),
            ("link", self.manual_link, "POST", "人工关联玩家存档"),
        ):
            self.plugin.context.register_web_api(
                f"/{PLUGIN_NAME}/console/{endpoint}",
                handler,
                [method],
                f"二次元抽抽乐 · {title}",
            )

    def close(self) -> None:
        self._closed = True
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [
                route
                for route in routes
                if getattr(route[1], "__self__", None) is not self
            ]

    def _reply(self, data: dict[str, Any], status: int = 200) -> Any:
        return self.json_response(data, status_code=status, headers=NO_STORE)

    async def _serve(self, operation: Callable[[], Awaitable[dict[str, Any]]]) -> Any:
        if not self.request.username:
            return self._reply(
                {"status": "error", "message": "请先登录 AstrBot 管理面板。"}, 401
            )
        if self._closed:
            return self._reply(
                {"status": "error", "message": "插件正在停止，请稍后刷新。"}, 503
            )
        try:
            return self._reply(await operation())
        except RequestError as exc:
            return self._reply({"status": "error", "message": str(exc)}, exc.status)
        except MigrationError as exc:
            return self._reply({"status": "error", "message": str(exc)}, 409)
        except Exception as exc:  # noqa: BLE001 - keep internal error details out of the response
            logger.error(
                f"[二次元抽抽乐] 管理页面 {operation.__name__} 失败：{type(exc).__name__}"
            )
            return self._reply(
                {"status": "error", "message": "读取或保存失败，请查看插件日志。"}, 500
            )

    def _param(self, key: str, default: str = "", limit: int = 350) -> str:
        value = str(self.request.query.get(key, default)).strip()
        if len(value) > limit:
            raise RequestError("查询参数过长。")
        return value

    def _page(self) -> int:
        value = self._param("page", "1", 8)
        if not value.isascii() or not value.isdecimal() or int(value) < 1:
            raise RequestError("页码需要是正整数。")
        return int(value)

    @staticmethod
    def _field(body: dict, name: str, limit: int = 128) -> str:
        value = body[name]
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise RequestError("提交的群聊或玩家标识有误。")
        return value

    def _settings(self, snapshot: dict[str, Any], scope: str) -> dict:
        config = object_value(snapshot.get(f"{scope}:config"))
        defaults = {
            "draw_hourly_limit": self.plugin.draw_hourly_limit_default,
            "claim_cooldown": self.plugin.claim_cooldown_default,
            "harem_max_size": self.plugin.harem_max_size_default,
            "draw_cooldown": 0,
            "draw_time_range": "无限制",
        }
        return {key: config.get(key, default) for key, default in defaults.items()}

    def _character(self, cid: str) -> dict[str, str]:
        record = self.plugin.char_manager.get_character_by_id(cid) or {}
        return {
            "id": cid,
            "name": str(record.get("name") or f"角色 #{cid}"),
            "source": str(record.get("source") or ""),
        }

    async def overview(self) -> Any:
        async def load() -> dict[str, Any]:
            snapshot = await self.plugin.store.snapshot()
            groups = []
            for scope, players in group_players(snapshot).items():
                platform, _, group = scope.partition("|")
                groups.append(
                    {
                        "scope": scope,
                        "platform": unquote(platform),
                        "group": unquote(group),
                        "players": len(players),
                    }
                )
            groups.sort(key=lambda item: (item["platform"], item["group"]))
            return {
                "groups": groups,
                "players": sum(item["players"] for item in groups),
            }

        return await self._serve(load)

    async def players(self) -> Any:
        async def load() -> dict[str, Any]:
            scope = self._param("scope")
            query = self._param("q", limit=100).casefold()
            page, size = self._page(), 25
            snapshot = await self.plugin.store.snapshot()
            groups = group_players(snapshot)
            if scope not in groups:
                raise RequestError("找不到这条群聊记录，请刷新列表。", 404)
            rank = object_value(snapshot.get(f"{scope}_harem_heats"))
            players = []
            for uid in groups[scope]:
                prefix = f"{scope}:{uid}"
                name = str(snapshot.get(f"{prefix}:display_name") or "")
                if (
                    query
                    and query not in uid.casefold()
                    and query not in name.casefold()
                ):
                    continue
                partners = ids(snapshot.get(f"{prefix}:partners"))
                wishes = ids(snapshot.get(f"{prefix}:wish_list"))
                players.append((uid, name, partners, wishes))
            players.sort(key=lambda row: (-len(row[2]), row[1].casefold(), row[0]))
            start = (page - 1) * size
            items = []
            for uid, name, partners, wishes in players[start : start + size]:
                prefix = f"{scope}:{uid}"
                favorite = str(snapshot.get(f"{prefix}:fav") or "")
                pending = self.plugin.migration.is_pending_uid(uid)
                items.append(
                    {
                        "uid": "" if pending else uid,
                        "name": name,
                        "pending": pending,
                        "partners": [self._character(cid) for cid in partners],
                        "wishes": [self._character(cid) for cid in wishes],
                        "favorite": self._character(favorite)
                        if favorite in partners
                        else None,
                        "heat": number(rank.get(uid)),
                        "can_reset": not pending
                        and (
                            f"{prefix}:draw_status" in snapshot
                            or f"{prefix}:last_claim" in snapshot
                        ),
                    }
                )
            return {
                "items": items,
                "total": len(players),
                "page": page,
                "size": size,
                "settings": self._settings(snapshot, scope),
            }

        return await self._serve(load)

    async def _json_body(self, fields: set[str]) -> dict:
        content_type = (
            (self.request.content_type or "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            raise RequestError("请提交 JSON 请求。")
        raw = await self.request.body()
        if len(raw) > 4096:
            raise RequestError("请求内容过长。", 413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise RequestError("JSON 请求格式有误。") from None
        if not isinstance(body, dict) or set(body) != fields:
            raise RequestError("请重新填写所需信息。")
        return body

    async def reset(self) -> Any:
        async def update() -> dict[str, Any]:
            body = await self._json_body({"scope", "uid"})
            scope = self._field(body, "scope", 350)
            uid = self._field(body, "uid")
            if not PLAYER_ID.fullmatch(uid):
                raise RequestError("请选择有效的群聊和玩家。")
            await self._require_group(scope)
            async with (
                self.plugin._get_group_lock(scope),
                self.plugin.store.transaction(),
            ):
                await self._require_player(scope, uid)
                prefix = f"{scope}:{uid}"
                await self.plugin.store.delete(f"{prefix}:draw_status")
                await self.plugin.store.delete(f"{prefix}:last_claim")
            return {"uid": uid}

        return await self._serve(update)

    async def _require_group(self, scope: str) -> None:
        if scope not in group_players(await self.plugin.store.snapshot()):
            raise RequestError("请先在目标群使用游戏指令，再刷新群聊列表。", 404)

    async def _require_player(self, scope: str, uid: str) -> None:
        if self.plugin.migration.is_pending_uid(uid):
            raise RequestError("请选择已经使用过游戏指令的新服玩家。")
        if uid not in group_players(await self.plugin.store.snapshot()).get(
            scope, set()
        ):
            raise RequestError(
                "玩家记录尚未建立，请先让玩家在目标群使用游戏指令。", 404
            )

    async def migration(self) -> Any:
        async def load() -> dict[str, Any]:
            status = await self.plugin.migration.status()
            groups = status["source_groups"]
            return {
                "source_groups": groups,
                "linked": sum(group["linked"] for group in groups),
            }

        return await self._serve(load)

    async def pending(self) -> Any:
        async def load() -> dict[str, Any]:
            scope = self._param("scope")
            query = self._param("q", limit=100).casefold()
            page, size = self._page(), 25
            await self._require_group(scope)
            rows = await self.plugin.migration.pending(scope)
            matched = [
                row
                for row in rows
                if not query
                or any(
                    query in str(row.get(field) or "").casefold()
                    for field in ("old_uid", "nickname", "card")
                )
            ]
            matched.sort(key=lambda row: str(row["old_uid"]))
            offset = (page - 1) * size
            return {
                "items": matched[offset : offset + size],
                "total": len(matched),
                "page": page,
                "size": size,
            }

        return await self._serve(load)

    async def map_group(self) -> Any:
        async def update() -> dict[str, Any]:
            body = await self._json_body({"scope", "source_group"})
            scope = self._field(body, "scope", 350)
            source_group = self._field(body, "source_group")
            await self._require_group(scope)
            async with self.plugin._get_group_lock(scope):
                return await self.plugin.migration.map_group(scope, source_group)

        return await self._serve(update)

    async def manual_link(self) -> Any:
        async def update() -> dict[str, Any]:
            body = await self._json_body({"scope", "uid", "old_uid"})
            scope = self._field(body, "scope", 350)
            uid = self._field(body, "uid")
            old_uid = self._field(body, "old_uid")
            if not PLAYER_ID.fullmatch(uid) or not PLAYER_ID.fullmatch(old_uid):
                raise RequestError("请选择有效的新旧玩家标识。")
            await self._require_group(scope)
            async with (
                self.plugin._get_group_lock(scope),
                self.plugin.store.transaction(),
            ):
                await self._require_player(scope, uid)
                result = await self.plugin.migration.manual_link(scope, uid, old_uid)
            if result.status not in {"linked", "already_linked"}:
                raise RequestError(result.message, 409)
            return {"status": result.status, "message": result.message}

        return await self._serve(update)
