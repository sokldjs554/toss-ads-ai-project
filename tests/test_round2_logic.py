"""2차 내부 재점검(검토자 공격)에서 재현된 로직 결함의 회귀 테스트 — 원문 경로의 교차 확인·업종·상한, 재사용 키, 교차 확인의 독립성, 평가·레드팀 집계.

통제된 응답으로 판단 규칙을 확인한다 (실제 모델 품질 측정이 아니다).
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from agents.adjudicator import adjudicate
from agents.pipeline import RunOptions, run_pipeline
from agents.recheck import build_edited_copy, recheck, recheck_categories, recheck_key
from core.config import Settings
from core.schemas import AdCopy, CampaignBrief, Claim, Decision, Fact, ProductCategory, ProductFacts, RewriteStatus
from guardrails import output_guard
from guardrails.policy import get_catalog
from llm.base import Task
from llm.errors import BudgetExceeded, ProviderError
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import TaskScriptedProvider

CAT = get_catalog()
LIVE = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="sk-ant-fake-not-a-real-key", force_demo=False)
PARTIAL = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="", force_demo=False)
PRODUCT = ProductFacts(name="OO몰 쿠폰", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)")])
BRIEF = CampaignBrief(advertiser_input="가을 쿠폰 캠페인, 월 200만원, 구매 전환", product=PRODUCT, monthly_budget_krw=2_000_000)


def review(decision="APPROVE", confidence=0.95, rewrite=None, findings=()):
    return {"decision": decision, "confidence": confidence, "findings": [{"rule_id": r, "quote": q, "explanation": "통제된 응답"} for r, q in findings],
            "rationale": "통제된 응답", "rewrite": rewrite}


def copy_response(headline="전 상품 10% 할인 쿠폰", n=1):
    variant = {"headline": headline, "body": "전 상품 10% 할인 쿠폰, 10월 31일까지 1인 1회.", "cta": "쿠폰 받기", "claims": [{"text": "전 상품 10% 할인", "fact_id": "F1"}],
               "disclosures": ["10월 31일까지, 1인 1회"], "tone": "x"}
    return {"variants": [dict(variant, headline=f"{headline}{'' if i == 0 else i}") for i in range(n)]}


def make_router(settings, openai_handlers=None, anthropic_handlers=None, **kw):
    oa = TaskScriptedProvider("openai", openai_handlers)
    an = TaskScriptedProvider("anthropic", anthropic_handlers)
    return Router(settings, providers={"openai": oa, "anthropic": an}, tracer=Tracer()), oa, an


def run_original(settings, openai_handlers, anthropic_handlers=None, n=1, **opts):
    handlers = {Task.COPY: copy_response(n=n), **(anthropic_handlers or {})}
    r, oa, an = make_router(settings, openai_handlers, handlers)
    run = run_pipeline(BRIEF, settings, router=r, options=RunOptions(n_variants=n, enable_audience=False, **opts), tracer=r.tracer)
    return run, oa, an


# ── 원문 경로의 교차 확인 ────────────────────────────────────────────────────────
def test_original_path_sends_a_borderline_verdict_to_the_cross_check_and_approves_when_it_agrees():
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.85)}, {Task.SECOND_OPINION: review("APPROVE", 0.9)}, enable_second_opinion=True)
    rep = run.reports[0]
    assert rep.final_decision == Decision.APPROVE and rep.verdict.second_opinion == Decision.APPROVE and rep.verdict.second_opinion_independent
    assert an.count(Task.SECOND_OPINION) == 1


def test_original_path_does_not_pass_a_borderline_verdict_when_the_required_cross_check_fails():
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.85), Task.SECOND_OPINION: ProviderError("outage", provider="openai")},
                               {Task.SECOND_OPINION: ProviderError("outage", provider="anthropic")}, enable_second_opinion=True)
    rep = run.reports[0]
    assert rep.final_decision == Decision.HUMAN_REVIEW and rep.verdict.second_opinion_failed and "교차 확인" in rep.decision_reason
    assert any(f.stage == "second_opinion" for f in run.failures)
    assert any(c.name.startswith("교차 검증") and not c.passed for c in rep.checklist)           # 체크리스트에도 실패가 보인다


def test_original_path_skips_the_cross_check_for_confident_verdicts_and_when_it_is_off():
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.95)}, {Task.SECOND_OPINION: review("APPROVE", 0.9)}, enable_second_opinion=True)
    assert an.count(Task.SECOND_OPINION) == 0 and run.reports[0].final_decision == Decision.APPROVE
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.85)}, {Task.SECOND_OPINION: review("APPROVE", 0.9)}, enable_second_opinion=False)
    assert an.count(Task.SECOND_OPINION) == 0 and run.reports[0].final_decision == Decision.APPROVE


def test_original_path_respects_the_margin():
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.85)}, {Task.SECOND_OPINION: review("APPROVE", 0.9)}, enable_second_opinion=True, second_opinion_margin=0.0)
    assert an.count(Task.SECOND_OPINION) == 0       # 0.85 ≥ τ(0.8) + 0.0 — 경계가 아니다


def test_original_path_passes_the_cross_check_flag_to_the_rewrite_check():
    rewrite = {"headline": "전 상품 10% 할인 쿠폰", "body": "10월 31일까지, 1인 1회 사용할 수 있어요.", "cta": "자세히 보기", "claims": [], "disclosures": ["10월 31일까지, 1인 1회"], "tone": "x"}
    handlers = {Task.REVIEW: review("REVISE", 0.85, rewrite), Task.SECOND_OPINION: review("REVISE", 0.9)}
    run, oa, an = run_original(LIVE, {Task.REVIEW: lambda req: review("APPROVE", 0.85) if "rewrite" in (req.label or "") else review("REVISE", 0.85, rewrite)},
                               {Task.SECOND_OPINION: review("APPROVE", 0.9)}, enable_second_opinion=True)
    assert an.count(Task.SECOND_OPINION) == 2          # 원문 1 + 수정안 1 — 수정안 검사도 같은 조건에서 교차 확인을 받는다
    run, oa, an = run_original(LIVE, {Task.REVIEW: lambda req: review("APPROVE", 0.85) if "rewrite" in (req.label or "") else review("REVISE", 0.85, rewrite)},
                               {Task.SECOND_OPINION: review("APPROVE", 0.9)}, enable_second_opinion=False)
    assert an.count(Task.SECOND_OPINION) == 0


def test_a_same_provider_fallback_is_not_a_cross_check_when_both_providers_are_configured():
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.85), Task.SECOND_OPINION: review("APPROVE", 0.95)},
                               {Task.SECOND_OPINION: ProviderError("outage", provider="anthropic")}, enable_second_opinion=True)
    rep = run.reports[0]
    assert rep.verdict.second_opinion_provider == "openai" and not rep.verdict.second_opinion_independent
    assert rep.final_decision == Decision.HUMAN_REVIEW and rep.verdict.second_opinion_failed       # 같은 제공자가 대신 답한 것은 교차 확인이 아니다
    assert any(f.stage == "second_opinion" for f in run.failures)


def test_a_same_provider_second_opinion_is_accepted_when_only_one_provider_is_configured():
    """키가 하나뿐인 실행에서는 처음부터 독립성이 없다고 화면에 표시하는 설계 — 담당자 확인으로 막지 않는다."""
    r = Router(PARTIAL, providers={"openai": TaskScriptedProvider("openai", {Task.REVIEW: review("APPROVE", 0.85), Task.SECOND_OPINION: review("APPROVE", 0.9)}),
                                   "anthropic": TaskScriptedProvider("anthropic", {}, )}, tracer=Tracer())
    r.providers["anthropic"].available = False
    run = run_pipeline(BRIEF, PARTIAL, router=r, options=RunOptions(n_variants=1, enable_audience=False, enable_second_opinion=True), tracer=r.tracer)
    rep = run.reports[0]
    assert rep.final_decision == Decision.APPROVE and not rep.verdict.second_opinion_independent and not rep.verdict.second_opinion_failed


# ── 원문 경로의 업종 ─────────────────────────────────────────────────────────────
def test_original_path_applies_the_categories_its_generated_copy_brings_like_a_recheck():
    run, oa, an = run_original(LIVE, {Task.REVIEW: review("APPROVE", 0.95)}, {Task.COPY: copy_response("쿠폰 받고 대출 걱정 없이 쇼핑")})
    rep = run.reports[0]
    assert "FIN-DISCLOSE-LOAN-01" in {f.rule_id for f in rep.verdict.findings} and rep.final_decision == Decision.REVISE
    edited = build_edited_copy(rep.ad_copy, rep.ad_copy.headline, rep.ad_copy.body, rep.ad_copy.cta, rep.ad_copy.disclosures)
    r2, _, _ = make_router(LIVE, {Task.REVIEW: review("APPROVE", 0.95)})
    res = recheck(edited, PRODUCT, set(run.categories), LIVE, router=r2, catalog=CAT, tracer=Tracer())
    assert res.report.final_decision == rep.final_decision            # 안 고친 문구를 다시 검사해도 판정이 뒤집히지 않는다


# ── 비용·호출 상한 ──────────────────────────────────────────────────────────────
def test_cap_hit_in_the_rewrite_check_keeps_the_variants_finished_review_and_counts_one_event():
    rewrite = {"headline": "전 상품 10% 할인 쿠폰", "body": "10월 31일까지, 1인 1회 사용할 수 있어요.", "cta": "자세히 보기", "claims": [], "disclosures": ["10월 31일까지, 1인 1회"], "tone": "x"}

    def reviewer(req):
        if "rewrite" in (req.label or ""):
            return BudgetExceeded("실행 예산 초과")
        return review("REVISE", 0.95, rewrite, [("TA-OMISSION-01", "전 상품 10% 할인 쿠폰")])

    run, oa, an = run_original(LIVE, {Task.REVIEW: reviewer}, n=2)
    assert run.halted and "FM-20" in run.halt_reason and len(run.reports) == 2
    first, second = run.reports
    assert first.final_decision == Decision.REVISE and first.verdict.provider == "openai" and first.rewrite_status == RewriteStatus.UNCHECKED       # 끝난 검토 결과를 버리지 않는다
    assert second.final_decision == Decision.HUMAN_REVIEW and "FM-20" in second.decision_reason                                               # 검사하지 못한 광고안은 담당자 확인
    assert sum(1 for f in run.failures if f.code == "FM-20") == 1                                                                             # 같은 사건을 두 번 세지 않는다


def test_eval_budget_check_counts_attempts_as_well_as_cost():
    from evals.runner import _budget_hit

    r = Router(PARTIAL, providers={"openai": TaskScriptedProvider("openai")}, tracer=Tracer())
    assert not _budget_hit(r)
    r.budget.attempts = r.budget.max_calls
    assert _budget_hit(r)
    r.budget.attempts, r.budget.spent_usd = 0, r.budget.cap_usd
    assert _budget_hit(r)


# ── 재사용 키 ────────────────────────────────────────────────────────────────────
def test_reuse_key_covers_carried_facts_categories_and_the_margin():
    base = AdCopy(headline="무료로 체험해 보세요", body="지금 가입", cta="시작하기")
    carried = base.model_copy(update={"carried_fact_ids": ["F1"]})
    k = lambda copy=base, **kw: recheck_key(copy, PRODUCT, "", LIVE, CAT, [], **kw)
    assert k() != k(carried)
    assert k(second_opinion=True, second_opinion_margin=0.1) != k(second_opinion=True, second_opinion_margin=0.0)
    assert k(categories={"ecommerce"}) != k(categories={"ecommerce", "loan"})
    assert k(categories={"ecommerce"}) == k(categories={"ecommerce"})


def test_recheck_categories_is_the_same_union_the_recheck_uses():
    copy = AdCopy(headline="쿠폰 받고 대출 걱정 없이 쇼핑", body="x", cta="y")
    assert recheck_categories(CAT, PRODUCT, copy, {"ecommerce"}) >= {"ecommerce"} and len(recheck_categories(CAT, PRODUCT, copy, {"ecommerce"})) >= 2


def test_blank_copy_is_not_approved():
    empty = AdCopy(headline="", body="", cta="")
    r, _, _ = make_router(LIVE, {Task.REVIEW: review("APPROVE", 0.97)})
    with pytest.raises(ValueError):
        recheck(empty, PRODUCT, {"ecommerce"}, LIVE, router=r, catalog=CAT, tracer=Tracer())


# ── 레드팀·처리 기록 집계 ─────────────────────────────────────────────────────────
def test_redteam_aggregates_exclude_errored_attacks_and_flag_lower_bound_cost():
    from evals.redteam import Attack, _redteam_report
    from evals.runner import baseline_allowed, load_dataset

    r = Router(PARTIAL, providers={"openai": TaskScriptedProvider("openai")}, tracer=Tracer())
    r.budget.failed()                                    # 응답을 받지 못한 요청 1건
    seeds = load_dataset("redteam_seeds")
    atk = lambda **kw: Attack(seed_id="S-01", strategy="spacing", headline="h", body="b", cta="c", intent="i", **kw)
    attacks = [atk(predicted="REJECT"), atk(predicted="APPROVE", evaded=True), atk(predicted="HUMAN_REVIEW", error="Timeout: x"), atk(predicted="HUMAN_REVIEW", error="Timeout: y")]
    rep = _redteam_report(PARTIAL, r, seeds, attacks, [])
    s = rep["by_strategy"]["spacing"]
    assert (s["n"], s["evaded"], s["human_review"], s["errors"]) == (2, 1, 0, 2) and s["evasion_rate"] == 0.5 and rep["evasion_rate"] == 0.5 and rep["judged"] == 2
    assert rep["failed_requests"] == 1 and rep["cost_estimated"] is True
    assert baseline_allowed(rep)[0] is False             # 장애가 섞였다
    partial = _redteam_report(PARTIAL, r, seeds[:1], [atk(predicted="REJECT")], [], 1)
    assert baseline_allowed(partial)[0] is False and "일부" in baseline_allowed(partial)[1] or "제한" in baseline_allowed(partial)[1]


# ── 출력 위치 ────────────────────────────────────────────────────────────────────
def test_disclosure_negation_filter_follows_the_real_disclosure_start_even_when_a_field_is_empty():
    P = ProductFacts(name="상품", category=ProductCategory.INVESTMENT, facts=[Fact(id="F1", text="총보수 연 0.15%")])
    copy = AdCopy(headline="인덱스 펀드", body="총보수 연 0.15%", cta="", disclosures=["원금 보장하지 않습니다", "투자에는 원금 손실 가능성이 있습니다"])
    det = output_guard.check(copy, P, {"investment"}, "", [], CAT)
    assert not [f for f in det.findings if "부정문" in f.explanation]          # 고지문 속 '보장하지 않습니다' 는 정상 — 빈 CTA 가 있어도


def test_reviewer_rewrite_inherits_the_original_claims_facts():
    rewrite = {"headline": "무료로 체험해 보세요", "body": "지금 가입", "cta": "시작하기", "claims": [], "disclosures": [], "tone": "x"}
    run, oa, an = run_original(LIVE, {Task.REVIEW: lambda req: review("APPROVE", 0.95) if "rewrite" in (req.label or "") else review("REVISE", 0.95, rewrite)})
    assert run.reports[0].verdict.proposed_rewrite.carried_fact_ids == ["F1"]


# ── 테스트가 환경변수와 무관한가 ────────────────────────────────────────────────────
@pytest.mark.parametrize("env", [{"PREFLIGHT_AUTO_DECISION_CONFIDENCE": "0.5"}, {"PREFLIGHT_RUN_BUDGET_USD": "0.0001"}, {"PREFLIGHT_FORCE_DEMO": "1"}])
def test_module_level_settings_do_not_pick_up_the_developers_environment(env):
    """수집(import) 시점에 Settings.from_env() 를 부르는 테스트 모듈이 개발자 셸의 설정을 읽으면 결과가 달라진다 — conftest 가 import 때 비운다."""
    import os

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_crosscheck_parity.py", "tests/test_rewrite_parity.py"],
                          cwd=root, env={**os.environ, **env}, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-1500:]
