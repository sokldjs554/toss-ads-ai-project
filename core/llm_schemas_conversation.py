"""대화형 캠페인 셋업(Task.CONVERSE)에 쓰는 모델 출력 스키마.

규칙 (OpenAI Structured Outputs strict 호환): 평평하고 전부 required, 기본값·범위 제약 없음, 열거는 Literal.
그래서 여기에는 값의 범위(예: 신뢰도 0~1)나 길이 제약이 없다 — 범위·존재 검증은 `agents/conversation.py` 가 코드로 한다.
모델이 돌려준 값은 '후보' 일 뿐이다: 근거 구절이 사용자 말에 글자 그대로 있는지, 숫자가 그 구절의 숫자와 같은지, 값이 닫힌 어휘에 속하는지를 코드가 대조해 통과한 것만 슬롯이 된다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SlotUpdateOut(BaseModel):
    slot: Literal["product_name", "category", "objective", "monthly_budget_krw", "audience", "period",
                  "landing_url", "key_conditions", "tone", "ad_set_count", "bid_strategy"]
    value: str              # 모든 값을 문자열 하나로. 예산은 원 단위 숫자만('3000000'), 업종·목표는 닫힌 어휘 값('loan', 'conversion')
    evidence_quote: str     # 이번 사용자 말에서 글자 그대로 복사한 구절
    confidence: float       # 0~1 자기 평가. 범위는 코드가 자른다


class ConverseOut(BaseModel):
    slot_updates: list[SlotUpdateOut]
    reply: str              # 광고주에게 보일 한두 문장 (질문은 넣지 않는다)
    questions: list[str]    # 참고용. 무엇을 되물을지는 코드가 정한다
