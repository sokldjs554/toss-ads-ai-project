from types import SimpleNamespace as NS
import pytest
from llm.live_pilot_gateway import LiveGateway


class Ledger:
    def __init__(self): self.events = []
    def start_call(self, *args): self.events.append("reserved"); return 1
    def finish_call(self, *args): self.events.append(args[-1])


def sdk(response, ledger):
    def create(**kwargs):
        assert ledger.events == ["reserved"]
        if isinstance(response, Exception): raise response
        return response
    return NS(chat=NS(completions=NS(create=create)))


def response(**overrides):
    d = dict(model="gpt-4o-mini-2024-07-18", usage=NS(prompt_tokens=100, completion_tokens=20),
             choices=[NS(finish_reason="stop", message=NS(refusal=None, model_dump=lambda **kw: {"role":"assistant", "content":"{}"}))])
    return NS(**{**d, **overrides})


@pytest.mark.parametrize("r", [response(model="other"), response(usage=None), response(usage=NS(prompt_tokens=-1,completion_tokens=2))])
def test_unverified_usage_is_recorded_and_cannot_produce_success(r):
    ledger = Ledger()
    gateway = LiveGateway("fake", ledger, "credential", "request", client=sdk(r, ledger))
    with pytest.raises(ValueError): gateway(messages=[], stage="rag")
    assert ledger.events[-1]["status"] == "usage_unknown"
    assert ledger.events[-1]["cost_usd"] is None


def test_success_uses_actual_model_usage_and_writes_cost_before_return():
    ledger = Ledger()
    gateway = LiveGateway("fake", ledger, "credential", "request", client=sdk(response(), ledger))
    gateway(messages=[], stage="rag")
    saved = ledger.events[-1]
    assert saved["model"] == "gpt-4o-mini-2024-07-18"
    assert saved["input_tokens"] == 100 and saved["output_tokens"] == 20
    assert saved["cost_usd"] == .000027


def test_oversize_request_never_reserves_or_calls_provider():
    ledger = Ledger()
    gateway = LiveGateway("fake", ledger, "credential", "request", client=sdk(response(), ledger))
    with pytest.raises(ValueError): gateway(messages=[{"role":"user","content":"x"*100000}], stage="rag")
    assert ledger.events == []
