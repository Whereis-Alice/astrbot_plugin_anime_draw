"""Stage legacy AstrBot records and restore group accounts after verification."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from .storage import GameStore

LEGACY_PLUGIN_ID = "kennylimz/astrbot_plugin_mudae_qq"
ARCHIVE_FORMAT = "astrbot_plugin_anime_draw.migration"
CODE_TTL = 30 * 86400
ARCHIVE_LIMIT = 64 * 1024 * 1024
MANIFEST_LIMIT = 8 * 1024 * 1024
IMAGE_LIMIT = 10 * 1024 * 1024
ARCHIVE_FILENAME = "migration.zip"
SOURCE_LIST_KEY = "migration:sources"

_GROUP_ID = re.compile(r"(?:[0-9]{1,32}|global)\Z")
_OLD_UID = re.compile(r"[0-9]{1,32}\Z")
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_SCOPE = re.compile(r"(?:[\w.~%-]+)\|(?:[\w.~%-]+)\Z", re.ASCII)
_IMAGE_NAME = re.compile(r"img/legacy-[0-9a-f]{64}\.(?:png|jpg|gif|webp)\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT_FIELDS = {"partners", "fav", "wish_list", "draw_status", "last_claim"}


class MigrationError(ValueError):
    """A migration operation needs administrator or player attention."""


@dataclass(frozen=True, slots=True)
class LinkResult:
    status: str
    message: str


def _source_key(group: str) -> str:
    return f"migration:archive:{group}"


def _target_key(scope: str) -> str:
    return f"migration:target:{scope}"


def _mapping_key(group: str) -> str:
    return f"migration:mapped:{group}"


def _seed_key(group: str) -> str:
    return f"migration:seed:{group}"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _claim_code(seed: str, group: str, uid: str, issued_at: int) -> str:
    digest = hmac.digest(bytes.fromhex(seed), f"{group}|{uid}|{issued_at}".encode(), "sha256")
    return base64.urlsafe_b64encode(digest[:24]).decode()


def _old_group(value: str) -> str:
    value = str(value)
    if not _GROUP_ID.fullmatch(value):
        raise MigrationError("旧群号格式无效。")
    return value


def _new_scope(value: str) -> str:
    if not isinstance(value, str) or len(value) > 600 or not _SCOPE.fullmatch(value):
        raise MigrationError("目标群标识无效，请选择管理页面中的群聊。")
    return value


def _ids(values: object, pattern: re.Pattern = _OLD_UID) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value) for value in values if pattern.fullmatch(str(value))))


def _name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:128]


def _match_name(value: object) -> str:
    return unicodedata.normalize("NFKC", _name(value)).casefold()


def _index(rows: dict) -> tuple[dict[str, dict], dict[str, str], dict[str, list[str]]]:
    """Reconcile old forward and reverse character indexes."""
    accounts: dict[str, dict] = {}
    owners: dict[str, str] = {}
    wishes: dict[str, list[str]] = {}
    for key, value in rows.items():
        ident, sep, field = key.partition(":")
        if not sep or not _OLD_UID.fullmatch(ident):
            continue
        if field in _ACCOUNT_FIELDS:
            accounts.setdefault(ident, {})[field] = value
        elif field == "married_to" and _OLD_UID.fullmatch(str(value)):
            owners[ident] = str(value)
        elif field == "wished_by":
            wishes[ident] = _ids(value)
    for uid, fields in accounts.items():
        for cid in _ids(fields.get("partners")):
            owners.setdefault(cid, uid)
        for cid in _ids(fields.get("wish_list")):
            if uid not in wishes.setdefault(cid, []):
                wishes[cid].append(uid)
    indexed_uids = set(owners.values()) | {uid for group in wishes.values() for uid in group}
    for uid in sorted(indexed_uids):
        accounts.setdefault(uid, {})
    heats = rows.get("_harem_heats")
    if isinstance(heats, dict):
        for uid in heats:
            if _OLD_UID.fullmatch(uid):
                accounts.setdefault(uid, {})
    return accounts, owners, wishes


def _split_groups(values: dict) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for key, value in values.items():
        if key.endswith("_harem_heats"):
            group, slot = key.removesuffix("_harem_heats"), "_harem_heats"
        else:
            group, sep, slot = key.partition(":")
            if not sep:
                continue
        if _GROUP_ID.fullmatch(group) and slot:
            groups.setdefault(group, {})[slot] = value
    return groups


def _image_extension(contents: bytes) -> str | None:
    if contents.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if contents.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if contents.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if contents.startswith(b"RIFF") and contents[8:12] == b"WEBP":
        return "webp"
    return None


def _read_image(legacy_dir: Path, old_name: str) -> bytes:
    img_dir = (legacy_dir / "img").resolve()
    relative = Path(old_name)
    if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "img":
        return b""
    path = (legacy_dir / relative).resolve()
    if not path.is_relative_to(img_dir) or not path.is_file():
        return b""
    if path.stat().st_size > IMAGE_LIMIT:
        return b""
    with path.open("rb") as source:
        return source.read(IMAGE_LIMIT + 1)


def _install_image(data_dir: Path, name: str, contents: bytes) -> None:
    path = data_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.stat().st_size != len(contents) or path.read_bytes() != contents:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
        try:
            temporary.write_bytes(contents)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _collect_images(data_dir: Path, legacy_dir: Path, groups: dict) -> tuple[dict, int]:
    captured: dict[str, dict[str, str]] = {}
    by_name: dict[str, str] = {}
    missing: set[str] = set()
    for group, rows in groups.items():
        for key, paths in rows.items():
            if not key.endswith(":custom_images") or not isinstance(paths, list):
                continue
            for old_name in (value for value in paths if isinstance(value, str)):
                if old_name not in by_name and old_name not in missing:
                    try:
                        contents = _read_image(legacy_dir, old_name)
                    except (OSError, ValueError):
                        contents = b""
                    extension = _image_extension(contents)
                    if not extension or len(contents) > IMAGE_LIMIT:
                        missing.add(old_name)
                        continue
                    name = f"img/legacy-{_digest(contents)}.{extension}"
                    _install_image(data_dir, name, contents)
                    by_name[old_name] = name
                if old_name in by_name:
                    captured.setdefault(group, {})[old_name] = by_name[old_name]
    return captured, len(missing)


def _archive_summary(groups: dict) -> dict:
    return {
        "groups": len(groups),
        "accounts": sum(len(_index(group["rows"])[0]) for group in groups.values()),
        "profiles": sum(len(group["profiles"]) for group in groups.values()),
        "images": len(_archive_images(groups)),
    }


def _archive_images(groups: dict) -> set[str]:
    return {name for group in groups.values() for name in group["images"].values()}


def _write_archive(path: Path, data_dir: Path, groups: dict) -> None:
    data = json.dumps(
        {"format": ARCHIVE_FORMAT, "version": 1, "groups": groups},
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(data) > MANIFEST_LIMIT:
        raise MigrationError("迁移包的数据记录超过大小限制。")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    total = len(data)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("migration.json", data)
            for name in sorted(_archive_images(groups)):
                asset = data_dir / name
                if not asset.is_file():
                    raise MigrationError("迁移图片缺失，请重新读取旧服数据。")
                size = asset.stat().st_size
                total += size
                if size > IMAGE_LIMIT or total > ARCHIVE_LIMIT:
                    raise MigrationError("迁移包的图片超过大小限制。")
                contents = asset.read_bytes()
                if not _valid_asset(name, contents):
                    raise MigrationError("迁移图片校验失败，请重新读取旧服数据。")
                archive.writestr(name, contents)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_manifest(data: object) -> dict:
    if not isinstance(data, dict) or data.get("format") != ARCHIVE_FORMAT or data.get("version") != 1:
        raise MigrationError("迁移包格式或版本不兼容。")
    groups = data.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise MigrationError("迁移包里没有旧群数据。")
    for group, payload in groups.items():
        _old_group(group)
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(field), dict) for field in ("rows", "profiles", "codes", "images")
        ):
            raise MigrationError("迁移包中的群组记录损坏。")
        if not isinstance(payload.get("captured_at"), int) or payload["captured_at"] < 0:
            raise MigrationError("迁移包中的导出时间无效。")
        if any(not _IMAGE_NAME.fullmatch(str(name)) for name in payload["images"].values()):
            raise MigrationError("迁移包的图片文件名无效。")
        for uid, profile in payload["profiles"].items():
            if not _OLD_UID.fullmatch(uid) or not isinstance(profile, dict):
                raise MigrationError("迁移包中的 QQ 资料格式无效。")
            payload["profiles"][uid] = {field: _name(profile.get(field)) for field in ("card", "nickname")}
        for uid, code in payload["codes"].items():
            if (
                not _OLD_UID.fullmatch(uid)
                or not isinstance(code, dict)
                or not _HASH.fullmatch(str(code.get("hash")))
                or not isinstance(code.get("expires_at"), int)
                or not isinstance(code.get("issued_at", 0), int)
            ):
                raise MigrationError("迁移包中的认领码格式无效。")
    return groups


def _valid_asset(name: str, contents: bytes) -> bool:
    extension = _image_extension(contents)
    return extension is not None and name == f"img/legacy-{_digest(contents)}.{extension}"


def _read_archive(path: Path, data_dir: Path) -> dict:
    try:
        if path.stat().st_size > ARCHIVE_LIMIT + MANIFEST_LIMIT:
            raise MigrationError("迁移包压缩文件超过大小限制。")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            files = {entry.filename: entry for entry in entries}
            if len(files) != len(entries) or "migration.json" not in files:
                raise MigrationError("迁移包缺少清单或有重复文件。")
            if sum(entry.file_size for entry in entries) > ARCHIVE_LIMIT:
                raise MigrationError("迁移包解压大小超过限制。")
            if files["migration.json"].file_size > MANIFEST_LIMIT:
                raise MigrationError("迁移包的清单超过大小限制。")
            groups = _validate_manifest(json.loads(archive.read("migration.json")))
            images = _archive_images(groups)
            if files.keys() != images | {"migration.json"}:
                raise MigrationError("迁移包图片和清单不一致。")
            for name in images:
                if files[name].file_size > IMAGE_LIMIT or not _valid_asset(name, archive.read(name)):
                    raise MigrationError("迁移包中的图片大小或内容校验失败。")
            for name in images:
                _install_image(data_dir, name, archive.read(name))
            return groups
    except FileNotFoundError as exc:
        raise MigrationError(f"未找到 {path.name}，请先把旧服的迁移包复制到这里。") from exc
    except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError, RecursionError, UnicodeError, ValueError) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError("无法读取迁移包，请检查文件是否完整。") from exc


def _stat(value: object) -> float:
    return value if isinstance(value, (int, float)) and 0 <= value < float("inf") else 0


def _config(values: dict) -> dict:
    ranges = {
        "draw_cooldown": (0, 600),
        "query_cooldown": (0, 600),
        "draw_hourly_limit": (1, 10),
        "harem_max_size": (1, 10000),
        "draw_scope": (1, 1000000),
        "ntr_chance": (0, 100),
        "claim_cooldown": (0, 86400),
    }
    result = {
        key: value for key, (low, high) in ranges.items()
        if type(value := values.get(key)) is int and low <= value <= high
    }
    window = values.get("draw_time_range")
    clock = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
    if isinstance(window, str) and re.fullmatch(f"{clock}-{clock}", window):
        result["draw_time_range"] = window
    return result


def _draw_status(value: object) -> list | None:
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str):
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{1,3}-[0-9]{1,2}", value[0]):
        return None
    return [value[0], int(_stat(value[1]))]


def _merge_draw_status(current: object, old: object) -> list | None:
    candidates = [status for value in (current, old) if (status := _draw_status(value))]
    return max(candidates, key=lambda status: (*map(int, status[0].split("-")), status[1]), default=None)


class Migration:
    def __init__(self, store: GameStore, data_dir: Path):
        self.store = store
        self.data_dir = Path(data_dir)

    @staticmethod
    def is_pending_uid(uid: str) -> bool:
        return str(uid).startswith("held-")

    @staticmethod
    def is_archive_image(path: str) -> bool:
        return bool(_IMAGE_NAME.fullmatch(path))

    async def pending_display(self, scope: str, uid: str) -> str:
        return await self.store.get(f"{scope}:{uid}:display_name", "待认领")

    async def capture_legacy(self, legacy_dir: Path | None = None) -> dict:
        """Read a consistent addon preference snapshot from this AstrBot host."""
        from astrbot.core import sp

        rows = await sp.range_get_async("plugin", LEGACY_PLUGIN_ID)
        return await self.capture_rows(
            {row.key: row.value["val"] for row in rows if "val" in row.value},
            legacy_dir,
        )

    async def capture_rows(self, values: dict, legacy_dir: Path | None = None) -> dict:
        groups = _split_groups(values)
        if not groups:
            raise MigrationError("本机没有找到原插件的群聊存档。")
        legacy_dir = Path(legacy_dir) if legacy_dir else self.data_dir.parent / "astrbot_plugin_mudae_qq"
        assets, missing = await asyncio.to_thread(_collect_images, self.data_dir, legacy_dir, groups)
        captured_at = int(time.time())
        payloads = {
            group: {
                "rows": rows,
                "profiles": {},
                "codes": {},
                "images": assets.get(group, {}),
                "captured_at": captured_at,
            }
            for group, rows in groups.items()
        }
        async with self.store.transaction():
            result = await self._stage(payloads)
            for group in groups:
                payload = await self.store.get(_source_key(group))
                accounts, _, _ = _index(payload["rows"])
                if accounts:
                    await self._prepare_codes(group, payload, accounts)
        return {**result, "missing_images": missing}

    async def collect_profiles(self, event: AstrMessageEvent, source_group: str | None = None) -> dict:
        """Record OneBot account nicknames and card names for existing players."""
        group = _old_group(source_group or event.get_group_id())
        if event.get_platform_name() != "aiocqhttp" or str(event.get_group_id()) != group or group == "global":
            raise MigrationError("请在旧服的 OneBot 原群中采集资料。")
        return await self._collect_group_profiles(event.bot.api, group)

    async def collect_all_profiles(self, event: AstrMessageEvent) -> dict:
        """Collect each staged group's profiles using the authenticated source bot."""
        if event.get_platform_name() != "aiocqhttp":
            raise MigrationError("请在旧服通过 OneBot 采集资料。")
        groups = await self.store.get(SOURCE_LIST_KEY, [])
        if not groups:
            raise MigrationError("请先读取本机的原插件存档。")
        result = {"groups": 0, "accounts": 0, "profiles": 0, "failed": 0}
        for group in (group for group in groups if group != "global"):
            try:
                collected = await self._collect_group_profiles(event.bot.api, group)
            except MigrationError:
                result["failed"] += 1
                continue
            if collected["accounts"]:
                result["groups"] += 1
                result["accounts"] += collected["accounts"]
                result["profiles"] += collected["profiles"]
        return result

    async def _collect_group_profiles(self, api, group: str) -> dict:
        payload = await self.store.get(_source_key(group))
        if not payload:
            raise MigrationError("请先读取本机的原插件存档。")
        accounts = _index(payload["rows"])[0]
        if not accounts:
            return {"source_group": group, "accounts": 0, "profiles": 0}
        try:
            async with asyncio.timeout(12):
                members = await api.call_action("get_group_member_list", group_id=int(group))
        except Exception as exc:
            raise MigrationError("OneBot 无法读取该群成员列表，请确认旧机器人仍在群里。") from exc
        if not isinstance(members, list):
            raise MigrationError("OneBot 未返回群成员列表。")
        by_uid = {
            str(member.get("user_id")): member
            for member in members
            if isinstance(member, dict) and str(member.get("user_id")) in accounts
        }
        limiter = asyncio.Semaphore(8)

        async def fetch(uid: str) -> tuple[str, dict]:
            member = by_uid.get(uid, {})
            profile = {field: _name(member.get(field)) for field in ("card", "nickname")}
            if not profile["nickname"]:
                try:
                    async with limiter:
                        async with asyncio.timeout(8):
                            info = await api.call_action("get_stranger_info", user_id=int(uid))
                except Exception:
                    info = None
                if isinstance(info, dict):
                    profile["nickname"] = _name(info.get("nickname"))
            return uid, profile

        profiles = dict(await asyncio.gather(*(fetch(uid) for uid in accounts)))
        successful = {uid: profile for uid, profile in profiles.items() if any(profile.values())}
        async with self.store.transaction():
            current = await self.store.get(_source_key(group))
            current["profiles"].update(successful)
            await self.store.put(_source_key(group), current)
        return {"source_group": group, "accounts": len(accounts), "profiles": len(successful)}

    async def issue_code(self, source_group: str, old_uid: str) -> str:
        group = _old_group(source_group)
        uid = str(old_uid)
        async with self.store.transaction():
            payload = await self.store.get(_source_key(group))
            if not payload or uid not in _index(payload["rows"])[0]:
                raise MigrationError("旧群存档里没有此 QQ 账号的游戏记录。")
            seed = await self.store.get(_seed_key(group))
            if not seed:
                raise MigrationError("请在读取过原始存档的旧服上获取认领码。")
            code = payload["codes"].get(uid, {})
            if code.get("expires_at", 0) < time.time():
                raise MigrationError("认领码已过期，请联系管理员重新读取旧服存档并更新迁移包。")
            issued_at = code.get("issued_at", 0)
            candidate = _claim_code(seed, group, uid, issued_at)
            if not secrets.compare_digest(code["hash"], _digest(candidate.encode())):
                raise MigrationError("认领码与本机存档不一致，请联系管理员更新迁移包。")
            return candidate

    async def _prepare_codes(self, group: str, payload: dict, accounts: dict) -> None:
        seed = await self.store.get(_seed_key(group))
        if not seed:
            seed = secrets.token_hex(32)
            await self.store.put(_seed_key(group), seed)
        now = time.time()
        issued_at = time.time_ns()
        for uid in accounts:
            code = payload["codes"].get(uid, {})
            candidate = _claim_code(seed, group, uid, code.get("issued_at", 0))
            if code.get("expires_at", 0) >= now and secrets.compare_digest(code["hash"], _digest(candidate.encode())):
                continue
            candidate = _claim_code(seed, group, uid, issued_at)
            payload["codes"][uid] = {
                "hash": _digest(candidate.encode()),
                "expires_at": int(now) + CODE_TTL,
                "issued_at": issued_at,
            }
        await self.store.put(_source_key(group), payload)

    async def export_archive(self, path: Path | None = None) -> dict:
        path = Path(path) if path else self.data_dir / ARCHIVE_FILENAME
        async with self.store.transaction():
            groups = {
                group: await self.store.get(_source_key(group))
                for group in await self.store.get(SOURCE_LIST_KEY, [])
            }
        if not groups:
            raise MigrationError("请先读取原插件存档。")
        await asyncio.to_thread(_write_archive, path, self.data_dir, groups)
        return {**_archive_summary(groups), "filename": path.name}

    async def import_archive(self, path: Path | None = None) -> dict:
        path = Path(path) if path else self.data_dir / ARCHIVE_FILENAME
        groups = await asyncio.to_thread(_read_archive, path, self.data_dir)
        return {**await self._stage(groups), "filename": path.name}

    async def _stage(self, incoming: dict) -> dict:
        frozen_groups = 0
        async with self.store.transaction():
            source_groups = set(await self.store.get(SOURCE_LIST_KEY, []))
            for group, data in incoming.items():
                existing = await self.store.get(_source_key(group))
                if existing:
                    if await self.store.get(_mapping_key(group)) and existing["rows"] != data["rows"]:
                        frozen_groups += 1
                        data = {**data, "rows": existing["rows"], "images": existing["images"]}
                    elif existing["captured_at"] > data["captured_at"]:
                        data = {**data, "rows": existing["rows"], "images": existing["images"]}
                    prior_codes = existing["codes"]
                    data["codes"] = {**prior_codes, **{
                        uid: code for uid, code in data["codes"].items()
                        if (code["expires_at"], code.get("issued_at", 0)) >= (
                            prior_codes.get(uid, {}).get("expires_at", 0),
                            prior_codes.get(uid, {}).get("issued_at", 0),
                        )
                    }}
                    data["profiles"] = {**existing["profiles"], **data["profiles"]}
                    data["captured_at"] = max(existing["captured_at"], data["captured_at"])
                await self.store.put(_source_key(group), data)
                source_groups.add(group)
            await self.store.put(SOURCE_LIST_KEY, sorted(source_groups))
        return {**_archive_summary(incoming), "frozen_groups": frozen_groups}

    async def status(self, scope: str | None = None) -> dict:
        async with self.store.transaction():
            rows = []
            for group in await self.store.get(SOURCE_LIST_KEY, []):
                data = await self.store.get(_source_key(group))
                target_scope = await self.store.get(_mapping_key(group))
                target = await self.store.get(_target_key(target_scope), {}) if target_scope else {}
                rows.append({
                    "source_group": group,
                    "accounts": len(_index(data["rows"])[0]),
                    "profiles": len(data["profiles"]),
                    "linked": len(target.get("links", {})),
                    "target_scope": target_scope,
                })
            current = await self.store.get(_target_key(scope), {}) if scope else {}
        return {"source_groups": rows, "source_group": current.get("source_group"), "target_scope": scope}

    async def pending(self, scope: str) -> list[dict]:
        _new_scope(scope)
        async with self.store.transaction():
            target = await self.store.get(_target_key(scope))
            if not target:
                return []
            data = await self.store.get(_source_key(target["source_group"]))
            return [
                {"old_uid": uid, **data["profiles"].get(uid, {})}
                for uid in target["held"]
                if uid not in target["links"]
            ]

    async def map_group(self, scope: str, source_group: str) -> dict:
        """Hold old game progress under internal player IDs in the new group."""
        scope = _new_scope(scope)
        group = _old_group(source_group)
        async with self.store.transaction():
            data = await self.store.get(_source_key(group))
            if not data:
                raise MigrationError("没有找到此旧群，请先导入迁移包。")
            target = await self.store.get(_target_key(scope))
            if target and target["source_group"] == group:
                return {"source_group": group, "target_scope": scope, "accounts": len(target["held"]), "conflicts": target["conflicts"]}
            if target or await self.store.get(_mapping_key(group)):
                raise MigrationError("旧群或目标群已经关联，请检查当前迁移状态。")
            accounts, owners, wishes = _index(data["rows"])
            held = {uid: f"held-{_digest(f'{scope}|{group}|{uid}'.encode())[:32]}" for uid in accounts}
            conflicts = await self._hold_game(scope, data, held, accounts, owners, wishes)
            target = {"source_group": group, "held": held, "links": {}, "conflicts": conflicts}
            await self.store.put(_target_key(scope), target)
            await self.store.put(_mapping_key(group), scope)
        return {"source_group": group, "target_scope": scope, "accounts": len(accounts), "conflicts": conflicts}

    async def _hold_game(
        self, scope: str, data: dict, held: dict[str, str], accounts: dict, owners: dict, wishes: dict,
    ) -> int:
        rows = data["rows"]
        partners: dict[str, list[str]] = {uid: [] for uid in accounts}
        wish_lists: dict[str, list[str]] = {uid: [] for uid in accounts}
        conflicts = 0
        for cid, uid in owners.items():
            key = f"{scope}:{cid}:married_to"
            if await self.store.get(key):
                conflicts += 1
                continue
            await self.store.put(key, held[uid])
            partners[uid].append(cid)
        for cid, uids in wishes.items():
            key = f"{scope}:{cid}:wished_by"
            active = _ids(await self.store.get(key, []), _UID)
            for uid in uids:
                active.append(held[uid])
                wish_lists[uid].append(cid)
            await self.store.put(key, list(dict.fromkeys(active)))

        user_key = f"{scope}:user_list"
        await self.store.put(user_key, list(dict.fromkeys([*_ids(await self.store.get(user_key, []), _UID), *held.values()])))
        heats_key = f"{scope}_harem_heats"
        heats = await self.store.get(heats_key, {})
        old_heats = rows.get("_harem_heats", {})
        for uid, old_data in accounts.items():
            prefix = f"{scope}:{held[uid]}"
            profile = data["profiles"].get(uid, {})
            label = profile.get("card") or profile.get("nickname") or "待认领"
            old_partners = [cid for cid in _ids(old_data.get("partners")) if cid in partners[uid]]
            old_wishes = [cid for cid in _ids(old_data.get("wish_list")) if cid in wish_lists[uid]]
            await self.store.put(f"{prefix}:display_name", label)
            await self.store.put(f"{prefix}:partners", list(dict.fromkeys([*old_partners, *partners[uid]])))
            await self.store.put(f"{prefix}:wish_list", list(dict.fromkeys([*old_wishes, *wish_lists[uid]])))
            if str(old_data.get("fav")) in partners[uid]:
                await self.store.put(f"{prefix}:fav", str(old_data["fav"]))
            if draw := _draw_status(old_data.get("draw_status")):
                await self.store.put(f"{prefix}:draw_status", draw)
            if claim := _stat(old_data.get("last_claim")):
                await self.store.put(f"{prefix}:last_claim", claim)
            if isinstance(old_heats, dict) and uid in old_heats:
                heats[held[uid]] = _stat(old_heats[uid])
        if heats:
            await self.store.put(heats_key, heats)

        if isinstance(old_config := rows.get("config"), dict):
            config = await self.store.get(f"{scope}:config", {})
            await self.store.put(f"{scope}:config", {**_config(old_config), **config})
        for field in ("last_draw", "last_query"):
            if previous := _stat(rows.get(field)):
                key = f"{scope}:{field}"
                await self.store.put(key, max(previous, _stat(await self.store.get(key))))
        for key, paths in rows.items():
            cid, _, field = key.partition(":")
            if field != "custom_images" or not _OLD_UID.fullmatch(cid) or not isinstance(paths, list):
                continue
            images = [data["images"][path] for path in paths if isinstance(path, str) and path in data["images"]]
            if images:
                target_key = f"{scope}:{cid}:custom_images"
                current = await self.store.get(target_key, [])
                await self.store.put(target_key, list(dict.fromkeys([*current, *images])))
        return conflicts

    async def try_link(
        self,
        scope: str,
        uid: str,
        sender_name: str | None = None,
        *,
        allow_name: bool = False,
        verified_qq: bool = False,
    ) -> LinkResult:
        async with self.store.transaction():
            target = await self.store.get(_target_key(scope))
            if not target:
                return LinkResult("not_mapped", "")
            if uid in target["links"].values():
                return LinkResult("already_linked", "")
            if verified_qq and uid in target["held"]:
                return await self._link(scope, uid, uid, target)
            name = _match_name(sender_name)
            if not allow_name or not name or name == _match_name(uid):
                return LinkResult("no_name", "暂未自动找回旧存档。请使用一次性认领码，或联系管理员核对账号。")
            data = await self.store.get(_source_key(target["source_group"]))
            matches = [
                old_uid for old_uid, profile in data["profiles"].items()
                if old_uid in target["held"] and name in {_match_name(profile.get(field)) for field in ("nickname", "card")}
            ]
            if not matches:
                return LinkResult("no_match", "没有找到匹配的旧存档，抽卡可以继续。可用认领码或联系管理员找回。")
            if len(matches) != 1:
                return LinkResult("ambiguous", "发现重名的旧玩家。请使用认领码，或联系管理员核对。")
            return await self._link(scope, uid, matches[0], target)

    async def redeem(self, scope: str, uid: str, code: str) -> LinkResult:
        async with self.store.transaction():
            target = await self.store.get(_target_key(scope))
            if not target:
                return LinkResult("not_mapped", "本群尚未设置旧群迁移，请联系管理员。")
            if uid in target["links"].values():
                return LinkResult("already_linked", "此账号已找回本群的旧存档。")
            data = await self.store.get(_source_key(target["source_group"]))
            if re.fullmatch(r"[A-Za-z0-9_-]{32}", code):
                digest = _digest(code.encode())
                for old_uid, entry in data["codes"].items():
                    if entry["expires_at"] >= time.time() and secrets.compare_digest(entry["hash"], digest):
                        return await self._link(scope, uid, old_uid, target)
            return LinkResult("invalid_code", "认领码无效或已过期，请确认所在群和导入的迁移包。")

    async def manual_link(self, scope: str, uid: str, old_uid: str) -> LinkResult:
        _new_scope(scope)
        async with self.store.transaction():
            target = await self.store.get(_target_key(scope))
            if not target:
                return LinkResult("not_mapped", "本群尚未设置旧群迁移。")
            return await self._link(scope, uid, old_uid, target)

    async def _link(self, scope: str, uid: str, old_uid: str, target: dict) -> LinkResult:
        if not _UID.fullmatch(uid) or self.is_pending_uid(uid):
            return LinkResult("conflict", "当前玩家标识无效。")
        held = target["held"].get(old_uid)
        if not held:
            return LinkResult("no_match", "此群未找到对应旧玩家。")
        prior = target["links"].get(old_uid)
        if prior == uid:
            return LinkResult("already_linked", "此账号已找回本群的旧存档。")
        if prior:
            return LinkResult("claimed", "此旧存档已被认领，请联系管理员核对。")
        if uid in target["links"].values():
            return LinkResult("conflict", "当前玩家已关联另一份旧存档。")

        player = f"{scope}:{uid}"
        old = f"{scope}:{held}"
        if not await self.store.get(f"{player}:display_name"):
            data = await self.store.get(_source_key(target["source_group"]))
            profile = data["profiles"].get(old_uid, {})
            if label := profile.get("card") or profile.get("nickname"):
                await self.store.put(f"{player}:display_name", label)
        partners = _ids(await self.store.get(f"{player}:partners", []))
        restored = []
        for cid in _ids(await self.store.get(f"{old}:partners", [])):
            key = f"{scope}:{cid}:married_to"
            if await self.store.get(key) == held:
                await self.store.put(key, uid)
                restored.append(cid)
        partners = list(dict.fromkeys([*partners, *restored]))
        await self.store.put(f"{player}:partners", partners)
        favorite = await self.store.get(f"{player}:fav")
        old_fav = str(await self.store.get(f"{old}:fav"))
        if str(favorite) not in partners and old_fav in partners:
            await self.store.put(f"{player}:fav", old_fav)
        wishes = _ids(await self.store.get(f"{old}:wish_list", []))
        for cid in wishes:
            key = f"{scope}:{cid}:wished_by"
            wishers = _ids(await self.store.get(key, []), _UID)
            await self.store.put(key, list(dict.fromkeys(uid if wisher == held else wisher for wisher in wishers)))
        active_wishes = _ids(await self.store.get(f"{player}:wish_list", []))
        await self.store.put(f"{player}:wish_list", list(dict.fromkeys([*active_wishes, *wishes])))

        draw_status = _merge_draw_status(
            await self.store.get(f"{player}:draw_status"), await self.store.get(f"{old}:draw_status"),
        )
        if draw_status:
            await self.store.put(f"{player}:draw_status", draw_status)
        last_claim = max(_stat(await self.store.get(f"{player}:last_claim")), _stat(await self.store.get(f"{old}:last_claim")))
        if last_claim:
            await self.store.put(f"{player}:last_claim", last_claim)
        heats_key = f"{scope}_harem_heats"
        heats = await self.store.get(heats_key, {})
        if held in heats:
            old_heat = heats.pop(held)
            heats.setdefault(uid, old_heat)
            await self.store.put(heats_key, heats)
        user_key = f"{scope}:user_list"
        users = _ids(await self.store.get(user_key, []), _UID)
        await self.store.put(user_key, list(dict.fromkeys([*(user for user in users if user != held), uid])))
        for field in _ACCOUNT_FIELDS | {"display_name"}:
            await self.store.delete(f"{old}:{field}")

        target["links"][old_uid] = uid
        await self.store.put(_target_key(scope), target)
        return LinkResult("linked", f"旧存档已找回。已恢复 {len(restored)} 个角色、{len(wishes)} 条许愿。")
