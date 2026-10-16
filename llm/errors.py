"""타입이 있는 실패. 코드(FM-xx)는 guardrails/failure_modes.py 카탈로그와 1:1 이다."""
from __future__ import annotations


class LLMError(Exception):
    code = "FM-05"

    def __init__(self, message: str = "", provider: str = "", **extra: object) -> None:
        super().__init__(message)
        self.provider = provider
        self.extra = extra


class Timeout(LLMError):
    code = "FM-05"


class RateLimited(LLMError):
    code = "FM-06"


class CircuitOpen(LLMError):
    code = "FM-06"


class ProviderError(LLMError):
    code = "FM-07"


class ProviderUnavailable(LLMError):
    """fallback 체인의 모든 제공자가 실패."""

    code = "FM-07"


class ModelRefusal(LLMError):
    code = "FM-08"


class SchemaMismatch(LLMError):
    """출력이 JSON 이 아니거나 스키마에 맞지 않음. raw_text 를 들고 있어 수리 호출에 쓴다."""

    code = "FM-01"

    def __init__(self, message: str = "", provider: str = "", raw_text: str = "", **extra: object) -> None:
        super().__init__(message, provider, **extra)
        self.raw_text = raw_text


class TruncatedOutput(LLMError):
    code = "FM-16"


class BudgetExceeded(LLMError):
    code = "FM-20"
