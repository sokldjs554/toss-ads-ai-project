"""2026-10-20 후속 회귀: 예측 오차·여러 비교·관측 자료의 원인 가설 표현."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core import performance as P
from evals import insights_eval as EV


@pytest.mark.parametrize("scenario_id", ["holdout-no_event-1", "holdout-seasonal_dip-1"])
def test_known_normal_variability_no_longer_creates_an_incident(scenario_id):
    # 이미 본 사례의 회귀 검사이며 새 독립 평가가 아니다. 시나리오는 그대로 사용한다.
    scenario = next(s for s in EV.load_scenarios("holdout") if s.id == scenario_id)
    analysis = ie.analyze(P.generate(scenario.spec))
    assert analysis.incidents == []
    assert [a.action_id for a in analysis.actions] == ["keep_watching"]


def test_a_report_with_more_comparisons_requires_stronger_evidence():
    # 같은 수치에 더 많은 독립 검사를 하는 조건만 바꾼다. 중간 강도 신호를 무조건 알리지 않아야 한다.
    dates = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(42)]
    counts = {d: ie.Counts(10000, 1000, 100, 100000 if i < 39 else 110000) for i, d in enumerate(dates)}
    sample = ie.SampleStatus(True, True, 28000, 2800, 28)
    ordinary = ie.detect_metric("cpc", counts, set(dates), dates[:28], dates[28:], sample, comparisons=1)
    crowded = ie.detect_metric("cpc", counts, set(dates), dates[:28], dates[28:], sample, comparisons=1000000)
    assert ordinary.spans
    assert not crowded.spans


@pytest.mark.parametrize("seed,scale", [(102, 0.002), (208, 0.008), (311, 0.012)])
def test_sparse_conversions_are_withheld_even_when_recent_values_disappear(seed, scale):
    rows = P.generate(P.ScenarioSpec("small-counts", seed, scale=scale))
    cutoff = max(r.date for r in rows) - dt.timedelta(days=13)
    rows = [dataclasses.replace(r, conversions=0) if r.date >= cutoff else r for r in rows]
    analysis = ie.analyze(rows)
    assert not analysis.sample.conv_ok
    assert all(analysis.detections[m].status == "withheld" for m in ie.CONV_METRICS)
    assert not any(m in ie.CONV_METRICS for i in analysis.incidents for m, _ in i.findings)


@pytest.mark.parametrize("text", [
    "원인은 전환 추적 오류입니다.",
    "전환 추적 오류로 전환이 줄었습니다.",
    "전환 추적 오류가 확실합니다.",
    "원인을 확정할 수 없습니다. 하지만 원인은 전환 추적 오류입니다.",
    "전환 추적 오류가 원인입니다. 확인하세요.",
    "원인은 전환 추적 오류입니다 확인하세요.",
    "원인은 전환 추적 오류입니다 (원인을 확정할 수 없습니다).",
    "전환 추적 오류가 확실합니다 (확신은 100%).",
])
def test_aggregate_observations_never_authorize_a_causal_assertion(text):
    assert ins.causal_issue(text, {"tracking_break"}, {"tracking_break": 1.0}).startswith("cause_overclaim")


def test_high_support_remains_a_hypothesis_with_a_falsifiable_next_check():
    rows = P.generate(P.ScenarioSpec("strong-signal", 21, events=(P.EventSpec("tracking_break", 31, None, {"residual": 0.03}),)))
    analysis = ie.analyze(rows)
    hyp = analysis.diagnoses[0].hyp("tracking_break")
    assert hyp.confidence >= 0.75
    assert hyp.state == analysis.diagnoses[0].cause_state == "supported"
    assert "서버" in hyp.verification and "기록" in hyp.verification
    narrative = ie.template_narrative(analysis.to_context())
    text = " ".join([narrative["summary"]] + [f["text"] for f in narrative["findings"]] + [a["why"] for a in narrative["actions"]])
    assert "가설" in text and "서버" in text
    assert "원인은 전환 추적 오류입니다" not in text
    assert not ins.causal_issue(text, {"tracking_break"}, analysis.cause_conf())


def test_large_allocation_shift_can_be_explained_even_with_a_small_aggregate_rate_change():
    rows = []
    for day in range(42):
        for device, clicks, rate in (("Android", 1000 if day < 28 else 1600, 0.05), ("iOS", 1000 if day < 28 else 400, 0.06)):
            rows.append(P.MetricRow(dt.date(2026, 1, 1) + dt.timedelta(days=day), "campaign", "set", "creative", device,
                                    clicks * 10, clicks, round(clicks * rate), clicks * 100))
    analysis = ie.analyze(rows)
    assert analysis.incidents
    assert analysis.diagnoses[0].top_cause == "mix_shift"
    assert [a.action_id for a in analysis.actions] == ["review_segment_mix"]


def test_model_actions_require_checking_the_hypothesis_before_changing_settings():
    analysis = ie.analyze(P.generate(P.ScenarioSpec("budget", 21, events=(P.EventSpec("budget_cap", 30, None, {"cap_frac": 0.65}),))))
    proposal = next(a for a in analysis.actions if a.action_id == "raise_budget")
    dropped = []
    result = ins._validate_actions([{"action_id": "raise_budget", "why": "예산을 늘리세요.", "evidence_ids": proposal.evidence_ids}],
                                   analysis, ins._ctx_from(analysis), dropped, None)
    assert result == []
    assert any(d.reason == "verification_required" for d in dropped)


def test_public_hypothesis_card_displays_support_and_a_check_instead_of_certainty(monkeypatch):
    from ui import performance_view as pv

    analysis = ie.analyze(P.generate(P.ScenarioSpec("card", 21, events=(P.EventSpec("tracking_break", 31, None, {"residual": 0.03}),))))
    captured = []
    monkeypatch.setattr(pv, "_html", captured.append)
    pv._hyp_card(analysis.diagnoses[0].hyp("tracking_break"), analysis.evidence, 1)
    text = " ".join(captured)
    assert "단정 가능" not in text
    assert "근거 일치" in text and "서버" in text and "확인할 일" in text


def test_normal_variability_across_noise_scale_trend_and_group_counts():
    # 개발용 스트레스 검사. 이 결과로 실데이터의 오류 확률을 보장하지 않는다.
    alerted = []
    for i in range(72):
        spec = P.ScenarioSpec(f"normal-stress-{i}", 20000 + i,
                              start_date=dt.date(2026, 2, 1) + dt.timedelta(days=i % 7),
                              noise=(0.7, 1.1, 1.8, 2.4)[i % 4], trend_per_day=(-0.003, 0, 0.003)[i // 4 % 3],
                              scale=(0.1, 0.5, 2.0)[i // 12 % 3], n_ad_sets=(2, 4)[i // 36],
                              creatives_per_set=3, devices=("Android", "iOS", "Web"), dow_amp=(1.0, 3.0)[i % 2])
        if ie.analyze(P.generate(spec)).incidents:
            alerted.append(i)
    assert alerted == [], alerted


@pytest.mark.parametrize("onset", [16, 19, 22])
@pytest.mark.parametrize("kind,params", [
    ("budget_cap", {"cap_frac": 0.45}),
    ("tracking_break", {"residual": 0.03}),
    ("creative_fatigue", {"depth": 0.6, "ramp_days": 5}),
    ("bid_competition", {"cpc_up": 0.5, "imp_down": 0.3}),
])
def test_persistent_change_is_detected_when_it_started_inside_the_usual_baseline(onset, kind, params):
    # 새 개발용 사례. 기준선에 사건이 섞이는 위치만 바꾸며 정답 날짜를 엔진에 전달하지 않는다.
    rows = P.generate(P.ScenarioSpec("early-change", 94000 + onset, scale=2.0, noise=0.8,
                                     events=(P.EventSpec(kind, onset, None, params),)))
    analysis = ie.analyze(rows)
    assert analysis.incidents
    assert analysis.window == (max(r.date for r in rows) - dt.timedelta(days=13), max(r.date for r in rows))
    if onset < 20:
        assert analysis.baseline[1] < analysis.window[0] - dt.timedelta(days=1)
    assert analysis.sample.base_days >= ie.MIN_BASELINE_DAYS
    narrative = ie.template_narrative(analysis.to_context())
    text = " ".join(f["text"] for f in narrative["findings"])
    if analysis.reference_evidence:
        assert "이전 기준 기간" in text and "시작일" in text and "확정" in text
    assert ins.audit_report(ins.build_report(rows, router=None, analysis=analysis)) == []


@pytest.mark.parametrize("trend", [-0.006, -0.003, 0.003, 0.006])
def test_searching_earlier_references_does_not_turn_a_continuing_trend_into_a_step(trend):
    rows = []
    for day in range(42):
        import math
        clicks = round(10000 * math.exp(trend * day))
        rows.append(P.MetricRow(dt.date(2026, 1, 1) + dt.timedelta(days=day), "campaign", "set", "creative", "device",
                                clicks * 10, clicks, round(clicks * 0.1), clicks * 100))
    assert ie.analyze(rows).incidents == []


def test_weekday_note_does_not_call_opposite_weekend_directions_the_same_pattern():
    dates = [dt.date(2026, 1, 1) + dt.timedelta(days=d) for d in range(42)]
    rows = []
    for i, day in enumerate(dates):
        clicks = (800 if i < 28 else 1004) if day.weekday() >= 5 else 1000
        rows.append(P.MetricRow(day, "campaign", "set", "creative", "device", clicks * 10, clicks, 100, clicks * 100))
    table = ie.build_table(rows, dates[0], dates[-1])
    assert ie._weekday_note(table, dates[:28], dates[28:], ie.EvidenceBook()) == ""


def test_ambiguous_case_report_certainty_is_checked_even_with_the_new_supported_state():
    from tests.test_insights_eval import res

    row = res(ambiguous=True, main_causes=[], cause_states=["supported"],
              report={"unsupported_numbers": 0, "audit_problems": 1, "causal_overclaims": 1})
    assert EV.aggregate([row])["ambiguous_not_asserted"] == 0


def test_model_cannot_omit_the_earlier_reference_and_start_date_limitation(tracer):
    from tests.test_insights_report import LIVE, live_router

    rows = P.generate(P.ScenarioSpec("early-model", 94019, scale=2.0, noise=0.8,
                                     events=(P.EventSpec("budget_cap", 19, None, {"cap_frac": 0.45}),)))
    analysis = ie.analyze(rows)
    assert analysis.reference_evidence
    narrative = ie.template_narrative(analysis.to_context())
    narrative["findings"] = [f for f in narrative["findings"] if analysis.reference_evidence not in f["evidence_ids"]]
    router, _ = live_router(tracer, lambda req: narrative)
    report = ins.build_report(rows, router=router, settings=LIVE, analysis=analysis)
    assert "reference" in report.filled_from_template
    assert any(analysis.reference_evidence in f.evidence_ids and "실제 시작일" in f.text for f in report.findings)
    assert ins.audit_report(report) == []


def test_a_change_already_present_at_the_left_boundary_has_no_observed_start_speed():
    rows = P.generate(P.ScenarioSpec("early-shape", 94019, scale=2.0, noise=0.8,
                                     events=(P.EventSpec("creative_fatigue", 19, None, {"depth": 0.6, "ramp_days": 5}),)))
    analysis = ie.analyze(rows)
    assert analysis.reference_evidence and analysis.incidents[0].start == analysis.window[0]
    checks = analysis.diagnoses[0].hyp("creative_fatigue").checks
    assert all(c.passed is None for c in checks if c.cid in ("C3", "K2"))
    assert not any(e.kind == "shape" and "단번에" in e.text for e in analysis.evidence.values())


@pytest.mark.parametrize("constant", ["FIRST_FOLLOWUP_PATH", "FIRST_HOLDOUT_PATH"])
def test_first_followup_reports_are_protected_from_accidental_overwrite(monkeypatch, tmp_path, constant):
    report = tmp_path / "first.json"
    report.write_text("first frozen result", encoding="utf-8")
    monkeypatch.setattr(EV, constant, report)
    with pytest.raises(SystemExit) as err:
        EV.main(["--regression", "--out", str(report)])
    assert err.value.code == 2 and report.read_text(encoding="utf-8") == "first frozen result"
