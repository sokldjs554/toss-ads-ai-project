"""모델이 제안한 수정안의 재검사 — 원문과 '같은 검사' 를 거친다: 코드 검사(표현·안내·상품 정보 일치·형식·중복) + 검토 모델의 의미 검사.

파이프라인·직접 수정 재검사·평가가 모두 이 함수를 쓴다. 코드 검사를 통과한 수정안만 의미 검사를 받고(비용), 의미 검사가 실패하면
'재검사하지 못함' 으로 남겨 제시하지 않는다. 수정안이 또 수정안을 내더라도 그것은 따라가지 않는다 (한 단계만).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.schemas import AdCopy, Decision, Finding, Layer, ProductFacts, ReviewVerdict
from guardrails import output_guard
from guardrails.output_guard import OutputGuardResult
from guardrails.policy import PolicyCatalog
from llm.router import Router
from observability.tracer import Tracer

from . import reviewer


@dataclass
class RewriteCheck:
    det: OutputGuardResult
    semantic: Optional[ReviewVerdict] = None     # None = 의미 검사를 하지 못함 (코드 검사에서 이미 걸렸거나 모델 실패)
    semantic_error: str = ""

    @property
    def passed(self) -> Optional[bool]:
        """True = 코드 검사와 의미 검사 모두 통과, False = 어느 하나에서 걸림, None = 의미 검사를 못 해 판단 불가."""
        if self.det.decision != Decision.APPROVE:
            return False
        if self.semantic is None:
            return None
        return self.semantic.decision == Decision.APPROVE

    @property
    def findings(self) -> list[Finding]:
        out = list(self.det.findings)
        if self.semantic is not None:
            out += [f for f in self.semantic.findings if f.layer == Layer.SEMANTIC and f.verified_quote]
        return out


def check_rewrite(rewrite: AdCopy, product: ProductFacts, categories: set[str], landing_text: str, rejected_history: list[str],
                  catalog: PolicyCatalog, router: Optional[Router], facts_text: str, tracer: Tracer) -> RewriteCheck:
    with tracer.span("rewrite_recheck", kind="guardrail") as span:
        det = output_guard.check(rewrite, product, categories, landing_text, rejected_history, catalog)
        span.set(decision=det.decision.value, findings=[f.model_dump(mode="json") for f in det.findings])
        rc = RewriteCheck(det=det)
        if det.decision != Decision.APPROVE:
            span.status = "intercepted"
            return rc
        if router is None:
            span.event("semantic_recheck_skipped", reason="no reviewer")
            return rc
        try:
            ro = reviewer.review(rewrite, det, categories, facts_text, landing_text, catalog, router, label_suffix="rewrite")
            rc.semantic = ro.verdict
            span.set(semantic_decision=ro.verdict.decision.value, semantic_confidence=ro.verdict.confidence, unverified_quotes=ro.unverified_quotes)
            if ro.verdict.decision != Decision.APPROVE:
                span.status = "intercepted"
        except Exception as e:  # noqa: BLE001 — 의미 검사 실패 → 수정안은 '재검사하지 못함'
            rc.semantic_error = f"{type(e).__name__}: {e}"[:200]
            span.event("semantic_recheck_failed", error=rc.semantic_error)
            span.status = "error"
        return rc
