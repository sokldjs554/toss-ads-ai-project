"""업종별 필수 고지 체크 (FM-13). 본문·고지 문구 어디에든 있으면 충족으로 본다."""
from __future__ import annotations

from dataclasses import dataclass, field

from core.schemas import AdCopy, Finding, Layer, Severity
from guardrails.normalize import loose_text
from guardrails.policy import DisclosureItem, PolicyCatalog, get_catalog


@dataclass
class DisclosureResult:
    findings: list[Finding] = field(default_factory=list)
    missing: list[DisclosureItem] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    checked_categories: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def check(copy: AdCopy, categories: set[str], catalog: PolicyCatalog | None = None) -> DisclosureResult:
    catalog = catalog or get_catalog()
    text = loose_text(copy.full_text())
    result = DisclosureResult()
    seen_keys: set[str] = set()
    for cat in sorted(categories):
        spec = catalog.disclosure_for(cat)
        if spec is None:
            continue
        result.checked_categories.append(cat)
        rule = catalog.get(spec.rule_id)
        missing_here: list[DisclosureItem] = []
        for item in spec.items:
            if item.key in seen_keys:
                continue
            seen_keys.add(item.key)
            if any(p.search(text) for p in item.any_of):
                result.present.append(item.label)
            else:
                missing_here.append(item)
        if missing_here and rule is not None:
            result.missing.extend(missing_here)
            result.findings.append(Finding(
                rule_id=rule.id, severity=rule.severity, layer=Layer.DISCLOSURE, quote="", start=-1, end=-1,
                explanation=f"필수 고지 누락: {', '.join(i.label for i in missing_here)}", verified_quote=True,
            ))
    # 고지 문구란에 광고 문구 (TA-DISCLOSURE-SLOT-01)
    slot_rule = catalog.get("TA-DISCLOSURE-SLOT-01")
    if slot_rule is not None:
        strong = ("지금 바로", "신청하세요", "클릭", "!", "특가", "이벤트", "지금 신청")
        legal_keys = ("손실", "연체", "약관", "심의", "개인차", "보호", "상환", "금리", "변동", "노력", "이자율", "연회비", "실적",
                      "면책", "감액", "보장 내용", "해지", "수수료", "세금", "거절", "평점", "조건", "기준")
        for d in copy.disclosures:
            cues = [c for c in catalog.slot_marketing_cues if c in d]
            legal = any(k in d for k in legal_keys)
            flagged = any(c in d for c in strong) or (len(cues) >= 2 and not legal)
            if flagged:
                result.findings.append(Finding(
                    rule_id=slot_rule.id, severity=slot_rule.severity, layer=Layer.STRUCTURAL, quote=d,
                    start=-1, end=-1, explanation=f"고지 문구란에 광고성 표현({', '.join(cues)})", verified_quote=True,
                ))
    return result


def canonical_lines(missing: list[DisclosureItem]) -> list[str]:
    return [i.canonical for i in missing]
