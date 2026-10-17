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


SHIP = ProductFacts(name="s", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="배송까지 15일 소요됩니다.")])


def test_review_repro_number_substring_not_supported():
    """리뷰 재현: '배송까지 15일' 사실을 근거로 '5% 할인' 을 쓰면 숫자 5 가 15 에 들어 있어도 근거가 아니다."""
    copy = AdCopy(headline="5% 할인", body="지금 구매", cta="보기", claims=[Claim(text="5% 할인", fact_id="F1")])
    r = grounding.check(copy, SHIP)
    assert "5%" in r.numbers_unsupported
    assert any(f.rule_id == "GRND-NUM-01" and f.quote == "5%" for f in r.findings)
    assert any(f.rule_id == "GRND-CLAIM-01" and "뒷받침하지 않음" in f.explanation for f in r.findings)


def test_same_number_different_unit_not_supported():
    """같은 숫자에 다른 단위: '15일' 사실로 '15% 할인' 은 근거가 아니다 (단위·뜻 불일치)."""
    copy = AdCopy(headline="15% 할인", body="지금 구매", cta="보기", claims=[Claim(text="15% 할인", fact_id="F1")])
    r = grounding.check(copy, SHIP)
    assert "15%" in r.numbers_unsupported
    assert any("단위·뜻이 다른" in f.explanation for f in r.findings if f.rule_id == "GRND-NUM-01")


def test_same_number_same_unit_is_supported():
    copy = AdCopy(headline="배송 15일", body="지금 구매", cta="보기", claims=[Claim(text="배송 15일", fact_id="F1")])
    r = grounding.check(copy, SHIP)
    assert not r.numbers_unsupported and not r.claims_unsupported


def test_won_amount_variants_match():
    """5,000만원 · 5천만원 · 50,000,000원 은 같은 금액."""
    prod = ProductFacts(name="l", category=ProductCategory.LOAN, facts=[Fact(id="F2", text="최대 한도 5천만원 (심사 결과에 따라 다름)")])
    for text in ("최대 5,000만원", "최대 50,000,000원", "최대 5천만원"):
        copy = AdCopy(headline=text, body="심사 결과에 따라 다름", cta="신청")
        r = grounding.check(copy, prod)
        assert not r.numbers_unsupported, text


def test_same_number_different_sense_not_supported():
    """같은 숫자·같은 단위라도 '할인 5%' 와 '금리 5%' 는 다른 뜻."""
    prod = ProductFacts(name="l", category=ProductCategory.LOAN, facts=[Fact(id="F1", text="금리 연 5%")])
    r = grounding.check(AdCopy(headline="5% 할인", body="b", cta="c"), prod)
    assert "5%" in r.numbers_unsupported


def test_omitted_condition_flagged_and_present_condition_passes():
    """근거 사실에 '체험 후 월 19,900원 자동결제' 조건이 있는데 광고에 없으면 GRND-COND-01. 조건을 쓰면 통과."""
    prod = ProductFacts(name="a", category=ProductCategory.APP, facts=[Fact(id="F1", text="7일 무료 체험 후 월 19,900원 자동결제 (체험 중 해지 가능)")])
    omitted = AdCopy(headline="영어 첫 문장", body="7일 무료 체험 (체험 중 해지 가능)", cta="체험 시작", claims=[Claim(text="7일 무료 체험", fact_id="F1")])
    r = grounding.check(omitted, prod)
    assert any(f.rule_id == "GRND-COND-01" and "자동결제" in f.explanation for f in r.findings)
    shown = AdCopy(headline="영어 첫 문장", body="7일 무료 체험 후 월 19,900원 자동결제 (체험 중 해지 가능)", cta="체험 시작", claims=[Claim(text="7일 무료 체험", fact_id="F1")])
    r2 = grounding.check(shown, prod)
    assert not any(f.rule_id == "GRND-COND-01" for f in r2.findings) and not r2.numbers_unsupported
    # 조건이 고지 문구에 있어도 충족
    disc = AdCopy(headline="영어 첫 문장", body="7일 무료 체험", cta="체험 시작", claims=[Claim(text="7일 무료 체험", fact_id="F1")],
                  disclosures=["체험 종료 후 월 19,900원 자동결제, 체험 중 해지 가능"])
    assert not any(f.rule_id == "GRND-COND-01" for f in grounding.check(disc, prod).findings)


def test_condition_check_applies_to_rewrites_without_claims():
    """수정안·직접 편집 문구처럼 claims 가 없어도 숫자로 끌어다 쓴 사실의 조건은 검사한다."""
    prod = ProductFacts(name="a", category=ProductCategory.APP, facts=[Fact(id="F1", text="7일 무료 체험 후 월 19,900원 자동결제")])
    r = grounding.check(AdCopy(headline="7일 무료 체험", body="지금 시작", cta="시작"), prod)
    assert r.referenced_fact_ids == ["F1"] and any(f.rule_id == "GRND-COND-01" for f in r.findings)


def test_claim_text_without_numbers_needs_vocabulary_overlap():
    prod = ProductFacts(name="l", category=ProductCategory.LOAN, facts=[Fact(id="F3", text="중도상환수수료 없음"), Fact(id="F4", text="모바일 신청, 심사 완료 시 당일 입금 가능")])
    ok = AdCopy(headline="h", body="b", cta="c", claims=[Claim(text="중도상환수수료 없음", fact_id="F3"), Claim(text="당일 입금", fact_id="F4")])
    assert not grounding.check(ok, prod).claims_unsupported
    bad = AdCopy(headline="h", body="b", cta="c", claims=[Claim(text="업계 최저 금리", fact_id="F3")])
    assert grounding.check(bad, prod).claims_unsupported == ["업계 최저 금리"]


def test_relink_claims_keeps_only_surviving_claims():
    from agents.reviewer import relink_claims

    claims = [Claim(text="7일 무료 체험", fact_id="F1"), Claim(text="최대 5,000만원", fact_id="F2")]
    kept = relink_claims(claims, "7일 무료 체험 후 자동결제\n지금 시작")
    assert [c.text for c in kept] == ["7일 무료 체험"]
