"""플래너 — 자연어 요청 → 캠페인 구조 (GPT-4o Structured Outputs). 출력은 플랫폼 제약으로 검증·수리한다 (FM-02)."""
from __future__ import annotations

from dataclasses import dataclass, field

from core.llm_schemas import PlanOut
from core.platform_spec import CPM_ONLY_PLACEMENT, PLACEMENTS, PlanIssue, allowed_bids
from core.schemas import AdSet, CampaignBrief, CampaignPlan, FINANCIAL_CATEGORIES, Objective
from guardrails.injection import spotlight
from llm.base import LLMRequest, Task
from llm.router import Router

from .prompts import PLANNER_SYSTEM

import re

_YOUTH = ("10대", "청소년", "미성년", "중학생", "고등학생")
_YOUTH_AGE = re.compile(r"만\s*(?:1[0-8]|[0-9])\s*세")   # '만 14세'·'만 18세 이하' 는 걸리고 '만 19세 이상' 은 걸리지 않는다 (예전의 '만 1' 접두 검사는 둘 다 걸렸다)


def targets_minors(audience: str) -> bool:
    """타겟 문구가 만 19세 미만을 겨냥하는가."""
    return any(k in audience for k in _YOUTH) or bool(_YOUTH_AGE.search(audience))


@dataclass
class PlanResult:
    plan: CampaignPlan
    issues: list[PlanIssue] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    provider: str = ""
    model: str = ""


def _user_prompt(brief: CampaignBrief, effective_input: str, categories: set[str], description: str) -> str:
    facts = "\n".join(f"- {f.id}: {f.text}" for f in brief.product.facts) or "- (없음)"
    budget = f"{brief.monthly_budget_krw:,}원" if brief.monthly_budget_krw else "요청문 참조"
    return (
        f"{spotlight('advertiser_request', effective_input)}\n\n"
        f"상품명: {brief.product.name}\n업종(선언/추론): {brief.product.category.value} / {', '.join(sorted(categories))}\n"
        f"월 예산 입력값: {budget}\n사실 목록:\n{facts}\n\n"
        + (spotlight("product_description", description[:1500]) if description else "")
    )


def validate_and_repair(raw: PlanOut | dict, categories: set[str]) -> tuple[CampaignPlan, list[PlanIssue]]:
    """모델 출력(스키마는 맞음)을 플랫폼 제약으로 검사한다. 고칠 수 있는 것은 고치고 repaired=True, 못 고치면 ValueError."""
    d = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
    issues: list[PlanIssue] = []
    objective = Objective(d["objective"])
    bids = allowed_bids(objective)
    ad_sets = d.get("ad_sets") or []
    if not ad_sets:
        raise ValueError("ad_sets 가 비어 있음")
    for a in ad_sets:
        if a["bid_strategy"] not in bids:
            issues.append(PlanIssue("bid_strategy", f"'{a['name']}': 목표 {objective.value} 에 허용되지 않는 입찰 '{a['bid_strategy']}' → '{bids[0]}'", True))
            a["bid_strategy"] = bids[0]
        if a["bid_strategy"] == "직접 입찰 CPM" and a.get("placement") != CPM_ONLY_PLACEMENT:
            issues.append(PlanIssue("placement", f"'{a['name']}': CPM 은 {CPM_ONLY_PLACEMENT} 전용 → 지면 수정", True))
            a["placement"] = CPM_ONLY_PLACEMENT
        if a.get("placement") not in PLACEMENTS:
            issues.append(PlanIssue("placement", f"'{a['name']}': 알 수 없는 지면 '{a.get('placement')}' → 리스트 배너", True))
            a["placement"] = PLACEMENTS[0]
        if targets_minors(a.get("audience") or ""):
            issues.append(PlanIssue("audience", f"'{a['name']}': 만 19세 미만을 겨냥한 타겟 표현 — 집행 불가", False))
        if a["budget_krw"] < 0:
            issues.append(PlanIssue("budget", f"'{a['name']}': 음수 예산 → 0", True))
            a["budget_krw"] = 0
    total = int(d.get("total_budget_krw") or 0)
    s = sum(int(a["budget_krw"]) for a in ad_sets)
    if total <= 0 and s > 0:
        d["total_budget_krw"] = total = s
    if total > 0 and abs(s - total) > max(1, total // 100):
        issues.append(PlanIssue("budget", f"ad set 예산 합 {s:,} ≠ 총 예산 {total:,} → 비례 재배분", True))
        if s > 0:
            acc = 0
            for i, a in enumerate(ad_sets):
                if i < len(ad_sets) - 1:
                    a["budget_krw"] = int(total * int(a["budget_krw"]) / s)
                    acc += a["budget_krw"]
                else:
                    a["budget_krw"] = total - acc
        else:
            share = total // len(ad_sets)
            for i, a in enumerate(ad_sets):
                a["budget_krw"] = share if i < len(ad_sets) - 1 else total - share * (len(ad_sets) - 1)
    risks = list(d.get("risks") or [])
    if categories & {c.value for c in FINANCIAL_CATEGORIES} and not any("심의" in r for r in risks):
        risks.append("금융 업종: 준법감시인 심의·심의필 없이는 심사 승인 불가")
        issues.append(PlanIssue("risks", "금융 업종 심의 리스크를 코드가 추가", True))
    plan = CampaignPlan(
        objective=objective, kpi=d["kpi"], total_budget_krw=total,
        ad_sets=[AdSet(**a) for a in ad_sets], planner_reasoning=d["planner_reasoning"],
        assumptions=list(d.get("assumptions") or []), risks=risks, repaired=any(i.repaired for i in issues),
    )
    return plan, issues


def plan_campaign(brief: CampaignBrief, effective_input: str, categories: set[str], description: str, router: Router) -> PlanResult:
    req = LLMRequest(
        task=Task.PLAN, system=PLANNER_SYSTEM, user=_user_prompt(brief, effective_input, categories, description),
        schema=PlanOut, max_tokens=1800, temperature=0.3,
        context={"effective_input": effective_input, "category": brief.product.category.value,
                 "monthly_budget_krw": brief.monthly_budget_krw, "product_name": brief.product.name},
        label="planner",
    )
    resp = router.complete(req)
    if resp.parsed is None:
        raise ValueError("플래너 출력이 비어 있음")
    plan, issues = validate_and_repair(resp.parsed, categories)
    return PlanResult(plan=plan, issues=issues, raw=resp.parsed.model_dump(), provider=resp.provider, model=resp.model)
