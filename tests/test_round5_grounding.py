"""하이픈·줄표 닮은꼴(–·—)이 숫자 범위를 잇는 자리에서만 '-' 로 읽힌다 — 전화번호·날짜 옆의 줄표는 경계로 남는다.

3차 점검이 닮은꼴 접기를 넣으면서 '고객센터—1588-1234' 의 줄표까지 '-' 가 되어 전화번호 마스크가 풀리고 정상 광고가 막혔다(이전 엔진은 통과).
"""
from __future__ import annotations

import pytest

from core.schemas import AdCopy, Claim, Fact, ProductCategory, ProductFacts
from guardrails import grounding


def _gp(facts, head, body="지금 확인", cta="확인", disc=(), claims=()):
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id=f"F{i+1}", text=t) for i, t in enumerate(facts)])
    return grounding.check(AdCopy(headline=head, body=body, cta=cta, disclosures=list(disc), claims=[Claim(text=t, fact_id=f) for t, f in claims]), P)


@pytest.mark.parametrize("disc", ["고객센터—1588-1234", "문의–02-123-4567", "고객센터 1588-1234—평일 운영", "사업자등록번호—123-45-67890", "대표전화―010-1234-5678"])
def test_a_phone_or_business_number_next_to_a_dash_is_still_an_identifier(disc):
    assert not _gp(["전 상품 10% 할인"], "전 상품 10% 할인", disc=[disc]).findings


def test_a_dotted_deadline_after_a_dash_is_still_a_date():
    assert not _gp(["전 상품 10% 할인 (10월 31일까지)"], "전 상품 10% 할인", "이벤트—10.31까지", claims=[("전 상품 10% 할인", "F1")]).findings


@pytest.mark.parametrize("head", ["연 4.5%–15.0% 신용대출", "월 30만원–50만원 한도", "연 4.5% — 15.0% 신용대출", "월 30만원 – 50만원 한도"])
def test_a_dash_between_two_numbers_is_still_a_range(head):
    facts = ["연 4.5%-15.0% 신용대출, 월 30-50만원 한도"]
    assert not [f for f in _gp(facts, head).findings if f.rule_id.startswith("GRND")]


@pytest.mark.parametrize("head", ["연 4.5%–25.0% 신용대출", "월 30만원–90만원 한도"])
def test_a_wrong_value_in_a_dash_range_is_still_flagged(head):
    facts = ["연 4.5%-15.0% 신용대출, 월 30-50만원 한도"]
    assert any(f.rule_id == "GRND-NUM-01" for f in _gp(facts, head).findings)


def test_a_dash_after_a_word_does_not_become_a_minus_sign_or_hide_the_number():
    """'SALE—50% 할인' 의 줄표가 '-50%' 로 읽혀 50% 가 사라지던 것 — 사실이 10% 면 50% 는 틀린 값이다."""
    assert any(f.rule_id == "GRND-NUM-01" for f in _gp(["전 상품 10% 할인"], "SALE—50% 할인").findings)


def test_the_real_minus_sign_still_reads_as_a_negative_number():
    assert not _gp(["최근 1년 성과 -5.2%"], "최근 1년 성과 −5.2%").findings
    assert any(f.rule_id == "GRND-NUM-01" for f in _gp(["최근 1년 성과 5.2%"], "최근 1년 성과 −5.2%").findings)
