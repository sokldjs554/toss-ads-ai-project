"""온라인 모니터링 규칙: 통계 도구의 정확도, 임계값 경계(PSI), 최소 표본 보류, 규칙별 동작, 시뮬레이션 점수.

시뮬레이션 테스트가 확인하는 것은 '규칙이 합성 변화를 잡고 결과가 결정론적이다' 이지 '실제 서비스에서 이만큼 잡는다' 가 아니다.
정상 스트림에서 오경보 0 은 요구하지 않는다 — 오경보율을 보고하는 구조가 있는지만 본다.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from ops import monitors
from ops.monitors import (
    MonitorConfig,
    RULE_SPECS,
    RunRecord,
    SCENARIOS,
    burn_rate,
    chi2_homogeneity,
    chi2_sf,
    evaluate,
    generate_stream,
    psi,
    robust_center_scale,
    simulate_and_score,
)

CFG = MonitorConfig()
BASE_PROBS = (0.4, 0.3, 0.2, 0.1)


def rec(i=0, verdicts=(4, 3, 2, 1), latency=30_000.0, cost=0.12, att=None, fail=None, sem=0, unv=0) -> RunRecord:
    return RunRecord(index=i, verdicts=verdicts, latency_ms=latency, cost_usd=cost, provider_attempts=att or {}, provider_failures=fail or {}, semantic_findings=sem, unverified_quotes=unv)


def counts_to_records(counts: list[int], per_run: int = 10) -> list[RunRecord]:
    """판정 건수 합이 counts 가 되도록 실행 기록 여러 개로 나눈다 (한 실행에 per_run 건 이하)."""
    left = list(counts)
    out = []
    while sum(left):
        v = [0, 0, 0, 0]
        room = per_run
        for k in range(4):
            take = min(left[k], room)
            v[k], left[k], room = take, left[k] - take, room - take
        out.append(rec(len(out), verdicts=tuple(v)))
    return out


def status(results, rule):
    return next(r for r in results if r.rule == rule)


# --- 통계 도구 ---------------------------------------------------------------------------
def test_psi_is_zero_for_identical_and_matches_hand_calculation():
    assert psi([10, 20, 30, 40], [10, 20, 30, 40]) == pytest.approx(0.0, abs=1e-12)
    w, b = [30, 70], [50, 50]
    nw, nb = 100 + 1.0, 100 + 1.0                      # 칸마다 0.5 가상 건수
    pw, pb = [30.5 / nw, 70.5 / nw], [50.5 / nb, 50.5 / nb]
    expected = sum((x - y) * math.log(x / y) for x, y in zip(pw, pb))
    assert psi(w, b) == pytest.approx(expected)
    assert psi(w, b) == pytest.approx(psi(b, w), rel=0.05)         # 거의 대칭
    assert psi([0, 10, 0, 0], [5, 5, 0, 0]) > 0 and math.isfinite(psi([0, 10, 0, 0], [5, 5, 0, 0]))   # 빈 칸도 유한
    with pytest.raises(ValueError):
        psi([1, 2], [1, 2, 3])


@pytest.mark.parametrize("x,df,p", [(3.841458820694124, 1, 0.05), (7.814727903251179, 3, 0.05), (11.344866730144373, 3, 0.01), (5.991464547107979, 2, 0.05), (18.307038053275146, 10, 0.05)])
def test_chi2_sf_matches_table_values(x, df, p):
    assert chi2_sf(x, df) == pytest.approx(p, rel=1e-6)


def test_chi2_sf_edges():
    assert chi2_sf(0, 3) == 1.0 and chi2_sf(-5, 3) == 1.0 and chi2_sf(1000, 3) < 1e-100
    with pytest.raises(ValueError):
        chi2_sf(1, 0)


def test_chi2_homogeneity_known_table():
    stat, df, p = chi2_homogeneity([10, 20], [30, 40])
    assert stat == pytest.approx(0.7937, abs=1e-3) and df == 1 and p == pytest.approx(0.373, abs=2e-3)
    assert chi2_homogeneity([10, 0, 5], [10, 0, 5])[2] == pytest.approx(1.0) and chi2_homogeneity([10, 0, 5], [10, 0, 5])[1] == 1       # 양쪽 0 인 칸은 뺀다
    assert chi2_homogeneity([0, 0], [5, 5]) == (0.0, 0, 1.0)


def test_robust_center_scale_resists_outliers_and_floors_the_scale():
    med, scale = robust_center_scale([1, 2, 3, 4, 1000])
    assert med == 3 and scale < 5
    med, scale = robust_center_scale([5.0] * 10)
    assert med == 5.0 and scale == pytest.approx(0.5)                # MAD=0 → 중앙값의 10%


def test_burn_rate_definition():
    assert burn_rate(1, 20, 0.95) == pytest.approx(1.0)
    assert burn_rate(6, 20, 0.95) == pytest.approx(6.0)
    assert burn_rate(0, 10, 0.99) == 0.0
    with pytest.raises(ValueError):
        burn_rate(1, 0, 0.95)
    with pytest.raises(ValueError):
        burn_rate(1, 10, 1.0)


# --- PSI 경계 ------------------------------------------------------------------------------
def _shifted(t: float, n: int) -> list[int]:
    p = (BASE_PROBS[0] - t, BASE_PROBS[1], BASE_PROBS[2] + t, BASE_PROBS[3])
    return [round(x * n) for x in p]


def _t_for_psi(target: float, n: int = 200_000) -> float:
    base = [round(x * n) for x in BASE_PROBS]
    lo, hi = 0.0, 0.39
    for _ in range(60):
        mid = (lo + hi) / 2
        if psi(_shifted(mid, n), base) < target:
            lo = mid
        else:
            hi = mid
    return hi


@pytest.mark.parametrize("target,below,above", [(0.10, "ok", "warn"), (0.25, "warn", "critical")])
def test_psi_boundaries_switch_status_just_below_and_above(target, below, above):
    n = 20_000                                            # 충분히 커서 카이제곱이 늘 유의 → PSI 경계만 본다
    base = counts_to_records([round(x * n) for x in BASE_PROBS], per_run=50)
    t = _t_for_psi(target, n)
    cfg = MonitorConfig(window_runs=100_000, min_ads_window=100, min_ads_baseline=100)
    lo_recs = counts_to_records(_shifted(t * 0.97, n), per_run=50)
    hi_recs = counts_to_records(_shifted(t * 1.03, n), per_run=50)
    r_lo, r_hi = status(evaluate(lo_recs, base, cfg), "verdict_drift"), status(evaluate(hi_recs, base, cfg), "verdict_drift")
    assert r_lo.detail["psi"] < target < r_hi.detail["psi"]
    assert r_lo.status == below and r_hi.status == above, (r_lo.detail["psi"], r_hi.detail["psi"])
    assert r_lo.detail["p"] < cfg.chi2_alpha and r_hi.detail["p"] < cfg.chi2_alpha


def test_psi_threshold_is_inclusive_at_equality():
    n = 20_000
    base = counts_to_records([round(x * n) for x in BASE_PROBS], per_run=50)
    recs = counts_to_records(_shifted(0.1, n), per_run=50)
    v = psi([sum(r.verdicts[k] for r in recs) for k in range(4)], [sum(r.verdicts[k] for r in base) for k in range(4)])
    cfg = MonitorConfig(window_runs=100_000, psi_warn=v, psi_alert=v + 1.0)
    assert status(evaluate(recs, base, cfg), "verdict_drift").status == "warn"                     # PSI == 경고 임계 → 울린다 (>=)
    cfg2 = MonitorConfig(window_runs=100_000, psi_warn=v + 1e-9, psi_alert=v + 1.0)
    assert status(evaluate(recs, base, cfg2), "verdict_drift").status == "ok"


def test_psi_critical_threshold_is_inclusive_at_equality():
    n = 20_000
    base = counts_to_records([round(x * n) for x in BASE_PROBS], per_run=50)
    recs = counts_to_records(_shifted(0.2, n), per_run=50)
    v = psi([sum(r.verdicts[k] for r in recs) for k in range(4)], [sum(r.verdicts[k] for r in base) for k in range(4)])
    assert v > 0.1
    at_eq = MonitorConfig(window_runs=100_000, psi_warn=v / 2, psi_alert=v)
    assert status(evaluate(recs, base, at_eq), "verdict_drift").status == "critical"               # PSI == 심각 임계 → 심각 (>=)
    above = MonitorConfig(window_runs=100_000, psi_warn=v / 2, psi_alert=v + 1e-9)
    assert status(evaluate(recs, base, above), "verdict_drift").status == "warn"


def test_psi_alone_does_not_alert_when_the_difference_is_plausibly_chance():
    base = counts_to_records([80, 60, 40, 20], per_run=10)                           # 기준 200건
    cfg = MonitorConfig(window_runs=1000)
    small_shift = status(evaluate(counts_to_records([25, 30, 35, 10], per_run=10), base, cfg), "verdict_drift")     # 100건: PSI 0.15 지만 p≈0.016
    assert small_shift.detail["psi"] >= cfg.psi_warn and small_shift.detail["p"] >= cfg.chi2_alpha
    assert small_shift.status == "ok" and "우연" in small_shift.message                 # 크기만 보고 울리지 않는다 (카이제곱으로 거름)
    big_shift = status(evaluate(counts_to_records([20, 30, 40, 10], per_run=10), base, cfg), "verdict_drift")      # PSI 0.27, p≈0.0004
    assert big_shift.detail["psi"] >= cfg.psi_alert and big_shift.detail["p"] < cfg.chi2_alpha and big_shift.status == "critical"
    assert status(evaluate(counts_to_records([6, 2, 1, 1], per_run=10), base, cfg), "verdict_drift").status == "insufficient"   # 10건은 보류


# --- 최소 표본: 보류 ------------------------------------------------------------------------------
def test_every_rule_holds_judgement_when_samples_are_too_small_even_for_extreme_values():
    extreme = [rec(i, verdicts=(0, 0, 0, 3), latency=1e9, cost=100.0, att={"openai": 5}, fail={"openai": 5}, sem=2, unv=2) for i in range(4)]
    base = [rec(i, att={"openai": 8}, fail={"openai": 0}, sem=3, unv=0) for i in range(5)]
    for r in evaluate(extreme, base, CFG):
        assert r.status == "insufficient", r
        assert not r.fired and "판단 보류" in r.message and r.runbook == RULE_SPECS[r.rule].runbook


def test_min_sample_boundary_is_exact_for_human_review_surge():
    base = counts_to_records([180, 0, 0, 20], per_run=10)              # 기준 200건, 확인율 10%
    for n_w, expect in ((CFG.min_ads_window - 1, "insufficient"), (CFG.min_ads_window, "warn")):
        win = counts_to_records([0, 0, 0, n_w], per_run=10)              # 전부 담당자 확인
        assert status(evaluate(win, base, MonitorConfig(window_runs=10_000)), "human_review_surge").status == expect, n_w
    short_base = counts_to_records([180, 0, 0, 19], per_run=10)         # 기준 199건 → 보류
    assert status(evaluate(counts_to_records([0, 0, 0, 100]), short_base, MonitorConfig(window_runs=10_000)), "human_review_surge").status == "insufficient"


def test_other_rules_min_sample_boundaries():
    base_c = [rec(i, cost=0.1 + 0.001 * (i % 5)) for i in range(CFG.min_runs_baseline)]
    hi = lambda n: [rec(i, cost=0.5) for i in range(n)]                    # noqa: E731
    assert status(evaluate(hi(CFG.min_runs_window - 1), base_c, CFG), "cost_anomaly").status == "insufficient"
    assert status(evaluate(hi(CFG.min_runs_window), base_c, CFG), "cost_anomaly").status == "warn"
    assert status(evaluate(hi(CFG.min_runs_window), base_c[:-1], CFG), "cost_anomaly").status == "insufficient"
    slow = lambda n: [rec(i, latency=CFG.latency_slo_ms * 2) for i in range(n)]       # noqa: E731
    assert status(evaluate(slow(CFG.min_runs_burn_long - 1), [], CFG), "latency_slo_burn").status == "insufficient"
    assert status(evaluate(slow(CFG.min_runs_burn_long), [], CFG), "latency_slo_burn").status == "critical"
    pf = lambda n: [rec(i, att={"openai": 1}, fail={"openai": 1}) for i in range(n)]    # noqa: E731
    pb = [rec(i, att={"openai": 1}, fail={"openai": 0}) for i in range(CFG.min_attempts_baseline)]
    assert status(evaluate(pf(CFG.min_attempts_window - 1), pb, CFG), "provider_failure_surge").status == "insufficient"
    assert status(evaluate(pf(CFG.min_attempts_window), pb, CFG), "provider_failure_surge").status == "critical"
    cb = [rec(i, sem=1, unv=0) for i in range(CFG.min_findings_baseline)]
    cw = lambda n: [rec(i, sem=1, unv=1) for i in range(n)]                         # noqa: E731
    assert status(evaluate(cw(CFG.min_findings_window - 1), cb, CFG), "citation_surge").status == "insufficient"
    assert status(evaluate(cw(CFG.min_findings_window), cb, CFG), "citation_surge").status == "warn"


# --- 규칙별 동작 ---------------------------------------------------------------------------------
def test_human_review_surge_needs_both_ratio_and_confident_lower_bound():
    base = counts_to_records([180, 0, 0, 20], per_run=10)                        # 기준 10%
    cfg = MonitorConfig(window_runs=10_000)
    r25 = status(evaluate(counts_to_records([75, 0, 0, 25], per_run=10), base, cfg), "human_review_surge")
    assert r25.status == "ok" and r25.value == pytest.approx(0.25)                    # 25% 지만 100건이라 95% 하한(17.6%)이 임계 20% 에 못 미친다
    r30 = status(evaluate(counts_to_records([70, 0, 0, 30], per_run=10), base, cfg), "human_review_surge")
    assert r30.status == "warn" and r30.threshold == pytest.approx(0.2)               # 30/100 은 하한 21.9% > 20%
    assert status(evaluate(counts_to_records([880, 0, 0, 120], per_run=10), base, cfg), "human_review_surge").status == "ok"     # 12% 는 기준 10% 의 2배가 아니다
    big_up = status(evaluate(counts_to_records([700, 0, 0, 300], per_run=10), base, cfg), "human_review_surge")
    assert big_up.status == "warn" and big_up.value == pytest.approx(0.3)


def test_human_review_surge_does_not_fire_on_small_absolute_change_from_zero_baseline():
    base = counts_to_records([200, 0, 0, 0], per_run=10)
    r = status(evaluate(counts_to_records([95, 0, 0, 5], per_run=10), base, MonitorConfig(window_runs=10_000)), "human_review_surge")
    assert r.status == "ok"                                     # 기준 0% → 임계는 +5%p. 5/100 의 95% 하한은 그보다 낮다


def test_cost_anomaly_detects_a_shift_and_ignores_practically_small_ones():
    base = [rec(i, cost=0.12 * (1 + 0.1 * math.sin(i))) for i in range(60)]
    assert status(evaluate([rec(i, cost=0.36) for i in range(30)], base, CFG), "cost_anomaly").status == "warn"          # 3배
    assert status(evaluate([rec(i, cost=0.12) for i in range(30)], base, CFG), "cost_anomaly").status == "ok"
    tight = [rec(i, cost=0.1 + 1e-6 * (i % 3)) for i in range(60)]                     # 분산이 거의 없는 기준 → 척도 하한(10%) 적용
    r = status(evaluate([rec(i, cost=0.12) for i in range(50)], tight, CFG), "cost_anomaly")
    assert r.status == "ok" and r.detail["ratio"] == pytest.approx(1.2, rel=1e-3) and r.value >= CFG.robust_z      # z 는 크지만 1.25배 미만이라 실무상 의미 없음
    assert status(evaluate([rec(i, cost=0.14) for i in range(50)], tight, CFG), "cost_anomaly").status == "warn"


def test_cost_anomaly_holds_when_baseline_cost_is_zero_like_demo_mode():
    r = status(evaluate([rec(i, cost=1.0) for i in range(30)], [rec(i, cost=0.0) for i in range(40)], CFG), "cost_anomaly")
    assert r.status == "insufficient" and "0" in r.message


def test_latency_burn_needs_both_windows_and_grades_by_burn_rate():
    good = [rec(i, latency=10_000) for i in range(100)]
    slo = CFG.latency_slo_ms
    # 긴 창은 멀쩡한데 짧은 창만 나쁨 → 울리지 않는다 (일시적 튐)
    spike = good[:96] + [rec(i, latency=slo * 2) for i in range(4)]            # 짧은 창은 4/12(번 레이트 6.7), 긴 창은 4/60(1.3)
    assert status(evaluate(spike, [], CFG), "latency_slo_burn").status == "ok"
    # 둘 다 30% 이상 나쁨 → 번 레이트 6배 이상 → 심각
    sustained = [rec(i, latency=slo * 2 if i % 3 == 0 else 10_000) for i in range(100)]
    r = status(evaluate(sustained, [], CFG), "latency_slo_burn")
    assert r.status == "critical" and r.detail["burn_long"] >= 6 and r.detail["burn_short"] >= 6
    # 10~30% → 주의
    mild = [rec(i, latency=slo * 2 if i % 6 == 0 else 10_000) for i in range(100)]
    assert status(evaluate(mild, [], CFG), "latency_slo_burn").status == "warn"
    assert status(evaluate(good, [], CFG), "latency_slo_burn").status == "ok"
    # SLO 경계: 정확히 SLO 와 같은 지연은 '좋은' 실행
    assert status(evaluate([rec(i, latency=slo) for i in range(100)], [], CFG), "latency_slo_burn").status == "ok"


def test_slo_boundary_is_exclusive_in_both_burn_windows():
    slo = CFG.latency_slo_ms
    bad, edge = slo * 2, slo
    # 긴 창: 앞 48건은 정확히 SLO(좋은 실행), 뒤 12건만 SLO 초과 → 긴 창 번 레이트는 12/60 → 4.0 (>= 로 세면 20.0)
    r = status(evaluate([rec(i, latency=edge) for i in range(48)] + [rec(i, latency=bad) for i in range(12)], [], CFG), "latency_slo_burn")
    assert r.detail["bad_long"] == 12 and r.detail["burn_long"] == pytest.approx(4.0) and r.status == "warn"
    # 짧은 창: 앞 48건이 SLO 초과, 마지막 12건은 정확히 SLO → 짧은 창은 정상(>= 로 세면 심각)
    r2 = status(evaluate([rec(i, latency=bad) for i in range(48)] + [rec(i, latency=edge) for i in range(12)], [], CFG), "latency_slo_burn")
    assert r2.detail["bad_short"] == 0 and r2.status == "ok"


def test_provider_failure_surge_is_per_provider_and_ignores_providers_without_data():
    base = [rec(i, att={"openai": 8, "anthropic": 4}, fail={"openai": 0, "anthropic": 0}) for i in range(40)]
    outage = [rec(i, att={"openai": 8, "anthropic": 4}, fail={"openai": 5, "anthropic": 0}) for i in range(10)]
    r = status(evaluate(outage, base, CFG), "provider_failure_surge")
    assert r.status == "critical" and r.message.startswith("openai") and r.detail["providers"]["anthropic"] in ("ok", "insufficient")
    assert status(evaluate([rec(i, att={"openai": 8}, fail={"openai": 0}) for i in range(10)], base, CFG), "provider_failure_surge").status == "ok"
    none = status(evaluate([rec(i) for i in range(10)], [rec(i) for i in range(10)], CFG), "provider_failure_surge")
    assert none.status == "insufficient" and "대역" in none.message


def test_citation_surge_fires_only_on_a_clear_rise():
    base = [rec(i, sem=3, unv=0 if i % 25 else 3) for i in range(60)]           # 기준 약 4%
    assert status(evaluate([rec(i, sem=3, unv=3 if i % 2 == 0 else 0) for i in range(30)], base, CFG), "citation_surge").status == "warn"    # 50%
    assert status(evaluate([rec(i, sem=3, unv=0) for i in range(30)], base, CFG), "citation_surge").status == "ok"


def test_evaluate_rejects_unknown_rules_and_can_select_a_subset():
    assert [r.rule for r in evaluate([], [], CFG, rules=["cost_anomaly"])] == ["cost_anomaly"]
    with pytest.raises(ValueError):
        evaluate([], [], CFG, rules=["nope"])
    assert {r.rule for r in evaluate([], [], CFG)} == set(RULE_SPECS)


def test_rule_specs_document_thresholds_runbooks_and_assumptions():
    assert set(RULE_SPECS) == {"verdict_drift", "human_review_surge", "cost_anomaly", "latency_slo_burn", "provider_failure_surge", "citation_surge"}
    for s in RULE_SPECS.values():
        assert s.severity in ("warn", "critical") and s.min_samples and s.basis and re.fullmatch(r"§4\.1\.[1-6]", s.runbook) and s.title
    src = Path(monitors.__file__).read_text(encoding="utf-8")
    assert "가정" in src and "ASSUMED_LATENCY_SLO_MS" in src and "오경보" in src


def test_runbook_sections_exist_in_operations_doc():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    for s in RULE_SPECS.values():
        num = s.runbook.lstrip("§")
        assert re.search(rf"^#+\s*{re.escape(num)}\b", doc, re.M), f"OPERATIONS.md 에 {num} 절이 없음"


# --- RunRecord 변환 ---------------------------------------------------------------------------
def test_run_record_from_a_real_demo_run_matches_the_run():
    from agents.pipeline import run_pipeline
    from core.config import Settings
    from observability.tracer import Tracer
    from ui.presets import PRESETS, build_brief

    t = Tracer()
    run = run_pipeline(build_brief(PRESETS["malicious"]), Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True), tracer=t)
    r = RunRecord.from_run(run, t.traces[-1], index=3)
    assert r.index == 3 and r.n_ads == len(run.reports) and r.mode == "demo"
    assert r.verdicts == tuple(sum(rep.final_decision.value == d for rep in run.reports) for d in monitors.DECISION_ORDER)
    assert r.provider_attempts == {} and r.latency_ms == run.total_latency_ms        # 대역 호출은 제공자 실패율에 넣지 않는다
    recs = monitors.records_from_runs([run, run], t.traces)
    assert [x.index for x in recs] == [0, 1]
    # 세션 기록이 적으면 모든 점검이 보류
    assert all(x.status == "insufficient" for x in evaluate(recs[1:], recs[:1], CFG))


# --- 시뮬레이션 ---------------------------------------------------------------------------------
def test_generate_stream_is_deterministic_and_applies_the_change_only_after_change_at():
    a = generate_stream(SCENARIOS["cost_spike"], 200, 100, seed=7)
    b = generate_stream(SCENARIOS["cost_spike"], 200, 100, seed=7)
    assert [x.cost_usd for x in a] == [x.cost_usd for x in b] and generate_stream(SCENARIOS["cost_spike"], 200, 100, seed=8)[0].cost_usd != a[0].cost_usd
    before = sum(x.cost_usd for x in a[:100]) / 100
    after = sum(x.cost_usd for x in a[100:]) / 100
    assert 2.4 < after / before < 3.6
    normal = generate_stream(SCENARIOS["normal"], 200, 100, seed=7)
    assert 0.8 < (sum(x.cost_usd for x in normal[100:]) / sum(x.cost_usd for x in normal[:100])) < 1.25
    out = generate_stream(SCENARIOS["provider_outage"], 300, 150, seed=1)
    assert sum(x.provider_failures["openai"] for x in out[150:]) > 10 * sum(x.provider_failures["openai"] for x in out[:150]) + 1
    assert all(x.n_ads == monitors.ADS_PER_RUN for x in a)


@pytest.fixture(scope="module")
def small_score():
    return simulate_and_score(n_streams=6, n_runs=260, change_at=170, baseline_runs=100, seed=11, eval_every=3)


def test_simulation_is_reproducible(small_score):
    again = simulate_and_score(n_streams=6, n_runs=260, change_at=170, baseline_runs=100, seed=11, eval_every=3)
    assert again.to_dict() == small_score.to_dict()
    other = simulate_and_score(n_streams=6, n_runs=260, change_at=170, baseline_runs=100, seed=12, eval_every=3, scenarios=["cost_spike"])
    assert other.to_dict()["scenarios"]["cost_spike"]["delay_runs_median"] is not None


def test_simulation_detects_clear_faults_with_a_bounded_delay(small_score):
    sc = small_score.scenarios
    for name in ("provider_outage", "cost_spike"):
        d = sc[name].to_dict()
        assert d["detection_rate"] == 1.0, name
        assert d["delay_runs_median"] <= 40 and d["delay_runs_max"] <= 60, (name, d["delay_runs_median"])
    assert sc["drift_large"].to_dict()["detection_rate"] >= 0.8
    assert set(sc["provider_outage"].collateral_streams) <= {"latency_slo_burn", "verdict_drift", "cost_anomaly"}      # 장애는 지연도 같이 울릴 수 있다


def test_simulation_reports_false_alarm_rate_instead_of_demanding_zero(small_score):
    normal = small_score.scenarios["normal"].to_dict()
    assert normal["expected_rules"] == [] and normal["detection_rate"] is None
    by_rule = normal["false_alarm_by_rule"]
    assert set(by_rule) == set(RULE_SPECS)
    for r in by_rule.values():
        assert 0 <= r["streams_with_alert"] <= r["n_streams"] and r["wilson95"][1] >= r["wilson95"][0]
        assert r["wilson95"][1] > 0                           # 0건 관측이어도 95% 상한은 0 이 아니다
    assert sum(r["streams_with_alert"] for r in by_rule.values()) <= 6 * 2         # 상식적 상한 (합성 정상 스트림에서 대부분 조용)
    assert "합성" in small_score.to_dict()["note"] and "실제" in small_score.to_dict()["note"]


def test_format_score_is_a_readable_table(small_score):
    text = monitors.format_score(small_score)
    assert "provider_outage" in text and "정상 스트림 오경보" in text and "verdict_drift" in text


def test_simulate_and_score_validates_arguments():
    for kw in ({"n_streams": 0}, {"baseline_runs": 5}, {"change_at": 50}, {"n_runs": 150}, {"eval_every": 0}):
        with pytest.raises(ValueError):
            simulate_and_score(**{"n_streams": 1, "n_runs": 260, "change_at": 170, "baseline_runs": 100, **kw})


def test_cli_prints_json(capsys):
    assert monitors.main(["--streams", "2", "--runs", "230", "--change-at", "150", "--baseline", "100", "--eval-every", "10", "--json"]) == 0
    import json

    d = json.loads(capsys.readouterr().out)
    assert d["kind"] == "monitor_simulation" and "normal" in d["scenarios"]
