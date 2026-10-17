"""파이프라인 — 단계를 잇고, 각 단계를 트레이스 스팬과 failsafe 로 감싼다.

순서: Safety Intercept → Planner → Copywriter → (소재마다) Output Guard → Audience → Reviewer(+2차 의견) → Adjudicate
어느 단계가 실패해도 예외가 바깥으로 새지 않는다. 실패는 run.failures 에 FM 코드로 남고, 판정은 HUMAN_REVIEW 로 떨어진다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.config import Settings
from core.schemas import CampaignBrief, Decision, PipelineRun, ReviewVerdict, RewriteStatus
from guardrails import input_guard, output_guard
from guardrails.failsafe import failsafe
from guardrails.policy import PolicyCatalog, get_catalog
from llm.base import Task
from llm.errors import BudgetExceeded
from llm.router import Router
from observability.tracer import Tracer, get_tracer

from . import adjudicator, audience as audience_agent, copywriter, planner, reviewer
from .prompts import PROMPT_VERSION


ProgressCallback = Callable[[str, str], None]  # (stage_key, human_label)


@dataclass
class RunOptions:
    n_variants: int = 3
    landing_text: str = ""
    rejected_history: list[str] = field(default_factory=list)
    enable_audience: bool = True
    enable_second_opinion: bool = True
    second_opinion_margin: float = 0.1  # 임계값 + margin 아래면 경계 사례로 보고 2차 의견
    on_progress: Optional[ProgressCallback] = None  # UI 진행 표시용 — 실제 단계가 시작될 때 불린다


def _progress(options: "RunOptions", key: str, label: str) -> None:
    if options.on_progress is not None:
        try:
            options.on_progress(key, label)
        except Exception:  # noqa: BLE001 — 진행 표시 실패가 파이프라인을 멈추면 안 된다
            pass


def run_pipeline(brief: CampaignBrief, settings: Settings, router: Optional[Router] = None,
                 options: Optional[RunOptions] = None, catalog: Optional[PolicyCatalog] = None,
                 tracer: Optional[Tracer] = None) -> PipelineRun:
    options = options or RunOptions()
    catalog = catalog or get_catalog()
    tracer = tracer or get_tracer()
    router = router or Router(settings, tracer=tracer)
    router.new_run()
    run = PipelineRun(mode=settings.mode.value, brief=brief, effective_input=brief.advertiser_input)
    t0 = time.perf_counter()
    with tracer.trace("preflight", run_id=run.run_id, mode=settings.mode.value, prompt_version=PROMPT_VERSION,
                      policy_version=catalog.version, product=brief.product.name, category=brief.product.category.value) as trace:
        run.trace_id = trace.trace_id
        try:
            _run_stages(run, brief, settings, router, options, catalog, tracer)
        except BudgetExceeded as e:
            run.halted = True
            run.halt_reason = f"FM-20 실행 예산 초과로 중단: {e}"
            _escalate_pending(run)
    run.total_cost_usd = trace.total_cost_usd
    run.total_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return run


def _escalate_pending(run: PipelineRun) -> None:
    for r in run.reports:
        if r.final_decision == Decision.APPROVE and r.verdict.rationale == "":
            r.final_decision = Decision.HUMAN_REVIEW


def _run_stages(run: PipelineRun, brief: CampaignBrief, settings: Settings, router: Router, options: RunOptions,
                catalog: PolicyCatalog, tracer: Tracer) -> None:
    # 0) Safety Intercept ---------------------------------------------------
    _progress(options, "intercept", "요청 검사")
    with tracer.span("input_guard:intercept", kind="guardrail") as span:
        notice, effective_input, description, categories = input_guard.intercept(brief, catalog, router=router)
        run.intercept = notice
        run.effective_input = effective_input
        span.set(triggered=notice.triggered, kind=notice.kind.value if notice.kind else None, hard_block=notice.hard_block,
                 findings=[f.model_dump(mode="json") for f in notice.findings], categories=sorted(categories),
                 removed=notice.removed_phrases, suggested_input=notice.suggested_input)
        if notice.triggered:
            span.status = "intercepted"
        if notice.hard_block:
            run.halted = True
            run.halt_reason = notice.message
            return
    facts_text = "\n".join(f"- {f.id}: {f.text}" for f in brief.product.facts) or "- (없음)"

    # 1) Planner --------------------------------------------------------------
    _progress(options, "planner", "캠페인 구성")
    with tracer.span("planner", kind="stage") as span:
        with failsafe(run, stage="planner", fallback="plan=None, 소재 생성은 계속") as g:
            pr = planner.plan_campaign(brief, effective_input, categories, description, router)
            run.plan = pr.plan
            span.set(objective=pr.plan.objective.value, ad_sets=len(pr.plan.ad_sets), repaired=pr.plan.repaired,
                     issues=[i.__dict__ for i in pr.issues], reasoning=pr.plan.planner_reasoning, provider=pr.provider)
            if pr.issues:
                span.event("plan_repaired", issues=[i.message for i in pr.issues])
                if pr.plan.repaired:
                    run.failures.append(_fm02(len(pr.issues)))

    # 2) Copywriter -----------------------------------------------------------
    _progress(options, "copywriter", "광고 초안 작성")
    with tracer.span("copywriter", kind="stage") as span:
        with failsafe(run, stage="copywriter", fallback="실행 중단 (생성할 소재가 없음)") as g:
            cr = copywriter.write_copy(brief, run.plan, categories, description, catalog, router, n=options.n_variants)
            span.set(variants=len(cr.variants), provider=cr.provider, repaired=cr.repaired, dropped=cr.dropped)
        if g.failed:
            run.halted = True
            run.halt_reason = f"소재 생성 실패 ({g.event.code if g.event else '?'}) — 사람에게 넘김"
            return

    # 3~6) per variant ---------------------------------------------------------
    for idx, copy in enumerate(cr.variants, start=1):
        _progress(options, "review", f"광고안 {idx}/{len(cr.variants)} 검사")
        with tracer.span(f"variant:{copy.variant_id}", kind="stage", headline=copy.headline):
            with tracer.span("output_guard", kind="guardrail") as span:
                det = output_guard.check(copy, brief.product, categories, options.landing_text, options.rejected_history, catalog)
                span.set(decision=det.decision.value, findings=[f.model_dump(mode="json") for f in det.findings],
                         checklist=[c.model_dump(mode="json") for c in det.checklist])
                if det.findings:
                    span.status = "intercepted"

            aud = None
            if options.enable_audience:
                with tracer.span("audience", kind="stage") as span:
                    with failsafe(run, stage="audience", fallback="패널 없이 진행") as g:
                        aud = audience_agent.simulate(copy, router)
                        span.set(mean_trust=aud.mean_trust, mean_appeal=aud.mean_appeal, diversity=aud.diversity,
                                 sycophancy=aud.sycophancy_flag, click_rate=aud.click_rate)
                        if aud.sycophancy_flag:
                            span.event("sycophancy_flag", diversity=aud.diversity)
                            run.failures.append(_fm15(copy.variant_id, aud.diversity))

            verdict: Optional[ReviewVerdict] = None
            with tracer.span("reviewer", kind="stage") as span:
                with failsafe(run, stage="reviewer", fallback="HUMAN_REVIEW") as g:
                    ro = reviewer.review(copy, det, categories, facts_text, options.landing_text, catalog, router)
                    verdict = ro.verdict
                    span.set(decision=verdict.decision.value, confidence=verdict.confidence, unverified_quotes=ro.unverified_quotes,
                             unknown_rule_ids=ro.unknown_rule_ids, provider=verdict.provider)
                    if ro.unverified_quotes:
                        span.event("hallucinated_quote", count=ro.unverified_quotes)
                        run.failures.append(_fm04(copy.variant_id, ro.unverified_quotes))
                    if ro.unknown_rule_ids:
                        run.failures.append(_fm03(copy.variant_id, ro.unknown_rule_ids))
                    if ro.overruled_by_det:
                        span.event("det_authoritative_overrule", count=ro.overruled_by_det)
                        run.failures.append(_fm23(copy.variant_id, ro.overruled_by_det))

            # 교차 검증: 경계 사례만 (비용을 아끼면서 상관 오류를 줄인다)
            if verdict is not None and options.enable_second_opinion and _is_borderline(verdict, settings, options):
                with tracer.span("second_opinion", kind="stage") as span:
                    with failsafe(run, stage="second_opinion", fallback="2차 의견 없이 진행") as g:
                        so = reviewer.review(copy, det, categories, facts_text, options.landing_text, catalog, router, task=Task.SECOND_OPINION)
                        verdict.second_opinion = so.verdict.decision
                        verdict.second_opinion_provider = so.verdict.provider
                        verdict.second_opinion_independent = so.verdict.provider != verdict.provider
                        verdict.disagreement = so.verdict.decision != verdict.decision
                        span.set(decision=so.verdict.decision.value, disagreement=verdict.disagreement, provider=so.verdict.provider,
                                 independent=verdict.second_opinion_independent)
                        if not verdict.second_opinion_independent:
                            span.event("same_provider_second_opinion", provider=so.verdict.provider)
                        if verdict.disagreement:
                            span.event("disagreement")
                            run.failures.append(_fm17(copy.variant_id))

            # 수정안 재검사 (FM-19) — 모델이 제안한 수정안은 전체 가드레일을 다시 통과해야만 '검증된 수정안' 이 된다
            recheck = None
            if verdict is not None and verdict.proposed_rewrite is not None:
                with tracer.span("rewrite_recheck", kind="guardrail") as span:
                    recheck = output_guard.check(verdict.proposed_rewrite, brief.product, categories, options.landing_text, [], catalog)
                    span.set(decision=recheck.decision.value, findings=[f.model_dump(mode="json") for f in recheck.findings])
                    if recheck.decision != Decision.APPROVE:
                        span.status = "intercepted"

            with tracer.span("adjudicate", kind="guardrail") as span:
                report = adjudicator.adjudicate(copy, det, verdict, settings.auto_decision_confidence, recheck, aud)
                span.set(final=report.final_decision.value, reason=report.decision_reason,
                         rewrite_status=report.rewrite_status.value, rewrite_note=report.rewrite_note)
                if report.rewrite_status == RewriteStatus.FAILED_RECHECK:
                    run.failures.append(_fm19(copy.variant_id, report.final_decision, [f.rule_id for f in report.rewrite_recheck_findings]))
                run.reports.append(report)


def _is_borderline(verdict: ReviewVerdict, settings: Settings, options: RunOptions) -> bool:
    if verdict.decision == Decision.REJECT:
        return False  # 검증된 반려는 교차 검증 불필요
    return verdict.confidence < settings.auto_decision_confidence + options.second_opinion_margin or any(
        f.severity.value == "warn" for f in verdict.findings)


# FailureEvent 헬퍼 -------------------------------------------------------------
from core.schemas import FailureEvent  # noqa: E402


def _fm02(n: int) -> FailureEvent:
    return FailureEvent(code="FM-02", stage="planner", message=f"플랫폼 제약 위반 {n}건을 코드가 수리", fallback="repaired plan")


def _fm03(v: str, n: int) -> FailureEvent:
    return FailureEvent(code="FM-03", stage="reviewer", message=f"{v}: 카탈로그에 없는 rule_id {n}건을 버림", fallback="finding dropped")


def _fm04(v: str, n: int) -> FailureEvent:
    return FailureEvent(code="FM-04", stage="reviewer", message=f"{v}: 원문에 없는 인용 {n}건 → warn 강등·신뢰도 상한", fallback="HUMAN_REVIEW 후보")


def _fm23(v: str, n: int) -> FailureEvent:
    return FailureEvent(code="FM-23", stage="reviewer", message=f"{v}: 코드가 충족으로 확인한 고지·근거·구조를 모델이 위반이라 함 {n}건 → 버림", fallback="finding dropped")


def _fm15(v: str, d: float) -> FailureEvent:
    return FailureEvent(code="FM-15", stage="audience", message=f"{v}: 패널 분산 {d} — 아첨 의심, 점수는 참고용", fallback="가중치 0")


def _fm17(v: str) -> FailureEvent:
    return FailureEvent(code="FM-17", stage="second_opinion", message=f"{v}: 두 모델 계열 판정 불일치", fallback="HUMAN_REVIEW")


def _fm19(v: str, final: Decision, rule_ids: list[str]) -> FailureEvent:
    rules = ", ".join(sorted(set(rule_ids))) or "?"
    return FailureEvent(code="FM-19", stage="rewrite_recheck",
                        message=f"{v}: 자동 수정안이 재검사에서 다시 걸림 ({rules})",
                        fallback=f"수정안 폐기 — 제시하지 않음. 원문 판정 {final.value} 은 그대로")
