"""OpenAI 제공자 — 구조화 출력(Structured Outputs) 담당.

`chat.completions.parse(response_format=PydanticModel)` 은 strict JSON Schema 로 제약 디코딩을 하므로
스키마 위반 출력이 '생성될 수 없다'. 그래도 값 수준 검증(예산 합, 인용 존재)은 호출 측이 다시 한다.
"""
from __future__ import annotations

import time
from typing import Optional

from llm.base import LLMRequest, LLMResponse, estimate_tokens
from llm.errors import ModelRefusal, ProviderError, RateLimited, SchemaMismatch, Timeout, TruncatedOutput
from llm.json_utils import extract_json
from observability.pricing import estimate_cost_usd

_TEMPERATURE_OK_PREFIXES = ("gpt-4o", "gpt-4.1", "gpt-4-", "gpt-3.5")


def _user_content(req: LLMRequest):
    """이미지가 없으면 글 그대로, 있으면 글 + 이미지(data URL) 조각 목록."""
    if not req.images:
        return req.user
    import base64

    parts: list[dict] = [{"type": "text", "text": req.user}]
    for img in req.images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{base64.b64encode(img.data).decode('ascii')}", "detail": "high"}})
    return parts


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout_s: float = 45.0, max_retries: int = 2) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key, timeout=self._timeout, max_retries=self._max_retries)
        return self._client

    def complete(self, req: LLMRequest) -> LLMResponse:
        import openai

        client = self._get_client()
        messages = [{"role": "system", "content": req.system}, {"role": "user", "content": _user_content(req)}]
        kwargs: dict = dict(model=self.model, messages=messages, max_completion_tokens=req.max_tokens)
        if self.model.startswith(_TEMPERATURE_OK_PREFIXES):
            kwargs["temperature"] = req.temperature
        t0 = time.perf_counter()
        try:
            if req.schema is not None:
                completion = client.chat.completions.parse(response_format=req.schema, **kwargs)
            else:
                completion = client.chat.completions.create(**kwargs)
        except openai.LengthFinishReasonError as e:
            raise TruncatedOutput(str(e), provider=self.name) from e
        except openai.ContentFilterFinishReasonError as e:
            raise ModelRefusal(str(e), provider=self.name) from e
        except openai.APITimeoutError as e:
            raise Timeout(str(e), provider=self.name) from e
        except openai.RateLimitError as e:
            raise RateLimited(str(e), provider=self.name) from e
        except openai.APIStatusError as e:
            raise ProviderError(f"HTTP {e.status_code}: {e.message}", provider=self.name, status=e.status_code) from e
        except openai.APIConnectionError as e:
            raise ProviderError(f"connection: {e}", provider=self.name) from e
        latency = (time.perf_counter() - t0) * 1000

        choice = completion.choices[0]
        msg = choice.message
        if getattr(msg, "refusal", None):
            raise ModelRefusal(msg.refusal, provider=self.name)
        if choice.finish_reason == "length":
            raise TruncatedOutput("finish_reason=length", provider=self.name)
        text = msg.content or ""
        parsed: Optional[object] = getattr(msg, "parsed", None) if req.schema is not None else None
        if req.schema is not None and parsed is None:
            # parse() 가 parsed 를 못 채운 드문 경우 — 텍스트에서 복구 시도
            try:
                parsed = req.schema.model_validate(extract_json(text))
            except Exception as e:  # noqa: BLE001
                raise SchemaMismatch(str(e), provider=self.name, raw_text=text) from e
        usage = completion.usage
        itok = getattr(usage, "prompt_tokens", None) if usage is not None else None
        otok = getattr(usage, "completion_tokens", None) if usage is not None else None
        estimated = itok is None or otok is None
        if estimated:  # 사용량을 못 받으면 글자 수로 보수적으로 추정하고 '추정' 으로 표시한다 (0원 확정 금지)
            itok = estimate_tokens(req.system + req.user) if itok is None else itok
            otok = estimate_tokens(text) if otok is None else otok
        return LLMResponse(
            text=text, parsed=parsed, provider=self.name, model=self.model,
            input_tokens=itok, output_tokens=otok, latency_ms=round(latency, 1),
            cost_usd=estimate_cost_usd(self.model, itok, otok), finish_reason=choice.finish_reason or "", cost_estimated=estimated,
        )
