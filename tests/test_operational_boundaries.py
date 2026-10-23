"""Offline production boundary tests; no provider credentials or requests."""
import json

import pytest

from agents.recheck import recheck
from core.config import Settings
from core.schemas import AdCopy, ProductFacts
from llm.base import LLMRequest, Task
from llm.errors import ProviderUnavailable
from llm.mock_provider import MockProvider
from llm.router import Router
from observability.tracer import Tracer


def unavailable_router(tracer):
    return Router(Settings.from_env(openai_api_key="test-not-a-real-key", anthropic_api_key="", force_demo=False),
                  providers={"mock": MockProvider()}, tracer=tracer)


def test_live_without_available_provider_cannot_substitute_demo():
    tracer = Tracer()
    with tracer.trace("offline-live-boundary", mode="partial"):
        with pytest.raises(ProviderUnavailable):
            unavailable_router(tracer).complete(LLMRequest(task=Task.REVIEW, system="", user=""))


def test_unavailable_live_recheck_holds_and_is_not_reusable():
    tracer = Tracer()
    router = unavailable_router(tracer)
    result = recheck(AdCopy(variant_id="test", headline="상품 정보를 확인하세요", body="", cta=""),
                     ProductFacts(name="synthetic"), set(), router.settings, router=router, tracer=tracer)
    assert result.report.final_decision.value == "HUMAN_REVIEW"
    assert result.failed and not result.reusable
    assert result.calls == 0


def test_shared_operational_record_excludes_free_text_and_keeps_measurements():
    tracer = Tracer()
    with tracer.trace("preflight", mode="partial", prompt_version="2026.10.4", policy_version="2026.10.7", product="PRIVATE PRODUCT") as trace:
        with tracer.span("reviewer", kind="llm", provider="openai", model="gpt-4o", prompt_user="PRIVATE INPUT", response_text="PRIVATE OUTPUT",
                         input_tokens=12, output_tokens=7, cost_usd=.003, cost_estimated=False) as span:
            span.event("provider_error", type="Timeout", error="Bearer SECRET-KEY PRIVATE INPUT")
    public = trace.operational_record()
    text = json.dumps(public)
    assert all(secret not in text for secret in ("PRIVATE", "SECRET-KEY", "Bearer"))
    assert public["trace_id"] == trace.trace_id
    assert public["metadata"]["policy_version"] == "2026.10.7"
    assert public["spans"][1]["metrics"]["input_tokens"] == 12
    assert public["spans"][1]["events"][0] == {"name": "provider_error", "type": "Timeout"}
    assert public["duration_ms"] >= 0
