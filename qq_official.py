"""Native QQ Markdown and buttons using AstrBot's official adapter session."""

from __future__ import annotations

import asyncio
import base64
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image

OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
MAX_INLINE_IMAGE = 10 * 1024 * 1024
MARKDOWN_SYNTAX = re.compile(r"([\\`*_{}\[\]()#+.!|<>~\-])")


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


async def _request(target: _Target, endpoint: str, payload: dict[str, Any]) -> _Response | None:
    """Use the adapter's authentication without botpy's implicit POST retries."""
    from botpy.http import Route

    http = target.http
    try:
        await http.check_session()
        route = Route("POST", f"{target.endpoint}/{endpoint}", is_sandbox=http.is_sandbox)
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
        logger.warning("[二次元抽抽乐] QQ 会话不可用（%s）", type(exc).__name__)
        return _Response(0, None)

    try:
        async with session.post(
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
            code = int(raw_code) if str(raw_code).isdecimal() else 0
            return _Response(response.status, document, code)
    except aiohttp.ClientConnectorError:
        return _Response(0, None)
    except (OSError, TimeoutError, aiohttp.ClientError, RuntimeError) as exc:
        logger.warning("[二次元抽抽乐] QQ 未确认发送结果（%s）", type(exc).__name__)
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


def _local_image(path: Path) -> str:
    with path.open("rb") as handle:
        content = handle.read(MAX_INLINE_IMAGE + 1)
    if not 0 < len(content) <= MAX_INLINE_IMAGE:
        raise ValueError("QQ image exceeds the upload limit")
    return base64.b64encode(content).decode("ascii")


def _public_url(source: str) -> bool:
    if not source.startswith(("https://", "http://")):
        return False
    try:
        parsed = urlsplit(source)
    except ValueError:
        return False
    return bool(parsed.hostname and not parsed.username and not parsed.password)


async def _upload_image(target: _Target, image: str | Path) -> str | None:
    source = str(image)
    payload = {"file_type": 1, "srv_send_msg": False}
    try:
        if _public_url(source):
            payload["url"] = source
        else:
            local_path = Path(source.removeprefix("file:///"))
            payload["file_data"] = await asyncio.to_thread(_local_image, local_path)
    except (OSError, ValueError) as exc:
        logger.warning("[二次元抽抽乐] 角色图片读取失败（%s）", type(exc).__name__)
        return None

    response = await _request(target, "files", payload)
    if response is not None and response.accepted and response.document:
        file_info = response.document.get("file_info")
        if isinstance(file_info, str) and file_info:
            return file_info
    if response is not None:
        logger.warning(
            "[二次元抽抽乐] QQ 图片上传失败（状态 %s，代码 %s）", response.status, response.code
        )
    return None


async def _supplement_image(event: AstrMessageEvent, target: _Target, image: str | Path) -> None:
    if target.uses_v2:
        file_info = await _upload_image(target, image)
        if file_info:
            response = await _send(target, {"msg_type": 7, "media": {"file_info": file_info}})
            if response is not None and not response.accepted:
                logger.warning(
                    "[二次元抽抽乐] QQ 图片发送失败（状态 %s，代码 %s）",
                    response.status,
                    response.code,
                )
        return
    source = str(image)
    component = (
        Image.fromURL(source)
        if _public_url(source)
        else Image.fromFileSystem(source.removeprefix("file:///"))
    )
    try:
        await event.send(MessageChain(chain=[component]))
    except (OSError, ValueError, RuntimeError, aiohttp.ClientError) as exc:
        logger.warning("[二次元抽抽乐] QQ 频道图片发送失败（%s）", type(exc).__name__)


async def send_qq_reply(
    event: AstrMessageEvent,
    text: str,
    *,
    markdown: str | None = None,
    actions: Sequence[tuple[str, str]] = (),
    image: str | Path | None = None,
    button_owner: str | None = None,
    use_markdown: bool = True,
    use_buttons: bool = True,
) -> QQReply:
    """Send an official QQ reply, returning its id for reply-to-result commands."""
    target = _reply_target(event)
    if target is None:
        return QQReply()

    footer = "快捷指令 " + " · ".join(command for _, command in actions[:5]) if actions else ""
    plain = "\n\n".join(part for part in (text.strip(), footer) if part)
    if use_markdown and (text or markdown):
        body = markdown if markdown is not None else escape_markdown(text).replace("\n", "  \n")
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
            if result.delivered and image:
                await _supplement_image(event, target, image)
            return result
        if response is not None and response.status:
            logger.debug(
                "[二次元抽抽乐] QQ 改用普通消息（状态 %s，代码 %s）", response.status, response.code
            )

    if image and target.uses_v2:
        file_info = await _upload_image(target, image)
        if file_info:
            payload = {"msg_type": 7, "media": {"file_info": file_info}}
            if plain:
                payload["content"] = plain
            result = _accepted_reply(event, await _send(target, payload))
            if result.handled:
                return result
    if not plain:
        return QQReply()
    result = _accepted_reply(event, await _send(target, {"msg_type": 0, "content": plain}))
    if result.delivered and image and not target.uses_v2:
        await _supplement_image(event, target, image)
    return result


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
    image: str | Path | None = None,
    actions: Sequence[tuple[str, str]] = (),
    *,
    button_owner: str | None = None,
    use_markdown: bool = True,
    use_buttons: bool = True,
) -> bool:
    """Format a game reply heading and preserve a readable plain-text fallback."""
    title = title.strip()
    body = body.strip()
    text = "\n\n".join(part for part in (title, body) if part)
    markdown_title = f"## {escape_markdown(' '.join(title.splitlines()))}" if title else ""
    markdown_body = escape_markdown(body).replace("\n", "  \n")
    markdown = "\n\n".join(part for part in (markdown_title, markdown_body) if part)
    result = await send_qq_reply(
        event,
        text,
        markdown=markdown,
        actions=actions,
        image=image,
        button_owner=button_owner,
        use_markdown=use_markdown,
        use_buttons=use_buttons,
    )
    return result.handled
