"""사용자가 직접 고친 문구(또는 수정 제안을 편집한 문구)를 같은 검사 경로로 다시 검사한다.

Studio 의 '수정한 문구 다시 검사' 가 이것을 부른다. 결정론 가드레일 → 의미 심사 → (수정안 없음) → 판정 — 파이프라인과 같은 함수들을 쓴다.
판정 로직을 화면 쪽에 다시 만들지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from core.config import Settings
from core.schemas import AdCopy, Claim, FailureEvent, PreflightReport, ProductFacts
from guardrails import output_guard
from guardrails.failsafe import classify_exception
from guardrails.policy import PolicyCatalog, get_catalog
from llm.router import Router
from observability.tracer import Tracer, get_tracer

from . import adjudicator, reviewer
from .crosscheck import DEFAULT_MARGIN, apply_second_opinion, is_borderline
from .prompts import PROMPT_VERSION
from .reviewer import carried_fact_ids, relink_claims
from .rewrite_check import check_rewrite


def copy_fingerprint(copy: AdCopy) -> str:
    """편집된 문구의 지문 — 재검사 결과가 '지금 화면의 문구' 에 대한 것인지 묶어 두는 데 쓴다."""
    return hashlib.sha256(copy.full_text().encode("utf-8")).hexdigest()[:12]


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def recheck_categories(catalog: PolicyCatalog, product: ProductFacts, copy: AdCopy, base: Iterable[str] = ()) -> set[str]:
    """재검사에 적용할 업종 — 처음 검사에 적용한 업종 + 고친 문구가 가져오는 단서 (화면이 키를 만들 때와 recheck() 가 검사할 때 같은 값을 쓰도록 한 곳에서 정한다)."""
    return set(base) | catalog.categories_for(product, copy.full_text())


def recheck_key(copy: AdCopy, product: ProductFacts, landing_text: str, settings: Settings, catalog: PolicyCatalog,
                rejected_history: Optional[list[str]] = None, second_opinion: bool = False, categories: Optional[Iterable[str]] = None,
                second_opinion_margin: float = DEFAULT_MARGIN) -> str:
    """재검사 결과를 다시 써도 되는 '같은 조건' 의 지문.

    문구만이 아니라 결과를 바꿀 수 있는 모든 것을 넣는다: 상품 정보(이름·업종·사실·설명)·랜딩 문구·반려 이력, 검사 기준 버전·프롬프트 버전,
    실행 방식·모델·자동 판정 기준, 어느 제공자의 키가 쓰이는지, 교차 확인 사용 여부와 경계 기준, 적용 업종, 이어받은 근거 사실. 하나라도 다르면 이전 결과를 쓰지 않고 다시 검사한다.
    각 항목은 따로 해시한 뒤 합쳐서, 항목 사이의 경계가 달라 우연히 같아지는 일이 없게 한다.
    """
    payload = {
        "copy": _digest(copy.headline, copy.body, copy.cta, *copy.disclosures), "claims": _digest(*[f"{c.text}\x1e{c.fact_id}" for c in copy.claims]),
        "product": _digest(product.name, product.category.value, product.raw_description or "", *[f"{f.id}\x1e{f.text}" for f in product.facts]),
        "landing": _digest(landing_text), "history": _digest(*(rejected_history or [])),
        "policy": catalog.version, "prompt": PROMPT_VERSION, "mode": settings.mode.value,
        "providers": [settings.has_openai, settings.has_anthropic], "models": [settings.openai_model, settings.anthropic_model],
        "tau": settings.auto_decision_confidence, "second_opinion": bool(second_opinion), "margin": second_opinion_margin if second_opinion else None,
        "categories": sorted(categories) if categories is not None else None, "carried": sorted(copy.carried_fact_ids),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


@dataclass
class RecheckResult:
    report: PreflightReport
    fingerprint: str
    mode: str
    cost_usd: float
    cost_estimated: bool
    key: str = ""                    # 재사용 키 (recheck_key) — 같은 키의 '성공한' 결과만 다시 쓴다
    trace_id: str = ""               # 이 재검사 자체의 처리 기록
    review_error: str = ""           # 검토 모델 호출이 실패한 이유. 비어 있으면 모델 판정이 있었다
    rewrite_error: str = ""          # 수정안 의미 검사 호출이 실패한 이유
    calls: int = 0                   # 성공 응답 수
    attempts: int = 0                # 제공자에게 보낸 요청 수 (실패·재시도 포함)
    failed_attempts: int = 0         # 응답(사용량)을 받지 못한 요청 수 — 그 비용은 알 수 없다
    failures: list = field(default_factory=list)   # FailureEvent
    info: dict = field(default_factory=dict)       # 검사 당시 설정: 모델·기준값·프롬프트·검사 기준 버전
    at: datetime = field(default_factory=datetime.now)

    @property
    def failed(self) -> bool:
        return bool(self.review_error or self.rewrite_error)

    @property
    def reusable(self) -> bool:
        """모델 호출이 끝까지 성공한 결과만 같은 조건에서 다시 쓴다. 실패한 결과는 다시 시도한다."""
        return not self.failed


@dataclass
class RecheckHistory:
    """광고안 하나의 재검사 시도 기록. 같은 조건의 성공은 재사용하고, 실패는 다시 시도하며, 실패한 시도의 기록과 사용량은 지우지 않는다."""

    attempts: list = field(default_factory=list)   # RecheckResult, 오래된 것부터

    def add(self, r: RecheckResult) -> None:
        self.attempts.append(r)

    def reusable_for(self, key: str) -> Optional[RecheckResult]:
        for r in reversed(self.attempts):
            if r.key == key and r.reusable:
                return r
        return None

    def latest_for(self, key: str) -> Optional[RecheckResult]:
        """지금 조건(key)에 해당하는 가장 최근 시도 — 실패한 시도일 수 있다. 다른 문구의 결과는 돌려주지 않는다."""
        for r in reversed(self.attempts):
            if r.key == key:
                return r
        return None

    def latest(self) -> Optional[RecheckResult]:
        return self.attempts[-1] if self.attempts else None

    def attempts_for(self, key: str) -> list:
        return [r for r in self.attempts if r.key == key]

    def usage_for(self, key: str) -> dict:
        rs = self.attempts_for(key)
        return {"tries": len(rs), "failed_tries": sum(1 for r in rs if r.failed), "cost_usd": round(sum(r.cost_usd for r in rs), 6),
                "calls": sum(r.calls for r in rs), "attempts": sum(r.attempts for r in rs), "failed_attempts": sum(r.failed_attempts for r in rs),
                "cost_estimated": any(r.cost_estimated or r.failed_attempts for r in rs)}


def build_edited_copy(original: AdCopy, headline: str, body: str, cta: str, disclosures: list[str]) -> AdCopy:
    """편집본. 원문의 주장→근거 연결은 새 문구에 그대로 남은 것만 이어받는다."""
    edited = AdCopy(variant_id=original.variant_id, headline=headline.strip(), body=body.strip(), cta=cta.strip(),
                    disclosures=[d.strip() for d in disclosures if d.strip()], tone=(original.tone.replace(" (수정안)", "") + " (직접 수정)").strip())
    edited.claims = relink_claims(original.claims, edited.full_text())
    edited.carried_fact_ids = carried_fact_ids(original)
    return edited


def recheck(copy: AdCopy, product: ProductFacts, categories: set[str], settings: Settings, router: Optional[Router] = None,
            landing_text: str = "", rejected_history: Optional[list[str]] = None, catalog: Optional[PolicyCatalog] = None,
            tracer: Optional[Tracer] = None, enable_second_opinion: bool = False, second_opinion_margin: float = DEFAULT_MARGIN) -> RecheckResult:
    """enable_second_opinion: 원문 검사와 같은 조건(경계 사례)에서 다른 모델 계열의 교차 확인을 받는다. 필요한데 호출이 실패하면 담당자 확인이다."""
    if not (copy.headline.strip() or copy.body.strip()):
        raise ValueError("검사할 문구가 비어 있습니다 (제목과 본문이 모두 비어 있음)")
    catalog = catalog or get_catalog()
    tracer = tracer or get_tracer()
    router = router or Router(settings, tracer=tracer)
    router.new_run()
    facts_text = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
    failures: list[FailureEvent] = []
    review_error = ""
    categories = recheck_categories(catalog, product, copy, categories)
    with tracer.trace("recheck", product=product.name, variant_id=copy.variant_id, fingerprint=copy_fingerprint(copy), mode=settings.mode.value,
                      prompt_version=PROMPT_VERSION, policy_version=catalog.version) as trace:
        with tracer.span("output_guard", kind="guardrail") as span:
            det = output_guard.check(copy, product, categories, landing_text, rejected_history or [], catalog)
            span.set(decision=det.decision.value, findings=[f.model_dump(mode="json") for f in det.findings])
        verdict = None
        with tracer.span("reviewer", kind="stage") as span:
            try:
                ro = reviewer.review(copy, det, categories, facts_text, landing_text, catalog, router)
                verdict = ro.verdict
                span.set(decision=verdict.decision.value, confidence=verdict.confidence, unverified_quotes=ro.unverified_quotes)
            except Exception as e:  # noqa: BLE001 — 심사 실패는 사람 검토로 (fail-closed). 실패는 결과에 남겨 같은 문구를 다시 시도할 수 있게 한다
                review_error = f"{type(e).__name__}: {str(e)[:200]}"
                failures.append(FailureEvent(code=classify_exception(e), stage="reviewer", message=review_error, fallback="HUMAN_REVIEW — 같은 문구로 다시 시도할 수 있음"))
                span.status = "error"
                span.event("reviewer_failed", error=review_error)
        if verdict is not None and enable_second_opinion and is_borderline(verdict, settings.auto_decision_confidence, second_opinion_margin):
            with tracer.span("second_opinion", kind="stage") as span:
                try:
                    apply_second_opinion(copy, det, categories, facts_text, landing_text, catalog, router, verdict)
                    span.set(decision=verdict.second_opinion.value, disagreement=verdict.disagreement, provider=verdict.second_opinion_provider,
                             independent=verdict.second_opinion_independent)
                except Exception as e:  # noqa: BLE001 — 교차 확인이 필요한데 못 했다 → 판정 함수가 담당자 확인으로 보낸다. 같은 문구로 다시 시도할 수 있게 실패로 남긴다
                    review_error = f"교차 확인 실패 — {type(e).__name__}: {str(e)[:200]}"
                    failures.append(FailureEvent(code=classify_exception(e), stage="second_opinion", message=review_error,
                                                 fallback="담당자 확인 — 같은 문구로 다시 시도할 수 있음"))
                    span.status = "error"
        recheck_rw = None
        if verdict is not None and verdict.proposed_rewrite is not None:
            recheck_rw = check_rewrite(verdict.proposed_rewrite, product, categories, landing_text, rejected_history or [], catalog, router, facts_text, tracer,
                                       settings.auto_decision_confidence, second_opinion=enable_second_opinion, second_opinion_margin=second_opinion_margin)
            if recheck_rw.semantic_error:
                failures.append(FailureEvent(code=recheck_rw.semantic_error_code or "FM-05", stage="rewrite_recheck", message=recheck_rw.semantic_error,
                                             fallback="수정안은 '확인하지 못함' 으로 두고 제시하지 않음"))
        with tracer.span("adjudicate", kind="guardrail") as span:
            report = adjudicator.adjudicate(copy, det, verdict, settings.auto_decision_confidence, recheck_rw, None)
            span.set(final=report.final_decision.value, reason=report.decision_reason, rewrite_status=report.rewrite_status.value)
    b = router.budget
    return RecheckResult(report=report, fingerprint=copy_fingerprint(copy), mode=settings.mode.value, cost_usd=b.spent_usd, cost_estimated=b.estimated,
                         key=recheck_key(copy, product, landing_text, settings, catalog, rejected_history, enable_second_opinion, categories, second_opinion_margin), trace_id=trace.trace_id,
                         review_error=review_error, rewrite_error=(recheck_rw.semantic_error if recheck_rw is not None else ""),
                         calls=b.calls, attempts=b.attempts, failed_attempts=b.failed_attempts, failures=failures,
                         info={"mode": settings.mode.value, "openai_model": settings.openai_model, "anthropic_model": settings.anthropic_model,
                               "threshold": settings.auto_decision_confidence, "prompt_version": PROMPT_VERSION, "policy_version": catalog.version,
                               "second_opinion": bool(enable_second_opinion)})
