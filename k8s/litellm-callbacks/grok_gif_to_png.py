"""grok_gif_to_png — 打到 sub2api(x.ai) 的请求里，把 GIF 图转成 PNG（只取第一帧）。

Why（2026-10-14 实测）: x.ai 只收 JPG/PNG/WebP/ICO，GIF 一律
``400 invalid_image: Downloaded response does not contain a valid JPG, PNG, WebP, or ICO image``。
Claude Code 的 tool_result 里会带 ``image/gif`` 截图/动图（线上 cursor-limengxin-h4fd，30 张），
每次 400 后兜底到 deepseek。A/B（``scripts/grok-422/probe_gif_image.py``，/v1/messages 打 sa-grok-4.7，
关重试关兜底）: tool_result 带 PNG 200 200 / 同位置换 GIF 400 400 / 纯文本 200 200。

做法: ``async_pre_call_deployment_hook``，``api_base`` 含 sub2api 才动。三种形状都认：
  - Anthropic ``{"type":"image","source":{"type":"base64","media_type":"image/gif","data":...}}``（含 tool_result 里嵌套的）
  - Responses ``{"type":"input_image","image_url":"data:image/gif;base64,..."}``
  - Chat ``{"type":"image_url","image_url":{"url":"data:image/gif;base64,..."}}``
pod 里没有 Pillow ⇒ 纯 Python 解 GIF 第一帧（LZW + 隔行 + 透明色）→ RGBA PNG。
解不开（坏图 / 过大）就把这一张换成一句文字说明，绝不让整条请求失败；钩子自身出错原样放行。
"""
import base64
import logging
import struct
import zlib
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("grok_gif_to_png")

_SUB2API_MARKER = "sub2api"
_GIF_DATA_URL = "data:image/gif;base64,"
_MAX_PIXELS = 4096 * 4096
_OMITTED = "[GIF image omitted: the model only accepts JPG/PNG/WebP]"


# ── GIF 第一帧 → PNG（纯 Python）────────────────────────────────────

def _lzw_decode(data: bytes, min_code_size: int, n_pixels: int) -> bytes:
    clear = 1 << min_code_size
    eoi = clear + 1
    size = min_code_size + 1
    base = [bytes([i]) for i in range(clear)] + [b"", b""]
    table = list(base)
    out = bytearray()
    prev = None
    data = data + b"\x00\x00\x00"
    bitpos = 0
    total = (len(data) - 3) * 8
    while bitpos + size <= total and len(out) < n_pixels:
        i = bitpos >> 3
        code = (int.from_bytes(data[i:i + 3], "little") >> (bitpos & 7)) & ((1 << size) - 1)
        bitpos += size
        if code == clear:
            table = list(base)
            size = min_code_size + 1
            prev = None
            continue
        if code == eoi:
            break
        if prev is None:
            if code >= len(table):
                raise ValueError("bad first code")
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            if len(table) < 4096:
                table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            if len(table) < 4096:
                table.append(entry)
        else:
            raise ValueError("bad code")
        out += entry
        prev = entry
        if len(table) == (1 << size) and size < 12:
            size += 1
    return bytes(out[:n_pixels])


def _sub_blocks(buf: bytes, pos: int) -> tuple[bytes, int]:
    parts = []
    while True:
        n = buf[pos]
        pos += 1
        if n == 0:
            return b"".join(parts), pos
        parts.append(buf[pos:pos + n])
        pos += n


def _png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def gif_to_png(gif: bytes) -> bytes:
    """GIF 第一帧 → RGBA PNG。格式不对 / 过大抛 ValueError。"""
    if gif[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a gif")
    flags = gif[10]
    pos = 13
    gct = None
    if flags & 0x80:
        n = 3 * (2 << (flags & 7))
        gct = gif[pos:pos + n]
        pos += n
    transparent = None
    while pos < len(gif):
        b = gif[pos]
        pos += 1
        if b == 0x21:  # extension
            label = gif[pos]
            pos += 1
            payload, pos = _sub_blocks(gif, pos)
            if label == 0xF9 and len(payload) >= 4 and payload[0] & 1:
                transparent = payload[3]
            continue
        if b == 0x2C:  # image descriptor
            _left, _top, w, h, iflags = struct.unpack("<HHHHB", gif[pos:pos + 9])
            pos += 9
            if not w or not h or w * h > _MAX_PIXELS:
                raise ValueError("bad size")
            palette = gct
            if iflags & 0x80:
                n = 3 * (2 << (iflags & 7))
                palette = gif[pos:pos + n]
                pos += n
            if not palette:
                raise ValueError("no palette")
            mcs = gif[pos]
            pos += 1
            data, pos = _sub_blocks(gif, pos)
            idx = _lzw_decode(data, mcs, w * h)
            idx = idx + bytes(w * h - len(idx))  # 截断的图用 0 补齐，不报错
            n_colors = len(palette) // 3
            lut = []
            for c in range(256):
                if c < n_colors:
                    r, g, bl = palette[3 * c:3 * c + 3]
                else:
                    r = g = bl = 0
                lut.append(bytes((r, g, bl, 0 if c == transparent else 255)))
            rows_in = [idx[y * w:(y + 1) * w] for y in range(h)]
            if iflags & 0x40:  # interlaced
                order = [y for start, step in ((0, 8), (4, 8), (2, 4), (1, 2)) for y in range(start, h, step)]
                rows = [b""] * h
                for src, dst in enumerate(order):
                    rows[dst] = rows_in[src]
            else:
                rows = rows_in
            return _png(w, h, [b"".join(lut[p] for p in row) for row in rows])
        if b == 0x3B:
            break
        raise ValueError("bad block")
    raise ValueError("no image")


# ── 请求改写 ────────────────────────────────────────────────────────

def _convert_b64(b64: str) -> str | None:
    try:
        return base64.b64encode(gif_to_png(base64.b64decode(b64, validate=False))).decode()
    except Exception:
        return None


def _fix_part(part: dict, counts: dict) -> dict | None:
    """返回替换后的 part；None = 不是 GIF，保持原样。"""
    t = part.get("type")
    if t == "image":
        src = part.get("source")
        if isinstance(src, dict) and src.get("type") == "base64" and src.get("media_type") == "image/gif":
            png = _convert_b64(src.get("data") or "")
            if png is None:
                counts["omitted"] = counts.get("omitted", 0) + 1
                return {"type": "text", "text": _OMITTED}
            counts["converted"] = counts.get("converted", 0) + 1
            return {**part, "source": {**src, "media_type": "image/png", "data": png}}
        return None
    if t in ("input_image", "image_url"):
        holder = part.get("image_url")
        url = holder.get("url") if isinstance(holder, dict) else holder
        if isinstance(url, str) and url.startswith(_GIF_DATA_URL):
            png = _convert_b64(url[len(_GIF_DATA_URL):])
            if png is None:
                counts["omitted"] = counts.get("omitted", 0) + 1
                return {"type": "input_text" if t == "input_image" else "text", "text": _OMITTED}
            new_url = "data:image/png;base64," + png
            counts["converted"] = counts.get("converted", 0) + 1
            return {**part, "image_url": ({**holder, "url": new_url} if isinstance(holder, dict) else new_url)}
    return None


def _walk(obj: Any, counts: dict) -> None:
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, dict):
                repl = _fix_part(item, counts)
                if repl is not None:
                    obj[i] = repl
                    continue
            _walk(item, counts)
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _walk(v, counts)


def _api_base(kwargs: dict) -> str:
    for src in (kwargs, kwargs.get("litellm_params") or {}):
        if isinstance(src, dict) and src.get("api_base"):
            return str(src["api_base"])
    return ""


def fix_request(kwargs: dict) -> dict:
    counts: dict = {}
    for key in ("messages", "input"):
        v = kwargs.get(key)
        if isinstance(v, list):
            _walk(v, counts)
    return counts


class GrokGifToPng(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> Any:
        try:
            if isinstance(kwargs, dict) and _SUB2API_MARKER in _api_base(kwargs):
                counts = fix_request(kwargs)
                if counts:
                    _log.warning("grok_gif_to_png: %s model=%s", counts, kwargs.get("model"))
        except Exception as exc:  # 防御：改不动也绝不能拦请求
            _log.warning("grok_gif_to_png error: %r", exc)
        return kwargs


grok_gif_to_png_instance = GrokGifToPng()
grok_gif_to_png = grok_gif_to_png_instance

# litellm-198-lane-callback-add.py verify 认这一行判「已加载」
_log.warning("grok_gif_to_png: installed (gate api_base contains %r)", _SUB2API_MARKER)
