from agents.adjudicator import adjudicate
from core.schemas import AdCopy, Decision, Fact, Finding, Layer, ProductCategory, ProductFacts, ReviewVerdict, Severity
from guardrails import output_guard

PRODUCT = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인")])
CLEAN = AdCopy(headline="전 상품 10% 할인", body="좋은 상품", cta="보기")


def det(copy=CLEAN):
    return output_guard.check(copy, PRODUCT, {"ecommerce"})


def verdict(decision=Decision.APPROVE, conf=0.9, findings=(), second=None, rewrite=None):
    v = ReviewVerdict(variant_id=CLEAN.variant_id, decision=decision, confidence=conf, findings=list(findings), compliant_rewrite=rewrite)
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


def test_rewrite_recheck_failure_drops_rewrite_but_keeps_revise():
    bad_rewrite = AdCopy(headline="타사보다 2배 적립", body="x", cta="y")
    copy = AdCopy(headline="지금 아니면 못 받는 할인", body="전 상품 10% 할인", cta="보기")
    d = det(copy)
    assert d.decision == Decision.REVISE
    v = verdict(Decision.REVISE, 0.9, rewrite=bad_rewrite)
    r = adjudicate(copy, d, v, 0.8, rewrite_recheck=det(bad_rewrite))
    assert r.final_decision == Decision.REVISE and r.verdict.compliant_rewrite is None and "FM-19" in r.decision_reason


def test_clean_copy_approved():
    r = adjudicate(CLEAN, det(), verdict(conf=0.9), 0.8)
    assert r.final_decision == Decision.APPROVE and r.checklist[-1].passed


def test_unsupported_revise_routes_to_human():
    """모델이 REVISE 라 했지만 남은 근거 finding 이 없으면(코드가 반증) 자동 REVISE 하지 않고 사람 검토."""
    v = verdict(Decision.REVISE, 0.85)
    v.unsupported_decision = True
    r = adjudicate(CLEAN, det(), v, 0.8)
    assert r.final_decision == Decision.HUMAN_REVIEW and "근거" in r.decision_reason
