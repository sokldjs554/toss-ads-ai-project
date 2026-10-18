"""근거 결합 (FM-12) 과 랜딩 일치 (TA-LANDING-01).

소재의 숫자·주장은 광고주가 제공한 사실 목록(ProductFacts.facts) 또는 랜딩 텍스트에서 확인돼야 한다.

검사 범위 (코드가 판단할 수 있는 것만 — 그 밖은 의미 심사와 사람의 몫이다)
- 숫자: 값 전체 + 단위 종류(비율·금액·기간·수량·배수) 가 같은 수치가 사실/랜딩에 있어야 한다. '5%' 는 '15일' 로 뒷받침되지 않는다.
  같은 값·같은 단위라도 주변 어휘가 '할인' vs '금리' 처럼 다른 뜻이면 뒷받침으로 보지 않는다 (어휘 목록에 있는 경우만).
  금액의 주기('월 19,900원' 과 '연 19,900원') 도 다르면 뒷받침이 아니다. 검사 대상은 헤드라인·본문·CTA **와 고지 문구** 이다.
  '할인5%'·'금리4.5%' 처럼 한글에 붙여 쓴 숫자도 (% · 원 · 배 · 일 · 개월 단위일 때) 잡는다. 전화번호·심의번호·사업자번호 같은 식별번호는 혜택 수치가 아니다.
- 주장→근거: 주장이 가리킨 fact_id 가 존재해야 하고, 그 사실이 실제로 그 주장을 뒷받침해야 한다 (주장의 숫자가 그 사실에 있거나, 핵심 어휘가 겹침).
- 조건 (GRND-COND-01 · GRND-COND-02): 코드가 비교하는 조건은 아래 `_CONDITIONS` 의 여섯 종류뿐이다.
  · 빠짐(COND-01) — 광고가 가리킨 사실에 그 조건이 있는데 광고 문구·고지 어디에도 같은 조건이 없음. 단어가 있다고 충족이 아니다:
    금액·날짜·기간·횟수 조건은 **같은 값** 이어야 하고, '자동결제 없음' 처럼 부정된 표현은 '자동결제' 조건을 충족하지 않는다.
  · 다름(COND-02) — 광고가 같은 종류의 조건을 말하는데 값이 사실과 다르거나(5만원 이상 ↔ 5천원 이상, 10월 31일까지 ↔ 11월 30일까지, 1인 1회 ↔ 1인 3회),
    뜻이 반대(자동결제 있음 ↔ 자동결제 없음, 심사 결과에 따라 ↔ 무심사, 이상 ↔ 이하).
  이 여섯 종류 밖의 조건(예: '신규 고객 한정', '첫 달만')은 코드가 확인하지 못한다 — 의미 심사와 사람의 몫이다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.schemas import AdCopy, Fact, Finding, Layer, ProductFacts, Severity
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog

MAX_ANALYZED_CHARS = 20_000   # 이보다 긴 글은 앞부분만 본다 (정규식 비용이 글 길이에 비례해 커지는 입력을 막는다. 광고 문구·사실은 이보다 훨씬 짧다)

_MULT = {"십": 10, "백": 100, "천": 1_000, "만": 10_000, "억": 100_000_000}
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
# 숫자 (앞이 영문자·숫자·점이면 이름·버전의 일부라 읽지 않는다). 'A-12' 처럼 영문자 뒤 하이픈도 제외. 하이픈은 범위 구분자('50-60%')로 읽는다.
_NUM_TOKEN = re.compile(rf"(?<![A-Za-z\d.])(?<![A-Za-z]-)({_NUMBER})(?!\d)")
_MULT_RUN = re.compile(r"[십백천만억]+")
_CHAIN = re.compile(rf"[ \t]*({_NUMBER})([십백천만억]*)")
_RANGE_BETWEEN = re.compile(r"^\s*[~\-–—〜]\s*$")
_UNIT_NAMES = ("퍼센트", "프로", "개월", "포인트", "시간", "kg", "㎏", "퍼", "원", "명", "배", "일", "달", "년", "분", "초", "회", "건", "호", "개", "점", "p", "g")
_UNIT_AT = re.compile(r"(%|" + "|".join(_UNIT_NAMES) + r")", re.IGNORECASE)
_WS = re.compile(r"\s*")
# 숫자 바로 뒤 글자가 단위처럼 보여도 실제로는 다른 낱말인 경우 ('1분기' · '7일정' · '100원금' · '3회원')
_NOT_UNIT_WORD = re.compile(
    r"(?:분기|분석|분명|일정|일부|일반|일시|일상|일찍|원금|원래|회원|회사|회의|개인|개발|개선|개통|건강|건물|건설|호텔|호출|점검|점수|명품|명세|명단|달성|달러|달라|배경|배우|배터리)")
# 숫자와 단위 사이를 띄어 썼다면, 단위 뒤에 한글이 바로 이어질 때는 이 말들(조사·'이상' 같은 꼬리)일 때만 단위로 읽는다
_UNIT_TAIL_OK = re.compile(r"(?:이상|이하|이내|이후|이전|미만|초과|간|까지|마다|당|부터|씩|째|짜리|에|의|은|는|이|가|을|를|도|로|과|와|만|만큼|정도|쯤|여|내)")
_UNIT_CLASS = {"%": "percent", "원": "krw", "명": "count", "회": "count", "건": "count", "개": "count", "호": "count",
               "배": "multiple", "일": "duration", "개월": "duration", "년": "duration", "시간": "duration", "분": "duration",
               "초": "duration", "점": "points", "p": "points", "포인트": "points", "kg": "weight", "㎏": "weight", "g": "weight"}
_UNIT_ALIAS = {"퍼센트": "%", "프로": "%", "퍼": "%", "달": "개월"}
_GLUED_UNITS = {"%", "원", "배", "개월", "일"}   # 한글에 붙여 쓴 숫자('할인5%')는 혜택을 말하는 단위일 때만 본다. '코스피200'·'제2026호' 같은 이름·번호는 무시
# 혜택 수치가 아닌 식별번호·기준일: 찾은 자리는 공백으로 지워 숫자로 읽지 않는다.
_IDENT_PRE = re.compile(      # 날짜보다 먼저 지운다 — 심의필 유효기간·기준일은 광고가 약속하는 날짜가 아니다
    r"제\s*[\d\-–]+\s*호(?:\s*\(\s*\d{4}[^)\n]{0,40}\))?"
    r"|(?:유효\s*기간|사용\s*기한|심의\s*기간)\s*:?\s*\d{4}[\d.\-/년월일 \t~–]{4,40}"
    r"|\d{4}\s*[.\-/년]\s*\d{1,2}\s*(?:[.\-/월]\s*\d{1,2}\s*일?\.?|월)?\s*(?:기준|현재)")
_IDENT = re.compile(
    r"https?://\S+"
    r"|(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+"
    r"|(?<![\w.@-])(?:[A-Za-z0-9-]+\.)+(?:com|net|kr|im|io|co|org|me|app|dev|shop|store)(?:/\S*)?"
    r"|(?<![\d.])\d+(?:\.\d+){2,}(?![\d.])"
    r"|(?<![\d\-])(?:0\d{1,2}|1[5-9]\d{2})\s*[-)]\s*\d{3,4}(?:\s*-\s*\d{4})?(?![\d\-]|\s*(?:원|%|일|개월|배|명|회|건|개|점))"
    r"|(?<![\d\-])\d{3}-\d{2}-\d{5}(?![\d\-])")
# 날짜: 숫자 대조에서 '31일'·'2026년' 같은 기간 수치로 오인하지 않도록 먼저 잡아 (월, 일) 로 비교한다.
#  · 연도가 있는 형태(2026.10.31 · 2026-10-31 · 2026년 10월 31일) · '월' 이 있는 형태(10월 31일)
#  · 연도도 '월' 도 없는 '10.31'·'10/31' 은 뒤에 '(금)'·'까지'·'종료'·'마감' 이 올 때만 날짜다 ('4.5'·'3-5일'·'1.5배' 는 날짜가 아니다)
_DATE_PARTS = (r"(?:(?P<y>\d{4})\s*[.\-/년]\s*(?P<m>\d{1,2})\s*[.\-/월]\s*(?P<d>\d{1,2})"
               r"|(?P<m2>\d{1,2})\s*월\s*(?P<d2>\d{1,2})"
               r"|(?<![\d.\-/])(?P<m3>\d{1,2})\s*[./]\s*(?P<d3>\d{1,2})(?=\s*(?:\(\s*[월화수목금토일]\s*\)|까지|종료|마감)))")
_DATE = re.compile(rf"(?<![\d.]){_DATE_PARTS}\s*일?(?![\d%]|,\d{{3}}(?!\d))")
_DATE_TAIL = re.compile(r"\s*[~\-–—〜]\s*(\d{1,2})\s*일")   # '10월 1일 ~ 31일' 의 뒷날짜 (같은 달)
_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
_STOP = {"지금", "바로", "확인", "조건", "상품", "서비스", "가능", "신청", "이용", "고객", "혜택", "제공", "있습니다", "있어요", "합니다", "해요",
         "최대", "최소", "기준", "경우", "통해", "위해", "대한", "모든", "전체"}
# 금액·비율의 주기: '월 19,900원' 과 '연 19,900원' 은 같은 수치가 아니다. 숫자 앞의 주기 말('월', '매월', '연회비', '월 최대', '연 이용료') 또는 뒤의 '/월' 로 읽는다.
_CYCLE_TOKEN = re.compile(r"(?<![가-힣\d])(매월|매년|매주|매일|하루|연간|월간|연회비|월회비|연이율|월정액|1\s*년에|한\s*달에|월|연|년|주)(?=[\s가-힣]|$)")
_CYCLE_FILLER = re.compile(r"^(?:[ \t]*(?:최대|최소|평균|약|총|기본|정기|일반|이용료|구독료|요금|수수료|회비|이자|금리|할인|캐시백|적립|혜택|금액|비용|납입|한도|부터|당|가입|이용|결제|납부)){0,3}[ \t]*$")
_CYCLE_POST = re.compile(r"^\s*/\s*(월|연|년|주|일)(?![가-힣])")
_CYCLE_NAME = {"매월": "월", "월": "월", "월간": "월", "월회비": "월", "월정액": "월", "한달에": "월", "매년": "연", "연": "연", "년": "연", "연간": "연", "연회비": "연",
               "연이율": "연", "1년에": "연", "매주": "주", "주": "주", "매일": "일", "하루": "일", "일": "일"}
# 같은 숫자·단위라도 뜻이 다르면 뒷받침이 아니다. 숫자와 같은 구절 안의 뜻 어휘를 '묶음' 으로 비교한다 (할인≈쿠폰≈세일, 금리≈이자…).
_SENSE_GROUPS: dict[str, tuple[str, ...]] = {
    "할인": ("할인", "쿠폰", "세일", "특가"), "금리": ("금리", "이자", "이율"), "수익": ("수익률", "수익", "배당", "성과"),
    "적립": ("적립", "캐시백", "포인트", "리워드"), "수수료": ("보수", "수수료"), "연체": ("연체",), "환급": ("환급",), "배송": ("배송",),
    "체험": ("체험", "무료"), "한도": ("한도",), "보장": ("보장", "보상"), "할부": ("할부",),
}
_SENSE_WORDS = tuple(w for ws in _SENSE_GROUPS.values() for w in ws)
_SENSE_OF = {w: g for g, ws in _SENSE_GROUPS.items() for w in ws}


def _fold(text: str) -> str:
    """글자 수를 바꾸지 않는 NFKC 접기 — 전각 숫자·％·～ 를 반각으로. 길이가 달라지는 글자(㎏→kg 등)는 그대로 둔다. 그래서 위치(start/end)가 원문과 같다."""
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
            continue
        n = unicodedata.normalize("NFKC", ch)
        out.append(n if len(n) == 1 else ch)
    return "".join(out)


@dataclass(frozen=True)
class Quantity:
    value: float
    cls: str            # percent | krw | count | multiple | duration | points | weight | date | bare
    unit: str           # 원문 단위 (duration·count 는 단위까지 같아야 한다)
    sense: frozenset    # 같은 구절의 뜻 묶음들 (없으면 빈 집합)
    start: int
    end: int
    raw: str
    cycle: str = ""     # 금액·비율의 주기 (월 · 연 · 주 · 일). 말하지 않았으면 빈 문자열
    year: int = 0       # date 수량이 연도를 말했으면 그 연도 (0 = 말하지 않음). 둘 다 말했을 때만 비교한다

    def supports(self, other: "Quantity") -> bool:
        """self(사실 쪽) 가 other(광고 쪽) 를 뒷받침하는가."""
        if abs(self.value - other.value) > 1e-9:
            return False
        if other.cls == "date" or self.cls == "date":
            return self.cls == other.cls and (not self.year or not other.year or self.year == other.year)
        if other.cls != "bare":
            # 광고가 단위를 붙여 말한 숫자는 사실에도 같은 종류의 단위로 있어야 한다. 전화번호·상품명 속 숫자는 근거가 아니다.
            if self.cls == "bare" or self.cls != other.cls:
                return False
            if self.cls in ("duration", "count") and self.unit != other.unit:
                return False
        if self.cycle and other.cycle and self.cycle != other.cycle:
            return False  # 둘 다 주기를 말했는데 다르면 다른 수치 (한쪽이 주기를 말하지 않은 경우는 코드가 판단하지 않는다)
        if self.sense and other.sense and not (self.sense & other.sense):
            return False
        return True


@dataclass
class GroundingResult:
    findings: list[Finding] = field(default_factory=list)
    numbers_checked: int = 0
    numbers_unsupported: list[str] = field(default_factory=list)
    claims_unsupported: list[str] = field(default_factory=list)
    conditions_omitted: list[str] = field(default_factory=list)   # 사실의 조건이 광고에 없음 (COND-01)
    conditions_conflict: list[str] = field(default_factory=list)  # 광고의 조건이 사실과 값·뜻이 다름 (COND-02)
    landing_checked: bool = False
    landing_mismatch: list[str] = field(default_factory=list)
    referenced_fact_ids: list[str] = field(default_factory=list)


_SEG_DELIM = re.compile(r"[\n.,;()·~\-/]")


def _sense(text: str, start: int, end: int) -> frozenset:
    """숫자와 같은 구절(줄·쉼표·괄호로 끊기는 구간) 안에 있는 뜻 묶음 전부. 다른 줄의 어휘를 끌어오지 않는다."""
    seg_start = max((m.end() for m in _SEG_DELIM.finditer(text, 0, start)), default=0)
    m_end = _SEG_DELIM.search(text, end)
    seg_end = m_end.start() if m_end else len(text)
    seg = text[seg_start:seg_end]
    return frozenset(_SENSE_OF[w] for w in _SENSE_WORDS if w in seg)


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(a < end and start < b for a, b in spans)


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _mask(text: str, pat: re.Pattern) -> str:
    return _blank(text, [(m.start(), m.end()) for m in pat.finditer(text)])


def _date_of(m: re.Match) -> tuple[int, int, int]:
    """(연, 월, 일) — 연도를 말하지 않았으면 0."""
    if m.group("y"):
        return int(m.group("y")), int(m.group("m")), int(m.group("d"))
    if m.group("m2"):
        return 0, int(m.group("m2")), int(m.group("d2"))
    return 0, int(m.group("m3")), int(m.group("d3"))


def _dates(folded: str, original: str) -> tuple[list[Quantity], str]:
    """날짜를 (월*100+일) 값의 date 수량으로 뽑고, 그 자리를 공백으로 지운 텍스트를 돌려준다. 뒷날짜('10월 1일 ~ 31일')도 같은 달로 읽는다."""
    out: list[Quantity] = []
    spans: list[tuple[int, int]] = []
    for m in _DATE.finditer(folded):
        year, month, day = _date_of(m)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        end = m.end()
        out.append(Quantity(value=float(month * 100 + day), cls="date", unit="date", sense=frozenset(), start=m.start(), end=end,
                            raw=original[m.start():end].strip(), year=year))
        tail = _DATE_TAIL.match(folded, end)
        if tail and 1 <= int(tail.group(1)) <= 31:
            out.append(Quantity(value=float(month * 100 + int(tail.group(1))), cls="date", unit="date", sense=frozenset(), start=tail.start(1), end=tail.end(),
                                raw=original[tail.start(1):tail.end()], year=year))
            end = tail.end()
        spans.append((m.start(), end))
    return out, _blank(folded, spans)


def _cycle_of(text: str, start: int, end: int) -> str:
    """숫자의 주기. 앞쪽은 같은 구절 안에서 '주기 말 (+ 최대·평균·이용료 같은 짧은 수식어) + 숫자' 꼴만 읽는다."""
    seg_start = max(start - 14, 0)
    cut = max((m.end() for m in _SEG_DELIM.finditer(text, seg_start, start)), default=seg_start)
    window = text[cut:start]
    last = None
    for m in _CYCLE_TOKEN.finditer(window):
        last = m
    if last is not None and _CYCLE_FILLER.match(window[last.end():]):
        return _CYCLE_NAME[re.sub(r"\s+", "", last.group(1))]
    post = _CYCLE_POST.match(text[end:end + 6])
    return _CYCLE_NAME[post.group(1)] if post else ""


def _mult_value(run: str) -> float:
    v = 1.0
    for ch in run:
        v *= _MULT.get(ch, 1)
    return v


def _split_run(run: str) -> tuple[float, float]:
    """자릿수 단위 문자열을 (십·백·천 의 곱, 만·억 의 곱) 으로 나눈다. '천만' → (1000, 10000)."""
    small = big = 1.0
    for ch in run:
        if ch in "만억":
            big *= _MULT[ch]
        else:
            small *= _MULT[ch]
    return small, big


def _read_amount(t: str, num_end: int, first: float) -> tuple[float, int, str]:
    """숫자 뒤의 자릿수 단위(십·백·천·만·억)를 읽는다. '1만5천' · '1억5천만' · '1천5백만' 처럼 큰 단위부터 이어진 것은 하나의 값으로 더한다 (1만5천원 = 15,000원).
    '3만 5만' 처럼 작아지지 않는 것은 이어 붙이지 않는다. 반환: (값, 끝 위치, 읽은 자릿수 단위 문자열 — 없으면 '')."""
    m = _MULT_RUN.match(t, num_end)
    if not m:
        return first, num_end, ""
    small, big = _split_run(m.group())
    total = first * small * big
    last_place = small * big
    end = m.end()
    pre_sum: Optional[float] = first * small if big == 1 else None   # '1천5백만' 처럼 만·억 앞에서 끝나는 천·백 단위의 합 (만·억이 붙기 전)
    min_small = small
    while True:
        n = _CHAIN.match(t, end)
        if not n:
            break
        v2 = float(n.group(1).replace(",", ""))
        if n.group(2):
            s2, b2 = _split_run(n.group(2))
            place = s2 * b2
            if place < last_place:
                total += v2 * place
                last_place = place
                pre_sum = (pre_sum + v2 * s2) if (pre_sum is not None and b2 == 1) else None
                min_small = s2 if b2 == 1 else min_small
            elif b2 > 1 and s2 > 1 and pre_sum is not None and s2 < min_small:
                total = (pre_sum + v2 * s2) * b2      # '1천5백만' = (1천 + 5백) × 만
                last_place, pre_sum = place, None
            else:
                break
            end = n.end()
            continue
        # 맨 끝 자릿수 단위 없는 꼬리: 붙여 쓴 네 자리 이하 정수 뒤에 '원' 이 올 때만 ('1만5000원')
        if n.start(1) == end and re.fullmatch(r"\d{1,4}", n.group(1)) and v2 < last_place and re.match(r"\s*원", t[n.end():]):
            total += v2
            end = n.end()
        break
    return total, end, m.group()


def _read_unit(t: str, pos: int) -> tuple[str, int]:
    """pos 뒤의 단위를 읽는다. 단위가 아니면 ('', pos). 띄어 쓴 뒤 한글이 이어지면(예: '7 일정') 단위로 보지 않는다."""
    ws_end = _WS.match(t, pos).end()
    spaced = ws_end > pos
    m = _UNIT_AT.match(t, ws_end)
    if not m:
        return "", pos
    unit, end = m.group(1), m.end()
    if unit != "%":
        if _NOT_UNIT_WORD.match(t, ws_end):
            return "", pos
        nxt = t[end:end + 1]
        if nxt and nxt.isalpha():
            if unit.isascii() and nxt.isascii():
                return "", pos              # 'gb' · 'pt' 처럼 영문이 이어지는 것은 단위가 아니다
            if spaced and not _UNIT_TAIL_OK.match(t, end):
                return "", pos              # '7 일정' · '100 원금' — 띄어 쓴 뒤 다른 낱말이 이어짐
    unit = unit.lower() if unit.isascii() else unit
    return _UNIT_ALIAS.get(unit, unit), end


def extract_quantities(text: str) -> list[Quantity]:
    text = text[:MAX_ANALYZED_CHARS]
    folded = _fold(text)
    folded = _mask(folded, _IDENT_PRE)               # 심의필 유효기간·기준일은 날짜보다 먼저 지운다
    dates, blanked = _dates(folded, text)
    blanked = _mask(blanked, _IDENT)                 # 전화·심의번호·URL·버전 등은 혜택 수치가 아니다
    out: list[Quantity] = list(dates)
    toks: list[dict] = []
    consumed = 0
    for m in _NUM_TOKEN.finditer(blanked):
        if m.start() < consumed:
            continue                                  # '1만5천원' 의 '5천' 처럼 앞 숫자가 이미 읽은 부분
        first = float(m.group(1).replace(",", ""))
        value, num_end, mult_run = _read_amount(blanked, m.end(), first)
        unit, end = _read_unit(blanked, num_end)
        glued = m.start() > 0 and "가" <= blanked[m.start() - 1] <= "힣"
        if glued and unit not in _GLUED_UNITS:
            continue                                  # '코스피200' · '제2026호' — 이름·번호는 숫자로 읽지 않는다
        consumed = max(end, m.end())
        toks.append({"start": m.start(), "end": end, "value": value, "unit": unit, "mult": mult_run, "digits": re.sub(r"\D", "", m.group(1))})
    # 범위('4.5~15.0%' · '50-60%' · '1~2만원') 의 앞 숫자는 단위가 없으면 뒤 숫자의 단위를 이어받는다
    for i, tk in enumerate(toks[:-1]):
        nx = toks[i + 1]
        if not tk["unit"] and not tk["mult"] and nx["unit"] and _RANGE_BETWEEN.match(blanked[tk["end"]:nx["start"]]):
            tk["unit"] = nx["unit"]
            if nx["mult"]:
                tk["value"] *= _mult_value(nx["mult"])
                tk["mult"] = nx["mult"]
    for tk in toks:
        unit, mult = tk["unit"], tk["mult"]
        cls = _UNIT_CLASS.get(unit, "bare") if unit else "bare"
        if cls == "bare" and not mult and len(tk["digits"]) < 3:
            continue  # 단위 없는 한두 자리 숫자는 무시 (예: '3개 중 1')
        cycle = _cycle_of(blanked, tk["start"], tk["end"]) if cls in ("krw", "percent") else ""
        out.append(Quantity(value=tk["value"], cls=cls, unit=unit, sense=_sense(blanked, tk["start"], tk["end"]),
                            start=tk["start"], end=tk["end"], raw=text[tk["start"]:tk["end"]], cycle=cycle))
    out.sort(key=lambda q: q.start)
    return out


def extract_numbers(text: str) -> list[tuple[str, str, int, int]]:
    """하위 호환: (정규화 숫자, 단위, start, end)."""
    return [(q.raw.replace(",", "").replace(" ", ""), q.unit, q.start, q.end) for q in extract_quantities(text)]


def _supported(q: Quantity, pool: list[Quantity]) -> bool:
    return any(p.supports(q) for p in pool)


# ─────────────────────────── 조건 비교 (코드가 확인하는 여섯 종류만) ───────────────────────────
# 부정: 조건 표식 바로 뒤에 '없음·않음·아님·안 함' 같은 말이 오면 조건이 아니라 조건이 없다는 말이다 ('제외 품목 없음', '자동결제 없이', '자동결제 되지 않아요',
# '제외 상품이 없어요', '심사 불필요', '자동결제 안 해요', '자동결제 NO'). 이 목록에 있는 표현만 부정으로 읽는다 — 그 밖의 돌려 말하기는 의미 심사와 사람의 몫이다.
_NEG_WORD = (r"(?:없|않|아님|아닙|아니|아닌|안\s*(?:돼|됩|되|해|합|함|하)|불가|제로|0\s*건|면제|생략|불필요|필요\s*없|미적용|미부과|미청구|해당\s*없"
             r"|해제\s*(?:됨|돼|되|됐)|no(?![A-Za-z]))")
_NEG_NOUN = r"(?:품목|상품|대상|항목|걱정|부담|절차|과정|조건|요금|비용|결제|과금|탈락|거절)"
_NEG_PART = r"(?:이|가|은|는|도|를|을|엔|에는)"
_NEGATED_AFTER = re.compile(
    rf"^\s*(?:{_NEG_PART}\s*)?(?:(?:되는|하는|되어|하여)\s*)?(?:{_NEG_NOUN}\s*){{0,2}}(?:{_NEG_PART}\s*)?(?:(?:되지|돼지|하지|이루어지지)\s*)?{_NEG_WORD}[가-힣]*",
    re.IGNORECASE)
_CANCEL_NEAR = re.compile(r"해지|취소|환불|위약")   # '자동결제 없이 해지 가능' 처럼 해지·취소를 말하는 부정은 조건이 반대라는 뜻이 아니다
_AUTOPAY = re.compile(r"자동\s*(?:으로\s*)?(?:결제|갱신|연장|청구)|유료\s*(?:전환|결제)|정기\s*결제|과금")
_AMT_NUM = r"(?:\d{1,3}(?:,\d{3}){1,4}|\d{1,15})(?:\.\d{1,6})?"      # 길이를 제한해, 긴 숫자 나열에서 정규식 비용이 늘어나지 않게 한다
_AMOUNT = rf"(?<![\d,.])((?:{_AMT_NUM}\s*[십백천만억]+\s*){{0,4}}{_AMT_NUM}\s*[십백천만억]*)\s*원"
_MIN_AMOUNT = re.compile(_AMOUNT + r"\s*(이상|초과|이하|미만)")
_DL_CORE = (r"(?:(?P<y>\d{4})\s*[.\-/년]\s*(?P<m>\d{1,2})\s*[.\-/월]\s*(?P<d>\d{1,2})"
            r"|(?P<m2>\d{1,2})\s*[./월]\s*(?P<d2>\d{1,2}))")
_DL_MID = (r"\s*일?\s*(?:\(\s*[월화수목금토일]\s*\)\s*)?"
           r"(?:(?:\d{1,2}\s*[:시]\s*(?:\d{1,2}\s*분?)?|자정|정오|오전\s*\d{1,2}\s*시|오후\s*\d{1,2}\s*시)\s*)?(?:에\s*)?")
_DEADLINE_DATE = re.compile(rf"(?<![\d.]){_DL_CORE}{_DL_MID}(?P<mark>까지|종료|마감)")
_DEADLINE_RANGE = re.compile(r"(?<![\d.])(\d{1,2})\s*월\s*(\d{1,2})\s*일?\s*[~\-–—〜]\s*(?:(\d{1,2})\s*월\s*)?(\d{1,2})\s*일?\s*(까지|종료|마감)")
_DEADLINE_DUR = re.compile(r"(?<![\d.])(\d{1,6})\s*(일|개월|달|주)\s*(까지|이내)")
_LIMIT_PER_PERSON = re.compile(r"(?<!\d)1\s*인\s*(?:당|최대)?\s*(?:최대\s*)?(\d{1,6})\s*회|(?:고객|회원|계정|아이디|ID|인|사람)\s*당\s*(?:최대\s*)?(\d{1,6})\s*회", re.IGNORECASE)
_LIMIT_AD_ONLY = re.compile(r"(?<!\d)(\d{1,6})\s*회\s*(?:한정|만|씩)|한\s*번만")
_EXCLUDE = re.compile(r"(?<![가-힣A-Za-z])(?:(?:일부|특정)[ \t]*)?(?:[가-힣A-Za-z]{1,10}[ \t]*)?제외")
_REVIEW = re.compile(r"심사")
_REVIEW_COND = re.compile(r"\s*(?:결과|기준|내용)?\s*(?:에\s*(?:따라|따른|의해)|별로)")


@dataclass(frozen=True)
class Cond:
    kind: str
    value: Optional[tuple]   # 값을 비교하는 조건의 값. None = 있는지·뜻만 본다
    negated: bool            # '없음' 으로 부정된 표현
    raw: str
    start: int
    end: int
    binding: bool = True     # 사실 쪽에서 광고가 반드시 지켜야 하는 조건인가 (False = 비교용 참고값)


def _neg_len(text: str, end: int) -> int:
    m = _NEGATED_AFTER.match(text[end:end + 24])
    return m.end() if m else 0


def _krw(expr: str) -> Optional[float]:
    """'1만5천' · '5천만' · '50,000' → 원 단위 값. 큰 단위부터 작아지는 조합만 하나로 더한다 ('3만 5만' 같은 조합은 읽지 않는다)."""
    total, last = 0.0, None
    for num, mult in re.findall(rf"({_NUMBER})\s*([십백천만억]*)", expr):
        v = float(num.replace(",", ""))
        mv = _mult_value(mult) if mult else 1.0
        if last is not None and mv >= last:
            return None
        total += v * mv
        last = mv
    return total if last is not None else None


def _cond(kind: str, orig: str, start: int, end: int, value=None, negated: bool = False, binding: bool = True) -> Cond:
    """공백을 뺀 위치로 만든다 — raw 와 위치가 copy.full_text()[start:end] 와 같고 줄을 넘지 않는다."""
    while start < end and orig[start].isspace():
        start += 1
    while end > start and orig[end - 1].isspace():
        end -= 1
    return Cond(kind, value, negated, orig[start:end], start, end, binding)


def _simple(kind: str, folded: str, orig: str, pat: re.Pattern) -> list[Cond]:
    out = []
    for m in pat.finditer(folded):
        n = _neg_len(folded, m.end())
        out.append(_cond(kind, orig, m.start(), m.end() + n, negated=bool(n)))
    return out


def _c_autopay(folded: str, orig: str, is_fact: bool) -> list[Cond]:
    return _simple("자동결제·유료 전환", folded, orig, _AUTOPAY)


def _c_review(folded: str, orig: str, is_fact: bool) -> list[Cond]:
    out = []
    for m in _REVIEW.finditer(folded):
        n = _neg_len(folded, m.end())
        pre_neg = folded[max(0, m.start() - 1):m.start()] == "무"   # '무심사'
        negated = bool(n) or pre_neg
        cond = _REVIEW_COND.match(folded, m.end())
        if not negated and not cond:
            continue  # '심사 완료 시 입금'·'간편 심사' 같은 말은 '심사 결과에 따라' 라는 조건을 말한 것이 아니다 (사실·광고 모두)
        start = m.start() - 1 if pre_neg else m.start()
        end = m.end() + (n if negated else (cond.end() - m.end()))
        out.append(_cond("심사 조건", orig, start, end, negated=negated))
    return out


def _c_min_amount(folded: str, orig: str, is_fact: bool) -> list[Cond]:
    out = []
    for m in _MIN_AMOUNT.finditer(folded):
        value = _krw(m.group(1))
        if value is None:
            continue
        comp = m.group(2)
        # 사실 쪽의 '이상·초과' 는 광고가 지켜야 하는 구매 하한이다. '이하·미만' 은 비교용 참고값
        out.append(_cond("최소 금액 조건", orig, m.start(), m.end(), value=(value, comp), binding=(comp in ("이상", "초과")) if is_fact else True))
    return out


def _c_deadline(folded: str, orig: str, is_fact: bool) -> list[Cond]:
    out = []
    spans: list[tuple[int, int]] = []
    for m in _DEADLINE_RANGE.finditer(folded):          # '10월 1일 ~ 31일까지' → 마감은 31일
        m1, d1, m2, d2 = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
        month = int(m2) if m2 else m1
        if not (1 <= month <= 12 and 1 <= d2 <= 31) or (is_fact and m.group(5) != "까지"):
            continue
        out.append(_cond("기간 한정", orig, m.start(), m.end(), value=("date", month * 100 + d2, 0)))
        spans.append((m.start(), m.end()))
    rest = _blank(folded, spans)
    for m in _DEADLINE_DATE.finditer(rest):
        if m.group("y"):
            year, month, day = int(m.group("y")), int(m.group("m")), int(m.group("d"))
        else:
            year, month, day = 0, int(m.group("m2")), int(m.group("d2"))
        if not (1 <= month <= 12 and 1 <= day <= 31) or (is_fact and m.group("mark") != "까지"):
            continue
        out.append(_cond("기간 한정", orig, m.start(), m.end(), value=("date", month * 100 + day, year)))
        spans.append((m.start(), m.end()))
    rest = _blank(folded, spans)
    for m in _DEADLINE_DUR.finditer(rest):
        if is_fact and m.group(3) != "까지":
            continue          # 사실 쪽의 '3일 이내' 는 배송·처리 기간 안내일 수 있어 조건으로 보지 않는다 (광고 쪽 '30일 이내' 는 '30일까지' 와 같은 기간으로 읽는다)
        unit = "개월" if m.group(2) == "달" else m.group(2)
        out.append(_cond("기간 한정", orig, m.start(), m.end(), value=("dur", float(m.group(1)), unit)))
    return sorted(out, key=lambda c: c.start)


def _c_limit(folded: str, orig: str, is_fact: bool) -> list[Cond]:
    out = []
    for m in _LIMIT_PER_PERSON.finditer(folded):
        n = m.group(1) or m.group(2)
        out.append(_cond("1인 1회", orig, m.start(), m.end(), value=(float(n),)))
    if not is_fact:
        for m in _LIMIT_AD_ONLY.finditer(folded):
            n = m.group(1) or "1"
            out.append(_cond("1인 1회", orig, m.start(), m.end(), value=(float(n),)))
    return sorted(out, key=lambda c: c.start)


def _c_exclude(folded: str, orig: str, is_fact: bool) -> list[Cond]:
    return _simple("제외 조건", folded, orig, _EXCLUDE)


# 코드가 사실과 광고를 비교하는 조건은 이 여섯 종류가 전부다 (그 밖의 조건은 의미 심사와 사람의 몫).
_CONDITIONS: tuple[tuple[str, Callable[[str, str, bool], list[Cond]]], ...] = (
    ("자동결제·유료 전환", _c_autopay), ("심사 조건", _c_review), ("최소 금액 조건", _c_min_amount),
    ("기간 한정", _c_deadline), ("1인 1회", _c_limit), ("제외 조건", _c_exclude),
)
CONDITION_KINDS = tuple(name for name, _ in _CONDITIONS)


@dataclass(frozen=True)
class CondIssue:
    kind: str
    code: str            # "omitted" (COND-01) | "conflict" (COND-02)
    fact: Fact
    fact_raw: str        # 사실 쪽 조건 원문
    ad_raw: str          # 광고 쪽 조건 원문 ('' = 광고에 없음)
    start: int           # 광고 전체 문구(full_text) 안의 위치 (-1 = 위치 없음)
    end: int
    why: str


def _cond_text(c: Cond) -> str:
    return f"'{c.raw}'"


def _same(a: Optional[tuple], b: Optional[tuple]) -> bool:
    """조건 값이 같은가. 날짜는 월·일이 같아야 하고, 둘 다 연도를 말했을 때만 연도도 같아야 한다."""
    if a is None or b is None:
        return a is b
    if a[0] == "date" and b[0] == "date":
        return a[1] == b[1] and (not a[2] or not b[2] or a[2] == b[2])
    return a == b


def _subtype(c: Cond) -> Optional[str]:
    return c.value[0] if c.kind == "기간 한정" and c.value else None   # 마감 날짜(date)와 기간(dur)은 서로 다른 말이라 값을 맞대지 않는다


def condition_issues(copy: AdCopy, all_facts: list[Fact], refs: list[Fact]) -> list[CondIssue]:
    """사실의 조건과 광고(문구+고지)의 조건 비교.

    - conflict: 광고가 같은 종류의 조건을 말하는데 값이 사실의 어느 값과도 다르거나, 뜻이 반대(있다↔없다)인 경우. 사실 전체와 비교한다.
    - omitted: 광고가 가리킨 사실의 조건이 광고에 같은 값으로 없는 경우. 단어만 있다고 충족이 아니다 (값이 다르거나 부정되면 충족이 아니다).
    같은 종류의 조건에 conflict 가 있으면 omitted 는 따로 내지 않는다 (같은 문제를 두 번 말하지 않는다).
    """
    orig = copy.full_text()[:MAX_ANALYZED_CHARS]
    text = _fold(orig)
    out: list[CondIssue] = []
    for name, finder in _CONDITIONS:
        ad = finder(text, orig, False)
        ad_aff = [c for c in ad if not c.negated]
        # 해지·취소·환불을 말하는 부정('해지 수수료 없음')은 조건이 반대라는 말이 아니다
        ad_neg = [c for c in ad if c.negated and not _CANCEL_NEAR.search(text[max(0, c.start - 12):c.end + 12])]
        fact_conds = [(f, c) for f in all_facts for c in finder(_fold(f.text), f.text, True)]
        fact_aff = [(f, c) for f, c in fact_conds if not c.negated]
        fact_neg = [(f, c) for f, c in fact_conds if c.negated]
        conflict = False
        if fact_aff:
            for a in ad_aff:
                if a.value is None:
                    continue
                same_kind = [(f, c) for f, c in fact_aff if _subtype(c) == _subtype(a)]
                if same_kind and not any(_same(a.value, c.value) for _, c in same_kind):
                    f, fc = same_kind[0]
                    out.append(CondIssue(name, "conflict", f, fc.raw, a.raw, a.start, a.end,
                                         f"광고의 {_cond_text(a)} 이(가) 근거 {f.id}('{f.text}')의 {name} {_cond_text(fc)} 과(와) 값이 다름"))
                    conflict = True
            if ad_neg:   # 광고 어딘가에 같은 조건을 긍정하는 말이 함께 있어도, 부정한 말이 있으면 뜻이 반대인 문장이 광고에 있는 것이다
                a, (f, fc) = ad_neg[0], fact_aff[0]
                out.append(CondIssue(name, "conflict", f, fc.raw, a.raw, a.start, a.end,
                                     f"광고는 {_cond_text(a)} 라고 하지만 근거 {f.id}('{f.text}')에는 {name} {_cond_text(fc)} 이(가) 있음 — 뜻이 반대"))
                conflict = True
        if fact_neg and not fact_aff and ad_aff and not ad_neg:
            a, (f, fc) = ad_aff[0], fact_neg[0]
            out.append(CondIssue(name, "conflict", f, fc.raw, a.raw, a.start, a.end,
                                 f"광고는 {_cond_text(a)} 를 말하지만 근거 {f.id}('{f.text}')는 {_cond_text(fc)} (없음) — 뜻이 반대"))
            conflict = True
        if conflict:
            continue
        for f in refs:
            for fc in finder(_fold(f.text), f.text, True):
                if fc.negated or not fc.binding:
                    continue
                if any(_same(a.value, fc.value) for a in ad_aff):
                    continue
                shown = "같은 값으로 " if fc.value is not None else ""
                out.append(CondIssue(name, "omitted", f, fc.raw, "", -1, -1,
                                     f"근거 {f.id}('{f.text}')의 조건 '{fc.raw}'({name})이 광고 문구·고지 어디에도 {shown}없음"))
                break  # 사실 하나에서 같은 종류는 한 번만
    return out


def claim_supported_by(claim_text: str, fact: Fact) -> tuple[bool, str]:
    """사실이 주장을 실제로 뒷받침하는가. (지원 여부, 이유)"""
    cq = extract_quantities(claim_text)
    fq = extract_quantities(fact.text)
    if cq:
        missing = [q.raw for q in cq if not _supported(q, fq)]
        if missing:
            return False, f"주장의 수치 {', '.join(missing)} 가 {fact.id} 에 없거나 단위·뜻이 다름"
        return True, "수치 일치"
    ctoks = [t for t in _TOKEN.findall(loose_text(claim_text)) if t not in _STOP]
    ftext = loose_text(fact.text)
    ftoks = set(_TOKEN.findall(ftext))
    if not ctoks:
        return True, "비교할 어휘 없음"
    hit = sum(1 for t in ctoks if t in ftext or any(t in f or f in t for f in ftoks))
    if hit == 0:
        return False, f"주장의 핵심 어휘({', '.join(ctoks[:4])})가 {fact.id} 에 없음"
    return True, f"어휘 겹침 {hit}/{len(ctoks)}"


def _vocab_overlap(text: str, fact_text: str) -> int:
    ftext = loose_text(fact_text)
    ftoks = set(_TOKEN.findall(ftext))
    toks = {t for t in _TOKEN.findall(loose_text(text)) if t not in _STOP}
    return sum(1 for t in toks if t in ftext or any(t in f or f in t for f in ftoks))


def referenced_facts(copy: AdCopy, product: ProductFacts) -> list[Fact]:
    """광고가 근거로 가리킨 사실 + 숫자로 끌어다 쓴 사실 (수정안·직접 편집한 문구처럼 claims 가 없어도 찾는다). 고지 문구의 숫자도 본다."""
    by_id = {f.id: f for f in product.facts}
    refs: dict[str, Fact] = {}
    for c in copy.claims:
        if c.fact_id in by_id:
            refs[c.fact_id] = by_id[c.fact_id]
    ad_q = extract_quantities(copy.full_text())
    for f in product.facts:
        if f.id in refs:
            continue
        fq = extract_quantities(f.text)
        if any(_supported(q, fq) for q in ad_q):
            refs[f.id] = f
    # 고쳐 쓰면서 주장 연결이 끊겨도, 원래 광고가 근거로 삼은 사실과 낱말이 겹치는 문구는 그 사실의 조건을 계속 지켜야 한다
    for fid in getattr(copy, "carried_fact_ids", []):
        if fid in by_id and fid not in refs and _vocab_overlap(copy.full_text(), by_id[fid].text) > 0:
            refs[fid] = by_id[fid]
    return list(refs.values())


def omitted_conditions(copy: AdCopy, facts: list[Fact]) -> list[tuple[Fact, str, str]]:
    """참조된 사실의 조건이 광고(문구+고지)에 같은 값으로 없는 경우. (사실, 조건 이름, 조건 원문)"""
    return [(i.fact, i.kind, i.fact_raw) for i in condition_issues(copy, facts, facts) if i.code == "omitted"]


def check(copy: AdCopy, product: ProductFacts, landing_text: str = "", catalog: PolicyCatalog | None = None) -> GroundingResult:
    catalog = catalog or get_catalog()
    res = GroundingResult()
    text = copy.full_text()   # 문구·CTA 와 함께 고지 문구까지 본다 — 고지에 쓴 금액·기간도 광고가 하는 말이다
    facts_q = [q for f in product.facts for q in extract_quantities(f.text)]
    landing_q = extract_quantities(landing_text) if landing_text else []
    num_rule = catalog.get("GRND-NUM-01")
    land_rule = catalog.get("TA-LANDING-01")
    claim_rule = catalog.get("GRND-CLAIM-01")
    cond_rule = catalog.get("GRND-COND-01")
    diff_rule = catalog.get("GRND-COND-02")

    refs = referenced_facts(copy, product)
    res.referenced_fact_ids = [f.id for f in refs]
    issues = condition_issues(copy, product.facts, refs) if (cond_rule is not None or diff_rule is not None) else []
    conflict_spans = [(i.start, i.end) for i in issues if i.code == "conflict" and i.start >= 0]

    # 1) 숫자: 값 전체 + 단위 종류 + 주기 + (있으면) 뜻 이 같은 수치가 사실/랜딩에 있어야 한다
    ad_q = extract_quantities(text)
    for q in ad_q:
        res.numbers_checked += 1
        in_facts = _supported(q, facts_q)
        in_landing = bool(landing_q) and _supported(q, landing_q)
        if not in_facts and not in_landing and num_rule is not None:
            if _overlaps(q.start, q.end, conflict_spans):
                continue  # 같은 자리를 GRND-COND-02 가 '조건 값이 다름' 으로 이미 말한다 — 같은 문제를 두 번 적지 않는다
            same_value = [p for p in facts_q + landing_q if abs(p.value - q.value) < 1e-9]
            cycle_only = [p for p in same_value if p.cls == q.cls and p.cycle and q.cycle and p.cycle != q.cycle]
            if cycle_only:
                why = f"'{q.raw}'({q.cycle} 기준) 와 값은 같지만 사실은 {cycle_only[0].cycle} 기준({cycle_only[0].raw})임"
            elif same_value:
                why = f"'{q.raw}' 와 값은 같지만 단위·뜻이 다른 수치({', '.join(sorted({p.raw for p in same_value}))})만 있음"
            else:
                why = f"'{q.raw}' 가 사실 목록·랜딩 어디에도 없음"
            res.numbers_unsupported.append(q.raw)
            res.findings.append(Finding(rule_id=num_rule.id, severity=num_rule.severity, layer=Layer.GROUNDING,
                                        quote=q.raw, start=q.start, end=q.end, explanation=why, verified_quote=True))
        elif landing_q and not in_landing and land_rule is not None:
            res.landing_mismatch.append(q.raw)
            res.findings.append(Finding(rule_id=land_rule.id, severity=land_rule.severity, layer=Layer.GROUNDING,
                                        quote=q.raw, start=q.start, end=q.end,
                                        explanation=f"'{q.raw}' 가 랜딩 페이지 텍스트에서 확인되지 않음", verified_quote=True))

    # 2) 주장 → fact_id: 존재해야 하고, 그 사실이 주장을 실제로 뒷받침해야 한다
    by_id = {f.id: f for f in product.facts}
    for c in copy.claims:
        if claim_rule is None:
            break
        if c.fact_id is None:
            res.claims_unsupported.append(c.text)
            res.findings.append(Finding(rule_id=claim_rule.id, severity=claim_rule.severity, layer=Layer.GROUNDING,
                                        quote=c.text, start=-1, end=-1, explanation="상품 정보의 어느 항목을 근거로 하는지 표시되지 않은 주장", verified_quote=True))
        elif c.fact_id not in by_id:
            res.claims_unsupported.append(c.text)
            res.findings.append(Finding(rule_id=claim_rule.id, severity=claim_rule.severity, layer=Layer.GROUNDING,
                                        quote=c.text, start=-1, end=-1,
                                        explanation=f"상품 정보에 없는 항목 '{c.fact_id}' 을 근거로 가리킴 (모델이 지어낸 근거)", verified_quote=True))
        else:
            ok, why = claim_supported_by(c.text, by_id[c.fact_id])
            if not ok:
                res.claims_unsupported.append(c.text)
                res.findings.append(Finding(rule_id=claim_rule.id, severity=claim_rule.severity, layer=Layer.GROUNDING,
                                            quote=c.text, start=-1, end=-1,
                                            explanation=f"근거로 지정한 {c.fact_id}('{by_id[c.fact_id].text}')가 이 주장을 뒷받침하지 않음 — {why}",
                                            verified_quote=True))

    # 3) 조건: 사실의 조건이 광고에 빠졌는가 (GRND-COND-01) · 광고의 조건이 사실과 다른가 (GRND-COND-02)
    seen: set[tuple[str, str, str]] = set()
    for i in issues:
        key = (i.fact.id, i.kind, i.code)
        if key in seen:
            continue
        seen.add(key)
        if i.code == "conflict":
            if diff_rule is None:
                continue
            res.conditions_conflict.append(f"{i.fact.id}: 광고 '{i.ad_raw}' ≠ 사실 '{i.fact_raw}'")
            res.findings.append(Finding(rule_id=diff_rule.id, severity=diff_rule.severity, layer=Layer.GROUNDING,
                                        quote=i.ad_raw, start=i.start, end=i.end, explanation=i.why, verified_quote=True))
            continue
        if cond_rule is None:
            continue
        res.conditions_omitted.append(f"{i.fact.id}: {i.fact_raw}")
        # 인용은 그 사실을 끌어다 쓴 광고 구절 — 숫자가 있으면 그 숫자, 없으면 주장 텍스트
        quote, start, end = "", -1, -1
        fq = extract_quantities(i.fact.text)
        for q in ad_q:
            if _supported(q, fq):
                quote, start, end = q.raw, q.start, q.end
                break
        if not quote:
            for c in copy.claims:
                if c.fact_id == i.fact.id:
                    quote = c.text
                    break
        res.findings.append(Finding(rule_id=cond_rule.id, severity=cond_rule.severity, layer=Layer.GROUNDING,
                                    quote=quote, start=start, end=end, explanation=i.why, verified_quote=True))

    # 4) 랜딩 표현 일치 (토큰 겹침이 낮은 주장)
    if landing_text and land_rule is not None:
        res.landing_checked = True
        land_tokens = set(_TOKEN.findall(loose_text(landing_text)))
        for c in copy.claims:
            core_text = re.sub(r"\([^)]*\)", " ", loose_text(c.text))
            toks = [t for t in _TOKEN.findall(core_text) if t not in _STOP]
            if len(toks) < 2:
                continue
            overlap = sum(1 for t in toks if any(t in lt or lt in t for lt in land_tokens)) / len(toks)
            if overlap < 0.4 and c.text not in res.landing_mismatch:
                res.landing_mismatch.append(c.text)
                res.findings.append(Finding(rule_id=land_rule.id, severity=Severity.WARN, layer=Layer.GROUNDING,
                                            quote=c.text, start=-1, end=-1,
                                            explanation=f"주장 '{c.text}' 의 핵심 어휘가 랜딩에서 확인되지 않음 (겹침 {overlap:.0%})",
                                            verified_quote=True))
    return res
