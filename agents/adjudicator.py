"""최종 판정은 모델이 아니라 이 코드가 한다. 승인으로 가는 길은 좁고, 사람 검토로 가는 길은 넓다 (fail-closed).

판정 규칙 (위에서부터 먼저 맞는 것) — `decide()` 하나에만 있다. 실제 서비스(pipeline)와 평가(evals)가 같은 함수를 쓰므로
같은 입력·같은 모델 응답·같은 임계값이면 같은 판정이 나온다. 평가의 임계값별 분석도 이 함수를 다시 부른다.
1. 결정론 BLOCK 이 하나라도 있으면 REJECT — 모델은 이를 뒤집을 수 없다.
2. 심사 모델이 실패했으면 HUMAN_REVIEW.
3. 모델이 REJECT 라 했지만 검증된 block 인용이 없으면 HUMAN_REVIEW (증거 없는 반려는 자동화하지 않는다).
   모델이 REVISE/REJECT 를 말했는데 근거 finding 이 하나도 남지 않았어도(인용 없음, 또는 코드가 반증·FM-23) HUMAN_REVIEW.
4. 신뢰도 < 임계값(보정으로 정함) 이면 HUMAN_REVIEW.
5. 2차 의견(다른 모델 계열)이 있고 판정이 다르면 HUMAN_REVIEW.
6. FIX 가 있으면 REVISE.
7. 그 외 APPROVE.

수정안은 판정과 '따로' 관리한다 (RewriteStatus). 재검사를 통과한 수정안만 verified_rewrite 로 돌려주고, 통과하지 못한 수정안은
final_decision 이 무엇이든(REVISE·REJECT·HUMAN_REVIEW) 절대 제시하지 않는다 (FM-19).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from core.schemas import (
    AdCopy,
    AudienceReport,
    ChecklistItem,
    Decision,
    Layer,
    PreflightReport,
    ReviewVerdict,
    RewriteStatus,
    Severity,
)
from guardrails.output_guard import OutputGuardResult

if False:  # 타입 힌트 전용 (순환 import 회피)
    from agents.rewrite_check import RewriteCheck


@dataclass
class DecisionInputs:
    """판정에 필요한 원본 — 저장해 두면 다른 임계값으로 그대로 재생할 수 있다."""

    det_block: bool = False
    det_fix: bool = False
    verdict_available: bool = False
    model_decision: Optional[str] = None        # APPROVE | REVISE | REJECT (모델이 말한, 완화 불가 규칙 적용 후)
    confidence: float = 0.0
    sem_block_verified: bool = False            # 검증된 인용을 가진 semantic block finding 이 있는가
    unverified_quotes: int = 0                  # 원문에 없는 인용(환각) 수
    unsupported_decision: bool = False          # 모델이 수정/반려를 말했지만 남은 근거 finding 이 없음
    disagreement: bool = False                  # 2차 의견 불일치
    second_opinion: Optional[str] = None
    has_rewrite: bool = False
    rewrite_passed: Optional[bool] = None       # None = 재검사 없음/불가. 코드 검사 + 의미 검사 모두 통과해야 True
    rewrite_semantic: Optional[str] = None      # 수정안에 대한 검토 모델 판정 (재생용)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionInputs":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Adjudication:
    decision: Decision
    reason: str
    rewrite_status: RewriteStatus = RewriteStatus.NONE
    rewrite_note: str = ""


def inputs_from(det: OutputGuardResult, verdict: Optional[ReviewVerdict],
                rewrite_recheck: Optional["OutputGuardResult | RewriteCheck"] = None) -> DecisionInputs:
    inp = DecisionInputs(det_block=any(f.severity == Severity.BLOCK for f in det.findings),
                         det_fix=any(f.severity == Severity.FIX for f in det.findings))
    if verdict is None:
        return inp
    inp.verdict_available = True
    inp.model_decision = verdict.decision.value
    inp.confidence = float(verdict.confidence)
    inp.sem_block_verified = any(f.layer == Layer.SEMANTIC and f.severity == Severity.BLOCK and f.verified_quote for f in verdict.findings)
    inp.unverified_quotes = sum(1 for f in verdict.findings if not f.verified_quote)
    inp.unsupported_decision = bool(verdict.unsupported_decision)
    inp.disagreement = bool(verdict.disagreement)
    inp.second_opinion = verdict.second_opinion.value if verdict.second_opinion else None
    inp.has_rewrite = verdict.proposed_rewrite is not None
    if inp.has_rewrite and rewrite_recheck is not None:
        if hasattr(rewrite_recheck, "passed"):  # RewriteCheck: 코드 검사 + 의미 검사
            inp.rewrite_passed = rewrite_recheck.passed
            inp.rewrite_semantic = rewrite_recheck.semantic.decision.value if rewrite_recheck.semantic is not None else None
        else:  # 코드 검사만 (OutputGuardResult) — 의미 검사가 없으므로 '통과' 로 확정하지 않는다
            inp.rewrite_passed = False if rewrite_recheck.decision != Decision.APPROVE else None
    return inp


def decide(inp: DecisionInputs, threshold: float) -> Adjudication:
    """순수 함수. 같은 입력·같은 임계값 → 같은 판정."""
    if inp.det_block:
        adj = Adjudication(Decision.REJECT, "결정론 가드레일에서 반려 사유(block)가 확인됨 — 모델 판정과 무관하게 반려")
    elif not inp.verdict_available:
        adj = Adjudication(Decision.HUMAN_REVIEW, "심사 모델 단계가 실패해 자동 판정을 하지 않음 (fail-closed)")
    elif inp.model_decision == Decision.REJECT.value and not inp.sem_block_verified:
        adj = Adjudication(Decision.HUMAN_REVIEW, "모델이 반려를 제안했지만 원문에서 검증된 반려 근거가 없음 → 사람 검토")
    elif inp.model_decision == Decision.REJECT.value:
        adj = Adjudication(Decision.REJECT, "의미 심사에서 검증된 인용과 함께 반려 사유 확인")
    elif inp.unverified_quotes:
        adj = Adjudication(Decision.HUMAN_REVIEW, f"모델 finding {inp.unverified_quotes}건의 인용이 원문에 없음(환각 의심) → 사람 검토")
    elif inp.unsupported_decision:
        adj = Adjudication(Decision.HUMAN_REVIEW, "모델이 수정/반려를 제안했지만 남은 근거 finding 이 없음(인용 없음 또는 코드가 반증) → 사람 검토")
    elif inp.confidence < threshold:
        adj = Adjudication(Decision.HUMAN_REVIEW, f"심사 신뢰도 {inp.confidence:.2f} < 자동 판정 임계값 {threshold:.2f}")
    elif inp.disagreement:
        adj = Adjudication(Decision.HUMAN_REVIEW, f"두 모델 계열의 판정 불일치 ({inp.model_decision} vs {inp.second_opinion or '?'})")
    elif inp.model_decision == Decision.REVISE.value or inp.det_fix:
        adj = Adjudication(Decision.REVISE, "수정 필요 — 결정론 finding 또는 검증된 의미 finding 이 fix 등급")
    else:
        adj = Adjudication(Decision.APPROVE, f"모든 가드레일 통과, 심사 신뢰도 {inp.confidence:.2f} ≥ {threshold:.2f}")
    # 수정안 상태는 판정과 독립이다. 어떤 판정 경로에서도 재검사 실패/미실시 수정안은 '검증됨' 이 될 수 없다.
    if not inp.has_rewrite:
        adj.rewrite_status, adj.rewrite_note = RewriteStatus.NONE, "자동 수정안 없음"
    elif inp.rewrite_passed is True:
        adj.rewrite_status, adj.rewrite_note = RewriteStatus.VERIFIED, "자동 수정안이 코드 검사와 의미 검사를 모두 다시 통과해 함께 제시"
    elif inp.rewrite_passed is False:
        adj.rewrite_status, adj.rewrite_note = RewriteStatus.FAILED_RECHECK, "자동 수정안이 재검사에서 다시 걸려 폐기(FM-19) — 제시하지 않음"
    else:
        adj.rewrite_status, adj.rewrite_note = RewriteStatus.UNCHECKED, "자동 수정안의 의미 검사를 하지 못해 제시하지 않음"
    return adj


def adjudicate(copy: AdCopy, det: OutputGuardResult, verdict: Optional[ReviewVerdict], threshold: float,
               rewrite_recheck: Optional["OutputGuardResult | RewriteCheck"] = None, audience: Optional[AudienceReport] = None) -> PreflightReport:
    inp = inputs_from(det, verdict, rewrite_recheck)
    adj = decide(inp, threshold)
    rc_decision: Optional[Decision] = None
    rc_findings: list = []
    if inp.has_rewrite and rewrite_recheck is not None:
        rc_findings = list(rewrite_recheck.findings)
        rc_decision = rewrite_recheck.decision  # RewriteCheck: 공통 기준의 결과 / OutputGuardResult: 코드 검사 결과
        reason = getattr(rewrite_recheck, "reason", "")
        if reason and adj.rewrite_status != RewriteStatus.VERIFIED:
            adj.rewrite_note += f" — 수정안 판단: {reason}"
    checklist = list(det.checklist)
    if verdict is not None:
        checklist.append(ChecklistItem(name="의미 심사 (LLM)", passed=verdict.decision == Decision.APPROVE, layer=Layer.SEMANTIC,
                                       detail=f"{verdict.decision.value} · 신뢰도 {verdict.confidence:.2f} · {verdict.provider}/{verdict.model}"))
        if verdict.second_opinion is not None:
            checklist.append(ChecklistItem(name="교차 검증 (다른 모델 계열)", passed=not verdict.disagreement, layer=Layer.SEMANTIC,
                                           detail=f"2차 의견 {verdict.second_opinion.value} · {verdict.second_opinion_provider}"
                                           + ("" if verdict.second_opinion_independent else " · ⚠️ 같은 제공자 — 독립성 없음 (키 하나뿐인 PARTIAL 모드)")))
    if inp.has_rewrite:
        checklist.append(ChecklistItem(name="자동 수정안 재검사", passed=adj.rewrite_status == RewriteStatus.VERIFIED, layer=Layer.STRUCTURAL,
                                       detail=adj.rewrite_note))
    checklist.append(ChecklistItem(name="최종 판정", passed=adj.decision == Decision.APPROVE, layer=Layer.STRUCTURAL, detail=adj.reason))
    if verdict is None:
        verdict = ReviewVerdict(variant_id=copy.variant_id, decision=Decision.HUMAN_REVIEW, confidence=0.0, findings=det.findings,
                                rationale="심사 단계 실패 (failsafe)")
    verified = verdict.proposed_rewrite if adj.rewrite_status == RewriteStatus.VERIFIED else None
    return PreflightReport(
        variant_id=copy.variant_id, ad_copy=copy, audience=audience, verdict=verdict,
        final_decision=adj.decision, checklist=checklist, decision_reason=adj.reason,
        rewrite_status=adj.rewrite_status, verified_rewrite=verified,
        rewrite_recheck_decision=rc_decision,
        rewrite_recheck_findings=rc_findings,
        rewrite_note=adj.rewrite_note,
    )
