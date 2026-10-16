"""최종 판정은 모델이 아니라 이 코드가 한다. 승인으로 가는 길은 좁고, 사람 검토로 가는 길은 넓다 (fail-closed).

규칙 (위에서부터 먼저 맞는 것):
1. 결정론 BLOCK 이 하나라도 있으면 REJECT — 모델은 이를 뒤집을 수 없다.
2. 심사 모델이 실패했으면 HUMAN_REVIEW.
3. 모델이 REJECT 라 했지만 검증된 block 인용이 없으면 HUMAN_REVIEW (증거 없는 반려는 자동화하지 않는다).
4. 신뢰도 < 임계값(보정으로 정함) 이면 HUMAN_REVIEW.
5. 2차 의견(다른 모델 계열)이 있고 판정이 다르면 HUMAN_REVIEW.
6. FIX 가 있으면 REVISE — 단, 자동 수정안은 전체 가드레일을 다시 통과한 것만 붙인다. 통과 못 하면 수정안을 폐기하고 finding 만 돌려준다 (FM-19).
7. 그 외 APPROVE.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.schemas import AdCopy, AudienceReport, ChecklistItem, Decision, Layer, PreflightReport, ReviewVerdict, Severity
from guardrails.output_guard import OutputGuardResult


@dataclass
class Adjudication:
    decision: Decision
    reason: str
    rewrite_kept: bool = False


def adjudicate(copy: AdCopy, det: OutputGuardResult, verdict: Optional[ReviewVerdict], threshold: float,
               rewrite_recheck: Optional[OutputGuardResult] = None, audience: Optional[AudienceReport] = None) -> PreflightReport:
    checklist = list(det.checklist)
    if any(f.severity == Severity.BLOCK for f in det.findings):
        adj = Adjudication(Decision.REJECT, "결정론 가드레일에서 반려 사유(block)가 확인됨 — 모델 판정과 무관하게 반려")
    elif verdict is None:
        adj = Adjudication(Decision.HUMAN_REVIEW, "심사 모델 단계가 실패해 자동 판정을 하지 않음 (fail-closed)")
    else:
        sem_block_verified = any(f.layer == Layer.SEMANTIC and f.severity == Severity.BLOCK and f.verified_quote for f in verdict.findings)
        unverified = [f for f in verdict.findings if not f.verified_quote]
        if verdict.decision == Decision.REJECT and not sem_block_verified:
            adj = Adjudication(Decision.HUMAN_REVIEW, "모델이 반려를 제안했지만 원문에서 검증된 반려 근거가 없음 → 사람 검토")
        elif verdict.decision == Decision.REJECT:
            adj = Adjudication(Decision.REJECT, "의미 심사에서 검증된 인용과 함께 반려 사유 확인")
        elif unverified:
            adj = Adjudication(Decision.HUMAN_REVIEW, f"모델 finding {len(unverified)}건의 인용이 원문에 없음(환각 의심) → 사람 검토")
        elif verdict.confidence < threshold:
            adj = Adjudication(Decision.HUMAN_REVIEW, f"심사 신뢰도 {verdict.confidence:.2f} < 자동 판정 임계값 {threshold:.2f}")
        elif verdict.disagreement:
            adj = Adjudication(Decision.HUMAN_REVIEW, f"두 모델 계열의 판정 불일치 ({verdict.decision.value} vs {verdict.second_opinion.value if verdict.second_opinion else '?'})")
        elif verdict.decision == Decision.REVISE or any(f.severity == Severity.FIX for f in det.findings):
            if verdict.compliant_rewrite is not None and rewrite_recheck is not None:
                if rewrite_recheck.decision == Decision.APPROVE:
                    adj = Adjudication(Decision.REVISE, "수정 필요 — 자동 수정안이 전체 가드레일 재검사를 통과해 함께 제시", rewrite_kept=True)
                else:
                    adj = Adjudication(Decision.REVISE, "수정 필요 — 자동 수정안이 재검사에서 다시 걸려 폐기(FM-19). 결정론 finding 을 근거로 광고주가 직접 수정")
                    verdict.compliant_rewrite = None
            else:
                adj = Adjudication(Decision.REVISE, "수정 필요 — 자동 수정안 없음")
        else:
            adj = Adjudication(Decision.APPROVE, f"모든 가드레일 통과, 심사 신뢰도 {verdict.confidence:.2f} ≥ {threshold:.2f}")
    if verdict is not None:
        checklist.append(ChecklistItem(name="의미 심사 (LLM)", passed=verdict.decision == Decision.APPROVE, layer=Layer.SEMANTIC,
                                       detail=f"{verdict.decision.value} · 신뢰도 {verdict.confidence:.2f} · {verdict.provider}/{verdict.model}"))
        if verdict.second_opinion is not None:
            checklist.append(ChecklistItem(name="교차 검증 (다른 모델 계열)", passed=not verdict.disagreement, layer=Layer.SEMANTIC,
                                           detail=f"2차 의견 {verdict.second_opinion.value} · {verdict.second_opinion_provider}"
                                           + ("" if verdict.second_opinion_independent else " · ⚠️ 같은 제공자 — 독립성 없음 (키 하나뿐인 PARTIAL 모드)")))
    checklist.append(ChecklistItem(name="최종 판정", passed=adj.decision == Decision.APPROVE, layer=Layer.STRUCTURAL, detail=adj.reason))
    if verdict is None:
        verdict = ReviewVerdict(variant_id=copy.variant_id, decision=Decision.HUMAN_REVIEW, confidence=0.0, findings=det.findings,
                                rationale="심사 단계 실패 (failsafe)")
    return PreflightReport(variant_id=copy.variant_id, ad_copy=copy, audience=audience, verdict=verdict,
                           final_decision=adj.decision, checklist=checklist, decision_reason=adj.reason)
