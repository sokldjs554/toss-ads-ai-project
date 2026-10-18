"""샘플 트레이스 기록 — UI 의 '재생' 메뉴에서 API 없이 볼 수 있다.

키가 있으면 실제 호출(LIVE)로, 없으면 DEMO 모드로 기록된다. 파일 이름에 모드가 붙는다.
    python scripts/record_traces.py

다섯 시나리오를 모두 메모리에서 돌린 뒤, **제공자 장애가 한 건도 없을 때만** 파일을 쓴다 (FM-24). 장애는 실행 결과(run.failures)뿐 아니라 처리 기록 안의 모델 호출 span(오류 상태·제공자 오류·대체 제공자 응답)에서도 찾는다.
장애(타임아웃·레이트 리밋·제공자 전체 장애·비용 상한)가 섞인 처리 기록은 '실제 모델의 처리' 가 아니라 장애 기록이므로
기존 샘플을 덮어쓰지 않고 종료 코드 2 로 끝난다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.pipeline import RunOptions, run_pipeline  # noqa: E402
from core.config import Settings  # noqa: E402
from guardrails.failsafe import PROVIDER_FAILURE_CODES  # noqa: E402
from observability.tracer import Tracer  # noqa: E402
from ui.presets import PRESETS, build_brief  # noqa: E402

SCENARIOS = ("loan_ok", "malicious", "injection", "prohibited", "commerce_landing")
# 제공자 쪽 실패로 분류되는 실패 모드(타임아웃/미분류 FM-05, 레이트 리밋 FM-06, 제공자 전체 장애 FM-07, 비용·호출 상한 FM-20)는 guardrails/failsafe.py 의 PROVIDER_FAILURE_CODES.
# 수정안 의미 검사(rewrite_recheck)가 실패한 것도 같은 코드로 run.failures 에 남으므로 여기서 함께 센다.


def provider_failures(run) -> list[str]:
    """처리 한 번에서 제공자 장애로 기록된 실패 사건 (예: 'FM-07@reviewer')."""
    return [f"{f.code}@{f.stage}" for f in run.failures if f.code in PROVIDER_FAILURE_CODES]


_TRACE_FAILURE_EVENTS = ("provider_error", "refusal", "truncated", "schema_mismatch")


def trace_failures(trace) -> list[str]:
    """처리 기록 안의 모델 호출 span 에서 실패가 있었던 흔적 — run.failures 에 남지 않는 것도 잡는다
    (입력 검사의 되묻기 호출이 실패해 조용히 넘어간 경우, 대체 제공자가 대신 답한 경우). 예: 'error@redirect:intercept', 'provider_error@llm:review'."""
    out: list[str] = []
    for sp in trace.llm_spans():
        if sp.status == "error":
            out.append(f"error@{sp.name}")
        for e in sp.events:
            if e.get("name") in _TRACE_FAILURE_EVENTS:
                out.append(f"{e['name']}@{sp.name}")
    return out


def main(settings: Optional[Settings] = None, out: Optional[Path] = None, scenarios: tuple[str, ...] = SCENARIOS,
         pipeline: Callable = run_pipeline, log: Callable[[str], None] = print) -> int:
    settings = settings or Settings.from_env()
    out = out or ROOT / "traces"
    recorded: list[tuple[str, str, str]] = []   # (파일 이름, 트레이스 JSON, 한 줄 요약)
    failed: dict[str, list[str]] = {}
    for key in scenarios:
        preset = PRESETS[key]
        tracer = Tracer()
        brief = build_brief(preset)
        run = pipeline(brief, settings, options=RunOptions(landing_text=preset["landing_text"]), tracer=tracer)
        trace = tracer.latest()
        trace.metadata["preset"] = key
        trace.metadata["final_decisions"] = [r.final_decision.value for r in run.reports]
        trace.metadata["halted"] = run.halted
        pf = provider_failures(run) + trace_failures(trace)
        if pf:
            failed[key] = pf
        recorded.append((f"sample_{settings.mode.value}_{key}.json", trace.to_json(),
                         f"spans={len(trace.spans())} llm={len(trace.llm_spans())} cost=${trace.total_cost_usd:.4f} "
                         f"decisions={trace.metadata['final_decisions']} halted={run.halted}"))
    if failed:
        log(f"트레이스를 저장하지 않음 — 제공자 장애가 섞인 처리 기록은 측정이 아니다: {failed}")
        for name, _, line in recorded:
            log(f"  (미저장) {name}: {line}")
        return 2
    out.mkdir(exist_ok=True)
    for name, text, line in recorded:
        (out / name).write_text(text, encoding="utf-8")
        log(f"{name}: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
