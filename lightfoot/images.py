"""Image blocks in requests: their real token cost, and saving them to files.

An image block carries base64 data, so measuring it as text overstates its cost by
about 40x: a 200 KB screenshot is about 270,000 characters but only about 1,500
tokens. The API charges roughly width x height / 750 tokens per image, which needs only
the dimensions, read here from the first bytes of the PNG, JPEG, GIF or WebP data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct
import time
from pathlib import Path

from .config import private_dir

FALLBACK_TOKENS = 1600  # unknown format or dimensions: about a 1092 x 1092 image
PIXELS_PER_TOKEN = 750
MAX_IMAGE_TOKENS = 12_000
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def is_base64_image(block) -> bool:
    """An image block whose data is inline (the only kind lightfoot can save and measure)."""
    if not isinstance(block, dict) or block.get("type") != "image":
        return False
    source = block.get("source")
    return isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str)


def _head_bytes(data: str, n: int = 65_536) -> bytes:
    """The first bytes of the decoded image, without decoding all of it."""
    chunk = data[: (n // 3) * 4]
    try:
        return base64.b64decode(chunk + "=" * (-len(chunk) % 4), validate=False)
    except (binascii.Error, ValueError):
        return b""


def dimensions(head: bytes) -> tuple[int, int] | None:
    """(width, height) from the header of a PNG, GIF, WebP or JPEG file."""
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", head[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(head) >= 25:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X":
            return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        return None
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            # Start-of-frame markers hold the size; C4, C8 and CC are other segment types.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", head[i + 5 : i + 9])
                return w, h
            length = struct.unpack(">H", head[i + 2 : i + 4])[0]
            i += 2 + length
    return None


def image_tokens(block) -> int:
    """Approximate input tokens of one image block."""
    if not is_base64_image(block):
        return FALLBACK_TOKENS
    size = dimensions(_head_bytes(block["source"]["data"]))
    if not size or size[0] <= 0 or size[1] <= 0:
        return FALLBACK_TOKENS
    return max(1, min(MAX_IMAGE_TOKENS, (size[0] * size[1]) // PIXELS_PER_TOKEN))


def describe(block) -> str:
    """Short label such as "1280x800 png" for messages about an image."""
    if not is_base64_image(block):
        return "image"
    kind = EXTENSIONS.get(block["source"].get("media_type"), "image")
    size = dimensions(_head_bytes(block["source"]["data"]))
    return f"{size[0]}x{size[1]} {kind}" if size else kind


def save(block, folder: Path) -> Path | None:
    """Write an inline image to `folder`, named by its content hash; None if it can't be decoded."""
    if not is_base64_image(block):
        return None
    data = block["source"]["data"]
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
    ext = EXTENSIONS.get(block["source"].get("media_type"), "bin")
    path = folder / f"{hashlib.sha256(raw).hexdigest()[:16]}.{ext}"
    if path.exists():
        return path
    if folder.parent.name == ".lightfoot":
        private_dir(folder.parent)
        ignore = folder.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    private_dir(folder)
    tmp = path.with_name(f"{path.name}.{time.time_ns()}.tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)
    return path
