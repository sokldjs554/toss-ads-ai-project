from core.schemas import AdCopy
from guardrails import disclosure


def test_missing_loan_items_reported():
    copy = AdCopy(headline="신용대출", body="연 4.5%~15.0% (심사 결과에 따라 다름)", cta="신청", disclosures=[])
    r = disclosure.check(copy, {"loan"})
    assert not r.ok
    assert {i.key for i in r.missing} >= {"overdue_rate", "repayment", "warning", "review_no"}
    assert r.findings[0].rule_id == "FIN-DISCLOSE-LOAN-01"


def test_full_loan_disclosure_passes():
    copy = AdCopy(headline="신용대출", body="조건 확인", cta="신청", disclosures=[
        "연 4.5%~15.0% (심사 결과에 따라 다름)", "연체 시 연체이자율 적용", "상환 방식은 상품 설명서 참조",
        "계약 체결 전 상품 설명서와 약관을 반드시 읽어 보시기 바랍니다.", "과도한 빚은 당신에게 큰 불행을 안겨줄 수 있습니다.",
        "준법감시인 심의필 제2026-0412호"])
    assert disclosure.check(copy, {"loan"}).ok


def test_marketing_text_in_disclosure_slot_flagged():
    copy = AdCopy(headline="h", body="b", cta="c", disclosures=["지금 바로 신청하세요! 특가 혜택"])
    r = disclosure.check(copy, {"ecommerce"})
    assert any(f.rule_id == "TA-DISCLOSURE-SLOT-01" for f in r.findings)


def test_legal_disclosure_with_benefit_word_not_flagged():
    copy = AdCopy(headline="h", body="b", cta="c", disclosures=["연회비 국내 1만원, 전월 실적 30만원 이상 시 혜택 적용"])
    assert not any(f.rule_id == "TA-DISCLOSURE-SLOT-01" for f in disclosure.check(copy, {"card"}).findings)
