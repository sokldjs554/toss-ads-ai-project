"""Exact-model SDK boundary with preflight reservations and no automatic retries."""
import json
import time

from agents.live_pilot import MODEL
from observability.pricing import estimate_cost_usd


class LiveGateway:
    def __init__(self, api_key, store, token, request_id, *, client=None):
        if not api_key:
            raise ValueError("provider_not_configured")
        self.api_key, self.store, self.token, self.request_id = api_key, store, token, request_id
        self.client = client

    def __call__(self, *, stage, **kwargs):
        wire = json.dumps(kwargs, ensure_ascii=False).encode()
        if len(wire) > 90000:
            raise ValueError("request_size_limit")
        if self.client is None:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key, timeout=30.0, max_retries=0)
        sequence = self.store.start_call(self.token, self.request_id, stage)
        started = time.perf_counter()
        record = {"status": "failed", "reason": "provider_failure", "model": MODEL, "cost_usd": None}
        try:
            response = self.client.chat.completions.create(model=MODEL, max_completion_tokens=1200, temperature=0, **kwargs)
            usage = response.usage
            itok, otok = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
            if (response.model != MODEL or type(itok) is not int or type(otok) is not int
                    or not 0 <= itok <= len(wire) + 4096 or not 0 <= otok <= 1200):
                record.update(status="usage_unknown", reason="usage_or_model_unverified")
                raise ValueError("usage_or_model_unverified")
            record.update(input_tokens=itok, output_tokens=otok, cost_usd=estimate_cost_usd(MODEL, itok, otok))
            choice = response.choices[0]
            if choice.finish_reason not in {"stop", "tool_calls"} or getattr(choice.message, "refusal", None):
                record["reason"] = "refusal_or_truncated"
                raise ValueError("refusal_or_truncated")
            record.update(status="success", reason="")
            return choice.message.model_dump(include={"role", "content", "tool_calls"}, exclude_none=True)
        except Exception as exc:
            if type(exc).__name__ in {"APITimeoutError", "TimeoutError", "ReadTimeout"}:
                record["reason"] = "provider_timeout"
                raise TimeoutError("provider_timeout") from None
            raise
        finally:
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.store.finish_call(self.token, self.request_id, sequence, record)
