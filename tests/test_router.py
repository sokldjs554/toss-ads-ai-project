import pytest
from pydantic import BaseModel

from core.config import Settings
from llm.base import LLMRequest, LLMResponse, Task
from llm.errors import BudgetExceeded, ModelRefusal, ProviderUnavailable, RateLimited, SchemaMismatch, Timeout, TruncatedOutput
from llm.router import CircuitBreaker, Router
from tests.fakes import FakeProvider


class Out(BaseModel):
    ok: bool


def settings(**kw) -> Settings:
    return Settings.from_env(openai_api_key="k", anthropic_api_key="k", **kw)


def router(oa: FakeProvider, an: FakeProvider, tracer, **kw) -> Router:
    return Router(settings(**kw), providers={"openai": oa, "anthropic": an}, tracer=tracer)


def req(task=Task.REVIEW, schema=Out) -> LLMRequest:
    return LLMRequest(task=task, system="s", user="u", schema=schema)


def test_timeout_falls_back(tracer):
    oa = FakeProvider("openai", errors=[Timeout("t", provider="openai")])
    an = FakeProvider("anthropic")
    with tracer.trace("t"):
        resp = router(oa, an, tracer).complete(req())
    assert resp.provider == "anthropic" and resp.fallback_chain == ["openai", "anthropic"]
    span = tracer.latest().llm_spans()[0]
    assert span.status == "fallback" and any(e["name"] == "provider_error" for e in span.events)


def test_cross_provider_fallback_for_creative_task(tracer):
    oa = FakeProvider("openai")
    an = FakeProvider("anthropic", errors=[RateLimited("429", provider="anthropic")])
    with tracer.trace("t"):
        resp = router(oa, an, tracer).complete(req(Task.COPY))
    assert resp.provider == "openai"


def test_circuit_breaker_opens(tracer):
    oa = FakeProvider("openai", errors=[Timeout("t", provider="openai")] * 3)
    an = FakeProvider("anthropic")
    r = router(oa, an, tracer)
    with tracer.trace("t"):
        for _ in range(3):
            r.complete(req())
        assert r.breakers["openai"].is_open
        r.complete(req())
    assert oa.calls == 3  # 네 번째 호출은 서킷이 열려 건너뛴다


def test_refusal_is_typed_and_falls_back(tracer):
    oa = FakeProvider("openai", errors=[ModelRefusal("no", provider="openai")])
    an = FakeProvider("anthropic")
    with tracer.trace("t"):
        resp = router(oa, an, tracer).complete(req())
    assert resp.provider == "anthropic"
    assert any(e["name"] == "refusal" for e in tracer.latest().llm_spans()[0].events)


def test_all_fail_raises_provider_unavailable(tracer):
    oa = FakeProvider("openai", errors=[Timeout("t", provider="openai")])
    an = FakeProvider("anthropic", errors=[Timeout("t", provider="anthropic")])
    with tracer.trace("t"):
        with pytest.raises(ProviderUnavailable):
            router(oa, an, tracer).complete(req())


def test_repair_path(tracer):
    """창의 제공자가 JSON 을 깨뜨리면 구조화 제공자가 수리한다 (FM-01)."""
    an = FakeProvider("anthropic", errors=[SchemaMismatch("bad", provider="anthropic", raw_text="ok: true (not json)")])
    oa = FakeProvider("openai")
    with tracer.trace("t"):
        resp = router(oa, an, tracer).complete(req(Task.COPY))
    assert resp.repaired and resp.provider == "openai" and "repair" in resp.fallback_chain
    assert any(e["name"] == "repaired" for e in tracer.latest().llm_spans()[0].events)


def test_truncation_retry_doubles_max_tokens(tracer):
    seen = []

    def behave(r: LLMRequest) -> LLMResponse:
        seen.append(r.max_tokens)
        return LLMResponse(text='{"ok": true}', parsed=Out(ok=True), provider="openai", model="fake")

    oa = FakeProvider("openai", behavior=behave, errors=[TruncatedOutput("len", provider="openai")])
    with tracer.trace("t"):
        router(oa, FakeProvider("anthropic"), tracer).complete(LLMRequest(task=Task.REVIEW, system="s", user="u", schema=Out, max_tokens=100))
    assert seen == [200]


def test_budget_cap(tracer):
    oa = FakeProvider("openai")
    r = router(oa, FakeProvider("anthropic"), tracer, run_budget_usd=0.0025)
    with tracer.trace("t"):
        r.complete(req()); r.complete(req()); r.complete(req())  # 0.003 누적
        with pytest.raises(BudgetExceeded):
            r.complete(req())


def test_demo_mode_uses_mock(tracer):
    r = Router(Settings.from_env(openai_api_key="", anthropic_api_key=""), tracer=tracer)
    assert r.chain_for(Task.REVIEW) == ["mock"]


def test_live_mode_never_uses_mock(tracer):
    r = router(FakeProvider("openai"), FakeProvider("anthropic"), tracer)
    assert "mock" not in r.chain_for(Task.REVIEW)


def test_simulated_failure_drill(tracer):
    oa, an = FakeProvider("openai"), FakeProvider("anthropic")
    r = router(oa, an, tracer)
    r.simulate_failure("openai", "timeout")
    with tracer.trace("t"):
        resp = r.complete(req())
    assert resp.provider == "anthropic" and oa.calls == 0


def test_breaker_half_open_after_cooldown():
    b = CircuitBreaker(threshold=1, cooldown_s=0.0)
    b.failure()
    assert not b.is_open  # cooldown 0 → 바로 반개방
