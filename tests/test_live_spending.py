import pytest

from core.config import Settings
from llm.base import LLMRequest, LLMResponse, Task
from llm.errors import BudgetExceeded, Timeout, ProviderUnavailable, TruncatedOutput
from llm.router import Router
from tests.fakes import FakeProvider


def real_factory(monkeypatch, **kw):
    provider = FakeProvider("openai", model="gpt-4o", **kw)
    monkeypatch.setattr(Router, "_default_providers", staticmethod(lambda settings: {"openai":provider}))
    return provider


def test_default_provider_is_never_called_if_worst_case_request_exceeds_cap(monkeypatch):
    provider = real_factory(monkeypatch)
    router = Router(Settings.from_env(openai_api_key="fake",anthropic_api_key="",force_demo=False,run_budget_usd=.001))
    with pytest.raises(BudgetExceeded):
        router.complete(LLMRequest(task=Task.REVIEW,system="",user="",max_tokens=200))
    assert provider.calls == 0
    assert not router.budget.estimated and router.budget.failed_attempts == 0


def test_failed_default_provider_keeps_reservation_before_retry(monkeypatch):
    provider = real_factory(monkeypatch, errors=[Timeout("offline")])
    router = Router(Settings.from_env(openai_api_key="fake",anthropic_api_key="",force_demo=False,run_budget_usd=.02))
    req=LLMRequest(task=Task.REVIEW,system="",user="",max_tokens=500)
    with pytest.raises(ProviderUnavailable):
        router.complete(req)
    with pytest.raises(BudgetExceeded):
        router.complete(req)
    assert provider.calls == 1


def test_truncation_retry_reserves_the_larger_output_before_sending(monkeypatch):
    provider=real_factory(monkeypatch,errors=[TruncatedOutput("offline")])
    router=Router(Settings.from_env(openai_api_key="fake",anthropic_api_key="",force_demo=False,run_budget_usd=.03))
    with pytest.raises(BudgetExceeded):
        router.complete(LLMRequest(task=Task.REVIEW,system="",user="",max_tokens=500))
    assert provider.calls == 1


def test_verified_usage_releases_unused_reservation(monkeypatch):
    provider=real_factory(monkeypatch)
    router=Router(Settings.from_env(openai_api_key="fake",anthropic_api_key="",force_demo=False,run_budget_usd=.02))
    for _ in range(2):
        router.complete(LLMRequest(task=Task.REVIEW,system="",user="",max_tokens=500))
    assert provider.calls == 2
    assert router.spending.accounted == pytest.approx(.00015)


def test_missing_usage_is_not_treated_as_free(monkeypatch):
    provider=real_factory(monkeypatch,behavior=lambda req:LLMResponse(text="",parsed=None,provider="openai",model="gpt-4o",cost_estimated=True))
    router=Router(Settings.from_env(openai_api_key="fake",anthropic_api_key="",force_demo=False,run_budget_usd=.02))
    req=LLMRequest(task=Task.REVIEW,system="",user="",max_tokens=500)
    for _ in range(2):
        with pytest.raises(BudgetExceeded):
            router.complete(req)
    assert provider.calls == 1
    assert router.budget.estimated and router.budget.failed_attempts == 1


def test_unpriced_model_is_withheld_before_any_request(monkeypatch):
    provider=real_factory(monkeypatch)
    provider.model="unknown-model"
    router=Router(Settings.from_env(openai_api_key="fake",anthropic_api_key="",force_demo=False,run_budget_usd=1))
    with pytest.raises(BudgetExceeded):
        router.complete(LLMRequest(task=Task.REVIEW,system="",user="",max_tokens=500))
    assert provider.calls == 0
