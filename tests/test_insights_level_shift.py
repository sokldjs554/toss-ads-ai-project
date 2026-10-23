"""Level shift versus continuing slope: analytic development fixtures, not final set."""
import datetime as dt
import math

import pytest

from core import insight_engine as ie


def detect(metric="cvr", step=0.5, slope=0.0, recovered=False, sparse=False):
    dates = [dt.date(2026, 2, 2) + dt.timedelta(days=i) for i in range(42)]
    counts = {}
    for i, day in enumerate(dates):
        # Repeated small non-monotonic noise, with weekday effects and a known step.
        factor = math.exp(slope * i + (0.08, -0.06, 0.03, -0.04, 0.06)[i % 5])
        factor *= 1 + 0.12 * (day.weekday() == 5)
        if i >= 20 and not (recovered and i >= 28):
            factor *= step
        counts[day] = ie.Counts(100000, 10000, round(600 * factor), 1000000)
    return ie.detect_metric(metric, counts, set(dates), dates[:20], dates[20:],
                            ie.SampleStatus(True, not sparse, 200000, 12000, 20),
                            h_score=6, min_effect=.15, min_len=2, comparisons=2016,
                            report_start=dates[28], include_slope_uncertainty=True)


def test_joint_evidence_recovers_persistent_change_without_assuming_zero_slope():
    det = detect(step=.6)
    assert det.spans
    assert det.spans[0].direction == "down"
    assert det.spans[0].start >= dt.date(2026, 3, 2)
    assert det.spans[0].pct < -.3


@pytest.mark.parametrize("slope", [-.015, -.006, 0, .006, .015])
def test_continuing_slope_is_not_a_new_level_change(slope):
    assert not detect(step=1, slope=slope).spans


def test_recovered_change_is_not_a_recent_persistent_change():
    assert not detect(recovered=True).spans


def test_insufficient_conversions_remain_withheld():
    assert detect(sparse=True).status == "withheld"


def test_inverse_metric_direction_is_preserved():
    det = detect(metric="cpa", step=.6)
    assert det.spans and det.spans[0].direction == "up"
