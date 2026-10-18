"""사용자가 직접 고친 문구(또는 수정 제안을 편집한 문구)를 같은 검사 경로로 다시 검사한다.

Studio 의 '수정한 문구 다시 검사' 가 이것을 부른다. 결정론 가드레일 → 의미 심사 → (수정안 없음) → 판정 — 파이프라인과 같은 함수들을 쓴다.
판정 로직을 화면 쪽에 다시 만들지 않는다.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from core.config import Settings
from core.schemas import AdCopy, Claim, PreflightReport, ProductFacts
from guardrails import output_guard
from guardrails.policy import PolicyCatalog, get_catalog
from llm.router import Router
from observability.tracer import Tracer, get_tracer

from . import adjudicator, reviewer
from .reviewer import relink_claims
from .rewrite_check import check_rewrite


def copy_fingerprint(copy: AdCopy) -> str:
    """편집된 문구의 지문 — 재검사 결과가 '지금 화면의 문구' 에 대한 것인지 묶어 두는 데 쓴다."""
    return hashlib.sha256(copy.full_text().encode("utf-8")).hexdigest()[:12]


@dataclass
class RecheckResult:
    report: PreflightReport
    fingerprint: str
    mode: str
    cost_usd: float
    cost_estimated: bool


def build_edited_copy(original: AdCopy, headline: str, body: str, cta: str, disclosures: list[str]) -> AdCopy:
    """편집본. 원문의 주장→근거 연결은 새 문구에 그대로 남은 것만 이어받는다."""
    edited = AdCopy(variant_id=original.variant_id, headline=headline.strip(), body=body.strip(), cta=cta.strip(),
                    disclosures=[d.strip() for d in disclosures if d.strip()], tone=(original.tone.replace(" (수정안)", "") + " (직접 수정)").strip())
    edited.claims = relink_claims(original.claims, edited.full_text())
    return edited


def recheck(copy: AdCopy, product: ProductFacts, categories: set[str], settings: Settings, router: Optional[Router] = None,
            landing_text: str = "", rejected_history: Optional[list[str]] = None, catalog: Optional[PolicyCatalog] = None,
            tracer: Optional[Tracer] = None) -> RecheckResult:
    catalog = catalog or get_catalog()
    tracer = tracer or get_tracer()
    router = router or Router(settings, tracer=tracer)
    router.new_run()
    facts_text = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
    with tracer.trace("recheck", product=product.name, variant_id=copy.variant_id, fingerprint=copy_fingerprint(copy), mode=settings.mode.value):
        with tracer.span("output_guard", kind="guardrail") as span:
            det = output_guard.check(copy, product, categories, landing_text, rejected_history or [], catalog)
            span.set(decision=det.decision.value, findings=[f.model_dump(mode="json") for f in det.findings])
        verdict = None
        with tracer.span("reviewer", kind="stage") as span:
            try:
                ro = reviewer.review(copy, det, categories, facts_text, landing_text, catalog, router)
                verdict = ro.verdict
                span.set(decision=verdict.decision.value, confidence=verdict.confidence, unverified_quotes=ro.unverified_quotes)
            except Exception as e:  # noqa: BLE001 — 심사 실패는 사람 검토로 (fail-closed)
                span.status = "error"
                span.event("reviewer_failed", error=str(e)[:200])
        recheck_rw = None
        if verdict is not None and verdict.proposed_rewrite is not None:
            recheck_rw = check_rewrite(verdict.proposed_rewrite, product, categories, landing_text, rejected_history or [], catalog, router, facts_text, tracer,
                                       settings.auto_decision_confidence)
        with tracer.span("adjudicate", kind="guardrail") as span:
            report = adjudicator.adjudicate(copy, det, verdict, settings.auto_decision_confidence, recheck_rw, None)
            span.set(final=report.final_decision.value, reason=report.decision_reason, rewrite_status=report.rewrite_status.value)
    return RecheckResult(report=report, fingerprint=copy_fingerprint(copy), mode=settings.mode.value,
                         cost_usd=router.budget.spent_usd, cost_estimated=router.budget.estimated)
