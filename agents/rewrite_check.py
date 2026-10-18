"""모델이 제안한 수정안의 재검사 — 원문·직접 재검사와 '같은 기준' 으로 판단한다.

판단 기준은 하나다: 코드 검사(표현·안내·상품 정보 일치·형식·중복) + 검토 모델의 의미 검사를 `adjudicator.decide()` 에 넣는다.
따라서 같은 문구·상품 정보·검토 응답·임계값이면 자동 수정안 검사와 '수정한 문구 다시 검사' 가 같은 결론을 낸다.
- 모델이 APPROVE 라고만 답해서는 통과하지 못한다: 신뢰도가 임계값보다 낮거나, 원문에 없는 인용이 있거나, 검토 호출이 실패하면 '확인하지 못함' 이다.
- 코드 검사에서 반려 사유(block)가 확인되면 모델을 부르지 않아도 결론이 같다(REJECT). 그 밖에는 모델을 불러 같은 함수로 판단한다.
- 수정안 상태는 원문 판정과 따로 정한다 (수정안 검사가 실패해도 원래 광고의 판정을 바꾸지 않는다).
수정안이 또 수정안을 내더라도 그것은 따라가지 않는다 (한 단계만).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.schemas import AdCopy, Decision, Finding, Layer, ProductFacts, ReviewVerdict
from guardrails import output_guard
from guardrails.failsafe import classify_exception
from guardrails.output_guard import OutputGuardResult
from guardrails.policy import PolicyCatalog
from llm.router import Router
from observability.tracer import Tracer

from . import adjudicator, reviewer


@dataclass
class RewriteCheck:
    det: OutputGuardResult
    semantic: Optional[ReviewVerdict] = None     # None = 의미 검사를 하지 않았거나(코드 반려) 하지 못함(모델 실패)
    semantic_error: str = ""
    semantic_error_code: str = ""                # 의미 검사 호출이 실패했을 때의 실패 모드 코드 (FM-05/07/20 …)
    decision: Decision = Decision.HUMAN_REVIEW   # 공통 판정 기준(decide)을 수정안 문구에 적용한 결과
    reason: str = ""
    inputs: Optional[adjudicator.DecisionInputs] = None

    @classmethod
    def judge(cls, det: OutputGuardResult, semantic: Optional[ReviewVerdict], threshold: float, semantic_error: str = "",
              semantic_error_code: str = "") -> "RewriteCheck":
        """코드 검사 결과와 (있다면) 의미 검사 결과를 공통 판정 함수에 넣어 수정안의 판단을 정한다."""
        inp = adjudicator.inputs_from(det, semantic, None)
        inp.has_rewrite = False  # 수정안의 수정안은 따라가지 않는다
        adj = adjudicator.decide(inp, threshold)
        return cls(det=det, semantic=semantic, semantic_error=semantic_error, semantic_error_code=semantic_error_code, decision=adj.decision, reason=adj.reason, inputs=inp)

    @property
    def passed(self) -> Optional[bool]:
        """True = 공통 기준을 통과(APPROVE), False = 고치거나 쓸 수 없음(REVISE·REJECT), None = 확인하지 못함(HUMAN_REVIEW)."""
        if self.decision == Decision.APPROVE:
            return True
        if self.decision == Decision.HUMAN_REVIEW:
            return None
        return False

    @property
    def findings(self) -> list[Finding]:
        out = list(self.det.findings)
        if self.semantic is not None:
            out += [f for f in self.semantic.findings if f.layer == Layer.SEMANTIC and f.verified_quote]
        return out


def check_rewrite(rewrite: AdCopy, product: ProductFacts, categories: set[str], landing_text: str, rejected_history: list[str],
                  catalog: PolicyCatalog, router: Optional[Router], facts_text: str, tracer: Tracer, threshold: float) -> RewriteCheck:
    """threshold: 자동 판정에 필요한 최소 확신 — 원문 판정과 같은 값을 넘긴다."""
    with tracer.span("rewrite_recheck", kind="guardrail") as span:
        det = output_guard.check(rewrite, product, categories, landing_text, rejected_history, catalog)
        semantic: Optional[ReviewVerdict] = None
        error = ""
        error_code = ""
        det_block = any(f.severity.value == "block" for f in det.findings)
        if det_block:
            span.event("semantic_recheck_skipped", reason="code block — 결론이 같아 모델을 부르지 않음")
        elif router is None:
            span.event("semantic_recheck_skipped", reason="no reviewer")
        else:
            try:
                ro = reviewer.review(rewrite, det, categories, facts_text, landing_text, catalog, router, label_suffix="rewrite")
                semantic = ro.verdict
                span.set(semantic_decision=semantic.decision.value, semantic_confidence=semantic.confidence, unverified_quotes=ro.unverified_quotes)
            except Exception as e:  # noqa: BLE001 — 의미 검사 실패 → 공통 기준에서 '확인하지 못함'. 실패는 호출한 쪽(실행·평가)이 세도록 코드와 함께 남긴다
                error = f"{type(e).__name__}: {e}"[:200]
                error_code = classify_exception(e)
                span.event("semantic_recheck_failed", error=error, code=error_code)
                span.status = "error"
        rc = RewriteCheck.judge(det, semantic, threshold, error, error_code)
        # decision = 공통 기준의 결과(수정안의 최종 상태를 정하는 값). 코드 검사만의 결과는 det_decision.
        span.set(decision=rc.decision.value, reason=rc.reason, det_decision=det.decision.value, threshold=threshold,
                 findings=[f.model_dump(mode="json") for f in rc.findings])
        if rc.decision != Decision.APPROVE and span.status != "error":
            span.status = "intercepted"
        return rc
