"""Reports must compare a continuing trend with its forecast, not an old level."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core.performance import MetricRow


def _rows(trend: float, factor: float) -> list[MetricRow]:
    rows = []
    for day in range(42):
        scale = trend ** day * (factor if day >= 24 else 1)
        rows.append(MetricRow(dt.date(2026, 1, 1) + dt.timedelta(days=day),
                              "campaign", "set", "creative", "device", 100_000,
                              round(1000 * scale), round(100 * scale), round(100_000 * scale)))
    return rows


@pytest.mark.parametrize("trend,factor,direction,word", [
    (1.03, 0.75, "down", "낮았습니다"),
    (0.97, 1.4, "up", "높았습니다"),
])
def test_trending_incident_report_does_not_reverse_the_detected_change(trend, factor, direction, word):
    rows = _rows(trend, factor)
    analysis = ie.analyze(rows)
    assert analysis.reference_evidence  # selected historical reference predates the display window
    assert analysis.detections["ctr"].trend != 0
    assert analysis.detections["ctr"].spans[0].direction == direction
    diagnosis = analysis.diagnoses[0]
    evidence = analysis.evidence[diagnosis.metric_evidence["ctr"]]
    assert evidence.direction == direction
    assert word in evidence.text
    assert word in ie.template_narrative(analysis.to_context())["headline"]
    report = ins.build_report(rows, router=None, analysis=analysis)
    assert ins.audit_report(report) == []


@pytest.mark.parametrize("trend,factor", [(1.03, 0.75), (0.97, 1.4)])
def test_window_cards_and_incident_evidence_use_the_forecast_count_totals(trend, factor):
    analysis = ie.analyze(_rows(trend, factor))
    diagnosis = analysis.diagnoses[0]
    incident = diagnosis.incident
    for metric in ("clicks", "cost", "conversions"):
        expected = sum(value for day, value in analysis.detections[metric].expected.items()
                       if incident.start <= day <= incident.end)
        if metric in diagnosis.metric_evidence:
            numbers = {n.key: n.value for n in analysis.evidence[diagnosis.metric_evidence[metric]].numbers}
            assert numbers["expected"] == pytest.approx(expected, abs=0.11)
        # Both reproductions occupy the full display window, so summary and detailed values agree.
        assert analysis.window_stats[metric]["expected"] == pytest.approx(expected)
    stats = analysis.window_stats
    assert stats["ctr"]["expected"] == pytest.approx(stats["clicks"]["expected"] / stats["impressions"]["expected"])
    assert math.log(stats["cpa"]["actual"] / stats["cpa"]["expected"]) == pytest.approx(
        math.log(stats["cpc"]["actual"] / stats["cpc"]["expected"])
        - math.log(stats["cvr"]["actual"] / stats["cvr"]["expected"]))
