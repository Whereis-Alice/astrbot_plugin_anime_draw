"""Keep QQ chat previews small while preserving detail in the uploaded image."""

import asyncio
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

MAX_WIDTH = 240
MAX_HEIGHT = 200
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 32_000_000
CATALOG_HOSTS = frozenset({
    "lain.bgm.tv", "bgmlain.waterbee.cc", "static.wikitide.net", "patchwiki.biligame.com",
    "exedra.wiki", "static.wikia.nocookie.net", "act-upload.mihoyo.com",
    "storage.moegirl.org.cn", "i.pixiv.moe", "pics0.baidu.com", "img2.baidu.com",
})


@dataclass(frozen=True, slots=True)
class PictureFile:
    data: bytes
    extension: str
    content_type: str


@dataclass(frozen=True, slots=True)
class Picture:
    preview: PictureFile
    inline: PictureFile | None
    width: int
    height: int


def _local_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(MAX_BYTES + 1)


async def _remote_bytes(url: str) -> bytes:
    parsed = urlsplit(url)
    if (
        parsed.hostname not in CATALOG_HOSTS or parsed.port not in {None, 80, 443}
        or parsed.username or parsed.password
    ):
        raise ValueError("Invalid catalog image URL")
    candidates = [url]
    if parsed.hostname == "bgmlain.waterbee.cc":
        candidates.append(parsed._replace(scheme="https", netloc="lain.bgm.tv").geturl())
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        for candidate in candidates:
            try:
                async with session.get(candidate, allow_redirects=False) as response:
                    if response.status != 200 or (response.content_length or 0) > MAX_BYTES:
                        continue
                    result = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        result.extend(chunk)
                        if len(result) > MAX_BYTES:
                            raise ValueError("Image exceeds size limit")
                    return bytes(result)
            except (aiohttp.ClientError, TimeoutError):
                continue
    raise ValueError("Catalog image unavailable")


def _encode(image: Image.Image) -> PictureFile:
    output = BytesIO()
    image.info.clear()
    if image.mode == "RGBA":
        image.save(output, format="PNG")
        return PictureFile(output.getvalue(), "png", "image/png")
    image.save(output, format="JPEG", quality=92, optimize=True)
    return PictureFile(output.getvalue(), "jpg", "image/jpeg")


def _resize(data: bytes, preserve_original: bool) -> Picture:
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("Image exceeds size limit")
    with Image.open(BytesIO(data), formats=["JPEG", "PNG", "GIF", "WEBP"]) as source:
        if source.width * source.height > MAX_PIXELS:
            raise ValueError("Image exceeds pixel limit")
        # Keep a single frame for the chat thumbnail and normalize EXIF rotation.
        corrected = ImageOps.exif_transpose(source)
        alpha = "A" in corrected.getbands() or "transparency" in corrected.info
        original = corrected.convert("RGBA" if alpha else "RGB")
        preview = original.copy()
        preview.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.Resampling.LANCZOS)
        compact = _encode(preview)
        inline = None
        if (
            preserve_original
            and source.getexif().get(274, 1) == 1
            and not getattr(source, "is_animated", False)
        ):
            if source.format == "PNG":
                inline = PictureFile(data, "png", "image/png")
            elif source.format == "JPEG" and source.mode in {"RGB", "L"}:
                inline = PictureFile(data, "jpg", "image/jpeg")
        if inline is None:
            normalized = _encode(original)
            if len(normalized.data) <= MAX_BYTES:
                inline = normalized
        return Picture(compact, inline, preview.width, preview.height)


async def prepare_picture(image: str | Path) -> Picture:
    source = str(image)
    remote = source.startswith(("https://", "http://"))
    if remote:
        data = await _remote_bytes(source)
    else:
        data = await asyncio.to_thread(_local_bytes, Path(source.removeprefix("file:///")))
    return await asyncio.to_thread(_resize, data, preserve_original=remote)
