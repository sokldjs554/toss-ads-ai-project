from types import SimpleNamespace

from agents.reviewer import _to_verdict
from core.schemas import AdCopy, Decision, Fact, ProductCategory, ProductFacts
from guardrails import output_guard
from guardrails.policy import get_catalog

CAT = get_catalog()


def parsed(decision="REVISE", confidence=0.95, findings=(), rewrite=None):
    return SimpleNamespace(decision=decision, confidence=confidence, rationale="r", rewrite=rewrite,
                           findings=[SimpleNamespace(rule_id=r, quote=q, explanation="e") for r, q in findings],
                           model_dump=lambda: {})


def det_for(copy: AdCopy):
    return output_guard.check(copy, ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인")]), {"ecommerce"})


def test_unknown_rule_id_dropped():
    copy = AdCopy(headline="전 상품 10% 할인", body="좋은 상품", cta="보기")
    out = _to_verdict(copy, parsed(findings=[("FIN-XX-99", "좋은 상품")]), det_for(copy), CAT, "p", "m")
    assert out.unknown_rule_ids == 1 and not [f for f in out.verdict.findings if f.rule_id == "FIN-XX-99"]


def test_hallucinated_quote_downgraded():
    copy = AdCopy(headline="전 상품 10% 할인", body="좋은 상품", cta="보기")
    out = _to_verdict(copy, parsed(decision="REJECT", confidence=0.99, findings=[("TA-COMPARE-01", "타사보다 저렴")]), det_for(copy), CAT, "p", "m")
    f = [f for f in out.verdict.findings if f.rule_id == "TA-COMPARE-01"][0]
    assert not f.verified_quote and f.severity.value == "warn"
    assert out.unverified_quotes == 1 and out.verdict.confidence <= 0.6


def test_model_cannot_relax_deterministic_decision():
    copy = AdCopy(headline="타사보다 2배 적립", body="전 상품 10% 할인", cta="보기")
    det = det_for(copy)
    assert det.decision == Decision.REJECT
    out = _to_verdict(copy, parsed(decision="APPROVE", confidence=0.99), det, CAT, "p", "m")
    assert out.verdict.decision == Decision.REJECT


def test_verified_quote_keeps_severity():
    copy = AdCopy(headline="전 상품 10% 할인", body="조건 없이 모두에게", cta="보기")
    out = _to_verdict(copy, parsed(decision="REVISE", findings=[("TA-OMISSION-01", "조건 없이 모두에게")]), det_for(copy), CAT, "p", "m")
    f = [f for f in out.verdict.findings if f.rule_id == "TA-OMISSION-01"][0]
    assert f.verified_quote and f.severity.value == "fix" and out.verdict.decision == Decision.REVISE
