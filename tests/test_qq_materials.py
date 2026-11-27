import asyncio
import hashlib
import importlib
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


MATERIALS = "/v2/media-images"
APP_ID = "102083127"
OTHER_APP_ID = "102083128"
CATALOG_URL = "https://lain.bgm.tv/test.png"
MATERIAL_URL = "qqbot-media://image/v1/new-material"


@pytest.fixture
def qq(monkeypatch):
    pytest.importorskip("astrbot.api")
    plugin_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(plugin_dir.parent))
    module = importlib.import_module(f"{plugin_dir.name}.qq_official")
    monkeypatch.setattr(module, "_image_refs", OrderedDict())
    monkeypatch.setattr(module, "_image_quota_retry", {})
    monkeypatch.setattr(module, "_image_locks", {})
    return module


def target(qq, app_id):
    http = SimpleNamespace(_token=SimpleNamespace(app_id=app_id), is_sandbox=False)
    return qq._Target(http, "/v2/groups/test", "test-inbound", True)


def material(app_id, sequence, created_at, *, filename=None):
    image_id = f"{app_id}_{sequence:016x}"
    return {
        "image_id": image_id,
        "file_name": filename if filename is not None else f"anime-draw-{sequence:024x}.png",
        "url": f"qqbot-media://image/v1/{image_id}",
        "created_at": created_at,
    }


class FakeQQStorage:
    def __init__(self, qq, monkeypatch, *, lists, quota_stage=None):
        self.qq = qq
        self.lists = iter(lists)
        self.quota_stage = quota_stage
        self.calls = []
        self.deleted = []
        self.uploads = []
        monkeypatch.setattr(qq, "_request", self.request)
        monkeypatch.setattr(qq.aiohttp, "ClientSession", self.session)

    async def request(self, target, endpoint, payload=None, *, method="POST"):
        parsed = urlsplit(endpoint)
        path = parsed.path
        self.calls.append((method, path))
        if method == "GET" and path == MATERIALS:
            cursor = parse_qs(parsed.query).get("cursor", [""])[0]
            expected_cursor, page = next(self.lists)
            assert cursor == expected_cursor
            return self.qq._Response(200, page)
        if method == "DELETE" and path.endswith("/delete"):
            self.deleted.append(path.removeprefix(f"{MATERIALS}/").removesuffix("/delete"))
            return self.qq._Response(200, {})
        if method == "POST" and path == self.quota_stage:
            self.quota_stage = None
            return self.qq._Response(400, {"code": "40035007"}, 40035007)
        if method == "POST" and path == f"{MATERIALS}/prepare":
            assert payload["file_name"].startswith("anime-draw-")
            return self.qq._Response(200, {
                "upload_id": "test-upload",
                "presigned_url": "https://test.cos.ap-guangzhou.myqcloud.com/upload?signature=test",
            })
        if method == "POST" and path == f"{MATERIALS}/complete":
            return self.qq._Response(200, {
                "image_id": f"{target.http._token.app_id}_fedcba9876543210",
                "url": MATERIAL_URL,
                "ttl": 600,
            })
        raise AssertionError(f"Unexpected QQ request {method} {endpoint}")

    @asynccontextmanager
    async def session(self, **kwargs):
        yield self

    @asynccontextmanager
    async def put(self, url, *, data, headers, allow_redirects):
        assert url.startswith("https://test.cos.")
        assert headers == {"Content-Type": "image/png"}
        assert not allow_redirects
        self.uploads.append(data)
        etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
        yield SimpleNamespace(status=200, headers={"ETag": etag})


def test_prunes_only_oldest_owned_material_across_pages(qq, monkeypatch):
    owned = [material(APP_ID, i, str(999_999_995 + i)) for i in range(1, 8)]
    recent = material(APP_ID, 8, str(int(time.time())))
    other = material(APP_ID, 100, "900000001", filename="mywife-0123456789abcdef01234567.png")
    similar = material(APP_ID, 101, "900000002", filename="anime-draw-fake.png")
    unknown_time = material(APP_ID, 102, "unknown")
    pages = [
        ("", {"total": "95", "next_cursor": "opaque/+position=", "images": [recent, *reversed(owned[3:])]}),
        ("opaque/+position=", {"total": "95", "next_cursor": "", "images": [
            other, similar, unknown_time, *reversed(owned[:3]),
        ]}),
    ]
    storage = FakeQQStorage(qq, monkeypatch, lists=pages)
    expires = time.monotonic() + 60
    oldest_key = (APP_ID, False, f"{1:024x}" + "0" * 16)
    retained_key = (APP_ID, False, f"{8:024x}" + "0" * 16)
    foreign_key = (OTHER_APP_ID, False, oldest_key[-1])
    qq._image_refs.update({
        oldest_key: (expires, owned[0]["url"]),
        retained_key: (expires, recent["url"]),
        foreign_key: (expires, owned[0]["url"]),
    })

    picture = qq.PictureFile(b"new-png", "png", "image/png")
    assert asyncio.run(qq._markdown_image(target(qq, APP_ID), picture)) == MATERIAL_URL

    assert storage.deleted == [row["image_id"] for row in owned]
    assert oldest_key not in qq._image_refs
    assert retained_key in qq._image_refs
    assert foreign_key in qq._image_refs
    assert storage.calls[:2] == [("GET", MATERIALS), ("GET", MATERIALS)]
    assert storage.calls[-2:] == [("POST", f"{MATERIALS}/prepare"), ("POST", f"{MATERIALS}/complete")]


@pytest.mark.parametrize("quota_stage", [f"{MATERIALS}/prepare", f"{MATERIALS}/complete"])
def test_full_storage_reclaims_and_retries_the_upload(qq, monkeypatch, quota_stage):
    app_id = {f"{MATERIALS}/prepare": "102083129", f"{MATERIALS}/complete": "102083130"}[quota_stage]
    owned = [material(app_id, i, 1_000_000_000 + i) for i in range(1, 11)]
    storage = FakeQQStorage(qq, monkeypatch, lists=[
        ("", {"total": "94", "next_cursor": "", "images": []}),
        ("", {"total": "100", "next_cursor": "", "images": list(reversed(owned))}),
    ], quota_stage=quota_stage)
    picture = qq.PictureFile(b"new-png", "png", "image/png")

    assert asyncio.run(qq._markdown_image(target(qq, app_id), picture)) == MATERIAL_URL
    assert storage.calls.count(("POST", quota_stage)) == 2
    assert storage.deleted == [row["image_id"] for row in owned]


def test_incomplete_listing_does_not_trigger_deletion(qq, monkeypatch):
    owned = material(APP_ID, 1, "999999996")
    storage = FakeQQStorage(qq, monkeypatch, lists=[
        ("", {"total": "95", "next_cursor": "repeat", "images": [owned]}),
        ("repeat", {"total": "95", "next_cursor": "repeat", "images": []}),
    ])
    picture = qq.PictureFile(b"new-png", "png", "image/png")

    assert asyncio.run(qq._markdown_image(target(qq, APP_ID), picture)) == MATERIAL_URL
    assert storage.deleted == []


def test_default_catalog_url_does_not_access_qq_storage(qq, monkeypatch):
    app_target = target(qq, OTHER_APP_ID)
    event = SimpleNamespace(get_platform_name=lambda: "qq_official", get_sender_id=lambda: "viewer")
    image = qq.PictureFile(b"new-png", "png", "image/png")
    picture = qq.Picture(image, image, 240, 180, CATALOG_URL)
    messages = []

    async def send(target, endpoint, payload=None, *, method="POST"):
        assert endpoint == "messages"
        messages.append(payload)
        return qq._Response(200, {"id": "test-outbound"})

    monkeypatch.setattr(qq, "_reply_target", lambda _: app_target)
    monkeypatch.setattr(qq, "_request", send)
    result = asyncio.run(qq.send_qq_reply(event, "测试卡片", image=picture, actions=[("抽卡", "抽卡")]))

    assert result.delivered
    payload, = messages
    assert payload["msg_type"] == 2
    assert f"![角色图片 #240px #180px]({CATALOG_URL})" in payload["markdown"]["content"]
    assert "keyboard" in payload
