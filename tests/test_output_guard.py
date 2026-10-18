import time

import pytest

from core.schemas import AdCopy, Decision, Fact, ProductCategory, ProductFacts
from guardrails import output_guard

PRODUCT = ProductFacts(name="p", category=ProductCategory.APP, facts=[Fact(id="F1", text="7일 무료 체험")])


def test_format_drift():
    copy = AdCopy(headline="Best app ever, try now and enjoy the greatest experience", body="Free trial for 7 days", cta="Start")
    r = output_guard.check(copy, PRODUCT, {"app"})
    ids = {f.rule_id for f in r.findings}
    assert {"TA-LANG-01", "TA-LENGTH-01"} <= ids and r.decision == Decision.REVISE


def test_checklist_covers_all_layers():
    copy = AdCopy(headline="7일 무료 체험", body="체험 중 해지 가능", cta="시작")
    r = output_guard.check(copy, PRODUCT, {"app"}, landing_text="7일 무료 체험 체험 중 해지 가능")
    assert all(c.passed for c in r.checklist) and r.decision == Decision.APPROVE
    assert {c.layer.value for c in r.checklist} >= {"injection", "lexical", "disclosure", "grounding", "structural"}


@pytest.mark.parametrize("phrase", ["원금보장", "대박", "치료", "로또", "토토", "완치", "손실 걱정 없", "리딩방"])
@pytest.mark.parametrize("n", [20_000, 100_000])
def test_output_guard_is_bounded_on_a_disclosure_made_of_one_banned_phrase(phrase, n):
    """같은 금지 문구가 수만 번 반복돼도 3초 안에 끝나고, 읽지 못한 뒷부분이 있다는 알림(수정 필요)도 함께 낸다."""
    text = (phrase * (n // len(phrase) + 1))[:n]
    t0 = time.perf_counter()
    r = output_guard.check(AdCopy(headline="7일 무료 체험", body="체험 중 해지 가능", cta="시작", disclosures=[text]), PRODUCT,
                           {"app", "investment", "loan", "ecommerce", "other"}, "", [])
    assert time.perf_counter() - t0 < 3.0
    assert r.decision != Decision.APPROVE
    assert any(f.rule_id == "GRND-NUM-01" and "뒷부분은 숫자·조건을 확인하지 못함" in f.explanation for f in r.findings)


def test_output_guard_checklist_marks_the_unread_tail_in_the_grounding_row():
    r = output_guard.check(AdCopy(headline="7일 무료 체험", body="체험 중 해지 가능", cta="시작", disclosures=["가" * 20_001]), PRODUCT, {"app"})
    row = next(c for c in r.checklist if c.name.startswith("숫자·주장"))
    assert not row.passed and "확인하지 못한 뒷부분" in row.detail
    ok = output_guard.check(AdCopy(headline="7일 무료 체험", body="체험 중 해지 가능", cta="시작"), PRODUCT, {"app"})
    assert next(c for c in ok.checklist if c.name.startswith("숫자·주장")).passed
