"""근거 결합 (FM-12) 과 랜딩 일치 (TA-LANDING-01).

소재의 숫자·주장은 광고주가 제공한 사실 목록(ProductFacts.facts) 또는 랜딩 텍스트에서 확인돼야 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.schemas import AdCopy, Finding, Layer, ProductFacts, Severity
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog

_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%|원|만\s*원|천\s*원|억|명|배|일|개월|년|kg|분|시간|회|건|호|p|개|점)?")
_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
_STOP = {"지금", "바로", "확인", "조건", "상품", "서비스", "가능", "신청", "이용", "고객", "혜택", "제공", "있습니다", "있어요", "합니다", "해요"}


@dataclass
class GroundingResult:
    findings: list[Finding] = field(default_factory=list)
    numbers_checked: int = 0
    numbers_unsupported: list[str] = field(default_factory=list)
    claims_unsupported: list[str] = field(default_factory=list)
    landing_checked: bool = False
    landing_mismatch: list[str] = field(default_factory=list)


def _norm_num(s: str) -> str:
    return s.replace(",", "").replace(" ", "")


def extract_numbers(text: str) -> list[tuple[str, str, int, int]]:
    out = []
    for m in _NUM.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "")
        if not unit and len(_norm_num(raw)) < 3:
            continue  # 단위 없는 한두 자리 숫자는 무시 (예: '3개 중 1')
        out.append((_norm_num(raw), _norm_num(unit), m.start(), m.end()))
    return out


def _number_in(num: str, blob: str) -> bool:
    # 쉼표·공백 제거 후 포함 여부. '4.5' 가 '4.5%' 또는 '4.50' 로 있어도 인정
    return num in blob or (num.endswith(".0") and num[:-2] in blob)


def check(copy: AdCopy, product: ProductFacts, landing_text: str = "", catalog: PolicyCatalog | None = None) -> GroundingResult:
    catalog = catalog or get_catalog()
    res = GroundingResult()
    marketing = "\n".join([copy.headline, copy.body, copy.cta])
    facts_blob = _norm_num(loose_text(" ".join(f.text for f in product.facts)))
    landing_blob = _norm_num(loose_text(landing_text)) if landing_text else ""
    num_rule = catalog.get("GRND-NUM-01")
    land_rule = catalog.get("TA-LANDING-01")
    claim_rule = catalog.get("GRND-CLAIM-01")

    # 1) 숫자
    for num, unit, s, e in extract_numbers(marketing):
        res.numbers_checked += 1
        quote = marketing[s:e]
        in_facts = _number_in(num, facts_blob)
        in_landing = bool(landing_blob) and _number_in(num, landing_blob)
        if not in_facts and not in_landing and num_rule is not None:
            res.numbers_unsupported.append(quote)
            res.findings.append(Finding(rule_id=num_rule.id, severity=num_rule.severity, layer=Layer.GROUNDING,
                                        quote=quote, start=s, end=e,
                                        explanation=f"'{quote}' 가 사실 목록·랜딩 어디에도 없음", verified_quote=True))
        elif landing_blob and not in_landing and land_rule is not None:
            res.landing_mismatch.append(quote)
            res.findings.append(Finding(rule_id=land_rule.id, severity=land_rule.severity, layer=Layer.GROUNDING,
                                        quote=quote, start=s, end=e,
                                        explanation=f"'{quote}' 가 랜딩 페이지 텍스트에서 확인되지 않음", verified_quote=True))

    # 2) 주장 → fact_id
    fact_ids = product.fact_ids()
    for c in copy.claims:
        if claim_rule is None:
            break
        if c.fact_id is None:
            res.claims_unsupported.append(c.text)
            res.findings.append(Finding(rule_id=claim_rule.id, severity=claim_rule.severity, layer=Layer.GROUNDING,
                                        quote=c.text, start=-1, end=-1, explanation="근거(fact_id) 없는 주장", verified_quote=True))
        elif c.fact_id not in fact_ids:
            res.claims_unsupported.append(c.text)
            res.findings.append(Finding(rule_id=claim_rule.id, severity=claim_rule.severity, layer=Layer.GROUNDING,
                                        quote=c.text, start=-1, end=-1,
                                        explanation=f"존재하지 않는 fact_id '{c.fact_id}' 를 가리킴 (모델이 지어낸 근거)", verified_quote=True))

    # 3) 랜딩 표현 일치 (토큰 겹침이 낮은 주장)
    if landing_blob and land_rule is not None:
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
