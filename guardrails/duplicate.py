"""반려 소재의 유사 변형 재등록 탐지 (TA-DUP-01). 문자 3-gram Jaccard — 임베딩 없이 결정론적으로."""
from __future__ import annotations

from core.schemas import Finding, Layer
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog


def char_ngrams(text: str, n: int = 3) -> set[str]:
    t = loose_text(text).replace(" ", "")
    if len(t) < n:
        return {t} if t else set()
    return {t[i: i + n] for i in range(len(t) - n + 1)}


def similarity(a: str, b: str, n: int = 3) -> float:
    ga, gb = char_ngrams(a, n), char_ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def check(text: str, rejected_history: list[str], threshold: float = 0.6,
          catalog: PolicyCatalog | None = None) -> tuple[Finding | None, float]:
    catalog = catalog or get_catalog()
    best = 0.0
    for prev in rejected_history:
        best = max(best, similarity(text, prev))
    rule = catalog.get("TA-DUP-01")
    if best >= threshold and rule is not None:
        return Finding(rule_id=rule.id, severity=rule.severity, layer=Layer.STRUCTURAL, quote="", start=-1, end=-1,
                       explanation=f"이미 반려된 소재와 유사도 {best:.0%} (임계 {threshold:.0%}) — 사유를 해소하지 않은 재등록",
                       verified_quote=True), best
    return None, best
