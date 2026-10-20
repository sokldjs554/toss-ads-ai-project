"""Observed report values must remain measurements, even when detection uses smoothing."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core.performance import MetricRow


def daily_rows(recent_conversions: int, recent_cost: int = 100_000) -> list[MetricRow]:
    return [
        MetricRow(
            dt.date(2026, 1, 1) + dt.timedelta(days=i),
            "campaign", "set", "creative", "device", 10_000, 1_000,
            5 if i < 28 else recent_conversions,
            100_000 if i < 28 else recent_cost,
        )
        for i in range(42)
    ]


@pytest.mark.parametrize("recent_cost", [100_000, 50_000])
def test_funnel_contributions_follow_the_raw_count_identity(recent_cost):
    analysis = ie.analyze(daily_rows(1, recent_cost))
    evidence = next(e for e in analysis.evidence.values() if e.kind == "funnel")
    numbers = {n.key: n.value for n in evidence.numbers}
    log_cpa_change = math.log((recent_cost / 1) / (100_000 / 5))
    expected_cpc_share = math.log(recent_cost / 100_000) / log_cpa_change * 100
    expected_cvr_share = -math.log((1 / 1_000) / (5 / 1_000)) / log_cpa_change * 100

    assert numbers["cpc_part"] == pytest.approx(expected_cpc_share, abs=0.05)
    assert numbers["cvr_part"] == pytest.approx(expected_cvr_share, abs=0.05)
    assert numbers["cpc_part"] + numbers["cvr_part"] == pytest.approx(100, abs=0.1)


def test_zero_conversions_report_explains_undefined_cpa_instead_of_inventing_it():
    report = ins.build_report(daily_rows(0), router=None)
    assert "전환당 비용" not in report.headline
    assert any("전환이 0건" in f.text and "계산" in f.text for f in report.findings)
    assert not any("전환당 비용은" in e.text for e in report.analysis.evidence.values())
    assert ins.audit_report(report) == [] and report.unsupported_numbers == 0


def test_zero_conversion_cards_and_chart_keep_observed_values():
    analysis = ie.analyze(daily_rows(0))
    assert analysis.window_stats["cvr"]["actual"] == 0
    assert "cpa" not in analysis.window_stats
    recent_cpa = [p for p in analysis.series["cpa"] if p["window"]]
    assert len(recent_cpa) == 14 and all(p["actual"] is None for p in recent_cpa)
    assert all(p["actual"] == 0 for p in analysis.series["cvr"] if p["window"])


def test_zero_spend_report_keeps_zero_cost_without_log_decomposition():
    report = ins.build_report(daily_rows(1, 0), router=None)
    assert report.analysis.window_stats["cpa"]["actual"] == 0
    assert report.analysis.window_stats["cpc"]["actual"] == 0
    assert ins.audit_report(report) == [] and report.unsupported_numbers == 0
