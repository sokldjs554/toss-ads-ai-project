"""성과 리포트 문장화(Task.INSIGHT)의 모델 출력 스키마.

OpenAI Structured Outputs(strict) 호환: 모든 필드 required, 기본값·범위 제약 없음.
액션 이름은 닫힌 카탈로그의 Literal 로 고정해 모델이 카탈로그 밖의 액션을 만들 수 없게 한다(1차 방어).
스키마를 통과해도 코드가 다시 대조한다 — 비구조화 fallback 경로에서는 스키마가 강제되지 않는다(2차 방어: agents/insights.py).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from core.insight_engine import CATALOG

ActionId = Literal[tuple(CATALOG)]  # type: ignore[valid-type]


class InsightFindingOut(BaseModel):
    text: str
    evidence_ids: list[str]


class InsightActionOut(BaseModel):
    action_id: ActionId  # type: ignore[valid-type]
    why: str
    evidence_ids: list[str]


class InsightOut(BaseModel):
    headline: str
    summary: str
    findings: list[InsightFindingOut]
    actions: list[InsightActionOut]
