"""배너 소재 구성과 읽기 쉬움 점수 — 이미지 생성 모델 없이 광고 문구(AdCopy)만으로 SVG 배너를 만든다.

- compose(): 크기 × 템플릿 × 팔레트를 정해 글을 배치한다. 글자 폭은 글꼴 파일 없이 **추정**한다(한글·한자 1em, 숫자 0.58em, 라틴 0.55em,
  공백 0.3em, 기호 0.5em) — 실제 글꼴로 그리면 줄바꿈이 달라질 수 있다. 줄바꿈은 어절 우선, 한 어절이 한 줄보다 길면 글자 단위로 자른다.
  글자 크기는 max→min 으로 줄여 맞추고, **최소 크기에서도 안 들어가면 글을 자르지 않고 `fit_violation`(넘침)으로 표시한다.**
- score(): 글자 대비(WCAG 2.x 상대 휘도), 최소 글자 크기, 안전 여백, 글자 면적 비율, 버튼, 줄 수, 넘침, 고지 표시를 각각 0~1 로 점수 매기고 가중 평균한다.
  넘침이나 대비 미달이 있으면 총점에 상한을 걸어 **좋은 점수가 나쁜 가독성을 가리지 못하게** 한다.
- recommend(): 조합을 점수순으로 늘어놓고 서로 너무 비슷한 것은 뺀다. 결정론.

이 점수가 의미하지 않는 것: 실제 광고 성과(클릭률)나 플랫폼 심사 통과 여부가 아니다. 읽기 쉬운 정도를 이 프로젝트가 정한 기준으로 센 값일 뿐이다.
배너의 글은 광고 문구 그대로이므로 배너 심사는 곧 문구 심사다 — 이 모듈은 문구를 바꾸지 않는다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional, Sequence, Union
from xml.sax.saxutils import escape as _xml_escape

from core.schemas import AdCopy

# ---------------------------------------------------------------------------
# 이 프로젝트가 가정한 값 (실제 광고 콘솔의 규격·기준과 다를 수 있다)
# ---------------------------------------------------------------------------
FONT_FAMILY = "Pretendard, 'Noto Sans KR', 'Malgun Gothic', sans-serif"


@dataclass(frozen=True)
class BannerSize:
    key: str
    label: str
    width: int
    height: int

    @property
    def ratio(self) -> float:
        return self.width / self.height


# 가정한 규격이다. 실제 토스애즈 콘솔의 배너 규격과 다를 수 있다(공개 자료로 확인하지 못했다) — 값은 이 프로젝트가 정한 시험용이다.
ASSUMED_BANNER_SIZES: dict[str, BannerSize] = {
    "landscape": BannerSize("landscape", "가로형", 1200, 628),
    "square": BannerSize("square", "정사각", 1080, 1080),
    "strip": BannerSize("strip", "띠형", 1200, 300),
}

# 가정한 가독성 기준. 폭 1200px 배너에서 본문 글자의 최소 크기이며, 다른 폭에서는 폭 비율로 환산한다.
ASSUMED_MIN_FONT_PX = 28
ASSUMED_REF_WIDTH_PX = 1200
ROLE_MIN_FACTOR = {"title": 1.4, "body": 1.0, "button": 1.0, "disclosure": 0.8}   # 역할별 최소 글자 크기 = 본문 최소 × 계수
COMFORT_FACTOR = 1.3                      # 최소의 1.3배 이상이면 '넉넉한 크기' 로 본다
ASSUMED_SAFE_MARGIN_RATIO = 0.05          # 안전 여백: min(폭, 높이의 2배)의 5% (사방 같은 픽셀 — 띠형처럼 낮은 배너는 높이를 기준으로 줄인다). 기기·지면이 가장자리를 가릴 수 있다고 가정
ASSUMED_MIN_BUTTON_H_PX = 40              # 폭 1200px 기준 버튼 최소 높이
ASSUMED_MIN_BUTTON_W_PX = 110
AA_RATIO = 4.5                            # WCAG 2.x AA 일반 글자 기준. 이 프로젝트는 제목·버튼을 포함해 모든 글자에 4.5:1 을 요구한다
LINE_RATIO = {"title": 1.25, "body": 1.45, "button": 1.2, "disclosure": 1.4}
WEIGHT = {"title": 800, "body": 400, "button": 700, "disclosure": 400}
FIT_SLACK = 0.96                          # 폭 추정 오차를 감안해 줄 폭의 96% 만 쓴다
SEARCH_STEPS = 21                         # 크기 탐색 단계(max→min)
DISCLOSURE_MAX_LINES = 3
DISCLOSURE_SHARE = 0.28                   # 고지에 쓰는 높이의 목표 상한(영역 높이 대비)

# 점수 가중치(합 1.0 이 아니어도 된다 — 해당 없는 항목은 빼고 다시 나눈다)
SCORE_WEIGHTS: dict[str, float] = {
    "fit": 0.22, "contrast_title": 0.12, "contrast_body": 0.12, "contrast_button": 0.08, "min_font": 0.14,
    "safe_margin": 0.08, "density": 0.08, "button": 0.06, "lines": 0.10, "disclosure": 0.10,
}
# 총점 상한 — 이 문제가 있으면 다른 항목이 아무리 좋아도 이 값을 넘지 못한다
CAP_FIT_VIOLATION = 0.40
CAP_LOW_CONTRAST = 0.50
CAP_TINY_FONT = 0.60
CAP_DISCLOSURE_HIDDEN = 0.70


# ---------------------------------------------------------------------------
# 색: WCAG 2.x 상대 휘도·대비
# ---------------------------------------------------------------------------
_NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
          "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192), "navy": (0, 0, 128), "orange": (255, 165, 0),
          "purple": (128, 0, 128), "lime": (0, 255, 0), "teal": (0, 128, 128), "maroon": (128, 0, 0), "aqua": (0, 255, 255), "fuchsia": (255, 0, 255)}


def parse_color(value: str) -> Optional[tuple[int, int, int]]:
    """'#rgb' · '#rrggbb' · 'rgb(r,g,b)' · 기본 색 이름 → (r,g,b). 해석할 수 없으면 None (url(#grad)·currentColor·none 등)."""
    v = (value or "").strip().lower()
    if v in _NAMED:
        return _NAMED[v]
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))  # type: ignore[return-value]
    m = re.fullmatch(r"#([0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", v)
    if m:
        r, g, b = (int(x) for x in m.groups())
        if max(r, g, b) <= 255:
            return r, g, b
    return None


def relative_luminance(color: Union[str, tuple[int, int, int]]) -> float:
    """WCAG 2.x 상대 휘도: sRGB 각 채널을 선형화한 뒤 0.2126 R + 0.7152 G + 0.0722 B."""
    rgb = parse_color(color) if isinstance(color, str) else color
    if rgb is None:
        raise ValueError(f"해석할 수 없는 색: {color!r}")

    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(fg: Union[str, tuple[int, int, int]], bg: Union[str, tuple[int, int, int]]) -> float:
    """두 색의 WCAG 대비: (밝은 쪽 휘도 + 0.05) / (어두운 쪽 휘도 + 0.05). 1:1 ~ 21:1."""
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = (a, b) if a >= b else (b, a)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# 팔레트 · 템플릿
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Palette:
    key: str
    label: str
    bg: str
    title: str
    body: str
    accent: str            # 버튼 바탕(또는 분할 템플릿의 패널색)
    button_text: str
    disclosure: str


PALETTES: dict[str, Palette] = {p.key: p for p in (
    Palette("paper", "흰 바탕 · 파랑", "#FFFFFF", "#191F28", "#4E5968", "#2B5BD7", "#FFFFFF", "#4E5968"),
    Palette("navy", "남색 바탕 · 노랑", "#0B1F4B", "#FFFFFF", "#D6E0F5", "#FFD54A", "#0B1F4B", "#C3D0EE"),
    Palette("mint", "연한 초록 · 진초록", "#E6F4EC", "#0B3D24", "#1F5C3B", "#0B6B37", "#FFFFFF", "#2F5D45"),
    Palette("sand", "따뜻한 베이지 · 갈색", "#F6EFE6", "#2B2118", "#5A4A3A", "#8A4B00", "#FFFFFF", "#5A4A3A"),
    Palette("ink", "어두운 바탕 · 하늘", "#191F28", "#FFFFFF", "#E5E8EB", "#7FB0FF", "#191F28", "#C9CFD6"),
)}

TEMPLATES: dict[str, str] = {"left": "왼쪽 정렬", "center": "가운데 정렬", "split": "좌우(상하) 분할"}


def palette_report(p: Palette) -> dict[str, float]:
    """팔레트가 약속한 글자 대비(전부 4.5:1 이상이어야 한다)를 코드로 계산한다."""
    return {"title": contrast_ratio(p.title, p.bg), "body": contrast_ratio(p.body, p.bg), "disclosure": contrast_ratio(p.disclosure, p.bg),
            "button": contrast_ratio(p.button_text, p.accent), "button_inverse": contrast_ratio(p.accent, p.bg)}


def verify_palettes(palettes: Optional[Iterable[Palette]] = None) -> list[str]:
    """기준(4.5:1)에 못 미치는 팔레트 항목을 문장으로 돌려준다. 빈 목록이면 모두 통과."""
    bad: list[str] = []
    for p in palettes if palettes is not None else PALETTES.values():
        for k, v in palette_report(p).items():
            if v < AA_RATIO:
                bad.append(f"{p.key}.{k} = {v:.2f}:1")
    return bad


# ---------------------------------------------------------------------------
# 글자 폭 추정 · 줄바꿈
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8192)
def char_em(ch: str) -> float:
    """글꼴 없이 추정한 글자 폭(em). 한글·한자·전각 1.0 / 숫자 0.58 / 라틴 등 문자 0.55 / 공백 0.3 / 그 밖의 기호 0.5."""
    if ch in " \t 　":
        return 0.3
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf", "Cc"):
        return 0.0
    o = ord(ch)
    if (0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F or 0xA960 <= o <= 0xA97F or 0xD7B0 <= o <= 0xD7FF
            or 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF):
        return 1.0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 1.0
    if ch.isdigit():
        return 0.58
    if ch.isalpha():
        return 0.55
    return 0.5


def estimate_width(text: str, font_px: float) -> float:
    return sum(char_em(c) for c in text) * font_px


_NO_LINE_START = set(".,!?)]}%·…:;、。，！？’”")


@dataclass(frozen=True)
class _Words:
    words: tuple
    ems: tuple          # 어절별 폭(em)


def _words(text: str) -> _Words:
    t = re.sub(r"\s+", " ", text or "").strip()
    ws = tuple(t.split(" ")) if t else ()
    return _Words(ws, tuple(sum(char_em(c) for c in w) for w in ws))


def wrap_words(w: _Words, font_px: float, width: float) -> list[str]:
    limit = width * FIT_SLACK
    space = char_em(" ") * font_px
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    for word, em in zip(w.words, w.ems):
        ww = em * font_px
        if not cur:
            if ww <= limit:
                cur, cur_w = word, ww
                continue
        elif cur_w + space + ww <= limit:
            cur, cur_w = cur + " " + word, cur_w + space + ww
            continue
        else:
            lines.append(cur)
            cur, cur_w = "", 0.0
            if ww <= limit:
                cur, cur_w = word, ww
                continue
        for ch in word:                         # 한 어절이 한 줄보다 길다 → 글자 단위로 자른다 (줄 머리에 구두점이 오지 않게)
            cw = char_em(ch) * font_px
            if cur and cur_w + cw > limit and ch not in _NO_LINE_START:
                lines.append(cur)
                cur, cur_w = ch, cw
            else:
                cur, cur_w = cur + ch, cur_w + cw
    if cur:
        lines.append(cur)
    return lines


def wrap_text(text: str, font_px: float, width: float) -> list[str]:
    """어절 우선 줄바꿈. 글을 자르거나 줄이지 않는다 — 합치면 원문(공백만 정리)과 같다."""
    return wrap_words(_words(text), font_px, width)


# ---------------------------------------------------------------------------
# 배너 구조
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TextBlock:
    role: str                 # title | body | button | disclosure
    text: str
    x: float                  # 상자 왼쪽 위 (버튼은 버튼 네모)
    y: float
    w: float
    h: float
    font_px: float
    lines: tuple
    fg: str = ""
    bg: str = ""              # 이 글자 바로 아래에 깔린 색
    anchor: str = "start"
    line_h: float = 0.0
    overflow: bool = False    # 최소 크기에서도 영역에 들어가지 않음 (조용히 자르지 않고 표시한다)


@dataclass(frozen=True)
class Banner:
    svg: str
    blocks: tuple
    meta: dict
    size: BannerSize
    template: str
    palette: Palette

    @property
    def fit_violation(self) -> bool:
        return bool(self.meta.get("fit_violation"))

    def block(self, role: str) -> Optional[TextBlock]:
        return next((b for b in self.blocks if b.role == role), None)


@dataclass(frozen=True)
class _Rect:
    x: float
    y: float
    w: float
    h: float


@dataclass
class _Layout:
    text: _Rect
    button: Optional[_Rect]
    align: str
    valign: str
    shapes: list = field(default_factory=list)
    inverse_button: bool = False


def _n(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _layout(template: str, size: BannerSize, m: float, p: Palette) -> _Layout:
    W, H = size.width, size.height
    iw, ih = W - 2 * m, H - 2 * m
    ratio = W / H
    kind = "strip" if ratio >= 2.4 else ("square" if ratio < 1.25 else "wide")
    bar = max(6.0, 0.012 * W)
    if template == "left":
        shapes = [f'<rect x="0" y="0" width="{_n(bar)}" height="{_n(H)}" fill="{p.accent}"/>']
        if kind == "wide":
            shapes.append(f'<circle cx="{_n(W * 0.86)}" cy="{_n(H * 0.5)}" r="{_n(H * 0.24)}" fill="{p.accent}" opacity="0.12"/>')
            return _Layout(_Rect(m, m, iw * 0.68, ih), None, "start", "top", shapes)
        if kind == "square":
            return _Layout(_Rect(m, m, iw, ih), None, "start", "middle", shapes)
        return _Layout(_Rect(m, m, iw * 0.66, ih), _Rect(m + iw * 0.70, m, iw * 0.30, ih), "start", "middle", shapes)
    if template == "center":
        shapes = [f'<rect x="0" y="{_n(H - bar)}" width="{_n(W)}" height="{_n(bar)}" fill="{p.accent}"/>']
        if kind == "strip":
            return _Layout(_Rect(m + iw * 0.04, m, iw * 0.92, ih), None, "middle", "middle", shapes)
        return _Layout(_Rect(m + iw * 0.08, m, iw * 0.84, ih), None, "middle", "middle", shapes)
    # split: 한쪽 면을 강조색 패널로 하고 그 위에 버튼(바탕색 버튼 + 강조색 글자)을 둔다
    if kind == "square":
        py = round(H * 0.70)
        shapes = [f'<rect x="0" y="{py}" width="{_n(W)}" height="{_n(H - py)}" fill="{p.accent}"/>']
        return _Layout(_Rect(m, m, iw, py - 1.4 * m), _Rect(m, py + 0.7 * m, iw, H - py - 1.4 * m), "start", "middle", shapes, True)
    px = round(W * (0.72 if kind == "strip" else 0.62))
    shapes = [f'<rect x="{px}" y="0" width="{_n(W - px)}" height="{_n(H)}" fill="{p.accent}"/>']
    return _Layout(_Rect(m, m, px - 1.6 * m, ih), _Rect(px + 0.7 * m, m, W - px - 1.4 * m, ih), "start", "middle", shapes, True)


def safe_margin_px(size: BannerSize) -> float:
    return max(8.0, ASSUMED_SAFE_MARGIN_RATIO * min(size.width, 2 * size.height))


def min_font_px(width: int, role: str = "body") -> float:
    """역할별 최소 글자 크기. ASSUMED_MIN_FONT_PX 를 배너 폭 비율로 환산한다."""
    return ASSUMED_MIN_FONT_PX * width / ASSUMED_REF_WIDTH_PX * ROLE_MIN_FACTOR[role]


def _clean(text: str) -> str:
    """XML 에 쓸 수 없는 제어 문자를 걷어내고 줄바꿈은 공백으로 한다."""
    text = "".join(c for c in (text or "") if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cs", "Cn"))
    return re.sub(r"\s+", " ", text).strip()


def _as_size(size: Union[str, BannerSize]) -> BannerSize:
    if isinstance(size, BannerSize):
        return size
    if size not in ASSUMED_BANNER_SIZES:
        raise ValueError(f"알 수 없는 크기: {size!r} (가능: {', '.join(ASSUMED_BANNER_SIZES)})")
    return ASSUMED_BANNER_SIZES[size]


def _as_palette(p: Union[str, Palette]) -> Palette:
    if isinstance(p, Palette):
        for role in ("bg", "title", "body", "accent", "button_text", "disclosure"):
            color = getattr(p, role)
            if parse_color(color) is None or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in color):
                raise ValueError(f"해석할 수 없는 팔레트 색({role}): {color!r}")
        return p
    if p not in PALETTES:
        raise ValueError(f"알 수 없는 팔레트: {p!r} (가능: {', '.join(PALETTES)})")
    return PALETTES[p]


def _check_template(t: str) -> str:
    if t not in TEMPLATES:
        raise ValueError(f"알 수 없는 템플릿: {t!r} (가능: {', '.join(TEMPLATES)})")
    return t


@dataclass
class _Measured:
    font: float
    lines: list
    line_h: float
    h: float
    w: float            # 가장 긴 줄의 추정 폭


def _measure(w: _Words, font: float, width: float, role: str) -> _Measured:
    lines = wrap_words(w, font, width)
    lh = font * LINE_RATIO[role]
    return _Measured(font, lines, lh, len(lines) * lh, max((estimate_width(ln, font) for ln in lines), default=0.0))


def _lerp(lo: float, hi: float, s: float) -> float:
    return lo + (hi - lo) * s


def compose(copy: AdCopy, *, size: Union[str, BannerSize] = "landscape", template: str = "left", palette: Union[str, Palette] = "paper",
            include_disclosures: bool = True) -> Banner:
    """광고 문구 → SVG 배너. 글은 문구 그대로(공백만 정리)이며, 들어가지 않으면 자르지 않고 넘침으로 표시한다."""
    size = _as_size(size)
    template = _check_template(template)
    pal = _as_palette(palette)
    W, H = size.width, size.height
    title_t, body_t, cta_t = _clean(copy.headline), _clean(copy.body), _clean(copy.cta)
    disc_t = " · ".join(d for d in (_clean(x) for x in copy.disclosures) if d) if include_disclosures else ""
    if not (title_t or body_t or cta_t or disc_t):
        raise ValueError("배너에 넣을 글이 없습니다")
    m = safe_margin_px(size)
    lay = _layout(template, size, m, pal)
    ref = min(W, 2 * H)
    mins = {r: min_font_px(W, r) for r in ROLE_MIN_FACTOR}
    maxs = {"title": max(mins["title"], 0.07 * ref), "body": max(mins["body"], 0.038 * ref), "button": max(mins["button"], 0.036 * ref),
            "disclosure": max(mins["disclosure"], 0.026 * ref)}
    rect = lay.text
    tw, bw_, dw = _words(title_t), _words(body_t), _words(disc_t)
    cw = _words(cta_t)

    # 1) 고지(바닥 글): 줄 수 3 이하·영역의 28% 이하가 되는 가장 큰 크기. 안 되면 최소 크기.
    foot: Optional[_Measured] = None
    if disc_t:
        for i in range(SEARCH_STEPS):
            f = _lerp(maxs["disclosure"], mins["disclosure"], i / (SEARCH_STEPS - 1))
            cand = _measure(dw, f, rect.w, "disclosure")
            foot = cand
            if len(cand.lines) <= DISCLOSURE_MAX_LINES and cand.h <= DISCLOSURE_SHARE * rect.h:
                break
    foot_gap = (0.5 * foot.font) if foot else 0.0
    avail = rect.h - (foot.h + foot_gap if foot else 0.0)

    # 2) 제목·본문·(버튼) 쌓기: 가장 큰 크기부터 줄여 가며 영역에 맞는 첫 크기를 고른다. 줄 수 선호(제목 3·본문 5줄 이하)를 먼저, 안 되면 높이만 본다.
    def button_measure(font: float) -> tuple[_Measured, float, float]:
        padx, pady = 0.9 * font, 0.45 * font
        room = (lay.button.w if lay.button else rect.w)
        m_ = _measure(cw, font, max(10.0, room - 2 * padx), "button")
        bw = min(room, m_.w / FIT_SLACK + 2 * padx) if lay.button else min(rect.w, m_.w / FIT_SLACK + 2 * padx)
        bw = max(bw, min(room, 3 * font))
        return m_, bw, m_.h + 2 * pady

    def arrange(s: float):
        tf = mins["title"] + (maxs["title"] - mins["title"]) * s
        bf = mins["body"] + (maxs["body"] - mins["body"]) * s
        kf = mins["button"] + (maxs["button"] - mins["button"]) * s
        t_ = _measure(tw, tf, rect.w, "title") if title_t else None
        b_ = _measure(bw_, bf, rect.w, "body") if body_t else None
        btn = button_measure(kf) if cta_t else None
        gap1 = 0.45 * tf if (t_ and b_) else 0.0
        gap2 = 0.9 * bf if (btn and lay.button is None and (t_ or b_)) else 0.0
        total = (t_.h if t_ else 0) + gap1 + (b_.h if b_ else 0) + gap2 + (btn[2] if (btn and lay.button is None) else 0)
        return t_, b_, btn, gap1, gap2, total

    chosen = None
    for pref in (True, False):
        for i in range(SEARCH_STEPS):
            s = 1 - i / (SEARCH_STEPS - 1)
            t_, b_, btn, g1, g2, total = arrange(s)
            if total > avail + 0.5:
                continue
            if pref and ((t_ and len(t_.lines) > 3) or (b_ and len(b_.lines) > 5)):
                continue
            chosen = (t_, b_, btn, g1, g2, total)
            break
        if chosen:
            break
    if chosen is None:                                   # 최소 크기에서도 안 들어간다 → 최소 크기로 두고 넘친 블록을 표시한다
        chosen = arrange(0.0)
    t_, b_, btn, g1, g2, total = chosen

    blocks: list[TextBlock] = []
    cx = rect.x + rect.w / 2
    stack_bottom = rect.y + avail
    y = rect.y + ((avail - total) / 2 if (lay.valign == "middle" and total < avail) else 0.0)

    def place(role: str, text: str, ms: _Measured, fg: str, bg: str, top: float) -> TextBlock:
        w_ = max(ms.w, 1.0)
        x_ = rect.x if lay.align == "start" else cx - w_ / 2
        over = (top + ms.h > stack_bottom + 0.5) or (ms.w > rect.w + 0.5)
        return TextBlock(role, text, x_, top, w_, ms.h, ms.font, tuple(ms.lines), fg, bg, lay.align, ms.line_h, over)

    if t_ and title_t:
        blocks.append(place("title", title_t, t_, pal.title, pal.bg, y))
        y += t_.h + g1
    if b_ and body_t:
        blocks.append(place("body", body_t, b_, pal.body, pal.bg, y))
        y += b_.h
    btn_fill, btn_fg = (pal.bg, pal.accent) if lay.inverse_button else (pal.accent, pal.button_text)
    shapes = list(lay.shapes)
    btn_rect_svg = ""
    if btn and cta_t:
        ms, bw, bh = btn
        if lay.button is None:
            y += g2
            bx = rect.x if lay.align == "start" else cx - bw / 2
            by = y
            over = (by + bh > stack_bottom + 0.5) or (bw > rect.w + 0.5)
        else:
            bx = lay.button.x + (lay.button.w - bw) / 2
            by = lay.button.y + (lay.button.h - bh) / 2
            over = bh > lay.button.h + 0.5 or ms.w > lay.button.w - 1.8 * ms.font + 0.5
        blocks.append(TextBlock("button", cta_t, bx, by, bw, bh, ms.font, tuple(ms.lines), btn_fg, btn_fill, "middle", ms.line_h, over))
        btn_rect_svg = f'<rect x="{_n(bx)}" y="{_n(by)}" width="{_n(bw)}" height="{_n(bh)}" rx="{_n(min(bh / 2.4, 24))}" fill="{btn_fill}"/>'
    if foot and disc_t:
        fy = rect.y + rect.h - foot.h
        over = fy < rect.y - 0.5 or foot.w > rect.w + 0.5
        fw = max(foot.w, 1.0)
        fx = rect.x if lay.align == "start" else cx - fw / 2
        blocks.append(TextBlock("disclosure", disc_t, fx, fy, fw, foot.h, foot.font, tuple(foot.lines), pal.disclosure, pal.bg, lay.align, foot.line_h, over))

    # SVG: 진짜 <text>, 접근성 <title>·<desc>, 외부 리소스·스크립트 없음. 글은 XML 이스케이프한다.
    esc = lambda s: _xml_escape(s, {'"': "&quot;"})  # noqa: E731
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="bn-title bn-desc">',
             f'<title id="bn-title">{esc(title_t or body_t or cta_t)}</title>',
             f'<desc id="bn-desc">{esc(" ".join(x for x in (title_t, body_t, cta_t, disc_t) if x))}</desc>',
             f'<rect width="{W}" height="{H}" fill="{pal.bg}"/>', *shapes]
    if btn_rect_svg:
        parts.append(btn_rect_svg)
    for b in blocks:
        anchor = "middle" if (b.role == "button" or b.anchor == "middle") else "start"
        tx = (b.x + b.w / 2) if anchor == "middle" else b.x
        top = (b.y + (b.h - len(b.lines) * b.line_h) / 2) if b.role == "button" else b.y
        spans = []
        for i, ln in enumerate(b.lines):
            base = top + i * b.line_h + (b.line_h - b.font_px) / 2 + 0.84 * b.font_px
            spans.append(f'<tspan x="{_n(tx)}" y="{_n(base)}">{esc(ln)}</tspan>')
        parts.append(f'<text font-family="{FONT_FAMILY}" font-size="{_n(b.font_px)}" font-weight="{WEIGHT[b.role]}" fill="{b.fg}" text-anchor="{anchor}">{"".join(spans)}</text>')
    parts.append("</svg>")

    overflow_roles = [b.role for b in blocks if b.overflow]
    meta = {
        "size": size.key, "width": W, "height": H, "template": template, "palette": pal.key, "margin": round(m, 2),
        "fit_violation": bool(overflow_roles), "overflow_roles": overflow_roles,
        "has_disclosure_text": bool([d for d in copy.disclosures if _clean(d)]), "disclosure_included": bool(disc_t),
        "width_estimate_note": "글자 폭은 글꼴 없이 추정한 값이라 실제 글꼴에서는 줄바꿈이 달라질 수 있습니다",
        "size_assumed": "크기 규격은 이 프로젝트가 가정한 값이며 실제 광고 콘솔의 규격과 다를 수 있습니다",
    }
    return Banner(svg="".join(parts), blocks=tuple(blocks), meta=meta, size=size, template=template, palette=pal)


# ---------------------------------------------------------------------------
# 점수
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoreItem:
    key: str
    label: str
    value_text: str           # 사람이 읽는 값 ('7.1:1', '3줄' …)
    score: float              # 0~1
    detail: str               # 쉬운 한국어 설명
    value: Optional[float] = None
    applicable: bool = True


@dataclass(frozen=True)
class BannerWarning:
    rule_id: str              # 상세 기록용 규칙 코드 (화면 글에는 쓰지 않는다)
    text: str                 # 쉬운 한국어


@dataclass
class BannerScore:
    items: list
    raw: float
    total: float
    caps: list = field(default_factory=list)          # [(상한값, 이유)] — 걸린 상한 전부
    warnings: list = field(default_factory=list)
    flags: dict = field(default_factory=dict)

    def item(self, key: str) -> ScoreItem:
        return next(i for i in self.items if i.key == key)

    @property
    def capped(self) -> bool:
        return bool(self.caps) and self.total < self.raw - 1e-9

    @property
    def percent(self) -> int:
        return int(round(self.total * 100))


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _contrast_score(r: float) -> float:
    if r >= 7.0:
        return 1.0
    if r >= AA_RATIO:
        return 0.8 + 0.2 * (r - AA_RATIO) / (7.0 - AA_RATIO)
    if r >= 3.0:
        return 0.1 + 0.6 * (r - 3.0) / (AA_RATIO - 3.0)
    return _clamp01(0.1 * (r - 1.0) / 2.0)


def score(banner: Banner) -> BannerScore:
    """읽기 쉬운 정도. 모든 항목은 banner 의 블록(위치·크기·색)에서 다시 계산한다 — compose 가 남긴 표시를 믿지 않는다."""
    W, H = banner.size.width, banner.size.height
    u = W / ASSUMED_REF_WIDTH_PX
    items: list[ScoreItem] = []
    warnings: list[BannerWarning] = []
    caps: list[tuple[float, str]] = []
    flags = {"fit_violation": False, "low_contrast": False, "tiny_font": False, "disclosure_hidden": False}
    by_role = {b.role: b for b in banner.blocks}

    # 넘침
    over = [b for b in banner.blocks if b.overflow]
    flags["fit_violation"] = bool(over)
    names = {"title": "제목", "body": "본문", "button": "버튼 글", "disclosure": "안내 문구"}
    if over:
        txt = ", ".join(names[b.role] for b in over)
        items.append(ScoreItem("fit", "글이 칸에 들어감", "넘침", 0.0, f"{txt}이(가) 칸을 넘칩니다. 글자를 가장 작게 줄여도 들어가지 않으니 글을 줄이거나 더 큰 크기를 고르세요.", None))
        warnings.append(BannerWarning("IMG-FIT-01", f"{txt}이(가) 배너 칸에 들어가지 않아 잘려 보일 수 있습니다."))
        caps.append((CAP_FIT_VIOLATION, "글이 칸을 넘침"))
    else:
        items.append(ScoreItem("fit", "글이 칸에 들어감", "맞음", 1.0, "모든 글이 칸 안에 들어갑니다.", None))

    # 대비
    low_contrast = False
    for role, key, label in (("title", "contrast_title", "제목 글자 대비"), ("body", "contrast_body", "본문 글자 대비"), ("button", "contrast_button", "버튼 글자 대비")):
        b = by_role.get(role)
        if b is None:
            items.append(ScoreItem(key, label, "-", 1.0, "해당 글이 없습니다.", None, applicable=False))
            continue
        try:
            r = contrast_ratio(b.fg, b.bg)
        except ValueError:
            items.append(ScoreItem(key, label, "알 수 없음", 0.0, "글자색이나 바탕색을 해석하지 못해 대비를 확인하지 못했습니다.", None))
            low_contrast = True
            continue
        ok = r >= AA_RATIO
        low_contrast = low_contrast or not ok
        items.append(ScoreItem(key, label, f"{r:.1f}:1", _contrast_score(r),
                               "바탕과 충분히 구분됩니다 (기준 4.5:1 이상)." if ok else f"바탕색과 글자색이 비슷해 읽기 어렵습니다 (기준 4.5:1 이상, 지금 {r:.1f}:1).", r))
    dis = by_role.get("disclosure")
    if dis is not None:
        try:
            dr = contrast_ratio(dis.fg, dis.bg)
            if dr < AA_RATIO:
                low_contrast = True
        except ValueError:
            low_contrast = True
    flags["low_contrast"] = low_contrast
    if low_contrast:
        warnings.append(BannerWarning("IMG-CONTRAST-01", "글자색과 바탕색의 대비가 기준(4.5:1)에 못 미치는 글이 있습니다."))
        caps.append((CAP_LOW_CONTRAST, "글자 대비가 기준 미달"))

    # 최소 글자 크기
    ratios = [(b.font_px / min_font_px(W, b.role), b) for b in banner.blocks]
    if ratios:
        worst, wb = min(ratios, key=lambda t: t[0])
        tiny = worst < 0.999
        flags["tiny_font"] = tiny
        if tiny:
            sc = _clamp01(0.3 * worst)
            detail = f"{names[wb.role]}의 글자가 너무 작습니다 (기준 {min_font_px(W, wb.role):.0f}px, 지금 {wb.font_px:.0f}px)."
            warnings.append(BannerWarning("IMG-FIT-01", detail))
            caps.append((CAP_TINY_FONT, "글자 크기가 최소 기준 미만"))
        else:
            sc = 0.7 + 0.3 * _clamp01((worst - 1.0) / (COMFORT_FACTOR - 1.0))
            detail = "모든 글자가 최소 크기 이상입니다." + ("" if worst >= COMFORT_FACTOR else " 가장 작은 글자는 최소 크기에 가깝습니다.")
        items.append(ScoreItem("min_font", "가장 작은 글자 크기", f"{wb.font_px:.0f}px", sc, detail, wb.font_px))
    else:
        items.append(ScoreItem("min_font", "가장 작은 글자 크기", "-", 1.0, "글이 없습니다.", None, applicable=False))

    # 안전 여백
    m = banner.meta.get("margin", safe_margin_px(banner.size))
    outside = []
    for b in banner.blocks:
        if b.x < m - 0.5 or b.y < m - 0.5 or b.x + b.w > W - m + 0.5 or b.y + b.h > H - m + 0.5:
            outside.append(b)
    if banner.blocks:
        frac = 1 - len(outside) / len(banner.blocks)
        items.append(ScoreItem("safe_margin", "가장자리 여백", "침범 없음" if not outside else f"{len(outside)}곳 침범", frac,
                               "모든 글이 안전 여백 안에 있습니다." if not outside else "일부 글이 가장자리의 안전 여백을 넘어 기기에 따라 가려질 수 있습니다.", frac))
    else:
        items.append(ScoreItem("safe_margin", "가장자리 여백", "-", 1.0, "글이 없습니다.", None, applicable=False))

    # 글자 면적 비율
    area = sum(b.w * b.h for b in banner.blocks if b.role != "button") / (W * H)
    dens = _clamp01(1.0 - (area - 0.25) / (0.55 - 0.25))
    items.append(ScoreItem("density", "글자가 차지하는 면적", f"{area:.0%}", dens,
                           "글이 적당히 놓여 있습니다." if area <= 0.25 else ("글이 다소 빽빽합니다." if area <= 0.40 else "글이 너무 빽빽해 한눈에 읽기 어렵습니다."), area))

    # 버튼 크기
    bt = by_role.get("button")
    if bt is not None:
        need_h, need_w = ASSUMED_MIN_BUTTON_H_PX * u, ASSUMED_MIN_BUTTON_W_PX * u
        sc = _clamp01(min(bt.h / need_h, bt.w / need_w))
        items.append(ScoreItem("button", "버튼 크기", f"{bt.w:.0f}×{bt.h:.0f}px", sc, "누르기 쉬운 크기입니다." if sc >= 1.0 else "버튼이 작아 누르기 어려울 수 있습니다.", sc))
    else:
        items.append(ScoreItem("button", "버튼 크기", "-", 1.0, "버튼 글이 없습니다.", None, applicable=False))

    # 줄 수
    tl = len(by_role["title"].lines) if "title" in by_role else 0
    bl = len(by_role["body"].lines) if "body" in by_role else 0
    ts = 1.0 if tl <= 2 else (0.7 if tl == 3 else 0.3)
    bs = 1.0 if bl <= 3 else (0.85 if bl == 4 else (0.6 if bl == 5 else 0.3))
    if tl or bl:
        items.append(ScoreItem("lines", "줄 수", f"제목 {tl}줄 · 본문 {bl}줄", min(ts if tl else 1.0, bs if bl else 1.0),
                               "줄 수가 적당합니다." if min(ts if tl else 1.0, bs if bl else 1.0) >= 1.0 else "줄이 많아 읽는 데 시간이 걸립니다.", float(tl + bl)))
    else:
        items.append(ScoreItem("lines", "줄 수", "-", 1.0, "제목·본문이 없습니다.", None, applicable=False))

    # 고지
    if banner.meta.get("has_disclosure_text"):
        hidden = dis is None or dis.overflow or _safe_ratio(dis) < AA_RATIO
        flags["disclosure_hidden"] = bool(hidden)
        if hidden:
            why = "배너에 넣지 않았거나" if dis is None else "칸을 넘치거나 대비가 낮아"
            items.append(ScoreItem("disclosure", "필수 안내 문구 표시", "가려질 수 있음", 0.0,
                                   f"안내 문구가 {why} 잘려 보이거나 읽기 어려울 수 있습니다. 문구란에 따로 들어가는 안내라면 괜찮을 수 있습니다.", None))
            warnings.append(BannerWarning("IMG-DISC-01", "필수 안내 문구가 배너에서 잘려 보일 수 있습니다 (이 프로젝트의 규칙이며 실제 규정 해석이 아닙니다)."))
            caps.append((CAP_DISCLOSURE_HIDDEN, "안내 문구가 가려질 수 있음"))
        else:
            items.append(ScoreItem("disclosure", "필수 안내 문구 표시", "보임", 1.0, "안내 문구가 잘리지 않고 배너에 들어갑니다.", None))
    else:
        items.append(ScoreItem("disclosure", "필수 안내 문구 표시", "-", 1.0, "광고 문구에 안내 문구가 없습니다.", None, applicable=False))

    num = den = 0.0
    for it in items:
        if it.applicable:
            wgt = SCORE_WEIGHTS.get(it.key, 0.0)
            num += wgt * it.score
            den += wgt
    raw = num / den if den else 0.0
    total = min([raw] + [c for c, _ in caps])
    return BannerScore(items=items, raw=round(raw, 4), total=round(total, 4), caps=caps, warnings=warnings, flags=flags)


def _safe_ratio(b: TextBlock) -> float:
    try:
        return contrast_ratio(b.fg, b.bg)
    except ValueError:
        return 1.0


# ---------------------------------------------------------------------------
# 추천
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    rank: int
    banner: Banner
    score: BannerScore
    size_key: str
    template_key: str
    palette_key: str
    reasons: list = field(default_factory=list)


def _shares(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
    """크기·템플릿·팔레트 중 하나라도 같다."""
    return a[0] == b[0] or a[1] == b[1] or a[2] == b[2]


def _similar(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
    """크기와 템플릿이 같거나, 크기와 팔레트가 같거나, 템플릿과 팔레트가 같으면 너무 비슷하다고 본다."""
    return (a[0] == b[0] and a[1] == b[1]) or (a[0] == b[0] and a[2] == b[2]) or (a[1] == b[1] and a[2] == b[2])


def _reasons(sc: BannerScore) -> list[str]:
    out = [w.text for w in sc.warnings]
    good = [i for i in sc.items if i.applicable and i.score >= 0.95 and i.key in ("contrast_title", "contrast_body", "fit", "min_font")]
    if not out and good:
        out.append("글이 칸에 잘 들어가고 글자와 바탕이 잘 구분됩니다.")
    low = [i.detail for i in sc.items if i.applicable and i.score < 0.7 and i.key not in ("fit", "disclosure") and not any(i.detail == w.text for w in sc.warnings)]
    return out + low[:2]


def recommend(copy: AdCopy, n: int = 3, *, sizes: Optional[Sequence[Union[str, BannerSize]]] = None, include_disclosures: bool = True) -> list[Candidate]:
    """크기×템플릿×팔레트 조합을 점수순(동점이면 정해진 순서)으로 늘어놓고 서로 비슷한 것을 건너뛰며 n 개를 고른다. 같은 입력이면 같은 결과."""
    size_list = [_as_size(s) for s in (sizes if sizes is not None else ASSUMED_BANNER_SIZES.values())]
    pool: list[tuple[float, int, Banner, BannerScore]] = []
    order = 0
    for sz in size_list:
        for tk in TEMPLATES:
            for pk, pal in PALETTES.items():
                b = compose(copy, size=sz, template=tk, palette=pal, include_disclosures=include_disclosures)
                sc = score(b)
                pool.append((-sc.total, order, b, sc))
                order += 1
    pool.sort(key=lambda t: (t[0], t[1]))
    picked: list[tuple[Banner, BannerScore]] = []
    keys: list[tuple[str, str, str]] = []
    # 1차: 크기·템플릿·팔레트가 모두 다른 것 → 2차: 너무 비슷하지 않은 것 → 3차: 점수순으로 채움 (조합이 모자랄 때만)
    for conflict in (_shares, _similar, lambda a, b: False):
        for _, _, b, sc in pool:
            if len(picked) >= n:
                break
            k = (b.size.key, b.template, b.palette.key)
            if any(b is p[0] for p in picked) or any(conflict(k, other) for other in keys):
                continue
            picked.append((b, sc))
            keys.append(k)
    return [Candidate(rank=i + 1, banner=b, score=sc, size_key=b.size.key, template_key=b.template, palette_key=b.palette.key, reasons=_reasons(sc))
            for i, (b, sc) in enumerate(picked)]
