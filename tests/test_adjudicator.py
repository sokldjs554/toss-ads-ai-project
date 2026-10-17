import pytest

from agents.adjudicator import DecisionInputs, adjudicate, decide, inputs_from
from core.schemas import AdCopy, Decision, Fact, Finding, Layer, ProductCategory, ProductFacts, ReviewVerdict, RewriteStatus, Severity
from guardrails import output_guard

PRODUCT = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인")])
CLEAN = AdCopy(headline="전 상품 10% 할인", body="좋은 상품", cta="보기")


def det(copy=CLEAN):
    return output_guard.check(copy, PRODUCT, {"ecommerce"})


def verdict(decision=Decision.APPROVE, conf=0.9, findings=(), second=None, rewrite=None):
    v = ReviewVerdict(variant_id=CLEAN.variant_id, decision=decision, confidence=conf, findings=list(findings), proposed_rewrite=rewrite)
    if second is not None:
        v.second_opinion = second
        v.disagreement = second != decision
    return v


def test_deterministic_block_overrides_model():
    copy = AdCopy(headline="타사보다 2배", body="전 상품 10% 할인", cta="보기")
    r = adjudicate(copy, det(copy), verdict(Decision.APPROVE, 0.99), 0.8)
    assert r.final_decision == Decision.REJECT


def test_reviewer_failure_routes_to_human():
    assert adjudicate(CLEAN, det(), None, 0.8).final_decision == Decision.HUMAN_REVIEW


def test_low_confidence_routes_to_human():
    assert adjudicate(CLEAN, det(), verdict(conf=0.6), 0.8).final_decision == Decision.HUMAN_REVIEW


def test_disagreement_routes_to_human():
    r = adjudicate(CLEAN, det(), verdict(Decision.APPROVE, 0.95, second=Decision.REVISE), 0.8)
    assert r.final_decision == Decision.HUMAN_REVIEW and "불일치" in r.decision_reason


def test_reject_without_verified_evidence_routes_to_human():
    f = Finding(rule_id="TA-COMPARE-01", severity=Severity.WARN, layer=Layer.SEMANTIC, quote="없는 말", verified_quote=False)
    assert adjudicate(CLEAN, det(), verdict(Decision.REJECT, 0.99, [f]), 0.8).final_decision == Decision.HUMAN_REVIEW


BAD_REWRITE = AdCopy(headline="타사보다 2배 적립", body="x", cta="y")          # 재검사에서 block 에 걸린다
GOOD_REWRITE = AdCopy(headline="전 상품 10% 할인", body="좋은 상품", cta="보기")   # 재검사 통과


def test_rewrite_recheck_failure_drops_rewrite_but_keeps_revise():
    copy = AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기")
    d = det(copy)
    assert d.decision == Decision.REVISE
    v = verdict(Decision.REVISE, 0.9, rewrite=BAD_REWRITE)
    r = adjudicate(copy, d, v, 0.8, rewrite_recheck=det(BAD_REWRITE))
    assert r.final_decision == Decision.REVISE
    assert r.rewrite_status == RewriteStatus.FAILED_RECHECK and r.verified_rewrite is None
    assert "FM-19" in r.rewrite_note and r.rewrite_recheck_decision == Decision.REJECT
    assert any(c.name == "자동 수정안 재검사" and not c.passed for c in r.checklist)


@pytest.mark.parametrize("make", [
    # (설명, 원문, 심사 결과, 기대 판정) — 수정안 재검사 실패는 어느 판정 경로에서도 '검증됨' 이 될 수 없다
    lambda: ("REVISE 경로", AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기"), verdict(Decision.REVISE, 0.9, rewrite=BAD_REWRITE), Decision.REVISE),
    lambda: ("REJECT 경로 (결정론 block)", AdCopy(headline="타사보다 2배", body="전 상품 10% 할인", cta="보기"), verdict(Decision.REJECT, 0.95, rewrite=BAD_REWRITE), Decision.REJECT),
    lambda: ("HUMAN_REVIEW 경로 (낮은 신뢰도)", AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기"), verdict(Decision.REVISE, 0.6, rewrite=BAD_REWRITE), Decision.HUMAN_REVIEW),
    lambda: ("HUMAN_REVIEW 경로 (환각 인용)", CLEAN, verdict(Decision.REVISE, 0.95, [Finding(rule_id="TA-OMISSION-01", severity=Severity.FIX, layer=Layer.SEMANTIC, quote="없는 말", verified_quote=False)], rewrite=BAD_REWRITE), Decision.HUMAN_REVIEW),
    lambda: ("HUMAN_REVIEW 경로 (2차 의견 불일치)", AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기"), verdict(Decision.REVISE, 0.95, second=Decision.APPROVE, rewrite=BAD_REWRITE), Decision.HUMAN_REVIEW),
])
def test_failed_rewrite_never_verified_on_any_path(make):
    label, copy, v, expected = make()
    r = adjudicate(copy, det(copy), v, 0.8, rewrite_recheck=det(BAD_REWRITE))
    assert r.final_decision == expected, label
    assert r.rewrite_status == RewriteStatus.FAILED_RECHECK and r.verified_rewrite is None, label
    assert r.verdict.proposed_rewrite is BAD_REWRITE  # 모델의 제안 자체는 기록으로 남지만 '검증된 수정안' 은 아니다


def test_verified_rewrite_is_separate_from_original_decision():
    """재검사를 통과한 수정안은 원문 판정이 REJECT 여도 '검증됨' 으로 붙는다 — 원문 판정과 수정안 상태는 별개."""
    copy = AdCopy(headline="타사보다 2배", body="전 상품 10% 할인", cta="보기")
    r = adjudicate(copy, det(copy), verdict(Decision.REJECT, 0.95, rewrite=GOOD_REWRITE), 0.8, rewrite_recheck=det(GOOD_REWRITE))
    assert r.final_decision == Decision.REJECT and r.rewrite_status == RewriteStatus.VERIFIED and r.verified_rewrite is GOOD_REWRITE


def test_unchecked_rewrite_is_not_verified():
    copy = AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기")
    r = adjudicate(copy, det(copy), verdict(Decision.REVISE, 0.9, rewrite=GOOD_REWRITE), 0.8, rewrite_recheck=None)
    assert r.rewrite_status == RewriteStatus.UNCHECKED and r.verified_rewrite is None


def test_decide_is_pure_and_matches_adjudicate():
    """같은 입력·같은 임계값이면 decide() 와 adjudicate() 가 같은 판정 — 평가 곡선이 이 함수를 재생한다."""
    copy = AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기")
    d = det(copy)
    v = verdict(Decision.REVISE, 0.83)
    inp = inputs_from(d, v, None)
    for tau in (0.5, 0.8, 0.85, 0.95):
        assert decide(inp, tau).decision == adjudicate(copy, d, v, tau).final_decision
    rt = DecisionInputs.from_dict(inp.to_dict())
    assert rt == inp


def test_deterministic_block_is_independent_of_confidence():
    """결정론 block 은 신뢰도와 무관하게 REJECT — 임계값을 올려도 사람 검토로 바뀌지 않는다 (평가 곡선이 이를 빠뜨리면 안 된다)."""
    copy = AdCopy(headline="타사보다 2배", body="전 상품 10% 할인", cta="보기")
    inp = inputs_from(det(copy), verdict(Decision.REJECT, 0.6), None)
    assert decide(inp, 0.95).decision == Decision.REJECT


def test_clean_copy_approved():
    r = adjudicate(CLEAN, det(), verdict(conf=0.9), 0.8)
    assert r.final_decision == Decision.APPROVE and r.checklist[-1].passed


def test_unsupported_revise_routes_to_human():
    """모델이 REVISE 라 했지만 남은 근거 finding 이 없으면(코드가 반증) 자동 REVISE 하지 않고 사람 검토."""
    v = verdict(Decision.REVISE, 0.85)
    v.unsupported_decision = True
    r = adjudicate(CLEAN, det(), v, 0.8)
    assert r.final_decision == Decision.HUMAN_REVIEW and "근거" in r.decision_reason
