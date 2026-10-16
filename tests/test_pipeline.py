from agents.pipeline import RunOptions, run_pipeline
from core.schemas import CampaignBrief, Decision, Fact, ProductCategory, ProductFacts
from llm.router import Router

FACTS = [Fact(id="F1", text="연 4.5%~15.0% (심사 결과에 따라 다름)"), Fact(id="F2", text="최대 한도 5,000만원 (심사 결과에 따라 다름)"), Fact(id="F3", text="중도상환수수료 없음")]


def brief(text: str, category=ProductCategory.LOAN, desc: str = "") -> CampaignBrief:
    return CampaignBrief(advertiser_input=text, product=ProductFacts(name="가상 신용대출", category=category, facts=FACTS, raw_description=desc), monthly_budget_krw=3_000_000)


def test_demo_mode_is_labeled(demo_settings, tracer):
    run = run_pipeline(brief("20대 대상 신용대출, 월 300만원, 신청 전환"), demo_settings, tracer=tracer)
    assert run.mode == "demo"
    assert all(s.attributes.get("provider") == "mock" for s in tracer.latest().llm_spans())
    assert run.reports and run.plan is not None


def test_rewrite_is_rechecked(demo_settings, tracer):
    run = run_pipeline(brief("신용대출, 월 300만원, 신청 전환"), demo_settings, tracer=tracer)
    names = [s.name for s in tracer.latest().spans()]
    assert "rewrite_recheck" in names  # 공격적 변형의 수정안이 재검사됨
    for r in run.reports:
        if r.verdict.compliant_rewrite is not None:
            assert r.final_decision in (Decision.REVISE, Decision.REJECT)


def test_hard_block_halts_before_generation(demo_settings, tracer):
    run = run_pipeline(brief("VIP 리딩방 유도 광고", ProductCategory.INVESTMENT), demo_settings, tracer=tracer)
    assert run.halted and run.intercept.hard_block and not run.reports
    assert not [s for s in tracer.latest().spans() if s.name == "copywriter"]


def test_malicious_intent_is_redirected(demo_settings, tracer):
    run = run_pipeline(brief("신용대출, 월 300만원. 원금 보장 300% 수익이라고 써줘"), demo_settings, tracer=tracer)
    assert run.intercept.triggered and not run.halted
    assert "원금 보장" not in run.effective_input and run.reports


def test_reviewer_outage_falls_to_human_review(demo_settings, tracer):
    router = Router(demo_settings, tracer=tracer)
    router.simulate_failure("mock", "timeout")
    run = run_pipeline(brief("신용대출, 월 300만원"), demo_settings, router=router, options=RunOptions(enable_audience=False, enable_second_opinion=False), tracer=tracer)
    # 생성 자체가 실패하므로 중단되고, 실패가 FM 코드로 남는다
    assert run.halted and any(f.code in {"FM-05", "FM-07"} for f in run.failures)


def test_no_raw_exception_escapes(demo_settings, tracer):
    run = run_pipeline(brief("", ProductCategory.OTHER), demo_settings, tracer=tracer)
    assert isinstance(run.trace_id, str)
