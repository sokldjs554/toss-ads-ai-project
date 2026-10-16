import pytest

from agents.planner import validate_and_repair
from core.llm_schemas import PlanAdSetOut, PlanOut


def plan(**kw) -> PlanOut:
    base = dict(objective="awareness", kpi="도달", total_budget_krw=1_000_000,
                ad_sets=[PlanAdSetOut(name="a", audience="만 19세 이상", placement="리스트 배너", budget_krw=700_000, bid_strategy="자동 입찰 전환 최대화", rationale="r"),
                         PlanAdSetOut(name="b", audience="만 19세 이상", placement="보드 배너", budget_krw=200_000, bid_strategy="직접 입찰 CPM", rationale="r")],
                planner_reasoning="x", assumptions=[], risks=[])
    base.update(kw)
    return PlanOut(**base)


def test_plan_budget_repair():
    p, issues = validate_and_repair(plan(), {"loan"})
    assert sum(a.budget_krw for a in p.ad_sets) == 1_000_000 and p.repaired
    assert any(i.field == "budget" for i in issues)


def test_bid_strategy_repaired_to_allowed():
    p, issues = validate_and_repair(plan(), {"app"})
    assert all(a.bid_strategy == "직접 입찰 CPM" for a in p.ad_sets)  # awareness 는 CPM 만
    assert all(a.placement == "보드 배너" for a in p.ad_sets)       # CPM 은 보드 배너 전용
    assert any(i.field == "bid_strategy" for i in issues)


def test_financial_risk_added():
    p, _ = validate_and_repair(plan(), {"loan"})
    assert any("심의" in r for r in p.risks)


def test_youth_target_is_unrepairable_issue():
    p, issues = validate_and_repair(plan(ad_sets=[PlanAdSetOut(name="a", audience="10대 청소년", placement="보드 배너", budget_krw=1_000_000, bid_strategy="직접 입찰 CPM", rationale="r")]), {"app"})
    assert any(i.field == "audience" and not i.repaired for i in issues)


def test_empty_ad_sets_rejected():
    with pytest.raises(ValueError):
        validate_and_repair(plan(ad_sets=[]), {"app"})
