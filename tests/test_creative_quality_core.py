"""소재 퀄리티 신호(core/creative_quality.py) 단위 테스트 — 네트워크·모델 없음.

- 신호 8개의 방향(좋은 문구가 더 높다), 계산 불가 시 None 과 종합에서의 제외, 가중치 합 1, 등급 띠
- 조건 표시는 guardrails.grounding 의 공개 함수를 그대로 쓴다(새 비교 로직 없음) — 함수를 바꿔 끼우면 결과가 따라 바뀐다
- 빈 문구·공백·이모지·영어만·아주 긴 문구·한자에서도 예외 없이 0~1 점수 또는 None
- 결정론, 비교(compare_cards)의 같은 기준·무승부·대칭, 신호 빼기(ablation)가 실제로 점수에 영향
"""
from __future__ import annotations

import json
import time

import pytest

from core import creative_quality as cq
from core.schemas import AdCopy, Fact
from guardrails.policy import get_catalog

SHOP = [Fact(id="F1", text="전 상품 10% 할인, 50,000원 이상 구매 시"), Fact(id="F2", text="10월 31일까지 진행"), Fact(id="F4", text="쿠폰은 1인 1회 사용 가능")]
APP = [Fact(id="F1", text="소비 내역 자동 분류"), Fact(id="F2", text="7일 무료 체험 후 월 4,900원 자동결제"), Fact(id="F4", text="은행·카드 120곳 연동")]


def ad(h="", b="", c="", **kw) -> AdCopy:
    return AdCopy(headline=h, body=b, cta=c, **kw)


def card(copy: AdCopy, facts=None, cats=None) -> cq.QualityCard:
    return cq.quality_card(copy, SHOP if facts is None else facts, {"ecommerce"} if cats is None else cats)


GOOD = ad("전 상품 10% 할인", "5만원 이상 구매 시 10월 31일까지, 1인 1회 쓸 수 있어요.", "쿠폰 받기")


# ───────────────────────── 구조 ─────────────────────────
def test_weights_sum_to_one_and_cover_every_signal():
    assert set(cq.WEIGHTS) == set(cq.SIGNAL_KEYS) == set(cq.SIGNAL_NAME) == set(cq.SIGNAL_MEASURES) == set(cq.SIGNAL_NOT_MEASURE)
    assert abs(sum(cq.WEIGHTS.values()) - 1.0) < 1e-9 and all(w > 0 for w in cq.WEIGHTS.values())


def test_every_signal_carries_value_score_explanation_and_one_line_scope():
    c = card(GOOD)
    assert [s.key for s in c.signals] == list(cq.SIGNAL_KEYS)
    for s in c.signals:
        assert s.name == cq.SIGNAL_NAME[s.key] and s.explanation.strip()
        assert s.measures == cq.SIGNAL_MEASURES[s.key] and s.does_not_measure == cq.SIGNAL_NOT_MEASURE[s.key]
        assert "\n" not in s.measures and "\n" not in s.does_not_measure
        assert (s.score is None) == (s.value is None)
        if s.score is not None:
            assert 0.0 <= s.score <= 1.0
    json.dumps(c.to_dict(), ensure_ascii=False)          # 사전으로 바꿔 저장할 수 있다


def test_assumed_limits_match_the_policy_catalog_and_are_labeled_as_assumptions():
    lim = get_catalog().length_limits
    assert (cq.ASSUMED_LIMITS.headline_max, cq.ASSUMED_LIMITS.body_max, cq.ASSUMED_LIMITS.cta_max) == (lim["headline"], lim["body"], lim["cta"])
    assert "가정" in cq.PlatformLimits.__doc__ and "다를 수 있다" in cq.PlatformLimits.__doc__
    for lo, hi in (cq.ASSUMED_LIMITS.headline_best, cq.ASSUMED_LIMITS.body_best, cq.ASSUMED_LIMITS.cta_best):
        assert 0 < lo < hi


def test_grade_bands_follow_the_score_only():
    assert [cq.grade_of(x) for x in (1.0, 0.70, 0.699, 0.50, 0.499, 0.0)] == ["상", "상", "중", "중", "하", "하"]
    assert cq.grade_of(None) == ""
    assert card(GOOD).grade == cq.grade_of(card(GOOD).overall)


def test_deterministic_same_input_same_output():
    a = json.dumps(card(GOOD).to_dict(), ensure_ascii=False, sort_keys=True)
    b = json.dumps(card(GOOD).to_dict(), ensure_ascii=False, sort_keys=True)
    assert a == b


def test_overall_is_the_weighted_mean_of_computed_signals_with_weights_renormalized():
    c = card(ad("전 상품 10% 할인", "", ""), facts=[])           # 상품 정보가 없다 → 구체성·조건 표시는 계산 불가
    skipped = {s.key for s in c.signals if s.score is None}
    assert {"specificity", "conditions"} <= skipped
    used = {s.key: s.score for s in c.signals if s.score is not None}
    total = sum(cq.WEIGHTS[k] for k in used)
    assert c.overall == pytest.approx(sum(v * cq.WEIGHTS[k] / total for k, v in used.items()))
    assert c.weights_used.keys() == used.keys() and sum(c.weights_used.values()) == pytest.approx(1.0)
    assert {k for k, _ in c.excluded} == skipped and all(why.strip() for _, why in c.excluded)    # 제외한 사실과 이유를 숨기지 않는다


# ───────────────────────── 빈 입력·특이 입력 안전 ─────────────────────────
EDGE = {
    "empty": ad(), "spaces": ad("   ", "\n\n", "\t"), "emoji": ad("🔥🔥🔥", "🎉🎉", "👉"), "english": ad("Get 10% cashback today", "Apply now and save more with our card.", "Apply"),
    "punct": ad("!!!???", "....", "..."), "hanja": ad("特別 優待 혜택", "最高 品質 保證", "申請"), "digits": ad("1234567890", "0.5 1,000 99%", "123"),
    "very_long": ad("가" * 20_000, "나다 " * 8_000, "다" * 3_000), "many_disclosures": ad("a", "b", "c", disclosures=["고지"] * 500),
    "mixed_tabs": ad("전 상품\t10%\n할인", "5만원\r\n이상", " 쿠폰 받기 "),
}


@pytest.mark.parametrize("name", list(EDGE))
def test_edge_inputs_never_raise_and_stay_in_range(name):
    t0 = time.perf_counter()
    c = card(EDGE[name])
    assert time.perf_counter() - t0 < 5.0
    assert len(c.signals) == 8
    for s in c.signals:
        assert s.score is None or 0.0 <= s.score <= 1.0
    assert c.overall is None or 0.0 <= c.overall <= 1.0
    assert (c.overall is None) == (c.grade == "")
    json.dumps(c.to_dict(), ensure_ascii=False)


def test_empty_copy_has_no_overall_and_says_why_every_signal_was_excluded():
    c = card(EDGE["empty"])
    assert c.overall is None and c.grade == "" and c.weights_used == {}
    assert {k for k, _ in c.excluded} == set(cq.SIGNAL_KEYS)


def test_very_long_copy_is_read_only_up_to_the_cap_and_says_so():
    c = card(EDGE["very_long"])
    assert c.notes and "앞부분만" in c.notes[0]
    assert c.signal("length").value["headline"] == 20_000        # 길이 신호는 잘리기 전의 글자 수를 쓴다
    assert c.signal("length").score == 0.0


def test_non_korean_copy_is_handled_without_pretending():
    c = card(EDGE["english"])
    assert c.signal("tone").score is None                    # 한글 종결 어미가 없다 → 말투는 계산 불가
    assert c.signal("readability").score is not None and c.signal("readability").value["latin_ratio"] > 0.9
    assert c.signal("readability").score < card(GOOD).signal("readability").score


def test_unknown_category_and_enum_categories_are_accepted():
    from core.schemas import ProductCategory

    assert cq.quality_card(GOOD, SHOP, {ProductCategory.CARD, "nonexistent"}).overall is not None
    assert cq.quality_card(GOOD, SHOP, set()).overall is not None


# ───────────────────────── 신호 1 — 길이 ─────────────────────────
def test_length_inside_best_range_is_full_and_over_the_assumed_max_drops():
    ok = cq._length(ad("전 상품 10% 할인", "5만원 이상 구매 시 쓸 수 있어요.", "쿠폰 받기"), cq.ASSUMED_LIMITS)
    over = cq._length(ad("전 상품 10% 할인", "가" * 130, "쿠폰 받기"), cq.ASSUMED_LIMITS)
    short = cq._length(ad("할인", "5만원 이상 구매 시 쓸 수 있어요.", "쿠폰 받기"), cq.ASSUMED_LIMITS)
    assert ok.score == 1.0 and over.score < 0.8 and short.score < ok.score
    assert "가정한 상한 90자를 넘어요" in over.explanation and "가정" in ok.explanation


def test_length_uses_the_platform_argument():
    tight = cq.PlatformLimits(headline_max=10, body_max=20, cta_max=6, headline_best=(2, 6), body_best=(4, 12), cta_best=(2, 4))
    c = cq.quality_card(GOOD, SHOP, {"ecommerce"}, platform=tight)
    assert c.signal("length").score < card(GOOD).signal("length").score
    assert c.signal("length").value == card(GOOD).signal("length").value       # 실측치(글자 수)는 규격과 무관


def test_length_ignores_empty_fields_but_says_so():
    s = cq._length(ad("전 상품 10% 할인", "", ""), cq.ASSUMED_LIMITS)
    assert s.score == 1.0 and "비어 있어" in s.explanation


# ───────────────────────── 신호 2 — 구체성 ─────────────────────────
def test_specificity_prefers_fact_words_and_numbers_over_vague_modifiers():
    concrete = cq._specificity(ad("전 상품 10% 할인", "5만원 이상 구매 시 쓸 수 있어요."), SHOP)
    vague = cq._specificity(ad("특별한 혜택을 만나보세요", "다양한 즐거움이 가득해요."), SHOP)
    assert concrete.score > vague.score + 0.5
    assert vague.value["vague"] >= 3 and "막연한 말" in vague.explanation


def test_specificity_does_not_count_numbers_that_are_not_in_the_facts():
    grounded = cq._specificity(ad("전 상품 10% 할인", "5만원 이상 구매 시"), SHOP)
    invented = cq._specificity(ad("전 상품 25% 할인", "98%가 만족한 300만 명의 선택"), SHOP)
    assert invented.value["numbers"] > invented.value["numbers_supported"]
    assert grounded.score > invented.score and "상품 정보에 없는 숫자" in invented.explanation


def test_specificity_without_facts_or_words_is_not_computable():
    assert cq._specificity(ad("전 상품 10% 할인", "x"), []).score is None
    assert cq._specificity(ad("", ""), SHOP).score is None
    assert cq._specificity(ad("🔥🔥", "👉"), SHOP).score is None


def test_range_wording_choigo_with_a_number_is_not_a_vague_phrase():
    facts = [Fact(id="F1", text="연 최고 4.0% (12개월 만기, 세전)")]
    assert cq._specificity(ad("12개월 적금, 연 최고 4.0%", "세전 금리예요."), facts).value["vague"] == 0
    assert cq._specificity(ad("최고의 적금", "세전 금리예요."), facts).value["vague"] == 1


# ───────────────────────── 신호 3 — 조건 표시 (grounding 재사용) ─────────────────────────
def test_conditions_credit_only_conditions_stated_with_the_same_value():
    full = cq._conditions(ad("전 상품 10% 할인", "5만원 이상 구매 시, 10월 31일까지, 1인 1회 쓸 수 있어요."), SHOP)
    none = cq._conditions(ad("전 상품 10% 할인", "장바구니에 담고 결제만 하면 돼요."), SHOP)
    wrong = cq._conditions(ad("전 상품 10% 할인", "5천원 이상 구매 시, 10월 31일까지, 1인 1회 쓸 수 있어요."), SHOP)    # 값이 사실과 다르다
    assert full.score == 1.0 and full.value["stated"] == full.value["required"] == 3
    assert none.score == 0.0 and none.value["omitted"]
    assert wrong.score < 1.0 and wrong.value["conflict"]
    assert full.score > wrong.score >= none.score


def test_conditions_are_none_when_the_copy_uses_no_conditional_fact_or_none_exist():
    assert cq._conditions(ad("회원가입", "지금 시작해요"), [Fact(id="F1", text="월 이용료 없음")]).score is None       # 코드가 비교하는 조건이 없는 사실
    assert cq._conditions(ad("영어 공부", "함께 해요"), SHOP).score is None                                         # 문구가 조건 있는 사실을 쓰지 않았다
    assert cq._conditions(ad("전 상품 10% 할인"), []).score is None


def test_autopay_condition_is_read_by_grounding_not_by_this_module():
    stated = cq._conditions(ad("7일 무료 체험 후 월 4,900원", "체험이 끝나면 자동결제돼요."), APP)
    hidden = cq._conditions(ad("7일 무료로 써 보세요", "소비 내역 자동 분류를 먼저 경험해 보세요."), APP)
    assert stated.score == 1.0 and hidden.score == 0.0 and "자동결제" in hidden.explanation


def test_conditions_signal_is_driven_by_grounding_condition_issues(monkeypatch):
    """변이 확인: 조건 비교를 이 모듈이 직접 하지 않는다 — grounding.condition_issues 를 바꿔 끼우면 결과가 따라 바뀐다."""
    from guardrails.grounding import CondIssue

    copy = ad("전 상품 10% 할인", "5만원 이상 구매 시, 10월 31일까지, 1인 1회 쓸 수 있어요.")
    assert cq._conditions(copy, SHOP).score == 1.0
    calls = []
    real = cq.condition_issues

    def fake(c, all_facts, refs):
        calls.append(len(refs))
        out = real(c, all_facts, refs)
        if c.headline:      # 실제 문구에 대한 호출에만 가짜 이슈를 더한다(빈 문구 호출은 지켜야 할 조건 목록을 만드는 호출)
            out = out + [CondIssue("최소 금액 조건", "omitted", all_facts[0], "50,000원 이상", "", -1, -1, "가짜")]
        return out

    monkeypatch.setattr(cq, "condition_issues", fake)
    s = cq._conditions(copy, SHOP)
    assert calls and s.score < 1.0                        # 가짜 이슈가 점수에 반영됨 = 점수가 grounding 결과에서 온다


def test_conditions_module_defines_no_condition_patterns_of_its_own():
    src = open(cq.__file__, encoding="utf-8").read()
    for needle in ("_c_min_amount", "_c_deadline", "_c_limit", "_c_autopay", "_MIN_AMOUNT", "_DEADLINE", "_LIMIT_PER_PERSON", "_EXCLUDE ="):
        assert needle not in src
    assert "from guardrails.grounding import" in src and "condition_issues" in src


# ───────────────────────── 신호 4 — 행동 유도 ─────────────────────────
@pytest.mark.parametrize("cta,lo,hi", [
    ("카드 신청하기", 1.0, 1.0), ("쿠폰 받기", 1.0, 1.0), ("내 한도 확인하기", 1.0, 1.0), ("신청하기", 0.8, 0.8), ("지금 신청", 0.8, 0.8),
    ("자세히 보기", 0.2, 0.2), ("바로가기", 0.2, 0.2), ("클릭", 0.2, 0.2), ("더 알아보기", 0.4, 0.4), ("Apply now", 0.5, 0.5), ("👉", 0.5, 0.5),
])
def test_cta_scores_follow_the_constant_list(cta, lo, hi):
    s = cq._cta(ad("x", "y", cta))
    assert lo <= s.score <= hi


def test_empty_cta_is_not_computable():
    assert cq._cta(ad("x", "y", "  ")).score is None


# ───────────────────────── 신호 5 — 가독성 ─────────────────────────
def test_readability_penalizes_symbol_clumps_jargon_and_noun_chains():
    easy = cq._readability(ad("해외여행 상해 최대 1억원 보장", "월 3,200원부터, 가입은 3분이면 끝나요."))
    clump = cq._readability(ad("[여행자보험]상해 1억(조건별 상이)/월3,200원~(30세)", "해외상해·후유장해·휴대품손해·항공기지연 담보, 비대면 가입 3분 소요"))
    jargon = cq._readability(ad("프리미엄 스테이 얼리버드 프로모션", "3박+ 예약 시 리워드 쿠폰 인센티브 15% 어드밴티지 제공"))
    chain = cq._readability(ad("AI STT 회의록 요약 액션 아이템 추출 솔루션", "자동 요약 시스템 팀원 무료 플랜 제공"))
    assert easy.score > 0.9 and clump.score < easy.score - 0.1 and jargon.score < easy.score - 0.1 and chain.score < easy.score - 0.1
    assert jargon.value["jargon"] >= 3 and chain.value["longest_noun_run"] >= 6


def test_readability_long_sentence_and_hanja():
    long = cq._readability(ad("가" * 60, "나" * 70))
    hanja = cq._readability(ad("特別 優待 혜택", "最高 品質 保證 안내"))
    assert long.score < 0.8 and hanja.value["hanja_ratio"] > 0.3 and hanja.score < 0.9


def test_readability_needs_letters():
    assert cq._readability(ad("1234", "!!!")).score is None


# ───────────────────────── 신호 6 — 중복·군더더기 ─────────────────────────
def test_redundancy_flags_repeated_words_headline_echo_and_filler():
    fresh = cq._redundancy(ad("월 최대 3만원 캐시백", "전월 30만원 이상 쓰면 연회비 없이 받아요.", "카드 신청하기"))
    echo = cq._redundancy(ad("월 최대 3만원 캐시백", "월 최대 3만원 캐시백 혜택, 캐시백을 받아 가세요.", "카드 신청하기"))
    filler = cq._redundancy(ad("정말 정말 편한 가계부", "진짜 편한 가계부 앱, 편한 가계부로 편하게 쓰세요.", "앱 설치하기"))
    assert fresh.score > 0.9 and echo.score < fresh.score - 0.15 and filler.score < fresh.score - 0.15
    assert echo.value["headline_body_overlap"] > fresh.value["headline_body_overlap"] and filler.value["filler"] >= 2


def test_redundancy_needs_two_content_words_and_works_with_one_field():
    assert cq._redundancy(ad("할인")).score is None
    assert cq._redundancy(ad("전 상품 10% 할인 쿠폰", "", "쿠폰 받기")).value["headline_body_overlap"] is None


# ───────────────────────── 신호 7 — 압박·과장 밀도 ─────────────────────────
def test_pressure_counts_expression_hits_per_length_and_is_not_a_verdict():
    clean = cq._pressure(ad("전 상품 10% 할인", "5만원 이상 구매 시 쓸 수 있어요.", "쿠폰 받기"), {"ecommerce"})
    hyped = cq._pressure(ad("역대급 10% 할인, 지금 아니면 끝!", "5만원 이상 구매하면 쿠폰 적용! 서두르세요!!", "쿠폰 받기"), {"ecommerce"})
    assert clean.score == 1.0 and hyped.score < 0.4
    assert "심사가 따로 판단" in hyped.explanation and "위반" in cq.SIGNAL_NOT_MEASURE["pressure"]
    assert {"역대급", "지금 아니면"} <= set(hyped.value["phrases"])


def test_pressure_is_normalized_by_text_length():
    hit = "역대급 할인"
    short = cq._pressure(ad(hit, "", ""), set())
    padded = cq._pressure(ad(hit, "가입하면 월 최대 3만원을 전월 실적 30만원 이상 사용 시 돌려받을 수 있고 연회비는 없어요. 해외 결제 수수료도 면제돼요.", ""), set())
    assert padded.score > short.score and padded.value["weighted_hits"] == short.value["weighted_hits"]


def test_pressure_ignores_range_wording_but_counts_plain_superlatives():
    assert cq._pressure(ad("연 최고 4.0% 적금", "세전이에요."), {"savings"}).score == 1.0
    assert cq._pressure(ad("최고의 서비스를 만나보세요", "놀라운 혜택"), {"savings"}).score < 0.5


def test_pressure_counts_warn_level_hits_of_any_lexical_rule():
    """정책 카탈로그에서 '주의(WARN)' 로 분류되는 표현(후기·보증 암시)도 센다 — 등급은 읽지 않고 개수만 센다."""
    s = cq._pressure(ad("실제 후기 많은 앱", "만족도 98%", "앱 설치하기"), {"app"})
    assert s.value["weighted_hits"] >= 2 and s.score < 0.6


# ───────────────────────── 신호 8 — 말투 일관성 ─────────────────────────
@pytest.mark.parametrize("h,b,expect_lo,expect_hi", [
    ("소비 내역이 자동으로 분류돼요", "은행·카드를 연결하면 지출이 정리돼요.", 1.0, 1.0),
    ("소비 내역이 자동으로 분류됩니다", "은행·카드를 연결하면 지출이 정리돼요.", 0.35, 0.65),
    ("중도상환수수료가 없습니다", "서류 제출도 필요 없습니다.", 1.0, 1.0),
    ("한 번에 20분씩 수업해요", "일단 신청해", 0.0, 0.2),
    ("첫 인화 10장은 무료다", "배송비만 내면 출고돼요.", 0.0, 0.2),
    ("월 최대 3만원 캐시백", "연회비 없는 카드", 1.0, 1.0),
])
def test_tone_detects_mixed_politeness_levels(h, b, expect_lo, expect_hi):
    s = cq._tone(ad(h, b))
    assert expect_lo <= s.score <= expect_hi


def test_tone_nouns_that_end_in_yo_are_not_haeyo():
    s = cq._tone(ad("필요한 서류 없음", "중요 혜택 안내"))
    assert s.value == {"합쇼체": 0, "해요체": 0, "반말": 0}


def test_tone_is_not_computable_without_korean():
    assert cq._tone(ad("Get 10% cashback", "Apply now")).score is None


# ───────────────────────── 방향 — 8개 신호가 합쳐서도 좋은 문구를 위로 ─────────────────────────
def test_overall_orders_obvious_good_over_obvious_bad():
    bad = ad("역대급 혜택, 지금 아니면 끝!!", "특별한 혜택을 만나보세요. 정말 정말 좋아요. 진짜 좋아요", "자세히 보기")
    assert card(GOOD).overall > card(bad).overall + 0.25
    assert card(GOOD).grade == "상" and card(bad).grade in ("중", "하")


def test_quality_does_not_depend_on_variant_id_or_claims_or_tone_label():
    a = card(ad("전 상품 10% 할인", "5만원 이상 구매 시", "쿠폰 받기", variant_id="var_1", tone="담백"))
    b = card(ad("전 상품 10% 할인", "5만원 이상 구매 시", "쿠폰 받기", variant_id="var_2", tone="혜택 강조"))
    assert a.to_dict() == b.to_dict()


# ───────────────────────── 비교·신호 빼기 ─────────────────────────
def test_compare_is_symmetric_and_names_the_signals_that_decided():
    good, bad = card(GOOD), card(ad("특별한 혜택을 만나보세요", "다양한 즐거움이 가득해요.", "자세히 보기"))
    ab, ba = cq.compare_cards(good, bad), cq.compare_cards(bad, good)
    assert ab.winner == "a" and ba.winner == "b" and ab.diff == pytest.approx(-ba.diff)
    assert ab.reasons[0][1] > 0 and ab.reasons[0][0] in cq.SIGNAL_KEYS
    assert cq.compare_cards(good, good).winner == "tie"


def test_compare_uses_only_signals_computed_for_both_cards():
    with_cond = card(GOOD)
    no_cond = card(ad("지금 시작해요", "함께해요", "시작하기"))                # 조건 있는 사실을 쓰지 않음 → conditions 계산 불가
    assert with_cond.signal("conditions").score is not None and no_cond.signal("conditions").score is None
    cmp = cq.compare_cards(with_cond, no_cond)
    assert "conditions" not in cmp.basis                  # 한쪽에서만 계산된 신호는 비교에 넣지 않는다


def test_compare_with_nothing_in_common_is_unknown():
    assert cq.compare_cards(card(EDGE["empty"]), card(GOOD)).winner == "unknown"
    assert cq.compare_signal(card(EDGE["empty"]), card(GOOD), "cta") is None


def test_tie_margin_is_the_boundary():
    a, b = card(GOOD), card(ad("전 상품 10% 할인", "5만원 이상 구매 시 10월 31일까지, 1인 1회 쓸 수 있어요.", "쿠폰 받기"))
    d = abs(cq.compare_cards(a, b, margin=0.0).diff)
    assert d >= 0
    if d > 0:
        assert cq.compare_cards(a, b, margin=d + 1e-6).winner == "tie" and cq.compare_cards(a, b, margin=d - 1e-9 if d > 1e-9 else 0.0).winner in ("a", "b", "tie")


def test_removing_a_signal_changes_the_score_ablation_has_an_effect():
    """변이 확인: exclude 가 무시되면 이 테스트가 실패한다 — 신호를 빼면 종합과 비교 결과가 실제로 달라져야 ablation 을 읽을 수 있다."""
    good, vague = card(GOOD), card(ad("전 상품 10% 할인", "장바구니에 담고 결제만 하면 돼요.", "쿠폰 받기"))
    full = cq.compare_cards(good, vague)
    for key in ("conditions", "specificity"):
        assert key in full.basis
        less = cq.compare_cards(good, vague, exclude={key})
        assert key not in less.basis and less.diff != pytest.approx(full.diff)
    one, _ = cq.overall_score(good.signals)
    two, used = cq.overall_score(good.signals, exclude={"tone"})
    assert one != pytest.approx(two) or good.signal("tone").score == one
    assert "tone" not in used and sum(used.values()) == pytest.approx(1.0)


def test_weights_argument_changes_the_comparison():
    good, vague = card(GOOD), card(ad("전 상품 10% 할인", "장바구니에 담고 결제만 하면 돼요.", "쿠폰 받기"))
    only_tone = {k: (1.0 if k == "tone" else 0.0) for k in cq.SIGNAL_KEYS}
    assert cq.compare_cards(good, vague, weights=only_tone).winner == "tie"
    assert cq.compare_cards(good, vague).winner == "a"


# ───────────────────────── 무작위 입력 (시드 고정) ─────────────────────────
def test_random_text_never_breaks_the_card_and_scores_stay_in_range():
    """한글·영문·숫자·기호·이모지·한자·제로폭·전각 숫자를 섞은 무작위 문구 300개 — 예외 없이 0~1 점수 또는 None, 값이 없으면 점수도 없다."""
    import random

    rng = random.Random(20261019)
    alpha = (list("가나다라마바사아자차카타파하") + list("abcXYZ") + list("0123456789") + list(" \t\n,.!?%()[]{}<>/·~-+*&$#@") + list("🔥🎉👉✨") + list("特別優待保證")
             + ["습니다", "해요", "세요", "최고", "1+1", "10%", "5만원 이상", "까지", "자동결제", "1인 1회", "제외", "​", "　", "０１２"])

    def rs(n: int) -> str:
        return "".join(rng.choice(alpha) for _ in range(rng.randint(0, n)))

    facts_pool = SHOP + APP + [Fact(id="F9", text="일부 제외 1인 1회")]
    for _ in range(300):
        c = card(ad(rs(40), rs(150), rs(20), disclosures=[rs(30) for _ in range(rng.randint(0, 2))]), facts=rng.sample(facts_pool, rng.randint(0, 5)) + ([Fact(id="X", text=rs(60))] if rng.random() < 0.3 else []),
                 cats={rng.choice(["card", "loan", "other", "ecommerce"])})
        for s in c.signals:
            assert s.score is None or 0.0 <= s.score <= 1.0
            assert (s.score is None) == (s.value is None)
        assert c.overall is None or 0.0 <= c.overall <= 1.0
        json.dumps(c.to_dict(), ensure_ascii=False)
