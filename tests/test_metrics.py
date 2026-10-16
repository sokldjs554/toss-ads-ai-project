from evals.metrics import EvalRecord, calibration, coverage_curve, recommend_threshold, summarize, wilson


def rec(i, label, pred, conf):
    return EvalRecord(case_id=f"c{i}", label=label, predicted=pred, confidence=conf)


def test_ece_and_high_confidence_error():
    rs = [rec(1, "REJECT", "REJECT", 0.95), rec(2, "REJECT", "APPROVE", 0.9), rec(3, "APPROVE", "APPROVE", 0.6), rec(4, "APPROVE", "HUMAN_REVIEW", 0.3)]
    cal = calibration(rs)
    assert cal["n"] == 3 and cal["high_confidence_error"]["k"] == 1 and cal["confidently_wrong"] == ["c2"]
    assert 0 <= cal["ece"] <= 1
    s = summarize(rs)
    assert s["unsafe_approve"]["k"] == 1 and s["human_review"]["k"] == 1 and s["coverage"]["rate"] == 0.75


def test_coverage_curve_and_threshold():
    rs = [rec(1, "REJECT", "APPROVE", 0.7), rec(2, "REJECT", "REJECT", 0.9), rec(3, "APPROVE", "APPROVE", 0.9)]
    curve = coverage_curve(rs)
    assert curve[0]["tau"] == 0.5 and curve[0]["unsafe_approve"] == 0.5
    assert recommend_threshold(rs, 0.0) == 0.75  # 0.7 짜리 위험 승인을 걸러내는 최소 τ


def test_wilson_bounds():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.35
