"""二次元抽抽乐的 AstrBot 命令与生命周期。"""

import asyncio
import random
import re
import secrets
import time
from urllib.parse import urlsplit

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from . import qq_official as qq
from .draw import DrawGame
from .exchange import ExchangeGame
from .game_runtime import GameRuntime, ID_PATTERN, PLUGIN_NAME
from .migration import Migration, MigrationError
from .storage import GameStore
from .util.character_manager import CharacterManager
from .webui import Dashboard


class AnimeDrawPlugin(GameRuntime, DrawGame, ExchangeGame, Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = GameStore(self.data_dir / "game.sqlite3")
        self.migration = Migration(self.store, self.data_dir)
        self.char_manager = CharacterManager()
        self.super_admins = {str(uid) for uid in config.get("super_admins", [])}
        self.draw_hourly_limit_default = max(1, config.get("draw_hourly_limit", 5))
        self.claim_cooldown_default = max(0, config.get("claim_cooldown", 3600))
        self.harem_max_size_default = max(1, config.get("harem_max_size", 20))
        self.custom_images_limit_default = max(0, config.get("custom_images_limit", 5))
        self.group_locks: dict[str, asyncio.Lock] = {}
        self._actions: dict[str, dict] = {}
        self._dashboard = None
        self._capture_lock = asyncio.Lock()
        self._redeem_after: dict[tuple[str, str], float] = {}

    async def initialize(self) -> None:
        if not await asyncio.to_thread(self.char_manager.load_characters):
            raise RuntimeError("无法加载角色卡池")
        await asyncio.to_thread(self.char_manager.load_bonds)
        await self.store.initialize()
        try:
            self._dashboard = Dashboard(self)
            self._dashboard.register()
        except ImportError:
            logger.info("二次元抽抽乐的数据管理页面需要 AstrBot 4.29.4 或更新版本。")

    async def terminate(self) -> None:
        if self._dashboard:
            self._dashboard.close()
        await self.store.close()

    async def _maybe_restore(self, event: AstrMessageEvent, gid: str) -> str:
        if not self.config.get("migration_enabled", False):
            return ""
        uid = str(event.get_sender_id())
        platform = event.get_platform_name()
        allow_name = self.config.get("migration_match_names", False) and platform in qq.OFFICIAL_PLATFORMS
        async with self._get_group_lock(gid):
            notice_key = f"{gid}:{uid}:migration_notice"
            prior = await self.store.get(notice_key)
            name = self._event_nickname(event) if allow_name else ""
            if allow_name and not name:
                name = await self.store.get(self.migration.official_nickname_key(gid, uid), "")
            if isinstance(prior, dict) and (prior.get("linked") or prior.get("candidate") == [allow_name, name]):
                return ""
            result = await self.migration.try_link(
                gid, uid, name, allow_name=allow_name,
                verified_qq=platform == "aiocqhttp",
            )
            if result.status in {"not_mapped", "already_linked"}:
                return ""
            await self.store.put(notice_key, {"candidate": [allow_name, name], "linked": result.status == "linked"})
        if result.status == "linked":
            return result.message
        return f"{result.message}\n认领指令：抽抽乐认领 <迁移码>"

    @filter.command("抽抽乐迁移采集")
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect_migration(self, event: AstrMessageEvent, mode: str = "本群"):
        """导入旧插件存档，采集 OneBot 旧玩家的 QQ 昵称和群名片。"""
        if not self.config.get("migration_source_enabled", False):
            return
        admin = event.is_admin() or str(event.get_sender_id()) in self.super_admins
        if not admin and (mode == "全部" or await self.get_group_role(event) not in {"owner", "admin"}):
            yield event.plain_result("此操作需要群管理员权限；采集全部旧群需要 AstrBot 管理员权限。")
            return
        event.should_call_llm(True)
        async with self._capture_lock:
            try:
                await self.migration.capture_legacy()
                if mode == "全部":
                    result = await self.migration.collect_all_profiles(event)
                    details = f"检查 {result['groups']} 个旧群，共采集 {result['profiles']} 份资料。"
                    if result["failed"]:
                        details += f"{result['failed']} 个群的成员列表暂不可用。"
                else:
                    result = await self.migration.collect_profiles(event)
                    details = f"本群共采集 {result['profiles']} 份资料。"
                await self.migration.export_archive()
                message = f"已导出迁移包。{details}"
            except MigrationError as exc:
                message = str(exc)
        yield event.plain_result(message)

    @filter.command("抽抽乐迁移码")
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def issue_migration_code(self, event: AstrMessageEvent, source_group: str | None = None):
        """私聊旧机器人，凭当前 QQ 账号取得自己的迁移码。"""
        if not self.config.get("migration_source_enabled", False):
            return
        event.should_call_llm(True)
        if not source_group:
            yield event.plain_result("用法：抽抽乐迁移码 <旧群号>")
            return
        try:
            code = await self.migration.issue_code(source_group, str(event.get_sender_id()))
            yield event.plain_result(f"你的迁移码是 {code}。\n请到新群发送「抽抽乐认领 {code}」，首次使用后失效。")
        except MigrationError as exc:
            yield event.plain_result(str(exc))

    @filter.command("抽抽乐认领")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def claim_migration(self, event: AstrMessageEvent, code: str | None = None):
        """在新群验证迁移码并认领旧存档。"""
        if not self._game_enabled(event) or not self.config.get("migration_enabled", False):
            return
        event.should_call_llm(True)
        if not code:
            yield event.plain_result("用法：抽抽乐认领 <迁移码>。没有迁移码时，可以联系管理员。")
            return
        gid, uid = self._scope(event), str(event.get_sender_id())
        now = time.monotonic()
        self._redeem_after = {key: until for key, until in self._redeem_after.items() if until > now}
        if (gid, uid) in self._redeem_after:
            yield event.plain_result("请等几秒再尝试认领。")
            return
        self._redeem_after[gid, uid] = now + 5
        await self._remember(event, gid)
        async with self._get_group_lock(gid):
            try:
                result = await self.migration.redeem(gid, uid, code)
                message = result.message
            except MigrationError as exc:
                message = str(exc)
        yield event.plain_result(message)

    @filter.command("抽抽乐导入迁移")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def import_migration(self, event: AstrMessageEvent):
        """将数据目录里的 migration.zip 载入迁移区。"""
        if not self.config.get("migration_enabled", False) or not event.get_group_id():
            return
        if not (event.is_admin() or str(event.get_sender_id()) in self.super_admins):
            return
        event.should_call_llm(True)
        try:
            await self.migration.import_archive()
            await self._remember(event, self._scope(event))
            yield event.plain_result("已载入迁移包。请打开「数据管理」关联新旧群聊。")
        except (MigrationError, OSError) as exc:
            logger.warning("二次元抽抽乐载入迁移包失败：%s", type(exc).__name__)
            yield event.plain_result("无法载入迁移包。请检查插件数据目录中的 migration.zip 和控制台日志。")

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_notice(self, event: AstrMessageEvent):
        """处理 OneBot 的群消息贴表情通知。"""
        if not self._game_enabled(event) or str(event.get_sender_id()) == str(event.get_self_id()):
            return
        raw = event.message_obj.raw_message
        if (
            str(getattr(raw, "post_type", "")) != "notice"
            or str(getattr(raw, "notice_type", "")) != "group_msg_emoji_like"
            or getattr(raw, "likes", None) == []
        ):
            return
        gid = self._scope(event)
        msg_id = getattr(raw, "message_id", None)
        if msg_id is None:
            return
        if self._get_action(gid, "draw", msg_id):
            response = await self._claim(event, msg_id)
        elif self._get_action(gid, "exchange", msg_id):
            response = await self._accept_exchange(event, msg_id)
        else:
            return
        if response:
            event.should_call_llm(True)
            yield event.plain_result(response)

    @filter.command("菜单", alias={"帮助"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_help_menu(self, event: AstrMessageEvent):
        '''显示帮助菜单'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        await self._remember(event, self._scope(event))
        menu_lines = [
            "抽卡 / ck　抽取群卡池角色",
            "结婚 <短码>　收集抽到的角色",
            "离婚 <角色ID>",
            "最爱 <角色ID>",
            "查询 <角色ID> [图片序号]",
            "搜索 <角色名称>",
            "我的后宫 [页码]",
            "群排行",
            "添加图片 <角色ID>",
            "清除图片 <角色ID>",
            "交换 <我的角色ID> <对方角色ID>",
            "交换 接受 <短码>",
            "许愿 <角色ID>",
            "愿望单",
            "删除许愿 <角色ID>",
            "",
            "群管理员",
            "系统设置 <功能> <参数>",
            "系统设置 可抽卡时间范围 HH:mm-HH:mm",
            "清理后宫 <玩家ID>",
            "强制离婚 <角色ID>",
            "",
            "群主或超管",
            "刷新 <玩家ID>",
            "终极轮回"
        ]
        actions = [("抽卡", "抽卡"), ("我的后宫", "我的后宫"),
                   ("愿望单", "愿望单"), ("群排行", "群排行")]
        async for result in self._render(event, "二次元抽抽乐", "\n".join(menu_lines), actions=actions):
            yield result

    @filter.command("抽卡", alias={"ck"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_draw(self, event: AstrMessageEvent):
        """从群卡池抽取一名角色。"""
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        async for result in self._draw(event):
            yield result

    @filter.command("结婚")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_marry(self, event: AstrMessageEvent, code: str | None = None):
        """使用抽卡短码或回复抽卡消息收集角色。"""
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        async for result in self._marry_command(event, code):
            yield result

    @filter.command("我的后宫")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_harem(self, event: AstrMessageEvent, page: int = 0):
        '''显示收集的人物列表'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        uid = str(event.get_sender_id())
        await self._remember(event, gid)
        lock = self._get_group_lock(gid)
        async with lock:
            marry_list_key = f"{gid}:{uid}:partners"
            marry_list = await self.get_kv_data(marry_list_key, [])
            if not marry_list:
                yield event.chain_result([
                    Comp.Reply(id=event.message_obj.message_id),
                    Comp.At(qq=uid),
                    Comp.Plain("，你的后宫空空如也。")
                ])
                harem_heats_key = f"{gid}_harem_heats"
                harem_heats = await self.get_kv_data(harem_heats_key, {}) or {}
                if uid in harem_heats:
                    del harem_heats[uid]
                await self.put_kv_data(harem_heats_key, harem_heats)
                return
            lines = []
            per_page = 10
            fav = await self.get_kv_data(f"{gid}:{uid}:fav", None)
            bond_status_list = self.char_manager.get_bond_collection_status(marry_list)
            char_bond_ratio = {}
            for name, owned, total, ratio, owned_cids in bond_status_list:
                for cid in owned_cids:
                    char_bond_ratio[cid] = max(char_bond_ratio.get(cid, 1.0), ratio)
            total_heat = 0
            entries = []
            for cid in marry_list:
                char = self.char_manager.get_character_by_id(cid)
                if char is None:
                    continue
                base_heat = float(char.get("heat") or 0)
                wished_by = await self.get_kv_data(f"{gid}:{cid}:wished_by", [])
                num_wishers = len(wished_by)
                bond_ratio = char_bond_ratio.get(int(cid), 1.0)
                effective_heat = base_heat * (1.1 ** num_wishers) * bond_ratio
                heat_int = int(round(effective_heat))
                total_heat += heat_int
                fav_mark = ""
                if fav and str(fav) == str(cid):
                    fav_mark = "⭐"
                line = f"{fav_mark}{char.get('name')} (ID: {cid})"
                if (num_wishers or bond_ratio > 1) and base_heat > 0:
                    pct_increase = (effective_heat - base_heat) / base_heat * 100
                    line += f" (+{pct_increase:.1f}%)"
                entries.append(line)
            harem_heats_key = f"{gid}_harem_heats"
            harem_heats = await self.get_kv_data(harem_heats_key, {}) or {}
            harem_heats[uid] = total_heat
            await self.put_kv_data(harem_heats_key, harem_heats)
            official = event.get_platform_name() in qq.OFFICIAL_PLATFORMS
            if page == 0 and not official:
                sender_name = await self._user_label(gid, uid)
                header_parts = [
                    Comp.Plain(f"{sender_name}的后宫\n总人气: {total_heat}")
                ]
                if fav and str(fav) in marry_list:
                    fav_char = self.char_manager.get_character_by_id(fav)
                    if fav_char:
                        images = fav_char.get("image") or []
                        image_url = random.choice(images) if images else None
                        if image_url:
                            header_parts.insert(0, Comp.Image.fromURL(image_url))
                node_list = [
                    Comp.Node(
                        uin=event.get_self_id(),
                        name=f"{sender_name}的后宫",
                        content=header_parts
                    )
                ]
                for idx in range(0, len(entries), per_page):
                    chunk = entries[idx:idx + per_page]
                    node_list.append(
                        Comp.Node(
                            uin=event.get_self_id(),
                            name=f"{sender_name}的后宫",
                            content=[Comp.Plain("\n".join(chunk))]
                        )
                    )
                if bond_status_list:
                    bond_lines = [
                        f"{name}（{owned}/{total}）（+{(ratio - 1) * 100:.0f}%）"
                        for name, owned, total, ratio, _ in bond_status_list
                    ]
                    node_list.append(
                        Comp.Node(
                            uin=event.get_self_id(),
                            name=f"{sender_name}的后宫",
                            content=[Comp.Plain("\n".join(bond_lines))]
                        )
                    )
                yield event.chain_result([
                    Comp.Nodes(node_list)
                ])
                return
            total_pages = max(1, (len(entries) + per_page - 1) // per_page)
            if page < 1:
                page = 1
            if page > total_pages:
                page = total_pages
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            lines.append(f"阵容总人气: {total_heat}")
            lines.extend(entries[start_idx:end_idx])
            lines.append(f"(第{page}/{total_pages}页)")
            nav = []
            if page > 1:
                nav.append(("上一页", f"我的后宫 {page - 1}"))
            if page < total_pages:
                nav.append(("下一页", f"我的后宫 {page + 1}"))
        async for result in self._render(event, "我的后宫", "\n".join(lines), actions=nav):
            yield result

    @filter.command("离婚")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_divorce(self, event: AstrMessageEvent, cid: str | None = None):
        """解除自己与指定角色的婚姻。"""
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        if not cid or not re.fullmatch(r"[0-9]{1,12}", cid):
            yield event.plain_result("用法：离婚 <角色ID>")
            return
        gid, uid, cid = self._scope(event), str(event.get_sender_id()), str(int(cid))
        owner_key = f"{gid}:{cid}:married_to"
        async with self._get_group_lock(gid), self.store.transaction():
            owner = await self.store.get(owner_key)
            if owner == uid:
                await self._remove_partner(gid, uid, cid)
                await self.store.delete(owner_key)
        char = self.char_manager.get_character_by_id(cid)
        name = char["name"] if char else cid
        yield event.plain_result(f"已与 {name} 离婚。" if owner == uid else "你尚未与该角色结婚。")

    async def _remove_partner(self, gid: str, uid: str, cid: str) -> None:
        key = f"{gid}:{uid}:partners"
        partners = await self.store.get(key, [])
        remaining = [str(value) for value in partners if str(value) != cid]
        if remaining != partners:
            await self.store.put(key, remaining)
        fav_key = f"{gid}:{uid}:fav"
        if str(await self.store.get(fav_key)) == cid:
            await self.store.delete(fav_key)


    @filter.command("交换")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_exchange(
        self, event: AstrMessageEvent, my_cid: str | None = None, other_cid: str | None = None
    ):
        """发起交换或输入短码接受邀请。"""
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        async for result in self._exchange(event, my_cid, other_cid):
            yield result

    @filter.command("最爱")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_favorite(self, event: AstrMessageEvent, cid: str | None = None):
        '''将指定角色设为最爱'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        user_id = str(event.get_sender_id())
        if cid is None or not str(cid).strip().isdigit():
            yield event.plain_result("用法：最爱 <角色ID>")
            return
        cid = str(cid).strip()
        marry_list_key = f"{gid}:{user_id}:partners"
        async with self._get_group_lock(gid), self.store.transaction():
            marry_list = await self.get_kv_data(marry_list_key, [])
            target = next((m for m in marry_list if str(m) == str(cid)), None)
            if not target:
                yield event.plain_result("你尚未与该角色结婚！")
                return
            cname = (self.char_manager.get_character_by_id(cid) or {}).get("name") or ""
            await self.put_kv_data(f"{gid}:{user_id}:fav", cid)
        msg_chain = [
            Comp.Plain("已将 "),
            Comp.Plain(cname or str(cid)),
            Comp.Plain(" 设为你的最爱。"),
        ]
        cmd_msg_id = event.message_obj.message_id
        if cmd_msg_id is not None:
            msg_chain.insert(0, Comp.Reply(id=str(cmd_msg_id)))
        yield event.chain_result(msg_chain)

    @filter.command("许愿")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_wish(self, event: AstrMessageEvent, cid: str | None = None):
        '''许愿指定角色，稍稍增加概率'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        user_id = str(event.get_sender_id())
        config = await self.get_group_cfg(gid)
        if cid is None or not str(cid).strip().isdigit():
            yield event.plain_result("用法：许愿 <角色ID>")
            return
        cid = str(cid).strip()
        char = self.char_manager.get_character_by_id(cid)
        if not char:
            yield event.plain_result(f"未找到ID为 {cid} 的角色")
            return
        wish_list_key = f"{gid}:{user_id}:wish_list"
        async with self._get_group_lock(gid), self.store.transaction():
            wish_list = await self.get_kv_data(wish_list_key, [])
            if cid not in wish_list and len(wish_list) >= config.get("harem_max_size", self.harem_max_size_default):
                yield event.chain_result([
                    Comp.Reply(id=str(event.message_obj.message_id)),
                    Comp.Plain(f"愿望单已满"),
                ])
                return
            if cid not in wish_list:
                wish_list.append(cid)
                await self.put_kv_data(wish_list_key, wish_list)
            wished_by_key = f"{gid}:{cid}:wished_by"
            wished_by = await self.get_kv_data(wished_by_key, [])
            if user_id not in wished_by:
                wished_by.append(user_id)
                await self.put_kv_data(wished_by_key, wished_by)
        yield event.chain_result([
            Comp.Reply(id=str(event.message_obj.message_id)),
            Comp.Plain(f"已许愿 {char.get('name')}"),
        ])

    @filter.command("愿望单")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_wish_list(self, event: AstrMessageEvent):
        '''查看愿望单'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        user_id = str(event.get_sender_id())
        wish_list_key = f"{gid}:{user_id}:wish_list"
        wish_list = await self.get_kv_data(wish_list_key, [])
        if not wish_list:
            yield event.chain_result([
                Comp.Reply(id=str(event.message_obj.message_id)),
                Comp.At(qq=user_id),
                Comp.Plain("你的愿望单为空"),
            ])
            return
        lines = []
        for cid in wish_list:
            married_to = await self.get_kv_data(f"{gid}:{cid}:married_to", None)
            char = self.char_manager.get_character_by_id(cid)
            if char is None:
                continue
            line = f"{char.get('name')}(ID: {cid})"
            if married_to:
                if str(married_to) == user_id:
                    line += "❤️"
                else:
                    line += "💔"
            lines.append(line)
        async for result in self._render(event, "愿望单", "\n".join(lines), actions=[("抽卡", "抽卡")]):
            yield result

    @filter.command("删除许愿")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_wish_clear(self, event: AstrMessageEvent, cid: str | None = None):
        '''从愿望单中删除指定角色'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        user_id = str(event.get_sender_id())
        if cid is None or not str(cid).strip().isdigit():
            yield event.plain_result("用法：删除许愿 <角色ID>")
            return
        cid = str(cid).strip()
        wish_list_key = f"{gid}:{user_id}:wish_list"
        async with self._get_group_lock(gid), self.store.transaction():
            wish_list = await self.get_kv_data(wish_list_key, [])
            wish_list = [x for x in wish_list if str(x) != cid]
            await self.put_kv_data(wish_list_key, wish_list)
            wished_by_key = f"{gid}:{cid}:wished_by"
            wished_by = await self.get_kv_data(wished_by_key, [])
            wished_by = [uid for uid in wished_by if str(uid) != user_id]
            if wished_by:
                await self.put_kv_data(wished_by_key, wished_by)
            else:
                await self.delete_kv_data(wished_by_key)
        yield event.chain_result([
            Comp.Reply(id=str(event.message_obj.message_id)),
            Comp.Plain(f"已从愿望单移除"),
        ])

    @filter.command("查询")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_query(self, event: AstrMessageEvent, cid: str | None = None, iid: str | None = None):
        '''查询指定角色的信息，可选图片序号 iid（1 到 图片数量）'''
        if not self._game_enabled(event):
            return
        gid = self._scope(event)
        config = await self.get_group_cfg(gid)
        query_cooldown = config.get("query_cooldown", 0)
        if query_cooldown > 0:
            last_query_ts = await self.get_kv_data(f"{gid}:last_query", 0)
            if (time.time() - last_query_ts) < query_cooldown:
                yield event.plain_result(f"查询冷却中，请等待{round(query_cooldown-(time.time()-last_query_ts),1)}秒后重试")
                return
        await self.put_kv_data(f"{gid}:last_query", time.time())
        event.should_call_llm(True)
        if cid is None:
            yield event.plain_result("用法：查询 <角色ID> [图片序号]")
            return

        cid_str = str(cid).strip()
        if cid_str.isdigit():
            cid_int = int(cid_str)
            char = self.char_manager.get_character_by_id(cid_int)
            if not char:
                yield event.plain_result(f"未找到ID为 {cid_int} 的角色")
                return
            async for res in self.print_character_info(event, char, iid=iid):
                yield res
                return
        else:
            async for res in self.handle_search(event, cid_str):
                yield res
                return

    async def print_character_info(self, event: AstrMessageEvent, char: dict, iid: str | None = None):
        """展示角色、关联作品、收集状态和图片。"""
        gid = self._scope(event)
        cid = str(char["id"])
        pool = await self._images(gid, char)
        index = 0
        if pool:
            index = int(iid) if iid and iid.isascii() and iid.isdecimal() and len(iid) <= 6 else 0
            if not 1 <= index <= len(pool):
                index = random.randint(1, len(pool))
        owner = await self.store.get(f"{gid}:{cid}:married_to")
        wishes = await self.store.get(f"{gid}:{cid}:wished_by", [])
        base_heat = float(char.get("heat") or 0)
        heat = round(base_heat * (1.1 ** min(100, len(wishes))))
        lines = [f"角色 ID：{cid}　｜　热度：{heat}"]
        works = self.char_manager.get_works(cid)
        if not works and self.config.get("online_work_lookup", True):
            works = await self.char_manager.get_works_online(cid)
        if works:
            lines.append("关联作品：" + "、".join(works[:3]))
        bonds = self.char_manager.get_bonds_for_character(cid)
        if bonds:
            lines.append("羁绊：" + "、".join(bonds))
        if owner:
            lines.append(f"已与 {await self._user_label(gid, str(owner))} 结婚。")
        if index:
            lines.append(f"图片 {index}/{len(pool)}")
        actions = [("许愿", f"许愿 {cid}"), ("抽卡", "抽卡")]
        if len(pool) > 1:
            actions.append(("下一张图", f"查询 {cid} {index % len(pool) + 1}"))
        async for result in self._render(event, char["name"], "\n".join(lines), pool[index - 1] if index else None, actions):
            yield result

    @filter.command("搜索")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_search(self, event: AstrMessageEvent, keyword: str | None = None):
        '''搜索角色'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        if not keyword:
            yield event.plain_result("用法：搜索 <角色名字/部分名字>")
            return
        keyword = str(keyword).strip()
        matches = self.char_manager.search_characters_by_name(keyword)
        if not matches:
            yield event.plain_result(f"未找到名称包含“{keyword}”的角色")
            return
        if len(matches) == 1:
            char = matches[0]
            async for res in self.print_character_info(event, char):
                yield res
                return
            return
        else:
            top = matches[:10]
            lines = [f"{c.get('name')} (ID: {c.get('id')})" for c in top]
            more = "" if len(matches) <= len(top) else f"\n..."
            yield event.plain_result("\n".join(lines) + more)

    @filter.command("添加图片")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_add_image(self, event: AstrMessageEvent, cid: str | None = None):
        '''添加图片'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        user_id = str(event.get_sender_id())
        if cid is None or not str(cid).strip().isdigit():
            yield event.plain_result("用法：添加图片 <角色ID>")
            return
        cid = int(str(cid).strip())
        lock = self._get_group_lock(gid)
        async with lock:
            partners_key = f"{gid}:{user_id}:partners"
            partners = await self.get_kv_data(partners_key, [])
            if str(cid) not in partners:
                yield event.plain_result("角色不在后宫中")
                return
            parts = event.message_obj.message or []
            images = [item for part in parts for item in (getattr(part, "chain", None) or [part])
                      if isinstance(item, Comp.Image)]
            if not images:
                yield event.plain_result("请发送一张图片，或在 OneBot 中回复图片。")
                return
            key = f"{gid}:{cid}:custom_images"
            paths = await self.store.get(key, [])
            if len(paths) + len(images) > self.custom_images_limit_default:
                yield event.plain_result(f"每个角色最多可添加 {self.custom_images_limit_default} 张图片。")
                return
            folder = self.data_dir / "img"
            folder.mkdir(exist_ok=True)
            previous_count = len(paths)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                for image in images:
                    url = image.url or image.file or ""
                    try:
                        name = await self._save_qq_image(session, url, folder)
                    except (aiohttp.ClientError, OSError, ValueError):
                        continue
                    paths.append(f"img/{name}")
            count = len(paths) - previous_count
            if count:
                await self.store.put(key, paths)
            yield event.plain_result(
                f"成功添加 {count} 张图片，当前共 {len(paths)} 张。"
                if count else "未能保存图片。请发送 QQ 图片附件，单张最大 8 MB。"
            )

    @staticmethod
    async def _save_qq_image(session: aiohttp.ClientSession, url: str, folder) -> str:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        qq_domains = ("qpic.cn", "qq.com", "qq.com.cn")
        if (
            parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443}
            or not any(hostname == domain or hostname.endswith(f".{domain}") for domain in qq_domains)
        ):
            raise ValueError("图片地址不在 QQ 附件域名内")
        max_bytes = 8 * 1024 * 1024
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200 or (response.content_length or 0) > max_bytes:
                raise ValueError("图片服务器响应无效")
            contents = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                contents.extend(chunk)
                if len(contents) > max_bytes:
                    raise ValueError("图片超出大小上限")
        if contents.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif contents.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif contents.startswith((b"GIF87a", b"GIF89a")):
            ext = "gif"
        elif contents.startswith(b"RIFF") and contents[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise ValueError("不支持的图片格式")
        name = f"{secrets.token_hex(16)}.{ext}"
        await asyncio.to_thread((folder / name).write_bytes, contents)
        return name

    @filter.command("清除图片")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_clear_image(self, event: AstrMessageEvent, cid: str | None = None):
        '''清除指定角色的自定义图片'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        user_id = str(event.get_sender_id())
        if cid is None or not str(cid).strip().isdigit():
            yield event.plain_result("用法：清除图片 <角色ID>")
            return
        cid = int(str(cid).strip())
        married_to = await self.get_kv_data(f"{gid}:{cid}:married_to", None)
        group_role = await self.get_group_role(event)
        if str(married_to) != user_id and group_role not in ['admin', 'owner'] and str(event.get_sender_id()) not in self.super_admins:
            yield event.plain_result("无权限：结婚用户或群管理员可清除该角色自定义图片")
            return
        custom_images_key = f"{gid}:{cid}:custom_images"
        paths = await self.get_kv_data(custom_images_key, [])
        if not paths:
            yield event.plain_result("没有自定义图片")
            return
        for path in paths:
            full_path = self._custom_path(path)
            if full_path:
                await asyncio.to_thread(full_path.unlink, missing_ok=True)
        await self.delete_kv_data(custom_images_key)
        yield event.chain_result([
            Comp.Reply(id=str(event.message_obj.message_id)),
            Comp.Plain(f"已清除该角色的自定义图片"),
        ])

    @filter.command("强制离婚")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_force_divorce(self, event: AstrMessageEvent, cid: str | None = None):
        """解除指定角色的婚姻。"""
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        group_role = await self.get_group_role(event)
        if group_role not in {"admin", "owner"} and str(event.get_sender_id()) not in self.super_admins:
            yield event.plain_result("无权限执行此命令。")
            return
        if not cid or not re.fullmatch(r"[0-9]{1,12}", cid):
            yield event.plain_result("用法：强制离婚 <角色ID>")
            return
        gid, cid = self._scope(event), str(int(cid))
        async with self._get_group_lock(gid), self.store.transaction():
            key = f"{gid}:{cid}:married_to"
            owner = await self.store.get(key)
            users = await self.get_user_list(gid)
            if owner:
                users.add(str(owner))
            for uid in users:
                await self._remove_partner(gid, uid, cid)
            await self.store.delete(key)
        char = self.char_manager.get_character_by_id(cid)
        yield event.plain_result(f"{char['name'] if char else cid} 已被强制解除婚约。")


    @filter.command("清理后宫")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_clear_harem(self, event: AstrMessageEvent, uid: str | None = None):
        '''清理指定用户的后宫，最爱会被保留（管理员专用）'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        group_role = await self.get_group_role(event)
        if group_role not in ['admin', 'owner'] and str(event.get_sender_id()) not in self.super_admins:
            yield event.plain_result("无权限执行此命令。")
            return
        gid = self._scope(event)
        lock = self._get_group_lock(gid)
        async with lock, self.store.transaction():
            if not uid or not re.fullmatch(ID_PATTERN, uid):
                yield event.plain_result("用法：清理后宫 <玩家ID>")
                return
            uid = str(uid).strip()
            fav = await self.get_kv_data(f"{gid}:{uid}:fav", None)
            marry_list = await self.get_kv_data(f"{gid}:{uid}:partners", [])
            if not marry_list:
                await self.delete_kv_data(f"{gid}:{uid}:fav")
                await self.delete_kv_data(f"{gid}:{uid}:partners")
                yield event.plain_result(f"{uid} 的后宫为空")
                return
            for cid in marry_list:
                if str(cid) == str(fav):
                    continue
                await self.delete_kv_data(f"{gid}:{cid}:married_to")
            if fav is None:
                await self.delete_kv_data(f"{gid}:{uid}:partners")
            elif fav not in marry_list:
                await self.delete_kv_data(f"{gid}:{uid}:fav")
                await self.delete_kv_data(f"{gid}:{uid}:partners")
            else:
                await self.put_kv_data(f"{gid}:{uid}:partners", [fav])
            yield event.plain_result(f"已清理 {uid} 的后宫")

    @filter.command("系统设置")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_config(self, event: AstrMessageEvent, feature: str | None = None, value: str | None = None):
        '''系统设置（管理员专用）'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        group_role = await self.get_group_role(event)
        if group_role not in ['admin', 'owner'] and str(event.get_sender_id()) not in self.super_admins:
            yield event.plain_result("无权限执行此命令。")
            return
        config = await self.get_group_cfg(self._scope(event))
        menu_lines = [
            "用法：",
            "系统设置 抽卡冷却 [0~60]",
            f"———抽卡冷却（秒） | 当前值: {config.get('draw_cooldown', 0)}",
            "系统设置 查询冷却 [0~60]",
            f"———查询冷却（秒） | 当前值: {config.get('query_cooldown', 0)}",
            "系统设置 抽卡次数 [1~10]",
            f"———每小时抽卡次数 | 当前值: {config.get('draw_hourly_limit', self.draw_hourly_limit_default)}",
            "系统设置 后宫上限 [5~50]",
            f"———后宫人数上限 | 当前值: {config.get('harem_max_size', self.harem_max_size_default)}",
            "系统设置 抽卡范围 [>5000]",
            f"———抽卡热度范围 | 当前值: {config.get('draw_scope', '无')}",
            "系统设置 牛头人 [0~100]",
            f"———牛头人概率 | 当前值: {config.get('ntr_chance', 10)}",
            "系统设置 可抽卡时间范围 HH:mm-HH:mm",
            f"———可抽卡时间范围 | 当前值: {config.get('draw_time_range', '无限制')}"
        ]
        if feature is None:
            yield event.chain_result([Comp.Plain("\n".join(menu_lines))])
            return
        feature = str(feature).strip()
        if feature == "抽卡冷却":
            if value is None or not str(value).strip().isdigit():
                yield event.plain_result("用法：抽卡冷却 [0~60](秒)")
                return
            time = int(str(value).strip())
            if time < 0:
                time = 0
            if time > 60:
                yield event.plain_result("时间不能超过60秒")
                return
            await self.update_group_cfg(self._scope(event), "draw_cooldown", time)
            yield event.plain_result(f"抽卡冷却已设置为{time}秒")
        elif feature == "查询冷却":
            if value is None or not str(value).strip().isdigit():
                yield event.plain_result("用法：查询冷却 [0~60](秒)")
                return
            time = int(str(value).strip())
            if time < 0:
                time = 0
            if time > 60:
                yield event.plain_result("时间不能超过60秒")
                return
            await self.update_group_cfg(self._scope(event), "query_cooldown", time)
            yield event.plain_result(f"查询冷却已设置为{time}秒")
        elif feature == "抽卡次数":
            if value is None or not str(value).strip().isdigit():
                yield event.plain_result("用法：抽卡次数 [1~10]")
                return
            count = int(str(value).strip())
            if count < 1:
                count = 1
            if count > 10:
                yield event.plain_result("次数不能超过10次")
                return
            await self.update_group_cfg(self._scope(event), "draw_hourly_limit", count)
            yield event.plain_result(f"每小时抽卡次数已设置为{count}次")
        elif feature == "后宫上限":
            if value is None or not str(value).strip().isdigit():
                yield event.plain_result("用法：后宫上限 [5~50]")
                return
            count = int(str(value).strip())
            if count < 5:
                count = 5
            if count > 50:
                count = 50
            await self.update_group_cfg(self._scope(event), "harem_max_size", count)
            yield event.plain_result(f"后宫上限已设置为{count}")
        elif feature == "抽卡范围":
            if value is None or not str(value).strip().isdigit():
                yield event.plain_result("用法：抽卡范围 [>5000]")
                return
            scope = int(str(value).strip())
            if scope < 5000:
                scope = 5000
            await self.update_group_cfg(self._scope(event), "draw_scope", scope)
            yield event.plain_result(f"抽卡范围已设置为热度前{scope}")
        elif feature == "牛头人":
            if value is None or not str(value).strip().isdigit():
                yield event.plain_result("用法：牛头人 [0~100]")
                return
            value = int(str(value).strip())
            if value < 0 or value > 100:
                yield event.plain_result("用法：牛头人 [0~100]")
                return
            await self.update_group_cfg(self._scope(event), "ntr_chance", value)
            yield event.plain_result(f"现在抽到别人对象有{value}%概率可牛")
        elif feature == "可抽卡时间范围":
            if value in {"关闭", "不限"}:
                await self.update_group_cfg(self._scope(event), "draw_time_range", None)
                yield event.plain_result("已清除抽卡时段限制。")
                return
            if value is None:
                yield event.plain_result("用法：可抽卡时间范围 HH:mm-HH:mm 或 关闭")
                return
            time_range_match = re.search(r'^(\d{2}:\d{2})-(\d{2}:\d{2})$', str(value).strip())
            if not time_range_match:
                yield event.plain_result("用法：可抽卡时间范围 HH:mm-HH:mm")
                return
            start_time = time_range_match.group(1)
            end_time = time_range_match.group(2)
            start_hour, start_min = map(int, start_time.split(':'))
            end_hour, end_min = map(int, end_time.split(':'))
            if start_hour < 0 or start_hour > 23 or start_min < 0 or start_min > 59:
                yield event.plain_result("开始时间无效")
                return
            if end_hour < 0 or end_hour > 23 or end_min < 0 or end_min > 59:
                yield event.plain_result("结束时间无效")
                return
            await self.update_group_cfg(self._scope(event), "draw_time_range", f"{start_time}-{end_time}")
            yield event.plain_result(f"可抽卡时间范围已设置为 {start_time} ～ {end_time}")
        else:
            yield event.chain_result([Comp.Plain("\n".join(menu_lines))])

    @filter.command("刷新")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_refresh(self, event: AstrMessageEvent, user_id: str | None = None):
        '''刷新指定用户的抽卡和结婚冷却（群主和超管专用）'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        group_role = await self.get_group_role(event)
        if group_role not in ['owner'] and str(event.get_sender_id()) not in self.super_admins:
            yield event.plain_result("无权限执行此命令。")
            return
        if not user_id or not re.fullmatch(ID_PATTERN, user_id):
            yield event.plain_result("用法：刷新 <玩家ID>")
            return
        gid = self._scope(event)
        async with self._get_group_lock(gid), self.store.transaction():
            await self.store.delete(f"{gid}:{user_id}:draw_status")
            await self.store.delete(f"{gid}:{user_id}:last_claim")
        yield event.plain_result("次数已重置，结婚冷却已清除")

    @filter.command("群排行")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_rank(self, event: AstrMessageEvent):
        '''显示群排名'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        gid = self._scope(event)
        harem_heats_key = f"{gid}_harem_heats"
        harem_heats = await self.get_kv_data(harem_heats_key, {}) or {}
        if not harem_heats:
            yield event.plain_result("暂无排名数据")
            return
        sorted_heats = sorted(harem_heats.items(), key=lambda item: item[1], reverse=True)[:10]
        rank_lines = [
            f"{idx}. {await self._user_label(gid, str(uid))}　{heat}"
            for idx, (uid, heat) in enumerate(sorted_heats, 1)
        ]
        async for result in self._render(event, "群排行", "\n".join(rank_lines),
                                         actions=[("我的后宫", "我的后宫")]):
            yield result

    @filter.command("终极轮回")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_ultimate_reset(self, event: AstrMessageEvent, confirm: str | None = None):
        '''清除本群所有角色婚姻信息（除了最爱角色）（群主和超管专用）'''
        if not self._game_enabled(event):
            return
        event.should_call_llm(True)
        group_role = await self.get_group_role(event)
        if group_role not in ['owner'] and str(event.get_sender_id()) not in self.super_admins:
            yield event.plain_result("无权限执行此命令。")
            return
        if str(confirm) != "确认":
            yield event.plain_result("确定要进行终极轮回吗？此操作将清除本群所有角色婚姻信息（除了最爱角色）。\n如果确定要执行，请使用“终极轮回 确认”")
            return
        gid = self._scope(event)
        lock = self._get_group_lock(gid)
        async with lock, self.store.transaction():
            users = await self.get_kv_data(f"{gid}:user_list", [])
            harem_heats_key = f"{gid}_harem_heats"
            await self.delete_kv_data(harem_heats_key)
            for uid in users:
                fav = await self.get_kv_data(f"{gid}:{uid}:fav", None)
                marry_list = await self.get_kv_data(f"{gid}:{uid}:partners", [])
                if not marry_list:
                    await self.delete_kv_data(f"{gid}:{uid}:fav")
                    await self.delete_kv_data(f"{gid}:{uid}:partners")
                    continue
                for cid in marry_list:
                    if str(cid) == str(fav):
                        continue
                    await self.delete_kv_data(f"{gid}:{cid}:married_to")
                if fav is None:
                    await self.delete_kv_data(f"{gid}:{uid}:partners")
                elif fav not in marry_list:
                    await self.delete_kv_data(f"{gid}:{uid}:fav")
                    await self.delete_kv_data(f"{gid}:{uid}:partners")
                else:
                    await self.put_kv_data(f"{gid}:{uid}:partners", [fav])
            yield event.plain_result("已清除本群所有角色婚姻信息")

