import asyncio
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from migration import Migration, MigrationError
from storage import GameStore

OLD_GROUP = "7001"
SCOPE = "qq-official|group-openid"


class OneBotAPI:
    async def call_action(self, name, **kwargs):
        if name == "get_group_member_list":
            assert kwargs == {"group_id": int(OLD_GROUP)}
            return [
                {"user_id": 10001, "nickname": "北极星", "card": "同名"},
                {"user_id": 10002, "nickname": "月亮", "card": "同名"},
                {"user_id": 10003, "nickname": "旁观者", "card": "路人"},
            ]
        assert name == "get_stranger_info"
        assert kwargs == {"user_id": 10004}
        return {"nickname": "远行者"}


async def connect(path: Path) -> GameStore:
    path.mkdir()
    store = GameStore(path / "game.sqlite")
    await store.initialize()
    return store


def test_archive_and_claim_merge_without_losing_active_progress(tmp_path):
    async def scenario():
        source_dir = tmp_path / "source"
        dest_dir = tmp_path / "dest"
        legacy_dir = tmp_path / "legacy"
        (legacy_dir / "img").mkdir(parents=True)
        (legacy_dir / "img" / "legacy.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")
        source = await connect(source_dir)
        dest = await connect(dest_dir)
        try:
            old = Migration(source, source_dir)
            new = Migration(dest, dest_dir)
            rows = {
                "7001:user_list": [10001, 10002, 10003],
                "7001:10001:partners": [22, "21"],
                "7001:10001:draw_status": ["2099-1-1", 5],
                "7001:10001:fav": 22,
                "7001:21:married_to": "10001",
                "7001:22:married_to": "10001",
                "7001:10002:wish_list": ["24"],
                "7001:23:wished_by": ["10001", "10004"],
                "7001:22:custom_images": ["img/legacy.png", "img/missing.png"],
                "7001:config": {"draw_hourly_limit": 8, "draw_time_range": "99:00-10:00"},
                "7001_harem_heats": {"10001": 200},
            }
            captured = await old.capture_rows(rows, legacy_dir)
            assert (captured["accounts"], captured["images"], captured["missing_images"]) == (3, 1, 1)

            event = SimpleNamespace(
                get_group_id=lambda: OLD_GROUP,
                get_platform_name=lambda: "aiocqhttp",
                bot=SimpleNamespace(api=OneBotAPI()),
            )
            assert (await old.collect_profiles(event))["profiles"] == 3
            assert await old.collect_all_profiles(event) == {
                "groups": 1, "accounts": 3, "profiles": 3, "failed": 0,
            }
            with pytest.raises(MigrationError):
                await old.issue_code(OLD_GROUP, "10003")
            archive = source_dir / "migration.zip"
            await old.export_archive()
            code = await old.issue_code(OLD_GROUP, "10001")
            with ZipFile(archive) as bundle:
                assert code.encode() not in bundle.read("migration.json")
                seed = await source.get(f"migration:seed:{OLD_GROUP}")
                assert seed.encode() not in bundle.read("migration.json")

            await new.import_archive(archive)
            with pytest.raises(MigrationError):
                await new.issue_code(OLD_GROUP, "10001")
            await dest.put(f"{SCOPE}:21:married_to", "active-other")
            await dest.put(f"{SCOPE}:25:married_to", "openid1")
            await dest.put(f"{SCOPE}:openid1:partners", ["25"])
            await dest.put(f"{SCOPE}:openid1:fav", "25")
            await dest.put(f"{SCOPE}:config", {"draw_hourly_limit": 6})

            first = await new.map_group(SCOPE, OLD_GROUP)
            assert first["accounts"] == 3
            assert first["conflicts"] == 1
            assert await new.map_group(SCOPE, OLD_GROUP) == first
            with pytest.raises(MigrationError):
                await new.map_group("qq-official|other-group", OLD_GROUP)
            assert await dest.get(f"{SCOPE}:config") == {"draw_hourly_limit": 6}

            holder = await dest.get(f"{SCOPE}:22:married_to")
            assert new.is_pending_uid(holder)
            assert await new.pending_display(SCOPE, holder) == "同名"
            assert (await new.try_link(SCOPE, "openid1", "同名", allow_name=True)).status == "ambiguous"
            assert (await new.try_link(SCOPE, "openid1", "openid1", allow_name=True)).status == "no_name"
            assert (await new.redeem(SCOPE, "openid1", code)).status == "linked"
            assert (await new.redeem(SCOPE, "openid2", code)).status == "claimed"
            assert (await new.redeem(SCOPE, "openid1", code)).status == "already_linked"
            assert (await new.manual_link(SCOPE, "openid1", "10002")).status == "conflict"

            assert await dest.get(f"{SCOPE}:openid1:partners") == ["25", "22"]
            assert await dest.get(f"{SCOPE}:openid1:display_name") == "同名"
            assert await dest.get(f"{SCOPE}:openid1:draw_status") == ["2099-1-1", 5]
            assert await dest.get(f"{SCOPE}:openid1:fav") == "25"
            assert await dest.get(f"{SCOPE}:22:married_to") == "openid1"
            assert await dest.get(f"{SCOPE}:21:married_to") == "active-other"
            assert (await dest.get(f"{SCOPE}:23:wished_by"))[0] == "openid1"
            assert await dest.get(f"{SCOPE}_harem_heats") == {"openid1": 200}
            assert await dest.get(f"{SCOPE}:{holder}:partners") is None
            assert holder not in await dest.get(f"{SCOPE}:user_list")
            pictures = await dest.get(f"{SCOPE}:22:custom_images")
            assert len(pictures) == 1
            assert new.is_archive_image(pictures[0])
            assert (dest_dir / pictures[0]).is_file()

            await new.import_archive(archive)
            assert (await new.try_link(SCOPE, "openid1")).status == "already_linked"
            assert (await new.try_link(SCOPE, "10002", verified_qq=True)).status == "linked"
            assert (await new.try_link(SCOPE, "openid4", "远行者", allow_name=True)).status == "linked"
            assert await new.pending(SCOPE) == []
            assert (await new.status(SCOPE))["source_groups"][0]["linked"] == 3
        finally:
            await source.close()
            await dest.close()

    asyncio.run(scenario())


def test_rejects_tampered_archive_before_staging(tmp_path):
    async def scenario():
        database = await connect(tmp_path / "state")
        try:
            migration = Migration(database, tmp_path / "state")
            archive = tmp_path / "bad.zip"
            with ZipFile(archive, "w") as bundle:
                bundle.writestr("migration.json", "{}")
                bundle.writestr("../outside.txt", "bad")
            with pytest.raises(MigrationError):
                await migration.import_archive(archive)
            assert (await migration.status())["source_groups"] == []
            assert not (tmp_path / "outside.txt").exists()
        finally:
            await database.close()

    asyncio.run(scenario())
