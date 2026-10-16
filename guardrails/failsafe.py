"""실패를 '분류된 사건' 으로 바꾸고, 어떤 실패도 승인으로 흐르지 않게 한다 (fail-closed).

사용:
    with failsafe(run, stage="reviewer", code_map={ModelRefusal: "FM-08"}, fallback="HUMAN_REVIEW") as guard:
        ...
    if guard.failed: ...

예외를 삼키되(사용자에게 날것의 traceback 을 보이지 않음) FailureEvent 로 기록하고 트레이스 스팬에 이벤트를 남긴다.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

from core.schemas import FailureEvent, PipelineRun
from llm.errors import (
    BudgetExceeded,
    CircuitOpen,
    LLMError,
    ModelRefusal,
    ProviderUnavailable,
    SchemaMismatch,
    TruncatedOutput,
)
from observability.tracer import Tracer

DEFAULT_CODE_MAP: dict[type, str] = {
    SchemaMismatch: "FM-01",
    ModelRefusal: "FM-08",
    TruncatedOutput: "FM-16",
    BudgetExceeded: "FM-20",
    CircuitOpen: "FM-07",
    ProviderUnavailable: "FM-07",
}


@dataclass
class Guard:
    stage: str
    fallback: str
    failed: bool = False
    event: Optional[FailureEvent] = None
    extra_codes: dict[type, str] = field(default_factory=dict)

    def classify(self, exc: BaseException) -> str:
        for cls, code in {**DEFAULT_CODE_MAP, **self.extra_codes}.items():
            if isinstance(exc, cls):
                return code
        if isinstance(exc, LLMError):
            return getattr(exc, "code", "FM-05")
        return "FM-05"


@contextmanager
def failsafe(
    run: PipelineRun,
    stage: str,
    fallback: str,
    extra_codes: Optional[dict[type, str]] = None,
    reraise: tuple[type, ...] = (BudgetExceeded,),
) -> Iterator[Guard]:
    """예외를 FailureEvent 로 바꾼다. `reraise` 에 있는 예외(예산 초과)는 기록 후 다시 던져 실행을 멈춘다."""
    guard = Guard(stage=stage, fallback=fallback, extra_codes=extra_codes or {})
    try:
        yield guard
    except Exception as exc:  # noqa: BLE001 - 의도적으로 넓게 잡고 분류한다
        code = guard.classify(exc)
        guard.failed = True
        guard.event = FailureEvent(
            code=code, stage=stage, message=f"{type(exc).__name__}: {str(exc)[:300]}", fallback=fallback,
            recoverable=not isinstance(exc, reraise),
        )
        run.failures.append(guard.event)
        span = Tracer.current()
        if span is not None:
            span.event("failsafe", code=code, stage=stage, fallback=fallback, error=type(exc).__name__)
            span.status = "fallback"
        if isinstance(exc, reraise):
            raise
