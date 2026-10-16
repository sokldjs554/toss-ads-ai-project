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
