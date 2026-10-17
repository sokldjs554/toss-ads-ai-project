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
    # 0.7 짜리 위험 승인을 걸러내려면 τ ≥ 0.75. 0.75~0.9 는 커버리지가 같으므로(2/3) 그중 가장 높은 0.9 를 추천한다 — 같은 커버리지면 보수적인 쪽
    assert recommend_threshold(rs, 0.0) == 0.9
    assert recommend_threshold(rs, 0.5) == 0.7  # 위험 승인 50% 까지 허용하면 0.7 에서 커버리지 3/3 이 최대이고, 그 커버리지를 주는 가장 높은 τ 는 0.7


def test_recommend_threshold_prefers_highest_tau_at_equal_coverage():
    """5차 실제 모델 결과 재현: τ 0.5~0.9 에서 커버리지·위험 승인·오탐이 전부 같았는데 0.5 를 추천했다. 같은 커버리지면 더 높은 τ 가 맞다."""
    rs = [rec(1, "REJECT", "REJECT", 0.92), rec(2, "APPROVE", "APPROVE", 0.95), rec(3, "REVISE", "REVISE", 0.9), rec(4, "REJECT", "REJECT", 0.99)]
    curve = {row["tau"]: row["coverage"] for row in coverage_curve(rs)}
    assert curve[0.5] == curve[0.9] == 1.0 and curve[0.95] < 1.0
    assert recommend_threshold(rs, 0.0, 0.0) == 0.9


def test_wilson_bounds():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.35


def test_curve_replays_real_decision_rules():
    """결정론 block 으로 REJECT 된 사례(신뢰도 0.6)는 τ 를 올려도 자동 판정에 남는다 — 신뢰도 필터만 쓰면 빠진다 (리뷰 재현: 52 vs 51)."""
    from agents.adjudicator import DecisionInputs

    det_reject = DecisionInputs(det_block=True, verdict_available=True, model_decision="REJECT", confidence=0.6, sem_block_verified=False).to_dict()
    clean = DecisionInputs(verdict_available=True, model_decision="APPROVE", confidence=0.9).to_dict()
    rs = [EvalRecord(case_id="a", label="REJECT", predicted="REJECT", confidence=0.6, inputs=det_reject),
          EvalRecord(case_id="b", label="APPROVE", predicted="APPROVE", confidence=0.9, inputs=clean)]
    curve = coverage_curve(rs)
    at80 = next(r for r in curve if r["tau"] == 0.8)
    assert at80["n_auto"] == 2
    legacy = [EvalRecord(case_id="a", label="REJECT", predicted="REJECT", confidence=0.6), EvalRecord(case_id="b", label="APPROVE", predicted="APPROVE", confidence=0.9)]
    assert next(r for r in coverage_curve(legacy) if r["tau"] == 0.8)["n_auto"] == 1
    assert summarize(rs)["curve_method"] == "replay_decide" and summarize(legacy)["curve_method"] == "legacy_confidence_filter"


def test_zero_denominator_rates_are_none():
    s = summarize([rec(1, "REJECT", "REJECT", 0.9)])
    assert s["false_flag"]["n"] == 0 and s["false_flag"]["rate"] is None
