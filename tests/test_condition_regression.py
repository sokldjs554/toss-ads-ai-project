"""숫자·조건 비교 회귀 — 리뷰가 재현한 세 가지(A 구매 금액 변경 · B 자동결제 반전 · C 붙여 쓴 할인율)와 같은 종류의 틀린 문구,
그리고 조건을 사실 그대로 쓴 정상 대조 문구가 모두 같은 코드 검사 경로에서 기대한 대로 판정되는지 본다.

수정 전 코드는 이 사례 39건 중 14건에서 라벨과 다른 판정(또는 다른 이유)을 냈다 — evals/datasets/conditions.jsonl 머리말 참조.
정상 대조가 함께 있는 이유: 틀린 문구를 잡는 것만큼 '사실 그대로 쓴 문구를 막지 않는 것' 이 중요하기 때문이다.
"""
from __future__ import annotations

import pytest

from agents.recheck import recheck
from agents.rewrite_check import check_rewrite
from core.config import Settings
from core.schemas import AdCopy, Claim, Decision, Fact, ProductCategory, ProductFacts
from evals.runner import load_dataset
from guardrails import grounding, output_guard
from guardrails.policy import get_catalog
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider, scripted_review_provider

CAT = get_catalog()
CASES = load_dataset("conditions") + load_dataset("conditions_extra")   # 승인받은 비교 범위의 39건 + 내부 재점검(세 번)에서 늘린 144건
SETTINGS = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="", force_demo=False)


def det_of(case):
    copy, product = case.to_copy(), case.to_product()
    cats = CAT.effective_categories(product.category, copy.full_text())
    return output_guard.check(copy, product, cats, case.landing_text, [], CAT)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_dataset_case_gets_the_labelled_decision_for_the_labelled_reason(case):
    det = det_of(case)
    rules = {f.rule_id for f in det.findings}
    assert det.decision.value == case.label, (case.notes, [(f.rule_id, f.quote) for f in det.findings])
    for r in case.rule_ids:
        assert r in rules, (case.notes, rules)
    if case.label == "APPROVE":
        assert not rules, f"정상 대조 문구가 막혔다: {[(f.rule_id, f.quote, f.explanation) for f in det.findings]}"


def test_dataset_has_wrong_and_normal_cases_and_review_repros():
    labels = [c.label for c in CASES]
    assert labels.count("REVISE") >= 50 and labels.count("APPROVE") >= 50
    assert {c.id for c in CASES if c.source == "review_repro"} == {"K-A1", "K-B1", "K-C1", "K-C2"}      # 외부 리뷰가 보고한 입력 3건 + 그 띄어 쓴 형태
    assert len(load_dataset("conditions")) == 39 and len(load_dataset("conditions_extra")) == 144         # 실제 모델 비교 범위(110건)는 39건까지
    assert sum(1 for c in CASES if not c.claims) >= 100  # claims 가 비어 있는 직접 편집·수정안 경로가 대부분이다


# ───────────── 리뷰 재현 A · B · C 를 한 줄로 ─────────────
P_A = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인, 50,000원 이상 구매 시")])
P_B = ProductFacts(name="p", category=ProductCategory.APP, facts=[Fact(id="F1", text="7일 무료 체험 후 월 19,900원 자동결제")])
P_C = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="배송까지 15일 소요")])


def g(product, headline, body, disclosures=(), claims=()):
    return grounding.check(AdCopy(headline=headline, body=body, cta="자세히 보기", disclosures=list(disclosures), claims=list(claims)), product)


def rules(res):
    return {f.rule_id for f in res.findings}


def test_repro_A_amount_in_disclosure_is_compared_and_equivalent_forms_pass():
    assert "GRND-COND-02" in rules(g(P_A, "전 상품 10% 할인", "지금 구매", ["5,000원 이상 구매 시"]))
    for ok in ("50,000원 이상 구매 시", "5만원 이상 구매 시", "5만 원 이상 구매 시", "50000원 이상 구매 시"):
        assert not g(P_A, "전 상품 10% 할인", "지금 구매", [ok]).findings, ok


def test_repro_B_negated_keyword_does_not_satisfy_the_condition():
    res = g(P_B, "7일 무료 체험", "자동결제 없음")
    assert "GRND-COND-02" in rules(res) and "GRND-COND-01" not in rules(res)  # 같은 문제를 빠짐과 다름으로 두 번 말하지 않는다
    for denied in ("자동결제 없이 시작", "자동결제가 되지 않아요", "자동결제는 없습니다", "자동결제 걱정 없어요", "자동결제 아님"):
        assert "GRND-COND-02" in rules(g(P_B, "7일 무료 체험", denied)), denied
    assert not g(P_B, "7일 무료 체험", "체험 후 월 19,900원 자동결제").findings


def test_repro_C_attached_number_is_checked():
    for head in ("할인5%", "5% 할인", "할인 5%", "최대할인5%"):
        r = g(P_C, head, "지금 확인")
        assert "5%" in r.numbers_unsupported, head
    assert not g(P_C, "배송15일", "지금 확인").numbers_unsupported


def test_the_old_keyword_only_pass_is_gone():
    """조건 단어가 있다는 이유만으로 충족으로 보지 않는다: '이상', '까지', '1인' 이 있어도 값이 다르면 충족이 아니다."""
    fact = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (30,000원 이상, 10월 31일까지, 1인 1회)")])
    assert "GRND-COND-02" in rules(g(fact, "전 상품 10% 할인", "3,000원 이상, 10월 31일까지, 1인 1회"))
    assert "GRND-COND-02" in rules(g(fact, "전 상품 10% 할인", "30,000원 이상, 11월 30일까지, 1인 1회"))
    assert "GRND-COND-02" in rules(g(fact, "전 상품 10% 할인", "30,000원 이상, 10월 31일까지, 1인 5회"))
    assert not g(fact, "전 상품 10% 할인", "3만원 이상, 10월 31일까지, 1인당 1회").findings


# ───────────── 고지 문구·주기·식별번호 ─────────────
def test_disclosure_numbers_are_checked_against_facts():
    r = g(P_A, "전 상품 10% 할인", "지금 구매", ["50,000원 이상 구매 시", "최대 20% 추가 할인"])
    assert "20%" in r.numbers_unsupported
    assert "GRND-COND-02" not in rules(r)


def test_cycle_is_compared_only_when_both_sides_state_it():
    assert "19,900원" in g(P_B, "7일 무료 체험", "체험 후 연 19,900원 자동결제").numbers_unsupported
    assert not g(P_B, "7일 무료 체험", "체험 후 19,900원 자동결제").numbers_unsupported           # 광고가 주기를 말하지 않음 — 코드는 판단하지 않는다
    assert not g(P_B, "7일 무료 체험", "체험 후 월 19,900원 자동결제").numbers_unsupported
    assert not g(P_B, "7일 무료 체험", "체험 후 19,900원/월 자동결제").numbers_unsupported
    assert [x.cycle for x in grounding.extract_quantities("월 19,900원")] == ["월"]
    assert [x.cycle for x in grounding.extract_quantities("연 4.5%")] == ["연"]
    assert [x.cycle for x in grounding.extract_quantities("매월 5,000원")] == ["월"]
    assert [(x.raw, x.cycle) for x in grounding.extract_quantities("3월 5,000원")] == [("5,000원", "")]   # '3월' 의 월은 주기가 아니고 '3월 5' 는 날짜도 아니다
    assert [x.cycle for x in grounding.extract_quantities("19,900원/월")] == ["월"]


@pytest.mark.parametrize("text", ["문의 1588-1234", "고객센터 02-123-4567", "문의 031) 123-4567", "사업자등록번호 123-45-67890",
                                  "준법감시인 심의필 제2026-01234호", "여신금융협회 심의필 제0000-000호", "https://example.com/a/12345"])
def test_identifiers_are_not_benefit_numbers(text):
    assert not grounding.extract_quantities(text), text


def test_phone_digits_never_support_a_benefit_number():
    p = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="고객센터 1588-1234")])
    assert "1,234명" in g(p, "1,234명이 선택", "지금 확인").numbers_unsupported
    assert "1,588원" in g(p, "1,588원 혜택", "지금 확인").numbers_unsupported


def test_attached_number_without_benefit_unit_is_still_ignored():
    assert not grounding.extract_quantities("코스피200 추종")
    assert not grounding.extract_quantities("제2026호")
    assert [q.raw for q in grounding.extract_quantities("금리4.5% 월19,900원 최대2배")] == ["4.5%", "19,900원", "2배"]


def test_conditions_outside_the_six_kinds_are_not_claimed():
    """코드가 확인하지 못하는 조건('신규 고객 한정')은 빠져 있어도 코드는 아무것도 말하지 않는다 — 범용 분석기가 아니다."""
    p = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (신규 고객 한정, 첫 달만)")])
    r = g(p, "전 상품 10% 할인", "지금 구매")
    assert "GRND-COND-01" not in rules(r) and "GRND-COND-02" not in rules(r)
    assert grounding.CONDITION_KINDS == ("자동결제·유료 전환", "심사 조건", "최소 금액 조건", "기간 한정", "1인 1회", "제외 조건")


# ───────────── 수정안·직접 편집(claims 없음) 경로 ─────────────
def _router(response):
    r = Router(SETTINGS, providers={"openai": scripted_review_provider(response), "anthropic": FakeProvider("anthropic", available=False)}, tracer=Tracer())
    r.new_run()
    return r


APPROVE = {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "통제된 응답", "rewrite": None}


@pytest.mark.parametrize("case_id,verified", [("K-A1", False), ("K-A2", True), ("K-A3", True), ("K-B1", False), ("K-B2", True), ("K-C1", False), ("K-C3", True),
                                              ("K-F2", False), ("K-F1", True), ("K-G1", False), ("K-H1", False)])
def test_rewrite_and_direct_recheck_reject_wrong_conditions_even_when_model_approves(case_id, verified):
    """모델이 확신 0.95 로 APPROVE 라고 답해도, 코드가 조건·숫자 불일치를 확인한 수정안은 통과(수정안 확인 완료)가 아니다 — 자동 수정안 검사와 직접 재검사가 같은 결론."""
    case = next(c for c in CASES if c.id == case_id)
    copy, product = case.to_copy(), case.to_product()
    assert not copy.claims  # 수정안·직접 편집 문구처럼 claims 가 비어 있다
    cats = CAT.effective_categories(product.category, copy.full_text())
    facts_text = "\n".join(f"- {f.id}: {f.text}" for f in product.facts)
    tr = Tracer()
    with tr.trace("t"):
        auto = check_rewrite(copy, product, cats, "", [], CAT, _router(APPROVE), facts_text, tr, SETTINGS.auto_decision_confidence)
    direct = recheck(copy, product, cats, SETTINGS, router=_router(APPROVE), landing_text="", catalog=CAT, tracer=Tracer()).report
    assert (auto.decision == Decision.APPROVE) is verified, (case.notes, auto.reason)
    assert direct.final_decision == auto.decision, (auto.decision, direct.final_decision, direct.decision_reason)


def test_model_cannot_overrule_the_code_condition_finding():
    """GRND 계층 판정은 코드 몫이다: 모델이 같은 규칙으로 반박하거나 다른 곳에서 '충족' 이라고 해도 코드가 낸 COND-02 는 그대로다."""
    case = next(c for c in CASES if c.id == "K-B1")
    det = det_of(case)
    assert det.decision == Decision.REVISE
    assert any(f.rule_id == "GRND-COND-02" and f.verified_quote and f.quote and f.quote in case.to_copy().full_text() for f in det.findings)


def test_reviewer_prompt_states_what_the_code_did_and_did_not_check():
    from agents.prompts import PROMPT_VERSION, reviewer_system

    text = reviewer_system(CAT, {"ecommerce"})
    assert "여섯 종류뿐" in text and "GRND-COND-02" in text and "코드가 확인하지 못했다" in text
    for kind in ("자동결제", "심사", "최소 금액", "기간", "1인 횟수", "제외"):
        assert kind in text
    assert PROMPT_VERSION >= "2026.10.4"


# ───────────── 조건 비교 세부 (변경하면 반드시 실패해야 하는 동작) ─────────────
def test_condition_value_must_match_the_referenced_fact_not_just_any_fact():
    """광고가 다른 사실(F2)의 값을 써서 '이상' 이 있는 것처럼 보여도, 가리킨 사실(F1)의 조건이 충족된 것은 아니다."""
    p = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[
        Fact(id="F1", text="전 상품 10% 할인, 50,000원 이상 구매 시"), Fact(id="F2", text="신규 가입 시 쿠폰 3만원 이상 충전")])
    r = g(p, "전 상품 10% 할인", "3만원 이상 충전하면 쿠폰", claims=[Claim(text="전 상품 10% 할인", fact_id="F1")])
    assert "GRND-COND-02" not in rules(r)                                   # 3만원 이상은 F2 에 있는 값이라 '다름' 은 아니다
    assert any(f.rule_id == "GRND-COND-01" and "50,000원 이상" in f.explanation for f in r.findings)  # 하지만 F1 의 50,000원 이상은 빠졌다


def test_value_conflict_is_reported_once_not_as_number_and_condition():
    r = g(P_A, "전 상품 10% 할인", "지금 구매", ["5,000원 이상 구매 시"])
    assert rules(r) == {"GRND-COND-02"} and not r.numbers_unsupported
    assert [f.quote for f in r.findings] == ["5,000원 이상"]
    r2 = g(P_B, "7일 무료 체험", "자동결제 없음")
    assert rules(r2) == {"GRND-COND-02"}


def test_fact_that_denies_a_condition_conflicts_with_an_ad_that_states_it():
    p = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 (제외 품목 없음)")])
    assert "GRND-COND-02" in rules(g(p, "전 상품 10% 할인", "일부 품목 제외"))
    assert not g(p, "전 상품 10% 할인", "제외 품목 없이 모두 적용").findings


def test_numbers_in_disclosures_make_a_fact_referenced():
    """헤드라인·본문에 숫자가 없어도 고지 문구에 쓴 '7일 무료 체험' 이 사실을 가리킨다 — 빠진 자동결제 조건을 잡는다."""
    r = g(P_B, "영어 공부", "지금 시작해 보세요", ["7일 무료 체험"])
    assert r.referenced_fact_ids == ["F1"] and "GRND-COND-01" in rules(r)
