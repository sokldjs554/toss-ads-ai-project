"""첫 실제 모델 실행에서 드러난 경계조건의 오프라인 회귀 검사.

run1은 계획 타겟의 원문을 저장하지 않았으므로 아래 타겟 문구를 실제 응답으로
취급하지 않는다. 같은 경고를 독립적으로 재현한 최소 입력과 반례다.
상품명 사례는 저장된 사용자 말과 잘못 채워진 값을 재사용한다.
"""
from __future__ import annotations

import pytest

from agents import conversation as C
from agents import conversation_rules as R
from agents.planner import validate_and_repair
from core.config import Settings
from core.llm_schemas_conversation import SlotUpdateOut
from llm.base import Task
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import TaskScriptedProvider


def _plan(audience: str):
    return {
        "objective": "traffic", "kpi": "방문", "total_budget_krw": 1_000_000,
        "ad_sets": [{"name": "타겟 확인", "audience": audience, "placement": "리스트 배너",
                     "budget_krw": 1_000_000, "bid_strategy": "직접 입찰 CPC", "rationale": "테스트 입력"}],
        "planner_reasoning": "테스트 입력", "assumptions": [], "risks": [],
    }


@pytest.mark.parametrize("audience", [
    "만 19세 이상 40세 이하 직장인",
    "만 19세 이상이며 40세 이하인 성인",
    "40세 이하, 만 19세 이상",
    "성인 중 40세 이하",
    "만 19세 이상 성인, 미성년자 제외",
    "만 19세 이상 성인, 청소년 제외",
    "만 19세 이상, 만 18세 이하 제외",
    "20~39세 직장인, 고등학생은 제외합니다.",
])
def test_adult_bounds_and_explicit_exclusions_do_not_become_minor_targets(audience):
    plan, _ = validate_and_repair(_plan(audience), {"app"})
    assert C.plan_minor_sets(plan) == []


@pytest.mark.parametrize("audience", [
    "40세 이하 직장인",                 # 하한을 말하지 않았다.
    "만 19세 이상 또는 18세 이하",       # 대안 조건을 교집합으로 합치면 안 된다.
    "만 19세 이상 또는 40세 이하",
    "만 19세 이상 / 40세 이하",
    "20대, 40세 이하",                 # 별도 연령 집단을 하한 조건으로 바꾸지 않는다.
    "20대 및 40세 이하",
    "20~30대 성인과 10대",              # 성인 조건이 별도 미성년 집단을 덮으면 안 된다.
    "만 19세 이상, 청소년도 포함",
    "만 19세 이상, 청소년 제외하지 않음",
    "만 19세 이상, 미성년자 제외 안 함",
    "만 19세 이상, 고등학생은 제외 없이 포함",
    "미성년자는 제외, 초등학생도 포함",  # 제외를 다른 대상에 전파하면 안 된다.
    "만 19세 이상, 만 18세 이하",       # 모순되는 조건을 조용히 통과시키지 않는다.
])
def test_unsupported_or_minor_inclusive_audiences_still_need_attention(audience):
    plan, _ = validate_and_repair(_plan(audience), {"app"})
    assert C.plan_minor_sets(plan) == ["타겟 확인"]


@pytest.mark.parametrize("text,name", [
    ("적금 상품 광고입니다. 목표는 트래픽이에요.", "적금 상품"),
    ("보험 서비스 광고를 만들고 싶어요.", "보험 서비스"),
    ("이번에는 교육 과정 광고를 진행해요.", "교육 과정"),
    ("광고 소재에는 새 여름 신발을 넣어요.", "새 여름 신발"),
])
def test_model_cannot_turn_a_generic_description_into_a_product_name(text, name):
    candidate, dropped = C.validate_update(
        SlotUpdateOut(slot="product_name", value=name, evidence_quote=name, confidence=0.95), text,
    )
    assert candidate is None
    assert dropped is not None and dropped.detail


@pytest.mark.parametrize("text,name", [
    ("상품명은 여름 산책 운동화입니다.", "여름 산책 운동화"),
    ("'적금 상품'이라는 상품을 광고해요.", "적금 상품"),
    ("차곡차곡이라는 앱을 광고해요.", "차곡차곡"),
    ("파이라는 앱을 광고해요.", "파이"),
    ("아이라는 서비스를 광고해요.", "아이"),
    ("좋은 아이라는 서비스를 광고해요.", "좋은 아이"),
    ("파이라고 하는 앱을 광고해요.", "파이"),
    ("좋은 아이라고 하는 서비스를 광고해요.", "좋은 아이"),
    ("'별이'라는 앱을 광고해요.", "별이"),
    ("가상은행 자유적금 광고를 만들어요.", "가상은행 자유적금"),
])
def test_attributed_names_remain_accepted(text, name):
    candidate, dropped = C.validate_update(
        SlotUpdateOut(slot="product_name", value=name, evidence_quote=name, confidence=0.95), text,
    )
    assert dropped is None and candidate.value == name


@pytest.mark.parametrize("text", ["하라는 앱", "별이라는 상품"])
def test_called_name_particle_fix_does_not_accept_one_character_names(text):
    assert R._extract_name(text, []) == []


def test_real_run_generic_name_is_rejected_without_erasing_the_budget_warning():
    generic_name = {"slot": "product_name", "value": "적금 상품", "evidence_quote": "적금 상품", "confidence": 0.95}

    def converse(req):
        updates = [generic_name] if "적금 상품 광고입니다" in req.context["user_message"] else []
        return {"slot_updates": updates, "reply": "", "questions": []}

    provider = TaskScriptedProvider("openai", {Task.CONVERSE: converse})
    settings = Settings.from_env(openai_api_key="sk-fake-not-real", anthropic_api_key="", force_demo=False)
    router = Router(settings, providers={"openai": provider}, tracer=Tracer())
    first = C.step(C.new_state(), "적금 상품 광고입니다. 목표는 트래픽이에요.", router, settings)
    last = C.step(first.state, "월 예산 52만원으로 시작해 주세요.", router, settings)

    assert "product_name" not in last.state.slots
    assert first.dropped_updates[0].slot == "product_name"
    assert last.state.slots["monthly_budget_krw"].value == 520_000
    assert "BUDGET_LOW" in {issue.code for issue in last.state.issues}
    assert C.brief_for_workbench(last.state)["name_is_placeholder"]


def test_actual_minor_target_warning_remains_visible_through_a_full_turn():
    provider = TaskScriptedProvider("openai", {Task.PLAN: _plan("20~30대 성인과 10대")})
    settings = Settings.from_env(openai_api_key="sk-fake-not-real", anthropic_api_key="", force_demo=False)
    router = Router(settings, providers={"openai": provider}, tracer=Tracer())
    result = C.step(C.new_state(), "앱 광고, 사이트 방문, 월 예산 100만원", router, settings)
    assert "PLAN_AUDIENCE" in {issue.code for issue in result.state.issues}
    assert "10대" in result.state.plan_preview.ad_sets[0].audience
