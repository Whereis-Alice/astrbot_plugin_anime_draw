"""Draw and collection operations for OneBot and official QQ groups."""

import datetime
import random
import re
import time

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

from . import qq_official as qq
from .game_runtime import DRAW_MSG_TTL, GAME_TIMEZONE, OFFICIAL_ACTION_TTL


class DrawGame:
    async def _draw(self, event: AstrMessageEvent):
        gid = self._scope(event)
        uid = str(event.get_sender_id())
        await self._remember(event, gid)
        restored = await self._maybe_restore(event, gid)
        if restored:
            yield event.plain_result(restored)
        official = event.get_platform_name() in qq.OFFICIAL_PLATFORMS
        async with self._get_group_lock(gid):
            now = time.time()
            config = await self.get_group_cfg(gid)
            local = datetime.datetime.fromtimestamp(now, GAME_TIMEZONE)
            window = config.get("draw_time_range")
            if isinstance(window, str) and re.fullmatch(r"[0-2]\d:[0-5]\d-[0-2]\d:[0-5]\d", window):
                start, end = window.split("-")
                current = local.strftime("%H:%M")
                allowed = start <= current <= end if start <= end else current >= start or current <= end
                if not allowed:
                    yield event.plain_result(f"当前抽卡时段为 {start}～{end}（北京时间）。")
                    return
            key = f"{gid}:{uid}:draw_status"
            bucket = f"{local.year}-{local.timetuple().tm_yday}-{local.hour}"
            previous, count = await self.store.get(key, [None, 0])
            if previous != bucket:
                count = 0
            limit = config.get("draw_hourly_limit", self.draw_hourly_limit_default)
            if count >= limit:
                if count == limit:
                    await self.store.put(key, [bucket, count + 1])
                    yield event.plain_result("本小时抽卡次数已用完。")
                return
            last_draw = await self.store.get(f"{gid}:last_draw", 0)
            if now - last_draw < max(2, config.get("draw_cooldown", 0)):
                return
            wishes = await self.store.get(f"{gid}:{uid}:wish_list", [])
            char = None
            if wishes and random.random() < 0.003:
                char = self.char_manager.get_character_by_id(random.choice(wishes))
            char = char or self.char_manager.get_random_character(limit=config.get("draw_scope"))
            if not char:
                yield event.plain_result("卡池暂时不可用。")
                return
            cid = str(char["id"])
            owner = await self.store.get(f"{gid}:{cid}:married_to")
            wishers = await self.store.get(f"{gid}:{cid}:wished_by", [])
            contest = (
                bool(owner)
                and not self.migration.is_pending_uid(owner)
                and random.random() * 100 < config.get("ntr_chance", 10) * (1 + len(wishers))
            )
            images = await self._images(gid, char)
            image = random.choice(images) if images else None
            record = None
            ttl = OFFICIAL_ACTION_TTL if official else DRAW_MSG_TTL
            lines = [f"角色 ID：{cid}　｜　本小时剩余：{limit - count - 1}"]
            source = self.char_manager.get_works(cid)
            if source:
                lines.append(f"关联作品：{source[0]}")
            if owner:
                lines.append(f"已与 {await self._user_label(gid, str(owner))} 结婚。")
            if wishers and (not owner or contest):
                lines.append(f"{len(wishers)} 位玩家许愿了这个角色。")
            actions = [("再抽一次", "抽卡")]
            if not owner or contest:
                code, record = self._new_action(gid, "draw", ttl, cid=cid, owner=owner)
                lines.append(f"{ttl} 秒内发送：结婚 {code}")
                actions.insert(0, ("结婚", f"结婚 {code}"))
            if not official:
                chain = [{"type": "text", "data": {"text": f"{char['name']}\n"}}]
                for wisher in wishers[:10] if not owner or contest else []:
                    if str(wisher).isdecimal():
                        chain.append({"type": "at", "data": {"qq": str(wisher)}})
                if record:
                    lines.append("也可以回复抽卡消息「结婚」或给结果贴表情。")
                chain.append({"type": "text", "data": {"text": "\n".join(lines)}})
                if image:
                    file = image if image.startswith(("http://", "https://")) else self._image_component(image).file
                    chain.append({"type": "image", "data": {"file": file}})
                msg_id = await self._onebot_send(event, chain)
                if msg_id is not None:
                    if record:
                        self._actions[gid][str(msg_id)] = record
                    await self._record_draw(gid, key, bucket, count, now)
                    if record:
                        await self._onebot_react(event, msg_id, 128046 if contest else 66)
                    return
            await self._record_draw(gid, key, bucket, count, now)
        async for result in self._render(event, char["name"], "\n".join(lines), image, actions, button_owner=""):
            yield result

    async def _record_draw(self, gid: str, key: str, bucket: str, count: int, now: float) -> None:
        async with self.store.transaction():
            await self.store.put(key, [bucket, count + 1])
            await self.store.put(f"{gid}:last_draw", now)

    async def _daily_ally(self, event: AstrMessageEvent):
        gid = self._scope(event)
        await self._remember(event, gid)
        restored = await self._maybe_restore(event, gid)
        if restored:
            yield event.plain_result(restored)
        uid = str(event.get_sender_id())
        key = f"{gid}:{uid}:daily_ally"
        today = datetime.datetime.now(GAME_TIMEZONE).date().isoformat()
        async with self._get_group_lock(gid), self.store.transaction():
            cached = await self.store.get(key, {})
            char = self.char_manager.get_character_by_id(cached.get("cid")) if cached.get("date") == today else None
            if not char:
                config = await self.get_group_cfg(gid)
                char = self.char_manager.get_random_character(limit=config.get("draw_scope"))
                await self.store.put(key, {"date": today, "cid": char["id"]})
        source = self.char_manager.get_works(char["id"])
        lines = [f"{char['name']}（ID {char['id']}）"]
        if source:
            lines.append(f"关联作品：{source[0]}")
        lines.append("每天 00:00（北京时间）更新。")
        images = await self._images(gid, char)
        actions = [("抽卡", "抽卡"), ("查看角色", f"查询 {char['id']}"), ("我的后宫", "我的后宫")]
        async for result in self._render(event, "今日盟友", "\n".join(lines), images[0] if images else None, actions):
            yield result

    async def _marry_command(self, event: AstrMessageEvent, code: str | None):
        reference = code or next(
            (part.id for part in event.message_obj.message or [] if isinstance(part, Comp.Reply) and part.id), None
        )
        if not reference:
            yield event.plain_result("请发送「结婚 短码」，或在 OneBot 回复抽卡结果「结婚」。")
            return
        response = await self._claim(event, reference)
        yield event.plain_result(response or "这张卡的收集机会已过期或已被使用。")

    async def _claim(self, event: AstrMessageEvent, reference: str | int) -> str | None:
        gid = self._scope(event)
        user = str(event.get_sender_id())
        await self._remember(event, gid)
        async with self._get_group_lock(gid):
            draw = self._get_action(gid, "draw", reference)
            if not draw:
                return None
            cid = draw["cid"]
            owner_key = f"{gid}:{cid}:married_to"
            char = self.char_manager.get_character_by_id(cid)
            async with self.store.transaction():
                owner = await self.store.get(owner_key)
                if owner == user:
                    return f"{char['name']} 已与你结婚。"
                if owner != draw["owner"]:
                    return "这张卡的归属已经变化，请等待新的抽卡结果。"
                now = time.time()
                config = await self.get_group_cfg(gid)
                last_claim = await self.store.get(f"{gid}:{user}:last_claim", 0)
                cooldown = config.get("claim_cooldown", self.claim_cooldown_default)
                if cooldown > now - last_claim:
                    minutes = max(1, int((cooldown - now + last_claim + 59) // 60))
                    return f"结婚冷却中，还需等待 {minutes} 分钟。"
                key = f"{gid}:{user}:partners"
                partners = [str(value) for value in await self.store.get(key, [])]
                cap = config.get("harem_max_size", self.harem_max_size_default)
                if len(partners) >= cap:
                    return f"你的后宫已达到 {cap} 人的上限。"
                if owner:
                    fav_key = f"{gid}:{owner}:fav"
                    if str(await self.store.get(fav_key)) == cid:
                        if random.random() < 0.7:
                            draw["used"] = True
                            return "这次未能收集，对方的最爱守护成功。"
                        await self.store.delete(fav_key)
                    previous_key = f"{gid}:{owner}:partners"
                    previous = await self.store.get(previous_key, [])
                    await self.store.put(previous_key, [value for value in previous if str(value) != cid])
                if cid not in partners:
                    partners.append(cid)
                await self.store.put(key, partners)
                await self.store.put(owner_key, user)
                await self.store.put(f"{gid}:{user}:last_claim", now)
            draw["used"] = True
        return f"🎉 你与 {char['name']} 结婚了！"
