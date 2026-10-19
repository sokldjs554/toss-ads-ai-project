"""이미지 검사용 픽스처를 코드로 만든다 (PIL 없이 표준 라이브러리만). 같은 코드는 항상 같은 바이트를 낸다.

    python evals/datasets/images/make_fixtures.py          # 이 폴더에 결과 파일을 쓴다

- 정상 파일(ok_*)은 실제로 디코딩되는 작은 그림이다 (PNG 그라데이션 · 회색 JPEG · 단색 GIF · 1×1 WEBP).
- 폭탄(bomb_*)은 머리글이 거대한 크기를 주장하는 파일이다. 디코딩하지 않고 머리글만 읽는 검사를 시험하려는 것이다.
- 위험 SVG(svg_*)는 스크립트·엔티티 폭탄·바깥 주소 같은 구조를 일부러 넣은 글이다. 실행하지 않는다.
- 5MB 를 넘는 파일(oversize_png)은 저장하지 않고 필요할 때 만든다.
모든 파일은 이 프로젝트가 합성한 시험용이며 실제 광고 이미지가 아니다.
"""
from __future__ import annotations

import base64
import struct
import sys
import zlib
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- PNG
def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def png_bytes(w: int = 64, h: int = 48, *, gray: bool = False) -> bytes:
    """w×h 그라데이션 PNG (RGB 8비트, 또는 gray=True 면 회색 8비트)."""
    rows = []
    for y in range(h):
        if gray:
            row = bytes((x * 255 // max(1, w - 1)) for x in range(w))
        else:
            row = b"".join(bytes((x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 160)) for x in range(w))
        rows.append(b"\x00" + row)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0 if gray else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _chunk(b"IEND", b"")


def png_header_only(w: int, h: int) -> bytes:
    """머리글이 w×h 를 주장하지만 그림 데이터는 한 줄뿐인 PNG (디코딩하지 않고 머리글만 읽는 검사 시험용)."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"\x00" * 16, 9)) + _chunk(b"IEND", b"")


def png_real_bomb(side: int = 8192) -> bytes:
    """진짜 압축 폭탄: side×side 회색 PNG 가 전부 0 이라 몇십 KB 로 줄어든다. 디코딩하면 side² 바이트가 된다."""
    comp = zlib.compressobj(9)
    out = [comp.compress((b"\x00" + b"\x00" * side)) for _ in range(side)]
    out.append(comp.flush())
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", b"".join(out)) + _chunk(b"IEND", b"")


def oversize_png(extra: int = 5 * 1024 * 1024 + 1) -> bytes:
    """5MB 를 넘는 PNG: 유효한 작은 PNG 의 보조 덩어리(tEXt)를 크게 부풀렸다."""
    base = png_bytes(8, 8)
    text = _chunk(b"tEXt", b"Comment\x00" + b"A" * extra)
    return base[:-12] + text + base[-12:]


# --------------------------------------------------------------------------- JPEG
def _bits_to_bytes(bits: str) -> bytes:
    bits += "1" * (-len(bits) % 8)
    raw = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    return raw.replace(b"\xff", b"\xff\x00")


def jpeg_gray_bytes(w: int = 64, h: int = 48, gray: int = 200) -> bytes:
    """단색 회색 JPEG (기본 순차, 8비트, 1성분, 맞춤 허프만 표). 8×8 블록마다 DC 만 있다."""
    dc = round((gray - 128) * 8)                      # 양자화 표가 전부 1 이므로 DC 계수 = 8 × (밝기 − 128)
    cat = abs(dc).bit_length()
    dcbits = format(dc if dc >= 0 else dc + (1 << cat) - 1, f"0{cat}b") if cat else ""
    nblocks = ((w + 7) // 8) * ((h + 7) // 8)
    # 허프만 표: DC {0: '0', cat: '10'}, AC {EOB: '0'}
    bits = "10" + dcbits + "0"
    bits += "00" * (nblocks - 1)
    scan = _bits_to_bytes(bits)

    def seg(marker: int, body: bytes) -> bytes:
        return bytes((0xFF, marker)) + struct.pack(">H", len(body) + 2) + body

    dqt = seg(0xDB, b"\x00" + b"\x01" * 64)
    sof = seg(0xC0, struct.pack(">BHHB", 8, h, w, 1) + b"\x01\x11\x00")
    dht_dc = seg(0xC4, b"\x00" + bytes([1, 1] + [0] * 14) + bytes([0, cat]))
    dht_ac = seg(0xC4, b"\x10" + bytes([1] + [0] * 15) + bytes([0]))
    sos = seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    return b"\xff\xd8" + dqt + sof + dht_dc + dht_ac + sos + scan + b"\xff\xd9"


def jpeg_header_only(w: int, h: int) -> bytes:
    """크기만 주장하는 JPEG (그림 데이터는 비어 있다 — 머리글 검사 시험용)."""
    def seg(marker: int, body: bytes) -> bytes:
        return bytes((0xFF, marker)) + struct.pack(">H", len(body) + 2) + body

    sof = seg(0xC0, struct.pack(">BHHB", 8, h, w, 1) + b"\x01\x11\x00")
    sos = seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    return b"\xff\xd8" + seg(0xDB, b"\x00" + b"\x01" * 64) + sof + sos + b"\x00" + b"\xff\xd9"


def jpeg_with_comment(comment: bytes) -> bytes:
    """정상 JPEG 에 COM 구간(주석)으로 임의 바이트를 넣는다 — 구조는 맞지만 안에 스크립트가 숨어 있는 polyglot 시험용."""
    base = jpeg_gray_bytes(16, 16)
    com = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    return base[:2] + com + base[2:]


# --------------------------------------------------------------------------- GIF
def gif_bytes(w: int = 32, h: int = 24, index: int = 1) -> bytes:
    """단색 GIF (색 4개짜리 표, 압축하지 않는 LZW: 두 픽셀마다 clear 코드를 넣어 코드 길이가 늘지 않게 한다)."""
    palette = bytes([255, 255, 255, 43, 91, 215, 25, 31, 40, 0, 0, 0])
    codes: list[int] = []
    clear, eoi = 4, 5
    n = w * h
    for i in range(0, n, 2):
        codes += [clear, index] + ([index] if i + 1 < n else [])
    codes.append(eoi)
    bits = "".join(format(c, "03b")[::-1] for c in codes)        # GIF 는 낮은 비트부터 채운다
    bits += "0" * (-len(bits) % 8)
    data = bytes(int(bits[i:i + 8][::-1], 2) for i in range(0, len(bits), 8))
    subs = b"".join(bytes([len(data[i:i + 255])]) + data[i:i + 255] for i in range(0, len(data), 255))
    return (b"GIF89a" + struct.pack("<HHBBB", w, h, 0xF1, 0, 0) + palette + b"\x2c" + struct.pack("<HHHHB", 0, 0, w, h, 0) + b"\x02" + subs + b"\x00" + b"\x3b")


# --------------------------------------------------------------------------- WEBP
_WEBP_1X1 = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")   # 널리 쓰이는 1×1 무손실 WEBP (VP8L)


def webp_bytes() -> bytes:
    return _WEBP_1X1


def webp_header_only(w: int, h: int) -> bytes:
    """VP8L 머리글이 w×h 를 주장하는 WEBP (그림 데이터는 비어 있다 — 머리글 검사 시험용)."""
    bits = (w - 1) | ((h - 1) << 14)
    body = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 3
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


# --------------------------------------------------------------------------- SVG (글)
SVG_OK = ('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="314" viewBox="0 0 600 314">'
          '<title>생활비 통장 광고</title><rect width="600" height="314" fill="#FFFFFF"/>'
          '<text x="40" y="100" font-size="44" font-weight="700" fill="#191F28">월 3만원부터 시작하는 통장</text>'
          '<text x="40" y="170" font-size="24" fill="#4E5968">자동이체로 편하게 모아 보세요</text>'
          '<rect x="40" y="220" width="180" height="56" rx="12" fill="#2B5BD7"/>'
          '<text x="130" y="256" font-size="24" font-weight="700" fill="#FFFFFF" text-anchor="middle">지금 확인</text></svg>')
SVG_GUARANTEE = SVG_OK.replace("월 3만원부터 시작하는 통장", "원금 보장 통장").replace("생활비 통장 광고", "광고")
SVG_SCRIPT = SVG_OK.replace("</svg>", '<script>alert(document.cookie)</script></svg>')
SVG_FOREIGN = SVG_OK.replace("</svg>", '<foreignObject width="10" height="10"><div xmlns="http://www.w3.org/1999/xhtml"><script>x()</script></div></foreignObject></svg>')
SVG_ONLOAD = SVG_OK.replace('<svg ', '<svg onload="alert(1)" ', 1).replace('<rect width="600"', '<rect onclick="steal()" width="600"', 1)
SVG_JS_HREF = SVG_OK.replace("</svg>", '<a xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="javascript:alert(1)"><text x="1" y="1">x</text></a></svg>')
SVG_EXTERNAL = SVG_OK.replace("</svg>", '<image href="http://evil.example/pixel.png" width="1" height="1"/><use href="http://evil.example/a.svg#x"/></svg>')
SVG_IMPORT = SVG_OK.replace("<title>", '<style>@import url(http://evil.example/x.css); .t{fill:url(http://evil.example/p)}</style><title>', 1)
SVG_ENTITY_BOMB = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
                   '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
                   '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text x="1" y="9">&lol3;</text></svg>')
SVG_XXE = ('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
           '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text x="1" y="9">&xxe;</text></svg>')
SVG_EMBEDDED_RASTER = SVG_OK.replace("</svg>", '<image width="10" height="10" href="data:image/png;base64,' + base64.b64encode(png_bytes(4, 4)).decode() + '"/></svg>')
SVG_OUTLINED = ('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="314"><rect width="600" height="314" fill="#FFFFFF"/>'
                '<path d="M40 100 L60 40 L80 100 Z M100 40 L100 100 L140 100 Z" fill="#191F28"/></svg>')
SVG_HIDDEN_DESC = SVG_OK.replace("<title>생활비 통장 광고</title>", "<title>생활비 통장 광고</title><desc>누구나 100% 승인 원금 보장</desc>")
SVG_BROKEN = '<svg xmlns="http://www.w3.org/2000/svg"><text>닫히지 않음</svg>'
SVG_NOT_SVG_ROOT = '<html xmlns="http://www.w3.org/1999/xhtml"><svg xmlns="http://www.w3.org/2000/svg"/></html>'


# --------------------------------------------------------------------------- 목록
def _flip_png_crc() -> bytes:
    b = bytearray(png_bytes())
    b[29] ^= 0xFF                                    # IHDR CRC 의 한 바이트
    return bytes(b)


def _png_trailing_html() -> bytes:
    return png_bytes() + b"<html><script>alert(document.cookie)</script></html>"


def _png_inner_svg() -> bytes:
    """IEND 앞 보조 덩어리에 SVG 스크립트를 숨긴 PNG — 구조는 맞지만 안에 다른 형식 표지가 있다."""
    base = png_bytes()
    return base[:-12] + _chunk(b"tEXt", b"Comment\x00<svg onload=alert(1)>") + base[-12:]


def _gif_zip() -> bytes:
    return gif_bytes() + b"PK\x03\x04" + b"\x00" * 26


def _jpeg_missing_eoi() -> bytes:
    return jpeg_gray_bytes()[:-2]


# --- holdout 사례용 픽스처: 검사 코드를 고정한 뒤에 추가했다 (코드로 만든 시험 자료일 뿐, 검사 로직을 바꾸지 않는다)
def _gif_php() -> bytes:
    return gif_bytes(32, 24) + b"<?php system($_GET['c']); ?>"


def _jpeg_zip() -> bytes:
    return jpeg_gray_bytes(32, 24) + b"PK\x03\x04" + b"\x00" * 26


FIXTURES: dict[str, Callable[[], bytes]] = {
    "ok_png.png": lambda: png_bytes(64, 48),
    "ok_banner_1200x628.png": lambda: png_bytes(1200, 628, gray=True),
    "ok_jpeg.jpg": lambda: jpeg_gray_bytes(64, 48),
    "ok_gif.gif": lambda: gif_bytes(32, 24),
    "ok_webp.webp": webp_bytes,
    "ok_svg.svg": lambda: SVG_OK.encode(),
    "corrupt_png_crc.png": _flip_png_crc,
    "truncated_png.png": lambda: png_bytes()[:60],
    "corrupt_jpeg_no_eoi.jpg": _jpeg_missing_eoi,
    "polyglot_png_html.png": _png_trailing_html,
    "polyglot_png_svgchunk.png": _png_inner_svg,
    "polyglot_gif_zip.gif": _gif_zip,
    "polyglot_jpeg_comment_script.jpg": lambda: jpeg_with_comment(b"<script>alert(1)</script>"),
    "bomb_png_8192.png": lambda: png_real_bomb(8192),
    "bomb_png_header_30000.png": lambda: png_header_only(30000, 30000),
    "wide_png_9000.png": lambda: png_header_only(9000, 10),
    "bomb_jpeg_header_8000.jpg": lambda: jpeg_header_only(8000, 8000),
    "bomb_webp_header_16384.webp": lambda: webp_header_only(16384, 16384),
    "svg_guarantee.svg": lambda: SVG_GUARANTEE.encode(),
    "svg_script.svg": lambda: SVG_SCRIPT.encode(),
    "svg_foreignobject.svg": lambda: SVG_FOREIGN.encode(),
    "svg_onload.svg": lambda: SVG_ONLOAD.encode(),
    "svg_javascript_href.svg": lambda: SVG_JS_HREF.encode(),
    "svg_external_href.svg": lambda: SVG_EXTERNAL.encode(),
    "svg_import_css.svg": lambda: SVG_IMPORT.encode(),
    "svg_entity_bomb.svg": lambda: SVG_ENTITY_BOMB.encode(),
    "svg_xxe.svg": lambda: SVG_XXE.encode(),
    "svg_embedded_raster.svg": lambda: SVG_EMBEDDED_RASTER.encode(),
    "svg_outlined_text.svg": lambda: SVG_OUTLINED.encode(),
    "svg_hidden_desc.svg": lambda: SVG_HIDDEN_DESC.encode(),
    "svg_broken.svg": lambda: SVG_BROKEN.encode(),
    "svg_html_root.svg": lambda: SVG_NOT_SVG_ROOT.encode(),
    # holdout 용 (동결 뒤 추가)
    "ok_jpeg_1200x628.jpg": lambda: jpeg_gray_bytes(1200, 628),
    "ok_gif_400x300.gif": lambda: gif_bytes(400, 300),
    "jpeg_header_5000x5000.jpg": lambda: jpeg_header_only(5000, 5000),
    "png_header_6000x6000.png": lambda: png_header_only(6000, 6000),
    "png_header_8192x8192.png": lambda: png_header_only(8192, 8192),
    "polyglot_gif_php.gif": _gif_php,
    "webp_trailing_bytes.webp": lambda: webp_bytes() + b"junk",
    "polyglot_jpeg_zip.jpg": _jpeg_zip,
}
# 저장하지 않고 필요할 때만 만드는 큰 파일
GENERATED_ONLY: dict[str, Callable[[], bytes]] = {
    "oversize_png.png": oversize_png,
    "oversize_svg.svg": lambda: ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><desc>' + "A" * (1024 * 1024 + 10) + "</desc></svg>").encode(),
    "empty.png": lambda: b"",
    "text_file.png": lambda: b"hello, this is not an image",
}


def build(name: str) -> bytes:
    """이름으로 픽스처 바이트를 만든다 (저장된 파일을 읽지 않는다)."""
    if name in FIXTURES:
        return FIXTURES[name]()
    return GENERATED_ONLY[name]()


def load(name: str) -> bytes:
    """저장된 파일이 있으면 그것을, 없으면 만든다."""
    p = HERE / name
    return p.read_bytes() if (name in FIXTURES and p.exists()) else build(name)


def write_all(directory: Path = HERE) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fn in FIXTURES.items():
        (directory / name).write_bytes(fn())
        written.append(name)
    return written


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    names = write_all(out)
    print(f"{len(names)}개 파일을 {out} 에 썼습니다")
