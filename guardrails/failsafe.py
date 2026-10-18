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


# 모델 호출 자체가 실패한 것으로 분류되는 코드: 타임아웃/미분류(FM-05), 레이트 리밋(FM-06), 제공자 장애(FM-07), 비용·호출 상한(FM-20).
# 이 코드가 섞인 실행·평가·재검사는 '모델이 판단한 결과' 가 아니라 '모델을 못 부른 결과' 다 (FM-24).
PROVIDER_FAILURE_CODES = frozenset({"FM-05", "FM-06", "FM-07", "FM-20"})
# 단계가 끝나지 못한 것으로 보는 코드 — 위 네 가지에 스키마(FM-01)·거부(FM-08)·잘림(FM-16)을 더한다. 이 코드가 있으면 그 실행·재검사는 '일부 단계 실패' 이다.
STEP_FAILURE_CODES = PROVIDER_FAILURE_CODES | {"FM-01", "FM-08", "FM-16"}


def classify_exception(exc: BaseException, extra_codes: Optional[dict[type, str]] = None) -> str:
    """예외 → 실패 모드 코드. 파이프라인의 failsafe 와 재검사·평가가 같은 분류를 쓴다."""
    for cls, code in {**DEFAULT_CODE_MAP, **(extra_codes or {})}.items():
        if isinstance(exc, cls):
            return code
    if isinstance(exc, LLMError):
        return getattr(exc, "code", "FM-05")
    return "FM-05"


@dataclass
class Guard:
    stage: str
    fallback: str
    failed: bool = False
    event: Optional[FailureEvent] = None
    extra_codes: dict[type, str] = field(default_factory=dict)

    def classify(self, exc: BaseException) -> str:
        return classify_exception(exc, self.extra_codes)


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
