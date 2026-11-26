"""Trading offers and their group-scoped acceptance."""

import re

from astrbot.api.event import AstrMessageEvent

from . import qq_official as qq
from .game_runtime import DRAW_MSG_TTL, OFFICIAL_ACTION_TTL


class ExchangeGame:
    async def _exchange(self, event: AstrMessageEvent, my_cid: str, other_cid: str):
        if my_cid == "接受" and other_cid:
            response = await self._accept_exchange(event, other_cid)
            yield event.plain_result(response or "请求已过期，或只有被邀请的玩家可以接受。")
            return
        if not all(re.fullmatch(r"[0-9]{1,12}", str(value)) for value in (my_cid, other_cid)):
            yield event.plain_result("用法：交换 <我的角色ID> <对方角色ID>，或「交换 接受 短码」。")
            return
        my_cid, other_cid = str(int(my_cid)), str(int(other_cid))
        gid = self._scope(event)
        from_uid = str(event.get_sender_id())
        official = event.get_platform_name() in qq.OFFICIAL_PLATFORMS
        ttl = OFFICIAL_ACTION_TTL if official else DRAW_MSG_TTL
        async with self._get_group_lock(gid):
            owner = await self.store.get(f"{gid}:{my_cid}:married_to")
            to_uid = await self.store.get(f"{gid}:{other_cid}:married_to")
            if from_uid != owner:
                yield event.plain_result("你需要先拥有要拿去交换的角色。")
                return
            if not to_uid or from_uid == to_uid:
                yield event.plain_result("请指定另一位玩家拥有的角色。")
                return
            code, record = self._new_action(
                gid, "exchange", ttl, from_uid=from_uid, to_uid=to_uid, from_cid=my_cid, to_cid=other_cid
            )
            my_name = (self.char_manager.get_character_by_id(my_cid) or {}).get("name", my_cid)
            other_name = (self.char_manager.get_character_by_id(other_cid) or {}).get("name", other_cid)
            body = f"{my_name} ↔ {other_name}"
            manual_hint = f"被邀请的玩家可在 {ttl} 秒内发送「交换 接受 {code}」。"
            if not official:
                chain = [
                    {"type": "at", "data": {"qq": to_uid}},
                    {"type": "text", "data": {"text": f" {body}\n{manual_hint}\n也可以给这条消息贴表情。"}},
                ]
                msg_id = await self._onebot_send(event, chain)
                if msg_id is not None:
                    self._actions[gid][str(msg_id)] = record
                    return
        async for result in self._render(
            event, "交换邀请", body, actions=[("接受交换", f"交换 接受 {code}")],
            button_owner=to_uid, manual_hint=manual_hint,
        ):
            yield result

    async def _accept_exchange(self, event: AstrMessageEvent, reference: str | int) -> str | None:
        gid = self._scope(event)
        async with self._get_group_lock(gid):
            req = self._get_action(gid, "exchange", reference)
            if not req or str(event.get_sender_id()) != req["to_uid"]:
                return None
            users = (req["from_uid"], req["to_uid"])
            cards = (req["from_cid"], req["to_cid"])
            async with self.store.transaction():
                owners = [await self.store.get(f"{gid}:{cid}:married_to") for cid in cards]
                partner_keys = [f"{gid}:{uid}:partners" for uid in users]
                partners = [[str(cid) for cid in await self.store.get(key, [])] for key in partner_keys]
                if owners != list(users) or any(cid not in collection for cid, collection in zip(cards, partners)):
                    return "角色归属已变化，交换无法继续。"
                for index, (uid, cid, collection, key) in enumerate(zip(users, cards, partners, partner_keys)):
                    other = 1 - index
                    fav_key = f"{gid}:{uid}:fav"
                    if str(await self.store.get(fav_key)) == cid:
                        await self.store.delete(fav_key)
                    await self.store.put(key, [value for value in collection if value != cid] + [cards[other]])
                    await self.store.put(f"{gid}:{cid}:married_to", users[other])
            req["used"] = True
        names = [(self.char_manager.get_character_by_id(cid) or {}).get("name", cid) for cid in cards]
        return f"交换完成：{names[0]} ↔ {names[1]}"
