"""Native QQ Markdown and buttons using AstrBot's official adapter session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from PIL import Image as PILImage

from .qq_picture import Picture, PictureFile, prepare_picture

OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
MARKDOWN_SYNTAX = re.compile(r"([\\`*_{}\[\]()#+.!|<>~\-])")
MEDIA_URL = re.compile(r"""qqbot-media://image/v1/[^\s()<>\[\]\\"']{1,1500}""")
COS_HOST = re.compile(r"[a-z0-9-]+\.cos\.[a-z0-9-]+\.myqcloud\.com")
CATALOG_LINK_FIRST = "卡池外链优先"
QQ_STORAGE_FIRST = "QQ 官方素材优先"
MATERIALS_PATH = "/v2/media-images"
OWN_MATERIAL = re.compile(r"anime-draw-([0-9a-f]{24})\.(?:jpg|png)")
IMAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
IMAGE_CLEANUP_AT = 95
IMAGE_CLEANUP_GRACE = 60
IMAGE_QUOTA_EXCEEDED = 40035007
IMAGE_ALREADY_GONE = 40035008
_image_refs: OrderedDict[tuple[str, bool, str], tuple[float, str]] = OrderedDict()
_image_quota_retry: dict[tuple[str, bool], float] = {}
_image_locks: dict[tuple[str, bool], asyncio.Lock] = {}


@dataclass(frozen=True)
class QQReply:
    """A handled result includes ambiguous delivery, which must not be repeated."""

    handled: bool = False
    message_id: str | None = None
    delivered: bool = False


@dataclass(frozen=True)
class _Target:
    http: Any
    endpoint: str
    source_id: str
    uses_v2: bool


@dataclass(frozen=True)
class _Response:
    status: int
    document: dict[str, Any] | None
    code: int = 0

    @property
    def accepted(self) -> bool:
        return 200 <= self.status < 300 and self.code == 0

    @property
    def rejected(self) -> bool:
        return 400 <= self.status < 500 or (200 <= self.status < 300 and self.code != 0)


def escape_markdown(text: str) -> str:
    """Escape display names and other untrusted text before inserting them."""
    return MARKDOWN_SYNTAX.sub(r"\\\1", text)


def _reply_target(event: AstrMessageEvent) -> _Target | None:
    if event.get_platform_name() not in OFFICIAL_PLATFORMS:
        return None
    try:
        from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message

        source = event.message_obj.raw_message
        http = event.bot.api._http
        match source:
            case GroupMessage():
                path, recipient = "/v2/groups", source.group_openid
            case C2CMessage():
                path, recipient = "/v2/users", source.author.user_openid
            case DirectMessage():
                path, recipient = "/dms", source.guild_id
            case Message():
                path, recipient = "/channels", source.channel_id
            case _:
                return None
        message_id = str(event.message_obj.message_id or source.id or "")
        if not recipient or not message_id or not callable(getattr(http, "check_session", None)):
            return None
        return _Target(
            http, f"{path}/{quote(str(recipient), safe='')}", message_id, path.startswith("/v2/")
        )
    except (AttributeError, ImportError, TypeError):
        return None


def _keyboard(actions: Sequence[tuple[str, str]], user: str) -> dict[str, Any]:
    permission = {"type": 0, "specify_user_ids": [user]} if user else {"type": 2}
    buttons = [
        {
            "id": f"anime-draw-{index}",
            "render_data": {"label": label, "visited_label": label, "style": int(index == 1)},
            "action": {
                "type": 2,
                "permission": permission,
                "data": command,
                "enter": True,
                "unsupport_tips": f"请在当前聊天输入 {command}",
            },
        }
        for index, (label, command) in enumerate(actions[:5], start=1)
    ]
    rows = [{"buttons": buttons[:3]}]
    if len(buttons) > 3:
        rows.append({"buttons": buttons[3:]})
    return {"content": {"rows": rows}}


async def _request(
    target: _Target, endpoint: str, payload: dict[str, Any] | None = None, *, method: str = "POST",
) -> _Response | None:
    """Use the adapter's authentication without botpy's implicit retries."""
    from botpy.http import Route

    http = target.http
    log_failure = logger.debug if endpoint.startswith(MATERIALS_PATH) else logger.warning
    try:
        await http.check_session()
        path = endpoint if endpoint.startswith("/") else f"{target.endpoint}/{endpoint}"
        route = Route(method, path, is_sandbox=http.is_sandbox)
        session = http._session
        headers = dict(http._headers)
    except (
        AttributeError,
        TypeError,
        OSError,
        TimeoutError,
        aiohttp.ClientError,
        RuntimeError,
    ) as exc:
        log_failure("[二次元抽抽乐] QQ 会话不可用（%s）", type(exc).__name__)
        return _Response(0, None)

    try:
        async with session.request(
            method,
            route.url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=False,
        ) as response:
            try:
                document = await response.json(content_type=None)
            except (ValueError, TypeError, aiohttp.ClientPayloadError):
                document = None
            document = document if isinstance(document, dict) else None
            raw_code = document.get("code", 0) if document is not None else 0
            code = _material_number(raw_code) or 0
            return _Response(response.status, document, code)
    except aiohttp.ClientConnectorError:
        return _Response(0, None)
    except (OSError, TimeoutError, aiohttp.ClientError, RuntimeError) as exc:
        log_failure("[二次元抽抽乐] QQ 请求结果未确认（%s）", type(exc).__name__)
        return None


async def _send(target: _Target, payload: dict[str, Any]) -> _Response | None:
    payload = {"msg_id": target.source_id, **payload}
    if target.uses_v2:
        # QQ deduplicates replies by inbound message id and this sequence.
        payload["msg_seq"] = random.randint(1, 10000)
    else:
        payload.pop("msg_type", None)
    return await _request(target, "messages", payload)


def _accepted_reply(event: AstrMessageEvent, response: _Response | None) -> QQReply:
    if response is not None and response.status == 0:
        return QQReply()
    if response is None or not response.rejected:
        # The native send bypasses the event's regular transport.
        event._has_send_oper = True
        document = (response.document or {}) if response is not None else {}
        message_id = document.get("id") or document.get("message_id")
        return QQReply(
            handled=True,
            message_id=str(message_id) if message_id else None,
            delivered=response is not None and response.accepted,
        )
    return QQReply()


def _material_number(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    return None


async def _cleanup_markdown_images(
    target: _Target, app: tuple[str, bool], *, force: bool = False,
) -> int:
    """Reclaim old images with this plugin's exact filename pattern."""
    candidates: dict[str, tuple[int, str]] = {}
    cutoff = int(time.time()) - IMAGE_CLEANUP_GRACE
    removed = 0
    cursor = ""
    seen_cursors = set()
    try:
        async with asyncio.timeout(5):
            for page in range(20):
                path = f"{MATERIALS_PATH}?cursor={quote(cursor, safe='')}" if cursor else MATERIALS_PATH
                response = await _request(target, path, method="GET")
                if response is None or not response.accepted or not response.document:
                    return 0
                document = response.document
                total = _material_number(document.get("total"))
                if page == 0 and (total is None or (total < IMAGE_CLEANUP_AT and not force)):
                    return 0
                items = document.get("images")
                if not isinstance(items, list):
                    return 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    image_id = item.get("image_id")
                    name = item.get("file_name")
                    created = _material_number(item.get("created_at"))
                    if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
                        continue
                    match = OWN_MATERIAL.fullmatch(name) if isinstance(name, str) else None
                    if match and created is not None and 0 < created <= cutoff:
                        candidates[image_id] = (created, match[1])
                cursor = document.get("next_cursor", "")
                if cursor == "":
                    break
                if not isinstance(cursor, str) or len(cursor) > 1024 or cursor in seen_cursors:
                    return 0
                seen_cursors.add(cursor)
            else:
                return 0

            async def delete(image_id: str, sha_prefix: str) -> bool:
                nonlocal removed
                # A timed-out DELETE may still take effect on QQ's side.
                for key in tuple(_image_refs):
                    if key[:2] == app and key[2].startswith(sha_prefix):
                        _image_refs.pop(key, None)
                result = await _request(target, f"{MATERIALS_PATH}/{image_id}/delete", method="DELETE")
                if result is None or not (result.accepted or result.code == IMAGE_ALREADY_GONE):
                    return False
                removed += 1
                return True

            ordered = sorted(candidates.items(), key=lambda item: item[1][0])
            for start in range(0, len(ordered), 4):
                results = await asyncio.gather(*(
                    delete(image_id, prefix) for image_id, (_, prefix) in ordered[start:start + 4]
                ))
                if not all(results):
                    break
    except TimeoutError:
        logger.debug("[二次元抽抽乐] QQ Markdown 素材检查超时。")
    if removed:
        logger.debug("[二次元抽抽乐] 已清理本插件 %s 张 QQ Markdown 素材。", removed)
    return removed


async def _markdown_image(target: _Target, picture: PictureFile) -> str | None:
    """Reuse a valid reference, or upload after checking the bot's quota."""
    app_id = str(getattr(getattr(target.http, "_token", None), "app_id", "") or "")
    app = (app_id, bool(getattr(target.http, "is_sandbox", False)))
    sha1 = hashlib.sha1(picture.data, usedforsecurity=False).hexdigest()
    if not app_id:
        return _stored_reference(await _store_markdown_image(target, picture, sha1))

    async with _image_locks.setdefault(app, asyncio.Lock()):
        return await _get_markdown_reference(target, picture, app, sha1)


def _stored_reference(response: _Response | None) -> str | None:
    if response is None or not response.accepted or not response.document:
        return None
    reference = response.document.get("url")
    return reference if isinstance(reference, str) and MEDIA_URL.fullmatch(reference) else None


async def _get_markdown_reference(
    target: _Target, picture: PictureFile, app: tuple[str, bool], sha1: str,
) -> str | None:
    key = (*app, sha1)
    now = time.monotonic()
    cached = _image_refs.get(key)
    if cached and cached[0] > now:
        _image_refs.move_to_end(key)
        return cached[1]
    if _image_quota_retry.get(app, 0) > now:
        return None
    await _cleanup_markdown_images(target, app)
    completed = await _store_markdown_image(target, picture, sha1)
    if completed is not None and completed.code == IMAGE_QUOTA_EXCEEDED:
        if await _cleanup_markdown_images(target, app, force=True):
            completed = await _store_markdown_image(target, picture, sha1)
        if completed is not None and completed.code == IMAGE_QUOTA_EXCEEDED:
            _image_quota_retry[app] = time.monotonic() + 300
            logger.debug("[二次元抽抽乐] QQ Markdown 素材额度已满，5 分钟后重试新素材上传。")

    reference = _stored_reference(completed)
    if reference and completed and completed.document:
        ttl = _material_number(completed.document.get("ttl"))
        # Refresh before the service expiry; use a short lifetime if absent.
        seconds = min(ttl - 60, 86400) if ttl is not None else 600
        if seconds > 0:
            _image_refs[key] = (time.monotonic() + seconds, reference)
            _image_refs.move_to_end(key)
            if len(_image_refs) > 512:
                _image_refs.popitem(last=False)
    return reference


async def _store_markdown_image(target: _Target, picture: PictureFile, sha1: str) -> _Response | None:
    md5 = hashlib.md5(picture.data, usedforsecurity=False).hexdigest()
    response = await _request(target, f"{MATERIALS_PATH}/prepare", {
        "file_name": f"anime-draw-{sha1[:24]}.{picture.extension}",
        "file_size": len(picture.data),
        "content_type": picture.content_type,
        "md5": md5,
        "sha1": sha1,
    })
    if response is None or not response.accepted or not response.document:
        return response
    upload_id = response.document.get("upload_id")
    url = response.document.get("presigned_url")
    if not isinstance(upload_id, str) or not 0 < len(upload_id) <= 1024 or not isinstance(url, str) or len(url) > 10000:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not COS_HOST.fullmatch(parsed.hostname or "")
            or parsed.port not in {None, 443} or parsed.username or parsed.password
            or parsed.fragment or not parsed.query
        ):
            return None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.put(
                url, data=picture.data, headers={"Content-Type": picture.content_type}, allow_redirects=False,
            ) as uploaded:
                etag = uploaded.headers.get("ETag", "").strip().strip('"').lower()
                if not 200 <= uploaded.status < 300 or etag != md5:
                    return None
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
        return None
    return await _request(target, f"{MATERIALS_PATH}/complete", {"upload_id": upload_id, "md5": md5})


async def _upload_image(target: _Target, picture: Picture) -> str | None:
    response = await _request(target, "files", {
        "file_type": 1, "srv_send_msg": False,
        "file_data": base64.b64encode(picture.preview.data).decode("ascii"),
    })
    if response is not None and response.accepted and response.document:
        file_info = response.document.get("file_info")
        if isinstance(file_info, str) and file_info:
            return file_info
    if response is not None:
        logger.warning(
            "[二次元抽抽乐] QQ 图片上传失败（状态 %s，代码 %s）", response.status, response.code
        )
    return None


async def prepare_reply_picture(image: str | Path) -> Picture | None:
    try:
        return await prepare_picture(image)
    except (OSError, ValueError, PILImage.DecompressionBombError, aiohttp.ClientError, TimeoutError) as exc:
        logger.debug("[二次元抽抽乐] QQ 图片预处理失败（%s）", type(exc).__name__)
        return None


async def send_qq_reply(
    event: AstrMessageEvent,
    text: str,
    *,
    markdown: str | None = None,
    actions: Sequence[tuple[str, str]] = (),
    image: str | Path | Picture | None = None,
    button_owner: str | None = None,
    use_markdown: bool = True,
    use_buttons: bool = True,
    image_mode: str = CATALOG_LINK_FIRST,
    manual_hint: str = "",
) -> QQReply:
    """Send an official QQ reply, returning its id for reply-to-result commands."""
    if event.get_platform_name() not in OFFICIAL_PLATFORMS:
        return QQReply()

    footer = manual_hint or ("快捷指令 " + " · ".join(command for _, command in actions[:5]) if actions else "")
    plain = "\n\n".join(part for part in (text.strip(), footer) if part)
    target = _reply_target(event)
    if target is None:
        return QQReply()
    picture = image if isinstance(image, Picture) else (await prepare_reply_picture(image) if image else None)

    reference = picture.source_url if picture and use_markdown else ""
    if picture and picture.inline and use_markdown and (not reference or image_mode == QQ_STORAGE_FIRST):
        try:
            async with asyncio.timeout(15):
                reference = await _markdown_image(target, picture.inline) or reference
        except TimeoutError:
            logger.debug("[二次元抽抽乐] QQ Markdown 图片上传超时。")
    has_markdown = use_markdown and bool(text or markdown)
    if has_markdown:
        body = markdown if markdown is not None else escape_markdown(text).replace("\n", "  \n")
        if reference:
            body = f"{body.rstrip()}\n\n![角色图片 #{picture.width}px #{picture.height}px]({reference})"
        elif picture:
            # Keep the text and image together when inline media is unavailable.
            result = await _media_reply(event, target, plain, picture)
            if result.handled:
                return result
        owner = button_owner if button_owner is not None else str(event.get_sender_id() or "")
        payload = {"msg_type": 2, "markdown": {"content": body.rstrip() + "\n"}}
        if use_buttons and actions:
            payload["keyboard"] = _keyboard(actions, owner)
        elif footer:
            payload["markdown"] = {"content": f"{body.rstrip()}\n\n{escape_markdown(footer)}\n"}

        response = await _send(target, payload)
        if (
            response is not None
            and response.rejected
            and "keyboard" in payload
            and response.status not in {401, 404, 429}
        ):
            # A bot may have Markdown permission while inline button creation needs approval.
            response = await _send(
                target,
                {
                    "msg_type": 2,
                    "markdown": {"content": f"{body.rstrip()}\n\n{escape_markdown(footer)}\n"},
                },
            )
        result = _accepted_reply(event, response)
        if result.handled:
            return result
        if response is not None and response.status:
            logger.debug(
                "[二次元抽抽乐] QQ 改用普通消息（状态 %s，代码 %s）", response.status, response.code
            )

    if picture and (reference or not has_markdown):
        result = await _media_reply(event, target, plain, picture)
        if result.handled:
            return result
    if not plain:
        return QQReply()
    return _accepted_reply(event, await _send(target, {"msg_type": 0, "content": plain}))


async def _media_reply(event: AstrMessageEvent, target: _Target, text: str, picture: Picture) -> QQReply:
    if not target.uses_v2:
        return QQReply()
    file_info = await _upload_image(target, picture)
    if not file_info:
        return QQReply()
    return _accepted_reply(event, await _send(target, {
        "msg_type": 7, "content": text, "media": {"file_info": file_info},
    }))


async def send_qq_markdown(
    event: AstrMessageEvent,
    text: str,
    *,
    actions: Sequence[tuple[str, str]] = (),
    markdown: str | None = None,
) -> bool:
    """Return whether the official QQ response was handled."""
    return (await send_qq_reply(event, text, actions=actions, markdown=markdown)).handled


async def send(
    event: AstrMessageEvent,
    title: str,
    body: str,
    image: str | Path | Picture | None = None,
    actions: Sequence[tuple[str, str]] = (),
    *,
    button_owner: str | None = None,
    use_markdown: bool = True,
    use_buttons: bool = True,
    image_mode: str = CATALOG_LINK_FIRST,
    quote_line: str = "",
    quote_footer: str = "",
    manual_hint: str = "",
) -> bool:
    """Format a game reply heading and preserve a readable plain-text fallback."""
    title = title.strip()
    body = body.strip()
    summary = " ".join(quote_line.splitlines()).strip()
    footer = " ".join(quote_footer.splitlines()).strip()
    text = "\n\n".join(part for part in (title, summary, body, footer) if part)
    markdown_title = f"## {escape_markdown(' '.join(title.splitlines()))}" if title else ""
    markdown_summary = f"> {escape_markdown(summary)}" if summary else ""
    markdown_body = escape_markdown(body).replace("\n", "  \n")
    markdown_footer = f"> {escape_markdown(footer)}" if footer else ""
    markdown = "\n\n".join(
        part for part in (markdown_title, markdown_summary, markdown_body, markdown_footer) if part
    )
    result = await send_qq_reply(
        event,
        text,
        markdown=markdown,
        actions=actions,
        image=image,
        button_owner=button_owner,
        use_markdown=use_markdown,
        use_buttons=use_buttons,
        image_mode=image_mode,
        manual_hint=manual_hint,
    )
    return result.handled
