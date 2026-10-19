"""INSIGHT 대역 — 컨텍스트의 근거로 템플릿 문장을 만들어 InsightOut 스키마의 dict 로 돌려준다.

대역 출력은 모델 성능이 아니다. 같은 입력이면 같은 문장이고(결정론), 숫자와 근거 id 는 엔진이 계산한 근거 레코드에서 그대로 가져온다.
그래서 이 대역이 만든 보고서는 화면에서 '예시 실행: 문장은 템플릿이 만듭니다' 로 알린다.
"""
from __future__ import annotations

from core.insight_engine import template_narrative
from llm.base import LLMRequest


def handle(req: LLMRequest) -> dict:
    ctx = req.context.get("analysis")
    if not isinstance(ctx, dict) or "evidence" not in ctx:
        raise ValueError("INSIGHT 대역은 context['analysis'] (Analysis.to_context()) 가 필요합니다")
    return template_narrative(ctx)
