"""처리량·지연·메모리 측정 — 결정론 검사 계층과 대역 파이프라인 (모델 호출 없음, 네트워크 없음).

    python scripts/loadtest.py [--n 500] [--warmup 20] [--concurrency 1,4] [--repeats 3] [--seed 0] [--out evals/reports/loadtest.json]

무엇을 재나
- guardrails      : 평가 사례를 결정론 검사(`output_guard.check` — 표현·안내 문구·상품 정보 일치·형식·중복) 한 번씩 통과시키는 데 걸리는 시간
- pipeline_mock   : 예시 실행(대역) 파이프라인 한 번(요청 검사 → 캠페인 구성 → 광고 초안 → 광고안마다 검사·검토·수정안 재검사·판정)의 시간

이 숫자가 의미하지 않는 것
- 대역(mock)은 모델 지연이 없다(네트워크 왕복·토큰 생성 시간이 0). 그래서 이것은 '네트워크·모델 지연을 뺀, 우리 코드만의 처리량' 이다.
  실제 모델을 부르는 서비스의 처리량·응답 시간이 아니고, 실제 서비스의 용량 계획에 그대로 쓸 수 없다. 실제 모델 지연은 평가 리포트(compare_live.json)에서 사례당 약 3초로 따로 쟀다.
- 스레드를 늘려도 이 작업은 파이썬 GIL 때문에 거의 빨라지지 않는다(순수 파이썬 CPU 작업). 병렬도 항목은 '스레드에서 안전하게 돌고 지연이 얼마나 느려지는지' 를 보는 용도다.
  모델 호출을 기다리는 실제 서비스에서는 스레드(또는 비동기)가 대기 시간을 겹치게 해 주므로 결과가 다르다.
- 이 환경(CPU·파이썬 버전)에서 잰 값이다. 다른 기계에서는 다르다. 측정 중 다른 프로세스가 CPU 를 썼을 수 있어 적재량(load average)을 함께 기록한다.
- 메모리 증가는 파이썬 객체 할당(tracemalloc)과 프로세스 최대 상주 메모리(RSS)다. 누수 여부의 증명이 아니라 '반복해도 선형으로 늘지 않는지' 를 보는 단순 점검이다.

결과는 evals/reports/loadtest.json 에 쓴다. 같은 인자·같은 기계라도 지연은 실행마다 달라진다(시드는 사례 순서만 고정한다).
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import random
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_OUT = REPO / "evals" / "reports" / "loadtest.json"
DATASETS = ("golden", "holdout", "conditions")
PRESET_ORDER = ("loan_ok", "malicious", "injection", "prohibited", "commerce_landing", "investment")

WHAT_THIS_IS = ("모델 호출이 없는 결정론 검사 계층과 대역(mock) 파이프라인의 처리량·지연·메모리를 이 환경에서 잰 값이다. "
                "대역은 모델 지연이 없으므로 '네트워크·모델 지연을 뺀 우리 코드의 처리량' 이다.")
WHAT_THIS_IS_NOT = [
    "실제 모델을 부르는 서비스의 처리량·응답 시간이 아니다 (실제 모델 지연은 compare_live.json 에서 사례당 약 3초로 따로 쟀다).",
    "용량 계획의 근거로 그대로 쓸 수 없다 — 실제 요청 구성·동시 접속·네트워크·모델 제공자의 속도 제한이 빠져 있다.",
    "스레드 병렬도는 GIL 때문에 CPU 작업의 처리량을 거의 늘리지 않는다. 병렬도 행은 스레드 안전성과 지연 악화를 보는 용도다.",
    "다른 기계·다른 파이썬 버전에서는 값이 다르다. 같은 환경에서도 실행마다 달라진다.",
]


def percentile(sorted_values: Sequence[float], q: float) -> float:
    n = len(sorted_values)
    if n == 0:
        return math.nan
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] * (1 - (pos - lo)) + sorted_values[hi] * (pos - lo)


def summarize_ms(values: Sequence[float]) -> dict[str, Any]:
    v = sorted(values)
    if not v:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "mean": None, "max": None}
    return {"n": len(v), "p50": round(percentile(v, 0.5), 3), "p95": round(percentile(v, 0.95), 3), "p99": round(percentile(v, 0.99), 3),
            "mean": round(sum(v) / len(v), 3), "max": round(v[-1], 3)}


def environment() -> dict[str, Any]:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        load = []
    return {"python": platform.python_version(), "implementation": platform.python_implementation(), "platform": platform.platform(), "machine": platform.machine(),
            "cpu_model": cpu or platform.processor() or "알 수 없음", "cpu_count": os.cpu_count(), "load_average_at_start": load,
            "gil_enabled": bool(getattr(sys, "_is_gil_enabled", lambda: True)())}


# --- 작업 정의 ---------------------------------------------------------------------------------
def build_guardrail_task(seed: int) -> tuple[Callable[[int], None], int, str]:
    """결정론 검사 한 번 = 사례 하나를 output_guard.check 에 통과시킨다. (함수, 사례 수, 설명)"""
    from evals.runner import load_dataset
    from guardrails import output_guard
    from guardrails.policy import get_catalog

    catalog = get_catalog()
    cases = [c for ds in DATASETS for c in load_dataset(ds)]
    random.Random(seed).shuffle(cases)
    prepared = [(c.to_copy(), c.to_product(), c.landing_text) for c in cases]

    def task(i: int) -> None:
        copy, product, landing = prepared[i % len(prepared)]
        categories = catalog.categories_for(product, copy.full_text())
        output_guard.check(copy, product, categories, landing, [], catalog)

    return task, len(prepared), f"사례 {len(prepared)}건({'+'.join(DATASETS)})을 돌려 가며 output_guard.check 호출"


def build_pipeline_task(seed: int) -> tuple[Callable[[int], None], int, str]:
    """대역 파이프라인 한 번 = 예시 입력 하나를 run_pipeline(예시 실행)으로 끝까지 돌린다."""
    from agents.pipeline import run_pipeline
    from core.config import Settings
    from observability.tracer import Tracer
    from ui.presets import PRESETS, build_brief

    settings = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    tracer = Tracer(max_traces=50)      # 트레이스 보관 상한이 있어 메모리가 무한히 늘지 않는다 (서비스와 같은 설정)
    order = list(PRESET_ORDER)
    random.Random(seed).shuffle(order)
    briefs = [build_brief(PRESETS[k]) for k in order]

    def task(i: int) -> None:
        run_pipeline(briefs[i % len(briefs)], settings, tracer=tracer)

    return task, len(briefs), f"예시 입력 {len(briefs)}종을 돌려 가며 run_pipeline(예시 실행, 모델 호출 없음) 호출"


WORKLOADS = {"guardrails": build_guardrail_task, "pipeline_mock": build_pipeline_task}


# --- 측정 ---------------------------------------------------------------------------------------
def _timed(task: Callable[[int], None], i: int) -> tuple[float, Optional[str]]:
    t0 = time.perf_counter()
    err = None
    try:
        task(i)
    except Exception as e:  # noqa: BLE001 — 실패도 센다 (처리량에서 빼지 않고 오류 수로 보고)
        err = f"{type(e).__name__}: {str(e)[:120]}"
    return (time.perf_counter() - t0) * 1000.0, err


def run_once(task: Callable[[int], None], n: int, concurrency: int) -> dict[str, Any]:
    """n 건을 concurrency 개 스레드로 돌린다. 건마다 걸린 시간(스레드 대기 포함)과 전체 시간을 잰다."""
    gc.collect()
    lat: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    t0 = time.perf_counter()
    if concurrency <= 1:
        for i in range(n):
            ms, err = _timed(task, i)
            lat.append(ms)
            if err:
                errors.append(err)
    else:
        def work(i: int) -> None:
            ms, err = _timed(task, i)
            with lock:
                lat.append(ms)
                if err:
                    errors.append(err)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(work, range(n)))
    wall = time.perf_counter() - t0
    return {"wall_s": wall, "latencies_ms": lat, "errors": errors}


def measure_workload(name: str, builder: Callable[[int], tuple[Callable[[int], None], int, str]], n: int, warmup: int, concurrency: Sequence[int],
                     repeats: int, seed: int) -> dict[str, Any]:
    task, n_distinct, desc = builder(seed)
    for i in range(warmup):                       # 정규식 컴파일·캐시·임포트 지연을 측정에서 뺀다
        task(i)
    results = []
    for c in concurrency:
        runs = [run_once(task, n, c) for _ in range(repeats)]
        walls = [r["wall_s"] for r in runs]
        thr = [n / w if w > 0 else math.inf for w in walls]
        pooled = [x for r in runs for x in r["latencies_ms"]]
        results.append({
            "concurrency": c, "n_per_repeat": n, "repeats": repeats, "warmup_calls": warmup,
            "throughput_per_s_median": round(sorted(thr)[len(thr) // 2], 2), "throughput_per_s_all": [round(t, 2) for t in thr],
            "wall_s_all": [round(w, 3) for w in walls], "latency_ms": summarize_ms(pooled), "errors": sum(len(r["errors"]) for r in runs),
            "error_examples": sorted({e for r in runs for e in r["errors"]})[:3],
        })
    return {"description": desc, "distinct_inputs": n_distinct, "results": results, "memory": measure_memory(task, n)}


def _rss_kb() -> Optional[int]:
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)     # Linux: KB, 지금까지의 최대 상주 메모리 (줄어들지 않는다)
    except (ImportError, OSError):
        return None


def measure_memory(task: Callable[[int], None], n: int) -> dict[str, Any]:
    """tracemalloc 으로 n 건을 세 구간(앞·가운데·뒤)으로 나눠 돌리고 구간 끝마다 남은 파이썬 객체 크기를 잰다.
    앞 구간은 트레이서의 보관 상한(50개)이 차는 시간이라 증가로 보이는 것이 정상이다 — 가운데→뒤 구간의 건당 증가량을 본다. 호출이 늘수록 선형으로 늘면 누수 의심이다.
    n 이 150 미만이면 앞 구간이 짧아 상한이 차기 전일 수 있다(권장 n ≥ 300)."""
    gc.collect()
    rss_before = _rss_kb()
    third = max(1, n // 3)
    marks: list[int] = []
    tracemalloc.start()
    try:
        i = 0
        for _phase in range(3):
            for _ in range(third):
                task(i)
                i += 1
            gc.collect()
            marks.append(tracemalloc.get_traced_memory()[0])
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    rss_after = _rss_kb()
    return {
        "calls": third * 3, "retained_after_each_third_kb": [round(m / 1024, 1) for m in marks], "peak_during_kb": round(peak / 1024, 1),
        "growth_per_call_first_to_second_third_bytes": round((marks[1] - marks[0]) / third, 1), "growth_per_call_second_to_last_third_bytes": round((marks[2] - marks[1]) / third, 1),
        "rss_peak_before_kb": rss_before, "rss_peak_after_kb": rss_after, "rss_peak_growth_kb": (rss_after - rss_before) if (rss_before is not None and rss_after is not None) else None,
        "note": "tracemalloc 은 실행을 느리게 하므로 이 구간의 시간은 처리량 측정에 쓰지 않았다. RSS 는 프로세스 전체의 최대치라 앞선 측정의 영향을 받고 줄어들지 않는다.",
    }


def run_loadtest(n: int = 500, warmup: int = 20, concurrency: Sequence[int] = (1, 4), repeats: int = 3, seed: int = 0,
                 workloads: Optional[Sequence[str]] = None, command: str = "") -> dict[str, Any]:
    if n < 1 or warmup < 0 or repeats < 1 or not concurrency or any(c < 1 for c in concurrency):
        raise ValueError("n ≥ 1, warmup ≥ 0, repeats ≥ 1, concurrency 는 1 이상이어야 합니다")
    names = list(workloads) if workloads else list(WORKLOADS)
    env = environment()
    started = time.time()
    out: dict[str, Any] = {
        "kind": "loadtest", "generated_at": datetime.now(timezone.utc).isoformat(), "command": command or f"python scripts/loadtest.py --n {n} --warmup {warmup} --concurrency {','.join(map(str, concurrency))} --repeats {repeats} --seed {seed}",
        "what_this_is": WHAT_THIS_IS, "what_this_is_not": WHAT_THIS_IS_NOT, "model_calls": 0, "network_calls": 0,
        "environment": env, "config": {"n_per_repeat": n, "warmup_calls": warmup, "concurrency": list(concurrency), "repeats": repeats, "seed": seed, "datasets": list(DATASETS), "presets": list(PRESET_ORDER)},
        "workloads": {},
    }
    for name in names:
        out["workloads"][name] = measure_workload(name, WORKLOADS[name], n, warmup, concurrency, repeats, seed)
    try:
        out["environment"]["load_average_at_end"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    out["elapsed_s"] = round(time.time() - started, 1)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="결정론 검사·대역 파이프라인 처리량 측정 (모델 호출 없음)")
    ap.add_argument("--n", type=int, default=500, help="반복마다 처리할 건수")
    ap.add_argument("--warmup", type=int, default=20, help="측정 전 버리는 호출 수")
    ap.add_argument("--concurrency", default="1,4", help="스레드 수 목록(쉼표)")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workloads", default="", help="쉼표로 구분: guardrails,pipeline_mock (기본 전부)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    conc = [int(x) for x in a.concurrency.split(",") if x.strip()]
    names = [x.strip() for x in a.workloads.split(",") if x.strip()] or None
    cmd = "python scripts/loadtest.py " + " ".join(argv if argv is not None else sys.argv[1:])
    report = run_loadtest(a.n, a.warmup, conc, a.repeats, a.seed, names, command=cmd.strip())
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"저장: {out}")
    for name, w in report["workloads"].items():
        for r in w["results"]:
            lat = r["latency_ms"]
            print(f"{name:14s} 병렬도 {r['concurrency']}: 처리량 {r['throughput_per_s_median']}/초 · 지연 p50 {lat['p50']}ms p95 {lat['p95']}ms p99 {lat['p99']}ms · 오류 {r['errors']}")
        m = w["memory"]
        print(f"{name:14s} 메모리: 구간별 남은 파이썬 객체 {m['retained_after_each_third_kb']}KB, 뒤 구간 건당 증가 {m['growth_per_call_second_to_last_third_bytes']}B, 최대 RSS 증가 {m['rss_peak_growth_kb']}KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
