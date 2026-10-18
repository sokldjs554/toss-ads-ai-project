"""3차 재점검(검토자 재현) 중 숫자·조건 비교 엔진과 글 정규화의 결함 회귀.

- 시간: 장식 문자(. - ( * 줄바꿈 …)가 수천~수만 개 이어진 입력에서 normalize 가 제곱 시간이던 것, 그리고 같은 이유로 느려지던
  grounding.check · output_guard.check · run_pipeline (랜딩·주장·사실 글에는 길이 상한이 없었다).
- 정확도: 조사가 낀 부정, 한정 없는 '자동결제 없음', 조 단위 금액, 포인트 하이픈 범위, 초 단위 시각, 다른 표기의 기간·마감,
  라벨이 앞에 오는 기준일, 영문자·지역 이름이 낀 심의번호. 느슨하게 읽는 모든 곳에 '틀린 값은 여전히 잡힌다' 를 짝으로 둔다.
"""
from __future__ import annotations

import random
import re
import time

import pytest

from agents.pipeline import RunOptions, run_pipeline
from core.schemas import AdCopy, CampaignBrief, Claim, Fact, ProductCategory, ProductFacts
from guardrails import grounding, output_guard
from guardrails.normalize import loose_text, normalize
from guardrails.policy import get_catalog

CAT = get_catalog()

# 장식 문자·한글·숫자·공백·제로폭 — 한 종류가 이어진 긴 입력
CLASSES = {"hangul": "가", "digits": "1", "dot": ".", "dash": "-", "paren": "(", "star": "*", "dash-space": "- ", "newline": "\n", "space": " ",
           "zero-width": "\u200b", "slash": "/", "comma": ",", "tilde": "~", "quote": "'", "bar": "|", "hangul-dot": "가.", "hangul-space": "가 "}
N = 20_000
LIMIT = 3.0


def _long(unit: str, n: int = N) -> str:
    return (unit * (n // len(unit) + 1))[:n]


def _product(*facts: str, category=ProductCategory.ECOMMERCE) -> ProductFacts:
    return ProductFacts(name="p", category=category, facts=[Fact(id=f"F{i + 1}", text=t) for i, t in enumerate(facts)])


def run(facts, head, body="지금 확인", cta="확인", disc=(), claims=()):
    """grounding.check 의 [(규칙, 인용)] — 사실은 문자열 목록."""
    res = grounding.check(AdCopy(headline=head, body=body, cta=cta, disclosures=list(disc), claims=list(claims)), _product(*facts))
    return [(f.rule_id, f.quote) for f in res.findings]


def rules(*args, **kw) -> set[str]:
    return {r for r, _ in run(*args, **kw)}


# ───────────────────────── 시간: 길이가 늘어도 비용이 제곱으로 늘지 않는다 ─────────────────────────
@pytest.mark.parametrize("name", list(CLASSES))
def test_normalize_and_loose_text_are_linear_on_every_character_class(name):
    text = _long(CLASSES[name])
    t0 = time.perf_counter()
    normalize(text)
    loose_text(text)
    assert time.perf_counter() - t0 < LIMIT


@pytest.mark.parametrize("name", ["dot", "dash", "paren", "star", "dash-space", "newline", "hangul-dot", "hangul"])
def test_normalize_stays_fast_at_a_hundred_thousand_characters(name):
    text = _long(CLASSES[name], 100_000)
    t0 = time.perf_counter()
    norm = normalize(text)
    assert time.perf_counter() - t0 < LIMIT
    assert len(norm.tight.text) <= len(norm.loose.text) <= len(text)


@pytest.mark.parametrize("name", list(CLASSES))
def test_grounding_check_is_bounded_for_copy_landing_claim_and_fact_text(name):
    """헤드라인·본문·고지·랜딩·주장 글·사실 글이 모두 같은 긴 글이어도 3초 안에 끝난다 (랜딩·주장·사실 글에는 이전에 길이 상한이 없었다)."""
    text = _long(CLASSES[name])
    product = _product(text, "전 상품 10% 할인 (일부 상품 제외)")
    copy = AdCopy(headline=text, body=text, cta="확인", disclosures=[text], claims=[Claim(text=text, fact_id="F1"), Claim(text=text, fact_id="F2")])
    t0 = time.perf_counter()
    grounding.check(copy, product, landing_text=text)
    assert time.perf_counter() - t0 < LIMIT


@pytest.mark.parametrize("name", ["dot", "dash", "paren", "star", "dash-space", "hangul"])
def test_grounding_check_stays_bounded_with_a_hundred_thousand_character_landing_claim_and_fact(name):
    text = _long(CLASSES[name], 100_000)
    product = _product(text, "전 상품 10% 할인")
    copy = AdCopy(headline="전 상품 10% 할인", body="지금 확인", cta="확인", claims=[Claim(text=text, fact_id="F1")])
    t0 = time.perf_counter()
    grounding.check(copy, product, landing_text=text)
    assert time.perf_counter() - t0 < LIMIT


@pytest.mark.parametrize("name", list(CLASSES))
def test_output_guard_check_is_bounded_on_every_character_class(name):
    text = _long(CLASSES[name])
    copy = AdCopy(headline=text, body=text, cta="확인", disclosures=[text])
    t0 = time.perf_counter()
    output_guard.check(copy, _product("전 상품 10% 할인 (일부 상품 제외)"), {"ecommerce"}, text, [], CAT)
    assert time.perf_counter() - t0 < LIMIT


@pytest.mark.parametrize("name", ["dot", "dash", "paren", "star", "dash-space", "newline", "space", "zero-width", "hangul"])
def test_run_pipeline_demo_is_bounded_for_a_very_long_description_landing_and_request(name, demo_settings, tracer):
    text = _long(CLASSES[name])
    for where in ("raw_description", "landing_text", "advertiser_input"):
        brief = CampaignBrief(advertiser_input=text if where == "advertiser_input" else "가을 쿠폰 캠페인",
                              product=ProductFacts(name="OO몰 쿠폰", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (일부 상품 제외)")],
                                                   raw_description=text if where == "raw_description" else ""),
                              monthly_budget_krw=1)
        t0 = time.perf_counter()
        run_pipeline(brief, demo_settings, options=RunOptions(n_variants=1, landing_text=text if where == "landing_text" else ""), tracer=tracer)
        assert time.perf_counter() - t0 < LIMIT, where


def _words(n_chars: int, seed: int) -> str:
    rnd = random.Random(seed)
    out, total = [], 0
    while total < n_chars:
        w = "".join(chr(0xAC00 + rnd.randrange(0, 11000)) for _ in range(rnd.randint(2, 5)))
        out.append(w)
        total += len(w) + 1
    return " ".join(out)


def test_thousands_of_distinct_words_in_landing_claim_and_fact_are_bounded():
    """낱말을 서로 짝지어 비교하던 어휘 겹침(주장↔랜딩·사실)이 낱말 수의 제곱으로 느려지지 않는다."""
    product = _product(_words(N, 3))
    copy = AdCopy(headline="전 상품 10% 할인", body="지금 확인", cta="확인", claims=[Claim(text=_words(N, 2), fact_id="F1")])
    t0 = time.perf_counter()
    grounding.check(copy, product, landing_text=_words(N, 1))
    assert time.perf_counter() - t0 < LIMIT


def test_unmatched_open_parentheses_in_a_claim_do_not_make_the_parenthesis_strip_quadratic():
    text = "(" * 100_000
    t0 = time.perf_counter()
    assert grounding._strip_parens(text) == text
    assert time.perf_counter() - t0 < 1.0
    for s in ["(가나) 다라 (마바", "((가) 나)", "가(나)다(라)마", "()", ")(", "((((", "가", "(가나)(다라)"]:
        assert grounding._strip_parens(s) == re.sub(r"\([^)]*\)", " ", s), s


def test_landing_claim_and_fact_vocabulary_is_read_only_up_to_the_analysis_cap():
    """숫자 추출처럼(test_text_beyond_the_analysis_cap_is_not_scanned) 어휘 비교용 글도 분석 상한까지만 본다 — 랜딩·주장·사실 글에는 이전에 상한이 없었다."""
    cap = grounding.MAX_ANALYZED_CHARS
    assert "나다라" not in grounding._loose("가" * cap + " 나다라") and "나다라" in grounding._loose("나다라 " + "가" * cap)
    P = _product("전 상품 10% 할인")
    claim = Claim(text="업계 최저 수수료", fact_id="F1")
    copy = AdCopy(headline="전 상품 10% 할인", body="지금 확인", cta="확인", claims=[claim])
    behind_cap = "가" * grounding.MAX_ANALYZED_CHARS + " 업계 최저 수수료"
    assert any(f.rule_id == "TA-LANDING-01" for f in grounding.check(copy, P, landing_text=behind_cap).findings)
    assert not any(f.rule_id == "TA-LANDING-01" for f in grounding.check(copy, P, landing_text="업계 최저 수수료 " + "가" * cap).findings)


def test_a_fact_text_is_read_only_up_to_the_analysis_cap_for_conditions_too():
    """사실 글의 조건도 숫자처럼 분석 상한까지만 읽는다 — 상한 밖에 쓴 '10월 31일까지' 는 조건이 아니다 (사실 글에는 이전에 상한이 없었다)."""
    cap = grounding.MAX_ANALYZED_CHARS
    claim = [Claim(text="전 상품 할인", fact_id="F1")]
    inside = run(["10월 31일까지 " + "가" * cap + " 전 상품 할인"], "전 상품 할인", claims=claim)
    beyond = run(["전 상품 할인 " + "가" * cap + " 10월 31일까지"], "전 상품 할인", claims=claim)
    assert any(r == "GRND-COND-01" for r, _ in inside) and not any(r == "GRND-COND-01" for r, _ in beyond)
    # 다름(COND-02) 쪽도 같다: 상한 안의 마감과 다른 날짜를 쓰면 다름, 상한 밖에 쓴 마감과는 비교하지 않는다
    assert "GRND-COND-02" in rules(["10월 31일까지 " + "가" * cap + " 전 상품 할인"], "전 상품 할인", "11월 30일까지", claims=claim)
    assert "GRND-COND-02" not in rules(["전 상품 할인 " + "가" * cap + " 10월 31일까지"], "전 상품 할인", "11월 30일까지", claims=claim)


def test_word_overlap_matches_the_pairwise_definition():
    """_Words.overlaps 는 '낱말 중 하나가 t 의 일부이거나 t 가 낱말 중 하나의 일부' 라는 원래 정의와 같은 답을 낸다."""
    rnd = random.Random(5)
    alphabet = "가나다라마바사"
    for _ in range(300):
        text = " ".join("".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 5))) for _ in range(rnd.randint(1, 6)))
        words = grounding._Words(text)
        for _ in range(8):
            t = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(2, 6)))
            expect = t in text or any(t in w or w in t for w in set(grounding._TOKEN.findall(text)))
            assert words.overlaps(t) == expect, (text, t)
    # 길이 상한(_MAX_WORD)을 넘는 긴 덩어리가 낱말 집합에 있을 때도 같은 답
    long_word = "가나다" * 30
    words = grounding._Words(f"짧은 {long_word} 다른")
    for t, expect in ((long_word + "라마", True), ("라마바" * 40, False), ("가나다" * 31, True), ("마바", False), ("짧은말", True)):
        assert words.overlaps(t) is expect, t


def test_tight_surface_matches_the_per_character_rule_and_keeps_positions():
    """덩어리 단위 판단은 '글자마다 양쪽 첫 비장식 글자를 찾는' 원래 규칙과 같은 tight 표면·위치를 만든다."""
    decor = set(" \t\r\n.·•・∙‧,|/\\-_*~^'\"`“”‘’()[]{}<>!?:;")
    rnd = random.Random(9)
    alphabet = list("원금보장가나다 .-*(\n\t,/~'!?:;_|·") + ["  ", "- "]
    for _ in range(3000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30)))
        norm = normalize(s)
        chars, srcs = list(norm.loose.text), norm.loose.src
        want_c, want_s = [], []
        for i, c in enumerate(chars):
            if c in decor:
                j = i - 1
                while j >= 0 and chars[j] in decor:
                    j -= 1
                k = i + 1
                while k < len(chars) and chars[k] in decor:
                    k += 1
                if j >= 0 and k < len(chars) and re.match(r"[가-힣]", chars[j]) and re.match(r"[가-힣]", chars[k]):
                    continue
            want_c.append(c)
            want_s.append(srcs[i])
        assert (norm.tight.text, norm.tight.src) == ("".join(want_c), want_s), repr(s)


# ───────────────────────── 6. 부정: 조사가 낀 '되지는 않아요' · 한다체 '안 한다' ─────────────────────────
AUTOPAY = "7일 무료 체험 후 월 19,900원 자동결제"
AUTOPAY_DISC = "체험 종료 후 월 19,900원 자동결제"
EXCLUDE = "전 상품 10% 할인 (일부 상품 제외)"


@pytest.mark.parametrize("phrase", [
    "자동결제 되지는 않아요", "자동결제 되진 않아요", "자동결제 되지도 않아요", "자동결제 하지는 않아요", "자동결제가 되지는 않습니다", "자동결제 안 된다", "자동결제 안 한다", "자동결제는 안 된답니다",
    "자동결제 되질 않아요", "자동결제를 하지도 않습니다", "자동결제 하진 않습니다", "자동결제 안 됐어요", "자동결제 안 했어요", "자동결제 안 한대요", "자동결제 전혀 되지는 않아요", "자동결제 자체가 없어요",
    "자동결제 되는 일 없어요", "자동결제 되지 않아요", "자동결제 안돼", "자동결제 진행되지 않아요", "자동결제가 진행되지는 않습니다", "자동결제 청구되지 않아요", "자동결제 적용되지 않아요", "자동결제 진행 안 돼요"])
def test_autopay_negations_with_particles_and_plain_endings_are_negations(phrase):
    assert "GRND-COND-02" in rules([AUTOPAY], "7일 무료 체험", f"월 19,900원, {phrase}"), phrase


@pytest.mark.parametrize("phrase", ["제외 되지는 않아요", "제외 품목 하나 없이", "제외 대상이 하나 없어요", "제외 되진 않습니다", "제외 상품 안 된다", "제외 되지도 않아요", "제외 안 한다", "제외 품목 하나도 없이"])
def test_exclusion_negations_with_particles_and_plain_endings_are_negations(phrase):
    assert "GRND-COND-02" in rules([EXCLUDE], "전 상품 10% 할인", phrase), phrase


@pytest.mark.parametrize("phrase", ["심사 안 한다", "심사 하지는 않아요", "심사 하진 않습니다", "심사 되지는 않아요"])
def test_review_negations_with_particles_and_plain_endings_are_negations(phrase):
    assert "GRND-COND-02" in rules(["대출 심사 결과에 따라 한도 결정"], "대출 한도 결정", phrase), phrase


@pytest.mark.parametrize("phrase", ["자동결제는 됩니다", "자동결제 됩니다", "자동결제가 되는 상품입니다", "자동결제 안 한 달 요금은 별도", "자동결제 안 한 고객은 직접 결제"])
def test_affirmative_or_non_final_phrases_next_to_the_marker_are_not_negations(phrase):
    """긍정 표현과, 문장을 끝맺지 않는 '안 한'(관형형)은 부정으로 읽지 않는다 — 읽으면 정상 문구가 '반대' 로 막힌다."""
    assert "GRND-COND-02" not in rules([AUTOPAY], "7일 무료 체험", f"체험 후 월 19,900원 {phrase}"), phrase


def test_a_fact_that_denies_with_the_same_wording_can_be_copied():
    assert not run(["전 상품 10% 할인 (제외 품목 하나 없이 적용)"], "전 상품 10% 할인", "제외 품목 하나 없이 적용")
    assert not run(["전 상품 10% 할인 (제외 되지는 않아요)"], "전 상품 10% 할인", "제외 되지는 않아요")


# ───────────────────────── 7. 한정 없는 부정은 '체험' 이라는 이름만으로 면제되지 않는다 ─────────────────────────
@pytest.mark.parametrize("head,body", [
    ("7일 무료 체험, 자동결제 없음!", "지금 시작"), ("7일 무료 체험 · 자동결제 없이 시작", "지금 시작"), ("7일 무료 체험. 자동결제 없음", "지금 시작"),
    ("7일 무료 체험! 자동결제 없음!", "지금 시작"), ("체험 신청하세요. 자동결제 절대 없어요", "지금 시작"), ("이벤트 기간 자동결제 없음", "지금 시작"),
    ("체험 기간, 자동결제 없음", "지금 시작"), ("무료 체험", "자동결제 없음")])
def test_an_unscoped_autopay_denial_is_a_conflict_even_beside_the_word_trial(head, body):
    assert "GRND-COND-02" in rules([AUTOPAY], head, body, disc=[AUTOPAY_DISC]), (head, body)


@pytest.mark.parametrize("scoped", [
    "체험 기간 중 자동결제 없음", "체험 기간 중에는 자동결제 없음", "체험 기간에는 과금 없음", "체험 기간엔 과금 없음", "무료 체험 동안 자동결제 되지 않아요", "체험 중 자동결제 없음", "체험중 자동결제 없음",
    "체험 기간 내 자동결제 없음", "처음 7일은 자동결제 없음", "첫 달 자동결제 없음"])
def test_a_denial_scoped_to_the_trial_stays_exempt(scoped):
    assert not rules([AUTOPAY], "7일 무료 체험", scoped, disc=[AUTOPAY_DISC]), scoped


def test_a_scope_word_in_the_previous_sentence_or_line_does_not_scope_the_next_denial():
    assert "GRND-COND-02" in rules([AUTOPAY], "체험 기간 중 무료", "자동결제 없음", disc=[AUTOPAY_DISC])   # 필드(줄)가 다르다
    assert "GRND-COND-02" in rules([AUTOPAY], "7일 체험 중. 자동결제 없음", disc=[AUTOPAY_DISC])


@pytest.mark.parametrize("boundary", [". ", "! ", "? ", " · ", "; ", "\n"])
def test_every_sentence_boundary_stops_a_scope_word_from_reaching_the_next_denial(boundary):
    assert "GRND-COND-02" in rules([AUTOPAY], "7일 무료 체험", f"체험 기간 중{boundary}자동결제 없음", disc=[AUTOPAY_DISC]), repr(boundary)


def test_a_comma_or_a_decimal_point_does_not_end_the_scope():
    assert not rules([AUTOPAY], "7일 무료 체험", "체험 기간 중, 자동결제 없음", disc=[AUTOPAY_DISC])
    assert "GRND-COND-02" not in rules([AUTOPAY], "7일 무료 체험", "처음 3.5일 자동결제 없음", disc=[AUTOPAY_DISC])    # 3.5일 자체는 사실에 없는 기간이라 NUM-01 은 따로 난다


# ───────────────────────── 8. 조 단위 금액 ─────────────────────────
@pytest.mark.parametrize("text,raw,value", [
    ("10조원", "10조원", 10e12), ("1조 5천억원", "1조 5천억원", 1.5e12), ("5조 원", "5조 원", 5e12), ("1조5천억원", "1조5천억원", 1.5e12), ("3조 2,000억원", "3조 2,000억원", 3.2e12),
    ("10.5조원", "10.5조원", 10.5e12), ("10조원대", "10조원", 10e12), ("2조 3천만원", "2조 3천만원", 2e12 + 3e7), ("누적 거래액 10조원 돌파", "10조원", 10e12)])
def test_trillion_won_amounts_are_one_value(text, raw, value):
    (q,) = grounding.extract_quantities(text)
    assert (q.raw, q.value, q.cls) == (raw, value, "krw")


@pytest.mark.parametrize("text", ["2조건 충족", "3조회", "10조합", "100조항", "조건 없음", "제5조", "제 5조 2항", "약관 제5조의2", "조회 5회"])
def test_the_character_jo_inside_words_and_clause_numbers_is_not_a_unit(text):
    assert not [q for q in grounding.extract_quantities(text) if q.value >= 1e12], text


def test_jo_is_a_big_place_unit_like_man_and_eok():
    assert grounding._split_run("조") == (1.0, 1e12) and grounding._split_run("천조") == (1000.0, 1e12) and grounding._split_run("억") == (1.0, 1e8)


def test_trillion_amounts_are_compared_by_value():
    assert "GRND-NUM-01" in rules(["누적 거래액 1,000억원 돌파"], "누적 거래액 10조원 돌파")                  # 100배
    assert "GRND-NUM-01" in rules(["누적 거래액 1조원 돌파"], "누적 거래액 5조원 돌파")
    assert "GRND-NUM-01" in rules(["운용자산 5,000억원"], "운용자산 5조 원")
    assert "GRND-NUM-01" in rules(["누적 거래액 5천억원"], "누적 거래액 1조 5천억원")                          # 앞의 1조가 버려지던 것
    assert "GRND-NUM-01" in rules(["누적 거래액 1조 5천억원"], "누적 거래액 1조 4천억원")
    assert not run(["누적 거래액 1조 5천억원"], "누적 거래액 15,000억원") and not run(["누적 거래액 15,000억원"], "누적 거래액 1조 5천억원")   # 같은 금액
    assert not run(["누적 거래액 10조원 돌파"], "누적 거래액 10조 원 돌파") and not run(["누적 거래액 1조 5천억원"], "누적 거래액 1.5조원")


# ───────────────────────── 9. 전화번호 꼴 하이픈 범위 + 단위 ─────────────────────────
@pytest.mark.parametrize("unit", [*grounding._UNIT_NAMES, "%"])      # 추출기가 아는 단위 전부
def test_a_hyphen_range_followed_by_any_known_unit_is_a_benefit_range_not_a_phone_number(unit):
    qs = grounding.extract_quantities(f"1500-3000{unit}")
    assert [q.value for q in qs] == [1500.0, 3000.0], unit
    assert qs[0].cls == qs[1].cls != "bare"


def test_point_ranges_are_compared_and_wrong_ones_flagged():
    assert "3000포인트" in {q for _, q in run(["월 1000-1500포인트 적립"], "월 1500-3000포인트 적립")}
    assert "GRND-NUM-01" in rules(["월 1000-1500포인트 적립"], "월 1500~3000포인트 적립")                       # 물결표는 원래 읽던 것
    assert not run(["월 1000-1500포인트 적립"], "월 1000-1500포인트 적립") and not run(["월 1000~1500포인트 적립"], "월 1000-1500포인트 적립")
    assert "GRND-NUM-01" in rules(["월 1000-1500p 적립"], "월 1500-3000p 적립") and "GRND-NUM-01" in rules(["월 1000-1500점 적립"], "월 1500-3000점 적립")


@pytest.mark.parametrize("phone", ["1588-1234", "02-123-4567", "010-1234-5678", "1566-0000", "031) 123-4567", "고객센터 1588-1234 (24시간)", "문의 1588-1234 평일 9시", "고객센터 1588-1234 일요일 휴무", "문의 1588-1234 일반 문의",
                                   "문의 02-123-4567 분실 신고"])
def test_real_phone_numbers_stay_masked(phone):
    assert not [q for q in grounding.extract_quantities(phone) if q.cls != "duration"], phone       # 번호 숫자는 읽지 않는다 ('24시간' 같은 진짜 기간만 남을 수 있다)


# ───────────────────────── 11. 초 단위까지 쓴 시각 ─────────────────────────
@pytest.mark.parametrize("body", ["10월 31일 23시 59분 59초까지", "10월 31일 금요일 23시 59분 59초까지", "10월 31일 오후 11시 59분 59초까지", "10월 31일 23시 59분 59초 종료", "10월 31일 23시59분59초까지",
                                  "10월 31일(금) 23시 59분 59초까지", "2026.10.31 23시 59분 59초까지", "10월 31일 24시 00분 00초까지", "10월 31일 오전 9시 30분 15초까지"])
def test_clock_times_with_seconds_are_a_time_of_day_not_a_duration(body):
    assert not run(["10월 31일까지 10% 할인"], "10% 할인", body), body
    assert not [q for q in grounding.extract_quantities(body) if q.cls == "duration"], body


def test_a_wrong_date_is_still_flagged_with_seconds_and_a_real_duration_in_seconds_is_still_read():
    assert "GRND-COND-02" in rules(["10월 31일까지 10% 할인"], "10% 할인", "11월 30일 23시 59분 59초까지")
    assert "GRND-COND-02" in rules(["10월 31일까지 10% 할인"], "10% 할인", "10월 30일 23시 59분 59초까지")
    assert [q.raw for q in grounding.extract_quantities("59초 만에 가입")] == ["59초"]


# ───────────────────────── 12. 다른 표기의 기간 범위 ─────────────────────────
PERIOD_FACT = ["10월 1일부터 10월 31일까지 10% 할인"]


@pytest.mark.parametrize("body", [
    "10월 1일부터 31일까지", "10월 1일(수) ~ 10월 31일(금)", "2026.10.01 ~ 2026.10.31", "10.1 ~ 10.31", "10/1~10/31", "10월 1일(수)부터 31일(금)까지", "2026-10-01 ~ 2026-10-31", "2026/10/01~2026/10/31",
    "10/1(수)~10/31(금)", "10.1(수) ~ 10.31(금)", "2026.10.01(수) ~ 2026.10.31(금)", "10/1 (수) ~ 10/31 (금)", "10월 1일(수)~10월 31일(금)",
    "2026년 10월 1일 ~ 2026년 10월 31일", "10. 1. ~ 10. 31.", "10.01~10.31", "10월 1일 수요일 ~ 10월 31일 금요일", "10월 1일 ~ 10월 31일", "10월 1일부터 10월 31일까지"])
def test_a_period_range_in_any_notation_with_the_same_end_date_satisfies_a_from_to_fact(body):
    assert not run(PERIOD_FACT, "10% 할인", body), body


@pytest.mark.parametrize("body", [
    "10월 1일부터 30일까지", "10월 1일(수) ~ 11월 30일(금)", "2026.10.01 ~ 2026.11.30", "10.1 ~ 11.30", "10/1~11/30", "10월 1일부터 11월 30일까지", "10.1 ~ 10.30", "10/1~10/30", "2026-10-01 ~ 2026-11-30",
    "10월 1일(수)부터 30일(목)까지", "10/1(수)~11/30(금)", "10.1(수) ~ 11.30(금)"])
def test_a_period_range_whose_end_date_differs_is_still_flagged(body):
    assert "GRND-COND-02" in rules(PERIOD_FACT, "10% 할인", body), body


def test_a_month_day_with_a_trailing_dot_before_the_marker_is_a_date_for_the_number_check_too():
    for text in ("10. 31.까지", "10.31.까지"):
        assert [(q.cls, q.value) for q in grounding.extract_quantities(text)] == [("date", 1031.0)], text
    assert [(q.cls, q.value) for q in grounding.extract_quantities("10.31 좋아요")] == [("bare", 10.31)]      # 마감 표식이 없으면 소수


def test_a_weekday_after_the_end_day_of_a_day_only_range_is_read():
    """'10월 1일(수) ~ 31일(금)' — 끝에 월이 없는 기간은 PERIOD_RANGE 가 읽고, 요일 괄호가 붙어도 마감은 31일"""
    assert not run(PERIOD_FACT, "10% 할인", "10월 1일(수) ~ 31일(금)")
    assert "GRND-COND-02" in rules(PERIOD_FACT, "10% 할인", "10월 1일(수) ~ 30일(목)")


def test_only_real_date_ranges_are_periods():
    ends = grounding._numeric_period_ends
    assert ends("10.31 ~ 10.1") == [] and ends("12.25 ~ 3.15") == []                                  # 거꾸로 된 점 표기는 기간이 아니다
    assert ends("3.15 ~ 4.20배") == [] and ends("3.15 ~ 4.20명") == [] and ends("3.15 ~ 4.20원") == []   # 단위가 붙은 소수 범위
    assert ends("2026.13.01 ~ 2026.13.31") == [] and ends("0.15 ~ 10.31") == [] and ends("10/32 ~ 11/40") == []   # 없는 월·일
    assert [e for _, _, e in ends("10.1 ~ 10.31")] == [(0, 10, 31)] and [e for _, _, e in ends("2026.10.01 ~ 2026.10.31")] == [(2026, 10, 31)]


def test_a_tilde_after_a_number_is_a_range_not_a_leading_tilde():
    """'5~11/30' 의 물결표는 앞 숫자와 이어진 범위라 '11/30까지' 가 아니다 — 맨 앞 물결표만 마감으로 읽는다"""
    assert grounding._c_deadline("최대 5~11/30", "최대 5~11/30", False) == [] and grounding._c_deadline("할인 3%~11/30", "할인 3%~11/30", False) == []
    assert [c.value for c in grounding._c_deadline("할인 ~11/30", "할인 ~11/30", False)] == [("date", 1130, 0)]


def test_from_N_days_stays_a_duration_and_decimal_ranges_are_not_periods():
    fact = ["이벤트 기간: 10월 1일부터 7일간, 10% 할인"]
    assert not run(fact, "10% 할인", "10월 1일부터 7일간") and not run(fact, "10% 할인", "7일간 진행")
    assert "GRND-NUM-01" in rules(fact, "10% 할인", "10월 1일부터 8일간")
    assert [q.raw for q in grounding.extract_quantities("10월 1일부터 7일간") if q.cls == "duration"] == ["7일"]
    for rate in ("연 3.5~4.5% 적립", "금리 1.5 ~ 12.5 배", "배송 3.5~5.5일"):
        assert not grounding._numeric_period_ends(rate), rate
    # 소수 범위가 날짜 마감으로 읽혀 사실의 마감과 충돌하지 않는다
    assert "GRND-COND-02" not in rules(PERIOD_FACT, "10% 할인", "10월 31일까지, 금리 4.5 ~ 12.5 배")


def test_a_from_to_deadline_on_the_fact_side_is_binding_in_the_new_form_too():
    fact = ["10월 1일부터 31일까지 10% 할인"]
    assert "GRND-COND-01" in rules(fact, "10% 할인", "지금 확인")
    assert not run(fact, "10% 할인", "10월 31일까지") and "GRND-COND-02" in rules(fact, "10% 할인", "10월 30일까지")


# ───────────────────────── 13. 마침표·두 자리 연도·맨 앞 물결표·라벨로 쓴 마감 ─────────────────────────
@pytest.mark.parametrize("body", ["2026. 10. 31.까지", "2026.10.31.까지", "26.10.31까지", "~2026.10.31", "~ 2026.10.31", "2026. 10. 31. 까지", "10. 31.까지", "26.10.31 마감", "유효기간: 2026.10.31",
                                  "유효기간 2026.10.31", "사용기한: 2026.10.31", "2026.10.31까지", "10/31(금)까지"])
def test_deadline_notations_with_trailing_dot_short_year_leading_tilde_or_label_are_the_deadline(body):
    assert not run(["2026.10.31까지 10% 할인"], "10% 할인", body), body


@pytest.mark.parametrize("body", ["2026. 11. 30.까지", "2026.11.30.까지", "26.11.30까지", "~2026.11.30", "유효기간: 2026.11.30", "26.11.30 마감", "~11/30", "2027.10.31.까지", "사용기한: 2026.12.31"])
def test_wrong_dates_in_those_notations_are_still_flagged(body):
    assert "GRND-COND-02" in rules(["2026.10.31까지 10% 할인"], "10% 할인", body), body


def test_label_and_tilde_forms_against_month_day_facts():
    assert not run(["10월 31일까지 10% 할인"], "10% 할인", "~10/31") and not run(["10월 31일까지 10% 할인"], "10% 할인", "~ 10월 31일")
    assert "GRND-COND-02" in rules(["10월 31일까지 10% 할인"], "10% 할인", "~11/30")
    assert not run(["쿠폰 10월 31일까지 사용 가능, 10% 할인"], "10% 할인", "유효기간: 2026.10.31")
    assert "GRND-COND-02" in rules(["쿠폰 10월 31일까지 사용 가능, 10% 할인"], "10% 할인", "유효기간: 2026.11.30")


def test_versions_decimals_and_range_starts_are_not_read_as_deadlines():
    f = ["2026.10.31까지 10% 할인"]
    for body in ("버전 1.2.3", "v26.10.31 업데이트", "연 4.5% ~ 12.5%", "금리 ~4.5%", "~4.5"):
        assert "GRND-COND-02" not in rules(f, "10% 할인", body), body       # 마감으로 읽어 '다름' 이라고 하지 않는다 (빠짐은 별개)
    # '유효기간 10월 1일부터 …' 의 10월 1일은 시작일이다
    assert "GRND-COND-02" not in rules(["쿠폰 10월 31일까지 사용 가능, 10% 할인"], "10% 할인", "유효기간 10월 1일부터 10월 31일")
    assert [c.value for c in grounding._c_deadline("유효기간 2026.10.01부터", "유효기간 2026.10.01부터", False)] == []


# ───────────────────────── 14. 라벨이 앞에 오는 기준일 ─────────────────────────
@pytest.mark.parametrize("ad", ["연 4.5% (기준일 2026.10.1)", "연 4.5% (기준: 2026.10.1)", "연 4.5% (기준일: 2026.10.1.)", "연 4.5% (2026년 10월 1일자 기준)", "연 4.5% (기준일 2026년 10월 1일)", "연 4.5% (기준 2026.10.1)",
                                "연 4.5% (기준일자 2026.10.01)", "연 4.5% (기준일: 2026-10-01)", "연 4.5% (기준일 10월 1일)", "연 4.5% 금리 기준일 2026.10.1", "연 4.5% (2026.10.1자 기준)", "연 4.5% (조회일 2026.10.1)",
                                "연 4.5% (2026.10.1 기준)"])
def test_a_reference_date_with_the_label_first_is_not_a_promised_date(ad):
    assert not run(["연 4.5% (2026.10.1 기준)"], "신용 혜택", ad), ad


def test_reference_date_masking_does_not_hide_rates_or_real_deadlines():
    f = ["연 4.5% (2026.10.1 기준)"]
    assert [q for _, q in run(f, "신용 혜택", "연 5.5% (기준일 2026.10.1)")] == ["5.5%"]                       # 날짜만 지우고 금리는 그대로 비교
    assert "10월 31일" in {q for _, q in run(f, "신용 혜택", "기준 10월 31일까지 주문 시 연 4.5%")}                # 기준 뒤에 까지가 붙으면 약속하는 날짜
    assert not run(["최근 1년 성과 5.2% (기준일 2026.09.30)"], "최근 1년 성과 5.2%", "(2026.9.30 기준)")


def test_a_label_followed_by_a_deadline_marker_is_a_promise_not_a_reference_date():
    """'기준일 10월 31일까지' · '기준 2026.10.31까지' 는 기준일이 아니라 약속하는 마감 — 날짜를 지우지 않는다. 긴 날짜의 앞부분만 지우는 일도 없다."""
    for text, want in (("기준일 10월 31일까지 신청", 1031.0), ("기준: 10월 31일까지 신청", 1031.0), ("기준 2026.10.31까지 신청", 1031.0), ("기준일 2026.10.11까지 신청", 1011.0), ("기준일 2026.10.11 이후 신청", 1011.0)):
        assert [q.value for q in grounding.extract_quantities(text) if q.cls == "date"] == [want], text
    assert not [q for q in grounding.extract_quantities("기준일 2026.10.11") if q.cls == "date"]            # 표식이 없으면 기준일이라 지운다
    assert not [q for q in grounding.extract_quantities("기준일 2026.10.11 현재 연 4.5%") if q.cls == "date"]


def test_a_fact_side_period_in_numeric_notation_is_not_binding():
    """사실에 숫자 표기로 적힌 기간(마감 표식 없음)은 광고가 옮겨 적어야 할 조건이 아니다 — 광고 쪽에서만 끝날을 마감으로 읽는다 (README 한계와 같은 규칙)."""
    for fact in ("이벤트 기간 2026.10.01 ~ 2026.10.31, 10% 할인", "이벤트 기간 10/1~10/31, 10% 할인", "이벤트 기간 10.1 ~ 10.31, 10% 할인", "유효기간: 2026.10.31, 10% 할인", "~2026.10.31 10% 할인"):
        assert "GRND-COND-01" not in rules([fact], "10% 할인", "지금 확인"), fact


# ───────────────────────── 15. 영문자·지역 이름이 낀 심의번호·신고번호 ─────────────────────────
@pytest.mark.parametrize("disclosure", [
    "심의번호: 2026-01234", "광고심의필 2026-A-0123", "통신판매업신고 제2026-서울강남-01234호", "통신판매업 신고번호: 2026-서울강남-01234", "심의필 2026-0123", "심의번호 2026-C1-01234", "여신금융협회 심의필 제2026-C1-01234호",
    "심의필: 제2026-서울강남-01234호", "광고 심의번호 20260123", "통신판매업신고번호 2026-서울강남-01234", "대부업 등록번호 제2026-서울강남-01234호", "심의필번호 2026-0123", "심의필 제2026-0123호", "사업자등록번호 123-45-67890",
    "준법감시인 심의필 제2026-0123호 (2026.10.01~2027.09.30)", "심의필 2026-0123 (2026.10.01~2027.09.30)"])
def test_review_and_registration_numbers_are_not_benefit_numbers(disclosure):
    assert not run(["전 상품 10% 할인"], "전 상품 10% 할인", disc=[disclosure]), disclosure


@pytest.mark.parametrize("disclosure,flagged", [
    ("심의필 제2026-0123호, 최대 20% 할인", "20%"), ("심의번호: 2026-01234 연 20% 할인", "20%"), ("광고심의필 2026-A-0123, 20% 할인", "20%"), ("통신판매업 신고번호: 2026-서울강남-01234 최대 20% 할인", "20%"),
    ("심의필 2026-0123 20% 할인", "20%"), ("심의번호 2026-01234 5,000원 할인", "5,000원"), ("심의필 15% 할인", "15%"), ("심의 20% 할인", "20%"), ("사업자등록번호 123-45-67890 20% 할인", "20%")])
def test_a_real_benefit_number_next_to_such_a_label_is_still_compared(disclosure, flagged):
    assert [q for _, q in run(["전 상품 10% 할인"], "전 상품 10% 할인", disc=[disclosure])] == [flagged], disclosure


def test_a_hyphen_range_with_a_unit_right_after_a_label_is_a_benefit_range_not_an_id():
    assert [q.value for q in grounding.extract_quantities("심의 15-20% 할인")] == [15.0, 20.0]


def test_identifier_masking_is_not_widened_to_unlabelled_numbers():
    """라벨 없는 '2026-01234' 는 번호라고 알 수 없으므로 여전히 숫자로 읽는다."""
    assert grounding.extract_quantities("관리 2026-01234")


# ───────────────────────── 달력 요일 괄호: 정규화가 '일(수)' 를 '일수' 로 붙이지 않는다 ─────────────────────────
def test_a_weekday_in_parentheses_after_a_date_is_not_joined_into_the_policy_word_ilsu():
    for text in ("10월 1일(수) ~ 10월 31일(금)", "10월 5일 (수) 시작", "1일(수)부터"):
        assert "일수" not in normalize(text).tight.text, text
    for evasion in ("일(수)대출", "원(금)보장", "일.수", "일 수"):
        assert normalize(evasion).tight.text == evasion.replace("(", "").replace(")", "").replace(".", "").replace(" ", ""), evasion
    det = output_guard.check(AdCopy(headline="10% 할인", body="10월 1일(수) ~ 10월 31일(금)", cta="자세히 보기"), _product(*PERIOD_FACT), {"ecommerce"}, "", [], CAT)
    assert det.decision.value == "APPROVE" and not det.findings


# ───────────────────────── 느슨해진 곳이 새로 만든 입력 비용 ─────────────────────────
@pytest.mark.parametrize("unit", ["2026.10.01 ~ ", "10.1 ~ 10.31 ", "~2026.10.31 ", "유효기간 ", "기준일 ", "기준 2026.10.1 ", "심의번호: 20260123 ", "통신판매업 신고번호: 2026-서울강남-", "제 5조 ", "1조 5천", "1조 ", "23시 59분 59초",
                                  "10월 1일(수)부터 ", "1500-3000포인트 ", "26.10.31까지 ", "2026. 10. 31.", "되지는 ", "자동결제 안 ", "제외 하나 ", "체험 기간 "])
def test_new_readings_do_not_make_long_repeated_text_slow(unit):
    text = _long(unit)
    product = _product("전 상품 10% 할인 (10월 31일까지, 1인 1회, 일부 상품 제외)", "누적 1조 5천억원 (기준일 2026.10.1)")
    t0 = time.perf_counter()
    grounding.check(AdCopy(headline=text, body=text, cta="확인", disclosures=[text]), product, landing_text=text)
    assert time.perf_counter() - t0 < LIMIT
