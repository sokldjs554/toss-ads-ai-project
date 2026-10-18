"""원문·자동 수정안·직접 재검사가 같은 기준으로 판단하는지 — 통제된 검토 응답으로 재현한다 (실제 모델 품질 측정이 아니다).

재현 조건(리뷰에서 확인된 문제): 자동 판단 기준 0.8, 수정안에 대한 모델 응답 APPROVE, 모델이 반환한 확신 0.1.
수정 전에는 자동 수정안 경로가 '검증됨' 으로 수정안을 내고, 같은 문구를 직접 재검사하면 HUMAN_REVIEW 였다.
"""
from __future__ import annotations

import dataclasses

import pytest

from agents.adjudicator import adjudicate
from agents.recheck import recheck
from agents.rewrite_check import RewriteCheck, check_rewrite
from core.config import Settings
from core.schemas import AdCopy, Decision, Fact, ProductCategory, ProductFacts, ReviewVerdict, RewriteStatus
from guardrails import output_guard
from guardrails.policy import get_catalog
from llm.errors import ProviderError
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider, scripted_review_provider

CAT = get_catalog()
PRODUCT = ProductFacts(name="OO몰 쿠폰", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)")])
FACTS_TEXT = "- F1: 전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)"
GOOD = AdCopy(headline="전 상품 10% 할인 쿠폰", body="10월 31일까지, 1인 1회 사용할 수 있어요.", cta="자세히 보기", disclosures=["10월 31일까지, 1인 1회"])
BLOCKED = AdCopy(headline="타사보다 2배 적립", body="전 상품 10% 할인 쿠폰", cta="자세히 보기", disclosures=["10월 31일까지, 1인 1회"])
SETTINGS = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="")
TAU = SETTINGS.auto_decision_confidence  # 0.8


def review(decision="APPROVE", confidence=0.95, findings=()):
    return {"decision": decision, "confidence": confidence, "findings": [{"rule_id": r, "quote": q, "explanation": "통제된 응답"} for r, q in findings],
            "rationale": "통제된 응답", "rewrite": None}


def both(response, rewrite=GOOD, tau=None):
    """같은 문구·상품 정보·검토 응답·기준으로 자동 수정안 검사와 직접 재검사를 돌린다."""
    settings = SETTINGS if tau is None else dataclasses.replace(SETTINGS, auto_decision_confidence=tau)
    pa = scripted_review_provider(response)
    ra = Router(settings, providers={"openai": pa, "anthropic": FakeProvider("anthropic", available=False)}, tracer=Tracer())
    ra.new_run()
    tr = Tracer()
    with tr.trace("auto"):
        auto = check_rewrite(rewrite, PRODUCT, {"ecommerce"}, "", [], CAT, ra, FACTS_TEXT, tr, settings.auto_decision_confidence)
    pd = scripted_review_provider(response)
    rd = Router(settings, providers={"openai": pd, "anthropic": FakeProvider("anthropic", available=False)}, tracer=Tracer())
    direct = recheck(rewrite, PRODUCT, {"ecommerce"}, settings, router=rd, landing_text="", catalog=CAT, tracer=Tracer()).report
    return auto, direct, pa, pd


STATUS_OF = {Decision.APPROVE: True, Decision.REVISE: False, Decision.REJECT: False, Decision.HUMAN_REVIEW: None}


@pytest.mark.parametrize("name,response,rewrite,expected", [
    ("low_confidence_but_APPROVE", review("APPROVE", 0.1), GOOD, Decision.HUMAN_REVIEW),          # 리뷰에서 확인된 재현 조건
    ("just_below_threshold", review("APPROVE", 0.79), GOOD, Decision.HUMAN_REVIEW),
    ("at_threshold", review("APPROVE", 0.80), GOOD, Decision.APPROVE),
    ("confident_APPROVE", review("APPROVE", 0.95), GOOD, Decision.APPROVE),
    ("fabricated_quote", review("REVISE", 0.95, [("TA-OMISSION-01", "문구에 전혀 없는 인용")]), GOOD, Decision.HUMAN_REVIEW),
    # 모델은 확신 높게 APPROVE 라고 답했지만 근거로 든 인용이 문구에 없다 → 통과로 확정하지 않는다 (원문 판정과 같은 기준)
    ("approve_with_fabricated_quote", review("APPROVE", 0.95, [("TA-OMISSION-01", "문구에 전혀 없는 인용")]), GOOD, Decision.HUMAN_REVIEW),
    ("verified_quote_revise", review("REVISE", 0.95, [("TA-OMISSION-01", "사용할 수 있어요")]), GOOD, Decision.REVISE),
    ("provider_failure", ProviderError("simulated outage", provider="openai"), GOOD, Decision.HUMAN_REVIEW),
    ("code_block", review("APPROVE", 0.99), BLOCKED, Decision.REJECT),                           # 코드 반려는 모델 응답과 무관
])
def test_auto_rewrite_check_and_direct_recheck_reach_the_same_conclusion(name, response, rewrite, expected):
    auto, direct, _, _ = both(response, rewrite)
    assert auto.decision == expected, (name, auto.decision, auto.reason)
    assert direct.final_decision == expected, (name, direct.final_decision, direct.decision_reason)
    assert auto.decision == direct.final_decision
    assert auto.passed is STATUS_OF[expected]


def test_model_saying_approve_is_not_enough_for_a_verified_rewrite():
    auto, _, _, _ = both(review("APPROVE", 0.1))
    assert auto.semantic is not None and auto.semantic.decision == Decision.APPROVE  # 모델은 APPROVE 라고 답했다
    assert auto.passed is None and auto.decision == Decision.HUMAN_REVIEW and "신뢰도" in auto.reason
    sem = auto.semantic
    orig = ReviewVerdict(variant_id="v", decision=Decision.REVISE, confidence=0.95, proposed_rewrite=GOOD)
    report = adjudicate(AdCopy(headline="지금 아니면 못 받는 할인", body="x", cta="y"), output_guard.check(GOOD, PRODUCT, {"ecommerce"}), orig, TAU, auto)
    assert report.rewrite_status == RewriteStatus.UNCHECKED and report.verified_rewrite is None
    assert sem.confidence == pytest.approx(0.1)


def test_provider_failure_is_recorded_and_rewrite_is_not_presented():
    auto, direct, _, _ = both(ProviderError("simulated outage", provider="openai"))
    assert auto.semantic is None and "ProviderUnavailable" in auto.semantic_error
    assert auto.passed is None and direct.final_decision == Decision.HUMAN_REVIEW


def test_code_block_skips_the_model_but_reaches_the_same_conclusion():
    auto, direct, pa, pd = both(review("APPROVE", 0.99), BLOCKED)
    assert pa.calls == 0 and pd.calls >= 1           # 자동 경로는 결론이 정해져 있어 모델을 부르지 않는다
    assert auto.decision == direct.final_decision == Decision.REJECT


@pytest.mark.parametrize("original", [Decision.REVISE, Decision.REJECT, Decision.HUMAN_REVIEW])
@pytest.mark.parametrize("rewrite_outcome", ["verified", "failed", "unchecked"])
def test_original_decision_is_independent_of_rewrite_status_and_unverified_rewrites_are_never_shown(original, rewrite_outcome):
    """원문 판정과 수정안 상태는 독립이다: 수정안 검사 결과가 무엇이든 원문 판정은 그대로, 검증되지 않은 수정안은 어느 경로에서도 노출되지 않는다."""
    copy = AdCopy(headline="지금 아니면 못 받는 할인", body="x", cta="y")
    det = output_guard.check(copy, PRODUCT, {"ecommerce"})
    conf = 0.95 if original != Decision.HUMAN_REVIEW else 0.3
    verdict = ReviewVerdict(variant_id="v", decision=Decision.REJECT if original == Decision.REJECT else Decision.REVISE, confidence=conf, proposed_rewrite=GOOD)
    if original == Decision.REJECT:
        from core.schemas import Finding, Layer, Severity
        verdict.findings = [Finding(rule_id="TA-COMPARE-01", severity=Severity.BLOCK, layer=Layer.SEMANTIC, quote="x", verified_quote=True)]
    rws = {"verified": RewriteCheck.judge(output_guard.check(GOOD, PRODUCT, {"ecommerce"}), ReviewVerdict(variant_id="r", decision=Decision.APPROVE, confidence=0.95), TAU),
           "failed": RewriteCheck.judge(output_guard.check(BLOCKED, PRODUCT, {"ecommerce"}), None, TAU),
           "unchecked": RewriteCheck.judge(output_guard.check(GOOD, PRODUCT, {"ecommerce"}), ReviewVerdict(variant_id="r", decision=Decision.APPROVE, confidence=0.1), TAU)}
    baseline = adjudicate(copy, det, verdict, TAU, None)
    report = adjudicate(copy, det, verdict, TAU, rws[rewrite_outcome])
    assert report.final_decision == baseline.final_decision == original  # 수정안 상태가 원문 판정을 바꾸지 않는다
    expected_status = {"verified": RewriteStatus.VERIFIED, "failed": RewriteStatus.FAILED_RECHECK, "unchecked": RewriteStatus.UNCHECKED}[rewrite_outcome]
    assert report.rewrite_status == expected_status
    assert (report.verified_rewrite is not None) == (rewrite_outcome == "verified")


def test_fabricated_quote_blocks_even_when_the_threshold_would_allow_the_confidence():
    """인용 검증은 신뢰도와 별개의 조건이다: 신뢰도 상한(0.6)이 임계값(0.5)을 넘는 설정에서도, 문구에 없는 인용이 있으면 통과로 확정하지 않는다."""
    response = review("APPROVE", 0.95, [("TA-OMISSION-01", "문구에 전혀 없는 인용")])
    auto, direct, _, _ = both(response, tau=0.5)
    assert auto.semantic.confidence >= 0.5               # 신뢰도만 보면 통과할 수 있는 값이다
    assert auto.inputs.unverified_quotes == 1
    assert auto.decision == direct.final_decision == Decision.HUMAN_REVIEW and "인용" in auto.reason
