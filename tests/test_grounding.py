from core.schemas import AdCopy, Claim, Fact, ProductCategory, ProductFacts
from guardrails import grounding

PRODUCT = ProductFacts(name="p", category=ProductCategory.LOAN, facts=[Fact(id="F1", text="연 4.5%~15.0%"), Fact(id="F2", text="최대 한도 5,000만원")])


def test_unsupported_number_flagged():
    copy = AdCopy(headline="연 2.9% 금리", body="최대 한도 5,000만원", cta="신청")
    r = grounding.check(copy, PRODUCT)
    assert "2.9%" in " ".join(r.numbers_unsupported)
    assert "5,000만원" not in " ".join(r.numbers_unsupported)


def test_claim_without_fact_id_flagged_and_fake_id_flagged():
    copy = AdCopy(headline="h", body="b", cta="c", claims=[Claim(text="업계 최저", fact_id=None), Claim(text="x", fact_id="F9")])
    r = grounding.check(copy, PRODUCT)
    assert len(r.claims_unsupported) == 2
    assert any("지어낸" in f.explanation for f in r.findings)


def test_landing_mismatch_number():
    copy = AdCopy(headline="최대 한도 5,000만원", body="연 4.5%~15.0%", cta="신청")
    r = grounding.check(copy, PRODUCT, landing_text="신용대출 연 4.5%~15.0% 상담 환영")
    assert "5,000만원" in " ".join(r.landing_mismatch)
    assert any(f.rule_id == "TA-LANDING-01" for f in r.findings)


def test_grounded_copy_clean():
    copy = AdCopy(headline="최대 한도 5,000만원", body="연 4.5%~15.0%", cta="신청", claims=[Claim(text="최대 한도 5,000만원", fact_id="F2")])
    r = grounding.check(copy, PRODUCT, landing_text="최대 한도 5,000만원 연 4.5%~15.0%")
    assert not r.findings
