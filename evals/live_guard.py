"""승인된 실제 평가 전용 비용 장부. SDK 재시도는 반드시 0으로 구성한다.

요청 전: UTF-8 바이트 수(문자 토큰의 보수적 상한), 스키마/프로토콜 여유분,
이미지당 32,768 입력 토큰, 최대 출력 토큰을 공개 단가로 예약한다.
응답 후: 검증된 토큰 사용량으로 정산한다(캐시 할인은 적용하지 않아 보수적).
예외·사용량 누락·다른 모델 응답: 예약액을 그대로 유지한다. 0원으로 세지 않는다.
모델/요청 크기를 제한한 이 평가의 예산 제어이며 공급자의 청구서가 아니다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from llm.base import LLMRequest, LLMResponse, Provider
from llm.errors import BudgetExceeded

# 별칭이나 환경변수로 모델·단가가 바뀌는 평가를 허용하지 않는다.
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
# https://platform.claude.com/docs/en/models/sonnet-5-5/overview
APPROVED_MODELS = {
    "gpt-5.4-mini-2026-03-17": ("openai", 0.75, 4.50),
    "claude-sonnet-5-5": ("anthropic", 2.00, 10.00),
}
MAX_TEXT_BYTES = 180_000
MAX_OUTPUT_TOKENS = 8192
MAX_IMAGES = 2
MAX_IMAGE_BYTES = 4_000_000
IMAGE_TOKEN_ALLOWANCE = 32_768
PROTOCOL_ALLOWANCE = 4096


def request_reservation(req: LLMRequest, model: str) -> tuple[float, int]:
    _, pin, pout = APPROVED_MODELS[model]
    schema = json.dumps(req.schema.model_json_schema(), ensure_ascii=False) if req.schema else ""
    nbytes = len((req.system + req.user + schema).encode("utf-8"))
    if nbytes > MAX_TEXT_BYTES:
        raise ValueError("evaluation text size exceeds the frozen limit")
    if not 0 < req.max_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError("evaluation output token limit is not bounded")
    if len(req.images) > MAX_IMAGES or any(i.size > MAX_IMAGE_BYTES for i in req.images):
        raise ValueError("evaluation image size exceeds the frozen limit")
    input_bound = nbytes + PROTOCOL_ALLOWANCE + len(req.images) * IMAGE_TOKEN_ALLOWANCE
    return (input_bound * pin + req.max_tokens * pout) / 1_000_000, input_bound


@dataclass
class LiveLedger:
    cap_usd: float
    max_calls: int
    checkpoint: Optional[Callable[[dict], None]] = None
    calls: list[dict] = field(default_factory=list)
    stopped: bool = False

    def __post_init__(self):
        if not math.isfinite(self.cap_usd) or not 0 < self.cap_usd <= 10:
            raise ValueError("approved cost cap must be within (0, 10] USD")
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or not 0 < self.max_calls <= 400:
            raise ValueError("approved call count must be within 1..400")

    @property
    def accounted(self) -> float:
        return sum(c["accounted_usd"] for c in self.calls)

    def _save(self) -> None:
        if self.checkpoint is not None:
            # 비용 예약을 기록하지 못하면 호출도 시작하지 않는다.
            self.checkpoint(self.summary())

    def reserve(self, provider: Provider, req: LLMRequest) -> dict:
        amount, input_bound = request_reservation(req, provider.model)
        if self.stopped or len(self.calls) >= self.max_calls or self.accounted + amount > self.cap_usd + 1e-12:
            self.stopped = True
            raise BudgetExceeded("approved live-evaluation budget/call count reached before request")
        rec = {
            "ordinal": len(self.calls) + 1, "provider": provider.name, "requested_model": provider.model,
            "task": req.task.value, "label": req.label[:120], "status": "reserved",
            "input_token_bound": input_bound, "output_token_bound": req.max_tokens,
            "reserved_usd": amount, "accounted_usd": amount, "usage_cost_usd": None,
            "input_tokens": None, "output_tokens": None, "latency_ms": None,
            "request_sha256": hashlib.sha256((req.system + "\n" + req.user).encode()).hexdigest(),
            "image_sha256": [hashlib.sha256(i.data).hexdigest() for i in req.images],
        }
        self.calls.append(rec)
        self._save()
        return rec

    def success(self, rec: dict, resp: LLMResponse) -> None:
        _, pin, pout = APPROVED_MODELS[rec["requested_model"]]
        valid = (
            resp.provider == rec["provider"] and resp.model == rec["requested_model"]
            and not resp.cost_estimated and type(resp.input_tokens) is int and type(resp.output_tokens) is int
            and 0 <= resp.input_tokens <= rec["input_token_bound"]
            and 0 <= resp.output_tokens <= rec["output_token_bound"]
        )
        if not valid:
            rec.update(status="unverified_usage", error_type="UsageOrModelMismatch")
            self.stopped = True
            self._save()
            raise BudgetExceeded("model/usage differs from frozen live-evaluation contract; reservation retained")
        cost = (resp.input_tokens * pin + resp.output_tokens * pout) / 1_000_000
        rec.update(status="success", returned_model=resp.model, input_tokens=resp.input_tokens,
                   output_tokens=resp.output_tokens, usage_cost_usd=cost, accounted_usd=cost)
        self._save()

    def failed(self, rec: dict, exc: Exception) -> None:
        # 예외 메시지에는 요청이나 비밀값이 섞일 수 있어 타입과 HTTP 상태만 저장한다.
        rec.update(status="failed", error_type=type(exc).__name__)
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            rec["http_status"] = status
        self._save()

    def summary(self) -> dict:
        return copy.deepcopy({
            "cap_usd": self.cap_usd, "max_calls": self.max_calls, "attempts": len(self.calls),
            "successful_calls": sum(c["status"] == "success" for c in self.calls),
            "unknown_cost_calls": sum(c["status"] != "success" for c in self.calls),
            "usage_cost_usd": sum(c["usage_cost_usd"] or 0 for c in self.calls),
            "accounted_usd": self.accounted, "stopped": self.stopped,
            "billing_note": "공개 단가 × 응답 토큰 사용량. 캐시 할인 미적용. 실제 청구액이 아니며 사용량 미확인 호출은 예약액을 유지함.",
            "calls": self.calls,
        })


class BoundedProvider:
    def __init__(self, provider: Provider, ledger: LiveLedger):
        if provider.model not in APPROVED_MODELS or APPROVED_MODELS[provider.model][0] != provider.name:
            raise ValueError("unapproved live-evaluation model/provider")
        self._provider = provider
        self.ledger = ledger
        self.name, self.model = provider.name, provider.model

    @property
    def available(self) -> bool:
        return self._provider.available

    def complete(self, req: LLMRequest) -> LLMResponse:
        rec = self.ledger.reserve(self._provider, req)
        started = time.perf_counter()
        try:
            resp = self._provider.complete(req)
        except Exception as exc:
            rec["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self.ledger.failed(rec, exc)
            raise
        rec["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self.ledger.success(rec, resp)
        return resp
