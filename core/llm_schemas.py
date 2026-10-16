"""LLM 에게 요구하는 출력 스키마.

규칙 (OpenAI Structured Outputs strict 모드 호환):
- 모든 필드 required, 기본값 없음, 숫자 범위·길이·pattern 제약 없음 (범위 검증은 내부 스키마 변환 시).
- 열거형은 Literal/Enum 으로 고정 → 모델이 카탈로그 밖의 rule_id 를 만들어 낼 수 없다 (FM-03 방어 1차).
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, create_model


class PlanAdSetOut(BaseModel):
    name: str
    audience: str
    placement: str
    budget_krw: int
    bid_strategy: str
    rationale: str


class PlanOut(BaseModel):
    objective: Literal["awareness", "traffic", "conversion", "app_install", "lead"]
    kpi: str
    total_budget_krw: int
    ad_sets: list[PlanAdSetOut]
    planner_reasoning: str
    assumptions: list[str]
    risks: list[str]


class ClaimOut(BaseModel):
    text: str
    fact_id: Optional[str]


class CopyOut(BaseModel):
    headline: str
    body: str
    cta: str
    claims: list[ClaimOut]
    disclosures: list[str]
    tone: str


class CopyBatchOut(BaseModel):
    variants: list[CopyOut]


class PersonaFeedbackOut(BaseModel):
    persona_id: str
    appeal: int
    trust: int
    clarity: int
    would_click: bool
    concern: str
    quote: str


class AudienceOut(BaseModel):
    feedbacks: list[PersonaFeedbackOut]
    summary: str


class RewriteOut(BaseModel):
    headline: str
    body: str
    cta: str
    disclosures: list[str]


class RedirectOut(BaseModel):
    """Safety Intercept 가 내는 '준수 제안'."""

    message: str
    suggested_input: str


class AttackOut(BaseModel):
    strategy: str
    headline: str
    body: str
    cta: str
    intent: str  # 어떤 규칙을 피하려 했는지 (공격자 자백 — 평가셋 라벨로 쓴다)


class AttackBatchOut(BaseModel):
    attacks: list[AttackOut]


def build_review_schema(rule_ids: list[str]):
    """카탈로그의 rule_id 만 허용하는 심사 출력 스키마를 동적으로 만든다."""
    if not rule_ids:
        raise ValueError("rule_ids 가 비어 있습니다")
    RuleId = Literal[tuple(rule_ids)]  # type: ignore[valid-type]

    FindingOut = create_model(
        "FindingOut",
        rule_id=(RuleId, ...),
        quote=(str, ...),
        explanation=(str, ...),
        __base__=BaseModel,
    )
    ReviewOut = create_model(
        "ReviewOut",
        decision=(Literal["APPROVE", "REVISE", "REJECT"], ...),
        confidence=(float, ...),
        findings=(list[FindingOut], ...),  # type: ignore[valid-type]
        rationale=(str, ...),
        rewrite=(Optional[RewriteOut], ...),
        __base__=BaseModel,
    )
    return ReviewOut


class JudgeOut(BaseModel):
    """소재 품질 LLM-as-judge (쌍대 비교). 위치 편향을 줄이기 위해 호출 측에서 순서를 뒤집어 두 번 묻는다."""

    winner: Literal["A", "B", "TIE"]
    reason: str
