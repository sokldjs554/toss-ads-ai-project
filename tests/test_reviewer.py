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


def test_det_authoritative_layers_overrule_model():
    """결정론 체크리스트가 '고지 충족' 인 소재에 모델이 FIN-DISCLOSE-* 를 내면 버린다 (라이브 오탐 사례 G-060)."""
    from core.schemas import Fact, ProductCategory, ProductFacts
    copy = AdCopy(headline="OO은행 직장인 신용대출", body="연 4.5%~15.0% (심사 결과에 따라 다름)", cta="자세히 보기", disclosures=[
        "연 4.5%~15.0% (심사 결과에 따라 다름)", "연체 시 연체이자율 적용", "상환 방식·기간은 상품 설명서 참조",
        "계약 체결 전 상품 설명서와 약관을 반드시 읽어 보시기 바랍니다.", "상환 능력에 비해 대출금이 과도할 경우 개인신용평점이 하락할 수 있습니다.",
        "준법감시인 심의필 제2026-0412호"])
    product = ProductFacts(name="p", category=ProductCategory.LOAN, facts=[Fact(id="F1", text="연 4.5%~15.0% (심사 결과에 따라 다름)")])
    det = output_guard.check(copy, product, {"loan"})
    assert det.decision == Decision.APPROVE
    out = _to_verdict(copy, parsed(decision="REVISE", confidence=0.85, findings=[("FIN-DISCLOSE-LOAN-01", "연 4.5%~15.0%"), ("GRND-CLAIM-01", "연 4.5%~15.0%")]), det, CAT, "p", "m")
    assert out.overruled_by_det == 2
    assert not [f for f in out.verdict.findings if f.rule_id in ("FIN-DISCLOSE-LOAN-01", "GRND-CLAIM-01")]
    # 모델이 REVISE 라 했지만 남은 finding 이 없으므로 결정론 판정(APPROVE)보다 엄격한 모델 판정이 유지된다 — 임계값·교차 검증이 다음 방어선
    assert out.verdict.decision == Decision.REVISE
