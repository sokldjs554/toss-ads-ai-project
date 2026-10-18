"""경계 사례의 교차 확인·업종 판단·재사용 키·비용 상한이 원문 / 자동 수정안 / 직접 재검사에서 같은 규칙으로 움직이는지.

통제된 검토 응답으로 재현한다 (실제 모델 품질 측정이 아니라 판단 규칙 검증).
"""
from __future__ import annotations

import dataclasses

import pytest

from agents.pipeline import RunOptions, run_pipeline
from agents.recheck import recheck, recheck_key
from agents.rewrite_check import check_rewrite
from core.config import Settings
from core.schemas import AdCopy, CampaignBrief, Decision, Fact, ProductCategory, ProductFacts
from guardrails.policy import get_catalog
from llm.errors import BudgetExceeded, ProviderError
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider, scripted_review_provider

CAT = get_catalog()
PRODUCT = ProductFacts(name="OO몰 쿠폰", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)")])
FACTS_TEXT = "- F1: 전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)"
GOOD = AdCopy(headline="전 상품 10% 할인 쿠폰", body="10월 31일까지, 1인 1회 사용할 수 있어요.", cta="자세히 보기", disclosures=["10월 31일까지, 1인 1회"])
SETTINGS = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="sk-ant-fake-not-a-real-key")
TAU = SETTINGS.auto_decision_confidence


def review(decision="APPROVE", confidence=0.95):
    return {"decision": decision, "confidence": confidence, "findings": [], "rationale": "통제된 응답", "rewrite": None}


def router_with(primary, secondary):
    """검토는 openai(primary), 교차 확인은 anthropic(secondary) 로 나가는 통제된 라우터."""
    return Router(SETTINGS, providers={"openai": scripted_review_provider(primary, "openai"), "anthropic": scripted_review_provider(secondary, "anthropic")}, tracer=Tracer())


BORDER = review("APPROVE", 0.85)     # τ=0.8, margin 0.1 → 0.9 미만이라 경계 사례


def first_then_outage(first):
    """첫 호출(1차 검토)만 응답하고, 이어지는 호출(교차 확인과 그 대체 경로)은 모두 장애 — 교차 확인이 정말로 실패하는 상황."""
    state = {"n": 0}

    def behave(req):
        state["n"] += 1
        if state["n"] > 1:
            raise ProviderError("simulated outage", provider="openai")
        return first

    return behave


def failing_cross_check_router():
    return Router(SETTINGS, providers={"openai": scripted_review_provider(first_then_outage(BORDER), "openai"),
                                       "anthropic": scripted_review_provider(ProviderError("simulated outage", provider="anthropic"), "anthropic")}, tracer=Tracer())


def test_borderline_rewrite_is_cross_checked_like_the_original():
    r = router_with(BORDER, review("APPROVE", 0.9))
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU, second_opinion=True)
    assert rc.semantic is not None and rc.semantic.second_opinion == Decision.APPROVE and not rc.semantic.disagreement
    assert rc.decision == Decision.APPROVE and rc.passed is True
    assert r.providers["anthropic"].calls == 1


def test_rewrite_cross_check_disagreement_is_not_a_verified_rewrite():
    r = router_with(BORDER, review("REVISE", 0.9))
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU, second_opinion=True)
    assert rc.semantic.disagreement and rc.decision == Decision.HUMAN_REVIEW and rc.passed is None


def test_rewrite_cross_check_failure_is_not_a_pass():
    r = failing_cross_check_router()
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU, second_opinion=True)
    assert rc.semantic.second_opinion_failed and rc.semantic_error
    assert rc.decision == Decision.HUMAN_REVIEW and rc.passed is None and "교차 확인" in rc.reason


def test_confident_rewrite_does_not_spend_a_cross_check_call():
    r = router_with(review("APPROVE", 0.97), review("APPROVE", 0.97))
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU, second_opinion=True)
    assert rc.decision == Decision.APPROVE and r.providers["anthropic"].calls == 0


def test_cross_check_off_keeps_the_old_scope():
    r = router_with(BORDER, ProviderError("simulated outage", provider="anthropic"))
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU)   # second_opinion 기본값 False: 평가 범위와 같음
    assert rc.decision == Decision.APPROVE and r.providers["anthropic"].calls == 0


@pytest.mark.parametrize("make,expected", [(lambda: router_with(BORDER, review("APPROVE", 0.9)), Decision.APPROVE),
                                           (lambda: router_with(BORDER, review("REVISE", 0.9)), Decision.HUMAN_REVIEW),
                                           (failing_cross_check_router, Decision.HUMAN_REVIEW)])
def test_direct_recheck_applies_the_same_cross_check_as_the_rewrite_check(make, expected):
    res = recheck(GOOD, PRODUCT, {"ecommerce"}, SETTINGS, router=make(), landing_text="", catalog=CAT, tracer=Tracer(), enable_second_opinion=True)
    assert res.report.final_decision == expected
    r2 = make()
    r2.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r2, FACTS_TEXT, tr, TAU, second_opinion=True)
    assert rc.decision == res.report.final_decision


def test_direct_recheck_cross_check_failure_is_a_retryable_failure():
    res = recheck(GOOD, PRODUCT, {"ecommerce"}, SETTINGS, router=failing_cross_check_router(), landing_text="", catalog=CAT, tracer=Tracer(), enable_second_opinion=True)
    assert res.failed and not res.reusable and "교차 확인" in res.review_error
    assert res.report.final_decision == Decision.HUMAN_REVIEW
    assert any(f.stage == "second_opinion" for f in res.failures)


def test_direct_recheck_without_cross_check_is_unchanged():
    r = router_with(BORDER, ProviderError("simulated outage", provider="anthropic"))
    res = recheck(GOOD, PRODUCT, {"ecommerce"}, SETTINGS, router=r, landing_text="", catalog=CAT, tracer=Tracer())
    assert res.report.final_decision == Decision.APPROVE and not res.failed


# ── 재사용 키 ──────────────────────────────────────────────────────────────────
def key(copy=GOOD, settings=SETTINGS, so=False, product=PRODUCT, landing="", history=None):
    return recheck_key(copy, product, landing, settings, CAT, history, so)


def test_key_changes_with_cross_check_and_provider_configuration():
    base = key()
    assert key(so=True) != base
    only_openai = dataclasses.replace(SETTINGS, anthropic_api_key="")
    assert only_openai.mode != SETTINGS.mode and key(settings=only_openai) != base
    only_anthropic = dataclasses.replace(SETTINGS, openai_api_key="")
    assert only_openai.mode == only_anthropic.mode and key(settings=only_openai) != key(settings=only_anthropic)   # 같은 '한 제공자' 방식이어도 어느 제공자인지는 다른 조건이다
    forced = dataclasses.replace(SETTINGS, force_demo=True)
    assert key(settings=forced) != base
    assert key(settings=dataclasses.replace(SETTINGS, auto_decision_confidence=0.9)) != base
    assert key(settings=dataclasses.replace(SETTINGS, openai_model="gpt-other")) != base


def test_key_does_not_confuse_where_one_field_ends_and_the_next_begins():
    a = GOOD.model_copy(update={"headline": "전 상품 10%", "body": "할인 쿠폰 10월 31일까지"})
    b = GOOD.model_copy(update={"headline": "전 상품 10% 할인", "body": "쿠폰 10월 31일까지"})
    assert key(copy=a) != key(copy=b)                      # 이어 붙인 글이 비슷해도 필드 경계가 다르면 다른 문구다
    c = GOOD.model_copy(update={"headline": "전 상품 10%\n할인", "body": "쿠폰"})
    d = GOOD.model_copy(update={"headline": "전 상품 10%", "body": "할인\n쿠폰"})
    assert c.full_text() == d.full_text() and key(copy=c) != key(copy=d)
    d1 = GOOD.model_copy(update={"disclosures": ["가", "나다"]})
    d2 = GOOD.model_copy(update={"disclosures": ["가나", "다"]})
    assert key(copy=d1) != key(copy=d2)
    p1 = PRODUCT.model_copy(update={"facts": [Fact(id="F1", text="가"), Fact(id="F2", text="나")]})
    p2 = PRODUCT.model_copy(update={"facts": [Fact(id="F1", text="가\x1eF2나")]})
    assert key(product=p1) != key(product=p2)


def test_key_is_stable_for_identical_conditions():
    assert key() == key()
    assert key(history=["x"]) == key(history=["x"]) != key(history=["y"])


# ── 업종 판단 ─────────────────────────────────────────────────────────────────
LOAN_REWRITE = AdCopy(headline="누구나 대출 가능", body="전 상품 10% 할인 쿠폰", cta="자세히 보기", disclosures=["10월 31일까지, 1인 1회"])


def test_rewrite_gets_financial_rules_when_its_own_text_brings_a_financial_cue():
    """원문은 이커머스로만 검사됐어도, 수정안 문구가 금융 단서를 가져오면 수정안 검사에는 금융 규칙이 함께 적용된다."""
    r = router_with(review("APPROVE", 0.97), review("APPROVE", 0.97))
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(LOAN_REWRITE, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU)
    assert rc.decision != Decision.APPROVE and any(f.rule_id for f in rc.det.findings)


def test_direct_recheck_adds_the_categories_its_own_text_brings():
    """직접 재검사에 넘긴 업종이 이커머스뿐이어도, 고친 문구가 금융 단서를 가져오면 자동 수정안 검사와 같은 금융 규칙이 적용된다."""
    res = recheck(LOAN_REWRITE, PRODUCT, {"ecommerce"}, SETTINGS, router=router_with(review("APPROVE", 0.97), review("APPROVE", 0.97)), landing_text="", catalog=CAT, tracer=Tracer())
    r = router_with(review("APPROVE", 0.97), review("APPROVE", 0.97))
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(LOAN_REWRITE, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU)
    assert res.report.final_decision != Decision.APPROVE and res.report.final_decision == rc.decision
    assert {f.rule_id for f in res.report.verdict.findings if f.layer.value != "semantic"} == {f.rule_id for f in rc.det.findings}


def test_direct_recheck_and_pipeline_use_the_same_category_function():
    p = ProductFacts(name="상품", category=ProductCategory.ECOMMERCE, raw_description="신용대출 상담 연결", facts=[Fact(id="F1", text="전 상품 10% 할인")])
    assert CAT.categories_for(p, "안녕") == CAT.effective_categories(p.category, "안녕", "신용대출 상담 연결", "전 상품 10% 할인")
    assert len(CAT.categories_for(p, "안녕")) >= 2


# ── 비용·호출 상한 ──────────────────────────────────────────────────────────────
def test_budget_exceeded_in_rewrite_check_is_flagged_not_swallowed_as_a_normal_failure():
    r = router_with(BudgetExceeded("실행 예산 초과"), review())
    r.new_run()
    tr = Tracer()
    with tr.trace("t"):
        rc = check_rewrite(GOOD, PRODUCT, {"ecommerce"}, "", [], CAT, r, FACTS_TEXT, tr, TAU)
    assert rc.budget_exceeded and rc.semantic_error_code == "FM-20" and rc.decision == Decision.HUMAN_REVIEW


def test_budget_exceeded_while_checking_a_rewrite_halts_the_run_and_keeps_every_variant():
    """수정안 검사 중 상한에 닿으면 다음 광고안으로 넘어가지 않고 FM-20 으로 멈춘다 — 판정하지 못한 광고안도 사라지지 않고, 검증 못 한 수정안은 나오지 않는다.
    호출 상한을 3~15 로 바꿔 가며 '수정안 의미 검사' 단계에서 걸리는 경우를 찾아 확인한다 (대역 응답이라 호출 순서가 결정적이다)."""
    from tests.test_pipeline import brief

    hit = []
    settings = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    for cap in range(3, 16):
        tr = Tracer()
        router = Router(settings, tracer=tr)
        orig = router.new_run

        def tight(orig=orig, router=router, cap=cap):
            orig()
            router.budget.max_calls = cap

        router.new_run = tight
        run = run_pipeline(brief("신용대출, 월 300만원, 신청 전환"), settings, router=router, options=RunOptions(n_variants=3), tracer=tr)
        if any(f.code == "FM-20" and f.stage == "rewrite_recheck" for f in run.failures):
            hit.append((cap, run))
    assert hit, "수정안 검사 단계에서 상한에 닿는 경우를 만들지 못했다"
    for cap, run in hit:
        assert run.halted and "FM-20" in run.halt_reason, cap
        assert len(run.reports) == 3, cap                                   # 판정하지 못한 광고안도 사라지지 않는다
        assert all(r.rewrite_status.value != "verified" for r in run.reports), cap
