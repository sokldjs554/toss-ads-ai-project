"""효과 모델: 측정값(파일에서 읽음)과 가정(사용자 입력)이 섞이지 않는지, 범위 계산이 맞는지, '실제 운영 효과 아님' 문구가 항상 붙는지, 측정 파일이 없을 때 지어내지 않는지.

산식은 손으로 계산한 값과 대조하고, 범위는 임의의 내부 점이 [비관, 낙관] 안에 들어오는지 무작위로 확인한다(꼭짓점 계산이 정확한지).
"""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import random
from pathlib import Path

import pytest

from evals.metrics import wilson
from ops import impact
from ops.impact import (
    Assumptions,
    EXAMPLE_ASSUMPTIONS,
    ImpactInputError,
    NOT_REAL_EFFECT,
    RateMeasure,
    erlang_c,
    estimate,
    load_measured,
    mean_wait_minutes,
    required_reviewers,
    reviewer_minutes_per_day,
    validate_assumptions,
)

REPO_REPORTS = Path(__file__).resolve().parents[1] / "evals" / "reports"


def sysblock(n, hr, unsafe_k, unsafe_n, bad_h, good_h, good_n, unnec, cost, lat_total, calls=None):
    return {"n": n, "scored": n, "failed": 0, "human_review": {"k": hr, "n": n}, "unsafe_pass": {"k": unsafe_k, "n": unsafe_n}, "caught": {"k": unsafe_n - unsafe_k, "n": unsafe_n},
            "bad_to_human": {"k": bad_h, "n": unsafe_n}, "good_to_human": {"k": good_h, "n": good_n}, "unnecessary_revision": {"k": unnec, "n": good_n},
            "correct_pass": {"k": good_n - good_h - unnec, "n": good_n}, "calls": calls if calls is not None else n, "attempts": n, "failed_requests": 0,
            "latency_ms_total": lat_total, "cost_usd": cost, "estimated_calls": 0, "cost_is_lower_bound": False}


def make_compare(**over) -> dict:
    """작고 계산하기 쉬운 가짜 비교 리포트. dev: 100건(문제 있는 광고 60, 문제없는 광고 40), holdout: 20건(문제 12, 정상 8)."""
    d = {
        "kind": "comparison", "run_kind": "live", "complete": True, "aborted": None, "provider": "openai", "model": "gpt-4o", "generated_at": "2026-10-18T00:00:00+00:00",
        "failures": {"n": 0, "cases": [], "by_error": {}}, "caveats": ["테스트용 한계 문장"],
        "systems": {"code_only": {"label": "코드 검사만"}, "llm_only": {"label": "검토 모델만"}, "combined": {"label": "합친 방식"}},
        "datasets": {"golden": {"split": "dev"}, "holdout": {"split": "holdout"}},
        "overall_by_split": {
            "dev": {"code_only": sysblock(100, 0, 0, 60, 0, 0, 40, 0, 0.0, 200.0, 0), "llm_only": sysblock(100, 0, 0, 60, 0, 0, 40, 20, 1.0, 100_000.0),
                    "combined": sysblock(100, 10, 0, 60, 3, 7, 40, 2, 2.0, 300_000.0, 160)},
            "holdout": {"code_only": sysblock(20, 0, 2, 12, 0, 0, 8, 0, 0.0, 40.0, 0), "llm_only": sysblock(20, 0, 1, 12, 0, 0, 8, 2, 0.2, 20_000.0),
                        "combined": sysblock(20, 2, 0, 12, 1, 1, 8, 0, 0.4, 60_000.0, 30)},
        },
        "rows": [],
    }
    d.update(over)
    return d


@pytest.fixture
def reports(tmp_path):
    def write(name: str, data: dict) -> Path:
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return tmp_path
    return tmp_path, write


A = Assumptions(daily_reviews=100, minutes_per_review=10, hourly_wage_krw=30_000, minutes_per_flagged=20, spot_check_rate=0.0, workdays_per_month=20, usd_krw=1000, spread=0.0)


# --- 실제 저장소의 측정 파일에서 읽기 ------------------------------------------------------
def test_load_measured_from_the_real_report_matches_an_independent_recount():
    raw = json.loads((REPO_REPORTS / "compare_live.json").read_text(encoding="utf-8"))
    k = sum(raw["overall_by_split"][s]["combined"]["human_review"]["k"] for s in ("dev", "holdout"))
    n = sum(raw["overall_by_split"][s]["combined"]["human_review"]["n"] for s in ("dev", "holdout"))
    m = load_measured()
    c = m.systems["combined"]
    assert m.available and m.sources == ["compare_live.json"] and (c.human_review.k, c.human_review.n) == (k, n)
    assert c.human_review.interval == wilson(k, n)
    assert m.n_cases == n and m.provider == "openai" and set(m.systems) == {"code_only", "llm_only", "combined"}
    assert c.cost_per_case_usd == pytest.approx((raw["overall_by_split"]["dev"]["combined"]["cost_usd"] + raw["overall_by_split"]["holdout"]["combined"]["cost_usd"]) / n)
    assert c.latency_p50_ms is not None and c.latency_p95_ms >= c.latency_p50_ms
    for split in ("dev", "holdout"):
        ms = load_measured(split=split)
        assert ms.systems["combined"].human_review.n == raw["overall_by_split"][split]["combined"]["human_review"]["n"]


def test_real_report_estimate_has_the_required_structure():
    est = estimate(EXAMPLE_ASSUMPTIONS, load_measured())
    assert est.computed and est.item("담당자 확인으로 넘기는 비율").kind == "측정" and est.item("하루 절감 시간").kind == "계산"
    assert est.risk["unsafe_pass"]["n"] > 0 and est.comparison["llm_only"]["n"] > 0 and est.risk["unsafe_pass"]["wilson95"][1] > 0


# --- 산식(손계산과 대조) ---------------------------------------------------------------------
def test_arithmetic_matches_hand_calculation(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    hr = 10 / 100
    est = estimate(A, m)
    base_min = 100 * 10                                      # 1000
    with_min = 100 * hr * 20                                 # 200
    saved = base_min - with_min                              # 800
    assert est.item("하루 절감 시간").base == pytest.approx(saved)
    assert est.item("하루에 사람이 직접 봐야 하는 건수(담당자 확인 건)").base == pytest.approx(10)
    assert est.item("한 달 절감 시간").base == pytest.approx(saved / 60 * 20)
    labor = saved / 60 * 20 * 30_000
    assert est.item("한 달 절감 인건비 환산(시급 가정)").base == pytest.approx(labor)
    cost_case = 2.0 / 100
    ai = 100 * 20 * cost_case * 1000
    assert est.item("한 달 AI 호출 비용").base == pytest.approx(ai)
    assert est.item("한 달 순효과(절감 환산 − AI 호출 비용)").base == pytest.approx(labor - ai)
    assert est.item("담당자 확인 없이 끝나는 비율").base == pytest.approx(90.0)


def test_spot_check_reduces_savings_by_the_documented_formula(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    a = dataclasses.replace(A, spot_check_rate=0.2)
    est = estimate(a, m)
    expect = 100 * 10 - (100 * 0.1 * 20 + 100 * 0.9 * 0.2 * 10)
    assert est.item("하루 절감 시간").base == pytest.approx(expect)
    assert est.item("하루에 사람이 보는 건수(자동 판단 건의 재확인 포함)").base == pytest.approx(100 * (0.1 + 0.9 * 0.2))


# --- 범위: 비관 ≤ 기준 ≤ 낙관, 꼭짓점 계산이 정확 -----------------------------------------------
def test_ranges_are_ordered_and_use_wilson_bounds_of_the_measured_rate(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    lo, hi = wilson(10, 100)
    a = dataclasses.replace(A, spread=0.0)                    # 가정은 고정 → 범위는 측정 신뢰구간만으로 생긴다
    est = estimate(a, m)
    saved = est.item("하루 절감 시간")
    assert saved.low == pytest.approx(100 * 10 - 100 * hi * 20) and saved.high == pytest.approx(100 * 10 - 100 * lo * 20)
    assert saved.low < saved.base < saved.high
    hrow = est.item("담당자 확인으로 넘기는 비율")
    assert (hrow.low, hrow.base, hrow.high) == pytest.approx((lo * 100, 10.0, hi * 100))


def test_assumption_ranges_widen_the_result_and_are_exact_at_the_corners(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    a = dataclasses.replace(A, spread=0.3)
    est = estimate(a, m)
    s = est.item("하루 절감 시간")
    lo, hi = wilson(10, 100)
    # 비관: 건수·검수 시간 -30%, 확인 처리 시간 +30%, 확인율 상한 / 낙관: 반대 (spot_check 는 0 이라 0)
    pess = 70 * 7 - 70 * hi * 26
    opt = 130 * 13 - 130 * lo * 14
    assert s.low == pytest.approx(pess) and s.high == pytest.approx(opt)
    wide = estimate(dataclasses.replace(A, spread=0.0), m).item("하루 절감 시간")
    assert s.low < wide.low and s.high > wide.high


def test_random_interior_points_stay_inside_the_reported_range(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="all")
    a = dataclasses.replace(A, spread=0.4, spot_check_rate=0.1, violation_share=0.3)
    est = estimate(a, m)
    c = m.systems["combined"]
    rng = random.Random(0)
    bounds = {"N": (60, 140), "mr": (6, 14), "mf": (12, 28), "s": (0.06, 0.14), "wage": (18_000, 42_000), "days": (12, 28), "fx": (600, 1400), "share": (0.18, 0.42)}
    b_lo, b_hi = c.bad_to_human.interval
    g_lo, g_hi = c.good_to_human.interval
    ci = c.cost_interval_usd or (c.cost_per_case_usd, c.cost_per_case_usd)
    for _ in range(300):
        v = {k: rng.uniform(*r) for k, r in bounds.items()}
        b, g, cost = rng.uniform(b_lo, b_hi), rng.uniform(g_lo, g_hi), rng.uniform(*ci)
        hr = v["share"] * b + (1 - v["share"]) * g
        saved = v["N"] * v["mr"] - (v["N"] * hr * v["mf"] + v["N"] * (1 - hr) * v["s"] * v["mr"])
        net = saved / 60 * v["days"] * v["wage"] - v["N"] * v["days"] * cost * v["fx"]
        row, nrow = est.item("하루 절감 시간"), est.item("한 달 순효과(절감 환산 − AI 호출 비용)")
        assert row.low - 1e-6 <= saved <= row.high + 1e-6
        assert nrow.low - 1e-3 <= net <= nrow.high + 1e-3
    # 극단값(꼭짓점)이 실제로 범위의 끝과 같다
    hr_lo = min(sh * b_lo + (1 - sh) * g_lo for sh in (0.18, 0.42))       # 문제 있는 광고 쪽 확인율이 더 낮으므로 비중이 클수록 확인율은 낮아진다
    hr_hi = max(sh * b_hi + (1 - sh) * g_hi for sh in (0.18, 0.42))
    assert est.item("담당자 확인으로 넘기는 비율").low == pytest.approx(hr_lo * 100) and est.item("담당자 확인으로 넘기는 비율").high == pytest.approx(hr_hi * 100)


def test_notices_warn_that_the_measured_rate_excludes_cross_check_and_dev_cases_are_tuned(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    est = estimate(A, load_measured(tmp, split="all"))
    joined = " ".join(est.notices)
    assert "교차 확인" in joined and "규칙·프롬프트를 고치는 데 쓴" in joined
    assert "교차 확인" not in " ".join(estimate(dataclasses.replace(A, what_if_human_review_rate=0.1), load_measured(tmp)).notices)      # 직접 가정한 값에는 해당 없음


def test_pessimistic_and_optimistic_follow_the_direction_of_each_item(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    est = estimate(dataclasses.replace(A, spread=0.3, spot_check_rate=0.05), load_measured(tmp, split="all"))
    worse_up = {"담당자 확인으로 넘기는 비율", "하루에 사람이 직접 봐야 하는 건수(담당자 확인 건)", "하루에 사람이 보는 건수(자동 판단 건의 재확인 포함)", "한 달 AI 호출 비용", "문제 있는 광고를 통과시킨 비율"}
    for i in est.items:
        assert i.higher_is_worse == (i.name in worse_up), i.name
        if i.low is None or i.high is None:
            continue
        if i.higher_is_worse:
            assert i.pessimistic == i.high and i.optimistic == i.low and i.pessimistic >= i.base >= i.optimistic - 1e-9, i.name
        else:
            assert i.pessimistic == i.low and i.optimistic == i.high and i.pessimistic <= i.base <= i.optimistic + 1e-9, i.name
    cost = est.item("한 달 AI 호출 비용")
    assert cost.pessimistic > cost.optimistic                              # 비용은 많을수록 비관
    saved = est.item("하루 절감 시간")
    assert saved.pessimistic < saved.optimistic
    text = est.to_text()
    assert f"비관 {impact._fmt(cost.pessimistic)} ~ 낙관 {impact._fmt(cost.optimistic)}" in text
    assert est.to_dict()["items"][0]["pessimistic"] == est.items[0].pessimistic


def test_negative_savings_are_reported_not_clamped(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    slow = dataclasses.replace(A, minutes_per_flagged=500.0, minutes_per_review=1.0, spread=0.0)
    est = estimate(slow, m)
    assert est.item("하루 절감 시간").base < 0 and est.item("하루 절감 시간").high < 0


# --- 측정과 가정의 분리 ----------------------------------------------------------------------
def test_measured_items_do_not_change_when_only_assumptions_change(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    e1 = estimate(A, m)
    e2 = estimate(dataclasses.replace(A, daily_reviews=999, minutes_per_review=3, hourly_wage_krw=99_999, minutes_per_flagged=40, spread=0.5), m)
    for name in ("담당자 확인으로 넘기는 비율", "문제 있는 광고를 통과시킨 비율"):
        a, b = e1.item(name), e2.item(name)
        assert a.kind == "측정" == b.kind
        assert (a.base, a.high) == (b.base, b.high) or name == "담당자 확인으로 넘기는 비율"      # 확인율 칸의 범위는 가정과 무관
    assert e1.item("담당자 확인으로 넘기는 비율").base == e2.item("담당자 확인으로 넘기는 비율").base == pytest.approx(10.0)
    assert e1.item("하루 절감 시간").base != e2.item("하루 절감 시간").base


def test_every_item_is_tagged_with_a_kind_and_measured_items_have_a_source(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    est = estimate(EXAMPLE_ASSUMPTIONS, load_measured(tmp, split="all"))
    kinds = {i.kind for i in est.items}
    assert kinds <= {"측정", "가정", "계산"} and "측정" in kinds and "계산" in kinds
    for i in est.items:
        if i.kind == "측정":
            assert i.source == "compare_live.json", i.name
    assert all(i.kind == "계산" for i in est.items if "절감" in i.name or "순효과" in i.name)


def test_what_if_rate_replaces_the_measurement_and_is_labeled_as_an_assumption(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    est = estimate(dataclasses.replace(A, what_if_human_review_rate=0.25), m)
    hr = est.item("담당자 확인으로 넘기는 비율")
    assert hr.kind == "가정" and hr.base == pytest.approx(25.0) and hr.source == "사용자 입력"
    assert est.item("하루 절감 시간").base == pytest.approx(100 * 10 - 100 * 0.25 * 20)
    assert est.item("한 달 AI 호출 비용") is None and "AI 호출 비용" in est.unavailable and not est.risk and not est.comparison   # 측정을 쓰지 않았으니 측정에서 온 항목도 없다
    assert "가정" in est.basis and not any(i.kind == "측정" for i in est.items)


def test_violation_share_reweights_measured_strata(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")                       # 문제 있는 광고 60건 중 확인 3건(5%), 정상 40건 중 7건(17.5%)
    est = estimate(dataclasses.replace(A, violation_share=0.1, spread=0.0), m)
    hr = 0.1 * (3 / 60) + 0.9 * (7 / 40)
    assert est.item("담당자 확인으로 넘기는 비율").base == pytest.approx(hr * 100)
    assert est.item("담당자 확인으로 넘기는 비율").kind == "계산" and "다시 가중" in est.basis
    assert est.item("하루 절감 시간").base == pytest.approx(100 * 10 - 100 * hr * 20)
    assert est.risk["expected_unsafe_passes_per_day_upper95"] == pytest.approx(100 * 0.1 * wilson(0, 60)[1])
    plain = estimate(dataclasses.replace(A, spread=0.0), m).item("담당자 확인으로 넘기는 비율").base
    assert plain == pytest.approx(10.0) and plain != est.item("담당자 확인으로 넘기는 비율").base


def test_unsafe_pass_upper_bound_is_not_zero_even_with_zero_observed(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    est = estimate(A, load_measured(tmp, split="dev"))
    row = est.item("문제 있는 광고를 통과시킨 비율")
    assert row.base == 0.0 and row.high == pytest.approx(wilson(0, 60)[1] * 100) and row.high > 0


def test_comparison_with_llm_only_uses_measured_rates(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    est = estimate(A, load_measured(tmp, split="dev"))
    c = est.comparison
    assert (c["llm_only"]["k"], c["llm_only"]["n"]) == (20, 40) and (c["combined"]["k"], c["combined"]["n"]) == (2, 40)
    assert c["per_1000_benign_fewer_requests"] == pytest.approx((0.5 - 0.05) * 1000) and c["intervals_overlap"] is False
    assert "신뢰구간은 계산하지 않았다" in c["note"]


# --- '실제 운영 효과 아님' 문구 ---------------------------------------------------------------
def test_the_not_real_effect_notice_is_everywhere(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    for est in (estimate(A, load_measured(tmp)), estimate(A, load_measured(tmp / "nope")), estimate(dataclasses.replace(A, what_if_human_review_rate=0.1), load_measured(tmp))):
        assert NOT_REAL_EFFECT in est.headline and est.notices[0] == NOT_REAL_EFFECT
        text = est.to_text()
        assert text.splitlines()[0] == NOT_REAL_EFFECT and text.splitlines()[-1] == NOT_REAL_EFFECT
        assert est.to_dict()["notice"] == NOT_REAL_EFFECT and NOT_REAL_EFFECT in est.to_dict()["headline"]
    assert NOT_REAL_EFFECT == "이 평가 사례와 이 가정에서 계산한 값이며 실제 운영 효과가 아닙니다."


# --- 측정 파일이 없거나 쓸 수 없을 때 ----------------------------------------------------------
def test_no_measurement_files_means_no_numbers_are_invented(tmp_path):
    m = load_measured(tmp_path)
    assert not m.available and m.systems == {} and "담당자 확인율" in m.missing
    est = estimate(A, m)
    assert not est.computed and est.items == [] and est.risk == {} and est.comparison == {}
    assert "측정 없음" in est.headline and "담당자 확인율" in est.unavailable and NOT_REAL_EFFECT in est.to_text()
    assert est.to_dict()["measured"]["available"] is False


@pytest.mark.parametrize("patch", [
    {"run_kind": "controlled"}, {"run_kind": "demo"}, {"complete": False}, {"aborted": {"done": 3}}, {"failures": {"n": 2, "cases": [], "by_error": {}}}, {"kind": "other"},
])
def test_unusable_comparison_files_are_ignored_with_a_reason(reports, patch):
    tmp, write = reports
    write("compare_live.json", make_compare(**patch))
    m = load_measured(tmp)
    assert not m.available and "compare_live.json" in m.missing and m.missing["compare_live.json"]


def test_demo_and_code_only_baselines_are_never_used_as_measurements(reports):
    tmp, write = reports
    cases = [{"id": "x", "label": "APPROVE", "predicted": "APPROVE", "latency_ms": 5.0, "cost_usd": 0.0}] * 5
    for mode in ("demo", "deterministic_only"):
        write("baseline_partial.json", {"mode": mode, "cases": cases, "provider_failures": {"n": 0}})
        assert not load_measured(tmp).available, mode
    write("baseline_partial.json", {"mode": "partial", "cases": cases, "provider_failures": {"n": 1}})
    assert not load_measured(tmp).available


def test_falls_back_to_the_real_model_baseline_when_comparison_is_missing(reports):
    tmp, write = reports
    cases = ([{"id": f"b{i}", "label": "REJECT", "predicted": "REJECT" if i else "HUMAN_REVIEW", "latency_ms": 4000.0, "cost_usd": 0.02} for i in range(10)]
             + [{"id": f"g{i}", "label": "APPROVE", "predicted": "APPROVE" if i < 8 else ("HUMAN_REVIEW" if i == 8 else "REVISE"), "latency_ms": 3000.0, "cost_usd": 0.01} for i in range(10)])
    write("baseline_partial.json", {"mode": "partial", "provider": "openai", "model": "gpt-4o", "generated_at": "2026-10-17", "cases": cases, "provider_failures": {"n": 0}})
    m = load_measured(tmp, split="all")
    c = m.systems["combined"]
    assert m.available and m.sources == ["baseline_partial.json"] and (c.human_review.k, c.human_review.n) == (2, 20)
    assert (c.bad_to_human.k, c.bad_to_human.n) == (1, 10) and (c.good_to_human.k, c.good_to_human.n) == (1, 10) and (c.unnecessary_revision.k, c.unnecessary_revision.n) == (1, 10)
    assert "검토 모델만 대비 비교" in m.missing and "compare_live.json" in m.missing and any("대신했습니다" in n for n in m.notes)
    est = estimate(A, m)
    assert est.computed and est.comparison == {} and "검토 모델만 대비 불필요 수정 요청 감소" in est.unavailable        # 비교는 측정 없음으로 표시
    assert load_measured(tmp, split="holdout").available is False                                                    # 이 파일은 개발용만


def test_missing_llm_only_system_is_marked_not_measured(reports):
    tmp, write = reports
    d = make_compare()
    for s in ("dev", "holdout"):
        del d["overall_by_split"][s]["llm_only"]
    write("compare_live.json", d)
    m = load_measured(tmp)
    assert m.available and "검토 모델만 대비 비교" in m.missing
    est = estimate(A, m)
    assert est.computed and est.comparison == {} and "검토 모델만 대비 불필요 수정 요청 감소" in est.unavailable


def test_corrupt_report_file_is_treated_as_missing(tmp_path):
    (tmp_path / "compare_live.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "baseline_partial.json").write_text("[]", encoding="utf-8")
    assert not load_measured(tmp_path).available


def test_invalid_split_is_refused():
    with pytest.raises(ValueError):
        load_measured(split="nope")


# --- 가정 검증 ------------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("daily_reviews", 0), ("daily_reviews", -5), ("minutes_per_review", 0), ("hourly_wage_krw", 0), ("minutes_per_flagged", 0), ("workdays_per_month", 0), ("usd_krw", 0),
    ("daily_reviews", None), ("daily_reviews", float("nan")), ("hourly_wage_krw", float("inf")), ("minutes_per_review", "다섯"), ("daily_reviews", True),
    ("spot_check_rate", 1.5), ("spot_check_rate", -0.1), ("violation_share", 2), ("what_if_human_review_rate", -1), ("spread", 1.0), ("spread", -0.1),
])
def test_bad_assumptions_raise_easy_errors_not_division_errors(reports, field, value):
    tmp, write = reports
    write("compare_live.json", make_compare())
    bad = dataclasses.replace(A, **{field: value})
    errs = validate_assumptions(bad)
    assert errs and all(isinstance(e, str) and e.strip() for e in errs)
    with pytest.raises(ImpactInputError) as ei:
        estimate(bad, load_measured(tmp))
    assert ei.value.messages == errs
    joined = " ".join(errs)
    assert "Traceback" not in joined and "ZeroDivision" not in joined and ("‘" in joined)


def test_empty_assumptions_ask_for_each_required_value():
    errs = validate_assumptions(Assumptions())
    assert len(errs) == 4 and all("입력하세요" in e for e in errs)


def test_invalid_ranges_are_refused():
    for rng in ({"daily_reviews": (50, 40)}, {"daily_reviews": (0, 200)}, {"daily_reviews": (120, 150)}, {"spot_check_rate": (0.0, 2.0)}, {"nope": (1, 2)}, {"daily_reviews": (1,)}):
        assert validate_assumptions(dataclasses.replace(A, spot_check_rate=0.05, ranges=rng)), rng
    ok = dataclasses.replace(A, ranges={"daily_reviews": (80, 150)})
    assert validate_assumptions(ok) == []


def test_explicit_ranges_override_the_spread(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp, split="dev")
    a = dataclasses.replace(A, spread=0.0, ranges={"daily_reviews": (50, 200)})
    s = estimate(a, m).item("하루 절감 시간")
    lo, hi = wilson(10, 100)
    assert s.low == pytest.approx(50 * 10 - 50 * hi * 20) and s.high == pytest.approx(200 * 10 - 200 * lo * 20)


def test_example_assumptions_are_labeled_as_examples_without_basis():
    assert "예시" in impact.EXAMPLE_NOTE and "근거가 없" in impact.EXAMPLE_NOTE
    assert validate_assumptions(EXAMPLE_ASSUMPTIONS) == []
    src = Path(impact.__file__).read_text(encoding="utf-8")
    assert "실제 운영 효과가 아니다" in src and "이 숫자가 의미하지 않는 것" in src


def test_estimate_does_not_mutate_inputs(reports):
    tmp, write = reports
    write("compare_live.json", make_compare())
    m = load_measured(tmp)
    snap = copy.deepcopy(m.to_dict())
    a = dataclasses.replace(A, ranges={"daily_reviews": (50, 200)})
    estimate(a, m)
    assert m.to_dict() == snap


# --- 검수 용량 공식 ----------------------------------------------------------------------------
def test_reviewer_minutes_and_fte_follow_the_documented_formula():
    assert reviewer_minutes_per_day(200, 0.1, 8, 0.05, 5) == pytest.approx(200 * (0.1 * 8 + 0.9 * 0.05 * 5))
    assert required_reviewers(200, 0.1, 8, spot_check_rate=0.05, minutes_per_review=5, hours_per_day=8, utilization=0.7) == pytest.approx(200 * (0.8 + 0.225) / (8 * 60 * 0.7))
    base = required_reviewers(100, 0.1, 10)
    assert required_reviewers(100, 0.2, 10) == pytest.approx(2 * base)                      # 스팟 체크가 없으면 확인율에 정비례
    assert required_reviewers(100, 0.1, 10, peak_factor=3) == pytest.approx(3 * base)
    for bad in ({"daily_reviews": 0}, {"minutes_per_flagged": -1}, {"human_review_rate": 1.5}):
        kw = {"daily_reviews": 100, "human_review_rate": 0.1, "minutes_per_flagged": 10, **bad}
        with pytest.raises(ImpactInputError):
            required_reviewers(**kw)
    with pytest.raises(ImpactInputError):
        required_reviewers(100, 0.1, 10, utilization=1.5)
    with pytest.raises(ImpactInputError):
        required_reviewers(100, 0.1, 10, utilization=0)


def test_erlang_c_and_waiting_time_match_known_queueing_results():
    assert erlang_c(1, 0.5) == pytest.approx(0.5)                       # M/M/1: 대기 확률 = 이용률
    assert erlang_c(2, 1.0) == pytest.approx(1 / 3)
    assert erlang_c(3, 0.0) == 0.0 and erlang_c(2, 2.0) == 1.0 and erlang_c(2, 3.0) == 1.0
    lam, mu = 0.5, 1.0                                                  # M/M/1 평균 대기 = λ / (μ(μ−λ))
    assert mean_wait_minutes(lam * 60, 1 / mu, 1) == pytest.approx(lam / (mu * (mu - lam)))
    assert mean_wait_minutes(60, 2, 1) == math.inf                      # 도착 1건/분, 처리 2분 → 과부하
    assert mean_wait_minutes(10, 8, 2) == pytest.approx(6.4, abs=0.05)
    assert mean_wait_minutes(10, 8, 3) < mean_wait_minutes(10, 8, 2)
    with pytest.raises(ValueError):
        erlang_c(0, 1)
    with pytest.raises(ImpactInputError):
        mean_wait_minutes(0, 8, 1)


def test_rate_measure_basics():
    r = RateMeasure(3, 10)
    assert r.rate == 0.3 and r.interval == wilson(3, 10) and (r + RateMeasure(1, 10)).n == 20
    assert RateMeasure(0, 0).rate is None and RateMeasure(0, 0).interval is None
