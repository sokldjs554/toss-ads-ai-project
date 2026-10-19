"""CONVERSE 대역 — 모델 대신 결정론 규칙 추출기를 불러 같은 모양의 응답을 만든다.

그래서 키가 없을 때(DEMO)의 대화 = 규칙 기반 경로다. 이것은 모델 성능이 아니다 — MockProvider 가 provider="mock" 을 찍고 화면이 '예시 실행(규칙 기반)' 이라고 알린다.
응답은 모델 출력과 같은 경로(근거 구절 대조·숫자 대조·닫힌 어휘 검사)를 다시 거친다.
"""
from __future__ import annotations

from llm.base import LLMRequest


def handle(req: LLMRequest) -> dict:
    from agents import conversation_rules as R      # 지연 import — llm 패키지가 agents 를 import 할 때의 순환을 피한다

    ctx = req.context
    text = str(ctx.get("user_message", ""))
    ex = R.extract(text, R.ExtractContext(last_asked=frozenset(ctx.get("last_asked") or []), has_budget=bool(ctx.get("has_budget"))))
    updates = []
    for c in ex.candidates:
        if c.delta is not None:
            continue                  # 상대 수정('예산 20% 줄여줘')은 현재 값이 있어야 계산된다 — 규칙 경로가 처리한다
        updates.append({"slot": c.slot, "value": R.value_to_text(c.slot, c.value), "evidence_quote": c.quote,
                        "confidence": 0.9 if c.status == "confirmed" else 0.7})
    reply = "[대역] 알려주신 내용을 규칙대로 정리했어요." if updates else ""
    return {"slot_updates": updates, "reply": reply, "questions": []}
