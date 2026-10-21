"""Earlier-reference scans must use observed transitions without inventing persistence."""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core import performance as P
from evals import insights_eval as EV


def localized_drop_spec() -> P.ScenarioSpec:
    # Previously seen miss: regression evidence only, never a new blind holdout.
    return next(s.spec for s in EV.load_scenarios(path=EV.DATASETS / "perf_holdout_20261020.jsonl") if s.id == "h26-p059")


def test_observed_transition_recovers_a_local_change_hidden_in_the_regular_baseline():
    rows = P.generate(localized_drop_spec())
    analysis = ie.analyze(rows)
    assert analysis.incidents
    assert any(m == "cvr" and s.segment == "광고세트 세트B_30대" for i in analysis.incidents for m, s in i.findings)
    assert analysis.reference_evidence
    assert all(analysis.window[0] <= i.start <= i.end <= analysis.window[1] for i in analysis.incidents)
    assert analysis.sample.base_days >= ie.MIN_BASELINE_DAYS
    assert ins.audit_report(ins.build_report(rows, router=None, analysis=analysis)) == []


def test_removing_transition_observations_keeps_the_uncertain_forecast_withheld():
    spec = localized_drop_spec()
    rows = [r for r in P.generate(spec) if (r.date - spec.start_date).days not in range(20, 28)]
    analysis = ie.analyze(rows)
    assert analysis.incidents == []
    assert [a.action_id for a in analysis.actions] == ["keep_watching"]


def test_a_longer_scan_charges_more_comparisons_than_the_recent_window_alone():
    analysis = ie.analyze(P.generate(localized_drop_spec()))
    table = ie.build_table(P.generate(localized_drop_spec()), analysis.baseline[0], analysis.window[1])
    series = len(ie.METRICS) + sum(len(metrics) * len([v for v in table.dims[dim] if v != P.NO_DIM])
                                 for dim, metrics in ie.SEGMENT_METRICS.items() if table.has_dim(dim))
    previous_budget = 2 * (analysis.reference_windows - 1) * series
    assert analysis.detections["cvr"].comparisons > previous_budget
    assert analysis.detections["cvr"].score_threshold > math.sqrt(ie.H_SCORE ** 2 + 2 * math.log(previous_budget))


def test_a_zero_estimated_trend_does_not_remove_uncertainty_from_an_extended_scan():
    dates = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(42)]
    counts = {d: ie.Counts(10000, 1000, 100, 100000 if i < 20 else 112000) for i, d in enumerate(dates)}
    sample = ie.SampleStatus(True, True, 14000, 1400, 14)
    assume_fixed_slope = ie.detect_metric("cpc", counts, set(dates), dates[:14], dates[14:], sample, report_start=dates[28])
    uncertain_slope = ie.detect_metric("cpc", counts, set(dates), dates[:14], dates[14:], sample, report_start=dates[28],
                                      include_slope_uncertainty=True)
    assert assume_fixed_slope.trend == uncertain_slope.trend == 0
    assert assume_fixed_slope.spans
    assert uncertain_slope.spans == []


@pytest.mark.parametrize("end", [23, 25, 27])
def test_a_local_change_that_recovered_before_the_display_window_is_not_reported(end):
    spec = localized_drop_spec()
    spec = dataclasses.replace(spec, events=(dataclasses.replace(spec.events[0], end=end),))
    analysis = ie.analyze(P.generate(spec))
    assert analysis.incidents == []
    assert [a.action_id for a in analysis.actions] == ["keep_watching"]


@pytest.mark.parametrize("slope", [-0.015, -0.0075, 0.0075, 0.015])
def test_a_continuing_segment_conversion_trend_is_not_a_transition(slope):
    rows = []
    for day in range(42):
        for group in range(3):
            clicks = 10000
            rate = 0.06 * math.exp(slope * day) if group == 1 else 0.06
            rows.append(P.MetricRow(dt.date(2026, 1, 1) + dt.timedelta(days=day), "campaign", f"set-{group}", f"creative-{group}", "device",
                                    clicks * 20, clicks, round(clicks * rate), clicks * 100))
    assert ie.analyze(rows).incidents == []


def test_a_weak_aggregate_shift_with_missing_device_data_remains_unresolved():
    spec = next(s.spec for s in EV.load_scenarios(path=EV.DATASETS / "perf_holdout_20261020.jsonl") if s.id == "h26-p077")
    analysis = ie.analyze(P.generate(spec))
    assert not analysis.has_dims["device"]
    assert analysis.incidents == []
    assert [a.action_id for a in analysis.actions] == ["keep_watching"]
    narrative = ie.template_narrative(analysis.to_context())
    assert "보장할 수는 없습니다" in narrative["summary"]
