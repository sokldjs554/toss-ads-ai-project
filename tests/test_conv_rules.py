"""대화형 캠페인 셋업의 결정론 추출기(agents/conversation_rules.py) 테스트 — 네트워크·모델 호출 없음.

여기서 확인하는 것은 규칙 기반 추출기의 동작이다. 실제 모델의 이해력과는 무관하다.
모든 후보는 사용자 말에 글자 그대로 있는 구절(quote)을 근거로 가져야 한다 — 그 불변식을 수십 개의 말로 확인한다.
"""
from __future__ import annotations

import time

import pytest

from agents import conversation_rules as R


def ex(text: str, asked: tuple = ()) -> R.Extraction:
    return R.extract(text, R.ExtractContext(last_asked=frozenset(asked)))


def val(e: R.Extraction, slot: str):
    cs = e.for_slot(slot)
    return cs[0].value if cs else None


# ───────────── 한국어 숫자 ─────────────
@pytest.mark.parametrize("text,expected", [
    ("300만", 3_000_000), ("300만원", 3_000_000), ("3백만", 3_000_000), ("삼백만원", 3_000_000), ("3,000,000원", 3_000_000), ("2000000", 2_000_000),
    ("1억 5천만원", 150_000_000), ("1억5천", 150_000_000), ("1.5억", 150_000_000), ("2천5백만원", 25_000_000), ("이천오백만원", 25_000_000),
    ("오만원", 50_000), ("만원", 10_000), ("천만원", 10_000_000), ("백만원", 1_000_000), ("3만5천원", 35_000), ("2만 5000원", 25_000), ("일억오천만원", 150_000_000),
])
def test_parse_amount_korean_forms(text, expected):
    assert R.parse_amount(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "원", "삼백만가", "3.3.3만"])
def test_parse_amount_rejects_non_numbers(bad):
    assert R.parse_amount(bad) in (None, 0) or bad == "3.3.3만"


# ───────────── 예산 ─────────────
@pytest.mark.parametrize("text,monthly,status", [
    ("월 200만원", 2_000_000, "confirmed"), ("한 달에 500만원 쓸게요", 5_000_000, "confirmed"), ("월 예산 1,000만원", 10_000_000, "confirmed"),
    ("월 3,000,000원", 3_000_000, "confirmed"), ("월 3백만원", 3_000_000, "confirmed"), ("월 천만원", 10_000_000, "confirmed"), ("월 1.5억", 150_000_000, "confirmed"),
    ("예산은 삼백만원이에요", 3_000_000, "inferred"), ("예산 300만 원으로 부탁드립니다", 3_000_000, "inferred"), ("예산 2500000", 2_500_000, "inferred"),
    ("월 3m", 3_000_000, "inferred"), ("monthly budget 3 million won", 3_000_000, "inferred"),
    ("하루 5만원 쓸게요", 1_500_000, "inferred"), ("하루에 3만원", 900_000, "inferred"), ("일 예산 2만원", 600_000, "inferred"), ("매일 1만원", 300_000, "inferred"),
    ("주 20만원씩", 800_000, "inferred"), ("매주 10만원", 400_000, "inferred"), ("2주간 100만원", 2_143_000, "inferred"),
])
def test_budget_forms_and_status(text, monthly, status):
    e = ex(text)
    c = e.for_slot("monthly_budget_krw")
    assert c and c[0].value == monthly and c[0].status == status, [(x.value, x.status) for x in c]
    assert c[0].quote in text                                   # 근거 구절은 글자 그대로
    if status == "inferred" and any(w in text for w in ("하루", "일 예산", "매일", "주 ", "매주", "2주간")):
        assert "가정" in c[0].note                              # 환산은 가정이라고 밝힌다


def test_daily_budget_conversion_is_labelled_as_assumption():
    c = ex("하루 5만원").for_slot("monthly_budget_krw")[0]
    assert c.status == "inferred" and str(R.ASSUMED_DAYS_PER_MONTH) in c.note and "가정" in c.note and c.value == 50_000 * R.ASSUMED_DAYS_PER_MONTH


@pytest.mark.parametrize("text", [
    "5만원 이상 구매하면 무료배송", "최대 한도 5,000만원", "월 9,900원 구독 서비스", "연회비 1만원", "수수료 1%", "300만 명이 쓰는 앱", "1000자 이내로 써줘",
    "10% 할인", "전 상품 5,000원 할인 쿠폰", "보증금 1억", "연봉 5천만원 직장인",
])
def test_product_amounts_are_not_budget(text):
    assert not ex(text).for_slot("monthly_budget_krw"), text


def test_budget_with_product_amounts_in_same_message():
    e = ex("최대 한도 5,000만원 대출이고요, 월 300만원 쓸게요. 5만원 이상 구매 시 사은품")
    assert [c.value for c in e.for_slot("monthly_budget_krw")] == [3_000_000]


@pytest.mark.parametrize("text,kind", [
    ("예산은 넉넉히 해주세요", "vague"), ("예산은 알아서 해주세요", "vague"), ("광고비는 얼마든 상관 없어요", "vague"), ("예산 충분해요", "vague"),
    ("월 200~300만원 정도요", "range"), ("200만원에서 300만원 사이", "range"),
    ("예산 300", "unit"), ("총 예산 100만원이에요", "total_period"),
])
def test_vague_range_unit_and_total_budgets_become_ambiguities_not_values(text, kind):
    e = ex(text)
    assert not e.for_slot("monthly_budget_krw"), (text, [(c.value, c.quote) for c in e.candidates])
    assert any(a.slot == "monthly_budget_krw" and a.kind == kind for a in e.ambiguities), (text, e.ambiguities)


def test_total_budget_with_period_is_converted_with_a_note():
    c = ex("2주간 총 100만원").for_slot("monthly_budget_krw")
    assert c and c[0].status == "inferred" and "환산" in c[0].note


def test_monthly_budget_beats_daily_cap_in_same_message():
    e = ex("하루 최대 5만원, 총 한 달 300만원 안에서요")
    assert [c.value for c in e.for_slot("monthly_budget_krw")] == [3_000_000]


def test_bare_amount_is_budget_only_when_budget_was_just_asked_or_message_is_short():
    assert ex("300만원이요", asked=("monthly_budget_krw",)).for_slot("monthly_budget_krw")[0].status == "confirmed"
    short = ex("삼백만원이요").for_slot("monthly_budget_krw")
    assert short and short[0].status == "inferred"
    long_text = "우리 서비스는 오래전부터 많은 분이 쓰고 있어서 300만원 정도 가치가 있다고 봅니다 그래서 여러 가지를 고민 중이에요"
    assert not ex(long_text).for_slot("monthly_budget_krw")


# ───────────── 수정 표지 vs 충돌 ─────────────
@pytest.mark.parametrize("text,is_update", [
    ("예산을 150만원으로 줄여줘", True), ("300만원으로 바꿔줘", True), ("200만원 말고 300만원", True), ("아니 400만원으로", True), ("월 300만원으로 할게요", True),
    ("예산 수정: 월 500만원", True), ("월 300만원이에요", False), ("예산은 월 300만원이에요", False), ("월 300만원 정도 생각해요", False), ("한 달에 500만원 쓸게요", False),
])
def test_update_marker_is_distinguished_from_a_plain_statement(text, is_update):
    c = ex(text).for_slot("monthly_budget_krw")
    assert c, text
    assert c[-1].explicit_update is is_update, (text, c[-1])


def test_desire_verbs_are_not_update_markers():
    """'앱 설치를 늘리고 싶어요' 는 바꿔 달라는 말이 아니다 (로 + 바꾸는 동사가 붙어야 수정)."""
    assert ex("앱 설치를 늘리고 싶어요").for_slot("objective")[0].explicit_update is False
    assert ex("목표를 방문으로 바꿔줘").for_slot("objective")[0].explicit_update is True


@pytest.mark.parametrize("text,delta", [
    ("예산을 50만원 늘려줘", {"op": "add", "amount": 500_000}), ("예산 100만원 줄여줘", {"op": "add", "amount": -1_000_000}),
    ("예산 20% 줄여줘", {"op": "mul", "factor": 0.8}), ("예산 절반으로 줄여줘", {"op": "mul", "factor": 0.5}), ("예산 두 배로 늘려줘", {"op": "mul", "factor": 2.0}),
])
def test_relative_budget_changes(text, delta):
    c = ex(text).for_slot("monthly_budget_krw")
    assert len(c) == 1 and c[0].delta == delta and c[0].explicit_update


def test_relative_change_needs_the_word_budget():
    assert not ex("세트를 반으로 줄여줘").for_slot("monthly_budget_krw")


# ───────────── 목표 ─────────────
@pytest.mark.parametrize("text,obj", [
    ("구매를 늘리고 싶어요", "conversion"), ("신청 전환이 목표예요", "conversion"), ("회원 가입을 늘리고 싶어요", "conversion"), ("매출 늘리고 싶어요", "conversion"),
    ("앱 설치", "app_install"), ("설치 전환", "app_install"), ("앱 다운로드를 늘리고 싶어요", "app_install"),
    ("방문을 늘리고 싶어요", "traffic"), ("클릭 많이 받고 싶어요", "traffic"), ("사이트 유입", "traffic"),
    ("브랜드 인지도를 높이고 싶어요", "awareness"), ("도달률 높이기", "awareness"),
    ("상담 신청을 받고 싶어요", "lead"), ("잠재고객을 모으고 싶어요", "lead"), ("리드를 모으고 싶어요", "lead"),
    ("more app installs", "app_install"), ("sign up campaign", "conversion"), ("traffic", "traffic"), ("awareness", "awareness"),
])
def test_objective_cues_map_to_closed_vocabulary(text, obj):
    c = ex(text).for_slot("objective")
    assert c and c[0].value == obj and c[0].status == "confirmed", (text, [(x.value, x.status) for x in c])
    assert c[0].quote in text


def test_generic_conversion_word_does_not_conflict_with_specific_goal():
    assert [c.value for c in ex("앱 설치 전환이 목표예요").for_slot("objective")] == ["app_install"]
    assert [c.value for c in ex("상담 신청 전환").for_slot("objective")] == ["lead"]


def test_two_different_goals_give_two_candidates_so_the_state_can_ask():
    assert [c.value for c in ex("앱 설치와 구매를 둘 다 늘리고 싶어요").for_slot("objective")] == ["app_install", "conversion"]


def test_weak_goal_cue_is_weak_and_needs_confirmation():
    c = ex("신용대출을 알리고 싶어요").for_slot("objective")[0]
    assert c.value == "awareness" and c.status == "inferred" and c.weak and c.needs_confirm
    assert not ex("신용대출을 알리고 싶어요. 목표는 신청 전환이에요").for_slot("objective")[0].weak       # 분명한 단서가 있으면 약한 단서는 쓰지 않는다


def test_goal_word_inside_a_product_condition_sentence_is_not_a_goal():
    e = ex("5만원 이상 구매 시 무료 배송이에요. 방문을 늘리는 게 목표예요")
    assert [c.value for c in e.for_slot("objective")] == ["traffic"]


def test_bid_words_are_not_goals():
    e = ex("자동 입찰 클릭 최대화로 해주세요. 구매를 늘리고 싶어요")
    assert [c.value for c in e.for_slot("objective")] == ["conversion"]
    assert val(e, "bid_strategy") == "직접 입찰 CPC" or val(e, "bid_strategy") == "자동 입찰 클릭 최대화"


@pytest.mark.parametrize("text", ["목표는 잘 되게요", "광고 잘 됐으면 좋겠어요", "목표는 대박이에요"])
def test_vague_goal_is_an_ambiguity(text):
    e = ex(text)
    assert not e.for_slot("objective") and any(a.slot == "objective" and a.kind == "vague" for a in e.ambiguities)


# ───────────── 업종 ─────────────
@pytest.mark.parametrize("text,cat", [
    ("신용대출 광고", "loan"), ("전세자금대출", "loan"), ("신용카드 발급", "card"), ("보험 상담", "insurance"), ("ETF 투자 앱", "investment"), ("예금 상품", "savings"),
    ("비트코인 거래소", "crypto"), ("쇼핑몰 쿠폰", "ecommerce"), ("OO몰 가을 쿠폰", "ecommerce"), ("가계부 앱", "app"), ("영어 강의", "education"),
    ("건강기능식품", "health"), ("아파트 분양", "realestate"), ("my budgeting app", "app"), ("online shop", "ecommerce"),
])
def test_category_cues(text, cat):
    c = ex(text).for_slot("category")
    assert c and c[0].value == cat and c[0].weak and c[0].status == "inferred", (text, [(x.value) for x in c])


def test_financial_category_wins_over_app_or_commerce():
    assert val(ex("대출 한도 조회 앱"), "category") == "loan"
    assert val(ex("보험 비교 쇼핑몰"), "category") == "insurance"


def test_two_financial_categories_tie_makes_two_non_weak_candidates():
    c = ex("대출이랑 보험 광고").for_slot("category")
    assert len(c) == 2 and not any(x.weak for x in c) and {x.value for x in c} == {"loan", "insurance"}


def test_investment_word_in_budget_phrase_is_not_a_category():
    assert not ex("300만원 투자할게요").for_slot("category")
    assert not ex("광고에 투자할 예정이에요").for_slot("category")
    assert val(ex("투자 앱 광고"), "category") == "investment"


def test_category_declaration_is_confirmed_and_not_weak():
    c = ex("업종은 적금이에요").for_slot("category")[0]
    assert c.value == "savings" and c.status == "confirmed" and not c.weak


def test_category_quote_is_readable_word():
    assert ex("신용대출이에요").for_slot("category")[0].quote == "신용대출"


# ───────────── 타겟 ─────────────
@pytest.mark.parametrize("text,lo,hi", [
    ("20대", 20, 29), ("30~40대", 30, 49), ("20대~30대", 20, 39), ("20~30대 위주", 20, 39), ("40대 후반", 47, 49), ("20대 초반", 20, 23), ("2030 여성", 20, 39),
    ("만 25~34세", 25, 34), ("25-34세", 25, 34), ("만 19세 이상", 19, None), ("30세 이상", 30, None), ("targeting 20s", 20, 29), ("17살", 17, 17),
])
def test_age_forms(text, lo, hi):
    a = val(ex(text), "audience")
    assert a and a.get("age_min") == lo and a.get("age_max") == hi, (text, a)


@pytest.mark.parametrize("text", ["고등학생 대상", "10대 후반", "청소년 타겟", "미성년자", "만 17세", "중학생", "18살", "초등학생 대상", "만 15~18세", "만 19세 미만", "어린이 대상 앱", "10대"])
def test_minor_targets_are_detected(text):
    e = ex(text)
    assert any(R.is_minor_audience(c.value) for c in e.for_slot("audience")), text


@pytest.mark.parametrize("text", ["20대", "만 19세 이상", "30대 직장인", "대학생", "초등학생 학부모", "성인", "만 20세", "100대 기업"])
def test_adults_and_parents_are_not_minor(text):
    assert not any(R.is_minor_audience(c.value) for c in ex(text).for_slot("audience")), text


def test_gender_segments_interests_everyone():
    assert val(ex("여성 대상"), "audience") == {"gender": "여성"}
    assert val(ex("남성분들"), "audience") == {"gender": "남성"}
    assert val(ex("남녀 모두"), "audience") == {"gender": "무관"}
    assert {c.value["segments"][0] for c in ex("직장인과 사회초년생").for_slot("audience") if "segments" in c.value} == {"직장인", "사회초년생"}
    assert val(ex("재테크에 관심 있는 30대"), "audience") == {"age_min": 30, "age_max": 39, "age_text": "30대"} or any(
        c.value.get("interests") == ["재테크"] for c in ex("재테크에 관심 있는 30대").for_slot("audience"))
    assert val(ex("전체 고객 대상"), "audience")["everyone"] is True


def test_segment_word_must_stand_alone_as_a_word():
    assert not ex("3주부터 시작할게요").for_slot("audience")           # '주부' 가 '3주부터' 안에 있어도 직업이 아니다
    assert ex("주부 대상").for_slot("audience")


def test_merge_audience_keeps_fields_and_flags_conflict():
    merged, conflict = R.merge_audience({"age_min": 20, "age_max": 29, "age_text": "20대"}, {"gender": "여성"}, False)
    assert merged["gender"] == "여성" and merged["age_min"] == 20 and not conflict
    merged, conflict = R.merge_audience({"age_min": 20, "age_max": 29, "age_text": "20대"}, {"age_min": 40, "age_max": 49, "age_text": "40대"}, False)
    assert conflict
    merged, conflict = R.merge_audience({"age_min": 20, "age_max": 29, "age_text": "20대", "segments": ["직장인"]}, {"age_min": 30, "age_max": 39, "age_text": "30대"}, True)
    assert not conflict and merged["age_min"] == 30 and merged["segments"] == ["직장인"]


# ───────────── 기간 ─────────────
@pytest.mark.parametrize("text,start,end,days", [
    ("10월 한 달", "10-01", "10-31", 31), ("2월 한 달", "02-01", "02-28", 28), ("11/1~11/30", "11-01", "11-30", 30), ("11월 1일부터 11월 30일까지", "11-01", "11-30", 30),
    ("11월 1일~30일", "11-01", "11-30", 30), ("12/20-1/5", "12-20", "01-05", 17), ("2주간", None, None, 14), ("한 달간", None, None, 30), ("3개월 동안", None, None, 90),
    ("일주일", None, None, 7), ("30일간", None, None, 30),
])
def test_period_forms(text, start, end, days):
    p = val(ex(text), "period")
    assert p and p["start"] == start and p["end"] == end and p["days"] == days, (text, p)


def test_single_end_date_is_a_condition_not_a_campaign_period():
    e = ex("전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)")
    assert not e.for_slot("period")
    assert [c.value for c in e.for_slot("key_conditions")] == ["전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)"]


def test_month_word_in_budget_is_not_a_period():
    assert not ex("월 300만원").for_slot("period")


# ───────────── 조건 ─────────────
def test_conditions_are_always_inferred_and_quoted_literally():
    text = "연 4.5%부터 시작하고 중도상환수수료 없어요. 최대 한도 5,000만원, 10월 31일까지 신청하면 1인 1회 5,000원 적립"
    cs = ex(text).for_slot("key_conditions")
    assert cs and all(c.status == "inferred" and c.quote in text for c in cs)
    assert {c.quote for c in cs} >= {"연 4.5%부터 시작", "중도상환수수료 없어요"}


@pytest.mark.parametrize("text", ["월 300만원 쓸게요", "20대 직장인 대상 신용대출이에요", "문의 1588-0000", "목표는 신청 전환이에요", "10월 한 달 진행해요", "오늘 날씨 어때요?"])
def test_budget_audience_period_phone_sentences_are_not_conditions(text):
    assert not ex(text).for_slot("key_conditions"), text


def test_request_to_write_a_phrase_is_not_a_condition():
    assert not ex("최저 금리 연 3%라고 꼭 써줘").for_slot("key_conditions")


# ───────────── 주소 · 어조 · 세트 · 입찰 · 이름 ─────────────
def test_url_only_http_without_credentials():
    assert val(ex("https://example.com/promo?x=1 이 주소로"), "landing_url") == "https://example.com/promo?x=1"
    assert val(ex("www.example.co.kr 로 연결"), "landing_url") == "https://www.example.co.kr"
    for bad in ["javascript:alert(1)", "http://user:pw@evil.com/x", "ftp://example.com", "https://localhost", "data:text/html,hi", "http://."]:
        assert not ex(bad).for_slot("landing_url"), bad


def test_url_strips_trailing_punctuation_and_quote_is_literal():
    text = "주소는 https://example.com/a. 입니다"
    c = ex(text).for_slot("landing_url")[0]
    assert c.value == "https://example.com/a" and c.quote in text


@pytest.mark.parametrize("text,n", [("세트를 둘로 줄여줘", 2), ("세트는 1개만", 1), ("광고 세트 3개로", 3), ("두 개 세트로", 2), ("세트 하나로", 1)])
def test_ad_set_count(text, n):
    c = ex(text).for_slot("ad_set_count")
    assert c and c[0].value == n and c[0].explicit_update


@pytest.mark.parametrize("text,bid", [("CPC 입찰로 해주세요", "직접 입찰 CPC"), ("CPM로", "직접 입찰 CPM"), ("전환 최대화 방식", "자동 입찰 전환 최대화"),
                                     ("클릭 최대화로", "자동 입찰 클릭 최대화"), ("tCPA 로", "자동 입찰 tCPA")])
def test_bid_strategy(text, bid):
    assert val(ex(text), "bid_strategy") == bid


def test_tone_and_name_forms():
    assert val(ex("말투는 친근하게 해주세요"), "tone")
    assert val(ex("상품명은 가상은행 신용대출이에요"), "product_name") == "가상은행 신용대출"
    assert val(ex("“달콤몰”이라는 쇼핑몰이에요"), "product_name") == "달콤몰"
    assert val(ex("OO몰 가을 쿠폰"), "product_name") == "OO몰"
    assert not ex("온라인몰을 운영해요").for_slot("product_name")


# ───────────── 답변 해석 ─────────────
@pytest.mark.parametrize("text", ["네", "네!", "맞아요", "응", "그렇게 해주세요", "ok", "좋아요"])
def test_affirmative_short_answers(text):
    e = ex(text)
    assert e.affirmative and not e.candidates


@pytest.mark.parametrize("text", ["아니요", "아뇨", "아니야", "no", "틀려요"])
def test_negative_short_answers(text):
    assert ex(text).negative


def test_choice_words():
    assert ex("처음 말한 걸로요").choice == 0
    assert ex("두 번째로 할게요").choice == 1
    assert ex("나중에 말한 거요").choice == 1


# ───────────── 불변식 · 안전 ─────────────
CORPUS = [
    "20대 직장인 대상 신용대출, 월 300만원, 신청 전환이에요.", "가을 쿠폰 캠페인이에요. 월 200만원으로 구매를 늘리고 싶고, 10월 한 달 진행합니다. 전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)",
    "https://example.com 월 3m 하루 5만원 30~40대 여성 11/1~11/30 세트를 둘로 CPC", "I want more app installs. monthly budget 3 million won, targeting 20s",
    "💳 신용카드 발급 신청 전환 🎯 월 500만원 💰 20대 🙌", "예산을 150만원으로 줄여줘. 아니 200만원으로", "고등학생 대상 앱 설치", "월 200~300만원", "연 4.5%부터 시작하고 중도상환수수료 없어요",
]


@pytest.mark.parametrize("text", CORPUS)
def test_every_candidate_quote_is_a_literal_substring_of_the_user_text(text):
    for c in ex(text).candidates:
        assert c.quote and c.quote in text, (text, c)
        if c.start >= 0:
            assert text[c.start:c.end].strip() == c.quote or c.quote in text[c.start:c.end + 1]


def test_empty_and_symbol_only_inputs_give_nothing():
    for t in ["", "   ", "😀😀😀", "...", "?!?!"]:
        e = ex(t)
        assert not e.candidates and not e.ambiguities


@pytest.mark.parametrize("text", ["월 " * 400, "3" * 1000, "(" * 500, ", " * 400, "하고 " * 250, "백만" * 250, "~" * 900, "30~" * 300, "월 200만원 " * 90, "http://" + "a" * 990])
def test_extraction_is_fast_on_hostile_1000_char_inputs(text):
    t0 = time.perf_counter()
    R.extract(text[:1000])
    assert time.perf_counter() - t0 < 2.0


def test_extraction_is_deterministic():
    text = CORPUS[1]
    a = [(c.slot, str(c.value), c.quote, c.status) for c in ex(text).candidates]
    b = [(c.slot, str(c.value), c.quote, c.status) for c in ex(text).candidates]
    assert a == b


def test_value_text_round_trips_through_parse_value():
    """모델 출력 스키마(문자열)로 옮겼다가 되읽어도 같은 값이어야 한다 — 대역이 규칙을 되풀이할 수 있는 이유."""
    for text in CORPUS:
        for c in ex(text).candidates:
            if c.delta is not None or c.slot in ("category", "objective", "key_conditions", "product_name", "tone", "bid_strategy"):
                continue
            back = R.parse_value(c.slot, R.value_to_text(c.slot, c.value))
            if c.slot == "audience":
                assert back is not None and all(back.get(k) == v for k, v in c.value.items() if k not in ("age_text",)), (c, back)
            elif c.slot == "period":
                assert back["days"] == c.value["days"] and back["start"] == c.value["start"]
            else:
                assert back == c.value, (c, back)


def test_assumed_constants_are_named_and_documented():
    """플랫폼 규격 같은 상수는 ASSUMED_* 이름과 '가정' 주석을 가진다."""
    import inspect

    src = inspect.getsource(R)
    for name in ("ASSUMED_DAYS_PER_MONTH", "ASSUMED_MIN_MONTHLY_BUDGET_KRW", "ASSUMED_MIN_BUDGET_PER_AD_SET_KRW", "ASSUMED_MAX_AD_SETS"):
        line = next(l for l in src.splitlines() if l.startswith(name))
        assert "가정" in line, line
    assert R.ASSUMED_MIN_MONTHLY_BUDGET_KRW == 2 * R.ASSUMED_MIN_BUDGET_PER_AD_SET_KRW


# ───────────── 부정 · 연령 범위 · 절 나누기 ─────────────
def test_negated_cue_is_not_read_as_a_goal_or_category():
    e = ex("영어 학원 광고, 설치는 필요 없고 상담 신청이 목표")
    assert [c.value for c in e.for_slot("objective")] == ["lead"]
    assert [c.value for c in ex("대출 말고 적금 광고").for_slot("category")] == ["savings"]
    assert [c.value for c in ex("구매 말고 설치가 목표예요").for_slot("objective")] == ["app_install"]


@pytest.mark.parametrize("text,lo,hi", [
    ("20대 후반부터 30대 초반", 27, 33), ("30대 후반에서 40대 초반", 37, 43), ("20대~30대", 20, 39), ("20대 초반~20대 후반", 20, 29),
])
def test_age_span_between_two_decades_is_one_range_not_two_values(text, lo, hi):
    c = [x for x in ex(text).for_slot("audience") if "age_min" in x.value]
    assert len(c) == 1 and (c[0].value["age_min"], c[0].value["age_max"]) == (lo, hi)


def test_connective_endings_split_conditions_so_the_period_stays_out_of_the_quote():
    text = "10월 한 달 동안 진행할 거고 가입하면 5,000원 쿠폰을 드려요(1인 1회)"
    e = ex(text)
    assert val(e, "period")["days"] == 31
    assert [c.quote for c in e.for_slot("key_conditions")] == ["가입하면 5,000원 쿠폰을 드려요(1인 1회)"]


def test_new_customer_wanting_is_not_a_condition_but_new_customer_only_is():
    assert not ex("신규 고객을 많이 확보하고 싶어요").for_slot("key_conditions")
    assert ex("신규 고객 한정 쿠폰이에요").for_slot("key_conditions")
