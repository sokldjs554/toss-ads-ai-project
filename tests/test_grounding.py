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


def _g(h, facts, body="지금", claims=None):
    P = ProductFacts(name="x", category=ProductCategory.ECOMMERCE, facts=[Fact(id=f"F{i+1}", text=t) for i, t in enumerate(facts)])
    return grounding.check(AdCopy(headline=h, body=body, cta="확인", claims=claims or []), P)


def test_sense_synonyms_do_not_false_flag():
    assert not _g("전 상품 10% 쿠폰", ["전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)"], body="10월 31일까지, 1인 1회").findings
    assert not _g("이자 연 4.5%부터", ["대출 금리 연 4.5%~15.0%"]).numbers_unsupported
    assert _g("5% 할인", ["금리 연 5%"]).numbers_unsupported == ["5%"]


def test_bare_numbers_in_facts_do_not_support_unit_numbers():
    assert "1,234명" in _g("상담원 1,234명 대기", ["고객센터 1588-1234 (24시간)"]).numbers_unsupported
    assert "200%" in _g("최근 200% 상승", ["코스피200 추종 인덱스 펀드"]).numbers_unsupported


def test_dates_match_across_formats_and_are_not_durations():
    assert not _g("10월 31일까지 10% 할인", ["전 상품 10% 할인 (2026.10.31까지, 1인 1회)"], body="1인 1회").findings
    assert not _g("2026년 10월 1일 기준 연 3.0%", ["기본 연 3.0% (2026.10.1 기준, 변동 가능)"]).numbers_unsupported


def test_negated_marker_and_phrasing_variants():
    assert not _g("전 상품 10% 할인", ["전 상품 10% 할인 (제외 품목 없음)"]).conditions_omitted
    assert not _g("전 상품 10% 할인 쿠폰", ["전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)"], body="10월 31일까지, 1인당 1회").conditions_omitted


def test_quote_excludes_trailing_whitespace():
    assert [(q.raw, q.start, q.end) for q in grounding.extract_quantities("1억\n2억 3억")] == [("1억", 0, 2), ("2억", 3, 5), ("3억", 6, 8)]


# ───────────── 내부 재점검(self_review)에서 확인된 결함 ─────────────
import time

import pytest

from agents.recheck import build_edited_copy


def test_every_finding_position_matches_the_quoted_text_even_with_empty_fields():
    """finding 의 quote 는 항상 copy.full_text()[start:end] 와 같고, 한 필드 안에 있다 — 빈 필드가 있어도, 줄을 넘는 인용도 없다."""
    from evals.runner import load_dataset

    for case in load_dataset("conditions") + load_dataset("conditions_extra"):
        copy = case.to_copy()
        text = copy.full_text()
        spans = copy.field_spans()
        for f in grounding.check(copy, case.to_product(), case.landing_text).findings:
            if f.start < 0:
                continue
            assert text[f.start:f.end] == f.quote, (case.id, f.rule_id, f.quote, text[f.start:f.end])
            assert any(a <= f.start and f.end <= b for _, a, b in spans), (case.id, f.rule_id, f.quote)
    # 빈 본문·빈 CTA 가 있는 광고의 고지 문구 속 금액도 위치가 맞는다
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인, 50,000원 이상 구매 시")])
    copy = AdCopy(headline="전 상품 10% 할인", body="", cta="자세히 보기", disclosures=["5,000원 이상 구매 시"])
    res = grounding.check(copy, P)
    f = next(f for f in res.findings if f.rule_id == "GRND-COND-02")
    assert copy.full_text()[f.start:f.end] == f.quote == "5,000원 이상"
    assert [n for n, *_ in copy.field_spans()] == ["headline", "cta", "disc0"]


def test_field_spans_follow_full_text_exactly():
    for parts in [("가", "나", "다", ["라", "마"]), ("가", "", "다", ["라"]), ("", "나", "", []), ("가", "나", "다", ["", "마"])]:
        copy = AdCopy(headline=parts[0], body=parts[1], cta=parts[2], disclosures=parts[3])
        text = copy.full_text()
        values = {"headline": parts[0], "body": parts[1], "cta": parts[2], **{f"disc{i}": d for i, d in enumerate(parts[3])}}
        for name, a, b in copy.field_spans():
            assert text[a:b] == values[name], (parts, name)


def test_exclude_condition_quote_has_no_leading_space_and_stays_on_one_line():
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (일부 상품 제외)")])
    for body in ("전 상품, 제외 상품 없음", "전 상품 적용. 제외 없이 모두"):
        res = grounding.check(AdCopy(headline="전 상품 10% 할인", body=body, cta="확인"), P)
        f = next(f for f in res.findings if f.rule_id == "GRND-COND-02")
        assert f.quote == f.quote.strip() and "\n" not in f.quote, f.quote
    res = grounding.check(AdCopy(headline="전 상품 10% 할인", body="제외 상품 없음", cta="확인"), P)
    f = next(f for f in res.findings if f.rule_id == "GRND-COND-02")
    assert "\n" not in f.quote and f.quote.startswith("제외")      # 앞 줄(헤드라인)로 번지지 않는다


LONG_INPUTS = {"hangul": "가" * 50_000, "digits": "1" * 50_000, "exclude": "제외 " * 12_000, "at-signs": "a@" * 20_000, "commas": "1,2" * 15_000}


@pytest.mark.parametrize("name", list(LONG_INPUTS))
def test_long_unbroken_input_is_bounded_in_time(name):
    text = LONG_INPUTS[name]
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (일부 상품 제외)")])
    t0 = time.perf_counter()
    grounding.check(AdCopy(headline=text, body=text, cta="확인", disclosures=[text]), P, landing_text=text)
    assert time.perf_counter() - t0 < 3.0


def test_rewording_that_drops_every_claim_link_still_checks_the_original_facts_conditions():
    """수정 중에 주장 연결이 모두 끊겨도, 원래 광고가 근거로 삼은 사실과 낱말이 겹치는 문구는 그 사실의 조건(자동결제)을 계속 지켜야 한다."""
    P = ProductFacts(name="p", category=ProductCategory.APP, facts=[Fact(id="F1", text="7일 무료 체험 후 월 19,900원 자동결제")])
    original = AdCopy(headline="7일 무료 체험", body="체험 후 월 19,900원 자동결제", cta="시작하기", claims=[Claim(text="7일 무료 체험", fact_id="F1")])
    assert not grounding.check(original, P).findings
    for head in ("무료로 체험해 보세요", "무료 체험으로 시작"):
        edited = build_edited_copy(original, head, "지금 가입", "시작하기", [])
        assert edited.claims == [] and edited.carried_fact_ids == ["F1"]
        assert "GRND-COND-01" in {f.rule_id for f in grounding.check(edited, P).findings}, head
        # 같은 문구라도 연결 정보가 없는(처음부터 직접 쓴) 광고는 이 사실을 가리킨 적이 없으므로 숫자도 낱말도 겹치지 않으면 비교하지 않는다
    unrelated = build_edited_copy(original, "지금 바로 시작하세요", "부담 없이 써 보세요", "시작하기", [])
    assert not grounding.check(unrelated, P).findings                       # 그 사실을 더는 말하지 않는 문구 — 지킬 조건도 없다
    kept = build_edited_copy(original, "무료로 체험해 보세요", "체험 후 월 19,900원 자동결제", "시작하기", [])
    assert not grounding.check(kept, P).findings                           # 조건을 다시 적으면 통과


def test_carried_ids_survive_repeated_edits():
    original = AdCopy(headline="a", body="b", cta="c", claims=[Claim(text="a", fact_id="F1")])
    once = build_edited_copy(original, "x", "y", "z", [])
    twice = build_edited_copy(once, "x2", "y2", "z2", [])
    assert once.carried_fact_ids == twice.carried_fact_ids == ["F1"]


@pytest.mark.parametrize("text,expect", [
    ("모델 AB-123 출시", []),                    # 영문자 뒤 하이픈 번호는 상품 코드
    ("문의 02-123-4567", []),
    ("1588-1234", []),
    ("2번째 1 분기 7 일정", []),                # 띄어 쓴 낱말(분기·일정)을 단위로 읽지 않는다
])
def test_names_and_phone_numbers_are_still_not_benefit_numbers(text, expect):
    assert [q.raw for q in grounding.extract_quantities(text) if q.cls != "bare"] == expect


@pytest.mark.parametrize("text,vals", [
    ("1만5천원", [15000.0]), ("1만 5천원", [15000.0]), ("1억5천만원", [150000000.0]), ("1천5백만원", [15000000.0]), ("1만5000원", [15000.0]),
    ("5천만원", [50000000.0]), ("2만9천원", [29000.0]), ("1,500만원", [15000000.0]), ("1.5만원", [15000.0]),
])
def test_compound_korean_amounts_are_one_value(text, vals):
    assert [q.value for q in grounding.extract_quantities(text)] == vals


def test_numbers_that_do_not_shrink_are_not_chained():
    assert [q.value for q in grounding.extract_quantities("3만 5만원")] == [30000.0, 50000.0]
    assert [q.value for q in grounding.extract_quantities("1만 3개월")] == [10000.0, 3.0]


def test_dates_keep_the_year_only_when_both_sides_state_one():
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (2026.10.31까지, 1인 1회)")])
    ok = lambda body: not [f for f in grounding.check(AdCopy(headline="전 상품 10% 할인", body=body, cta="확인"), P).findings]
    assert ok("2026.10.31까지, 1인 1회") and ok("10월 31일까지, 1인 1회") and ok("2026년 10월 31일까지, 1인 1회")
    assert not ok("2027.10.31까지, 1인 1회")


def test_cycle_words_are_read_through_short_modifiers_and_compounds():
    cyc = lambda t: [q.cycle for q in grounding.extract_quantities(t) if q.cls in ("krw", "percent")]
    assert cyc("월 최대 5만원") == ["월"] and cyc("연 평균 3만원") == ["연"] and cyc("연회비 9,900원") == ["연"] and cyc("월 이용료 9,900원") == ["월"]
    assert cyc("9,900원/월") == ["월"] and cyc("연 4.5%") == ["연"] and cyc("5만원") == [""]
    assert cyc("월 3만원 이하 이용 시 5만원 환급") == ["월", ""]      # 사이에 다른 숫자가 있으면 주기를 끌어오지 않는다
    assert cyc("2월 5,000원") == [""]


def test_unit_like_words_after_a_number_are_not_units():
    q = lambda t: [x.raw for x in grounding.extract_quantities(t) if x.cls != "bare"]
    assert q("1분기 실적") == [] and q("7일정 안내") == [] and q("100원금 보장") == [] and q("누적 3회원") == []   # 붙여 쓴 낱말
    assert q("3 일꾼 모집") == [] and q("9 회색 옵션") == [] and q("2 명품관") == []                                  # 띄어 쓴 뒤 한글이 이어지면 단위가 아니다
    assert q("5% 할인") == ["5%"] and q("3일간") == ["3일"] and q("3 개월 무이자") == ["3 개월"] and q("5,000 원이상") == ["5,000 원"] and q("90 퍼 할인") == ["90 퍼"]


def test_text_beyond_the_analysis_cap_is_not_scanned():
    assert grounding.extract_quantities("가" * grounding.MAX_ANALYZED_CHARS + " 99%") == []
    assert [q.raw for q in grounding.extract_quantities("99% " + "가" * grounding.MAX_ANALYZED_CHARS)] == ["99%"]
