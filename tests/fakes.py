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


class TaskScriptedProvider:
    """작업(Task)마다 응답을 정해 두는 가짜 제공자. 응답이 없는 작업(캠페인 구성·광고 초안·가상 반응 등)은 대역(mock)이 맡는다.
    handler: 심사 출력 dict · 예외 인스턴스 · 요청을 받아 그 둘 중 하나를 돌려주는 함수. 실제 모델 품질 측정이 아니라 판단 규칙 검증용이다."""

    available = True

    def __init__(self, name: str, handlers: dict | None = None, model: str = "controlled", cost: float = 0.001):
        from llm.mock_provider import MockProvider

        self.name, self.model, self.handlers, self.cost = name, model, handlers or {}, cost
        self.calls: list[tuple] = []
        self._mock = MockProvider()

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append((req.task, req.label))
        h = self.handlers.get(req.task)
        if h is None:
            r = self._mock.complete(req)
            r.provider, r.model, r.cost_usd = self.name, self.model, self.cost
            return r
        r = h(req) if callable(h) else h
        if isinstance(r, Exception):
            raise r
        parsed = req.schema.model_validate(r)
        return LLMResponse(text="{}", parsed=parsed, provider=self.name, model=self.model, input_tokens=10, output_tokens=5, latency_ms=1.0, cost_usd=self.cost,
                           finish_reason="stop")

    def count(self, task) -> int:
        return sum(1 for t, _ in self.calls if t == task)
