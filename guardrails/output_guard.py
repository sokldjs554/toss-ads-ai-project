"""생성된 소재에 대한 결정론 가드레일 묶음. LLM 심사(semantic) 전에 돌고, 심사 결과와 합쳐진다.

여기서 잡힌 finding 은 '증거가 원문에 있는' 확정 사실이다. 모델은 이를 뒤집을 수 없고, 추가만 할 수 있다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.schemas import AdCopy, ChecklistItem, Decision, Finding, Layer, ProductFacts, Severity
from guardrails import disclosure, duplicate, grounding, injection
from guardrails.lexical import scan
from guardrails.policy import PolicyCatalog, get_catalog

_HANGUL = re.compile(r"[가-힣]")
_LETTER = re.compile(r"[A-Za-z가-힣]")


@dataclass
class OutputGuardResult:
    findings: list[Finding] = field(default_factory=list)
    checklist: list[ChecklistItem] = field(default_factory=list)
    missing_disclosures: list = field(default_factory=list)  # DisclosureItem
    duplicate_score: float = 0.0

    @property
    def decision(self) -> Decision:
        return decision_from(self.findings)

    def by_layer(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.layer.value, []).append(f)
        return out


def decision_from(findings: list[Finding]) -> Decision:
    sev = {f.severity for f in findings}
    if Severity.BLOCK in sev:
        return Decision.REJECT
    if Severity.FIX in sev:
        return Decision.REVISE
    return Decision.APPROVE


def _structural(copy: AdCopy, catalog: PolicyCatalog) -> list[Finding]:
    out: list[Finding] = []
    limits = catalog.length_limits
    len_rule = catalog.get("TA-LENGTH-01")
    if len_rule:
        for field_name, limit in (("headline", limits.get("headline", 30)), ("body", limits.get("body", 90)), ("cta", limits.get("cta", 12))):
            value = getattr(copy, field_name)
            if len(value) > limit:
                out.append(Finding(rule_id=len_rule.id, severity=len_rule.severity, layer=Layer.STRUCTURAL, quote=value[:40],
                                   explanation=f"{field_name} {len(value)}자 > 제한 {limit}자", verified_quote=True))
    lang_rule = catalog.get("TA-LANG-01")
    if lang_rule:
        text = copy.headline + copy.body
        letters = _LETTER.findall(text)
        if letters:
            ratio = len(_HANGUL.findall(text)) / len(letters)
            if ratio < 0.5:
                out.append(Finding(rule_id=lang_rule.id, severity=lang_rule.severity, layer=Layer.STRUCTURAL, quote=text[:40],
                                   explanation=f"한글 비율 {ratio:.0%} < 50%", verified_quote=True))
    return out


def check(copy: AdCopy, product: ProductFacts, categories: set[str], landing_text: str = "",
          rejected_history: list[str] | None = None, catalog: PolicyCatalog | None = None) -> OutputGuardResult:
    catalog = catalog or get_catalog()
    res = OutputGuardResult()
    text = copy.full_text()

    inj = injection.scan_text(text, catalog)
    res.findings.extend(inj.findings)
    res.checklist.append(ChecklistItem(name="인젝션·심사 조작 문구 없음", passed=not inj.detected, layer=Layer.INJECTION,
                                       detail=f"{len(inj.findings)}건" if inj.detected else "통과"))

    lex = scan(text, categories, catalog)
    disc_start = next((start for name, start, _ in copy.field_spans() if name.startswith("disc")), len(text) + 1)  # full_text 에서 고지 문구가 시작되는 위치 (빈 필드는 건너뜀)
    lex = [f for f in lex if not (f.start >= disc_start and "부정문" in f.explanation)]  # 고지문 속 '보장하지 않습니다' 는 정상
    res.findings.extend(lex)
    res.checklist.append(ChecklistItem(name="금지·주의 표현 (정규화 후 패턴)", passed=not any(f.severity != Severity.WARN for f in lex),
                                       layer=Layer.LEXICAL, detail=", ".join(sorted({f.rule_id for f in lex})) or "통과"))

    disc = disclosure.check(copy, categories, catalog)
    res.findings.extend(disc.findings)
    res.missing_disclosures = disc.missing
    res.checklist.append(ChecklistItem(name="업종별 필수 고지", passed=disc.ok and not any(f.rule_id == "TA-DISCLOSURE-SLOT-01" for f in disc.findings),
                                       layer=Layer.DISCLOSURE,
                                       detail=("누락: " + ", ".join(i.label for i in disc.missing)) if disc.missing else
                                       ("검사 대상 아님" if not disc.checked_categories else "충족")))

    grd = grounding.check(copy, product, landing_text, catalog)
    res.findings.extend(grd.findings)
    unproven = grd.numbers_unsupported + grd.claims_unsupported
    res.checklist.append(ChecklistItem(name="숫자·주장의 근거 (사실 목록)", passed=not unproven and not grd.unchecked,
                                       layer=Layer.GROUNDING,
                                       detail="; ".join(([f"근거 없음: {', '.join(unproven)}"] if unproven else [])
                                                        + ([f"확인하지 못한 뒷부분: {', '.join(grd.unchecked)}"] if grd.unchecked else []))
                                       or f"숫자 {grd.numbers_checked}개 확인"))
    cond_bad = grd.conditions_omitted + grd.conditions_conflict
    res.checklist.append(ChecklistItem(name="상품 정보의 조건과 일치", passed=not cond_bad, layer=Layer.GROUNDING,
                                       detail=("; ".join(cond_bad)) if cond_bad else "확인한 조건에서 다른 점 없음 (코드가 비교하는 조건 6종류)"))
    if landing_text:
        res.checklist.append(ChecklistItem(name="랜딩 페이지 표현 일치", passed=not grd.landing_mismatch, layer=Layer.GROUNDING,
                                           detail=("불일치: " + ", ".join(grd.landing_mismatch)) if grd.landing_mismatch else "일치"))

    struct = _structural(copy, catalog)
    res.findings.extend(struct)
    res.checklist.append(ChecklistItem(name="길이·언어 형식", passed=not struct, layer=Layer.STRUCTURAL,
                                       detail="; ".join(f.explanation for f in struct) or "통과"))

    dup, score = duplicate.check(copy.core_text(), rejected_history or [], catalog=catalog)   # 필수 고지는 어느 광고에나 같게 들어가므로 유사도에서 뺀다
    res.duplicate_score = score
    if dup:
        res.findings.append(dup)
    res.checklist.append(ChecklistItem(name="반려 소재 유사 재등록 아님", passed=dup is None, layer=Layer.STRUCTURAL,
                                       detail=f"최대 유사도 {score:.0%}"))
    return res
