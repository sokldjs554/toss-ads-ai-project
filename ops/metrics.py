"""운영 지표 — 처리 기록(PipelineRun)과 트레이서 스팬에서 계산한다. 계산만 하고 아무것도 보내지 않는다.

계산하는 것
- 판정 분포, 담당자 확인율
- 단계별 지연 p50/p95/p99 (트레이스 스팬의 실제 경과 시간), 실행 한 번의 총 지연
- 실행당·광고안당 비용, 비용이 '최소값' 인지(응답을 받지 못한 요청이 있음) 여부
- 제공자별 호출·실패·대체 경로 비율
- 실패 모드(FM-xx) 빈도, 재검사 사용률, 검토 모델 인용이 원문에 없던 비율

이 숫자가 의미하지 않는 것
- 표본은 '지금 넘겨 준 기록' 이 전부다. 이 세션이나 이 파일에 든 실행만 센 것이며 서비스 전체의 분포가 아니다.
- 예시 실행(mode=demo)의 지연·비용은 대역(mock)의 값이다. 모델의 지연·비용이 아니고, `synthetic` 표시가 붙는다. 대역 호출의 `latency_ms` 는 고정 35ms 가 더해진 값이라 따로 세지 않는다.
- 분위수는 표본 크기(n)에 크게 흔들린다. n 이 100 미만이면 p99 는 사실상 최댓값이다. 항상 n 을 함께 본다.
- 한 실행 안의 광고안들은 같은 상품에서 나와 서로 닮았다. 비율의 신뢰구간은 광고안을 독립으로 가정한 값보다 넓다.

Prometheus 텍스트는 '지금 가진 기록의 스냅숏' 이다. 실제 수집기(스크레이퍼)·저장소·알림 규칙은 이 프로젝트에 없다 — 형식만 맞췄다.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

from core.schemas import Decision, Layer, PipelineRun
from observability.tracer import Span, Trace

# 요청은 나갔는데 응답(사용량)을 받지 못한 경우를 라우터가 스팬 이벤트로 남기는 이름 (ui/trace_view.py 와 같은 목록)
FAILED_REQUEST_EVENTS = ("provider_error", "refusal", "truncated", "schema_mismatch", "repair_failed")
_CONTAINER_PREFIXES = ("variant",)


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """선형 보간(numpy 기본값과 같은 방식) 분위수. q 는 0~1. 빈 입력은 NaN."""
    n = len(sorted_values)
    if n == 0:
        return math.nan
    if n == 1:
        return float(sorted_values[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


@dataclass(frozen=True)
class Quantiles:
    n: int
    p50: float
    p95: float
    p99: float
    mean: float
    max: float
    total: float

    @classmethod
    def of(cls, values: Iterable[float]) -> "Quantiles":
        v = sorted(float(x) for x in values if x is not None and not (isinstance(x, float) and math.isnan(x)))
        if not v:
            return cls(0, math.nan, math.nan, math.nan, math.nan, math.nan, 0.0)
        return cls(len(v), percentile(v, 0.5), percentile(v, 0.95), percentile(v, 0.99), sum(v) / len(v), v[-1], sum(v))

    def to_dict(self) -> dict:
        return {k: (None if isinstance(x, float) and math.isnan(x) else (round(x, 4) if isinstance(x, float) else x)) for k, x in asdict(self).items()}


@dataclass
class ProviderStats:
    provider: str
    answered: int = 0              # 응답을 돌려준 호출 수
    failures: int = 0              # 이 제공자에서 난 실패(타임아웃·429·거부·잘림·형식 오류·수리 실패)
    circuit_skips: int = 0         # 서킷브레이커가 열려 건너뛴 횟수
    fallback_answers: int = 0      # 1순위가 아닌데 이 제공자가 대신 응답한 호출 수
    synthetic: bool = False        # 대역(mock) — 지연·비용이 모델 값이 아니다

    @property
    def attempts(self) -> int:
        return self.answered + self.failures

    @property
    def failure_rate(self) -> Optional[float]:
        return self.failures / self.attempts if self.attempts else None

    @property
    def fallback_rate(self) -> Optional[float]:
        return self.fallback_answers / self.answered if self.answered else None

    def to_dict(self) -> dict:
        return {"provider": self.provider, "answered": self.answered, "failures": self.failures, "attempts": self.attempts, "circuit_skips": self.circuit_skips,
                "fallback_answers": self.fallback_answers, "failure_rate": self.failure_rate, "fallback_rate": self.fallback_rate, "synthetic": self.synthetic}


@dataclass
class Metrics:
    n_runs: int = 0
    n_ads: int = 0
    n_halted: int = 0
    n_traces: int = 0
    modes: dict[str, int] = field(default_factory=dict)
    synthetic_only: bool = False                         # 모든 실행이 예시 실행(대역) — 비용·지연은 모델 값이 아니다
    verdicts: dict[str, int] = field(default_factory=dict)
    human_review_rate: Optional[float] = None
    run_latency_ms: Quantiles = field(default_factory=lambda: Quantiles.of([]))
    stage_latency_ms: dict[str, Quantiles] = field(default_factory=dict)
    llm_latency_ms: dict[str, Quantiles] = field(default_factory=dict)       # 제공자별 응답 지연(대역 제외)
    run_cost_usd: Quantiles = field(default_factory=lambda: Quantiles.of([]))
    cost_per_ad_usd: Optional[float] = None
    cost_is_lower_bound: bool = False
    providers: dict[str, ProviderStats] = field(default_factory=dict)
    llm_calls: int = 0
    llm_fallback_calls: int = 0
    failure_modes: dict[str, dict] = field(default_factory=dict)             # code → {count, runs, name}
    n_rechecks: int = 0                                                      # 이름이 'recheck' 인 트레이스 수 (고친 문구 다시 검사)
    recheck_rate: Optional[float] = None                                     # 다시 검사 수 / 광고안 수
    rewrite_checked_ads: int = 0
    semantic_findings: int = 0
    unverified_quotes: int = 0
    unverified_quote_rate: Optional[float] = None
    caveats: list[str] = field(default_factory=list)

    @property
    def verdict_rates(self) -> dict[str, float]:
        return {k: v / self.n_ads for k, v in self.verdicts.items()} if self.n_ads else {}

    @property
    def fallback_rate(self) -> Optional[float]:
        return self.llm_fallback_calls / self.llm_calls if self.llm_calls else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_runs": self.n_runs, "n_ads": self.n_ads, "n_halted": self.n_halted, "n_traces": self.n_traces, "modes": dict(self.modes), "synthetic_only": self.synthetic_only,
            "verdicts": dict(self.verdicts), "verdict_rates": self.verdict_rates, "human_review_rate": self.human_review_rate,
            "run_latency_ms": self.run_latency_ms.to_dict(), "stage_latency_ms": {k: v.to_dict() for k, v in self.stage_latency_ms.items()},
            "llm_latency_ms": {k: v.to_dict() for k, v in self.llm_latency_ms.items()},
            "run_cost_usd": self.run_cost_usd.to_dict(), "cost_per_ad_usd": self.cost_per_ad_usd, "cost_is_lower_bound": self.cost_is_lower_bound,
            "providers": {k: v.to_dict() for k, v in self.providers.items()}, "llm_calls": self.llm_calls, "llm_fallback_calls": self.llm_fallback_calls, "fallback_rate": self.fallback_rate,
            "failure_modes": {k: dict(v) for k, v in self.failure_modes.items()}, "n_rechecks": self.n_rechecks, "recheck_rate": self.recheck_rate,
            "rewrite_checked_ads": self.rewrite_checked_ads, "semantic_findings": self.semantic_findings, "unverified_quotes": self.unverified_quotes,
            "unverified_quote_rate": self.unverified_quote_rate, "caveats": list(self.caveats),
        }

    def to_prometheus_text(self, prefix: str = "preflight") -> str:
        return _prometheus(self, prefix)


# ---------------------------------------------------------------------------
# 계산
# ---------------------------------------------------------------------------
def stage_key(span: Span) -> Optional[str]:
    """집계할 단계 이름. 'input_guard:intercept' → 'input_guard', 광고안 묶음(variant:…)·모델 호출·루트는 제외."""
    if span.kind not in ("stage", "guardrail") or span.parent_id is None:
        return None
    base = span.name.split(":", 1)[0]
    return None if base in _CONTAINER_PREFIXES else base


def _is_failure_event(e: dict) -> bool:
    return str(e.get("name", "")) in FAILED_REQUEST_EVENTS


def compute_metrics(runs: Sequence[PipelineRun] = (), traces: Sequence[Trace] = ()) -> Metrics:
    """처리 기록 목록과 트레이스 목록에서 지표를 계산한다. 어느 쪽만 있어도 되지만, 판정·실패 모드·비용은 runs 에서만, 단계 지연·제공자는 traces 에서만 정확하다."""
    m = Metrics()
    runs, traces = list(runs), list(traces)
    m.n_runs, m.n_traces = len(runs), len(traces)
    caveats: list[str] = []

    # --- 실행 단위 (판정·실패 모드·비용) -------------------------------------------------
    verdicts: dict[str, int] = {d.value: 0 for d in Decision}
    fm_count: dict[str, int] = {}
    fm_runs: dict[str, int] = {}
    semantic = unverified = 0
    for run in runs:
        m.modes[run.mode] = m.modes.get(run.mode, 0) + 1
        m.n_halted += int(run.halted)
        for rep in run.reports:
            verdicts[rep.final_decision.value] += 1
            m.n_ads += 1
            m.rewrite_checked_ads += int(rep.rewrite_status.value != "none")
            for f in rep.verdict.findings:
                if f.layer == Layer.SEMANTIC:
                    semantic += 1
                    unverified += int(not f.verified_quote)
        for code in {f.code for f in run.failures}:
            fm_runs[code] = fm_runs.get(code, 0) + 1
        for f in run.failures:
            fm_count[f.code] = fm_count.get(f.code, 0) + 1
    if runs:
        m.verdicts = verdicts
        m.human_review_rate = verdicts[Decision.HUMAN_REVIEW.value] / m.n_ads if m.n_ads else None
        m.run_latency_ms = Quantiles.of(r.total_latency_ms for r in runs)
        m.run_cost_usd = Quantiles.of(r.total_cost_usd for r in runs)
        m.cost_per_ad_usd = (m.run_cost_usd.total / m.n_ads) if m.n_ads else None
        m.synthetic_only = all(r.mode == "demo" for r in runs)
        m.semantic_findings, m.unverified_quotes = semantic, unverified
        m.unverified_quote_rate = (unverified / semantic) if semantic else None
    names = _fm_names()
    m.failure_modes = {c: {"count": fm_count[c], "runs": fm_runs[c], "name": names.get(c, "(카탈로그에 없음)")} for c in sorted(fm_count)}

    # --- 트레이스 단위 (단계 지연·제공자·재검사) ------------------------------------------------
    stage_vals: dict[str, list[float]] = {}
    llm_vals: dict[str, list[float]] = {}
    provs: dict[str, ProviderStats] = {}
    lower_bound = False
    trace_cost: list[float] = []
    trace_runs = 0
    for t in traces:
        if t.name == "recheck":
            m.n_rechecks += 1
        if t.name == "preflight":
            trace_runs += 1
            trace_cost.append(t.total_cost_usd)
        for s in t.spans():
            if s.kind == "llm":
                _count_llm(s, provs, llm_vals, m, is_demo=str(t.metadata.get("mode")) == "demo")
                if t.metadata.get("mode") != "demo" and any(_is_failure_event(e) for e in s.events):
                    lower_bound = True
            else:
                key = stage_key(s)
                if key is not None and s.end_ms is not None:
                    stage_vals.setdefault(key, []).append(s.duration_ms)
    m.stage_latency_ms = {k: Quantiles.of(v) for k, v in sorted(stage_vals.items())}
    m.llm_latency_ms = {k: Quantiles.of(v) for k, v in sorted(llm_vals.items())}
    m.providers = provs
    m.cost_is_lower_bound = lower_bound
    if m.n_ads:
        m.recheck_rate = m.n_rechecks / m.n_ads

    # --- 실행 기록이 없고 트레이스만 있을 때: 겉으로 알 수 있는 것만 채운다 ---------------------------------
    if not runs and traces:
        pre = [t for t in traces if t.name == "preflight"]
        m.n_runs = len(pre)
        m.run_latency_ms = Quantiles.of(t.duration_ms for t in pre if t.root and t.root.end_ms is not None)
        m.run_cost_usd = Quantiles.of(trace_cost)
        m.modes = {}
        for t in pre:
            md = str(t.metadata.get("mode", "?"))
            m.modes[md] = m.modes.get(md, 0) + 1
        m.synthetic_only = bool(pre) and all(str(t.metadata.get("mode")) == "demo" for t in pre)
        for t in pre:
            for s in t.spans():
                if s.name == "adjudicate" and "final" in s.attributes:
                    verdicts[str(s.attributes["final"])] = verdicts.get(str(s.attributes["final"]), 0) + 1
        m.verdicts = verdicts
        m.n_ads = sum(verdicts.values())
        m.human_review_rate = verdicts.get(Decision.HUMAN_REVIEW.value, 0) / m.n_ads if m.n_ads else None
        m.cost_per_ad_usd = (m.run_cost_usd.total / m.n_ads) if m.n_ads else None
        if m.n_ads:
            m.recheck_rate = m.n_rechecks / m.n_ads
        caveats.append("처리 기록(PipelineRun) 없이 트레이스만 받아 계산했다 — 실패 모드(FM)·인용 비율은 알 수 없고 판정은 판정 단계 스팬에서 읽었다.")

    if m.synthetic_only and (runs or traces):
        caveats.append("모든 실행이 예시 실행(대역)이다 — 비용 0원과 지연은 대역의 값이며 모델의 지연·비용이 아니다.")
    if m.cost_is_lower_bound:
        caveats.append("응답을 받지 못한 요청이 있어 비용은 최소값이다 (그 요청의 비용은 기록에 없다).")
    if m.n_ads and m.n_ads < 100:
        caveats.append(f"광고안 {m.n_ads}건뿐이라 비율과 p95·p99 가 크게 흔들린다 (p99 는 사실상 최댓값).")
    if not runs and not traces:
        caveats.append("계산할 처리 기록이 없다.")
    m.caveats = caveats
    return m


def _fm_names() -> dict[str, str]:
    try:
        from guardrails.failure_modes import BY_CODE

        return {c: fm.name for c, fm in BY_CODE.items()}
    except Exception:  # noqa: BLE001 — 카탈로그를 못 읽어도 지표 계산은 계속한다
        return {}


def _count_llm(s: Span, provs: dict[str, ProviderStats], llm_vals: dict[str, list[float]], m: Metrics, is_demo: bool) -> None:
    for e in s.events:
        if isinstance(e, dict) and _is_failure_event(e) and e.get("provider"):
            p = provs.setdefault(str(e["provider"]), ProviderStats(str(e["provider"]), synthetic=str(e["provider"]) == "mock"))
            p.failures += 1
        elif isinstance(e, dict) and e.get("name") == "skip_provider" and e.get("provider"):
            p = provs.setdefault(str(e["provider"]), ProviderStats(str(e["provider"]), synthetic=str(e["provider"]) == "mock"))
            p.circuit_skips += 1
    name = s.attributes.get("provider")
    if not name or s.status == "error" and "cost_usd" not in s.attributes:
        return                       # 응답을 돌려준 호출만 센다 (끝내 실패한 호출은 위 이벤트로 이미 셌다)
    name = str(name)
    p = provs.setdefault(name, ProviderStats(name, synthetic=name == "mock"))
    p.answered += 1
    m.llm_calls += 1
    chain = s.attributes.get("fallback_chain") or []
    planned = s.attributes.get("chain") or []
    is_fb = s.status == "fallback" or (isinstance(chain, list) and len(chain) > 1) or (isinstance(planned, list) and planned and planned[0] != name)
    if is_fb:
        p.fallback_answers += 1
        m.llm_fallback_calls += 1
    lat = s.attributes.get("latency_ms")
    if name != "mock" and isinstance(lat, (int, float)):
        llm_vals.setdefault(name, []).append(float(lat))


def metrics_from_tracer(tracer: Any, runs: Sequence[PipelineRun] = ()) -> Metrics:
    """세션의 트레이서(저장된 트레이스 목록)와 처리 기록에서 지표를 계산한다."""
    return compute_metrics(runs, list(getattr(tracer, "traces", []) or []))


# ---------------------------------------------------------------------------
# Prometheus 텍스트 노출 형식 (text exposition format 0.0.4)
# ---------------------------------------------------------------------------
def escape_label_value(v: Any) -> str:
    """라벨 값 이스케이프: 역슬래시 → \\\\, 큰따옴표 → \\\", 줄바꿈 → \\n (노출 형식 규칙)."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n")


def _num(v: float) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return f"{float(v):.10g}"


class _Exposition:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def family(self, name: str, kind: str, help_: str) -> None:
        self.lines.append(f"# HELP {name} {_escape_help(help_)}")
        self.lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, value: float, labels: Optional[dict[str, Any]] = None) -> None:
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{escape_label_value(v)}"' for k, v in labels.items()) + "}"
        self.lines.append(f"{name}{lab} {_num(value)}")


def _prometheus(m: Metrics, prefix: str) -> str:
    x = _Exposition()
    p = prefix.rstrip("_")
    x.family(f"{p}_runs_total", "counter", "이 스냅숏에 든 실행 수 (mode 별)")
    for mode, n in sorted(m.modes.items()) or [("none", 0)]:
        x.sample(f"{p}_runs_total", n, {"mode": mode})
    x.family(f"{p}_halted_runs_total", "counter", "도중에 멈춘 실행 수")
    x.sample(f"{p}_halted_runs_total", m.n_halted)
    x.family(f"{p}_synthetic_only", "gauge", "1 이면 모든 실행이 예시 실행(대역)이라 비용·지연이 모델 값이 아님")
    x.sample(f"{p}_synthetic_only", 1 if m.synthetic_only else 0)
    x.family(f"{p}_verdicts_total", "counter", "최종 판정 건수")
    for d in Decision:
        x.sample(f"{p}_verdicts_total", m.verdicts.get(d.value, 0), {"decision": d.value})
    if m.human_review_rate is not None:
        x.family(f"{p}_human_review_ratio", "gauge", "담당자 확인으로 넘긴 광고안 비율")
        x.sample(f"{p}_human_review_ratio", m.human_review_rate)
    if m.stage_latency_ms:
        x.family(f"{p}_stage_duration_seconds", "summary", "단계별 경과 시간(트레이스 스팬)")
        for stage, q in m.stage_latency_ms.items():
            for qn, val in (("0.5", q.p50), ("0.95", q.p95), ("0.99", q.p99)):
                x.sample(f"{p}_stage_duration_seconds", val / 1000.0, {"stage": stage, "quantile": qn})
            x.sample(f"{p}_stage_duration_seconds_sum", q.total / 1000.0, {"stage": stage})
            x.sample(f"{p}_stage_duration_seconds_count", q.n, {"stage": stage})
    if m.run_latency_ms.n:
        q = m.run_latency_ms
        x.family(f"{p}_run_duration_seconds", "summary", "실행 한 번의 총 경과 시간")
        for qn, val in (("0.5", q.p50), ("0.95", q.p95), ("0.99", q.p99)):
            x.sample(f"{p}_run_duration_seconds", val / 1000.0, {"quantile": qn})
        x.sample(f"{p}_run_duration_seconds_sum", q.total / 1000.0)
        x.sample(f"{p}_run_duration_seconds_count", q.n)
    if m.run_cost_usd.n:
        x.family(f"{p}_run_cost_usd_total", "counter", "실행 비용 합계(USD). cost_lower_bound=1 이면 최소값")
        x.sample(f"{p}_run_cost_usd_total", m.run_cost_usd.total, {"cost_lower_bound": "1" if m.cost_is_lower_bound else "0"})
    if m.cost_per_ad_usd is not None:
        x.family(f"{p}_cost_per_ad_usd", "gauge", "광고안당 평균 비용(USD)")
        x.sample(f"{p}_cost_per_ad_usd", m.cost_per_ad_usd)
    if m.providers:
        x.family(f"{p}_provider_answers_total", "counter", "제공자가 응답을 돌려준 호출 수")
        for name, st in sorted(m.providers.items()):
            x.sample(f"{p}_provider_answers_total", st.answered, {"provider": name})
        x.family(f"{p}_provider_failures_total", "counter", "제공자에서 난 실패 수")
        for name, st in sorted(m.providers.items()):
            x.sample(f"{p}_provider_failures_total", st.failures, {"provider": name})
        x.family(f"{p}_provider_fallback_answers_total", "counter", "1순위가 아닌데 이 제공자가 대신 응답한 호출 수")
        for name, st in sorted(m.providers.items()):
            x.sample(f"{p}_provider_fallback_answers_total", st.fallback_answers, {"provider": name})
    x.family(f"{p}_failure_mode_total", "counter", "실패 모드(FM) 발생 건수")
    for code, d in sorted(m.failure_modes.items()):
        x.sample(f"{p}_failure_mode_total", d["count"], {"code": code, "name": d["name"]})
    if m.recheck_rate is not None:
        x.family(f"{p}_recheck_ratio", "gauge", "고친 문구 다시 검사 수 / 광고안 수")
        x.sample(f"{p}_recheck_ratio", m.recheck_rate)
    if m.unverified_quote_rate is not None:
        x.family(f"{p}_unverified_quote_ratio", "gauge", "검토 모델이 낸 근거 중 원문에서 찾지 못한 비율")
        x.sample(f"{p}_unverified_quote_ratio", m.unverified_quote_rate)
    return "\n".join(x.lines) + "\n"
