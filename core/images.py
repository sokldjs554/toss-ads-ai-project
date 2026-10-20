"""올린 이미지를 받아들일지 정하고, 받아들이면 안전한 모양으로 다듬는다.

PIL 같은 이미지 라이브러리 없이 (브라우저 실행판에서도 돌도록) 바이트 머리글만 읽는다.

- 종류는 파일 이름이나 Content-Type 이 아니라 **맨 앞 바이트(매직 바이트)** 로 정한다. 이름·Content-Type 이 그 종류와 다르면 거부한다.
- 한 파일이 두 종류 이상이거나(polyglot), 이미지 끝 뒤에 다른 데이터가 붙어 있으면 거부한다.
- 가로세로는 PNG(IHDR)·JPEG(SOF)·GIF(논리 화면)·WEBP(VP8/VP8L/VP8X) 머리글에서 읽는다. 읽을 수 없거나 0 이면 손상으로 본다.
- 크기 상한(5MB, SVG 1MB), 가로세로 각 8,192 · 곱 40M 픽셀 상한(압축 폭탄)을 둔다. 이 값들은 이 프로젝트가 정한 안전 상한이다.
- SVG 는 글자이자 코드이므로 **정화**한다: DOCTYPE/ENTITY(엔티티 폭탄)는 거부, 스크립트·foreignObject·on* 이벤트·javascript:·바깥 주소(href/url()/@import)는 제거하고
  제거했다는 사실을 sanitized_notes 로 알린다. 정화는 '무엇이 있었는지 모르는 채 통과' 를 막으려는 것이지, 정화가 끝난 SVG 가 안전하다는 보증이 아니다.

이 모듈은 이미지의 **내용(글자)을 읽지 않는다.** 읽는 일은 SVG 의 <text> 만 여기서 코드로 하고(svg_scene), 래스터 이미지는 모델이 읽는다(agents/vision.py).
"""
from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional, Union

from llm.base import ImageInput

# --- 이 프로젝트가 정한 안전 상한 (실제 플랫폼의 업로드 규격이 아니다) --------------------------------
MAX_RASTER_BYTES = 5 * 1024 * 1024
MAX_SVG_BYTES = 1 * 1024 * 1024
MAX_SIDE_PX = 8192
MAX_PIXELS = 40_000_000
MAX_SVG_ELEMENTS = 5_000
MAX_SVG_DEPTH = 40
MAX_SVG_EXPANDED = 20_000    # use 를 펼친 요소 수 상한
SVG_DEFAULT_SIZE = (300, 150)   # 브라우저가 크기 표시 없는 SVG 에 쓰는 기본값

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

MIME_BY_KIND = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}
_EXTENSIONS = {"png": {"png"}, "jpeg": {"jpg", "jpeg", "jpe", "jfif"}, "gif": {"gif"}, "webp": {"webp"}, "svg": {"svg"}}
_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}
KIND_LABEL = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP", "svg": "SVG"}


# ---------------------------------------------------------------------------
# 결과 형식
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ImageRejection:
    """받아들이지 않은 이유. message 는 화면에 그대로 보여 줄 쉬운 말, detail 은 상세 기록용."""

    code: str
    message: str
    detail: str = ""


@dataclass(frozen=True)
class ImageAsset:
    """받아들인 이미지. data 는 처리에 쓸 바이트(SVG 는 정화한 것)이고, sha256·bytes_len 은 올린 원본의 것이다."""

    sha256: str
    mime: str
    width: int
    height: int
    bytes_len: int
    name: str
    kind: str                                   # "raster" | "svg"
    data: bytes = field(repr=False, compare=False, default=b"")
    fmt: str = ""                               # png | jpeg | gif | webp | svg
    sanitized_notes: tuple = ()
    svg_unread_shapes: int = 0                  # SVG 안의 path·image·polygon·polyline 수 — 글자를 도형으로 그렸다면 <text> 로는 읽을 수 없다
    svg_embedded_images: int = 0                # SVG 안에 들어 있는 그림(래스터) 수 — 그림 속 글자는 SVG 글자 읽기로 읽을 수 없다

    @property
    def short_hash(self) -> str:
        return self.sha256[:16]

    def log_fields(self) -> dict:
        """처리 기록에 남길 값 — 해시·크기·종류뿐이다 (파일 이름과 바이트는 남기지 않는다)."""
        return {"sha256": self.short_hash, "bytes": self.bytes_len, "kind": self.kind, "format": self.fmt, "width": self.width, "height": self.height}


def is_rejection(x: object) -> bool:
    return isinstance(x, ImageRejection)


def safe_name(name: str, limit: int = 80) -> str:
    """화면·기록에 보일 파일 이름: 경로와 제어 문자를 걷어내고 길이를 줄인다 (HTML 이스케이프는 쓰는 쪽에서 한다)."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cn", "Cs"))
    return base[:limit]


class _Corrupt(Exception):
    pass


def _rej(code: str, message: str, detail: str = "") -> ImageRejection:
    return ImageRejection(code=code, message=message, detail=detail)


_CORRUPT_MSG = "이미지 파일이 손상되었거나 규격에 맞지 않아 읽을 수 없습니다. 다른 파일로 다시 올려 주세요."


# ---------------------------------------------------------------------------
# 종류 판별
# ---------------------------------------------------------------------------
_SVG_ROOT = re.compile(r"<(?:[A-Za-z_][\w.\-]*:)?svg[\s>/]", re.I)


def sniff_kind(data: bytes) -> Optional[str]:
    """맨 앞 바이트로 종류를 정한다. 알 수 없으면 None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:65536]
    stripped = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
    stripped = stripped.lstrip()
    if stripped.startswith(b"<"):
        try:
            text = stripped.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return None
        if _SVG_ROOT.search(text):
            return "svg"
    return None


# ---------------------------------------------------------------------------
# PNG · JPEG · GIF · WEBP 머리글
# ---------------------------------------------------------------------------
_PNG_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}


def _parse_png(b: bytes) -> tuple[int, int, int]:
    """(가로, 세로, 끝 위치). 모든 덩어리의 CRC 를 확인하고 IEND 까지 걷는다."""
    pos, n = 8, len(b)
    w = h = None
    seen_idat = False
    first = True
    while True:
        if pos + 12 > n:
            raise _Corrupt("PNG 가 IEND 전에 끝남")
        length = int.from_bytes(b[pos:pos + 4], "big")
        ctype = b[pos + 4:pos + 8]
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in ctype):
            raise _Corrupt("PNG 덩어리 이름이 이상함")
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > n:
            raise _Corrupt("PNG 덩어리 길이가 파일을 넘음")
        body = b[pos + 8:pos + 8 + length]
        crc = int.from_bytes(b[pos + 8 + length:end], "big")
        if (zlib.crc32(ctype + body) & 0xFFFFFFFF) != crc:
            raise _Corrupt(f"PNG {ctype.decode('ascii')} 덩어리 CRC 불일치")
        if first:
            if ctype != b"IHDR" or length != 13:
                raise _Corrupt("PNG 첫 덩어리가 IHDR 가 아님")
            w, h, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth not in _PNG_DEPTHS.get(color, set()) or comp != 0 or filt != 0 or interlace not in (0, 1):
                raise _Corrupt("PNG IHDR 값이 규격에 맞지 않음")
            first = False
        elif ctype == b"IHDR":
            raise _Corrupt("PNG IHDR 가 두 번 나옴")
        if ctype == b"IDAT":
            seen_idat = True
        pos = end
        if ctype == b"IEND":
            break
    if not seen_idat or not w or not h:
        raise _Corrupt("PNG 에 그림 데이터(IDAT)나 크기가 없음")
    return w, h, pos


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _parse_jpeg(b: bytes) -> tuple[int, int, int]:
    """(가로, 세로, EOI 뒤 위치). 구간 길이로 건너뛰므로 EXIF 안의 썸네일 SOF 를 크기로 착각하지 않는다."""
    n = len(b)
    pos = 2
    size: Optional[tuple[int, int]] = None
    while True:
        if pos >= n or b[pos] != 0xFF:
            raise _Corrupt("JPEG 구간 시작(0xFF)이 아님")
        while pos < n and b[pos] == 0xFF:
            pos += 1
        if pos >= n:
            raise _Corrupt("JPEG 가 중간에 끝남")
        marker = b[pos]
        pos += 1
        if marker == 0xD9:                       # EOI
            if size is None:
                raise _Corrupt("JPEG 에 크기 구간(SOF)이 없음")
            return size[0], size[1], pos
        if marker == 0x00 or marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            raise _Corrupt("JPEG 구간 순서가 이상함")
        if pos + 2 > n:
            raise _Corrupt("JPEG 구간 길이를 읽을 수 없음")
        length = int.from_bytes(b[pos:pos + 2], "big")
        if length < 2 or pos + length > n:
            raise _Corrupt("JPEG 구간 길이가 파일을 넘음")
        if marker in _SOF:
            if length < 8:
                raise _Corrupt("JPEG SOF 가 너무 짧음")
            precision = b[pos + 2]
            height = int.from_bytes(b[pos + 3:pos + 5], "big")
            width = int.from_bytes(b[pos + 5:pos + 7], "big")
            ncomp = b[pos + 7]
            if precision not in (8, 12, 16) or ncomp not in (1, 3, 4) or length != 8 + 3 * ncomp or width == 0 or height == 0:
                raise _Corrupt("JPEG SOF 값이 규격에 맞지 않음")
            if size is None:
                size = (width, height)
        pos += length
        if marker == 0xDA:                        # SOS: 뒤따르는 부호화 데이터를 다음 구간 표지까지 건너뛴다
            if size is None:
                raise _Corrupt("JPEG 에서 SOF 가 SOS 보다 늦음")
            while True:
                idx = b.find(b"\xff", pos)
                if idx < 0 or idx + 1 >= n:
                    raise _Corrupt("JPEG 에 끝 표지(EOI)가 없음")
                nxt = b[idx + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    pos = idx + 2
                elif nxt == 0xFF:
                    pos = idx + 1
                else:
                    pos = idx
                    break


def _parse_gif(b: bytes) -> tuple[int, int, int]:
    n = len(b)
    if n < 14:
        raise _Corrupt("GIF 머리글이 짧음")
    w = int.from_bytes(b[6:8], "little")
    h = int.from_bytes(b[8:10], "little")
    if w == 0 or h == 0:
        raise _Corrupt("GIF 논리 화면 크기가 0")
    packed = b[10]
    pos = 13
    if packed & 0x80:
        pos += 3 * (1 << ((packed & 7) + 1))

    def skip_sub_blocks(p: int) -> int:
        while True:
            if p >= n:
                raise _Corrupt("GIF 하위 블록이 중간에 끝남")
            size = b[p]
            p += 1
            if size == 0:
                return p
            p += size

    frames = 0
    while True:
        if pos >= n:
            raise _Corrupt("GIF 에 끝 표지가 없음")
        block = b[pos]
        if block == 0x3B:
            pos += 1
            break
        if block == 0x21:
            pos = skip_sub_blocks(pos + 2)
        elif block == 0x2C:
            if pos + 10 > n:
                raise _Corrupt("GIF 이미지 설명이 짧음")
            lpacked = b[pos + 9]
            pos += 10
            if lpacked & 0x80:
                pos += 3 * (1 << ((lpacked & 7) + 1))
            pos = skip_sub_blocks(pos + 1)
            frames += 1
        else:
            raise _Corrupt("GIF 블록 종류를 알 수 없음")
    if frames == 0:
        raise _Corrupt("GIF 에 그림 프레임이 없음")
    return w, h, pos


def _parse_webp(b: bytes) -> tuple[int, int, int]:
    n = len(b)
    if n < 20:
        raise _Corrupt("WEBP 머리글이 짧음")
    riff_size = int.from_bytes(b[4:8], "little")
    if riff_size + 8 != n:
        raise _Corrupt("WEBP RIFF 크기가 파일 크기와 다름")
    pos = 12
    size: Optional[tuple[int, int]] = None
    while pos < n:
        if pos + 8 > n:
            raise _Corrupt("WEBP 덩어리 머리글이 짧음")
        fourcc = b[pos:pos + 4]
        csize = int.from_bytes(b[pos + 4:pos + 8], "little")
        body = pos + 8
        end = body + csize + (csize & 1)
        if end > n:
            raise _Corrupt("WEBP 덩어리가 파일을 넘음")
        if size is None:
            if fourcc == b"VP8 ":
                if csize < 10 or b[body + 3:body + 6] != b"\x9d\x01\x2a" or (b[body] & 1) != 0:
                    raise _Corrupt("WEBP VP8 키프레임 머리글이 이상함")
                size = (int.from_bytes(b[body + 6:body + 8], "little") & 0x3FFF, int.from_bytes(b[body + 8:body + 10], "little") & 0x3FFF)
            elif fourcc == b"VP8L":
                if csize < 5 or b[body] != 0x2F:
                    raise _Corrupt("WEBP VP8L 서명이 이상함")
                bits = int.from_bytes(b[body + 1:body + 5], "little")
                size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            elif fourcc == b"VP8X":
                if csize != 10:
                    raise _Corrupt("WEBP VP8X 크기가 이상함")
                size = (int.from_bytes(b[body + 4:body + 7], "little") + 1, int.from_bytes(b[body + 7:body + 10], "little") + 1)
            else:
                raise _Corrupt("WEBP 첫 덩어리가 VP8/VP8L/VP8X 가 아님")
        pos = end
    if size is None or size[0] == 0 or size[1] == 0:
        raise _Corrupt("WEBP 크기를 읽을 수 없음")
    return size[0], size[1], n


# 래스터 파일 안에 숨은 다른 형식·실행 가능한 글의 표지. 압축된 그림 데이터에서 우연히 나올 확률은 무시할 만큼 작다.
_FOREIGN = re.compile(rb"<svg[\s>/:]|<script|<html|<\?php|<!doctype|%PDF-|PK\x03\x04|<iframe|<body[\s>]", re.I)
_OTHER_MAGIC = {
    "png": re.compile(rb"GIF8[79]a|RIFF[\s\S]{4}WEBP"),
    "jpeg": re.compile(rb"\x89PNG\r\n\x1a\n|GIF8[79]a|RIFF[\s\S]{4}WEBP"),
    "gif": re.compile(rb"\x89PNG\r\n\x1a\n|\xff\xd8\xff|RIFF[\s\S]{4}WEBP"),
    "webp": re.compile(rb"\x89PNG\r\n\x1a\n|\xff\xd8\xff|GIF8[79]a"),
}


def _polyglot_reason(b: bytes, kind: str, end: int) -> Optional[str]:
    if end != len(b):
        return f"이미지가 끝난 뒤({end}바이트 이후)에 {len(b) - end}바이트가 더 붙어 있음"
    m = _FOREIGN.search(b)
    if m:
        return f"이미지 데이터 안에서 다른 형식·스크립트 표지({m.group(0)[:12]!r})가 발견됨"
    if kind == "png" or kind == "gif" or kind == "webp":
        m2 = _OTHER_MAGIC[kind].search(b, 1)
        if m2:
            return f"다른 이미지 형식의 표지({m2.group(0)[:8]!r})가 함께 들어 있음"
    elif kind == "jpeg":
        m2 = _OTHER_MAGIC["jpeg"].search(b, 1)
        if m2:
            return f"다른 이미지 형식의 표지({m2.group(0)[:8]!r})가 함께 들어 있음"
    return None


# ---------------------------------------------------------------------------
# SVG 정화
# ---------------------------------------------------------------------------
_BLOCKED_TAGS = {"script", "foreignobject", "iframe", "object", "embed", "applet", "audio", "video", "canvas", "animate", "set", "animatemotion",
                 "animatetransform", "animatecolor", "handler", "listener", "meta", "link", "base", "body", "html", "head", "frame", "frameset"}
_UNREAD_SHAPES = {"path", "image", "polygon", "polyline"}      # 글자 모양을 그릴 수 있는 요소 (rect·circle 같은 기본 도형은 세지 않는다)
_DATA_IMAGE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=]*$", re.I)
_CSS_IMPORT = re.compile(r"@import\b[^;]*;?", re.I)
_CSS_URL = re.compile(r"url\(", re.I)
_CSS_DECLARATION = re.compile(r"[^;{}]+;?")
_CSS_BAD_DECL = re.compile(r"expression\s*\(|javascript:|vbscript:|-moz-binding|behavior\s*:", re.I)
_CSS_URL_ATTRS = {"fill", "stroke", "filter", "clip-path", "mask", "cursor", "marker", "marker-start", "marker-mid", "marker-end", "color-profile"}
_DOCTYPE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)\b", re.I)
_XML_STYLESHEET = re.compile(r"<\?xml-stylesheet", re.I)
_CTRL = re.compile(r"[\x00-\x20\x7f\u200b-\u200f\u2028\u2029\ufeff]+")
_LEN = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*(px|pt|pc|mm|cm|in|em|rem|%))?", re.I)
_TO_PX = {"": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "em": 16.0, "rem": 16.0}


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.S)   # 끝나지 않은 주석도 EOF 까지 한 번만 읽고 버린다


def _clean_css_urls(css: str) -> tuple[str, bool]:
    """URL 토큰을 한 번씩 읽는다. 끝나지 않은 토큰은 스타일을 비워 닫는다 (다음 url( 부터 같은 나머지를 다시 훑지 않는다)."""
    parts: list[str] = []
    pos = 0
    while match := _CSS_URL.search(css, pos):
        start = match.end()
        while start < len(css) and css[start].isspace():
            start += 1
        quote = css[start:start + 1]
        if quote in ("'", '"'):
            value_start = start + 1
            value_end = css.find(quote, value_start)
            if value_end < 0:
                return "", True
            end = value_end + 1
            while end < len(css) and css[end].isspace():
                end += 1
            if end == len(css) or css[end] != ")":
                return "", True
        else:
            value_start = start
            end = value_end = css.find(")", start)
            if end < 0:
                return "", True
        parts.append(css[pos:match.start()])
        value = css[value_start:value_end].strip()
        parts.append(css[match.start():end + 1] if value.startswith("#") else "none")
        pos = end + 1
    parts.append(css[pos:])
    out = "".join(parts)
    return out, out != css


def _clean_css(css: str) -> tuple[str, bool]:
    """스타일에서 바깥 주소·@import·실행 구문을 걷어낸다. 백슬래시 이스케이프(\\75rl( 처럼 글자를 숨기는 표기)가 있으면 검사를 피할 수 있으므로 그 스타일은 통째로 비운다."""
    if "\\" in css:
        return "", True
    out = _CSS_COMMENT.sub("", css)
    out = _CSS_IMPORT.sub("", out)
    out = _CSS_DECLARATION.sub(lambda m: "" if _CSS_BAD_DECL.search(m.group(0)) else m.group(0), out)
    out, _ = _clean_css_urls(out)
    return out, out != css


def _svg_length(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    m = _LEN.fullmatch(value.strip())
    if not m or (m.group(2) or "") == "%":
        return None
    return float(m.group(1)) * _TO_PX[(m.group(2) or "").lower()]


def _svg_dimensions(root: ET.Element) -> tuple[int, int, bool]:
    """(가로, 세로, 기본값을 썼는가). width/height 가 숫자면 그 값, 아니면 viewBox, 아니면 기본값."""
    w, h = _svg_length(root.get("width")), _svg_length(root.get("height"))
    vb = root.get("viewBox")
    vw = vh = None
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            try:
                vw, vh = float(parts[2]), float(parts[3])
            except ValueError:
                vw = vh = None
    if (w is None or h is None) and vw and vh:
        if w is None and h is None:
            w, h = vw, vh
        elif w is None:
            w = h * vw / vh
        else:
            h = w * vh / vw
    if not w or not h or w <= 0 or h <= 0:
        return SVG_DEFAULT_SIZE[0], SVG_DEFAULT_SIZE[1], True
    return int(round(w)), int(round(h)), False


def _expanded_size(root: ET.Element) -> Optional[int]:
    """use 를 펼쳤을 때 그려질 요소 수의 어림값(상한 도달 시 멈춤). use 가 서로를 부르는 순환이면 None."""
    ids = {el.get("id"): el for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
    memo: dict[int, int] = {}
    visiting: set[int] = set()
    cap = MAX_SVG_EXPANDED * 4

    def weight(el: ET.Element) -> int:
        key = id(el)
        if key in memo:
            return memo[key]
        if key in visiting:
            raise ValueError("cycle")
        visiting.add(key)
        total = 1
        for ch in el:
            if isinstance(ch.tag, str):
                total = min(cap, total + weight(ch))
        if _local(el.tag)[1].lower() == "use":
            href = next((v for k, v in el.attrib.items() if _local(k)[1].lower() == "href"), "")
            target = ids.get(href[1:]) if href.startswith("#") else None
            if target is not None:
                total = min(cap, total + weight(target))
        visiting.discard(key)
        memo[key] = total
        return total

    try:
        return weight(root)
    except (ValueError, RecursionError):
        return None


def sanitize_svg(raw: bytes) -> Union[tuple[bytes, list[str], dict], ImageRejection]:
    """SVG 를 정화한다. 반환: (정화한 SVG 바이트, 알림 목록, 부가 정보{width,height,unread_shapes,embedded_images}) 또는 거부.

    거부: DOCTYPE/ENTITY(엔티티 폭탄), UTF-8 이 아님, XML 이 깨짐, 루트가 svg 가 아님, 요소가 너무 많거나 너무 깊음.
    제거: 스크립트·foreignObject 등 실행/삽입 요소, on* 속성, javascript: 값, 바깥 주소(href·url()·@import), 편집기 전용 요소·속성.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _rej("svg_invalid", "SVG 파일의 글자 인코딩(UTF-8)을 읽을 수 없습니다.", "utf-8 decode 실패")
    if "\x00" in text:
        return _rej("svg_invalid", "SVG 파일에 읽을 수 없는 바이너리 값이 섞여 있습니다.", "NUL 포함")
    if _DOCTYPE.search(text):
        return _rej("svg_dangerous", "이 SVG 는 DOCTYPE/ENTITY 선언을 포함하고 있어 받을 수 없습니다. 외부 데이터를 끌어오거나 파일을 부풀리는 데 쓰일 수 있는 구조입니다. 선언을 뺀 SVG 로 다시 올려 주세요.",
                    "DOCTYPE/ENTITY 선언 발견 (엔티티 폭탄·외부 개체 위험)")
    notes: list[str] = []
    if _XML_STYLESHEET.search(text):
        notes.append("외부 스타일시트를 불러오는 처리 명령(xml-stylesheet)을 제거했습니다.")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        return _rej("svg_invalid", "SVG 파일이 올바른 XML 이 아니라서 읽을 수 없습니다.", f"XML 구문 오류: {e}")
    except (RecursionError, MemoryError):
        return _rej("svg_invalid", "SVG 구조가 너무 복잡해 읽을 수 없습니다.", "파서 재귀/메모리 한계")
    ns0, name0 = _local(root.tag)
    if name0 != "svg" or ns0 not in ("", SVG_NS):
        return _rej("svg_invalid", "SVG 파일이 아닙니다 (맨 바깥 요소가 svg 가 아님).", f"루트 {root.tag}")

    # 구조 상한: 반복으로 센다 (재귀 직렬화 전에 걸러야 한다)
    count = 0
    stack = [(root, 1)]
    while stack:
        el, depth = stack.pop()
        count += 1
        if depth > MAX_SVG_DEPTH or count > MAX_SVG_ELEMENTS:
            return _rej("svg_invalid", "SVG 구조가 너무 복잡합니다 (요소가 너무 많거나 너무 깊게 겹쳐 있음).", f"요소 {count}+ / 깊이 {depth}")
        for ch in el:
            stack.append((ch, depth + 1))

    removed: dict[str, int] = {}
    unread = 0
    embedded = 0
    # use 로 같은 묶음을 겹쳐 부르면 요소 수는 적어도 그려질 때 기하급수로 늘 수 있다(SVG 판 엔티티 폭탄). 늘어난 총량을 미리 세어 상한을 넘거나 서로를 부르면 거부한다.
    expanded = _expanded_size(root)
    if expanded is None or expanded > MAX_SVG_EXPANDED:
        return _rej("svg_dangerous", "이 SVG 는 같은 도형을 겹쳐 부르는(use) 구조가 너무 복잡해 그리면 화면이 멈출 수 있어 받을 수 없습니다.", f"use 를 펼친 요소 수 {expanded or '순환'} > {MAX_SVG_EXPANDED}")

    def bump(key: str) -> None:
        removed[key] = removed.get(key, 0) + 1

    # 이름공간 없는 요소는 SVG 로 본다 (브라우저는 xmlns 없는 SVG 를 그리지 않으므로 정화 결과에는 xmlns 를 붙인다)
    for el in root.iter():
        if isinstance(el.tag, str) and not el.tag.startswith("{"):
            el.tag = "{" + SVG_NS + "}" + el.tag

    def clean_attrs(el: ET.Element, lname: str) -> None:
        for attr in list(el.attrib):
            ans, aname = _local(attr)
            lan = aname.lower()
            value = el.attrib[attr]
            norm = _CTRL.sub("", value).lower()
            drop: Optional[str] = None
            if ans not in ("", XLINK_NS, XML_NS):
                drop = "editor_attr"
            elif lan.startswith("on"):
                drop = "event"
            elif ans == XML_NS and lan == "base":
                drop = "external_ref"
            elif lan in ("src", "formaction", "action"):
                drop = "external_ref"
            elif lan == "href":
                if norm.startswith("#"):
                    pass
                elif _DATA_IMAGE.match(norm) and lname == "image":
                    pass
                else:
                    drop = "js_url" if ("javascript:" in norm or "vbscript:" in norm) else "external_ref"
            elif "javascript:" in norm or "vbscript:" in norm or norm.startswith(("data:text", "data:application")):
                drop = "js_url"
            elif lan == "style":
                cleaned, changed = _clean_css(value)
                if changed:
                    bump("css")
                    el.set(attr, cleaned)
            elif lan in _CSS_URL_ATTRS or "url(" in norm:
                _, changed = _clean_css(value)
                if changed:
                    drop = "external_ref"
            if drop:
                bump(drop)
                del el.attrib[attr]

    clean_attrs(root, "svg")
    work = [root]
    while work:
        parent = work.pop()
        for child in list(parent):
            if not isinstance(child.tag, str):          # 주석·처리 명령은 파서가 이미 버렸다
                parent.remove(child)
                continue
            ns, name = _local(child.tag)
            lname = name.lower()
            if ns != SVG_NS:
                bump("non_svg")
                parent.remove(child)
                continue
            if lname in _BLOCKED_TAGS:
                bump("script" if lname == "script" else ("foreign" if lname == "foreignobject" else "blocked_tag"))
                parent.remove(child)
                continue
            clean_attrs(child, lname)
            if lname == "style":
                cleaned, changed = _clean_css(child.text or "")
                if changed:
                    bump("css")
                    child.text = cleaned
            if lname in ("image", "use"):
                href = next((v for k, v in child.attrib.items() if _local(k)[1].lower() == "href"), None)
                if href is None:                                  # 바깥 연결을 걷어내 가리킬 곳이 없어진 요소는 아무것도 그리지 못하므로 함께 뺀다
                    bump("empty_image")
                    parent.remove(child)
                    continue
                if lname == "image":
                    embedded += 1
            if lname in _UNREAD_SHAPES:
                unread += 1
            work.append(child)

    # 알림 문장
    labels = [("script", "스크립트"), ("foreign", "다른 문서를 끼워 넣는 요소"), ("blocked_tag", "코드를 실행하거나 바깥 내용을 끌어오는 요소"),
              ("event", "클릭 등에 반응해 코드를 실행하는 속성"), ("js_url", "코드를 실행하는 주소(javascript:)"), ("external_ref", "바깥 주소를 가리키는 연결"),
              ("css", "스타일 속의 바깥 주소"), ("empty_image", "가리킬 곳이 없어진 그림·재사용 요소"),
              ("non_svg", "SVG 가 아닌 요소(편집기 전용 등)"), ("editor_attr", "편집기 전용 속성")]
    for key, label in labels:
        if removed.get(key):
            notes.append(f"{label} {removed[key]}개를 제거했습니다.")

    width, height, defaulted = _svg_dimensions(root)
    if defaulted:
        notes.append(f"SVG 에 크기 표시가 없어 기본 크기({SVG_DEFAULT_SIZE[0]}×{SVG_DEFAULT_SIZE[1]})로 보았습니다.")
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    try:
        out = ET.tostring(root, encoding="unicode").encode("utf-8")
    except (RecursionError, ValueError):
        return _rej("svg_invalid", "SVG 구조가 너무 복잡해 읽을 수 없습니다.", "직렬화 실패")
    if len(out) > MAX_SVG_BYTES:
        return _rej("too_large", "정화한 SVG 가 1MB 를 넘습니다.", f"{len(out)} bytes")
    return out, notes, {"width": width, "height": height, "unread_shapes": unread, "embedded_images": embedded}


# 정화 결과를 확인하는 '다른 방식' 의 검사: 트리가 아니라 글자에서 찾는다 (정화 코드와 같은 맹점을 공유하지 않으려는 목적)
_HAZARDS: list[tuple[str, re.Pattern]] = [
    ("script", re.compile(r"<\s*script", re.I)),
    ("foreignObject", re.compile(r"<\s*foreignObject", re.I)),
    ("event", re.compile(r"\son[a-z]+\s*=", re.I)),
    ("javascript:", re.compile(r"(?:java|vb)script\s*:", re.I)),
    ("external-href", re.compile(r"""(?:xlink:)?href\s*=\s*(?:"(?!\s*#|\s*data:image/)|'(?!\s*#|\s*data:image/)|(?!["'\s])(?!#|data:image/))""", re.I)),
    ("external-url", re.compile(r"url\(\s*['\"]?\s*(?!#)", re.I)),
    ("import", re.compile(r"@import", re.I)),
    ("doctype", re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.I)),
]


def svg_hazards(svg_text: str) -> list[str]:
    """SVG 글자에 위험 요소가 남아 있는지 따로 훑어 이름을 돌려준다. 빈 목록이면 남은 것이 없다. (정화 성공 여부를 평가·테스트에서 확인하는 데 쓴다)"""
    return [name for name, pat in _HAZARDS if pat.search(svg_text)]


# ---------------------------------------------------------------------------
# 입구
# ---------------------------------------------------------------------------
def _ext_of(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def load_image(data: bytes, name: str = "", *, claimed_mime: Optional[str] = None) -> Union[ImageAsset, ImageRejection]:
    """올린 바이트를 검사해 받아들이면 ImageAsset, 아니면 ImageRejection(쉬운 말 사유) 을 돌려준다. 예외를 던지지 않는다.

    name 이 비어 있으면 이름(확장자) 검사를 건너뛴다. 이름에 확장자가 있는데 판별한 종류와 다르면 거부한다.
    claimed_mime 은 업로드 요청이 주장한 Content-Type — 판별한 종류와 다르면 거부한다.
    """
    try:
        return _load(data, name, claimed_mime)
    except _Corrupt as e:
        return _rej("corrupt", _CORRUPT_MSG, str(e))
    except (RecursionError, MemoryError, OverflowError, struct.error, IndexError, ValueError) as e:   # 예상 밖 입력도 거부로 닫는다 (fail-closed)
        return _rej("corrupt", _CORRUPT_MSG, f"{type(e).__name__}: {e}")


def _load(data: bytes, name: str, claimed_mime: Optional[str]) -> Union[ImageAsset, ImageRejection]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return _rej("unsupported", "이미지 데이터를 읽을 수 없습니다.", f"type={type(data).__name__}")
    data = bytes(data)
    if not data:
        return _rej("empty", "빈 파일입니다. 이미지를 다시 올려 주세요.")
    kind = sniff_kind(data)
    if kind is None:
        return _rej("unsupported", "지원하지 않는 형식입니다. PNG · JPEG · GIF · WEBP · SVG 만 올릴 수 있습니다.", f"앞 바이트 {data[:8]!r}")
    limit = MAX_SVG_BYTES if kind == "svg" else MAX_RASTER_BYTES
    if len(data) > limit:
        return _rej("too_large", f"파일이 너무 큽니다 ({len(data) / 1048576:.1f}MB). {KIND_LABEL[kind]} 는 {limit // 1048576}MB 까지 올릴 수 있습니다.", f"{len(data)} bytes > {limit}")
    ext = _ext_of(name)
    if ext and ext not in _EXTENSIONS[kind]:
        return _rej("extension_mismatch", f"파일 이름의 종류(.{ext[:8]})와 실제 파일 내용({KIND_LABEL[kind]})이 달라 받을 수 없습니다. 올바른 확장자로 저장해 다시 올려 주세요.",
                    f"확장자 .{ext} vs 판별 {kind}")
    if claimed_mime:
        cm = _MIME_ALIASES.get(claimed_mime.split(";")[0].strip().lower(), claimed_mime.split(";")[0].strip().lower())
        if cm and cm != MIME_BY_KIND[kind]:
            return _rej("type_mismatch", f"업로드가 알려 준 파일 종류({cm[:30]})와 실제 파일 내용({KIND_LABEL[kind]})이 달라 받을 수 없습니다.", f"Content-Type {cm} vs 판별 {kind}")

    sha = hashlib.sha256(data).hexdigest()
    shown = safe_name(name)
    if kind == "svg":
        res = sanitize_svg(data)
        if isinstance(res, ImageRejection):
            return res
        clean, notes, info = res
        w, h = info["width"], info["height"]
        if w > MAX_SIDE_PX or h > MAX_SIDE_PX or w * h > MAX_PIXELS:
            return _rej("too_many_pixels", f"SVG 의 그림 크기({w}×{h})가 너무 큽니다.", f"{w}x{h}")
        return ImageAsset(sha256=sha, mime=MIME_BY_KIND["svg"], width=w, height=h, bytes_len=len(data), name=shown, kind="svg", data=clean, fmt="svg",
                          sanitized_notes=tuple(notes), svg_unread_shapes=info["unread_shapes"], svg_embedded_images=info["embedded_images"])

    parser = {"png": _parse_png, "jpeg": _parse_jpeg, "gif": _parse_gif, "webp": _parse_webp}[kind]
    w, h, end = parser(data)
    if w > MAX_SIDE_PX or h > MAX_SIDE_PX or w * h > MAX_PIXELS:
        return _rej("too_many_pixels", f"이미지의 가로세로({w}×{h})가 너무 큽니다. 가로세로 각 {MAX_SIDE_PX:,}픽셀, 전체 {MAX_PIXELS // 1_000_000}백만 픽셀까지 받습니다.",
                    f"{w}x{h}={w * h} px (압축 폭탄 방지 상한)")
    why = _polyglot_reason(data, kind, end)
    if why:
        return _rej("polyglot", "이미지 파일에 이미지가 아닌 다른 데이터가 함께 들어 있어 받을 수 없습니다.", why)
    return ImageAsset(sha256=sha, mime=MIME_BY_KIND[kind], width=w, height=h, bytes_len=len(data), name=shown, kind="raster", data=data, fmt=kind)


def to_image_input(asset: ImageAsset) -> ImageInput:
    """모델에 보낼 이미지. 래스터만 보낸다 — SVG 는 글자를 코드로 직접 읽을 수 있으므로 모델에 보내지 않는다.
    이름은 사용자의 파일 이름 대신 해시로 만든다 (처리 기록에 파일 이름이 남지 않게)."""
    if asset.kind != "raster":
        raise ValueError("SVG 는 모델에 보내지 않습니다 (svg_scene 으로 글자를 직접 읽는다)")
    return ImageInput(data=asset.data, mime=asset.mime, name=f"upload-{asset.short_hash[:8]}.{asset.fmt}")


# ---------------------------------------------------------------------------
# SVG 장면 읽기 (글자·네모) — 모델 없이 코드로 읽는다
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SvgText:
    order: int
    text: str
    kind: str                   # "text"(화면에 그려지는 글) | "meta"(title·desc 등 보이지 않지만 읽히는 글)
    x: Optional[float] = None
    y: Optional[float] = None
    fill: Optional[str] = None
    font_px: Optional[float] = None
    anchor: str = "start"
    transformed: bool = False   # 변형(transform)이 걸려 있어 좌표가 화면 좌표와 같지 않다


@dataclass(frozen=True)
class SvgRect:
    order: int
    x: float
    y: float
    w: float
    h: float
    fill: str


@dataclass
class SvgScene:
    width: int
    height: int
    texts: list = field(default_factory=list)
    rects: list = field(default_factory=list)


_NUM = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")


def _first_number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    m = _NUM.search(value)
    return float(m.group(0)) if m else None


def _style_value(style: str, prop: str) -> Optional[str]:
    for part in style.split(";"):
        k, _, v = part.partition(":")
        if k.strip().lower() == prop:
            return v.strip()
    return None


def _prop(el: ET.Element, name: str) -> Optional[str]:
    st = el.get("style")
    if st:
        v = _style_value(st, name)
        if v:
            return v
    return el.get(name)


def _text_content(el: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def svg_scene(asset: ImageAsset) -> SvgScene:
    """정화한 SVG 에서 글자 요소와 단색 네모를 문서 순서대로 읽는다. 래스터면 빈 장면. 글자는 title·desc 도 포함한다 — 화면엔 안 보여도 읽히는 글이다."""
    scene = SvgScene(width=asset.width, height=asset.height)
    if asset.kind != "svg":
        return scene
    try:
        root = ET.fromstring(asset.data)
    except ET.ParseError:
        return scene
    order = 0
    stack: list[tuple[ET.Element, dict, bool]] = [(root, {}, False)]
    # 깊이 우선·문서 순서: 스택에 거꾸로 넣는다
    while stack:
        el, inh, xf = stack.pop()
        order += 1
        ns, name = _local(el.tag) if isinstance(el.tag, str) else ("", "")
        lname = name.lower()
        cur = dict(inh)
        fill = _prop(el, "fill")
        if fill and lname not in ("rect",):
            cur["fill"] = fill
        fs = _prop(el, "font-size")
        if fs:
            v = _svg_length(fs)
            if v:
                cur["font_px"] = v
        anc = _prop(el, "text-anchor")
        if anc:
            cur["anchor"] = anc
        has_xf = xf or bool(el.get("transform"))
        if lname == "text":
            txt = _text_content(el)
            if txt:
                x = _first_number(el.get("x"))
                y = _first_number(el.get("y"))
                if x is None or y is None:
                    for sub in el.iter():
                        if sub is not el:
                            x = x if x is not None else _first_number(sub.get("x"))
                            y = y if y is not None else _first_number(sub.get("y"))
                            break
                scene.texts.append(SvgText(order=order, text=txt, kind="text", x=x, y=y, fill=cur.get("fill"), font_px=cur.get("font_px"),
                                           anchor=cur.get("anchor", "start"), transformed=has_xf))
            continue
        if lname in ("title", "desc"):
            txt = _text_content(el)
            if txt:
                scene.texts.append(SvgText(order=order, text=txt, kind="meta"))
            continue
        if lname == "rect" and not has_xf:
            f = fill or cur.get("fill")
            x, y = _first_number(el.get("x")) or 0.0, _first_number(el.get("y")) or 0.0
            w, h = _svg_length(el.get("width")), _svg_length(el.get("height"))
            if f and w and h:
                scene.rects.append(SvgRect(order=order, x=x, y=y, w=w, h=h, fill=f.strip()))
        for ch in reversed(list(el)):
            if isinstance(ch.tag, str):
                stack.append((ch, cur, has_xf))
    return scene
