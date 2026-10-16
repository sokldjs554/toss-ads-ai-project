"""샘플 트레이스 기록 — UI 의 '재생' 메뉴에서 API 없이 볼 수 있다.

키가 있으면 실제 호출(LIVE)로, 없으면 DEMO 모드로 기록된다. 파일 이름에 모드가 붙는다.
    python scripts/record_traces.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.pipeline import RunOptions, run_pipeline  # noqa: E402
from core.config import Settings  # noqa: E402
from observability.tracer import Tracer  # noqa: E402
from ui.presets import PRESETS, build_brief  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    out = ROOT / "traces"
    out.mkdir(exist_ok=True)
    for key in ("loan_ok", "malicious", "injection", "prohibited", "commerce_landing"):
        preset = PRESETS[key]
        tracer = Tracer()
        brief = build_brief(preset)
        run = run_pipeline(brief, settings, options=RunOptions(landing_text=preset["landing_text"]), tracer=tracer)
        trace = tracer.latest()
        trace.metadata["preset"] = key
        trace.metadata["final_decisions"] = [r.final_decision.value for r in run.reports]
        trace.metadata["halted"] = run.halted
        path = out / f"sample_{settings.mode.value}_{key}.json"
        path.write_text(trace.to_json(), encoding="utf-8")
        print(f"{path.name}: spans={len(trace.spans())} llm={len(trace.llm_spans())} cost=${trace.total_cost_usd:.4f} "
              f"decisions={trace.metadata['final_decisions']} halted={run.halted}")


if __name__ == "__main__":
    main()
