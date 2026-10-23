"""표기 변형 우회(FM-11)를 접는 정규화. 원문 위치로 되돌릴 수 있도록 인덱스 맵을 유지한다.

두 표면(surface)을 만든다.
- loose: NFKC → 제로폭·방향 제어 문자 제거 → 한자/유사 문자 접기 → 공백 1개로 축약. 구두점은 유지(금리 범위 '4.5%~15%' 등).
- tight: loose 에서 한글 음절 사이의 공백·장식 기호('원 금 보 장', '원.금.보.장', '원*금*보*장')를 제거.
각 표면의 i 번째 문자가 원문의 어느 인덱스에서 왔는지 `src[i]` 로 알 수 있다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ZERO_WIDTH = set("\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
                 "\u2061\u2062\u2063\u2064\u034f\u115f\u1160\u3164\uffa0\u2800\ufe0e\ufe0f")
_KEEP_CONTROL = {"\n", "\t", "\r"}


def is_invisible(ch: str) -> bool:
    """제로폭·형식·제어·사용자 영역·미할당·결합 문자, 한글 채움 문자, 점자 공백 등 '보이지 않지만 글자를 가르는' 문자.
    라이브 레드팀(run 38928048713)이 NUL 을 끼워 '원금\\x00보장' 으로 어휘 필터를 피한 뒤 추가됐다."""
    if ch in ZERO_WIDTH:
        return True
    if ch in _KEEP_CONTROL:
        return False
    return unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn", "Cs", "Mn", "Me")

# 우회에 흔히 쓰이는 한자·기호 → 한글. 일반 텍스트의 한자를 전부 번역하려는 것이 아니라 '정책 어휘' 만 접는다.
FOLD_MAP: dict[str, str] = {
    "元": "원", "金": "금", "保": "보", "障": "장", "證": "증", "收": "수", "益": "익", "確": "확", "定": "정",
    "最": "최", "低": "저", "高": "고", "無": "무", "條": "조", "件": "건", "貸": "대", "出": "출", "利": "이",
    "子": "자", "損": "손", "失": "실", "當": "당", "日": "일", "即": "즉", "時": "시", "承": "승", "認": "인",
    "審": "심", "査": "사", "投": "투", "資": "자", "安": "안", "全": "전", "大": "대", "博": "박", "一": "일",
    "位": "위", "唯": "유", "完": "완", "璧": "벽", "免": "면", "責": "책",
    # 특수 문자 숫자/기호 유사체
    "％": "%", "～": "~", "〜": "~", "－": "-", "‐": "-", "–": "-", "—": "-", "ㅇ": "ㅇ",
}

_DECOR = set(" \t\r\n.·•・∙‧,|/\\-_*~^'\"`“”‘’()[]{}<>!?:;")
_HANGUL = re.compile(r"[가-힣]")
_WEEKDAYS = set("월화수목금토일")


def _is_weekday_paren(chars: list[str], start: int, end: int) -> bool:
    """chars[start:end] 가 '10월 1일(수)' 의 요일 괄호 여는 쪽인가. 날짜 뒤 요일 괄호까지 걷어내면 '일(수' 가 '일수' 로 붙어 정책 낱말(일수대출)에 걸리므로, 숫자 + 일 바로 뒤의 '(요일)' 만은 그대로 둔다."""
    return (end > start and chars[end - 1] == "(" and all(c == " " for c in chars[start:end - 1]) and start >= 2 and chars[start - 1] == "일" and chars[start - 2].isdigit()
            and end + 1 < len(chars) and chars[end] in _WEEKDAYS and chars[end + 1] == ")")


@dataclass
class Surface:
    name: str
    text: str
    src: list[int]  # text[i] 가 원문 original[src[i]] 에서 왔다
    loose_indices: list[int] | None = None  # tight[i]가 loose의 어느 문자였는지 (삭제한 경계 보존)

    def to_original(self, start: int, end: int) -> tuple[int, int]:
        """표면 구간 [start, end) → 원문 구간 [o_start, o_end)."""
        if end <= start or not self.src:
            return (0, 0)
        start = max(0, min(start, len(self.src) - 1))
        end = max(1, min(end, len(self.src)))
        return (self.src[start], self.src[end - 1] + 1)


@dataclass
class Normalized:
    original: str
    loose: Surface
    tight: Surface
    zero_width_count: int
    folded_count: int

    def surfaces(self) -> list[Surface]:
        return [self.loose, self.tight]

    def is_joined_fragment(self, start: int, end: int) -> bool:
        """tight 일치가 서로 다른 두 단어의 중간 조각을 이어 만든 것인가.

        '쿠[폰! 지]금'처럼 양 끝 모두 loose의 영숫자/한글 덩어리 안이고,
        그 사이에서 눈에 보이는 구분자를 제거한 경우만 제외한다.
        한쪽 끝이라도 경계인 '다 단계를', '불법다 단계'는 보수적으로 유지한다.
        한국어 조사·합성어 때문에 모든 일치에 정규식 \\b를 요구하지 않는다.
        제로폭 제거·NFKC·한자 접기는 이미 loose에 반영되어 탐지를 유지한다.
        """
        positions = self.tight.loose_indices
        if positions is None or not (0 <= start < end <= len(positions)):
            return False
        a, b = positions[start], positions[end - 1] + 1
        if b - a == end - start:  # 일치 내부에는 삭제한 구분자가 없음
            return False
        text = self.loose.text
        return (a > 0 and b < len(text)
                and text[a - 1].isalnum() and text[a].isalnum()
                and text[b - 1].isalnum() and text[b].isalnum())

    def had_evasion_in(self, o_start: int, o_end: int) -> bool:
        """원문 구간 안에 제로폭 문자나 접힌 한자가 있었는가 (= 표기 우회 흔적)."""
        seg = self.original[o_start:o_end]
        return any(is_invisible(ch) for ch in seg) or any(ch in FOLD_MAP and FOLD_MAP[ch] != ch for ch in seg)


def normalize(original: str) -> Normalized:
    chars: list[str] = []
    src: list[int] = []
    zero_width = 0
    folded = 0
    for i, ch in enumerate(original):
        if is_invisible(ch):
            zero_width += 1
            continue
        nf = unicodedata.normalize("NFKC", ch)
        for c in nf:
            if c in FOLD_MAP:
                if FOLD_MAP[c] != c:
                    folded += 1
                c = FOLD_MAP[c]
            if is_invisible(c):
                zero_width += 1
                continue
            if unicodedata.category(c) in ("Zs", "Zl", "Zp"):
                c = " "  # 모든 종류의 공백 문자는 보통 공백으로
            chars.append(c)
            src.append(i)
    # 공백 축약
    l_chars: list[str] = []
    l_src: list[int] = []
    prev_space = False
    for c, s in zip(chars, src):
        if c.isspace():
            if prev_space:
                continue
            c = " "
            prev_space = True
        else:
            prev_space = False
        l_chars.append(c)
        l_src.append(s)
    loose = Surface("loose", "".join(l_chars), l_src)

    # tight: 한글 사이의 공백·장식 제거. 장식 문자가 이어진 한 덩어리(run)는 왼쪽 이웃과 오른쪽 이웃이 같으므로
    # 덩어리마다 한 번만 판단한다 — 문자마다 덩어리 끝까지 다시 훑으면 '.....' 같은 입력에서 시간이 길이의 제곱으로 는다.
    t_chars: list[str] = []
    t_src: list[int] = []
    t_loose: list[int] = []
    n = len(l_chars)
    idx = 0
    while idx < n:
        if l_chars[idx] not in _DECOR:
            t_chars.append(l_chars[idx])
            t_src.append(l_src[idx])
            t_loose.append(idx)
            idx += 1
            continue
        end = idx + 1
        while end < n and l_chars[end] in _DECOR:
            end += 1
        # 덩어리 왼쪽의 비장식 문자와 오른쪽의 비장식 문자가 모두 한글이면 덩어리 전체를 버린다
        if not (idx > 0 and end < n and _HANGUL.match(l_chars[idx - 1]) and _HANGUL.match(l_chars[end])) or _is_weekday_paren(l_chars, idx, end):
            t_chars.extend(l_chars[idx:end])
            t_src.extend(l_src[idx:end])
            t_loose.extend(range(idx, end))
        idx = end
    tight = Surface("tight", "".join(t_chars), t_src, t_loose)
    return Normalized(original=original, loose=loose, tight=tight, zero_width_count=zero_width, folded_count=folded)


def loose_text(s: str) -> str:
    return normalize(s).loose.text
