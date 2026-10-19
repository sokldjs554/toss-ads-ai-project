"""성과 인사이트 평가 — 시나리오 구성(dev/holdout 분리·엔진 동결), 지표 계산, 게이트, 저장된 보고서의 최신성.

holdout 은 엔진을 동결한 뒤 만들었고 결과를 보고 엔진을 고치지 않았다. 그래서 엔진 지문이 바뀌면 이 파일의 테스트가 실패한다 —
의도한 변경이라면 holdout 을 '고정 후 한 번 본 값' 이라고 더는 말할 수 없으니 새 holdout 을 만들고 문서에 그 사실을 적어야 한다.
"""
from __future__ import annotations

import json
import shutil

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core import performance as P
from evals import insights_eval as EV

DEV = EV.load_scenarios("dev")
HOLD = EV.load_scenarios("holdout")


# ───────────── 시나리오 구성 ─────────────
def test_there_are_at_least_48_scenarios_split_into_dev_and_holdout():
    assert len(DEV) >= 36 and len(HOLD) >= 12 and len(DEV) + len(HOLD) >= 48
    ids = [s.id for s in DEV + HOLD]
    assert len(ids) == len(set(ids)) and all(s.split == "dev" for s in DEV) and all(s.split == "holdout" for s in HOLD)


def test_every_event_kind_and_every_control_is_covered_in_both_splits():
    for scenarios in (DEV, HOLD):
        kinds = {e.kind for s in scenarios for e in s.spec.events}
        assert set(P.EVENT_KINDS) <= kinds
        fams = {s.family for s in scenarios}
        assert {"control", "single", "multi", "ambiguous", "low_sample"} <= fams
        assert sum(1 for s in scenarios if s.truth["is_control"]) >= 4
        assert sum(1 for s in scenarios if len(s.spec.events) >= 2) >= 3


def test_holdout_uses_other_seeds_other_structures_and_confusable_combinations():
    dev_seeds, hold_seeds = {s.spec.seed for s in DEV}, {s.spec.seed for s in HOLD}
    assert not dev_seeds & hold_seeds and min(hold_seeds) > max(dev_seeds)
    assert {s.spec.n_ad_sets for s in HOLD} - {s.spec.n_ad_sets for s in DEV}                    # dev 에 없던 구조
    assert {len(s.spec.devices) for s in HOLD} - {len(s.spec.devices) for s in DEV}
    assert max(s.spec.noise for s in HOLD) > max(s.spec.noise for s in DEV)                       # 더 큰 잡음
    assert min(abs(s.spec.trend_per_day) for s in HOLD) == 0 or max(abs(s.spec.trend_per_day) for s in HOLD) > max(abs(s.spec.trend_per_day) for s in DEV)
    pairs = {frozenset(e.kind for e in s.spec.events) for s in HOLD if len(s.spec.events) >= 2}
    assert frozenset({"creative_fatigue", "audience_saturation"}) in pairs and frozenset({"bid_competition", "budget_cap"}) in pairs     # 같은 지표를 건드리는 조합
    dev_pairs = {frozenset(e.kind for e in s.spec.events) for s in DEV if len(s.spec.events) >= 2}
    assert not pairs & dev_pairs                                                                  # dev 에서 본 조합이 아니다


def test_scenario_truth_matches_what_the_generator_input_implies():
    for s in DEV + HOLD:
        assert s.truth == P.truth_for(s.spec), s.id
        assert not set(s.truth["allowed_actions"]) & set(s.truth["forbidden_actions"]), s.id
        assert set(s.truth["allowed_actions"]) | set(s.truth["forbidden_actions"]) <= set(ie.CATALOG), s.id
        for inc in s.truth["incidents"]:
            assert inc["start"] <= inc["end"]


def test_dataset_file_declares_that_it_is_synthetic_and_names_its_limits():
    meta = EV.dataset_meta()
    assert meta["kind"] == "synthetic" and "실제 광고 데이터가 아니다" in meta["description"] and "순환" in meta["caution"]
    assert set(meta["splits"]) == {"dev", "holdout"} and meta["holdout_frozen_engine_fingerprint"]


# ───────────── 엔진 동결 ─────────────
def test_engine_has_not_changed_since_the_holdout_was_created():
    frozen = {s.frozen_engine for s in HOLD}
    assert len(frozen) == 1 and "" not in frozen, "holdout 시나리오는 만든 시점의 엔진 지문을 가져야 한다"
    assert frozen == {EV.engine_fingerprint()}, (
        "엔진(core/insight_engine.py·core/performance.py)이 holdout 을 만든 뒤 바뀌었다. holdout 결과를 보고 엔진을 고쳤다면 holdout 은 더 이상 최종 확인용이 아니다 — "
        "새 holdout 을 만들고 그 사실을 문서에 적을 것.")
    assert EV.dataset_meta()["holdout_frozen_engine_fingerprint"] in frozen
    assert not EV.evaluate("holdout")["engine_changed_after_holdout"]


def test_the_fingerprint_notices_a_change_to_the_engine(monkeypatch, tmp_path):
    base = EV.engine_fingerprint()
    fake = tmp_path / "core"
    fake.mkdir()
    for name in ("insight_engine.py", "performance.py"):
        shutil.copy(EV.ROOT / "core" / name, fake / name)
    (fake / "insight_engine.py").write_text((fake / "insight_engine.py").read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    monkeypatch.setattr(EV, "ROOT", tmp_path)
    assert EV.engine_fingerprint() != base


# ───────────── 저장된 보고서 ─────────────
@pytest.fixture(scope="module")
def fresh():
    return EV.evaluate("all")


@pytest.fixture(scope="module")
def stored():
    rep = EV.load_report()
    assert rep is not None, "evals/reports/insights_controlled.json 이 있어야 한다 (python -m evals.insights_eval --out …)"
    return rep


def test_stored_report_is_up_to_date_and_reproducible(fresh, stored):
    assert stored["engine_fingerprint"] == fresh["engine_fingerprint"] and stored["dataset_fingerprint"] == fresh["dataset_fingerprint"]
    assert stored["splits"] == fresh["splits"]                                                    # 결정론: 같은 코드·같은 시나리오면 같은 숫자
    assert stored["scenarios"] == json.loads(json.dumps(fresh["scenarios"]))


def test_stored_report_carries_the_honesty_fields(stored):
    assert stored["data"] == "synthetic" and "python -m evals.insights_eval" in stored["command"] and "--out evals/reports/insights_controlled.json" in stored["command"]
    text = " ".join(stored["limitations"])
    for needle in ("합성", "순환", "holdout", "모델 문장화의 품질은 측정하지 않았다", "확신은 확률이 아니다"):
        assert needle in text, needle
    assert stored["holdout_observations"] and stored["engine_changed_after_holdout"] is False
    assert set(stored["splits"]) == {"dev", "holdout"} and stored["engine_params"]["assert_confidence"] == ie.ASSERT_CONFIDENCE


def test_the_zero_gates_hold_on_both_splits(stored):
    for split, agg in stored["splits"].items():
        assert agg["forbidden_proposals"] == 0, (split, agg["scenarios_with_forbidden"])        # 금지 액션 제안 0
        assert agg["unsupported_numbers"] == 0 and agg["audit_problems"] == 0                      # 보고서 근거 없는 숫자 0
        assert agg["perturbation_escaped"] == 0 and agg["perturbation_false_drops"] == 0 and agg["perturbation_injected"] > 100
    assert EV.check_gates(stored, stored) == []


def test_reported_numbers_are_not_better_than_the_data_say(stored):
    """저장된 숫자를 그대로 믿지 않고, 시나리오별 결과에서 다시 센다."""
    for split, agg in stored["splits"].items():
        rows = [r for r in stored["scenarios"] if r["split"] == split]
        assert agg == EV.aggregate(rows)
    hold = stored["splits"]["holdout"]
    assert hold["control_false_alarm_n"] == "2/4"                                                  # holdout 의 나쁜 결과가 숨겨지지 않았다
    assert any("대조" in o and "2개" in o for o in stored["holdout_observations"])


# ───────────── 게이트 ─────────────
def test_gate_fails_on_forbidden_actions_unsupported_numbers_and_regressions(stored):
    bad = json.loads(json.dumps(stored))
    bad["splits"]["dev"]["forbidden_proposals"] = 1
    bad["splits"]["holdout"]["unsupported_numbers"] = 2
    bad["splits"]["dev"]["perturbation_escaped"] = 3
    bad["splits"]["dev"]["detection_recall"] = stored["splits"]["dev"]["detection_recall"] - 0.2
    bad["splits"]["holdout"]["control_false_alarm_rate"] = 1.0
    fails = " | ".join(EV.check_gates(bad, stored))
    for needle in ("dev: forbidden_proposals", "holdout: unsupported_numbers", "dev: perturbation_escaped", "dev: detection_recall", "holdout: control_false_alarm_rate"):
        assert needle in fails, needle
    changed = json.loads(json.dumps(stored))
    changed["engine_changed_after_holdout"] = True
    assert any("holdout" in f and "바뀌었습니다" in f for f in EV.check_gates(changed, stored))


def test_small_wobble_within_tolerance_does_not_fail_the_gate(stored):
    ok = json.loads(json.dumps(stored))
    ok["splits"]["dev"]["detection_recall"] = round(stored["splits"]["dev"]["detection_recall"] - EV.REGRESSION_TOLERANCE + 0.001, 4)
    assert EV.check_gates(ok, stored) == []


def test_cli_writes_the_report_and_the_gate_passes(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert EV.main(["--split", "dev", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["splits"]["dev"]["n_scenarios"] == len(DEV)
    assert EV.main(["--split", "all", "--gate"]) == 0
    printed = capsys.readouterr().out
    assert "게이트 통과" in printed and "이상 탐지 재현율" in printed and "금지 액션 제안 0건" in printed


# ───────────── 지표 정의 (손으로 만든 결과로 확인) ─────────────
def res(**kw):
    base = {"id": "x", "split": "dev", "family": "single", "ambiguous": False, "detectable": True, "is_control": False, "truth_incidents": [{"kind": "landing_issue"}], "engine_incidents": [],
            "truth_detected": [True], "engine_incident_tp": [], "main_causes": ["landing_issue"], "ranked": [], "top1": None, "top3": [], "cause_states": [], "actions": [],
            "forbidden_hits": [], "allowed_hit": True, "avoid_rules": [],
            "report": {"unsupported_numbers": 0, "audit_problems": 0}, "perturbation": {"injected": 1, "caught": 1, "escaped": 0, "false_drops": 0}}
    base.update(kw)
    return base


def test_detection_precision_recall_and_control_false_alarm_definitions():
    rows = [res(truth_detected=[True, False], engine_incident_tp=[True, False]),                          # 사건 2개 중 1개 탐지, 엔진 구간 2개 중 1개 맞음
            res(detectable=False, truth_detected=[False]),                                                # 탐지를 기대하지 않은 것은 재현율 분모에서 뺀다
            res(is_control=True, truth_incidents=[], truth_detected=[], main_causes=[], engine_incidents=[{"x": 1}], engine_incident_tp=[False]),
            res(is_control=True, truth_incidents=[], truth_detected=[], main_causes=[])]
    a = EV.aggregate(rows)
    assert a["detection_recall"] == 0.5 and a["detection_recall_n"] == "1/2"
    assert a["detection_precision"] == round(1 / 3, 4) and a["control_false_alarm_rate"] == 0.5 and a["control_false_alarm_n"] == "1/2"


def test_cause_accuracy_counts_abstention_as_wrong_and_multi_needs_all_main_causes():
    rows = [res(top1_correct=True, top3_correct=True, any_in_top3=True, abstained=False, top1="landing_issue"),
            res(top1_correct=False, top3_correct=False, any_in_top3=False, abstained=True),                                 # 보류
            res(main_causes=["a", "b"], top1_correct=True, top3_correct=False, any_in_top3=True, abstained=False, top1="a"),   # 동시 사건: 하나만 3순위 안
            res(top1_correct=False, top3_correct=True, any_in_top3=True, abstained=False, top1="x")]
    a = EV.aggregate(rows)
    assert a["cause_top1"] == 0.5 and a["cause_top3"] == 0.5 and a["abstained_n"] == 1 and a["n_resolvable"] == 4
    assert a["overabstain_rate"] == 0.25


def test_undetermined_appropriateness_and_forbidden_counting():
    rows = [res(ambiguous=True, main_causes=[], cause_states=["undetermined"]), res(ambiguous=True, main_causes=[], cause_states=["likely"]),
            res(ambiguous=True, main_causes=[], cause_states=["asserted"]), res(forbidden_hits=["pause", "raise_budget"], id="bad")]
    a = EV.aggregate(rows)
    assert a["ambiguous_not_asserted"] == round(2 / 3, 4) and a["ambiguous_undetermined"] == round(1 / 3, 4)
    assert a["forbidden_proposals"] == 2 and a["scenarios_with_forbidden"] == ["bad"]


def test_calibration_bins_use_only_resolvable_scenarios():
    rows = [res(top1_correct=True, top3_correct=True, any_in_top3=True, abstained=False, ranked=[("landing_issue", 0.9), ("budget_cap", 0.3)]),
            res(top1_correct=False, top3_correct=False, any_in_top3=False, abstained=False, ranked=[("creative_fatigue", 0.8), ("landing_issue", 0.5)]),
            res(ambiguous=True, main_causes=[], ranked=[("landing_issue", 0.9)])]
    bins = {b["bin"][:2]: b for b in EV.calibration(rows)["bins"]}
    assert bins["단정"]["n"] == 2 and bins["단정"]["accuracy"] == 0.5 and bins["가능"]["n"] == 1 and bins["가능"]["accuracy"] == 1.0 and bins["후보"]["n"] == 1 and bins["후보"]["accuracy"] == 0.0


# ───────────── 주입 시험 자체를 시험한다 (변이 확인용) ─────────────
def make(spec):
    rows = P.generate(spec)
    a = ie.analyze(rows)
    return a, ins.build_report(rows, router=None, settings=None, analysis=a)


def test_perturbation_test_catches_every_kind_on_representative_scenarios():
    seen: set[str] = set()
    for sid in ("dev-tracking_break-1", "dev-landing_issue-2", "dev-no_event-1", "dev-ambiguous-1", "dev-seasonal_dip-2"):
        sc = next(s for s in DEV if s.id == sid)
        a, rep = make(sc.spec)
        r = EV.perturbation_test(a, rep)
        assert r["injected"] >= 7 and r["escaped"] == 0 and r["caught"] == r["injected"] and r["false_drops"] == 0, (sid, r)
        seen.add(sid)
    assert len(seen) == 5


def test_weakening_the_number_check_makes_the_injection_test_fail(monkeypatch):
    sc = next(s for s in DEV if s.id == "dev-landing_issue-2")
    a, rep = make(sc.spec)
    monkeypatch.setattr(ins, "unsupported_numbers", lambda text, nums, names: [])
    r = EV.perturbation_test(a, rep)
    assert r["escaped"] >= 2 and "number_tweak" in r["escaped_kinds"] and "number_inject" in r["escaped_kinds"]


def test_weakening_the_action_guard_makes_the_injection_test_fail(monkeypatch):
    sc = next(s for s in DEV if s.id == "dev-tracking_break-1")
    a, rep = make(sc.spec)
    monkeypatch.setattr(ins, "evaluate_action", lambda aid, state: ie.ActionVerdict(aid, True))
    r = EV.perturbation_test(a, rep)
    assert "forbidden_action" in r["escaped_kinds"]


def test_weakening_the_cause_assertion_limit_makes_the_injection_test_fail(monkeypatch):
    sc = next(s for s in DEV if s.id == "dev-landing_issue-2")
    a, rep = make(sc.spec)
    monkeypatch.setattr(ins, "causal_issue", lambda *args, **kw: "")
    assert "cause_overclaim" in EV.perturbation_test(a, rep)["escaped_kinds"]
