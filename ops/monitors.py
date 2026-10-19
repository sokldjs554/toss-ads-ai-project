"""온라인 모니터링 규칙 — 실행 기록 스트림을 기준 구간과 견주어 '평소와 다른 것' 을 알린다.

규칙 여섯 개 (각각 임계값·최소 표본·심각도·런북 절 번호를 `RULE_SPECS` 에 적어 두었다)
1. verdict_drift          판정 분포 변화 — PSI 로 크기를 보고 카이제곱 검정으로 우연인지 거른다
2. human_review_surge     담당자 확인율 급증 — 창 안 비율의 윌슨 하한이 기준선을 넘을 때
3. cost_anomaly           비용 이상 — 창 중앙값의 로버스트 z (중앙값·MAD)
4. latency_slo_burn       지연 SLO 번 레이트 — 오류 예산을 얼마나 빨리 쓰는지 (긴 창 + 짧은 창)
5. provider_failure_surge 제공자 실패율 급증
6. citation_surge         검토 모델 인용 중 원문에 없던 비율 급증

표본이 모자라면 판단하지 않고 '보류'(insufficient) 로 답한다. 보류는 정상이 아니다 — 아직 모른다는 뜻이다.

이 모듈의 임계값은 모두 '이 프로젝트가 가정한 값' 이다
- PSI 0.10/0.25 와 로버스트 z 3.5 는 각각 신용평가 모형 모니터링 관행·Iglewicz–Hoaglin 이상치 규칙으로 널리 쓰이는 경험칙을 빌렸고, 이 프로젝트의 데이터로 보정하지 않았다.
- 지연 SLO(`ASSUMED_LATENCY_SLO_MS`)·오류 예산·번 레이트 배수는 SRE 관행(다중 창 번 레이트)을 빌려 정한 '제안' 이다. 실제 서비스의 지연 분포를 잰 적이 없다.
- 기준 구간은 호출하는 쪽이 넘긴다. 기준 구간이 이미 비정상이었다면 그 비정상을 정상으로 배운다.

`simulate_and_score()` 가 재는 것과 의미하지 않는 것
- 합성 스트림(정상·드리프트·제공자 장애·비용 폭증·…)에 규칙을 돌려 '탐지 지연' 과 '오경보' 를 센다. 스트림의 분포·변화 크기는 이 프로젝트가 정한 예시이며
  실제 트래픽에서 잰 것이 아니다. 따라서 이 숫자는 "이 규칙이 이런 가정의 변화를 이 정도로 잡는다" 이지 "실제 서비스에서 이만큼 잡는다" 가 아니다.
- 실행 안의 광고안은 서로 닮는다. 시뮬레이션은 이를 흉내 내려고 실행마다 판정 확률을 흔든다(디리클레). 실제 닮음의 정도는 모른다.
- 임계값은 시뮬레이션을 돌리기 전에 정했다. 결과가 나쁘면 나쁜 대로 보고한다. 오경보 0 을 요구하지 않고 오경보율을 보고한다.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from core.schemas import Decision, Layer, PipelineRun
from evals.metrics import wilson

# --- 가정한 기준값 (이 프로젝트가 정한 값 — 실제 서비스 SLO 가 아니다) -----------------------------------
DECISION_ORDER = (Decision.APPROVE.value, Decision.REVISE.value, Decision.REJECT.value, Decision.HUMAN_REVIEW.value)
PSI_WARN = 0.10            # 경험칙: 0.1 미만 변화 없음, 0.1~0.25 보통, 0.25 초과 큼 (신용 모형 모니터링 관행)
PSI_ALERT = 0.25
PSI_SMOOTHING = 0.5        # 빈 칸의 로그 0 을 피하는 라플라스 보정(칸마다 더하는 가상 건수)
CHI2_ALPHA = 0.001         # 같은 분포라는 가설의 기각 수준. 실행 안 광고안이 닮아 독립 가정이 틀리므로 작게 잡았다
ROBUST_Z_ALERT = 4.0       # 창 중앙값의 로버스트 z (표준오차를 1.2533·σ/√n 으로 본 값)
COST_MIN_RATIO = 1.25      # 통계적으로 다르더라도 기준 중앙값의 1.25배 미만이면 실무상 의미 없다고 본다
COST_SCALE_FLOOR = 0.10    # MAD 가 0 에 가까울 때 척도의 하한 = 기준 중앙값의 10%
ASSUMED_LATENCY_SLO_MS = 90_000.0   # 가정: 실행 한 번이 90초 안에 끝나야 '좋은' 실행. 근거: traces/ 에 기록된 실제 모델 실행 4건이 29~37초(n=4)였고 그 약 2.4배의 여유를 뒀다. 분포의 꼬리는 잰 적이 없다
ASSUMED_SLO_TARGET = 0.95           # 가정: 좋은 실행이 95% 이상 → 오류 예산 5%
BURN_PAGE = 6.0            # 번 레이트 6배 = 오류 예산을 정해진 속도의 6배로 씀 (SRE 다중 창 번 레이트 관행에서 빌린 배수)
BURN_TICKET = 2.0
SURGE_RATIO = {"human_review_surge": 2.0, "provider_failure_surge": 3.0, "citation_surge": 2.0}   # 기준선의 몇 배를 넘어야 급증인가
SURGE_DELTA = {"human_review_surge": 0.05, "provider_failure_surge": 0.05, "citation_surge": 0.05}  # 그리고 기준선보다 절대값으로 이만큼 높아야


@dataclass(frozen=True)
class MonitorConfig:
    window_runs: int = 50            # 현재 창: 최근 실행 수
    burn_long_runs: int = 60         # 번 레이트 긴 창
    burn_short_runs: int = 12        # 번 레이트 짧은 창
    min_ads_window: int = 100        # 판정 분포·담당자 확인율의 창 최소 광고안 수
    min_ads_baseline: int = 200      # 기준 구간 최소 광고안 수
    min_runs_window: int = 20        # 비용 창 최소 실행 수
    min_runs_baseline: int = 30
    min_attempts_window: int = 30    # 제공자 호출 최소 수 (제공자별)
    min_attempts_baseline: int = 100
    min_findings_window: int = 30    # 인용 비율: 창 안 검토 모델 근거 최소 수
    min_findings_baseline: int = 100
    min_runs_burn_long: int = 30
    min_runs_burn_short: int = 6
    latency_slo_ms: float = ASSUMED_LATENCY_SLO_MS
    slo_target: float = ASSUMED_SLO_TARGET
    psi_warn: float = PSI_WARN
    psi_alert: float = PSI_ALERT
    chi2_alpha: float = CHI2_ALPHA
    robust_z: float = ROBUST_Z_ALERT
    burn_page: float = BURN_PAGE
    burn_ticket: float = BURN_TICKET


@dataclass(frozen=True)
class RuleSpec:
    name: str
    title: str          # 화면에 보이는 쉬운 말
    severity: str       # 낼 수 있는 가장 높은 심각도 (warn | critical)
    runbook: str        # docs/OPERATIONS.md 의 절
    min_samples: str
    basis: str          # 임계값의 근거 (가정임을 밝힌다)


RULE_SPECS: dict[str, RuleSpec] = {s.name: s for s in (
    RuleSpec("verdict_drift", "판정 분포가 평소와 달라짐", "critical", "§4.1.1",
             "창 광고안 100건 이상, 기준 구간 200건 이상", f"PSI ≥ {PSI_WARN} 경고, ≥ {PSI_ALERT} 심각 (경험칙) 이면서 카이제곱 p < {CHI2_ALPHA}"),
    RuleSpec("human_review_surge", "담당자 확인 비율이 급증", "warn", "§4.1.2",
             "창 광고안 100건 이상, 기준 구간 200건 이상", "창 비율의 윌슨 95% 하한이 max(기준×2, 기준+0.05) 초과 (가정)"),
    RuleSpec("cost_anomaly", "실행당 비용이 평소보다 큼", "warn", "§4.1.3",
             "창 실행 20건 이상, 기준 구간 30건 이상", f"창 중앙값의 로버스트 z ≥ {ROBUST_Z_ALERT}, 그리고 기준 중앙값의 {COST_MIN_RATIO}배 이상"),
    RuleSpec("latency_slo_burn", "처리 시간이 약속(SLO)을 빠르게 소진", "critical", "§4.1.4",
             "긴 창 실행 30건 이상, 짧은 창 6건 이상", f"오류 예산 소진 속도 {BURN_TICKET}배 경고·{BURN_PAGE}배 심각, 긴 창과 짧은 창이 함께 넘을 때 (SRE 관행에서 빌린 배수)"),
    RuleSpec("provider_failure_surge", "모델 제공자 실패가 급증", "critical", "§4.1.5",
             "창 호출 30회 이상, 기준 구간 100회 이상 (제공자별)", "창 실패율의 윌슨 하한이 max(기준×3, 기준+0.05) 초과 (가정)"),
    RuleSpec("citation_surge", "검토 모델 인용이 원문에 없는 비율이 급증", "warn", "§4.1.6",
             "창 근거 30개 이상, 기준 구간 100개 이상", "창 비율의 윌슨 하한이 max(기준×2, 기준+0.05) 초과 (가정)"),
)}


@dataclass(frozen=True)
class RunRecord:
    """모니터가 보는 실행 한 번의 요약. PipelineRun 에서 만들거나(`RunRecord.from_run`) 시뮬레이션이 직접 만든다."""

    index: int
    verdicts: tuple[int, int, int, int]       # DECISION_ORDER 순서의 광고안 수
    latency_ms: float
    cost_usd: float
    provider_attempts: Mapping[str, int] = field(default_factory=dict)
    provider_failures: Mapping[str, int] = field(default_factory=dict)
    semantic_findings: int = 0
    unverified_quotes: int = 0
    mode: str = "live"

    @property
    def n_ads(self) -> int:
        return sum(self.verdicts)

    @classmethod
    def from_run(cls, run: PipelineRun, trace: Any = None, index: int = 0) -> "RunRecord":
        counts = {d: 0 for d in DECISION_ORDER}
        sem = unv = 0
        for rep in run.reports:
            counts[rep.final_decision.value] += 1
            for f in rep.verdict.findings:
                if f.layer == Layer.SEMANTIC:
                    sem += 1
                    unv += int(not f.verified_quote)
        att: dict[str, int] = {}
        fail: dict[str, int] = {}
        if trace is not None:
            from ops.metrics import compute_metrics

            for name, st in compute_metrics((), [trace]).providers.items():
                if not st.synthetic:                      # 대역의 호출은 실제 제공자의 실패율이 아니다
                    att[name], fail[name] = st.attempts, st.failures
        return cls(index=index, verdicts=tuple(counts[d] for d in DECISION_ORDER), latency_ms=float(run.total_latency_ms), cost_usd=float(run.total_cost_usd),  # type: ignore[arg-type]
                   provider_attempts=att, provider_failures=fail, semantic_findings=sem, unverified_quotes=unv, mode=run.mode)


def records_from_runs(runs: Sequence[PipelineRun], traces: Sequence[Any] = ()) -> list[RunRecord]:
    """처리 기록 목록 → 모니터 입력. trace_id 가 같은 트레이스가 있으면 제공자 호출 수를 함께 읽는다."""
    by_id = {t.trace_id: t for t in traces}
    return [RunRecord.from_run(r, by_id.get(r.trace_id), i) for i, r in enumerate(runs)]


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: str                 # ok · warn · critical · insufficient
    value: Optional[float]      # 이 규칙이 본 값 (PSI, 비율, z, 번 레이트 …)
    threshold: Optional[float]
    n_window: int
    n_baseline: int
    message: str
    runbook: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return self.status in ("warn", "critical")


# ---------------------------------------------------------------------------
# 통계 도구
# ---------------------------------------------------------------------------
def psi(window_counts: Sequence[int], baseline_counts: Sequence[int], smoothing: float = PSI_SMOOTHING) -> float:
    """Population Stability Index = Σ (w_i − b_i)·ln(w_i / b_i). 칸마다 가상 건수를 더해 빈 칸을 처리한다."""
    k = len(window_counts)
    if k != len(baseline_counts) or k < 2:
        raise ValueError("두 분포의 칸 수가 같고 2 이상이어야 합니다")
    nw, nb = sum(window_counts) + smoothing * k, sum(baseline_counts) + smoothing * k
    total = 0.0
    for w, b in zip(window_counts, baseline_counts):
        pw, pb = (w + smoothing) / nw, (b + smoothing) / nb
        total += (pw - pb) * math.log(pw / pb)
    return total


def _gammaincc(a: float, x: float) -> float:
    """정규화된 위쪽 불완전 감마 Q(a, x) (Numerical Recipes 의 급수 + 연분수)."""
    if x <= 0:
        return 1.0
    lg = math.lgamma(a)
    if x < a + 1:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-15:
                break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - lg))
    tiny = 1e-300
    b = x + 1 - a
    c, d = 1.0 / tiny, 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - lg) * h)


def chi2_sf(x: float, df: int) -> float:
    """카이제곱 분포의 생존 함수 P(X ≥ x). scipy 없이 계산한다."""
    if df < 1:
        raise ValueError("자유도는 1 이상이어야 합니다")
    return _gammaincc(df / 2.0, max(x, 0.0) / 2.0)


def chi2_homogeneity(a: Sequence[int], b: Sequence[int]) -> tuple[float, int, float]:
    """두 표본이 같은 범주 분포에서 나왔는지 보는 카이제곱 동질성 검정. (통계량, 자유도, p값). 양쪽 합이 0 인 칸은 뺀다."""
    na, nb = sum(a), sum(b)
    if na == 0 or nb == 0:
        return 0.0, 0, 1.0
    stat, cells = 0.0, 0
    for ai, bi in zip(a, b):
        tot = ai + bi
        if tot == 0:
            continue
        cells += 1
        ea, eb = tot * na / (na + nb), tot * nb / (na + nb)
        stat += (ai - ea) ** 2 / ea + (bi - eb) ** 2 / eb
    df = cells - 1
    if df < 1:
        return 0.0, 0, 1.0
    return stat, df, chi2_sf(stat, df)


def robust_center_scale(values: Sequence[float], floor_ratio: float = COST_SCALE_FLOOR) -> tuple[float, float]:
    """(중앙값, 로버스트 σ = 1.4826·MAD). MAD 가 거의 0 이면 중앙값의 floor_ratio 배를 하한으로 둔다."""
    v = sorted(values)
    n = len(v)
    med = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    dev = sorted(abs(x - med) for x in v)
    mad = dev[n // 2] if n % 2 else (dev[n // 2 - 1] + dev[n // 2]) / 2
    return med, max(1.4826 * mad, floor_ratio * abs(med))


def _median(values: Sequence[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def burn_rate(bad: int, total: int, slo_target: float) -> float:
    """오류 예산 소진 속도 = 나쁜 비율 ÷ (1 − 목표). 1 이면 정해진 속도, 6 이면 6배 빠르게 소진."""
    if total <= 0:
        raise ValueError("total 은 1 이상이어야 합니다")
    budget = 1.0 - slo_target
    if budget <= 0:
        raise ValueError("목표가 100% 이면 오류 예산이 0 이라 번 레이트를 정의할 수 없습니다")
    return (bad / total) / budget


# ---------------------------------------------------------------------------
# 규칙
# ---------------------------------------------------------------------------
def _insufficient(rule: str, nw: int, nb: int, why: str) -> RuleResult:
    return RuleResult(rule, "insufficient", None, None, nw, nb, f"표본 부족 — 판단 보류. {why}", RULE_SPECS[rule].runbook)


def _tail(seq: Sequence[RunRecord], n: int) -> Sequence[RunRecord]:
    return seq[-n:] if n > 0 else seq[:0]


def _verdict_counts(recs: Iterable[RunRecord]) -> list[int]:
    out = [0, 0, 0, 0]
    for r in recs:
        for i, c in enumerate(r.verdicts):
            out[i] += c
    return out


def rule_verdict_drift(history: Sequence[RunRecord], baseline: Sequence[RunRecord], cfg: MonitorConfig) -> RuleResult:
    win = _tail(history, cfg.window_runs)
    wc, bc = _verdict_counts(win), _verdict_counts(baseline)
    nw, nb = sum(wc), sum(bc)
    if nw < cfg.min_ads_window or nb < cfg.min_ads_baseline:
        return _insufficient("verdict_drift", nw, nb, f"창 광고안 {nw}건(최소 {cfg.min_ads_window}), 기준 구간 {nb}건(최소 {cfg.min_ads_baseline}).")
    value = psi(wc, bc)
    stat, df, p = chi2_homogeneity(wc, bc)
    significant = p < cfg.chi2_alpha
    status = "ok"
    if significant and value >= cfg.psi_alert:
        status = "critical"
    elif significant and value >= cfg.psi_warn:
        status = "warn"
    note = "" if status != "ok" or value < cfg.psi_warn or significant else f" (PSI 는 {cfg.psi_warn} 이상이지만 우연으로 볼 수 있어 알리지 않음, p={p:.3g})"
    return RuleResult("verdict_drift", status, round(value, 4), cfg.psi_warn, nw, nb,
                      f"PSI {value:.3f} (경고 {cfg.psi_warn}, 심각 {cfg.psi_alert}), 카이제곱 p={p:.3g}{note}", RULE_SPECS["verdict_drift"].runbook,
                      {"psi": value, "chi2": stat, "df": df, "p": p, "window": dict(zip(DECISION_ORDER, wc)), "baseline": dict(zip(DECISION_ORDER, bc))})


def _surge(rule: str, kw: int, nw: int, kb: int, nb: int, min_w: int, min_b: int, severity: str, unit: str, extra: Optional[Mapping[str, Any]] = None) -> RuleResult:
    if nw < min_w or nb < min_b:
        return _insufficient(rule, nw, nb, f"창 {unit} {nw}(최소 {min_w}), 기준 구간 {nb}(최소 {min_b}).")
    pw, pb = kw / nw, kb / nb
    thr = max(SURGE_RATIO[rule] * pb, pb + SURGE_DELTA[rule])
    lo = wilson(kw, nw)[0]
    fired = lo > thr
    return RuleResult(rule, severity if fired else "ok", round(pw, 4), round(thr, 4), nw, nb,
                      f"창 {pw:.1%} ({kw}/{nw}), 기준 {pb:.1%} ({kb}/{nb}). 창 비율의 95% 하한 {lo:.1%} 가 임계 {thr:.1%} 를 {'넘음' if fired else '넘지 않음'}.",
                      RULE_SPECS[rule].runbook, {"window_rate": pw, "baseline_rate": pb, "wilson_lower": lo, **(extra or {})})


def rule_human_review_surge(history: Sequence[RunRecord], baseline: Sequence[RunRecord], cfg: MonitorConfig) -> RuleResult:
    win = _tail(history, cfg.window_runs)
    wc, bc = _verdict_counts(win), _verdict_counts(baseline)
    return _surge("human_review_surge", wc[3], sum(wc), bc[3], sum(bc), cfg.min_ads_window, cfg.min_ads_baseline, "warn", "광고안")


def rule_citation_surge(history: Sequence[RunRecord], baseline: Sequence[RunRecord], cfg: MonitorConfig) -> RuleResult:
    win = _tail(history, cfg.window_runs)
    kw, nw = sum(r.unverified_quotes for r in win), sum(r.semantic_findings for r in win)
    kb, nb = sum(r.unverified_quotes for r in baseline), sum(r.semantic_findings for r in baseline)
    return _surge("citation_surge", kw, nw, kb, nb, cfg.min_findings_window, cfg.min_findings_baseline, "warn", "근거")


def rule_provider_failure_surge(history: Sequence[RunRecord], baseline: Sequence[RunRecord], cfg: MonitorConfig) -> RuleResult:
    win = _tail(history, cfg.window_runs)
    providers = sorted({p for r in list(win) + list(baseline) for p in r.provider_attempts})
    if not providers:
        return _insufficient("provider_failure_surge", 0, 0, "제공자 호출 기록이 없습니다(예시 실행은 대역이라 세지 않습니다).")
    results = []
    for p in providers:
        kw, nw = sum(r.provider_failures.get(p, 0) for r in win), sum(r.provider_attempts.get(p, 0) for r in win)
        kb, nb = sum(r.provider_failures.get(p, 0) for r in baseline), sum(r.provider_attempts.get(p, 0) for r in baseline)
        res = _surge("provider_failure_surge", kw, nw, kb, nb, cfg.min_attempts_window, cfg.min_attempts_baseline, "critical", "호출", {"provider": p})
        results.append((p, res))
    fired = [(p, r) for p, r in results if r.fired]
    if fired:
        p, r = max(fired, key=lambda t: t[1].value or 0)
        return RuleResult(r.rule, r.status, r.value, r.threshold, r.n_window, r.n_baseline, f"{p}: {r.message}", r.runbook, {**r.detail, "providers": {q: x.status for q, x in results}})
    decided = [(p, r) for p, r in results if r.status != "insufficient"]
    if not decided:
        p, r = results[0]
        return RuleResult(r.rule, r.status, r.value, r.threshold, r.n_window, r.n_baseline, r.message, r.runbook, {"providers": {q: x.status for q, x in results}})
    p, r = max(decided, key=lambda t: t[1].value or 0)
    return RuleResult(r.rule, "ok", r.value, r.threshold, r.n_window, r.n_baseline, f"{p}: {r.message}", r.runbook, {**r.detail, "providers": {q: x.status for q, x in results}})


def rule_cost_anomaly(history: Sequence[RunRecord], baseline: Sequence[RunRecord], cfg: MonitorConfig) -> RuleResult:
    win = _tail(history, cfg.window_runs)
    nw, nb = len(win), len(baseline)
    if nw < cfg.min_runs_window or nb < cfg.min_runs_baseline:
        return _insufficient("cost_anomaly", nw, nb, f"창 실행 {nw}건(최소 {cfg.min_runs_window}), 기준 구간 {nb}건(최소 {cfg.min_runs_baseline}).")
    med_b, scale = robust_center_scale([r.cost_usd for r in baseline])
    if med_b <= 0:
        return _insufficient("cost_anomaly", nw, nb, "기준 구간의 비용 중앙값이 0 입니다 — 예시 실행(대역)이거나 비용이 기록되지 않았습니다.")
    med_w = _median([r.cost_usd for r in win])
    se = 1.2533 * scale / math.sqrt(nw)
    z = (med_w - med_b) / se
    ratio = med_w / med_b
    fired = z >= cfg.robust_z and ratio >= COST_MIN_RATIO
    return RuleResult("cost_anomaly", "warn" if fired else "ok", round(z, 3), cfg.robust_z, nw, nb,
                      f"창 중앙값 ${med_w:.4f} / 기준 ${med_b:.4f} = {ratio:.2f}배, 로버스트 z {z:.1f} (임계 {cfg.robust_z}, 최소 {COST_MIN_RATIO}배).",
                      RULE_SPECS["cost_anomaly"].runbook, {"median_window": med_w, "median_baseline": med_b, "ratio": ratio, "z": z})


def rule_latency_slo_burn(history: Sequence[RunRecord], baseline: Sequence[RunRecord], cfg: MonitorConfig) -> RuleResult:
    long_w, short_w = _tail(history, cfg.burn_long_runs), _tail(history, cfg.burn_short_runs)
    nl, ns = len(long_w), len(short_w)
    if nl < cfg.min_runs_burn_long or ns < cfg.min_runs_burn_short:
        return _insufficient("latency_slo_burn", nl, 0, f"긴 창 실행 {nl}건(최소 {cfg.min_runs_burn_long}), 짧은 창 {ns}건(최소 {cfg.min_runs_burn_short}).")
    bad_l = sum(r.latency_ms > cfg.latency_slo_ms for r in long_w)
    bad_s = sum(r.latency_ms > cfg.latency_slo_ms for r in short_w)
    bl, bs = burn_rate(bad_l, nl, cfg.slo_target), burn_rate(bad_s, ns, cfg.slo_target)
    both = min(bl, bs)
    status = "critical" if both >= cfg.burn_page else ("warn" if both >= cfg.burn_ticket else "ok")
    return RuleResult("latency_slo_burn", status, round(both, 3), cfg.burn_ticket, nl, 0,
                      f"{cfg.latency_slo_ms / 1000:.0f}초 초과 실행: 긴 창 {bad_l}/{nl}(번 레이트 {bl:.1f}), 짧은 창 {bad_s}/{ns}(번 레이트 {bs:.1f}). 목표 {cfg.slo_target:.0%} → 예산 {1 - cfg.slo_target:.0%}.",
                      RULE_SPECS["latency_slo_burn"].runbook, {"burn_long": bl, "burn_short": bs, "bad_long": bad_l, "bad_short": bad_s})


_RULE_FUNCS = {
    "verdict_drift": rule_verdict_drift, "human_review_surge": rule_human_review_surge, "cost_anomaly": rule_cost_anomaly,
    "latency_slo_burn": rule_latency_slo_burn, "provider_failure_surge": rule_provider_failure_surge, "citation_surge": rule_citation_surge,
}


def evaluate(history: Sequence[RunRecord], baseline: Sequence[RunRecord], config: Optional[MonitorConfig] = None,
             rules: Optional[Iterable[str]] = None) -> list[RuleResult]:
    """지금까지의 기록(history, 시간순)을 기준 구간(baseline)과 견줘 규칙마다 판정한다. 기준 구간은 history 에 들어 있지 않아야 한다."""
    cfg = config or MonitorConfig()
    names = list(rules) if rules is not None else list(_RULE_FUNCS)
    unknown = [n for n in names if n not in _RULE_FUNCS]
    if unknown:
        raise ValueError(f"알 수 없는 규칙: {unknown}")
    return [_RULE_FUNCS[n](history, baseline, cfg) for n in names]


# ---------------------------------------------------------------------------
# 시뮬레이션: 탐지 지연과 오경보를 잰다
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    expected: tuple[str, ...]                 # 이 변화를 알아채야 하는 규칙
    verdict_probs: Optional[tuple[float, float, float, float]] = None   # 변화 후 판정 확률 (None 이면 기준 그대로)
    failure_rate: Optional[Mapping[str, float]] = None                  # 변화 후 제공자별 실패율
    latency_factor: float = 1.0
    cost_factor: float = 1.0
    unverified_rate: Optional[float] = None


# 기준(정상) 분포 — 모두 예시이며 실제 트래픽에서 잰 값이 아니다
BASE_VERDICT_PROBS = (0.40, 0.30, 0.20, 0.10)
BASE_FAILURE_RATE = {"openai": 0.01, "anthropic": 0.01}
BASE_ATTEMPTS = {"openai": 8, "anthropic": 4}
BASE_LATENCY_MS = 33_000.0     # 실행 지연 중앙값 (예시: traces/ 의 실제 모델 실행 4건 29~37초의 가운데. 꼬리의 퍼짐 σ=0.35 는 가정)
BASE_COST_USD = 0.12           # 실행 비용 중앙값 (예시: 같은 4건이 $0.11~0.14. 퍼짐 σ=0.25 는 가정)
BASE_UNVERIFIED_RATE = 0.04
ADS_PER_RUN = 3
DIRICHLET_CONCENTRATION = 20.0  # 작을수록 실행마다 판정 구성이 많이 흔들림(과산포)

SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("normal", "정상 (변화 없음)", ()),
    Scenario("drift_moderate", "판정 분포가 보통 정도로 이동 (통과↓ 반려↑)", ("verdict_drift",), verdict_probs=(0.25, 0.30, 0.35, 0.10)),
    Scenario("drift_large", "판정 분포가 크게 이동", ("verdict_drift",), verdict_probs=(0.20, 0.25, 0.45, 0.10)),
    Scenario("review_surge", "담당자 확인 비율이 10% → 25%", ("human_review_surge",), verdict_probs=(0.35, 0.25, 0.15, 0.25)),
    Scenario("provider_outage", "한 제공자(openai) 실패율이 1% → 50%, 지연 2.5배", ("provider_failure_surge",), failure_rate={"openai": 0.5, "anthropic": 0.01}, latency_factor=2.5),
    Scenario("cost_spike", "실행당 비용 3배", ("cost_anomaly",), cost_factor=3.0),
    Scenario("slow_down", "처리 시간 2배", ("latency_slo_burn",), latency_factor=2.0),
    Scenario("citation_surge", "원문에 없는 인용 비율 4% → 20%", ("citation_surge",), unverified_rate=0.20),
)}


def _dirichlet(rng: random.Random, alpha: Sequence[float]) -> list[float]:
    g = [rng.gammavariate(a, 1.0) for a in alpha]
    s = sum(g)
    return [x / s for x in g]


def _binomial(rng: random.Random, n: int, p: float) -> int:
    return sum(rng.random() < p for _ in range(n))


def generate_stream(scenario: Scenario, n_runs: int, change_at: int, seed: int) -> list[RunRecord]:
    """결정론적 합성 스트림. change_at 번째 실행부터 시나리오의 변화가 적용된다(정상 시나리오는 변화 없음)."""
    rng = random.Random(seed)
    out: list[RunRecord] = []
    for i in range(n_runs):
        changed = i >= change_at
        probs = scenario.verdict_probs if (changed and scenario.verdict_probs) else BASE_VERDICT_PROBS
        fr = scenario.failure_rate if (changed and scenario.failure_rate) else BASE_FAILURE_RATE
        lat_f = scenario.latency_factor if changed else 1.0
        cost_f = scenario.cost_factor if changed else 1.0
        unv = scenario.unverified_rate if (changed and scenario.unverified_rate is not None) else BASE_UNVERIFIED_RATE
        run_p = _dirichlet(rng, [DIRICHLET_CONCENTRATION * p + 1e-3 for p in probs])
        counts = [0, 0, 0, 0]
        for _ in range(ADS_PER_RUN):
            u, acc = rng.random(), 0.0
            for k, p in enumerate(run_p):
                acc += p
                if u < acc or k == 3:
                    counts[k] += 1
                    break
        attempts = {p: max(1, base + rng.randint(-2, 2)) for p, base in BASE_ATTEMPTS.items()}
        failures = {p: _binomial(rng, attempts[p], fr.get(p, 0.01)) for p in attempts}
        sem = rng.randint(1, 5)
        out.append(RunRecord(
            index=i, verdicts=tuple(counts), latency_ms=BASE_LATENCY_MS * lat_f * rng.lognormvariate(0.0, 0.35),  # type: ignore[arg-type]
            cost_usd=BASE_COST_USD * cost_f * rng.lognormvariate(0.0, 0.25), provider_attempts=attempts, provider_failures=failures,
            semantic_findings=sem, unverified_quotes=_binomial(rng, sem, unv), mode="live"))
    return out


@dataclass
class ScenarioScore:
    scenario: str
    title: str
    n_streams: int
    n_runs: int
    change_at: int
    expected: tuple[str, ...]
    evaluations_per_stream: int = 0
    detected_streams: int = 0
    delays: list[int] = field(default_factory=list)           # 탐지까지 걸린 '변화 후 실행 수' (탐지한 스트림만)
    false_alarm_streams: dict[str, int] = field(default_factory=dict)       # 규칙 → 변화 전(또는 정상 스트림 전체)에 한 번이라도 울린 스트림 수
    false_alarm_episodes: dict[str, int] = field(default_factory=dict)      # 규칙 → 연속된 울림을 한 번으로 센 횟수
    collateral_streams: dict[str, int] = field(default_factory=dict)        # 변화 후 기대하지 않은 규칙이 울린 스트림 수

    @property
    def detection_rate(self) -> Optional[float]:
        return self.detected_streams / self.n_streams if self.expected and self.n_streams else None

    def delay_quantile(self, q: float) -> Optional[float]:
        if not self.delays:
            return None
        v = sorted(self.delays)
        pos = q * (len(v) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(v) - 1)
        return v[lo] * (1 - (pos - lo)) + v[hi] * (pos - lo)

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario, "title": self.title, "n_streams": self.n_streams, "n_runs": self.n_runs, "change_at": self.change_at, "expected_rules": list(self.expected),
            "evaluations_per_stream": self.evaluations_per_stream, "detected_streams": self.detected_streams, "detection_rate": self.detection_rate,
            "delay_runs_median": self.delay_quantile(0.5), "delay_runs_p90": self.delay_quantile(0.9), "delay_runs_max": max(self.delays) if self.delays else None,
            "false_alarm_streams": dict(self.false_alarm_streams), "false_alarm_episodes": dict(self.false_alarm_episodes),
            "false_alarm_by_rule": {r: {"streams_with_alert": self.false_alarm_streams.get(r, 0), "n_streams": self.n_streams, "episodes": self.false_alarm_episodes.get(r, 0),
                                        "stream_rate": (self.false_alarm_streams.get(r, 0) / self.n_streams) if self.n_streams else None,
                                        "wilson95": list(wilson(self.false_alarm_streams.get(r, 0), self.n_streams))} for r in RULE_SPECS},
            "collateral_streams": dict(self.collateral_streams),
        }


@dataclass
class SimulationScore:
    seed: int
    n_streams: int
    n_runs: int
    change_at: int
    baseline_runs: int
    eval_every: int
    config: MonitorConfig
    scenarios: dict[str, ScenarioScore]

    def to_dict(self) -> dict:
        return {
            "kind": "monitor_simulation",
            "note": "합성 스트림(이 프로젝트가 정한 예시 분포·변화 크기)에서 잰 값이다. 실제 트래픽에서 잰 값이 아니며 실제 탐지 성능을 말하지 않는다.",
            "seed": self.seed, "n_streams": self.n_streams, "n_runs": self.n_runs, "change_at": self.change_at, "baseline_runs": self.baseline_runs, "eval_every": self.eval_every,
            "base": {"verdict_probs": list(BASE_VERDICT_PROBS), "failure_rate": dict(BASE_FAILURE_RATE), "latency_ms_median": BASE_LATENCY_MS, "cost_usd_median": BASE_COST_USD,
                     "unverified_rate": BASE_UNVERIFIED_RATE, "ads_per_run": ADS_PER_RUN, "dirichlet_concentration": DIRICHLET_CONCENTRATION},
            "scenarios": {k: v.to_dict() for k, v in self.scenarios.items()},
        }


def _score_stream(records: Sequence[RunRecord], sc: Scenario, baseline_runs: int, change_at: int, eval_every: int, cfg: MonitorConfig,
                  score: ScenarioScore) -> None:
    baseline = records[:baseline_runs]
    fired_before: dict[str, bool] = {}
    open_episode: dict[str, bool] = {}
    first_detect: Optional[int] = None
    collateral: set[str] = set()
    for i in range(baseline_runs, len(records), eval_every):
        history = records[baseline_runs: i + 1]
        changed = (i >= change_at) and bool(sc.expected)
        for res in evaluate(history, baseline, cfg):
            if not changed:
                if res.fired:
                    fired_before[res.rule] = True
                    if not open_episode.get(res.rule):
                        score.false_alarm_episodes[res.rule] = score.false_alarm_episodes.get(res.rule, 0) + 1
                    open_episode[res.rule] = True
                else:
                    open_episode[res.rule] = False
            else:
                if res.rule in sc.expected and res.fired and first_detect is None:
                    first_detect = i - change_at + 1
                elif res.rule not in sc.expected and res.fired:
                    collateral.add(res.rule)
    for rule in fired_before:
        score.false_alarm_streams[rule] = score.false_alarm_streams.get(rule, 0) + 1
    for rule in collateral:
        score.collateral_streams[rule] = score.collateral_streams.get(rule, 0) + 1
    if first_detect is not None:
        score.detected_streams += 1
        score.delays.append(first_detect)


def simulate_and_score(n_streams: int = 100, n_runs: int = 300, change_at: int = 180, baseline_runs: int = 100, seed: int = 0, eval_every: int = 1,
                       config: Optional[MonitorConfig] = None, scenarios: Optional[Iterable[str]] = None) -> SimulationScore:
    """시나리오마다 합성 스트림 n_streams 개를 만들어 규칙을 돌리고 탐지 지연·오경보를 센다.

    - 앞 baseline_runs 건은 기준 구간이고, 그 뒤부터 매 eval_every 건마다 규칙을 평가한다. 변화는 change_at 번째 실행부터다.
    - 탐지 지연 = 변화 후 실행이 몇 건 쌓였을 때 기대한 규칙이 처음 울렸는가. 끝까지 안 울리면 '놓침' 이다.
    - 오경보 = 변화가 없는 구간(정상 시나리오 전체, 다른 시나리오의 변화 전)에서 울린 것. 연속된 울림은 한 번(에피소드)으로 센다.
    - 같은 seed 면 결과가 같다 (스트림마다 seed·시나리오 번호·스트림 번호로 파생한 난수열을 쓴다).
    """
    if n_streams < 1 or baseline_runs < 10 or change_at <= baseline_runs or n_runs <= change_at or eval_every < 1:
        raise ValueError("n_streams ≥ 1, baseline_runs ≥ 10, baseline_runs < change_at < n_runs, eval_every ≥ 1 이어야 합니다")
    cfg = config or MonitorConfig()
    names = list(scenarios) if scenarios is not None else list(SCENARIOS)
    out: dict[str, ScenarioScore] = {}
    for si, name in enumerate(names):
        sc = SCENARIOS[name]
        score = ScenarioScore(sc.name, sc.title, n_streams, n_runs, change_at, sc.expected,
                              evaluations_per_stream=len(range(baseline_runs, n_runs, eval_every)))
        for k in range(n_streams):
            recs = generate_stream(sc, n_runs, change_at, seed=seed * 1_000_003 + si * 10_007 + k)
            _score_stream(recs, sc, baseline_runs, change_at, eval_every, cfg, score)
        out[name] = score
    return SimulationScore(seed, n_streams, n_runs, change_at, baseline_runs, eval_every, cfg, out)


def format_score(score: SimulationScore) -> str:
    """사람이 읽는 표 (docs 에 붙이거나 터미널에서 본다)."""
    lines = [f"seed={score.seed} 스트림 {score.n_streams}개 × 실행 {score.n_runs}건 (기준 구간 {score.baseline_runs}건, 변화 시작 {score.change_at}번째, 평가 간격 {score.eval_every})",
             "시나리오 | 기대 규칙 | 탐지율 | 지연 중앙값/p90/최대 (변화 후 실행 수) | 기대 밖 규칙이 울린 스트림"]
    for sc in score.scenarios.values():
        d = sc.to_dict()
        if sc.expected:
            delay = "-" if d["delay_runs_median"] is None else f"{d['delay_runs_median']:.0f}/{d['delay_runs_p90']:.0f}/{d['delay_runs_max']}"
            lines.append(f"{sc.scenario} | {','.join(sc.expected)} | {sc.detected_streams}/{sc.n_streams} ({d['detection_rate']:.0%}) | {delay} | {d['collateral_streams'] or '-'}")
    normal = score.scenarios.get("normal")
    if normal:
        d = normal.to_dict()["false_alarm_by_rule"]
        lines.append(f"정상 스트림 오경보 (스트림 {normal.n_streams}개 × 평가 {normal.evaluations_per_stream}회) — 점검 | 한 번이라도 울린 스트림 | 95% 구간 | 에피소드 수")
        for r, v in d.items():
            lines.append(f"  {r} | {v['streams_with_alert']}/{v['n_streams']} | {v['wilson95'][0]:.1%}~{v['wilson95'][1]:.1%} | {v['episodes']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="모니터 규칙의 탐지 지연·오경보를 합성 스트림으로 잰다 (실제 트래픽 아님)")
    ap.add_argument("--streams", type=int, default=100)
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--change-at", type=int, default=180)
    ap.add_argument("--baseline", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--eval-every", type=int, default=1)
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    a = ap.parse_args(argv)
    sc = simulate_and_score(a.streams, a.runs, a.change_at, a.baseline, a.seed, a.eval_every)
    print(json.dumps(sc.to_dict(), ensure_ascii=False, indent=2) if a.json else format_score(sc))
    return 0


if __name__ == "__main__":   # python -m ops.monitors
    raise SystemExit(main())
