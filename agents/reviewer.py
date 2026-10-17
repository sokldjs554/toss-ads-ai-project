"""심사관 — GPT-4o Structured Outputs. 출력 스키마의 rule_id 는 카탈로그 Literal 로 고정되고(FM-03),
인용은 원문에서 검증된다(FM-04). 경계 사례는 다른 모델 계열의 2차 의견을 받는다(FM-17)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.llm_schemas import build_review_schema
from core.schemas import AdCopy, Claim, Decision, Finding, Layer, ReviewVerdict, Severity
from guardrails.normalize import loose_text
from guardrails.output_guard import OutputGuardResult, decision_from
from guardrails.policy import PolicyCatalog
from llm.base import LLMRequest, Task
from llm.router import Router

from .prompts import reviewer_system


_SEVERITY_ORDER = {Decision.APPROVE: 0, Decision.HUMAN_REVIEW: 0, Decision.REVISE: 1, Decision.REJECT: 2}
# 이 계층들은 코드가 완전하게 판정한다 (체크리스트·숫자 대조·길이). 모델이 여기서 '누락·근거 없음' 을 새로 만들면 틀린 것이다 —
# 라이브 run 38928048713/38930364792 에서 gpt-4o 는 고지가 전부 있는 대출 소재에 FIN-DISCLOSE-LOAN-01 을, 근거가 있는 주장에 GRND-CLAIM-01 을 냈다.
DET_AUTHORITATIVE_LAYERS = {Layer.DISCLOSURE, Layer.GROUNDING, Layer.STRUCTURAL}


@dataclass
class ReviewOutcome:
    verdict: ReviewVerdict
    unverified_quotes: int = 0
    unknown_rule_ids: int = 0
    overruled_by_det: int = 0  # 결정론이 '충족' 으로 판정한 계층에서 모델이 만든 finding 을 버린 수 (FM-23)
    raw: dict = field(default_factory=dict)


def _quote_verified(quote: str, text: str) -> bool:
    q = loose_text(quote).strip()
    return bool(q) and q in loose_text(text)


def relink_claims(claims: list[Claim], new_text: str) -> list[Claim]:
    """원문의 주장 중 새 문구에 (정규화 후) 그대로 남아 있는 것만 이어받는다. 문구가 바뀐 주장의 근거 연결은 무효다."""
    blob = loose_text(new_text)
    kept: list[Claim] = []
    for c in claims:
        key = loose_text(c.text).strip()
        if key and key in blob:
            kept.append(Claim(text=c.text, fact_id=c.fact_id))
    return kept


def _user_prompt(copy: AdCopy, det: OutputGuardResult, facts: str, landing_text: str) -> str:
    det_lines = "\n".join(f"- {f.rule_id} [{f.severity.value}] '{f.quote}' — {f.explanation}" for f in det.findings) or "- (없음)"
    return (
        f"[소재]\n헤드라인: {copy.headline}\n본문: {copy.body}\nCTA: {copy.cta}\n고지: {' / '.join(copy.disclosures) or '(없음)'}\n"
        f"주장→근거: {[(c.text, c.fact_id) for c in copy.claims]}\n\n[광고주 사실 목록]\n{facts}\n\n"
        + (f"[랜딩 페이지 텍스트(발췌)]\n{landing_text[:1200]}\n\n" if landing_text else "")
        + f"[결정론 검사가 찾은 확정 finding]\n{det_lines}\n"
    )


def _to_verdict(copy: AdCopy, parsed, det: OutputGuardResult, catalog: PolicyCatalog, provider: str, model: str) -> ReviewOutcome:
    text = copy.full_text()
    findings: list[Finding] = []
    unverified = 0
    unknown = 0
    overruled = 0
    det_rule_ids = {f.rule_id for f in det.findings}
    det_keys = {(f.rule_id, loose_text(f.quote)) for f in det.findings}
    for f in parsed.findings:
        rule = catalog.get(f.rule_id)
        if rule is None:  # 스키마상 불가능하지만 fallback 경로(비제약) 대비
            unknown += 1
            continue
        if (rule.id, loose_text(f.quote)) in det_keys:
            continue  # 결정론 finding 의 반복
        if rule.layer in DET_AUTHORITATIVE_LAYERS and rule.id not in det_rule_ids:
            overruled += 1  # 코드가 '충족' 이라고 확인한 것을 모델이 '누락' 이라 함 → 모델이 틀렸다
            continue
        ok = _quote_verified(f.quote, text)
        if not ok:
            unverified += 1
        start = text.find(f.quote) if ok else -1
        findings.append(Finding(rule_id=rule.id, severity=rule.severity if ok else Severity.WARN, layer=Layer.SEMANTIC,
                                quote=f.quote, start=start, end=start + len(f.quote) if start >= 0 else -1,
                                explanation=f.explanation + ("" if ok else " — [인용 검증 실패: 원문에 없는 구절]"), verified_quote=ok))
    rewrite: Optional[AdCopy] = None
    if parsed.rewrite is not None and parsed.decision in ("REVISE", "REJECT"):
        rw = parsed.rewrite
        if rw.headline.strip() and rw.body.strip():
            rewrite = AdCopy(headline=rw.headline.strip(), body=rw.body.strip(), cta=(rw.cta or copy.cta).strip(),
                             claims=[], disclosures=[d for d in rw.disclosures if d.strip()], tone=copy.tone + " (수정안)")
            # 주장→근거 연결은 '바뀐 문구' 기준으로 다시 맺는다. 원문 주장 중 수정안에 그대로 남은 것만 이어받고,
            # 사라진 주장은 버린다. 새 숫자·새 주장은 grounding 이 사실 목록과 대조한다 (재검사).
            rewrite.claims = relink_claims(copy.claims, rewrite.full_text())
    all_findings = det.findings + findings
    # decision = max(결정론+검증된 semantic finding 으로 재계산한 판정, 모델이 말한 판정). 모델은 판정을 '완화' 할 수 없다.
    recomputed = decision_from([f for f in all_findings if f.verified_quote or f.layer != Layer.SEMANTIC])
    model_decision = Decision(parsed.decision)
    decision = max(recomputed, model_decision, key=_SEVERITY_ORDER.get)
    conf = max(0.0, min(1.0, float(parsed.confidence)))
    if unverified:
        conf = min(conf, 0.6)  # 환각 인용이 있으면 과신을 막는다
    surviving = [f for f in all_findings if f.verified_quote or f.layer != Layer.SEMANTIC]
    unsupported = model_decision in (Decision.REVISE, Decision.REJECT) and not surviving
    verdict = ReviewVerdict(variant_id=copy.variant_id, decision=decision, confidence=conf, findings=all_findings,
                            rationale=parsed.rationale.strip(), proposed_rewrite=rewrite, provider=provider, model=model,
                            unsupported_decision=unsupported)
    return ReviewOutcome(verdict=verdict, unverified_quotes=unverified, unknown_rule_ids=unknown, overruled_by_det=overruled,
                         raw=parsed.model_dump())


def review(copy: AdCopy, det: OutputGuardResult, categories: set[str], facts: str, landing_text: str,
           catalog: PolicyCatalog, router: Router, task: Task = Task.REVIEW, label_suffix: str = "") -> ReviewOutcome:
    rule_ids = [r.id for r in catalog.rules_for(categories)]
    schema = build_review_schema(rule_ids)
    req = LLMRequest(
        task=task, system=reviewer_system(catalog, categories), user=_user_prompt(copy, det, facts, landing_text),
        schema=schema, max_tokens=1800, temperature=0.0,
        context={"lexical_findings": [f.model_dump(mode="json") for f in det.findings], "rule_ids": rule_ids,
                 "copy": copy.model_dump(mode="json"),
                 "required_disclosures": [i.canonical for i in det.missing_disclosures]},
        label=f"{task.value}:{copy.variant_id}" + (f":{label_suffix}" if label_suffix else ""),
    )
    resp = router.complete(req)
    if resp.parsed is None:
        raise ValueError("심사 출력이 비어 있음")
    return _to_verdict(copy, resp.parsed, det, catalog, resp.provider, resp.model)
