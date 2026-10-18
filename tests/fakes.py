"""테스트용 가짜 제공자. 네트워크 없이 실패 모드를 재현한다."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from llm.base import LLMRequest, LLMResponse


@dataclass
class FakeProvider:
    name: str
    model: str = "fake"
    behavior: Callable[[LLMRequest], LLMResponse] | None = None
    errors: list[Exception] = field(default_factory=list)  # 앞에서부터 하나씩 던진다
    calls: int = 0
    available: bool = True

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.behavior is not None:
            return self.behavior(req)
        text = '{"ok": true}'
        parsed = req.schema.model_validate_json(text) if req.schema is not None else None
        return LLMResponse(text=text, parsed=parsed, provider=self.name, model=self.model, input_tokens=10, output_tokens=5,
                           latency_ms=1.0, cost_usd=0.001, finish_reason="stop")


def scripted_review_provider(response, name: str = "openai") -> "FakeProvider":
    """통제된 심사 응답을 내는 가짜 제공자. response 는 심사 출력 dict(decision·confidence·findings·rationale·rewrite) 이거나
    예외 인스턴스(호출이 실패하는 상황), 또는 요청을 받아 위 둘 중 하나를 돌려주는 함수. 실제 모델 품질 측정이 아니라 판단 기준 검증용이다."""

    def behave(req: LLMRequest) -> LLMResponse:
        r = response(req) if callable(response) else response
        if isinstance(r, Exception):
            raise r
        parsed = req.schema.model_validate(r)
        return LLMResponse(text="{}", parsed=parsed, provider=name, model="controlled", input_tokens=10, output_tokens=5,
                           latency_ms=1.0, cost_usd=0.001, finish_reason="stop")

    return FakeProvider(name, model="controlled", behavior=behave)
