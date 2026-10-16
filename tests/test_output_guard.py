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
