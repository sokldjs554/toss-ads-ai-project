"""근거 결합 (FM-12) 과 랜딩 일치 (TA-LANDING-01).

소재의 숫자·주장은 광고주가 제공한 사실 목록(ProductFacts.facts) 또는 랜딩 텍스트에서 확인돼야 한다.

검사 범위 (코드가 판단할 수 있는 것만 — 그 밖은 의미 심사와 사람의 몫이다)
- 숫자: 값 전체 + 단위 종류(비율·금액·기간·수량·배수) 가 같은 수치가 사실/랜딩에 있어야 한다. '5%' 는 '15일' 로 뒷받침되지 않는다.
  같은 값·같은 단위라도 주변 어휘가 '할인' vs '금리' 처럼 다른 뜻이면 뒷받침으로 보지 않는다 (어휘 목록에 있는 경우만).
- 주장→근거: 주장이 가리킨 fact_id 가 존재해야 하고, 그 사실이 실제로 그 주장을 뒷받침해야 한다 (주장의 숫자가 그 사실에 있거나, 핵심 어휘가 겹침).
- 조건 생략: 광고가 가리킨(또는 숫자로 끌어다 쓴) 사실에 '~후 자동결제', '심사 결과에 따라', '~이상', '~까지', '제외', '1인 1회' 같은
  조건이 있는데 광고 문구·고지 어디에도 그 조건이 없으면 GRND-COND-01. 조건 표식 목록은 아래 _CONDITION_MARKERS 가 전부다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.schemas import AdCopy, Fact, Finding, Layer, ProductFacts, Severity
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog

# 숫자 + (한국어 자릿수 단위) + (단위). 예: 5,000만원 / 4.5% / 15일 / 7일 / 19,900원 / 3배 / 5천만원
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([십백천만억]*)\s*(%|퍼센트|원|명|배|일|개월|달|년|시간|분|초|회|건|호|개|점|p|포인트|kg|㎏|g)?", re.IGNORECASE)
_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
_STOP = {"지금", "바로", "확인", "조건", "상품", "서비스", "가능", "신청", "이용", "고객", "혜택", "제공", "있습니다", "있어요", "합니다", "해요",
         "최대", "최소", "기준", "경우", "통해", "위해", "대한", "모든", "전체"}
_MULT = {"십": 10, "백": 100, "천": 1_000, "만": 10_000, "억": 100_000_000}
_UNIT_CLASS = {"%": "percent", "퍼센트": "percent", "원": "krw", "명": "count", "회": "count", "건": "count", "개": "count", "호": "count",
               "배": "multiple", "일": "duration", "개월": "duration", "달": "duration", "년": "duration", "시간": "duration", "분": "duration",
               "초": "duration", "점": "points", "p": "points", "포인트": "points", "kg": "weight", "㎏": "weight", "g": "weight"}
# 같은 숫자·단위라도 뜻이 다르면 뒷받침이 아니다. 숫자 주변(±10자)에 이 어휘가 있을 때만 비교한다.
_SENSE_WORDS = ("할인", "금리", "이자", "수익률", "수익", "적립", "캐시백", "보수", "수수료", "연체", "환급", "배송", "체험", "한도", "보장", "할부", "쿠폰")
# 조건 표식: (이름, 사실 텍스트에서 조건을 찾는 패턴, 광고 문구에 있어야 하는 패턴들 중 하나)
_CONDITION_MARKERS: tuple[tuple[str, re.Pattern, tuple[re.Pattern, ...]], ...] = (
    ("자동결제·유료 전환", re.compile(r"(자동\s*결제|자동\s*갱신|유료\s*(전환|결제)|정기\s*결제|과금)"),
     (re.compile(r"자동\s*결제"), re.compile(r"자동\s*갱신"), re.compile(r"유료\s*(전환|결제)"), re.compile(r"정기\s*결제"), re.compile(r"과금"))),
    ("심사 조건", re.compile(r"심사\s*(결과|기준)?\s*에\s*따라"), (re.compile(r"심사"),)),
    ("최소 금액 조건", re.compile(r"(\d[\d,]*\s*[십백천만억]*\s*원)\s*이상"), (re.compile(r"이상"),)),
    ("기간 한정", re.compile(r"(\d+\s*월\s*\d+\s*일|\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}|\d+\s*일)\s*까지"), (re.compile(r"까지"),)),
    ("1인 1회", re.compile(r"1\s*인\s*1\s*회"), (re.compile(r"1\s*인\s*1\s*회"), re.compile(r"1\s*회\s*한정"))),
    ("제외 조건", re.compile(r"(일부|특정)?\s*[가-힣A-Za-z]*\s*제외"), (re.compile(r"제외"),)),
)


@dataclass(frozen=True)
class Quantity:
    value: float
    cls: str            # percent | krw | count | multiple | duration | points | weight | bare
    unit: str           # 원문 단위 (duration·count 는 단위까지 같아야 한다)
    sense: str          # 주변 어휘로 본 뜻 (없으면 "")
    start: int
    end: int
    raw: str

    def supports(self, other: "Quantity") -> bool:
        """self(사실 쪽) 가 other(광고 쪽) 를 뒷받침하는가."""
        if abs(self.value - other.value) > 1e-9:
            return False
        if other.cls != "bare" and self.cls != "bare":
            if self.cls != other.cls:
                return False
            if self.cls in ("duration", "count") and self.unit != other.unit:
                return False
        if self.sense and other.sense and self.sense != other.sense:
            return False
        return True


@dataclass
class GroundingResult:
    findings: list[Finding] = field(default_factory=list)
    numbers_checked: int = 0
    numbers_unsupported: list[str] = field(default_factory=list)
    claims_unsupported: list[str] = field(default_factory=list)
    conditions_omitted: list[str] = field(default_factory=list)
    landing_checked: bool = False
    landing_mismatch: list[str] = field(default_factory=list)
    referenced_fact_ids: list[str] = field(default_factory=list)


_SEG_DELIM = re.compile(r"[\n.,;()·~\-/]")


def _sense(text: str, start: int, end: int) -> str:
    """숫자와 같은 구절(줄·쉼표·괄호로 끊기는 구간) 안에서 가장 가까운 뜻 어휘. 다른 줄의 어휘를 끌어오지 않는다."""
    seg_start = max((m.end() for m in _SEG_DELIM.finditer(text, 0, start)), default=0)
    m_end = _SEG_DELIM.search(text, end)
    seg_end = m_end.start() if m_end else len(text)
    seg = text[seg_start:seg_end]
    best, best_d = "", 10 ** 9
    for w in _SENSE_WORDS:
        for m in re.finditer(re.escape(w), seg):
            d = min(abs((seg_start + m.start()) - end), abs(start - (seg_start + m.end())))
            if d < best_d:
                best, best_d = ("수익" if w == "수익률" else w), d
    return best if best_d <= 12 else ""


def extract_quantities(text: str) -> list[Quantity]:
    out: list[Quantity] = []
    for m in _NUM.finditer(text):
        raw_num, mult, unit = m.group(1), m.group(2) or "", (m.group(3) or "")
        digits = raw_num.replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        for ch in mult:
            value *= _MULT.get(ch, 1)
        unit_norm = unit.lower()
        if unit_norm in ("퍼센트",):
            unit_norm = "%"
        cls = _UNIT_CLASS.get(unit_norm, "bare") if unit_norm else ("krw" if mult else "bare")
        if cls == "bare" and not mult and len(digits.replace(".", "")) < 3:
            continue  # 단위 없는 한두 자리 숫자는 무시 (예: '3개 중 1')
        if mult and not unit_norm:
            cls = "bare_mult"  # '5천' 처럼 단위 없는 자릿수 표기 — 값만 비교
        out.append(Quantity(value=value, cls="bare" if cls == "bare_mult" else cls, unit=unit_norm, sense=_sense(text, m.start(), m.end()),
                            start=m.start(), end=m.end(), raw=text[m.start():m.end()]))
    return out


def extract_numbers(text: str) -> list[tuple[str, str, int, int]]:
    """하위 호환: (정규화 숫자, 단위, start, end)."""
    return [(q.raw.replace(",", "").replace(" ", ""), q.unit, q.start, q.end) for q in extract_quantities(text)]


def _supported(q: Quantity, pool: list[Quantity]) -> bool:
    return any(p.supports(q) for p in pool)


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
    """광고가 근거로 가리킨 사실 + 숫자로 끌어다 쓴 사실 (수정안·직접 편집한 문구처럼 claims 가 없어도 찾는다)."""
    by_id = {f.id: f for f in product.facts}
    refs: dict[str, Fact] = {}
    for c in copy.claims:
        if c.fact_id in by_id:
            refs[c.fact_id] = by_id[c.fact_id]
    marketing_q = extract_quantities("\n".join([copy.headline, copy.body, copy.cta]))
    for f in product.facts:
        if f.id in refs:
            continue
        fq = extract_quantities(f.text)
        if any(_supported(q, fq) for q in marketing_q):
            refs[f.id] = f
    return list(refs.values())


def omitted_conditions(copy: AdCopy, facts: list[Fact]) -> list[tuple[Fact, str, str]]:
    """참조된 사실의 조건이 광고(문구+고지)에 없는 경우. (사실, 조건 이름, 조건 원문)"""
    blob = loose_text(copy.full_text())
    out = []
    for f in facts:
        for name, finder, needed in _CONDITION_MARKERS:
            m = finder.search(f.text)
            if not m:
                continue
            if not any(p.search(blob) for p in needed):
                out.append((f, name, m.group(0)))
    return out


def check(copy: AdCopy, product: ProductFacts, landing_text: str = "", catalog: PolicyCatalog | None = None) -> GroundingResult:
    catalog = catalog or get_catalog()
    res = GroundingResult()
    marketing = "\n".join([copy.headline, copy.body, copy.cta])
    facts_q = [q for f in product.facts for q in extract_quantities(f.text)]
    landing_q = extract_quantities(landing_text) if landing_text else []
    num_rule = catalog.get("GRND-NUM-01")
    land_rule = catalog.get("TA-LANDING-01")
    claim_rule = catalog.get("GRND-CLAIM-01")
    cond_rule = catalog.get("GRND-COND-01")

    # 1) 숫자: 값 전체 + 단위 종류 + (있으면) 뜻 이 같은 수치가 사실/랜딩에 있어야 한다
    for q in extract_quantities(marketing):
        res.numbers_checked += 1
        in_facts = _supported(q, facts_q)
        in_landing = bool(landing_q) and _supported(q, landing_q)
        if not in_facts and not in_landing and num_rule is not None:
            same_value = [p for p in facts_q + landing_q if abs(p.value - q.value) < 1e-9]
            why = (f"'{q.raw}' 와 값은 같지만 단위·뜻이 다른 수치({', '.join(sorted({p.raw for p in same_value}))})만 있음"
                   if same_value else f"'{q.raw}' 가 사실 목록·랜딩 어디에도 없음")
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

    # 3) 참조된 사실의 조건이 광고에 빠졌는가 (GRND-COND-01)
    refs = referenced_facts(copy, product)
    res.referenced_fact_ids = [f.id for f in refs]
    if cond_rule is not None:
        seen: set[tuple[str, str]] = set()
        for f, name, cond in omitted_conditions(copy, refs):
            if (f.id, name) in seen:
                continue
            seen.add((f.id, name))
            res.conditions_omitted.append(f"{f.id}: {cond}")
            # 인용은 그 사실을 끌어다 쓴 광고 구절 — 숫자가 있으면 그 숫자, 없으면 주장 텍스트
            quote, start, end = "", -1, -1
            fq = extract_quantities(f.text)
            for q in extract_quantities(marketing):
                if _supported(q, fq):
                    quote, start, end = q.raw, q.start, q.end
                    break
            if not quote:
                for c in copy.claims:
                    if c.fact_id == f.id:
                        quote = c.text
                        break
            res.findings.append(Finding(rule_id=cond_rule.id, severity=cond_rule.severity, layer=Layer.GROUNDING,
                                        quote=quote, start=start, end=end,
                                        explanation=f"근거 {f.id}('{f.text}')의 조건 '{cond}'({name})이 광고 문구·고지 어디에도 없음",
                                        verified_quote=True))

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
