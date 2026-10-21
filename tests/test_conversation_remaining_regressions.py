"""남은 대화 경계의 개발용 회귀 검사. 실제 제공자·네트워크 호출 없음.

고정된 2026-10-20 holdout의 라벨이나 과거 결과를 바꾸지 않는다. 새 대조
문장과 명시적인 후속 답변으로 확인·보완·미리보기·광고 작업 전달을 검사한다.
"""
from __future__ import annotations

import json

import pytest

from agents import conversation as C
from agents import conversation_rules as R
from core.config import Settings
from llm.base import Task
from llm.router import Router
from tests.fakes import TaskScriptedProvider


@pytest.fixture
def engine():
    settings = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    return Router(settings), settings


def _upper_only(engine, maximum=34):
    router, settings = engine
    text = f"투자 서비스 인지도 광고, 월 예산 120만원, 타깃은 {maximum}세 이하, 여성, 직장인."
    return C.step(C.new_state(), text, router, settings)


def _answer(engine, first, text):
    router, settings = engine
    return C.step(first.state, text, router, settings)


def _assert_preview_bounds(state, lower, upper):
    assert state.plan_preview is not None and not state.plan_stale
    for ad_set in state.plan_preview.ad_sets:
        ages = [c.value for c in R._extract_audience(ad_set.audience, [])
                if c.value.get("age_min") is not None or c.value.get("age_max") is not None]
        assert ages, ad_set.audience
        for age in ages:
            assert age.get("age_min") is not None and age["age_min"] >= lower, ad_set.audience
            if upper is not None:
                assert age.get("age_max") is not None and age["age_max"] <= upper, ad_set.audience


@pytest.mark.parametrize("maximum", [19, 34, 47])
def test_upper_only_age_requires_a_specific_lower_bound_and_offers_a_preserving_chip(engine, maximum):
    result = _upper_only(engine, maximum)
    audience = result.state.slots["audience"]
    assert not result.ready and result.state.status == "blocked"
    assert audience.value.get("age_min") is None and audience.value["age_max"] == maximum
    assert result.state.plan_preview is None
    question, = result.questions
    assert question.slot == "audience" and question.kind == "fix"
    assert "최소 연령" in question.text and str(maximum) in question.text
    assert question.choices == [f"타겟을 만 19~{maximum}세로 바꿔 주세요"]
    issue, = [i for i in result.issues if i.code == "MINOR_TARGET"]
    assert issue.severity == "block" and "확인" in issue.message
    with pytest.raises(C.NotReady):
        C.brief_for_workbench(result.state)


@pytest.mark.parametrize("maximum", [19, 34, 47])
def test_clickable_full_range_reply_preserves_the_upper_bound_through_preview_and_handoff(engine, maximum):
    first = _upper_only(engine, maximum)
    result = _answer(engine, first, first.questions[0].choices[0])
    assert result.ready
    audience = result.state.slots["audience"].value
    assert (audience["age_min"], audience["age_max"]) == (19, maximum)
    assert audience["gender"] == "여성" and audience["segments"] == ["직장인"]
    assert not any(i.code == "MINOR_TARGET" for i in result.issues)
    _assert_preview_bounds(result.state, 19, maximum)
    payload = C.brief_for_workbench(result.state)
    assert f"만 19~{maximum}세" in payload["advertiser_input"]


@pytest.mark.parametrize("answer,minimum", [("만 19세 이상", 19), ("만 23세 이상", 23), ("성인", 19)])
def test_explicit_lower_bound_answer_keeps_prior_upper_bound_and_literal_evidence(engine, answer, minimum):
    first = _upper_only(engine, 47)
    result = _answer(engine, first, answer)
    assert result.ready
    audience = result.state.slots["audience"]
    assert audience.status == "confirmed"
    assert (audience.value["age_min"], audience.value["age_max"]) == (minimum, 47)
    assert audience.value["gender"] == "여성" and audience.value["segments"] == ["직장인"]
    assert any(e.turn == 1 and "47세 이하" in e.quote for e in audience.evidence)
    assert any(e.turn == 2 and e.quote == answer for e in audience.evidence)
    assert all(e.quote in result.state.turns[2 * (e.turn - 1)].text for e in audience.evidence)
    _assert_preview_bounds(result.state, minimum, 47)


@pytest.mark.parametrize("answer", [
    "네", "아니요", "알아서 해주세요", "만 18세 이상", "만 35세 이상",
    "만 19세 이상은 아니에요", "만 19세 이상 또는 34세 이하",
    "만 19세 이상, 고등학생도 포함", "성인과 10대",
])
def test_vague_inconsistent_or_mixed_group_answers_cannot_complete_the_age_confirmation(engine, answer):
    result = _answer(engine, _upper_only(engine), answer)
    assert not result.ready
    assert result.state.plan_preview is None
    with pytest.raises(C.NotReady):
        C.to_brief(result.state)


def test_explicit_target_replacement_keeps_its_existing_meaning(engine):
    result = _answer(engine, _upper_only(engine), "타겟을 만 40세 이상으로 바꿔 주세요")
    assert result.ready
    assert result.state.slots["audience"].value["age_min"] == 40
    assert result.state.slots["audience"].value.get("age_max") is None
    _assert_preview_bounds(result.state, 40, None)


def test_a_model_cannot_supply_the_missing_adult_lower_bound_without_user_evidence():
    def converse(req):
        return {"slot_updates": [{"slot": "audience", "value": "만 19~34세",
                                  "evidence_quote": "34세 이하", "confidence": 0.99}],
                "reply": "", "questions": []}

    provider = TaskScriptedProvider("openai", {Task.CONVERSE: converse})
    settings = Settings.from_env(openai_api_key="sk-fake-not-real", anthropic_api_key="", force_demo=False)
    router = Router(settings, providers={"openai": provider})
    result = C.step(C.new_state(), "투자 서비스 인지도 광고, 월 예산 120만원, 34세 이하", router, settings)
    assert not result.ready and result.state.plan_preview is None
    assert result.state.slots["audience"].value.get("age_min") is None
    assert any(d.slot == "audience" and d.reason == "value_not_in_quote" for d in result.dropped_updates)


def _plan(audiences):
    return {"objective": "traffic", "kpi": "방문", "total_budget_krw": 1_200_000,
            "ad_sets": [{"name": f"세트 {i}", "audience": text, "placement": "리스트 배너",
                         "budget_krw": 400_000, "bid_strategy": "직접 입찰 CPC", "rationale": "개발용 대조"}
                        for i, text in enumerate(audiences, 1)],
            "planner_reasoning": "개발용 대조", "assumptions": [], "risks": []}


def _with_plan(audiences, target="만 21~37세"):
    provider = TaskScriptedProvider("openai", {Task.PLAN: _plan(audiences)})
    settings = Settings.from_env(openai_api_key="sk-fake-not-real", anthropic_api_key="", force_demo=False)
    router = Router(settings, providers={"openai": provider})
    return C.step(C.new_state(), f"앱 광고, 사이트 방문, 월 예산 120만원, 타겟은 {target}", router, settings)


def test_preview_constrains_all_sets_but_preserves_narrower_groups_and_non_age_details():
    result = _with_plan(["만 19세 이상 직장인", "만 25~29세 여성", "최근 30일 광고 클릭 고객"])
    assert result.ready
    _assert_preview_bounds(result.state, 21, 37)
    sets = result.state.plan_preview.ad_sets
    assert "직장인" in sets[0].audience and "25~29세" in sets[1].audience and "여성" in sets[1].audience
    assert "최근 30일 광고 클릭 고객" in sets[2].audience
    assert any("연령" in note for note in result.state.plan_notes)
    assert sum(a.budget_krw for a in sets) == 1_200_000


@pytest.mark.parametrize("audience,expected", [
    ("만 19세 이상 45세 이하 직장인", "만 21~37세 직장인"),
    ("45세 이하, 만 19세 이상 직장인", "만 21~37세 직장인"),
    ("만 25~29세 여성 또는 만 30~45세 직장인", "만 25~29세 여성 또는 만 30~37세 직장인"),
])
def test_preview_handles_joint_bounds_and_preserves_separate_compatible_groups(audience, expected):
    result = _with_plan([audience, "만 25~29세 여성", "최근 광고 클릭 고객"])
    assert result.state.plan_preview.ad_sets[0].audience == expected
    _assert_preview_bounds(result.state, 21, 37)


@pytest.mark.parametrize("bad_group", ["만 45~60세 직장인", "만 19세 이상 또는 만 18세 이하", "20대 성인과 고등학생"])
def test_disjoint_or_minor_model_groups_are_not_silently_overwritten_to_look_compliant(bad_group):
    result = _with_plan([bad_group, "만 25~29세 여성", "최근 광고 클릭 고객"])
    assert result.ready  # 명시적으로 확인한 설정은 광고 작업에 전달할 수 있다.
    assert result.state.plan_preview is None
    assert any(i.code == "PLAN_UNAVAILABLE" for i in result.issues)
    assert any(f.code == "FM-02" and f.stage == "conversation_plan" for f in result.failures)
    assert "만 21~37세" in C.brief_for_workbench(result.state)["advertiser_input"]


@pytest.mark.parametrize("budget,category,objective", [(520_000, "적금", "트래픽"), (480_000, "기타", "인지도")])
def test_low_budget_remains_an_advisory_with_a_ready_single_set_plan(engine, budget, category, objective):
    router, settings = engine
    result = C.step(C.new_state(), f"업종은 {category}, 목표는 {objective}, 월 예산 {budget}원", router, settings)
    assert result.ready
    issue, = [i for i in result.issues if i.code == "BUDGET_LOW"]
    assert issue.severity == "warn" and "가정" in issue.message and "실제 플랫폼 기준이 아니" in issue.message
    assert len(result.state.plan_preview.ad_sets) == 1
    assert result.state.plan_preview.ad_sets[0].budget_krw == budget
    assert not any(q.slot == "monthly_budget_krw" for q in result.questions)
    assert C.brief_for_workbench(result.state)["budget"] == budget


def test_current_conversation_reports_use_new_paths_and_preserve_the_prior_metrics():
    from evals import dialogs as E

    for current, previous_name in ((E.DEFAULT_REPORT, "dialogs_regression_v2.json"),
                                    (E.ORIGINAL_CURRENT_REPORT, "dialogs_original_after_fix_20261020.json")):
        previous_path = E.REPORTS / previous_name
        assert current != previous_path
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        revised = json.loads(current.read_text(encoding="utf-8"))
        assert revised["code"] != previous["code"]
        assert revised["repro"] != previous["repro"]
        assert {k: v for k, v in revised.items() if k not in {"code", "repro"}} == {
            k: v for k, v in previous.items() if k not in {"code", "repro"}}


@pytest.mark.parametrize("relative_path,sha256", [
    ("evals/datasets/dialogs_holdout_20261020.jsonl", "379b97b1de1277be13bfeae77606813a583b56040cd5c04c44121afbbc3da705"),
    ("evals/reports/live_features/features-20261020-run3.json", "3981f9ab9a82502f02a8e6086e4178289863c8a632317dbb5544710af828fd91"),
    ("evals/reports/dialogs_regression_v2.json", "2b06598fe6421c1bfc122f3eb28a936758ca94fba03fe0247ee13b24b06187a7"),
    ("evals/reports/dialogs_original_after_fix_20261020.json", "174872a2bcdb9937f72f20c5ff28e8802257785e64a32cbeadcd06734441df21"),
])
def test_frozen_conversation_inputs_and_prior_reports_remain_byte_identical(relative_path, sha256):
    from evals import dialogs as E

    assert E.sha256_of(E.ROOT / relative_path) == sha256
