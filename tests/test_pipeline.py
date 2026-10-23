from agents.pipeline import RunOptions, run_pipeline
from core.schemas import CampaignBrief, Decision, Fact, ProductCategory, ProductFacts, RewriteStatus
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
        if r.rewrite_status == RewriteStatus.VERIFIED:
            assert r.verified_rewrite is not None and r.rewrite_recheck_decision == Decision.APPROVE
        else:
            assert r.verified_rewrite is None
        if r.rewrite_status == RewriteStatus.FAILED_RECHECK:
            # 실패 기록(FM-19)의 조치와 실제 처리 결과가 일치한다: 수정안 폐기, 원문 판정 유지
            ev = [f for f in run.failures if f.code == "FM-19" and f.message.startswith(r.variant_id)]
            assert ev and r.final_decision.value in ev[0].fallback and "폐기" in ev[0].fallback


def test_failed_rewrite_not_shown_on_reject_and_revise_paths(demo_settings, tracer, monkeypatch):
    """리뷰에서 확인된 사례(기본 신용대출 예시): 수정안이 재검사에 걸려 FM-19 가 기록되는데도 REJECT 판정 소재에 수정안이 남아 화면에 보였다.
    대역의 수정안이 항상 block 표현을 품도록 바꿔, 모든 판정 경로에서 verified_rewrite 가 비고 FM-19 의 조치와 처리 결과가 일치하는지 본다."""
    from llm import mock_provider
    from ui.presets import PRESETS, build_brief

    original = mock_provider.MockProvider._review
    original_copy = mock_provider.MockProvider._copy

    def copy_with_real_block(self, req):
        out = original_copy(self, req)
        # REJECT 경로는 실제 비교 우위 위반으로 만든다. 커머스 예시의
        # 정상 쿠폰 문구의 금지 업종 과탐에 의존하면 정규화 수정 시 검증이 사라진다.
        out["variants"][0]["headline"] = "타사보다 2배 좋은 조건"
        return out

    def bad_review(self, req):
        out = original(self, req)
        if out.get("rewrite"):
            out["rewrite"]["headline"] = "타사보다 2배 좋은 조건"  # TA-COMPARE-01 (block) → 재검사 실패
        return out

    monkeypatch.setattr(mock_provider.MockProvider, "_review", bad_review)
    monkeypatch.setattr(mock_provider.MockProvider, "_copy", copy_with_real_block)
    p = PRESETS["commerce_landing"]
    run = run_pipeline(build_brief(p), demo_settings, options=RunOptions(landing_text=p["landing_text"]), tracer=tracer)
    with_rewrite = [r for r in run.reports if r.verdict.proposed_rewrite is not None]
    assert with_rewrite and {r.final_decision for r in with_rewrite} >= {Decision.REVISE, Decision.REJECT}
    for r in with_rewrite:
        assert r.rewrite_status == RewriteStatus.FAILED_RECHECK and r.verified_rewrite is None
        ev = [f for f in run.failures if f.code == "FM-19" and f.message.startswith(r.variant_id)]
        assert ev and "폐기" in ev[0].fallback and r.final_decision.value in ev[0].fallback


def test_demo_loan_preset_rewrite_status_consistent(demo_settings, tracer):
    """기본 예시: 수정안이 있으면 재검사 결과(verified/failed)와 verified_rewrite 유무가 항상 일치한다."""
    from ui.presets import PRESETS, build_brief

    p = PRESETS["loan_ok"]
    run = run_pipeline(build_brief(p), demo_settings, options=RunOptions(landing_text=p["landing_text"]), tracer=tracer)
    for r in run.reports:
        assert (r.verified_rewrite is not None) == (r.rewrite_status == RewriteStatus.VERIFIED)
        if r.rewrite_status == RewriteStatus.VERIFIED:
            assert r.rewrite_recheck_decision == Decision.APPROVE and not r.rewrite_recheck_findings


def test_progress_callback_reports_real_stages(demo_settings, tracer):
    seen = []
    run_pipeline(brief("신용대출, 월 300만원"), demo_settings, options=RunOptions(on_progress=lambda k, l: seen.append(k)), tracer=tracer)
    assert seen[:3] == ["intercept", "planner", "copywriter"] and "review" in seen


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


def test_budget_halt_keeps_remaining_variants_as_human_review(demo_settings, tracer):
    """FM-20: 비용·호출 상한에 걸리면 아직 검사하지 못한 광고안이 사라지지 않고 담당자 확인으로 남는다."""
    router = Router(demo_settings, tracer=tracer)
    orig = router.new_run

    def tight():
        orig()
        router.budget.max_calls = 5  # 두 번째 광고안 검사 도중에 걸린다

    router.new_run = tight
    run = run_pipeline(brief("신용대출, 월 300만원, 신청 전환"), demo_settings, router=router, options=RunOptions(n_variants=3), tracer=tracer)
    assert run.halted and "FM-20" in run.halt_reason
    assert len(run.reports) == 3  # 검사하지 못한 광고안도 사라지지 않는다
    unchecked = [r for r in run.reports if "FM-20" in r.decision_reason]
    assert unchecked, [r.decision_reason for r in run.reports]
    for r in unchecked:
        # 모델 검사 없이 통과시키지 않는다: 담당자 확인이거나, 코드 검사만으로 반려·수정 사유가 확인된 경우뿐
        assert r.final_decision in (Decision.HUMAN_REVIEW, Decision.REJECT, Decision.REVISE)
        assert r.rewrite_status == RewriteStatus.NONE and r.verified_rewrite is None
    assert any(r.final_decision == Decision.HUMAN_REVIEW for r in unchecked)
    assert any(f.code == "FM-20" for f in run.failures)


def test_rewrite_recheck_runs_semantic_review(demo_settings, tracer):
    """수정안 재검사는 코드 검사 뒤 검토 모델의 의미 검사까지 거친다 (span 에 semantic_decision 이 남는다)."""
    run = run_pipeline(brief("신용대출, 월 300만원, 신청 전환"), demo_settings, tracer=tracer)
    spans = [s for s in tracer.latest().spans() if s.name == "rewrite_recheck"]
    assert spans
    verified = [r for r in run.reports if r.rewrite_status == RewriteStatus.VERIFIED]
    for r in verified:
        sp = next(s for s in spans if s.attributes.get("decision") == "APPROVE" and s.attributes.get("semantic_decision"))
        assert sp.attributes["semantic_decision"] == "APPROVE"
