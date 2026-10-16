"""Anthropic 제공자 — 창의 생성·정성 시뮬레이션 담당.

- 창의 작업(COPY/AUDIENCE/REDIRECT/ATTACK): 제약 없이 자유 텍스트로 생성 → 방어적 JSON 추출 → 스키마 검증.
  실패하면 SchemaMismatch(raw_text) 를 던지고, 라우터가 구조화 제공자에게 수리를 맡긴다 (FM-01).
- 구조화 작업으로 fallback 될 때(OpenAI 장애, FM-07): `messages.parse(output_format=schema)` 로 제약 디코딩.
- 현 세대 모델은 temperature 등 샘플링 파라미터를 받지 않으므로 보내지 않는다.
"""
from __future__ import annotations

import time
from typing import Optional

from llm.base import LLMRequest, LLMResponse
from llm.errors import ModelRefusal, ProviderError, RateLimited, SchemaMismatch, Timeout, TruncatedOutput
from llm.json_utils import extract_json
from observability.pricing import estimate_cost_usd

_EFFORT_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-haiku-5", "claude-fable", "claude-opus-4-6",
                    "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_s: float = 45.0, max_retries: int = 2,
                 effort: str = "medium") -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._effort = effort
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self._api_key, timeout=self._timeout, max_retries=self._max_retries)
        return self._client

    def _common_kwargs(self, req: LLMRequest) -> dict:
        kwargs: dict = dict(
            model=self.model, max_tokens=req.max_tokens, system=req.system,
            messages=[{"role": "user", "content": req.user}],
        )
        if self._effort and self.model.startswith(_EFFORT_PREFIXES):
            kwargs["output_config"] = {"effort": self._effort}
        return kwargs

    def complete(self, req: LLMRequest) -> LLMResponse:
        import anthropic

        client = self._get_client()
        t0 = time.perf_counter()
        try:
            if req.constrained:
                message = client.messages.parse(output_format=req.schema, **self._common_kwargs(req))
            else:
                message = client.messages.create(**self._common_kwargs(req))
        except anthropic.APITimeoutError as e:
            raise Timeout(str(e), provider=self.name) from e
        except anthropic.RateLimitError as e:
            raise RateLimited(str(e), provider=self.name) from e
        except anthropic.APIStatusError as e:
            if e.status_code in (529, 503):
                raise RateLimited(f"overloaded {e.status_code}", provider=self.name) from e
            raise ProviderError(f"HTTP {e.status_code}: {e.message}", provider=self.name, status=e.status_code) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"connection: {e}", provider=self.name) from e
        latency = (time.perf_counter() - t0) * 1000

        if message.stop_reason == "refusal":
            detail = getattr(getattr(message, "stop_details", None), "explanation", "") or "refusal"
            raise ModelRefusal(str(detail), provider=self.name)
        if message.stop_reason == "max_tokens":
            raise TruncatedOutput("stop_reason=max_tokens", provider=self.name)

        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        parsed: Optional[object] = None
        if req.schema is not None:
            parsed = getattr(message, "parsed_output", None) if req.constrained else None
            if parsed is None:
                try:
                    parsed = req.schema.model_validate(extract_json(text))
                except Exception as e:  # noqa: BLE001
                    raise SchemaMismatch(str(e), provider=self.name, raw_text=text) from e
        itok = message.usage.input_tokens or 0
        otok = message.usage.output_tokens or 0
        return LLMResponse(
            text=text, parsed=parsed, provider=self.name, model=self.model,
            input_tokens=itok, output_tokens=otok, latency_ms=round(latency, 1),
            cost_usd=estimate_cost_usd(self.model, itok, otok), finish_reason=message.stop_reason or "",
        )
