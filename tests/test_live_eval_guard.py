"""실제 API 평가가 실패·재시도 비용을 빠뜨리지 않고 승인 범위에서 멈추는지."""
from __future__ import annotations

import json

import pytest

from evals.live_guard import BoundedProvider, LiveLedger
from llm.base import LLMRequest, LLMResponse, Task
from llm.errors import BudgetExceeded, ProviderError, TruncatedOutput
from observability.pricing import price_for


MODEL = "gpt-5.4-mini-2026-03-17"


class Provider:
    name = "openai"
    model = MODEL
    available = True

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def complete(self, req):
        self.calls += 1
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return value


def request(**kw):
    return LLMRequest(Task.JUDGE, "평가 기준", "공개 합성 예시", max_tokens=400, **kw)


def response(**kw):
    base = dict(text="{}", parsed=None, provider="openai", model=MODEL, input_tokens=100, output_tokens=50)
    base.update(kw)
    return LLMResponse(**base)


def test_price_uses_longest_supported_model_prefix():
    assert price_for("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert price_for("gpt-4.1-mini-2025-04-14") == (0.40, 1.60)
    assert price_for(MODEL) == (0.75, 4.50)


def test_request_is_reserved_and_checkpointed_before_the_network_call():
    snapshots = []
    ledger = LiveLedger(1.0, 3, checkpoint=lambda state: snapshots.append(state))
    provider = Provider([response()])
    wrapped = BoundedProvider(provider, ledger)
    wrapped.complete(request())
    assert snapshots[0]["attempts"] == 1
    assert snapshots[0]["calls"][0]["status"] == "reserved"
    assert snapshots[0]["accounted_usd"] > snapshots[-1]["accounted_usd"] > 0
    assert snapshots[-1]["calls"][0]["status"] == "success"
    assert ledger.summary()["usage_cost_usd"] == pytest.approx(0.0003)


def test_insufficient_budget_blocks_before_network():
    provider = Provider([response()])
    wrapped = BoundedProvider(provider, LiveLedger(0.0001, 5))
    with pytest.raises(BudgetExceeded):
        wrapped.complete(request())
    assert provider.calls == 0


def test_failed_attempt_keeps_full_reservation_and_never_logs_error_text():
    provider = Provider([ProviderError("secret-key-should-never-appear", provider="openai")])
    ledger = LiveLedger(1.0, 5)
    with pytest.raises(ProviderError):
        BoundedProvider(provider, ledger).complete(request())
    result = ledger.summary()
    assert result["unknown_cost_calls"] == 1
    assert result["accounted_usd"] == result["calls"][0]["reserved_usd"] > 0
    assert result["usage_cost_usd"] == 0
    assert "secret-key" not in json.dumps(result)


def test_attempt_limit_includes_truncated_attempt_before_retry():
    provider = Provider([TruncatedOutput("truncated", provider="openai"), response()])
    wrapped = BoundedProvider(provider, LiveLedger(1.0, 1))
    with pytest.raises(TruncatedOutput):
        wrapped.complete(request())
    with pytest.raises(BudgetExceeded):
        wrapped.complete(request())
    assert provider.calls == 1


def test_unapproved_model_and_unbounded_request_are_rejected():
    provider = Provider([])
    provider.model = "new-model-with-unknown-price"
    with pytest.raises(ValueError, match="model"):
        BoundedProvider(provider, LiveLedger(1, 5))
    provider.model = MODEL
    wrapped = BoundedProvider(provider, LiveLedger(1, 5))
    req = request()
    req.max_tokens = 8193
    with pytest.raises(ValueError, match="output"):
        wrapped.complete(req)
    assert provider.calls == 0


@pytest.mark.parametrize("change", [{"cost_estimated": True}, {"model": "unexpected-model"}, {"input_tokens": -1}])
def test_missing_usage_or_provider_drift_is_not_counted_as_measured_success(change):
    ledger = LiveLedger(1, 5)
    wrapped = BoundedProvider(Provider([response(**change)]), ledger)
    with pytest.raises(BudgetExceeded):
        wrapped.complete(request())
    result = ledger.summary()
    assert result["unknown_cost_calls"] == 1
    assert result["calls"][0]["status"] == "unverified_usage"


@pytest.mark.parametrize("cap,calls", [(0, 2), (-1, 2), (float("inf"), 2), (1, 0)])
def test_invalid_limits_are_rejected(cap, calls):
    with pytest.raises(ValueError):
        LiveLedger(cap, calls)
