"""대화형 캠페인 셋업의 결정론 추출기 (한국어 규칙 기반) — 키 없이, 브라우저(stlite)에서도 돈다.

사용자의 한 번의 말에서 슬롯 후보(`Candidate`)를 뽑는다. 모든 후보는 사용자 말에 **글자 그대로 있는 구절(quote)** 을 근거로 가진다 —
구절을 뗄 수 없는 값은 후보가 되지 못한다. 모델 경로(`Task.CONVERSE`)의 값도 같은 규칙으로 검증한다(`agents/conversation.py`).

이 모듈이 하는 일은 '말 → 후보' 변환뿐이다. 후보를 슬롯에 합치는 일(충돌 / 명시적 수정 구분), 플랫폼 제약 점검, 되묻기, 플랜은 `agents/conversation.py` 가 맡는다.

이 모듈의 정확도가 의미하지 않는 것
- 이 추출기가 `evals/datasets/dialogs.jsonl` 에서 낸 숫자는 **규칙 기반 추출기의 값**이다. 실제 언어 모델의 이해력이 아니고, 실제 광고주의 말투로 검증한 값도 아니다.
  평가 대화는 이 프로젝트 저자가 직접 쓴 가상 대화다(독립 라벨러 없음). 처음 보는 말투·사투리·긴 서술은 놓칠 수 있다.
- 여기 있는 숫자 상수는 이 프로젝트가 가정한 값이다 (`ASSUMED_*`). 토스·토스애즈의 실제 최소 예산이나 내부 기준이 아니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from core.schemas import Objective, ProductCategory
from guardrails.policy import PolicyCatalog, get_catalog

# ---------------------------------------------------------------------------
# 가정한 값 (이 프로젝트가 가정한 값 — 실제 플랫폼 기준 아님)
# ---------------------------------------------------------------------------
ASSUMED_DAYS_PER_MONTH = 30            # 하루 예산 → 월 예산 환산에 쓰는 일수 (가정)
ASSUMED_WEEKS_PER_MONTH = 4            # 주 예산 → 월 예산 환산에 쓰는 주수 (가정)
ASSUMED_MIN_BUDGET_PER_AD_SET_KRW = 300_000   # 광고 세트 하나가 학습·집행에 필요하다고 가정한 월 최소 예산 (가정)
ASSUMED_MIN_MONTHLY_BUDGET_KRW = 600_000      # 이 값 미만이면 세트를 둘 이상으로 나눌 수 없다고 본다 (= 세트 2개 × 위 값, 가정)
ASSUMED_MAX_AD_SETS = 3                # 플래너 프롬프트가 요구하는 세트 수 상한(2~3개)에 맞춘 이 프로젝트의 가정
MIN_BUDGET_STATEMENT_KRW = 10_000      # 이보다 작은 금액은 '예산' 으로 읽지 않고 단위를 되묻는다 (단위 착오 가능성)
MIN_BUDGET_WITHOUT_ANCHOR_KRW = 100_000  # '월 N원' 만 있고 '예산' 이라는 말이 없을 때 예산으로 읽는 최소 금액 (월 9,900원 구독료 같은 값을 거르려는 가정)
MAX_BUDGET_KRW = 100_000_000_000       # 1,000억 원. 이보다 크면 입력 착오로 보고 되묻는다
MAX_QUOTE_CHARS = 200
NOTE_NO_PERIOD = "기간을 말하지 않아 한 달 예산으로 이해했어요"

_REF_YEAR = 2024                       # 일수 계산용 기준 연도 (실제 연도가 아니라 윤년 날짜도 읽기 위한 가정)
_MONTH_DAYS = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

# ---------------------------------------------------------------------------
# 후보·결과 구조
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    slot: str
    value: Any
    quote: str                    # 사용자 말에 글자 그대로 있는 구절
    status: str = "confirmed"     # confirmed | inferred
    explicit_update: bool = False  # '…로 바꿔줘' 처럼 이전 값을 고치라는 뜻이 보이는가
    note: str = ""                # 화면에 보일 가정 설명 (예: 하루 예산을 30일로 곱한 값)
    start: int = -1
    end: int = -1
    delta: Optional[dict] = None  # 상대 수정(예: 예산 50만원 늘려줘) — {"op": "add"|"mul", "amount"|"factor": …}
    weak: bool = False            # 낱말 단서만으로 짐작한 값 — 이미 있는 값을 덮거나 충돌을 만들지 못한다 (명시적 수정 표지가 있을 때만 덮는다)
    needs_confirm: bool = False   # 모델이 의미로 옮긴 값 — 사용자가 맞다고 해야 확정된다


@dataclass
class Ambiguity:
    slot: str
    kind: str                     # vague | range | unit | total_period | multiple
    quote: str = ""
    detail: str = ""


@dataclass
class ExtractContext:
    last_asked: frozenset = frozenset()   # 직전 턴에 되물은 슬롯 — 짧은 대답('300만원이요')을 그 슬롯에 붙이는 데 쓴다
    has_budget: bool = False              # 이미 예산이 있는가 (상대 수정 해석용)


@dataclass
class Extraction:
    candidates: list[Candidate] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    affirmative: bool = False     # 짧은 긍정 답('네', '맞아요')
    negative: bool = False        # 짧은 부정 답('아니요')
    choice: Optional[int] = None  # '첫 번째', '두 번째' (0, 1)
    consumed: list[tuple[int, int]] = field(default_factory=list)

    def for_slot(self, slot: str) -> list[Candidate]:
        return [c for c in self.candidates if c.slot == slot]


# ---------------------------------------------------------------------------
# 한국어 숫자
# ---------------------------------------------------------------------------
_HD = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_SMALL = {"십": 10, "백": 100, "천": 1000}
_BIG = {"만": 10_000, "억": 100_000_000}


def parse_amount(s: str) -> Optional[int]:
    """'300만', '3백만', '삼백만', '1억 5천만', '2,000,000', '1.5억' → 원 단위 정수. 읽을 수 없으면 None."""
    s = s.replace(",", "").replace(" ", "").replace(" ", "")
    if s.endswith("원"):
        s = s[:-1]
    if not s:
        return None
    total = 0.0
    section = 0.0
    cur: Optional[float] = None
    eok_seen = False        # 억을 지났는가 — '1억5천' 은 '1억 5천만' 이라는 구어 표기를 따로 읽는다
    man_after_eok = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isdigit() or ch == ".":
            j = i
            while j < len(s) and (s[j].isdigit() or s[j] == "."):
                j += 1
            try:
                cur = float(s[i:j])
            except ValueError:
                return None
            i = j
            continue
        if ch in _HD:
            cur = float(_HD[ch])
        elif ch in _SMALL:
            section += (cur if cur is not None else 1.0) * _SMALL[ch]
            cur = None
        elif ch in _BIG:
            val = section + (cur if cur is not None else 0.0)
            if val == 0:
                val = 1.0
            total += val * _BIG[ch]
            section, cur = 0.0, None
            if ch == "억":
                eok_seen = True
            elif eok_seen:
                man_after_eok = True
        else:
            return None
        i += 1
    if eok_seen and not man_after_eok and section > 0 and section + (cur or 0.0) < 10_000:
        total += (section + (cur or 0.0)) * 10_000      # '1억5천' → 1억 5천만 (뒤의 '만' 을 줄여 말하는 표기)
    else:
        total += section + (cur if cur is not None else 0.0)
    return int(round(total))


_AMT = re.compile(
    r"""(?<![\d,.])
    (?:
        (?P<ascii>\d[\d,]*(?:\.\d+)?(?:\s*(?:억|천|백|십|만)(?:\s*\d[\d,]*(?:\.\d+)?)?)*)
        |
        (?<![가-힣])(?P<hangul>(?:[일이삼사오육칠팔구]?(?:억|천|백|십|만))+[일이삼사오육칠팔구]?)(?=\s*원)
    )
    (?P<won>\s*원)?""",
    re.X,
)
_NOT_AMOUNT_AFTER = re.compile(r"^\s*(?:명|개|건|회|번|배|세|살|년|월|주|시|분|초|%|퍼센트|곳|위|등|대|단계|가지|장|권|대)")
_ENG_MILLION = re.compile(r"(?<![\w.])(?P<n>\d+(?:\.\d+)?)\s*(?P<u>million|mil|m)\b\s*(?:krw|won|원)?", re.I)

_CTX_MONTHLY = re.compile(r"(?:^|[\s,(\[])(?:월|한\s*달|한달|매월|월간|이번\s*달|달)\s*(?:평균|기준)?\s*(?:예산|광고비|집행비?)?\s*(?:은|는|이|가|에|에는|당|으로|로|도|약|대략|최대|최소|정도|쯤|씩)*\s*$")
_CTX_DAILY = re.compile(r"(?:^|[\s,(\[])(?:하루|일일|매일|(?<!월)(?<!월 )1일|일)\s*(?:평균|기준)?\s*(?:예산|광고비|집행비?)?\s*(?:은|는|이|가|에|에는|당|으로|로|도|약|대략|최대|최소|정도|쯤|씩)*\s*$")
_CTX_WEEKLY = re.compile(r"(?:^|[\s,(\[])(?:주|매주|한\s*주|일주일|주간)\s*(?:평균|기준)?\s*(?:예산|광고비|집행비?)?\s*(?:은|는|이|가|에|에는|당|으로|로|도|약|대략|최대|최소|정도|쯤|씩)*\s*$")
_CTX_TOTAL = re.compile(r"(?:총|합계|전체|총예산|총 예산|통틀어)\s*(?:예산|광고비|집행비?)?\s*(?:은|는|이|가|에|으로|로|도|약|대략|최대|정도)*\s*$")
_CTX_PERIOD_BEFORE = re.compile(r"(?P<n>\d+|한|두|세|네)\s*(?P<u>주일|주|개월|달|일)\s*(?:간|동안|정도|짜리)?\s*(?:총|합계|전체)?\s*(?:은|는|에|으로|로|도|약|대략|최대|정도)*\s*$")
_STRONG_ANCHOR = re.compile(r"예산|광고비|집행|마케팅비|마케팅\s*비용|광고\s*비용|budget", re.I)
_WEAK_ANCHOR = re.compile(r"쓸게|쓸\s*거|쓸\s*예정|쓰려|쓸\s*수|쓸까|투입|사용할|지출|책정|투자할|태울|잡고\s*있|잡았")
_NONBUDGET = re.compile(r"한도|연회비|수수료|할인|캐시백|적립|혜택|지급|가격|판매가|상품권|금액|대출금|보험료|이상|이하|미만|환급|쿠폰|배송비|보증금|월세|연봉|급여|소득|매출|수익|이자|원금|상환|납입|월급|보상|상금|리워드|포인트")
_NONBUDGET_POST = re.compile(r"^\s*(?:이상|이하)\s*(?:구매|결제|주문|가입|이용|사용|충전)|^\s*(?:할인|적립|캐시백|환급|쿠폰|혜택|지급|상품권|포인트|무료)")
_VAGUE_BUDGET = re.compile(r"(?:예산|광고비|비용)[^.!?\n]{0,10}?(?:넉넉|충분|여유|많이|적당|알아서|상관\s*없|무제한|제한\s*없|최대한|빵빵|두둑|풍족)|(?:넉넉|충분|여유|빵빵|두둑)\s*(?:하게|히|한)\s*(?:예산|쓸|쓰|집행)|(?:돈|예산)\s*은?\s*(?:걱정|상관)\s*없")
_RANGE_BEFORE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:만|천|억)?\s*(?:원)?\s*(?:[~\-–∼]|에서|부터|내지|혹은|또는|or)\s*$", re.I)
_UNIT_BARE = re.compile(r"(?:예산|광고비)\s*(?:은|는|이|가|:)?\s*(?:약|대략)?\s*(?P<n>\d[\d,]*)(?!\s*[%명개건회번배세살년월일주시분\d만억천백십원.,])")



def _update_before(pre: str) -> bool:
    return bool(_UPDATE_PRE.search(pre))


_UPDATE_PRE = re.compile(
    r"(?:말고|대신|아니고|아니라|아니\s*[,]?|아냐\s*[,]?|아뇨\s*[,]?|수정\s*[:：]?|정정\s*[:：]?|변경\s*[:：]?|바꿔서|줄여서|늘려서|올려서|낮춰서|변경해서|수정해서|다시|그냥|차라리|이제|지금은|아까\s*말한\s*것?\s*대신)\s*$"
)
_UPDATE_POST = re.compile(
    r"^\s*(?:정도|가량|쯤|만큼)?\s*(?:으로|로)\s*(?:다시\s*|그냥\s*|좀\s*)?"
    r"(?:바꿔|바꾸|바꿀|바꾼|변경|수정|조정|고쳐|줄여|줄이|줄일|올려|올리|올릴|늘려|늘리|늘릴|내려|내리|낮춰|낮추|낮출|깎아|맞춰|정정|교체|해\s*주|해주|해\s*줘|하죠|하자|할게|할래|갈게|가자|진행|결정|확정|하겠|합시다|가요|해요|바뀌)"
)


def is_update_at(text: str, start: int, end: int) -> bool:
    """text[start:end] 앞뒤에 '이전 값을 고쳐 달라'는 표지가 있는가 ('…로 바꿔줘', '말고 …', '아니 …')."""
    pre = text[max(0, start - 16):start]
    post = text[end:end + 18]
    return bool(_update_before(pre) or _UPDATE_POST.search(post))


def _amount_value(m: re.Match) -> Optional[int]:
    raw = (m.group("ascii") or m.group("hangul") or "").strip()
    return parse_amount(raw)


def _find_amounts(text: str) -> list[tuple[int, int, int, bool, bool]]:
    """(시작, 끝, 원 단위 값, 원/단위가 붙었는가, 한글 숫자인가) 목록. 명·개·% 같은 단위가 뒤따르면 금액이 아니다."""
    out: list[tuple[int, int, int, bool, bool]] = []
    for m in _AMT.finditer(text):
        raw_ascii = m.group("ascii")
        raw_hangul = m.group("hangul")
        has_unit = bool(raw_hangul) or bool(re.search(r"[억천백십만]", raw_ascii or ""))
        has_won = bool(m.group("won"))
        if not (has_unit or has_won):
            # 단위도 '원' 도 없는 숫자는 금액으로 읽지 않는다 (예산 앞에서만 별도 패턴으로 읽는다)
            continue
        end = m.end()
        if not has_won and _NOT_AMOUNT_AFTER.match(text[end:end + 4]):
            continue
        val = _amount_value(m)
        if val is None or val <= 0:
            continue
        out.append((m.start(), end, val, has_won, bool(raw_hangul)))
    return out


# ---------------------------------------------------------------------------
# 예산
# ---------------------------------------------------------------------------
def _classify_pre(pre: str) -> str:
    if _CTX_DAILY.search(pre):
        return "daily"
    if _CTX_WEEKLY.search(pre):
        return "weekly"
    if _CTX_MONTHLY.search(pre):
        return "monthly"
    if _CTX_TOTAL.search(pre) or _CTX_PERIOD_BEFORE.search(pre):
        return "total"
    return "other"


def _period_days(n_raw: str, unit: str) -> Optional[int]:
    word = {"한": 1, "두": 2, "세": 3, "네": 4}
    n = word.get(n_raw)
    if n is None:
        try:
            n = int(n_raw)
        except ValueError:
            return None
    if n <= 0 or n > 1000:
        return None
    if unit in ("주", "주일"):
        return n * 7
    if unit in ("개월", "달"):
        return n * ASSUMED_DAYS_PER_MONTH
    return n


def _extract_budget(text: str, ctx: ExtractContext, consumed: list[tuple[int, int]]) -> tuple[list[Candidate], list[Ambiguity]]:
    cands: list[Candidate] = []
    amb: list[Ambiguity] = []
    amounts = _find_amounts(text)
    short = len(text.strip()) <= 24
    kept = 0
    for (s, e, val, has_won, _) in amounts:
        pre = text[max(0, s - 18):s]
        post = text[e:e + 14]
        kind = _classify_pre(pre)
        strong = bool(_STRONG_ANCHOR.search(pre[-14:] + post[:10]))
        weak = bool(_WEAK_ANCHOR.search(post[:10]) or _WEAK_ANCHOR.search(pre[-5:]))
        # 시간 표지(월·하루·주)가 있으면 바로 뒤에 '할인·적립' 같은 말이 붙은 경우만 예산이 아니다. 표지가 없으면 앞뒤 8자에 상품 조건 낱말이 있는지 본다
        nonbudget = bool(_NONBUDGET_POST.search(post)) or (kind == "other" and not strong and bool(_NONBUDGET.search(pre[-8:] + " " + post[:8])))
        if _RANGE_BEFORE.search(pre) or re.match(r"^\s*(?:[~\-–∼]|에서|부터|내지)\s*\d", post):
            amb.append(Ambiguity("monthly_budget_krw", "range", text[s:e], "금액이 범위로 말해졌다"))
            consumed.append((s, e))
            continue
        if val > MAX_BUDGET_KRW:
            amb.append(Ambiguity("monthly_budget_krw", "unit", text[s:e], "금액이 너무 크다"))
            consumed.append((s, e))
            continue
        quote = text[s:e].strip()
        note = ""
        status = "confirmed"
        monthly: Optional[int] = None
        if kind == "daily":
            if nonbudget:
                continue
            monthly = val * ASSUMED_DAYS_PER_MONTH
            status = "inferred"
            note = f"하루 예산 {val:,}원에 {ASSUMED_DAYS_PER_MONTH}일을 곱한 값이에요 (이 프로젝트의 가정)"
            if val < 1_000:
                continue
        elif kind == "weekly":
            if nonbudget:
                continue
            monthly = val * ASSUMED_WEEKS_PER_MONTH
            status = "inferred"
            note = f"주 예산 {val:,}원에 {ASSUMED_WEEKS_PER_MONTH}주를 곱한 값이에요 (이 프로젝트의 가정)"
        elif kind == "monthly":
            if nonbudget:
                continue
            if not strong and val < MIN_BUDGET_WITHOUT_ANCHOR_KRW:
                continue
            monthly = val
        elif kind == "total":
            pm = _CTX_PERIOD_BEFORE.search(pre)
            days = _period_days(pm.group("n"), pm.group("u")) if pm else None
            if nonbudget and not strong:
                continue
            if days:
                monthly = int(round(val * ASSUMED_DAYS_PER_MONTH / days, -3)) or val
                status = "inferred"
                note = f"{days}일 동안의 총 예산 {val:,}원을 {ASSUMED_DAYS_PER_MONTH}일 기준 월 예산으로 환산한 값이에요 (이 프로젝트의 가정)"
            else:
                amb.append(Ambiguity("monthly_budget_krw", "total_period", text[s:e], "총 예산인데 기간을 알 수 없다"))
                consumed.append((s, e))
                continue
        else:  # 달·기간 표지가 없는 금액
            if nonbudget:
                continue
            if strong or weak:
                monthly = val
                status = "inferred"
                note = NOTE_NO_PERIOD
            elif "monthly_budget_krw" in ctx.last_asked:
                monthly = val          # 예산을 물은 직후의 대답이다
            elif is_update_at(text, s, e) and (ctx.has_budget or kept > 0):
                monthly = val          # '아니 300만원으로 할게요' — 이미 있는 예산을 고치는 말이다
                status = "inferred"
                note = NOTE_NO_PERIOD
            elif short and not amb:
                monthly = val
                status = "inferred"
                note = NOTE_NO_PERIOD
            else:
                continue
        if monthly is None:
            continue
        if monthly < MIN_BUDGET_STATEMENT_KRW:
            amb.append(Ambiguity("monthly_budget_krw", "unit", quote, "금액이 너무 작다"))
            consumed.append((s, e))
            continue
        # 앞쪽 표지('월', '하루', '예산은')까지 근거 구절에 포함해 읽기 쉽게 한다
        qs = s
        mm = re.search(r"(?:매월|매일|매주|한\s*달|한달|하루|일일|월|일|주|예산|광고비)\s*(?:예산|광고비)?\s*(?:은|는|이|가|에|:)?\s*(?:약|대략|최대)?\s*$", text[max(0, s - 12):s])
        if mm and kind != "total":
            qs = max(0, s - 12) + mm.start()
        quote = text[qs:e].strip()
        cands.append(Candidate("monthly_budget_krw", monthly, quote, status, is_update_at(text, qs, e), note, qs, e))
        consumed.append((qs, e))
        kept += 1
    if any(c.note == "" or c.note == NOTE_NO_PERIOD for c in cands):       # 월 단위로 직접 말한 예산이 있으면 '하루 최대 10만원' 같은 환산값은 쓰지 않는다
        cands = [c for c in cands if not c.note.startswith(("하루 예산", "주 예산"))]
    # 'm' 단위 (월 3m, budget 3M) — 예산 표지가 있을 때만 읽는다
    for m in _ENG_MILLION.finditer(text):
        pre = text[max(0, m.start() - 16):m.start()]
        if not (_STRONG_ANCHOR.search(pre) or _CTX_MONTHLY.search(pre) or re.search(r"\bwon|krw|원\b", m.group(0), re.I)):
            continue
        if any(c.start <= m.start() < c.end for c in cands):
            continue
        val = int(float(m.group("n")) * 1_000_000)
        if val < MIN_BUDGET_STATEMENT_KRW or val > MAX_BUDGET_KRW:
            continue
        qs = m.start()
        cands.append(Candidate("monthly_budget_krw", val, text[qs:m.end()].strip(), "inferred", is_update_at(text, qs, m.end()),
                               f"'{m.group('u')}' 를 백만(1,000,000)으로 읽은 값이에요 (이 프로젝트의 가정)", qs, m.end()))
        consumed.append((qs, m.end()))
    # 예산 뒤에 단위 없이 숫자만 온 경우 → 단위를 되묻는다
    if not cands and not amb:
        mu = _UNIT_BARE.search(text)
        if mu:
            n = int(mu.group("n").replace(",", ""))
            if n < MIN_BUDGET_STATEMENT_KRW:
                amb.append(Ambiguity("monthly_budget_krw", "unit", mu.group(0).strip(), "단위가 없다"))
            elif n <= MAX_BUDGET_KRW:
                cands.append(Candidate("monthly_budget_krw", n, mu.group(0).strip(), "inferred", is_update_at(text, mu.start(), mu.end()),
                                       "단위가 없어 원 단위로 읽었어요", mu.start(), mu.end()))
                consumed.append((mu.start(), mu.end()))
    if not cands and not amb and _VAGUE_BUDGET.search(text):
        mv = _VAGUE_BUDGET.search(text)
        amb.append(Ambiguity("monthly_budget_krw", "vague", mv.group(0).strip(), "예산이 숫자로 말해지지 않았다"))
    # 상대 수정: '예산 50만원 늘려줘', '예산 20% 줄여줘', '예산 절반으로', '예산 두 배로'
    if re.search(r"예산|광고비", text):
        for m in re.finditer(r"(?P<amt>\d[\d,]*(?:\.\d+)?\s*(?:억|천|백|십|만)+\s*원?|\d[\d,]{4,}\s*원)\s*(?:만큼|정도)?\s*(?:더\s*)?(?P<verb>늘려|올려|증액|추가|줄여|깎아|감액|낮춰|내려)", text):
            val = parse_amount(m.group("amt"))
            if val:
                sign = 1 if m.group("verb") in ("늘려", "올려", "증액", "추가") else -1
                cands = [c for c in cands if not (c.start < m.end() and c.end > m.start())]      # 같은 구간을 읽은 일반 금액 후보는 버린다
                cands.append(Candidate("monthly_budget_krw", None, text[m.start():m.end()].strip(), "confirmed", True,
                                       "", m.start(), m.end(), delta={"op": "add", "amount": sign * val}))
                consumed.append((m.start(), m.end()))
        for m in re.finditer(r"(?P<p>\d+(?:\.\d+)?)\s*%\s*(?:만큼|정도)?\s*(?:더\s*)?(?P<verb>늘려|올려|증액|줄여|깎아|감액|낮춰|내려)", text):
            p = float(m.group("p")) / 100.0
            factor = 1 + p if m.group("verb") in ("늘려", "올려", "증액") else 1 - p
            if 0 < factor < 10:
                cands.append(Candidate("monthly_budget_krw", None, m.group(0).strip(), "confirmed", True, "", m.start(), m.end(),
                                       delta={"op": "mul", "factor": factor}))
                consumed.append((m.start(), m.end()))
        for m in re.finditer(r"(?:절반|반)\s*(?:으로|만)?\s*(?:줄여|깎아|낮춰|내려|줄이)|(?:두|2)\s*배\s*(?:로)?\s*(?:늘려|올려|늘리)", text):
            half = bool(re.match(r"절반|반", m.group(0)))
            cands.append(Candidate("monthly_budget_krw", None, m.group(0).strip(), "confirmed", True, "", m.start(), m.end(),
                                   delta={"op": "mul", "factor": 0.5 if half else 2.0}))
            consumed.append((m.start(), m.end()))
    return cands, amb


# ---------------------------------------------------------------------------
# 목표
# ---------------------------------------------------------------------------
# (목표, 정규식, 강한 단서인가) — 앞에서부터 구간을 차지한다. 한 구간은 한 목표에만 쓰인다.
_OBJ_CUES: list[tuple[Objective, re.Pattern, bool]] = [
    (Objective.LEAD, re.compile(r"상담(?:\s*(?:신청|예약|문의))?|(?:잠재\s*고객|리드)(?:\s*확보)?|견적|문의\s*(?:유도|받|늘|가\s*늘|증가|확대)|\blead", re.I), True),
    (Objective.APP_INSTALL, re.compile(r"앱\s*설치|설치|인스톨|다운로드|\binstall", re.I), True),
    (Objective.CONVERSION, re.compile(r"구매|주문|결제|판매|매출|신청|가입|예약|구독|전환|\bconversion|\bpurchase|\bsign[\s-]?up|\border(?:s)?\b", re.I), True),
    (Objective.TRAFFIC, re.compile(r"방문|유입|트래픽|클릭|접속|\btraffic|\bvisit|\bclick", re.I), True),
    (Objective.AWARENESS, re.compile(r"인지도|인지|도달|브랜딩|브랜드\s*(?:알리|노출|인지)|노출\s*(?:극대|최대|늘)|\bawareness|\breach\b|많은\s*(?:사람|분)(?:들)?에게\s*알리", re.I), True),
    (Objective.CONVERSION, re.compile(r"팔고\s*싶|많이\s*팔|팔아야|팔리게|팔리도록"), False),
    (Objective.AWARENESS, re.compile(r"알리고\s*싶|알리고자|알리려|알려\s*보|홍보"), False),
]
_OBJ_VAGUE = re.compile(r"(?:목표|원하는\s*것|바라는\s*것)[^.!?\n]{0,8}?(?:잘\s*되|많이|좋게|성공|효과|성과|알아서|대박)|(?:잘|효과\s*좋게|성과\s*좋게)\s*(?:되게|됐으면|되었으면|나오게|나왔으면)|광고\s*잘\s*(?:되|돌|나)")
_GENERIC_CONV = re.compile(r"전환")


def _word_left(text: str, s: int) -> int:
    """위치 s 가 속한 낱말(공백·문장부호 단위)의 시작 위치. 근거 구절을 '대출' 이 아니라 '신용대출' 처럼 읽기 좋게 넓히는 데 쓴다 — 항상 글자 그대로의 부분 문자열이다."""
    ls = s
    while ls > 0 and not text[ls - 1].isspace() and text[ls - 1] not in ",.!?;()[]\"'“”‘’·":
        ls -= 1
    return ls


def _readable(text: str, s: int, e: int, limit: int = 14) -> tuple[int, int]:
    ls = _word_left(text, s)
    return (ls, e) if 0 < e - ls <= limit else (s, e)


def _overlaps(spans: list[tuple[int, int]], s: int, e: int) -> bool:
    return any(s < te and e > ts for ts, te in spans)


_NEG_AFTER = re.compile(r"^\s*(?:은|는|이|가|도|을|를)?\s*(?:필요\s*없|필요하지|필요\s*않|아니|아닌|아닙|말고|싫|안\s*할|하지\s*않|원치|원하지)")


def _negated(text: str, end: int) -> bool:
    """단서 바로 뒤에 '필요 없고', '말고' 같은 부정이 이어지는가 — '설치는 필요 없고 상담이 목표' 의 설치는 목표가 아니다."""
    return bool(_NEG_AFTER.match(text[end:end + 12]))


_GOAL_WORDS = re.compile(r"목표|늘리|늘려|높이|올리|유도|위해|원해|싶|하려|하고자|목적")


def _extract_objective(text: str, consumed: list[tuple[int, int]], skip: Optional[list[tuple[int, int]]] = None) -> tuple[list[Candidate], list[Ambiguity]]:
    """skip: 목표 단서를 읽지 않을 구간 (이미 다른 슬롯의 근거로 쓰인 구간, 상품 조건 문장). consumed 와 겹치는 단서는 쓰지 않는다."""
    skip = list(skip or []) + list(consumed)
    taken: list[tuple[int, int]] = []
    found: list[tuple[Objective, bool, int, int]] = []
    for obj, pat, strong in _OBJ_CUES:
        for m in pat.finditer(text):
            if _overlaps(taken, m.start(), m.end()) or _overlaps(skip, m.start(), m.end()):
                continue
            taken.append((m.start(), m.end()))
            if _negated(text, m.end()):
                continue                      # 부정된 단서는 구간만 차지하고 목표로는 읽지 않는다
            found.append((obj, strong, m.start(), m.end()))
    # '전환' 은 다른 목표 단서('신청 전환', '설치 전환')가 함께 있으면 꾸밈말이다
    strong_objs = {o for o, st, _, _ in found if st}
    found = [f for f in found if not (f[0] == Objective.CONVERSION and f[1] and text[f[2]:f[3]] == "전환" and (strong_objs - {Objective.CONVERSION}))]
    found.sort(key=lambda x: x[2])
    use = [f for f in found if f[1]] or found
    cands: list[Candidate] = []
    amb: list[Ambiguity] = []
    seen: set[Objective] = set()
    for obj, strong, s, e in use:
        if obj in seen:
            continue                      # 같은 목표가 여러 번 나오면 첫 번째만 근거로 쓴다
        seen.add(obj)
        qs, qe = _readable(text, s, e)
        cands.append(Candidate("objective", obj.value, text[qs:qe], "confirmed" if strong else "inferred", is_update_at(text, qs, qe),
                               "" if strong else "'알리고 싶다' 같은 말만으로 목표를 추정했어요", qs, qe, weak=not strong, needs_confirm=not strong))
        consumed.append((qs, qe))
    if not cands and _OBJ_VAGUE.search(text):
        mv = _OBJ_VAGUE.search(text)
        amb.append(Ambiguity("objective", "vague", mv.group(0).strip(), "목표가 구체적이지 않다"))
    return cands, amb


# ---------------------------------------------------------------------------
# 업종
# ---------------------------------------------------------------------------
CATEGORY_LABEL = {
    ProductCategory.LOAN: "대출", ProductCategory.CARD: "카드", ProductCategory.INSURANCE: "보험", ProductCategory.INVESTMENT: "투자·증권",
    ProductCategory.SAVINGS: "예·적금", ProductCategory.CRYPTO: "가상자산", ProductCategory.ECOMMERCE: "커머스", ProductCategory.APP: "앱·서비스",
    ProductCategory.EDUCATION: "교육", ProductCategory.HEALTH: "건강·의료", ProductCategory.REALESTATE: "부동산", ProductCategory.OTHER: "기타",
}
_FIN_ORDER = ["loan", "card", "insurance", "investment", "savings", "crypto"]
_PRIORITY = ["loan", "card", "insurance", "investment", "savings", "crypto", "realestate", "health", "education", "ecommerce", "app"]
_EXTRA_CUES: dict[str, list[re.Pattern]] = {
    "card": [re.compile(r"(?:(?:신용|체크|법인)\s*)?카드(?!\s*(?:뉴스|깡))", re.I), re.compile(r"\bcredit\s*card", re.I)],
    "loan": [re.compile(r"\bloan\b", re.I)],
    "insurance": [re.compile(r"\binsurance\b", re.I)],
    "investment": [re.compile(r"증권|인덱스|\binvest(?:ment)?\b|\bfund\b", re.I)],
    "savings": [re.compile(r"\bsavings?\b", re.I)],
    "crypto": [re.compile(r"\bcrypto\b|\bbitcoin\b", re.I)],
    "realestate": [re.compile(r"부동산|분양|아파트|오피스텔|전세|월세|청약|공인중개|\breal\s*estate\b", re.I)],
    "health": [re.compile(r"건강기능식품|건강\s*관리\s*서비스|건기식|영양제|비타민|프로바이오틱스|병원|의원|치과|한의원|의료|다이어트|헬스|피트니스|\bhealth\b", re.I)],
    "education": [re.compile(r"강의|강좌|교육|학원|클래스|수강|과외|자격증|어학|부트캠프|교재|\beducation\b|\bcourse\b", re.I)],
    "ecommerce": [re.compile(r"쇼핑몰|쇼핑|스토어|온라인\s*몰|이커머스|커머스|쿠폰|상품\s*판매|배송|패션|의류|뷰티|화장품|식품|가전|굿즈|공동구매|특가|세일|[가-힣A-Za-z0-9]몰(?![가-힣])|마켓|\be-?commerce\b|\bshop(?:ping)?\b|\bstore\b", re.I)],
    "app": [re.compile(r"(?<![가-힣])앱(?:[을를은는이가에의도로과와]|(?![가-힣]))|어플|애플리케이션|\bapp\b|플랫폼|\bSaaS\b|게임", re.I)],
}
_BUDGET_VERB_INVEST = re.compile(r"^\s*(?:을|를)?\s*(?:할|하|해|했|하려|하고|하는|금|비)")
_CATEGORY_WORD = {
    "대출": "loan", "카드": "card", "보험": "insurance", "투자": "investment", "증권": "investment", "예금": "savings", "적금": "savings", "예적금": "savings",
    "가상자산": "crypto", "코인": "crypto", "커머스": "ecommerce", "쇼핑": "ecommerce", "쇼핑몰": "ecommerce", "앱": "app", "서비스": "app", "교육": "education",
    "건강": "health", "의료": "health", "부동산": "realestate", "기타": "other",
}
_CAT_DECLARE = re.compile(r"(?:업종|카테고리|분야)\s*(?:은|는|이|가|을|를|:)?\s*(?P<w>[가-힣A-Za-z]{1,8})")
_CATEGORY_UNSURE = re.compile(r"^(?:아직|모르|몰라|미정|미확정|결정|정하지|잘|어떤|뭐|추천|알아서|알$|아니|아닌|아닙)")
# 플랫폼 열거에 전용 항목이 없는 일상 업종. 업종을 직접 선언한 미등록 명칭도 아래에서 기타로 받는다.
_OTHER_BUSINESS = r"(?:카페|커피숍|음식점|식당|빵집|베이커리|꽃집|미용실|세탁소|사진관|공방|문구점|서점|호텔|펜션)"
_EXTRA_CUES["other"] = [re.compile(r"(?<![가-힣])" + _OTHER_BUSINESS + r"(?=[은는이가을를의에도로과와]|(?![가-힣]))")]
_OTHER_DECLARE = re.compile(r"(?<![가-힣])" + _OTHER_BUSINESS + r"\s*(?:예요|이에요|입니다|이야|이고|[을를]\s*운영)")


def _category_hits(text: str, catalog: PolicyCatalog) -> dict[str, list[tuple[int, int]]]:
    hits: dict[str, list[tuple[int, int]]] = {}
    for cat, pats in catalog.category_cues.items():       # 정책 카탈로그의 업종 단서를 그대로 쓴다
        for p in pats:
            for m in p.finditer(text):
                if _negated(text, m.end()):
                    continue          # '대출 말고 적금' 의 대출은 업종 단서가 아니다
                if cat == "investment" and m.group(0) == "투자" and (_BUDGET_VERB_INVEST.match(text[m.end():m.end() + 6]) or re.search(r"(?:광고|마케팅|예산|집행)\s*$", text[max(0, m.start() - 6):m.start()])):
                    continue     # '300만원 투자할게요' 의 투자는 상품 업종이 아니라 예산을 쓰겠다는 말이다
                hits.setdefault(cat, []).append((m.start(), m.end()))
    for cat, pats in _EXTRA_CUES.items():
        for p in pats:
            for m in p.finditer(text):
                if _negated(text, m.end()):
                    continue
                hits.setdefault(cat, []).append((m.start(), m.end()))
    return hits


def infer_categories(text: str, catalog: Optional[PolicyCatalog] = None) -> list[tuple[str, int, int, int]]:
    """(업종, 단서 시작, 끝, 점수) 목록 — 점수 높은 순. 금융 업종이 단서에 있으면 비금융 업종보다 앞선다 (금융 규칙이 더 엄격하므로)."""
    catalog = catalog or get_catalog()
    hits = _category_hits(text, catalog)
    ranked: list[tuple[str, int, int, int]] = []
    for cat, spans in hits.items():
        uniq = sorted(set(spans))
        ranked.append((cat, uniq[0][0], uniq[0][1], len(uniq)))
    fin = [r for r in ranked if r[0] in _FIN_ORDER]
    other = [r for r in ranked if r[0] not in _FIN_ORDER]
    fin.sort(key=lambda r: (-r[3], _FIN_ORDER.index(r[0]), r[1]))
    other.sort(key=lambda r: (-r[3], _PRIORITY.index(r[0]) if r[0] in _PRIORITY else 99, r[1]))
    return fin + other


def _extract_category(text: str, catalog: PolicyCatalog, consumed: list[tuple[int, int]]) -> tuple[list[Candidate], list[Ambiguity]]:
    cands: list[Candidate] = []
    amb: list[Ambiguity] = []
    m = _CAT_DECLARE.search(text)
    if m:
        w = m.group("w")
        named = next(((cat, m.start("w") + len(word)) for word, cat in sorted(_CATEGORY_WORD.items(), key=lambda item: -len(item[0])) if w.startswith(word)), None)
        if named is None:
            known = infer_categories(w, catalog)
            named = (known[0][0], m.start("w") + known[0][2]) if known else ("other", m.end())
        cat, cue_end = named
        if not _CATEGORY_UNSURE.match(w) and not _negated(text, cue_end):
            # 선언 구절은 '적금으로'까지 포함해도 수정 표지는 업종 낱말 뒤에서 읽는다.
            cands.append(Candidate("category", cat, text[m.start():m.end()].strip(), "confirmed", is_update_at(text, m.start(), cue_end), "", m.start(), m.end()))
            consumed.append((m.start(), m.end()))
            return cands, amb
    for m in _OTHER_DECLARE.finditer(text):
        if not _negated(text, m.end()):
            cands.append(Candidate("category", "other", m.group(0).strip(), "confirmed", is_update_at(text, m.start(), m.end()), "", m.start(), m.end()))
            consumed.append((m.start(), m.end()))
            return cands, amb
    ranked = infer_categories(text, catalog)
    if not ranked:
        return cands, amb
    top = ranked[0]
    ties = [r for r in ranked[1:] if top[0] in _FIN_ORDER and r[0] in _FIN_ORDER and r[3] == top[3]]
    qs, qe = _readable(text, top[1], top[2])
    cands.append(Candidate("category", top[0], text[qs:qe], "inferred", is_update_at(text, qs, qe), "", qs, qe, weak=not ties))
    consumed.append((qs, qe))
    # 금융 업종이 둘 이상 같은 점수로 나오면 어느 쪽 광고인지 알 수 없다 → 후보를 모두 내서 충돌로 되묻게 한다
    for r in ties:
        q2s, q2e = _readable(text, r[1], r[2])
        cands.append(Candidate("category", r[0], text[q2s:q2e], "inferred", False, "", q2s, q2e))
    return cands, amb


# ---------------------------------------------------------------------------
# 타겟
# ---------------------------------------------------------------------------
MINOR_WORDS = re.compile(r"미성년|청소년|중학생|고등학생|고딩|중딩|초등학생|초딩|어린이|아동|유아|키즈|10대")
_PARENT_AFTER = re.compile(r"^\s*(?:을|를|의)?\s*(?:자녀|학부모|부모|엄마|아빠|보호자|둔)")
_SEGMENTS = ["직장인", "사회초년생", "자영업자", "소상공인", "대학생", "취준생", "주부", "학부모", "프리랜서", "신혼부부", "1인 가구", "시니어", "은퇴자", "투자자", "청년", "중장년", "워킹맘", "운전자", "초보 투자자", "초보자", "임산부", "반려인", "게이머"]
_AGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?<!\d)(?P<a>[1-9])0\s*대\s*(?P<sa>초반|중반|후반)?\s*(?:부터|에서|~|-|–|∼)\s*(?P<b>[1-9])0\s*대\s*(?P<sb>초반|중반|후반)?"), "decade_span"),
    (re.compile(r"(?<!\d)(?P<a>[1-9])0\s*대\s*[~\-–∼]\s*(?P<b>[1-9])0\s*대"), "decade_range2"),
    (re.compile(r"(?<!\d)(?P<a>[1-9])0\s*[~\-–∼]\s*(?P<b>[1-9])0\s*대"), "decade_range"),
    (re.compile(r"(?<!\d)(?P<a>[1-6])0(?P<b>[1-6])0(?!\d)(?=\s*(?:세대|대|여성|남성|남녀|직장인|타겟|타깃|층|고객|유저|사용자))"), "decade_compact"),
    (re.compile(r"만\s*(?P<a>\d{1,2})\s*[~\-–∼]\s*(?P<b>\d{1,2})\s*세"), "age_range"),
    (re.compile(r"(?<!\d)(?P<a>\d{1,2})\s*[~\-–∼]\s*(?P<b>\d{1,2})\s*세"), "age_range"),
    (re.compile(r"(?:만\s*)?(?<!\d)(?P<a>\d{1,2})\s*세\s*(?P<d>이상|이하|미만|초과)"), "age_bound"),
    (re.compile(r"(?<!\d)(?P<a>[1-9])0\s*대\s*(?P<sub>초반|중반|후반)?\s*(?P<d>이상|이하|미만|초과)"), "decade_bound"),
    (re.compile(r"(?<!\d)(?P<a>[1-9])0\s*대\s*(?P<sub>초반|중반|후반)?"), "decade"),
    (re.compile(r"(?<![\w])(?P<a>[1-9])0s(?![\w])", re.I), "decade"),
    (re.compile(r"(?<!\d)(?P<a>\d{1,2})\s*살"), "age_exact"),
    (re.compile(r"만\s*(?P<a>\d{1,2})\s*세"), "age_exact"),
]
_GENDER_F = re.compile(r"여성|여자|여성분|여자분")
_GENDER_M = re.compile(r"남성|남자|남성분|남자분")
_GENDER_ANY = re.compile(r"남녀|남여|성별\s*무관|성별\s*상관\s*없")
_EVERYONE = re.compile(r"전\s*연령|전연령|전체\s*연령|모든\s*연령|전체\s*고객|전\s*고객|모든\s*고객|모든\s*사람")
_ADULT = re.compile(r"성인")
_INTEREST = re.compile(r"(?P<i>(?:[가-힣A-Za-z0-9·]+\s+)?[가-힣A-Za-z0-9·]{2,12})\s*(?:에|에도)\s*관심\s*(?:이\s*)?(?:있는|많은|높은|있으신|많으신|있으면)")


def _age_from(kind: str, m: re.Match) -> Optional[dict]:
    a = int(m.group("a"))
    if kind == "decade_span":      # '20대 후반부터 30대 초반' — 두 연령대를 이은 하나의 범위
        def span(d: int, sub: Optional[str]) -> tuple[int, int]:
            lo = d * 10 + (4 if sub == "중반" else 7 if sub == "후반" else 0)
            hi = d * 10 + (3 if sub == "초반" else 6 if sub == "중반" else 9)
            return lo, hi

        (la, ha), (lb, hb) = span(a, m.group("sa")), span(int(m.group("b")), m.group("sb"))
        return {"age_min": min(la, lb), "age_max": max(ha, hb)}
    if kind == "decade_range2" or kind == "decade_range":
        lo, hi = a * 10, int(m.group("b")) * 10 + 9
        return {"age_min": lo, "age_max": hi}
    if kind == "decade_compact":
        return {"age_min": a * 10, "age_max": int(m.group("b")) * 10 + 9}
    if kind in ("decade", "decade_bound"):
        sub = m.groupdict().get("sub")
        lo, hi = a * 10, a * 10 + 9
        if sub == "초반":
            hi = a * 10 + 3
        elif sub == "중반":
            lo, hi = a * 10 + 4, a * 10 + 6
        elif sub == "후반":
            lo = a * 10 + 7
        if kind == "decade_bound":
            direction = m.group("d")
            if direction == "이상":
                hi = None
            elif direction == "초과":
                lo, hi = hi + 1, None
            elif direction == "이하":
                lo = None
            else:
                lo, hi = None, lo - 1
        return {"age_min": lo, "age_max": hi}
    if kind == "age_range":
        b = int(m.group("b"))
        return {"age_min": min(a, b), "age_max": max(a, b)}
    if kind == "age_bound":
        d = m.group("d")
        if d == "이상":
            return {"age_min": a, "age_max": None}
        if d == "초과":
            return {"age_min": a + 1, "age_max": None}
        if d == "이하":
            return {"age_min": None, "age_max": a}
        return {"age_min": None, "age_max": a - 1}
    if kind == "age_exact":
        return {"age_min": a, "age_max": a}
    return None


def _extract_audience(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    cands: list[Candidate] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return not any(s < te and e > ts for ts, te in taken)

    for pat, kind in _AGE_PATTERNS:
        for m in pat.finditer(text):
            if not free(m.start(), m.end()):
                continue
            val = _age_from(kind, m)
            if not val or all(v is None for v in val.values()):
                continue
            if kind in ("age_range", "age_bound", "age_exact") and not all((v is None or 0 < v < 100) for v in val.values()):
                continue
            taken.append((m.start(), m.end()))
            quote = text[m.start():m.end()].strip()
            val = {**val, "age_text": quote}
            cands.append(Candidate("audience", val, quote, "confirmed", is_update_at(text, m.start(), m.end()), "", m.start(), m.end()))
    for m in MINOR_WORDS.finditer(text):
        if m.group(0) == "10대":
            continue          # '10대' 는 위 연령 패턴이 이미 읽는다
        if _PARENT_AFTER.match(text[m.end():m.end() + 8]) or re.match(r"^\s*(?:을|를|의)?\s*(?:자녀|학부모|부모)", text[m.end():m.end() + 6]):
            continue          # '초등학생 학부모' 의 타겟은 학부모다
        if not free(m.start(), m.end()):
            continue
        taken.append((m.start(), m.end()))
        quote = text[m.start():m.end()]
        cands.append(Candidate("audience", {"minor": True, "age_text": quote}, quote, "confirmed", is_update_at(text, m.start(), m.end()), "", m.start(), m.end()))
    mf, mm, ma = _GENDER_F.search(text), _GENDER_M.search(text), _GENDER_ANY.search(text)
    if ma or (mf and mm):
        m = ma or mf
        cands.append(Candidate("audience", {"gender": "무관"}, text[m.start():m.end()], "confirmed", is_update_at(text, m.start(), m.end()), "", m.start(), m.end()))
    elif mf:
        cands.append(Candidate("audience", {"gender": "여성"}, text[mf.start():mf.end()], "confirmed", is_update_at(text, mf.start(), mf.end()), "", mf.start(), mf.end()))
    elif mm:
        cands.append(Candidate("audience", {"gender": "남성"}, text[mm.start():mm.end()], "confirmed", is_update_at(text, mm.start(), mm.end()), "", mm.start(), mm.end()))
    segs: list[tuple[str, int, int]] = []
    for seg in sorted(_SEGMENTS, key=len, reverse=True):
        for m in re.finditer(r"(?<![가-힣])" + re.escape(seg).replace(r"\ ", r"\s*") + r"(?=[을를은는이가에의도로과와들분층님만께랑]|(?![가-힣]))", text):
            if not any(m.start() < e and m.end() > s0 for _, s0, e in segs):
                segs.append((seg, m.start(), m.start() + len(seg)))
            break
    segs.sort(key=lambda x: x[1])
    for seg, s0, e0 in segs:
        cands.append(Candidate("audience", {"segments": [seg]}, text[s0:e0], "confirmed", is_update_at(text, s0, e0), "", s0, e0))
    mi = _INTEREST.search(text)
    if mi:
        item = mi.group("i").strip()
        item = re.sub(r"^(?:요즘|특히|평소|주로)\s+", "", item)
        cands.append(Candidate("audience", {"interests": [item]}, text[mi.start():mi.end()].strip(), "confirmed", False, "", mi.start(), mi.end()))
    me = _EVERYONE.search(text)
    if me:
        cands.append(Candidate("audience", {"everyone": True, "age_text": text[me.start():me.end()]}, text[me.start():me.end()], "confirmed", is_update_at(text, me.start(), me.end()), "", me.start(), me.end()))
    mad = _ADULT.search(text)
    if mad and not any(c.value.get("age_min") for c in cands if isinstance(c.value, dict)):
        cands.append(Candidate("audience", {"age_min": 19, "age_max": None, "age_text": "성인"}, "성인", "confirmed", False, "", mad.start(), mad.end()))
    for c in cands:
        consumed.append((c.start, c.end))
    return cands


def is_minor_audience(value: Any) -> bool:
    """타겟이 만 19세 미만을 포함하는가 (core.platform_spec.MIN_AGE 기준)."""
    from core.platform_spec import MIN_AGE

    if not isinstance(value, dict):
        return False
    if value.get("minor"):
        return True
    lo, hi = value.get("age_min"), value.get("age_max")
    if lo is not None and lo < MIN_AGE:
        return True
    if lo is None and hi is not None:
        # '40세 미만'도 하한을 말하지 않았으므로 미성년자를 포함할 수 있다.
        return True
    return False


def audience_text(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    parts: list[str] = []
    if value.get("everyone"):
        parts.append("전체 고객")
    elif value.get("age_text"):
        parts.append(str(value["age_text"]))
    elif value.get("age_min") is not None or value.get("age_max") is not None:
        lo, hi = value.get("age_min"), value.get("age_max")
        parts.append(f"만 {lo}세 이상" if hi is None else (f"만 {hi}세 이하" if lo is None else f"만 {lo}~{hi}세"))
    if value.get("gender") and value["gender"] != "무관":
        parts.append(str(value["gender"]))
    parts.extend(value.get("segments") or [])
    if value.get("interests"):
        parts.append("관심: " + ", ".join(value["interests"]))
    return " · ".join(parts) or "—"


def merge_audience(old: dict, new: dict, replace: bool) -> tuple[dict, bool]:
    """타겟 값 병합. (결과, 충돌 여부). 같은 항목(연령·성별)이 다르게 나오면 replace=False 일 때 충돌, True 일 때 교체. 직업·관심은 합친다."""
    out = dict(old)
    conflict = False
    for key, val in new.items():
        if key in ("segments", "interests"):
            cur = list(out.get(key) or [])
            for v in val:
                if v not in cur:
                    cur.append(v)
            out[key] = cur
        elif key in ("age_min", "age_max", "age_text", "minor", "everyone"):
            continue
        elif key == "gender":
            if out.get("gender") not in (None, val):
                if replace:
                    out["gender"] = val
                else:
                    conflict = True
            else:
                out["gender"] = val
    age_keys = {"age_min", "age_max", "age_text", "minor", "everyone"}
    if age_keys & set(new):
        old_age = {k: out.get(k) for k in age_keys if out.get(k) not in (None, False)}
        new_age = {k: new.get(k) for k in age_keys if new.get(k) not in (None, False)}
        comparable = lambda d: (d.get("age_min"), d.get("age_max"), bool(d.get("minor")), bool(d.get("everyone")))  # noqa: E731
        if old_age and comparable(old_age) != comparable(new_age) and not replace:
            conflict = True
        else:
            for k in age_keys:
                out.pop(k, None)
            out.update(new_age)
    return out, conflict


# ---------------------------------------------------------------------------
# 기간
# ---------------------------------------------------------------------------
def _mmdd(m: int, d: int) -> Optional[str]:
    try:
        date(_REF_YEAR, m, d)
    except ValueError:
        return None
    return f"{m:02d}-{d:02d}"


def _span_days(m1: int, d1: int, m2: int, d2: int) -> Optional[int]:
    try:
        a = date(_REF_YEAR, m1, d1)
        b = date(_REF_YEAR if (m2, d2) >= (m1, d1) else _REF_YEAR + 1, m2, d2)
    except ValueError:
        return None
    return (b - a).days + 1


_PERIOD_P1 = re.compile(r"(?P<m1>\d{1,2})\s*월\s*(?P<d1>\d{1,2})\s*일?\s*(?:부터|~|-|–|에서|∼)\s*(?:(?P<m2>\d{1,2})\s*월\s*)?(?P<d2>\d{1,2})\s*일(?:\s*까지)?")
_PERIOD_P2 = re.compile(r"(?P<m1>\d{1,2})/(?P<d1>\d{1,2})\s*(?:부터|~|-|–|∼)\s*(?:(?P<m2>\d{1,2})/)?(?P<d2>\d{1,2})")
_PERIOD_P3 = re.compile(r"(?P<m>\d{1,2})\s*월\s*(?:한\s*달|한달|내내|전체|중|동안)(?:\s*간)?")
_PERIOD_P5 = re.compile(r"(?P<n>\d+|한|두|세|네)\s*(?P<u>주일|주|개월|달|일)\s*(?:간|동안|짜리)")
_PERIOD_P5B = re.compile(r"일주일(?:\s*간|\s*동안)?|보름(?:\s*간|\s*동안)?")
_PERIOD_WORDS = re.compile(r"연말까지|올해\s*말까지|월말까지|이번\s*달\s*말까지|상시|연중|계속\s*(?:진행|집행)|다음\s*달부터|내년까지")


def _extract_period(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    cands: list[Candidate] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return not any(s < te and e > ts for ts, te in taken)

    def add(s: int, e: int, start: Optional[str], end: Optional[str], days: Optional[int]) -> None:
        taken.append((s, e))
        quote = text[s:e].strip()
        cands.append(Candidate("period", {"text": quote, "start": start, "end": end, "days": days}, quote, "confirmed", is_update_at(text, s, e), "", s, e))

    for pat, kind in ((_PERIOD_P1, "md"), (_PERIOD_P2, "slash")):
        for m in pat.finditer(text):
            if not free(m.start(), m.end()):
                continue
            m1, d1, d2 = int(m.group("m1")), int(m.group("d1")), int(m.group("d2"))
            m2 = int(m.group("m2")) if m.group("m2") else m1
            a, b = _mmdd(m1, d1), _mmdd(m2, d2)
            if a is None or b is None:
                continue
            add(m.start(), m.end(), a, b, _span_days(m1, d1, m2, d2))
    for m in _PERIOD_P3.finditer(text):
        if not free(m.start(), m.end()):
            continue
        mo = int(m.group("m"))
        if mo not in _MONTH_DAYS:
            continue
        add(m.start(), m.end(), f"{mo:02d}-01", f"{mo:02d}-{_MONTH_DAYS[mo]:02d}", _MONTH_DAYS[mo])
    for m in _PERIOD_P5.finditer(text):
        if not free(m.start(), m.end()):
            continue
        days = _period_days(m.group("n"), m.group("u"))
        if days:
            add(m.start(), m.end(), None, None, days)
    for m in _PERIOD_P5B.finditer(text):
        if not free(m.start(), m.end()):
            continue
        add(m.start(), m.end(), None, None, 7 if m.group(0).startswith("일주일") else 15)
    for m in _PERIOD_WORDS.finditer(text):
        if not free(m.start(), m.end()):
            continue
        add(m.start(), m.end(), None, None, None)
    cands.sort(key=lambda c: c.start)
    for c in cands:
        consumed.append((c.start, c.end))
    return cands


def period_text(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    t = value.get("text") or ""
    days = value.get("days")
    return f"{t} (약 {days}일)" if days else t


# ---------------------------------------------------------------------------
# 상품명 · 주소 · 어조 · 세트 수 · 입찰 방식
# ---------------------------------------------------------------------------
_NAME_QUOTED = re.compile(r"[“\"'‘「『](?P<n>[^“”\"'‘’「」『』\n]{2,30})[”\"'’」』]\s*(?:이?라는|이?란|을|를|은|는|이|가|\s*(?:상품|앱|서비스|브랜드|제품|쇼핑몰))")
_NAME_LABEL = re.compile(r"(?:상품명|제품명|서비스명|브랜드명|앱\s*이름|브랜드\s*이름|상품\s*이름|이름)\s*(?:은|는|이|:|：)\s*(?P<n>[^\s,.!?]{1,20}(?:\s[^\s,.!?]{1,12}){0,2})")
_NAME_CALLED = re.compile(r"(?P<n>[가-힣A-Za-z0-9]+(?:\s[가-힣A-Za-z0-9]+){0,2}?)\s*(?:이?라는|이?라고\s*하는)\s*(?:상품|앱|서비스|브랜드|제품|쇼핑몰|회사)")
_NAME_ORG = re.compile(r"(?P<n>[가-힣A-Za-z0-9]{2,12}(?:은행|증권|뱅크|캐피탈|저축은행|카드|생명|화재|손해보험|페이)\s+[가-힣A-Za-z0-9]{2,12}(?:대출|카드|보험|펀드|적금|예금|통장|론))")
_NAME_MALL = re.compile(r"(?<![가-힣A-Za-z0-9])(?P<n>[가-힣A-Za-z0-9]{1,12}(?:몰|스토어|마켓|클럽))(?![가-힣A-Za-z0-9])")
_NAME_STRIP = re.compile(r"(?:이에요|예요|입니다|이고|이며|이라서|으로|에서|에게|하고|을|를|은|는|이|가|의|로|도|만|요)$")


def _extract_name(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    cands: list[Candidate] = []
    for pat, status in ((_NAME_QUOTED, "confirmed"), (_NAME_LABEL, "confirmed"), (_NAME_CALLED, "confirmed"), (_NAME_ORG, "inferred"), (_NAME_MALL, "inferred")):
        for m in pat.finditer(text):
            name = m.group("n").strip()
            if pat is _NAME_LABEL:
                name = _NAME_STRIP.sub("", name).strip()
            if not (1 < len(name) <= 30) or name in ("상품", "서비스", "앱", "브랜드", "제품", "쇼핑몰", "온라인몰", "스토어", "마켓", "클럽", "이커머스"):
                continue
            s = text.find(name, m.start())
            if s < 0:
                continue
            cands.append(Candidate("product_name", name, name, status, False, "", s, s + len(name)))
        if cands:
            break
    if cands:
        cands = cands[:1]
        consumed.append((cands[0].start, cands[0].end))
    return cands


_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'“”‘’，。]+", re.I)


def clean_url(raw: str) -> Optional[str]:
    """http(s) 주소만 받는다. 사용자 정보(user:pw@)가 든 주소·javascript:/data: 같은 것은 받지 않는다. 가져오기(네트워크)는 하지 않는다."""
    u = raw.strip().rstrip(".,)]}!?;:'\"")
    if u.lower().startswith("www."):
        u = "https://" + u
    m = re.match(r"^(https?)://([^/?#\s]+)([/?#]\S*)?$", u, re.I)
    if not m:
        return None
    host = m.group(2)
    if "@" in host or "." not in host or len(u) > 300 or host.startswith(".") or host.endswith("."):
        return None
    if not re.match(r"^[A-Za-z0-9.\-:\[\]가-힣]+$", host):
        return None
    return u


def _extract_url(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    cands: list[Candidate] = []
    for m in _URL.finditer(text):
        url = clean_url(m.group(0))
        if url is None:
            continue
        quote = m.group(0).rstrip(".,)]}!?;:'\"")
        cands.append(Candidate("landing_url", url, quote, "confirmed", is_update_at(text, m.start(), m.end()), "", m.start(), m.start() + len(quote)))
        consumed.append((m.start(), m.start() + len(quote)))
        break
    return cands


_TONE_LABEL = re.compile(r"(?:톤|분위기|느낌|말투)\s*(?:은|는|을|를|으로|로|:)?\s*(?P<t>[^,.!?;。\n]{2,80})")
_TONE_REQUEST = re.compile(r"\s+(?:(?:해|써|작성해|표현해|만들어|바꿔)\s*(?:주(?:세요|십시오|시고|면)|줘(?:요)?)|부탁(?:해요|드려요|드립니다|합니다))")
_TONE = re.compile(r"(?:친근|신뢰감?|전문적|캐주얼|유머러스|감성적|담백|차분|고급스러운|따뜻|발랄|진중)(?:한|하게|적인|스러운|있는|있게)?")


def _extract_tone(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    m = _TONE_LABEL.search(text)
    if m:
        start, end = m.span("t")
        # 같은 문장에 이어진 예산·타겟 등은 말투 설명에 포함하지 않는다.
        end = min([end] + [s for s, _ in consumed if start <= s < end])
        request = _TONE_REQUEST.search(text, start, end)
        if request:
            end = request.start()
        value = text[start:end].strip()
        end = start + len(text[start:end].rstrip())
    else:
        m = _TONE.search(text)
        value = m.group(0) if m else ""
        end = m.end() if m else 0
    if not m:
        return []
    quote = text[m.start():end].strip()
    if len(value) < 2:
        return []
    consumed.append((m.start(), end))
    return [Candidate("tone", value, quote, "confirmed", True, "", m.start(), end)]


_NUMWORD = {"한": 1, "하나": 1, "일": 1, "1": 1, "두": 2, "둘": 2, "이": 2, "2": 2, "세": 3, "셋": 3, "삼": 3, "3": 3, "네": 4, "넷": 4, "사": 4, "4": 4, "다섯": 5, "5": 5}
_SET_COUNT = re.compile(r"세트\s*(?:를|는|가|도|수를|수는|수가)?\s*(?P<n>\d+|하나|한|둘|두|셋|세|넷|네|다섯)\s*(?:개|곳|가지)?\s*(?:으로|로)?")
_SET_COUNT_B = re.compile(r"(?P<n>\d+|하나|한|둘|두|셋|세|넷|네|다섯)\s*(?:개|곳)\s*(?:의\s*)?(?:광고\s*)?세트")


def _extract_set_count(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    for pat in (_SET_COUNT, _SET_COUNT_B):
        m = pat.search(text)
        if m:
            n = _NUMWORD.get(m.group("n"))
            if n:
                consumed.append((m.start(), m.end()))
                return [Candidate("ad_set_count", n, text[m.start():m.end()].strip(), "confirmed", True, "", m.start(), m.end())]
    return []


BID_LABELS = {"cpc": "직접 입찰 CPC", "cpm": "직접 입찰 CPM", "tcpa": "자동 입찰 tCPA", "conv": "자동 입찰 전환 최대화", "click": "자동 입찰 클릭 최대화"}
_BID = [
    (re.compile(r"(?<![A-Za-z])t\s*CPA(?![A-Za-z])|목표\s*(?:전환)?\s*단가", re.I), "tcpa"),
    (re.compile(r"전환\s*최대화"), "conv"),
    (re.compile(r"클릭\s*최대화"), "click"),
    (re.compile(r"(?<![A-Za-z])CPC(?![A-Za-z])|클릭당", re.I), "cpc"),
    (re.compile(r"(?<![A-Za-z])CPM(?![A-Za-z])|노출당|천\s*회\s*노출당", re.I), "cpm"),
]


def _extract_bid(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    for pat, key in _BID:
        m = pat.search(text)
        if m:
            consumed.append((m.start(), m.end()))
            return [Candidate("bid_strategy", BID_LABELS[key], text[m.start():m.end()].strip(), "confirmed", True, "", m.start(), m.end())]
    return []


# ---------------------------------------------------------------------------
# 꼭 들어갈 조건 (항상 '추정' — 사용자가 확인해야 사실이 된다)
# ---------------------------------------------------------------------------
_COND_UNIT = re.compile(r"\d[\d,.]*\s*(?:%|퍼센트|원|만원|일|개월|월|주|회|번|명|개|세|배|시간|분|kg|km|GB|MB|건|점|포인트|p\b)", re.I)
_COND_WORD = re.compile(r"금리|이자|수수료|한도|할인|쿠폰|적립|캐시백|환급|혜택|수익률|보수|연회비|가입비|위약금|1인|인당|이상|이하|미만|이내|까지|부터|선착순|한정|제외|무이자|면제|증정|사은품|경품|자동\s*결제|중도상환")
_COND_NONNUM = re.compile(r"(?:수수료|연회비|가입비|배송비?|자동\s*결제|결제|위약금|약정|이자)[^,.\n]{0,8}(?:없|무료|면제|무이자)|무료\s*(?:배송|체험|취소|상담)|(?:신규|첫|처음)\s*\S{0,4}\s*(?:한정|만\s*(?:적용|가능|지급|제공))|선착순|한정\s*수량|기간\s*한정|제외|1인\s*\d*\s*회")
_COND_REQUEST = re.compile(r"(?:써|넣어|적어|강조|표현)[가-힣]*\s*(?:줘|주세요|주시고)\s*$")


_CONNECTIVES = ("하고", "이고", "인데", "이며", "하며", "지만", "이지만", "이면서", "하면서", "구요", "고요", "거고", "는데", "으며")


def _clauses(text: str) -> list[tuple[int, int]]:
    """문장·절 단위로 자른다. 괄호 안의 쉼표와 숫자 사이의 쉼표(1,000)에서는 자르지 않고, '진행하고 …' 같은 연결 어미 뒤에서는 자른다."""
    out: list[tuple[int, int]] = []
    start = 0
    depth = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in "([{（":
            depth += 1
        elif ch in ")]}）":
            depth = max(0, depth - 1)
        cut_at = -1
        if ch in ".!?\n;。" and depth == 0:
            if not (ch == "." and 0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()):    # 소수점 '4.5' 에서는 자르지 않는다
                cut_at = i
        elif ch in ",，" and depth == 0:
            if not (0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()):
                cut_at = i
        if cut_at >= 0:
            if cut_at > start:
                out.append((start, cut_at))
            start = cut_at + 1
        elif depth == 0 and ch in "하이인지구고거는으" and i > start:
            prev = text[i - 2] if text[i - 1] == " " and i >= 2 else text[i - 1]
            for conn in _CONNECTIVES:
                if text.startswith(conn, i) and i + len(conn) < n and text[i + len(conn)] == " " and "가" <= prev <= "힣":
                    out.append((start, i + len(conn)))
                    start = i + len(conn)
                    i = start - 1
                    break
        i += 1
    if n > start:
        out.append((start, n))
    return out


def _mask(text: str, s: int, e: int, spans: list[tuple[int, int]]) -> str:
    chars = list(text[s:e])
    for ts, te in spans:
        for k in range(max(ts, s), min(te, e)):
            chars[k - s] = " "
    return "".join(chars)


def _extract_conditions(text: str, consumed: list[tuple[int, int]]) -> list[Candidate]:
    cands: list[Candidate] = []
    for s, e in _clauses(text):
        raw = text[s:e]
        lead = len(raw) - len(raw.lstrip())
        quote = raw.strip()
        for conn in _CONNECTIVES:          # 절을 자른 연결 어미('…시작하고')는 근거 구절에서 뺀다
            if quote.endswith(conn) and len(quote) > len(conn) + 3:
                quote = quote[: -len(conn)].rstrip()
                break
        if len(quote) < 4 or _COND_REQUEST.search(quote):
            continue
        masked = _mask(text, s, e, consumed)
        has_num = bool(re.search(r"\d", masked))
        ok = (has_num and (bool(_COND_UNIT.search(masked)) or bool(_COND_WORD.search(masked)))) or bool(_COND_NONNUM.search(masked))
        if not ok:
            continue
        if re.fullmatch(r"[\s\d\-–()]+", masked):    # 전화번호 같은 숫자 덩어리
            continue
        cands.append(Candidate("key_conditions", quote[:MAX_QUOTE_CHARS], quote[:MAX_QUOTE_CHARS], "inferred", False,
                               "사용자가 확인하기 전에는 광고에 꼭 들어갈 사실로 쓰지 않아요", s + lead, s + lead + len(quote)))
    return cands


# ---------------------------------------------------------------------------
# 답변 해석 (긍정·부정·선택)
# ---------------------------------------------------------------------------
_YES = re.compile(r"^\s*(?:네+|넵|넹|예|응|어|맞아(?:요)?|맞습니다|그래(?:요)?|좋아(?:요)?|좋습니다|그렇게\s*(?:해|할게|하죠|해주세요|해\s*주세요)|ㅇㅇ|ㅇㅋ|오케이|ok|okay|yes|y|확인|그대로|그렇습니다)\W*$", re.I)
_NO = re.compile(r"^\s*(?:아니(?:요|야|에요)?|아뇨|아냐|노|no|n|ㄴㄴ|틀려(?:요)?|그게\s*아니(?:에요|야)?|다시)\W*$", re.I)
_FIRST = re.compile(r"첫\s*번째|첫째|처음|앞(?:의|에)?\s*(?:것|거|말)|먼저\s*말한|이전|아까\s*말한")
_SECOND = re.compile(r"두\s*번째|둘째|나중|뒤(?:의|에)?\s*(?:것|거|말)|방금|마지막|최근")


def is_affirmative(text: str) -> bool:
    return bool(_YES.match(text))


def is_negative(text: str) -> bool:
    return bool(_NO.match(text))


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
SLOTS = ("product_name", "category", "objective", "monthly_budget_krw", "audience", "period", "landing_url", "key_conditions", "tone", "ad_set_count", "bid_strategy")


def extract(text: str, ctx: Optional[ExtractContext] = None, catalog: Optional[PolicyCatalog] = None) -> Extraction:
    """사용자 말 한 번에서 슬롯 후보를 뽑는다. text 는 인젝션 문장과 보이지 않는 문자를 걷어낸 글이어야 한다."""
    ctx = ctx or ExtractContext()
    catalog = catalog or get_catalog()
    out = Extraction()
    if not text or not text.strip():
        return out
    out.affirmative = is_affirmative(text)
    out.negative = is_negative(text)
    first, second = _FIRST.search(text), _SECOND.search(text)
    if first and not second:
        out.choice = 0
    elif second and not first:
        out.choice = 1
    elif first and second:       # '첫 번째 말고 두 번째요' — 말고 앞의 것을 뺀 나머지를 고른다
        if re.search(_FIRST.pattern + r"\s*말고", text):
            out.choice = 1
        elif re.search(_SECOND.pattern + r"\s*말고", text):
            out.choice = 0
    if out.affirmative or out.negative:
        return out
    consumed: list[tuple[int, int]] = []
    cands: list[Candidate] = []
    bc, ba = _extract_budget(text, ctx, consumed)
    cands += bc
    out.ambiguities += ba
    cands += _extract_audience(text, consumed)
    cands += _extract_period(text, consumed)
    cands += _extract_url(text, consumed)
    cands += _extract_set_count(text, consumed)
    cands += _extract_bid(text, consumed)
    cands += _extract_name(text, consumed)
    cands += _extract_tone(text, consumed)
    # 상품 조건 문장('5만원 이상 구매 시 무료배송')에 든 '구매' 는 광고 목표가 아니다 — 목표를 말하는 낱말('목표', '늘리고')이 없으면 그 문장에서는 목표 단서를 읽지 않는다
    conds = _extract_conditions(text, consumed)
    cond_spans = [(c.start, c.end) for c in conds if not _GOAL_WORDS.search(c.quote)]
    oc, oa = _extract_objective(text, consumed, skip=cond_spans)
    cands += oc
    out.ambiguities += oa
    cc, ca = _extract_category(text, catalog, consumed)
    cands += cc
    out.ambiguities += ca
    cands += conds
    cands.sort(key=lambda c: (c.start, c.slot))
    out.candidates = cands
    out.consumed = consumed
    return out


# ---------------------------------------------------------------------------
# 값 ↔ 문자열 (모델 경로·대역이 쓰는 표기)
# ---------------------------------------------------------------------------
def value_to_text(slot: str, value: Any) -> str:
    """슬롯 값을 모델 출력 스키마(문자열 하나)에 넣는 표기로. parse_value 가 이 표기를 같은 값으로 되읽는다."""
    if slot == "monthly_budget_krw":
        return str(int(value))
    if slot == "audience":
        if not isinstance(value, dict):
            return str(value)
        if value.get("minor"):
            return str(value.get("age_text") or "미성년")
        parts = []
        if value.get("everyone") or value.get("age_text"):
            parts.append(str(value.get("age_text") or "전체 고객"))
        elif value.get("age_min") is not None or value.get("age_max") is not None:
            lo, hi = value.get("age_min"), value.get("age_max")
            parts.append(f"만 {lo}세 이상" if hi is None else (f"만 {hi}세 이하" if lo is None else f"만 {lo}~{hi}세"))
        if value.get("gender"):
            parts.append("남녀" if value["gender"] == "무관" else str(value["gender"]))
        parts.extend(value.get("segments") or [])
        parts.extend(f"{i}에 관심 있는" for i in (value.get("interests") or []))
        return " ".join(parts)
    if slot == "period":
        return str(value.get("text") if isinstance(value, dict) else value)
    if slot == "key_conditions":
        return value if isinstance(value, str) else "; ".join(value)
    return str(value)


def parse_value(slot: str, text: str) -> Any:
    """모델이 돌려준 문자열을 슬롯 값 구조로 읽는다. 읽을 수 없으면 None. (규칙 추출기와 같은 해석기를 쓴다.)"""
    t = (text or "").strip()
    if not t:
        return None
    if slot == "monthly_budget_krw":
        digits = t.replace(",", "").replace(" ", "")
        if re.fullmatch(r"\d{1,13}", digits):
            return int(digits)
        amt = parse_amount(t)
        return amt
    if slot == "audience":
        sink: list[tuple[int, int]] = []
        cs = _extract_audience(t, sink)
        if not cs:
            return None
        val: dict = {}
        for c in cs:
            val, _ = merge_audience(val, c.value, True)
        return val
    if slot == "period":
        cs = _extract_period(t, [])
        return cs[0].value if cs else None
    if slot == "landing_url":
        return clean_url(t)
    if slot == "ad_set_count":
        try:
            n = int(t)
        except ValueError:
            return None
        return n if 1 <= n <= 20 else None
    if slot == "bid_strategy":
        return t if t in BID_LABELS.values() else None
    return t
