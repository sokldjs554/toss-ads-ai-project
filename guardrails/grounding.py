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
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.schemas import AdCopy, Fact, Finding, Layer, ProductFacts, Severity
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog

# 숫자 + (한국어 자릿수 단위) + (단위). 예: 5,000만원 / 4.5% / 15일 / 7일 / 19,900원 / 3배 / 5천만원
_NUM = re.compile(r"(?<![A-Za-z가-힣\d.\-])(\d[\d,]*(?:\.\d+)?)(?![\d\-])([십백천만억]*)(?:\s*(%|퍼센트|원|명|배|일|개월|달|년|시간|분|초|회|건|호|개|점|p|포인트|kg|㎏|g))?", re.IGNORECASE)
# 한글에 붙여 쓴 숫자 ('할인5%', '금리4.5%', '월19,900원'). 단위가 있는 경우만, 그리고 혜택을 말하는 단위(% · 원 · 배 · 일 · 개월)만 본다.
# 단위 없는 '코스피200' 이나 '제2026호' 같은 이름·번호는 그대로 무시한다.
_NUM_GLUED = re.compile(r"(?<=[가-힣])(\d[\d,]*(?:\.\d+)?)(?![\d\-])([십백천만억]*)\s*(%|퍼센트|원|배|개월|일)")
# 혜택 수치가 아닌 식별번호: 전화·대표번호, 심의번호, 사업자등록번호, URL·이메일. 찾은 자리는 공백으로 지워 숫자로 읽지 않는다.
_IDENT = re.compile(
    r"https?://\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|제\s*\d[\d\-–]*\s*호"
    r"|(?<![\d\-])(?:0\d{1,2}|1[5-9]\d{2})\s*[-)]\s*\d{3,4}(?:\s*-\s*\d{4})?(?![\d\-])"
    r"|(?<![\d\-])\d{3}-\d{2}-\d{5}(?![\d\-])")
# 날짜: 숫자 대조에서 '31일'·'2026년' 같은 기간 수치로 오인하지 않도록 먼저 잡아 (월, 일) 로 비교한다
_DATE = re.compile(r"(?:(\d{4})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?(?![\d%]|,\d{3}(?!\d))")  # '3월 5,000원' 의 '3월 5' 는 날짜가 아니다
_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
_STOP = {"지금", "바로", "확인", "조건", "상품", "서비스", "가능", "신청", "이용", "고객", "혜택", "제공", "있습니다", "있어요", "합니다", "해요",
         "최대", "최소", "기준", "경우", "통해", "위해", "대한", "모든", "전체"}
_MULT = {"십": 10, "백": 100, "천": 1_000, "만": 10_000, "억": 100_000_000}
_UNIT_CLASS = {"%": "percent", "퍼센트": "percent", "원": "krw", "명": "count", "회": "count", "건": "count", "개": "count", "호": "count",
               "배": "multiple", "일": "duration", "개월": "duration", "달": "duration", "년": "duration", "시간": "duration", "분": "duration",
               "초": "duration", "점": "points", "p": "points", "포인트": "points", "kg": "weight", "㎏": "weight", "g": "weight"}
# 금액·비율의 주기: '월 19,900원' 과 '연 19,900원' 은 같은 수치가 아니다. 앞에 붙은 말('월 ', '매월') 또는 뒤에 붙은 말('/월')로 읽는다.
_CYCLE_PRE = re.compile(r"(?:^|[\s(\[·,/~])(매월|매년|매주|매일|하루|월|연|년|주)\s*$")
_CYCLE_POST = re.compile(r"^\s*/\s*(월|연|년|주|일)(?![가-힣])")
_CYCLE_NAME = {"매월": "월", "월": "월", "매년": "연", "연": "연", "년": "연", "매주": "주", "주": "주", "매일": "일", "하루": "일", "일": "일"}
# 같은 숫자·단위라도 뜻이 다르면 뒷받침이 아니다. 숫자와 같은 구절 안의 뜻 어휘를 '묶음' 으로 비교한다 (할인≈쿠폰≈세일, 금리≈이자…).
_SENSE_GROUPS: dict[str, tuple[str, ...]] = {
    "할인": ("할인", "쿠폰", "세일", "특가"), "금리": ("금리", "이자", "이율"), "수익": ("수익률", "수익", "배당", "성과"),
    "적립": ("적립", "캐시백", "포인트", "리워드"), "수수료": ("보수", "수수료"), "연체": ("연체",), "환급": ("환급",), "배송": ("배송",),
    "체험": ("체험", "무료"), "한도": ("한도",), "보장": ("보장", "보상"), "할부": ("할부",),
}
_SENSE_WORDS = tuple(w for ws in _SENSE_GROUPS.values() for w in ws)
_SENSE_OF = {w: g for g, ws in _SENSE_GROUPS.items() for w in ws}


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

    def supports(self, other: "Quantity") -> bool:
        """self(사실 쪽) 가 other(광고 쪽) 를 뒷받침하는가."""
        if abs(self.value - other.value) > 1e-9:
            return False
        if other.cls == "date" or self.cls == "date":
            return self.cls == other.cls
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


def _dates(text: str) -> tuple[list[Quantity], str]:
    """날짜를 (월*100+일) 값의 date 수량으로 뽑고, 그 자리를 공백으로 지운 텍스트를 돌려준다."""
    out: list[Quantity] = []
    spans: list[tuple[int, int]] = []
    for m in _DATE.finditer(text):
        month, day = int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        out.append(Quantity(value=float(month * 100 + day), cls="date", unit="date", sense=frozenset(), start=m.start(), end=m.end(), raw=text[m.start():m.end()].strip()))
        spans.append((m.start(), m.end()))
    return out, _blank(text, spans)


def _cycle_of(text: str, start: int, end: int) -> str:
    pre = _CYCLE_PRE.search(text[max(0, start - 4):start])
    if pre:
        return _CYCLE_NAME[pre.group(1)]
    post = _CYCLE_POST.match(text[end:end + 6])
    return _CYCLE_NAME[post.group(1)] if post else ""


def extract_quantities(text: str) -> list[Quantity]:
    dates, blanked = _dates(text)
    blanked = _blank(blanked, [(m.start(), m.end()) for m in _IDENT.finditer(blanked)])  # 전화·심의번호 등은 혜택 수치가 아니다
    out: list[Quantity] = list(dates)
    glued = list(_NUM_GLUED.finditer(blanked))
    glued_spans = [(m.start(), m.end()) for m in glued]
    # 붙여 쓴 '월19,900원' 의 '900원' 처럼 이미 읽은 숫자의 일부는 다시 읽지 않는다
    matches = glued + [m for m in _NUM.finditer(blanked) if not _overlaps(m.start(), m.end(), glued_spans)]
    for m in matches:
        raw_num, mult, unit = m.group(1), m.group(2) or "", (m.group(3) or "")
        digits = raw_num.replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        for ch in mult:
            value *= _MULT.get(ch, 1)
        unit_norm = unit.lower()
        if unit_norm == "퍼센트":
            unit_norm = "%"
        cls = _UNIT_CLASS.get(unit_norm, "bare") if unit_norm else ("krw" if mult else "bare")
        if cls == "bare" and not mult and len(digits.replace(".", "")) < 3:
            continue  # 단위 없는 한두 자리 숫자는 무시 (예: '3개 중 1')
        if mult and not unit_norm:
            cls = "bare"  # '5천' 처럼 단위 없는 자릿수 표기 — 값만 비교
        cycle = _cycle_of(blanked, m.start(), m.end()) if cls in ("krw", "percent") else ""
        out.append(Quantity(value=value, cls=cls, unit=unit_norm, sense=_sense(text, m.start(), m.end()),
                            start=m.start(), end=m.end(), raw=text[m.start():m.end()], cycle=cycle))
    out.sort(key=lambda q: q.start)
    return out


def extract_numbers(text: str) -> list[tuple[str, str, int, int]]:
    """하위 호환: (정규화 숫자, 단위, start, end)."""
    return [(q.raw.replace(",", "").replace(" ", ""), q.unit, q.start, q.end) for q in extract_quantities(text)]


def _supported(q: Quantity, pool: list[Quantity]) -> bool:
    return any(p.supports(q) for p in pool)


# ─────────────────────────── 조건 비교 (코드가 확인하는 여섯 종류만) ───────────────────────────
# 부정: 조건 표식 바로 뒤에 '없음·않음·아님' 이 오면 조건이 아니라 조건이 없다는 말이다 ('제외 품목 없음', '자동결제 없이', '자동결제 되지 않아요').
_NEGATED_AFTER = re.compile(
    r"^\s*(?:이|가|은|는|도|를|을)?\s*(?:(?:품목|상품|대상|항목|걱정|부담|절차|과정)\s*)?(?:(?:되지|돼지|하지|이루어지지)\s*)?"
    r"(?:없|않|아님|아닙|아니|안\s*[돼됩]|불가|제로|0\s*건|면제|생략)[가-힣]*")
_AUTOPAY = re.compile(r"자동\s*결제|자동\s*갱신|유료\s*(?:전환|결제)|정기\s*결제|과금")
_AMOUNT = r"(\d[\d,]*(?:\.\d+)?)\s*([십백천만억]*)\s*원"
_MIN_AMOUNT = re.compile(_AMOUNT + r"\s*(이상|초과|이하|미만)")
_DEADLINE_DATE = re.compile(r"(?:(\d{4})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\s*(까지|종료|마감)")
_DEADLINE_DUR = re.compile(r"(?<![\d.])(\d+)\s*(일|개월|달|주)\s*(까지)")
_LIMIT_PER_PERSON = re.compile(r"1\s*인\s*(?:당)?\s*(\d+)\s*회|(?:고객|회원|계정|아이디)\s*당\s*(\d+)\s*회")
_LIMIT_AD_ONLY = re.compile(r"(\d+)\s*회\s*(?:한정|만|씩)|한\s*번만")
_EXCLUDE = re.compile(r"(?:(?:일부|특정)\s*)?[가-힣A-Za-z]*\s*제외")
_REVIEW = re.compile(r"심사")
_REVIEW_COND = re.compile(r"\s*(?:결과|기준)?\s*에\s*따라")


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
    m = _NEGATED_AFTER.match(text[end:end + 16])
    return m.end() if m else 0


def _krw(num: str, mult: str) -> float:
    value = float(num.replace(",", ""))
    for ch in mult:
        value *= _MULT.get(ch, 1)
    return value


def _simple(kind: str, text: str, pat: re.Pattern) -> list[Cond]:
    out = []
    for m in pat.finditer(text):
        n = _neg_len(text, m.end())
        out.append(Cond(kind, None, bool(n), text[m.start():m.end() + n].strip(), m.start(), m.end() + n))
    return out


def _c_autopay(text: str, is_fact: bool) -> list[Cond]:
    return _simple("자동결제·유료 전환", text, _AUTOPAY)


def _c_review(text: str, is_fact: bool) -> list[Cond]:
    out = []
    for m in _REVIEW.finditer(text):
        n = _neg_len(text, m.end())
        pre_neg = text[max(0, m.start() - 1):m.start()] == "무"   # '무심사'
        negated = bool(n) or pre_neg
        cond = _REVIEW_COND.match(text, m.end())
        if not negated and is_fact and not cond:
            continue  # 사실 쪽에서 '심사 완료 시 입금' 같은 말은 조건이 아니다. '심사 결과에 따라' 만 조건.
        start = m.start() - 1 if pre_neg else m.start()
        end = m.end() + (n if negated else (cond.end() - m.end() if cond else 0))
        out.append(Cond("심사 조건", None, negated, text[start:end].strip(), start, end))
    return out


def _c_min_amount(text: str, is_fact: bool) -> list[Cond]:
    out = []
    for m in _MIN_AMOUNT.finditer(text):
        comp = m.group(3)
        out.append(Cond("최소 금액 조건", (_krw(m.group(1), m.group(2)), comp), False, m.group(0), m.start(), m.end(),
                        binding=(comp == "이상") if is_fact else True))
    return out


def _c_deadline(text: str, is_fact: bool) -> list[Cond]:
    out = []
    spans: list[tuple[int, int]] = []
    for m in _DEADLINE_DATE.finditer(text):
        month, day = int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31) or (is_fact and m.group(4) != "까지"):
            continue
        out.append(Cond("기간 한정", ("date", month * 100 + day), False, m.group(0), m.start(), m.end()))
        spans.append((m.start(), m.end()))
    rest = _blank(text, spans)
    for m in _DEADLINE_DUR.finditer(rest):
        unit = "개월" if m.group(2) == "달" else m.group(2)
        out.append(Cond("기간 한정", ("dur", float(m.group(1)), unit), False, text[m.start():m.end()], m.start(), m.end()))
    return sorted(out, key=lambda c: c.start)


def _c_limit(text: str, is_fact: bool) -> list[Cond]:
    out = []
    for m in _LIMIT_PER_PERSON.finditer(text):
        n = m.group(1) or m.group(2)
        out.append(Cond("1인 1회", (float(n),), False, m.group(0), m.start(), m.end()))
    if not is_fact:
        for m in _LIMIT_AD_ONLY.finditer(text):
            n = m.group(1) or "1"
            out.append(Cond("1인 1회", (float(n),), False, m.group(0), m.start(), m.end()))
    return sorted(out, key=lambda c: c.start)


def _c_exclude(text: str, is_fact: bool) -> list[Cond]:
    return _simple("제외 조건", text, _EXCLUDE)


# 코드가 사실과 광고를 비교하는 조건은 이 여섯 종류가 전부다 (그 밖의 조건은 의미 심사와 사람의 몫).
_CONDITIONS: tuple[tuple[str, Callable[[str, bool], list[Cond]]], ...] = (
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


def condition_issues(copy: AdCopy, all_facts: list[Fact], refs: list[Fact]) -> list[CondIssue]:
    """사실의 조건과 광고(문구+고지)의 조건 비교.

    - conflict: 광고가 같은 종류의 조건을 말하는데 값이 사실의 어느 값과도 다르거나, 뜻이 반대(있다↔없다)인 경우. 사실 전체와 비교한다.
    - omitted: 광고가 가리킨 사실의 조건이 광고에 같은 값으로 없는 경우. 단어만 있다고 충족이 아니다 (값이 다르거나 부정되면 충족이 아니다).
    같은 종류의 조건에 conflict 가 있으면 omitted 는 따로 내지 않는다 (같은 문제를 두 번 말하지 않는다).
    """
    text = copy.full_text()
    out: list[CondIssue] = []
    for name, finder in _CONDITIONS:
        ad = finder(text, False)
        ad_aff = [c for c in ad if not c.negated]
        ad_neg = [c for c in ad if c.negated]
        fact_conds = [(f, c) for f in all_facts for c in finder(f.text, True)]
        fact_aff = [(f, c) for f, c in fact_conds if not c.negated]
        fact_neg = [(f, c) for f, c in fact_conds if c.negated]
        conflict = False
        if fact_aff:
            known = {c.value for _, c in fact_aff}
            for a in ad_aff:
                if a.value is not None and a.value not in known:
                    f, fc = fact_aff[0]
                    out.append(CondIssue(name, "conflict", f, fc.raw, a.raw, a.start, a.end,
                                         f"광고의 {_cond_text(a)} 이(가) 근거 {f.id}('{f.text}')의 {name} {_cond_text(fc)} 과(와) 값이 다름"))
                    conflict = True
            if ad_neg and not ad_aff:
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
            for fc in finder(f.text, True):
                if fc.negated or not fc.binding:
                    continue
                if any(a.value == fc.value for a in ad_aff):
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
