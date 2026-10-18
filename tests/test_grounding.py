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


# ───────────── 2차 내부 재점검(검토자 공격)에서 확인된 결함 ─────────────
def _gp(facts, head, body="지금 확인", cta="확인", disc=()):
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id=f"F{i+1}", text=t) for i, t in enumerate(facts)])
    return grounding.check(AdCopy(headline=head, body=body, cta=cta, disclosures=list(disc)), P)


def test_a_unit_on_the_next_line_is_not_attached_to_a_number():
    res = _gp(["최대 한도 5천만원"], "최대 한도 3", "원 상당 혜택")
    for f in res.findings:
        assert "\n" not in f.quote, f.quote
    copy = AdCopy(headline="2026.11.30", body="지금 확인", cta="확인")
    res = _gp(["2026.10.31까지"], "2026.11.30", "지금 확인")
    f = next(f for f in res.findings if f.rule_id == "GRND-NUM-01")
    assert copy.full_text()[f.start:f.end] == f.quote == "2026.11.30"                 # 끝의 줄바꿈·공백이 인용에 들어가지 않는다


def test_a_negation_in_the_next_field_does_not_negate_the_previous_fields_marker():
    res = _gp(["7일 무료 체험 후 월 19,900원 자동결제"], "7일 무료 체험 후 월 19,900원 자동결제", "부담 없이 시작하세요")
    assert not res.findings
    res = _gp(["전 상품 10% 할인, 일부 상품 제외"], "전 상품 10% 할인", "일부 상품 제외", "부담 없이 쇼핑")
    assert not res.findings


def test_a_fact_that_says_both_polarities_can_be_copied_verbatim():
    for fact in ("7일간 자동결제 없이 무료 체험, 이후 월 19,900원 자동결제", "체험 기간 중 과금 없음, 체험 종료 후 월 9,900원 정기결제", "서류 심사 없이 간편 신청, 최종 한도는 심사 결과에 따라 결정"):
        assert not _gp([fact], "안내", fact).findings, fact


@pytest.mark.parametrize("phrase", ["자동결제 전혀 없음", "자동결제 절대 없어요", "자동결제 하나도 없어요", "자동결제는 전혀 되지 않습니다", "자동결제 걱정 전혀 없음"])
def test_intensified_negation_is_still_a_negation(phrase):
    assert "GRND-COND-02" in {f.rule_id for f in _gp(["7일 무료 체험 후 월 19,900원 자동결제"], "7일 무료 체험", f"월 19,900원, {phrase}").findings}


def test_coupon_validity_dates_are_compared_but_reference_dates_are_not():
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["10% 할인 쿠폰 사용 가능, 유효기간: 2026.10.31"], "10% 할인 쿠폰 사용 가능", "유효기간: 2026.12.31").findings}
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["10% 할인 쿠폰 사용 가능, 유효기간 2026.10.01~2026.12.31"], "10% 할인 쿠폰", "유효기간 2026.10.01~2027.12.31").findings}
    for ad in ("(9월 30일 기준)", "(9.30 기준)", "(9월 30일 현재)", "(2026. 9. 30. 기준)"):
        assert not _gp(["최근 1년 성과 5.2% (2026.09.30 기준)"], "최근 1년 성과 5.2%", ad).findings, ad


@pytest.mark.parametrize("fact,ad", [
    ("이벤트 기간: 10.1~10.31", "이벤트 기간: 10월 1일 ~ 10월 31일"), ("이벤트 기간: 10월 1일 ~ 10월 31일", "이벤트 기간: 10.1~10.31"),
    ("이벤트 기간: 10/1~10/31", "이벤트 기간: 10월 1일 ~ 10월 31일"), ("이벤트 기간: 10월 1일 ~ 10월 31일", "이벤트 기간: 10/1 ~ 10/31"),
    ("이벤트 기간: 2026.10.01 ~ 2026.10.31", "이벤트 기간: 10.1 ~ 10.31"), ("이벤트 기간: 10.1~10.31", "이벤트 기간: 10.1 ~ 10.31"),
])
def test_date_ranges_in_different_notations_match(fact, ad):
    assert not _gp([fact], ad).findings, (fact, ad)


def test_a_different_date_range_in_another_notation_is_still_caught():
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["이벤트 기간: 10.1~10.31"], "이벤트 기간: 10월 1일 ~ 11월 30일").findings}
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["이벤트 기간: 10월 1일 ~ 10월 31일"], "이벤트 기간: 10.1 ~ 11.30").findings}


def test_rate_ranges_without_a_percent_sign_are_not_read_as_dates():
    assert not _gp(["연 4.5%~12.5% 적립"], "연 4.5~12.5% 적립").findings
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["연 4.5%~12.5% 적립"], "적립 4.5 ~ 11.5").findings}


def test_ad_side_within_days_does_not_conflict_with_other_deadlines_and_fact_side_stays_non_binding():
    assert not _gp(["가입 후 30일까지 신청 가능", "신청 후 7일 이내 지급"], "가입 후 30일까지 신청 가능", "신청 후 7일 이내 지급").findings
    assert not _gp(["구매 후 30일까지 쿠폰 사용 가능", "배송 3일 이내"], "구매 후 30일까지 쿠폰 사용 가능", "배송 3일 이내").findings
    assert not _gp(["전 상품 10% 할인, 배송 3일 이내 도착"], "전 상품 10% 할인").findings           # 사실의 '3일 이내' 는 광고가 반드시 옮겨 적을 조건이 아니다


def test_digitless_man_won_is_one_man_won():
    q = grounding.extract_quantities("만원 이상 구매, 천원 할인")
    assert [(x.value, x.cls) for x in q] == [(10000.0, "krw"), (1000.0, "krw")]
    assert "GRND-COND-02" in {f.rule_id for f in _gp(["50,000원 이상 구매 시 무료배송"], "만원 이상 구매 시 무료배송").findings}
    assert not _gp(["만원 이상 구매 시 무료배송"], "10,000원 이상 구매 시 무료배송").findings
    assert grounding.extract_quantities("수만원") == [] and grounding.extract_quantities("몇만원") == []       # 앞에 글자가 붙은 말은 수량이 아니다


def test_sign_matters_for_performance_numbers_but_not_for_discount_labels():
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["최근 1년 성과 -5.2%"], "최근 1년 성과 5.2%").findings}
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["최근 1년 성과 5.2%"], "최근 1년 성과 -5.2%").findings}
    assert not _gp(["전 상품 30% 할인"], "정가 대비 -30% 할인").findings                                    # '-30%' 할인 표기는 부호를 보지 않는다
    assert not _gp(["최대 50-60% 할인"], "최대 50-60% 할인").findings                                       # 범위 하이픈은 부호가 아니다


def test_range_with_a_multiplier_on_the_first_endpoint_matches_the_unit_form():
    assert not _gp(["최대 적립 100만~500만원"], "적립 100만원~500만원").findings
    assert not _gp(["적립 5천~1만원"], "적립 5천원~1만원").findings


def test_clock_times_are_not_durations_and_deadline_wordings_with_times_match():
    for ad in ("10월 31일 23시 59분까지", "10월 31일 오후 11시 59분까지", "10월 31일 23:59까지", "10월 31일 밤 12시까지", "10월 31일(금) 오후 6시 30분까지", "10월 31일 이벤트 종료"):
        assert not _gp(["10월 31일까지 사용 가능"], ad).findings, ad


def test_a_period_range_satisfies_a_from_to_deadline():
    assert not _gp(["10월 1일부터 10월 31일까지 진행"], "10월 1일 ~ 10월 31일 진행").findings
    assert "GRND-COND-02" in {f.rule_id for f in _gp(["10월 1일부터 10월 31일까지 진행"], "10월 1일 ~ 11월 30일 진행").findings}


@pytest.mark.parametrize("fact,ad", [("14일 무료 체험", "14주 무료 체험"), ("만 19세 이상", "만 14세 이상"), ("4주 완성", "8주 완성")])
def test_week_and_age_units_are_compared(fact, ad):
    assert "GRND-NUM-01" in {f.rule_id for f in _gp([fact], ad).findings}


def test_words_that_start_with_week_or_age_characters_and_5g_are_not_units():
    assert grounding.extract_quantities("3 주문, 5 세트, 20세기 영화, 5G 요금제, 9 주소") == []
    assert [q.raw for q in grounding.extract_quantities("4주 완성")] == ["4주"]


def test_man_with_comma_tail_is_one_amount():
    assert [q.value for q in grounding.extract_quantities("월 1만 5,000원")] == [15000.0]
    assert [q.value for q in grounding.extract_quantities("월 1만5,000원")] == [15000.0]
    assert not _gp(["월 15,000원"], "월 1만 5,000원").findings


def test_absurdly_long_digit_runs_are_ignored_instead_of_comparing_equal():
    huge = "9" * 400
    assert grounding.extract_quantities(huge) == []
    assert not any(q for q in grounding.extract_quantities(f"{huge}원 이상"))


def test_digit_runs_longer_than_fifteen_are_not_quantities_but_fifteen_are():
    """16~300자리 숫자는 유한한 값으로 읽히지만 수치가 아니다 — 15자리까지만 비교 대상이다."""
    assert grounding.extract_quantities("수익 1234567890123456원") == []
    assert grounding.extract_quantities("12345678901234567890원 혜택") == []
    assert [q.value for q in grounding.extract_quantities("수익 123456789012345원")] == [123456789012345.0]


def test_the_meaning_of_a_negative_number_is_read_from_before_its_minus_sign():
    """마이너스 하이픈은 구절 구분자라서, 뜻(수익·성과)을 부호 뒤에서부터 찾으면 사라진다 — 부호 앞에서부터 찾아야 부호 비교가 동작한다."""
    for text in ("연 수익 -5.2%", "수익률 −5.2%"):
        (q,) = grounding.extract_quantities(text)
        assert q.neg and "수익" in q.sense, text
    assert "GRND-NUM-01" in {f.rule_id for f in _gp(["연 수익 5.2%"], "연 수익 -5.2%").findings}


def test_over_long_copy_never_passes_even_though_the_tail_is_not_scanned():
    """20,000자를 넘는 뒷부분은 숫자 비교를 하지 않지만, 길이 규칙(TA-LENGTH-01)이 이미 수정 필요로 막는다 — 길게 써서 통과하는 길은 없다."""
    from guardrails import output_guard
    from guardrails.policy import get_catalog

    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="연 4.5%~15.0%")])
    det = output_guard.check(AdCopy(headline="h", body="가" * grounding.MAX_ANALYZED_CHARS + " 연 99% 금리", cta="확인"), P, {"ecommerce"}, "", [], get_catalog())
    assert det.decision.value != "APPROVE" and "TA-LENGTH-01" in {f.rule_id for f in det.findings}


@pytest.mark.parametrize("name", ["10/31 ", "1.5 ", "2026.10.31 ", "1인 ", "유효기간 ", "심사 ", "제외 ", "자동 "])
def test_runs_of_blanks_after_a_marker_are_bounded_in_time(name):
    text = name.rstrip() + " " * 20_000
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (10월 31일까지, 1인 1회, 일부 상품 제외)")])
    t0 = time.perf_counter()
    grounding.check(AdCopy(headline=text, body="x", cta="확인", disclosures=[text[:5000]]), P)
    assert time.perf_counter() - t0 < 3.0


@pytest.mark.parametrize("unit", ["1원 ", "1~2% ", "만원 ", "1만 ", "10.1~10.31 ", "오후 11시 59분 "])
def test_many_numbers_in_a_long_text_are_bounded_in_time(unit):
    text = unit * (20_000 // len(unit))
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인")])
    t0 = time.perf_counter()
    grounding.check(AdCopy(headline=text, body="x", cta="확인"), P)
    assert time.perf_counter() - t0 < 3.0
