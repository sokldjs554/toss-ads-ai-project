"""작업 → 제공자 라우팅, fallback 체인, 서킷브레이커, 예산 상한, 수리 경로.

호출 한 번은 반드시 트레이스에 'llm' 스팬 하나로 남는다 (어떤 제공자를 시도했고 왜 넘어갔는지 포함).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from core.config import Mode, Settings
from llm.base import ROUTING, LLMRequest, LLMResponse, Provider, Task
from llm.errors import (
    BudgetExceeded,
    CircuitOpen,
    LLMError,
    ModelRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    SchemaMismatch,
    Timeout,
    TruncatedOutput,
)
from observability.tracer import Tracer, get_tracer

_TRANSIENT = (Timeout, RateLimited, ProviderError)


@dataclass
class CircuitBreaker:
    """연속 실패 N회 → T초 동안 open. 반개방(half-open) 상태에서 1회 성공하면 닫힌다."""

    threshold: int = 3
    cooldown_s: float = 60.0
    failures: int = 0
    opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at >= self.cooldown_s:
            return False  # half-open: 한 번 시도 허용
        return True

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.time()


@dataclass
class RunBudget:
    """실행 하나의 비용·호출 상한.

    - attempts: 실제로 제공자에게 보낸 요청 수 (실패·재시도·수리 포함). 호출 상한은 이 수에 건다.
    - calls: 성공 응답 수.
    - failed_attempts: 응답(사용량)을 받지 못한 요청 수 — 비용을 알 수 없으므로 0원으로 확정하지 않고 '추정' 으로 표시한다.
    """

    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    attempts: int = 0
    failed_attempts: int = 0
    max_calls: int = 60
    estimated: bool = False

    def check(self) -> None:
        if self.spent_usd >= self.cap_usd:
            raise BudgetExceeded(f"실행 예산 초과: ${self.spent_usd:.4f} ≥ ${self.cap_usd:.2f}")
        if self.attempts >= self.max_calls:
            raise BudgetExceeded(f"호출 횟수 상한 초과: 요청 {self.attempts}회 ≥ {self.max_calls}회")

    def attempt(self) -> None:
        """제공자에게 요청을 보내기 '직전' 에 부른다 — 재시도·수리 요청도 예외 없이."""
        self.check()
        self.attempts += 1

    def add(self, resp: "LLMResponse") -> None:
        self.spent_usd += float(resp.cost_usd)
        self.calls += 1
        if resp.cost_estimated:
            self.estimated = True

    def failed(self) -> None:
        self.failed_attempts += 1
        self.estimated = True


class Router:
    def __init__(self, settings: Settings, providers: Optional[dict[str, Provider]] = None,
                 tracer: Optional[Tracer] = None) -> None:
        self.settings = settings
        self.tracer = tracer or get_tracer()
        self.providers: dict[str, Provider] = providers if providers is not None else self._default_providers(settings)
        self.breakers: dict[str, CircuitBreaker] = {name: CircuitBreaker() for name in self.providers}
        self.budget = RunBudget(cap_usd=settings.run_budget_usd)
        self.simulated_failures: dict[str, str] = {}  # provider → "timeout"|"rate_limit"|"refusal"|"schema" (장애 훈련용)

    @staticmethod
    def _default_providers(s: Settings) -> dict[str, Provider]:
        from llm.anthropic_provider import AnthropicProvider
        from llm.mock_provider import MockProvider
        from llm.openai_provider import OpenAIProvider

        provs: dict[str, Provider] = {}
        if s.has_openai:
            provs["openai"] = OpenAIProvider(s.openai_api_key, s.openai_model, s.request_timeout_s, s.max_retries)
        if s.has_anthropic:
            provs["anthropic"] = AnthropicProvider(s.anthropic_api_key, s.anthropic_model, s.request_timeout_s, s.max_retries)
        provs["mock"] = MockProvider()  # 항상 마지막 보루 — 단, LIVE 모드에서는 체인에 넣지 않는다
        return provs

    @property
    def mode(self) -> Mode:
        return self.settings.mode

    def new_run(self) -> None:
        """파이프라인 실행마다 예산을 초기화한다."""
        self.budget = RunBudget(cap_usd=self.settings.run_budget_usd)

    def chain_for(self, task: Task) -> list[str]:
        chain = [p for p in ROUTING[task] if p in self.providers and self.providers[p].available]
        if self.mode == Mode.DEMO or not chain:
            chain.append("mock")
        return chain

    # ------------------------------------------------------------------
    def complete(self, req: LLMRequest) -> LLMResponse:
        chain = self.chain_for(req.task)
        tried: list[str] = []
        errors: list[str] = []
        label = req.label or f"llm:{req.task.value}"
        with self.tracer.span(label, kind="llm", task=req.task.value, chain=chain) as span:
            span.set(prompt_system=req.system, prompt_user=req.user, schema=req.schema.__name__ if req.schema else None,
                     constrained=req.constrained, max_tokens=req.max_tokens)
            for name in chain:
                provider = self.providers[name]
                breaker = self.breakers[name]
                if breaker.is_open:
                    errors.append(f"{name}: circuit open")
                    span.event("skip_provider", provider=name, reason="circuit_open")
                    continue
                tried.append(name)
                try:
                    resp = self._call(provider, req, span)
                    breaker.success()
                    resp.fallback_chain = tried
                    self._record(span, resp, tried)
                    return resp
                except BudgetExceeded:
                    span.status = "error"
                    raise
                except SchemaMismatch as e:
                    # 창의 제공자가 형식을 어겼다 → 구조화 제공자에게 수리 (FM-01). 수리도 실패하면 다음 제공자.
                    span.event("schema_mismatch", provider=name, error=str(e)[:200])
                    repaired = self._try_repair(req, e.raw_text, span, exclude=name)
                    if repaired is not None:
                        repaired.fallback_chain = tried + ["repair"]
                        repaired.repaired = True
                        self._record(span, repaired, repaired.fallback_chain)
                        span.status = "fallback"
                        return repaired
                    errors.append(f"{name}: schema mismatch")
                except ModelRefusal as e:
                    span.event("refusal", provider=name, detail=str(e)[:200])
                    errors.append(f"{name}: refusal")
                except TruncatedOutput as e:
                    span.event("truncated", provider=name)
                    errors.append(f"{name}: truncated ({e})")
                except _TRANSIENT as e:
                    breaker.failure()
                    span.event("provider_error", provider=name, type=type(e).__name__, error=str(e)[:200])
                    errors.append(f"{name}: {type(e).__name__}")
                except LLMError as e:
                    errors.append(f"{name}: {type(e).__name__}: {e}")
                    span.event("provider_error", provider=name, type=type(e).__name__, error=str(e)[:200])
            span.status = "error"
            raise ProviderUnavailable("모든 제공자 실패 → " + "; ".join(errors))

    def simulate_failure(self, provider: str, kind: Optional[str]) -> None:
        """장애 훈련(outage drill): 해당 제공자의 호출을 지정한 방식으로 실패시킨다. kind=None 이면 해제."""
        if kind:
            self.simulated_failures[provider] = kind
        else:
            self.simulated_failures.pop(provider, None)

    def _call(self, provider: Provider, req: LLMRequest, span) -> LLMResponse:
        """잘림(FM-16)은 max_tokens 2배로 한 번만 재시도."""
        kind = self.simulated_failures.get(provider.name)
        if kind:
            span.event("simulated_failure", provider=provider.name, kind=kind)
            if kind == "timeout":
                raise Timeout("simulated timeout", provider=provider.name)
            if kind == "rate_limit":
                raise RateLimited("simulated 429", provider=provider.name)
            if kind == "refusal":
                raise ModelRefusal("simulated refusal", provider=provider.name)
            if kind == "schema":
                raise SchemaMismatch("simulated invalid json", provider=provider.name, raw_text="{not json")
        self.budget.attempt()
        try:
            try:
                resp = provider.complete(req)
            except TruncatedOutput:
                # 재시도도 요청 1회다 — 상한을 다시 확인하고 센다
                self.budget.attempt()
                span.event("truncation_retry", provider=provider.name, max_tokens=req.max_tokens * 2)
                bigger = LLMRequest(task=req.task, system=req.system, user=req.user, schema=req.schema,
                                    max_tokens=req.max_tokens * 2, temperature=req.temperature, context=req.context, label=req.label)
                resp = provider.complete(bigger)
        except BudgetExceeded:
            raise
        except LLMError:
            self.budget.failed()  # 응답을 못 받은 요청: 비용 미확인 → 추정으로 표시
            raise
        self.budget.add(resp)
        span.set(budget_attempts=self.budget.attempts, budget_calls=self.budget.calls, budget_spent_usd=round(self.budget.spent_usd, 6))
        return resp

    def _try_repair(self, req: LLMRequest, raw_text: str, span, exclude: str) -> Optional[LLMResponse]:
        if req.schema is None or not raw_text.strip():
            return None
        repair_req = LLMRequest(
            task=Task.REPAIR,
            system="너는 형식 정규화기다. 아래 텍스트에 담긴 내용을 바꾸지 말고 요구된 JSON 스키마로만 옮겨라. 없는 정보는 지어내지 말고 빈 값으로 둬라.",
            user=raw_text, schema=req.schema, max_tokens=req.max_tokens, temperature=0.0,
            context={"raw_text": raw_text}, label=f"repair:{req.task.value}",
        )
        for name in self.chain_for(Task.REPAIR):
            if name == exclude or name not in self.providers:
                continue
            try:
                resp = self._call(self.providers[name], repair_req, span)
                span.event("repaired", by=name)
                return resp
            except BudgetExceeded:
                raise
            except LLMError as e:
                span.event("repair_failed", by=name, error=str(e)[:200])
        return None

    @staticmethod
    def _record(span, resp: LLMResponse, chain: list[str]) -> None:
        span.set(provider=resp.provider, model=resp.model, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                 cost_usd=resp.cost_usd, cost_estimated=resp.cost_estimated, latency_ms=resp.latency_ms, finish_reason=resp.finish_reason,
                 fallback_chain=chain, repaired=resp.repaired, response_text=resp.text[:6000])
        if len(chain) > 1:
            span.status = "fallback"
