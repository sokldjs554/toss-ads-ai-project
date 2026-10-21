"""성과 인사이트 엔진 — 탐지(요일 보정·표본 부족 보류)·진단(분해·확신 공식·원인 불명)·액션(가드 규칙).

변이 확인 대상: 요일 보정(주말 오경보) · 표본 부족 보류 · 금지 액션 가드 · undetermined 경로.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math

import pytest

from agents.insights import unsupported_numbers
from core import insight_engine as ie
from core import performance as P

E = P.EventSpec


def run(spec, **kw):
    rows = P.generate(spec)
    return rows, ie.analyze(rows, **kw)


def overlaps(a, truth_inc):
    t0, t1 = dt.date.fromisoformat(truth_inc["start"]), dt.date.fromisoformat(truth_inc["end"])
    return any(not (i.end < t0 or t1 < i.start) for i in a.incidents)


CASES = [   # (원인, 사건, 기대하는 1순위)
    ("budget_cap", E("budget_cap", 30, None, {"cap_frac": 0.65}), "budget_cap"),
    ("creative_fatigue", E("creative_fatigue", 29, None, {"depth": 0.55, "ramp_days": 10}), "creative_fatigue"),
    ("tracking_break", E("tracking_break", 31, None, {"residual": 0.03}), "tracking_break"),
    ("landing_issue", E("landing_issue", 30, None, {"depth": 0.4}), "landing_issue"),
    ("bid_competition", E("bid_competition", 30, None, {"cpc_up": 0.3, "imp_down": 0.2}), "bid_competition"),
    ("audience_saturation", E("audience_saturation", 29, None, {"ctr_depth": 0.3, "ramp_days": 8, "reach_down": 0.25}), "audience_saturation"),
]


@pytest.mark.parametrize("name,event,cause", CASES, ids=[c[0] for c in CASES])
def test_each_event_is_found_and_its_cause_ranks_first(name, event, cause):
    spec = P.ScenarioSpec(name, 21, events=(event,))
    _, a = run(spec)
    assert a.status == "ok" and a.incidents and overlaps(a, P.truth_for(spec)["incidents"][0])
    ranked = a.ranked_causes()
    assert ranked[0][0] == cause and ranked[0][1] >= ie.ASSERT_CONFIDENCE
    assert all(conf < ie.LIKELY_CONFIDENCE for c, conf in ranked[1:])                   # 다른 후보는 '가능성 높음' 에 닿지 않는다


def test_mix_shift_is_diagnosed_as_composition_not_a_worsening_of_each_group():
    """심슨 역설: 기기별 전환율은 그대로인데 노출이 전환율이 낮은 기기로 쏠려 전체 전환율이 내려간다."""
    spec = P.ScenarioSpec("mix", 22, first_device_share=0.35, events=(E("mix_shift", 30, None, {"to_share": 0.85, "ramp_days": 2}),))
    _, a = run(spec)
    dg = a.diagnoses[0]
    assert dg.top_cause == "mix_shift" and dg.hyp("mix_shift").confidence >= ie.ASSERT_CONFIDENCE
    assert dg.hyp("landing_issue").confidence < ie.SUSPECT_CONFIDENCE                   # '랜딩 문제' 로 오진하지 않는다 (필수 근거가 어긋남)
    mix = next(e for e in a.evidence.values() if e.kind == "mix")
    nums = {n.key: n.value for n in mix.numbers}
    assert nums["mix_share"] > 60 and nums["mix_share"] + nums["rate_share"] == pytest.approx(100, abs=0.2)
    assert [p.action_id for p in a.actions] == ["review_segment_mix"]                   # 멈추거나 예산을 줄이라고 하지 않는다


def test_decline_of_a_whole_ad_set_is_saturation_not_one_tired_creative():
    """같은 광고세트(= 같은 타겟)의 소재가 함께 떨어지면 소재 하나의 문제가 아니라 그 타겟 전체의 문제다. 형제 소재끼리 비교한다."""
    for seed in (81, 82, 84):
        spec = P.ScenarioSpec("set", seed, events=(E("audience_saturation", 29, None, {"ctr_depth": 0.3, "ramp_days": 8, "ad_set_index": 0, "reach_down": 0.25}),))
        _, a = run(spec)
        dg = a.diagnoses[0]
        assert dg.top_cause == "audience_saturation" and dg.hyp("creative_fatigue").confidence < ie.CANDIDATE_CONFIDENCE
        ev = next(e for e in a.evidence.values() if e.kind == "group" and "광고세트" in e.text and "소재" in e.text and "5% 넘게" in e.text)
        assert "세트A_20대" in ev.text or "같은 광고세트" in ev.text


def test_multiple_simultaneous_events_keep_both_causes_in_the_top_two():
    spec = P.ScenarioSpec("multi", 23, events=(E("creative_fatigue", 30, None, {"depth": 0.55, "ramp_days": 10}), E("bid_competition", 31, None, {"cpc_up": 0.3, "imp_down": 0.2})))
    _, a = run(spec)
    assert {c for c, _ in a.ranked_causes()[:2]} == {"creative_fatigue", "bid_competition"}


# ───────────── 탐지: 요일 보정 (변이 확인) ─────────────
def test_weekend_dip_is_not_an_anomaly_but_a_naive_comparison_would_flag_it():
    spec = P.ScenarioSpec("seasonal", 24, dow_amp=3.0)
    rows, a = run(spec)
    assert a.incidents == [] and a.status == "ok"
    # 요일을 보정하지 않고 '최근 주말 vs 직전 평일' 로 비교하면 큰 하락처럼 보인다 — 그래서 이 테스트는 의미가 있다
    by_day = {}
    for r in rows:
        by_day[r.date] = by_day.get(r.date, 0) + r.conversions
    last = sorted(by_day)[-14:]
    weekend = [by_day[d] for d in last if d.weekday() >= 5]
    weekdays = [by_day[d] for d in last if d.weekday() < 5]
    assert sum(weekend) / len(weekend) < 0.75 * sum(weekdays) / len(weekdays)
    assert a.notes and "같은 요일 패턴" in a.evidence[a.notes[0]].text                      # 이상으로 보지 않은 이유를 근거로 남긴다
    assert [p.action_id for p in a.actions] == ["keep_watching"]


def test_weekday_correction_uses_same_weekday_baseline():
    _, a = run(P.ScenarioSpec("seasonal", 25, dow_amp=3.0, start_date=dt.date(2026, 9, 12)))
    det = a.detections["conversions"]
    assert len(det.dow) == 7 and min(det.dow.values()) < -0.15 < 0.0 < max(det.dow.values()) + 0.2    # 주말 요일 계수가 뚜렷이 낮다
    assert a.incidents == []


def test_gradual_trend_in_the_baseline_does_not_cause_a_false_alarm():
    _, a = run(P.ScenarioSpec("trend", 14, trend_per_day=0.003, start_date=dt.date(2026, 9, 10)))
    assert a.incidents == [] and a.detections["impressions"].trend > 0


def test_single_day_noise_spikes_are_not_flagged():
    rows = P.generate(P.ScenarioSpec("spike", 26))
    day = sorted({r.date for r in rows})[35]
    bumped = [dataclasses.replace(r, cost_krw=int(r.cost_krw * 1.12)) if r.date == day else r for r in rows]
    assert ie.analyze(bumped).incidents == []


# ───────────── 탐지: 표본 부족은 판단 보류 (변이 확인) ─────────────
def test_too_few_conversions_withholds_conversion_metrics_and_blocks_big_changes():
    spec = P.ScenarioSpec("low", 27, scale=0.012, events=(E("tracking_break", 31, None, {"residual": 0.03}),))
    _, a = run(spec)
    assert a.status == "partial" and a.sample.clicks_ok and not a.sample.conv_ok
    assert all(a.detections[m].status == "withheld" for m in ie.CONV_METRICS) and a.detections["clicks"].status == "ok"
    assert a.state.sample_partial and a.incidents == []
    assert {p.action_id for p in a.actions} <= {"keep_watching", "collect_more_data"}
    assert "G2" in {v.rule for v in a.avoid} or not a.avoid                                  # 큰 변경은 막혔다고 알린다
    for aid in ("raise_budget", "pause", "raise_bid", "expand_audience"):
        assert not ie.evaluate_action(aid, a.state).allowed


def test_too_few_clicks_withholds_everything():
    _, a = run(P.ScenarioSpec("tiny", 28, scale=0.003))
    assert a.status == "insufficient" and a.incidents == [] and all(d.status == "withheld" for d in a.detections.values())
    assert [p.action_id for p in a.actions][:1] == ["collect_more_data"]
    assert all(not ie.evaluate_action(aid, a.state).allowed for aid in ie.CHANGE_IDS)


def test_short_history_is_not_analyzed():
    rows = P.generate(P.ScenarioSpec("short", 29, days=12))
    a = ie.analyze(rows)
    assert a.status == "insufficient_history" and a.incidents == []
    assert a.evidence[a.status_evidence].numbers and "비교할 수 없습니다" in a.evidence[a.status_evidence].text
    assert ie.analyze([]).status == "insufficient_history"


# ───────────── 진단: 원인 불명은 억지로 고르지 않는다 (변이 확인) ─────────────
def test_without_dimension_columns_the_cause_is_left_undetermined():
    spec = P.ScenarioSpec("amb", 30, collapse=("device", "creative", "ad_set"), events=(E("landing_issue", 31, None, {"depth": 0.3}),))
    rows, a = run(spec)
    assert not {p.action_id for p in a.actions} & set(ie.CHANGE_IDS)
    # 이 경계 표본은 기준 기간 탐색의 추가 비교 보정 뒤 전체 탐지에서는 보류될 수 있다.
    # 행/강도를 바꾸지 않고 미리 지정한 CVR 한 계열의 구간으로 진단기와 액션 보류를 따로 검증한다.
    table = ie.build_table(rows, a.baseline[0], a.window[1])
    base = [d for d in table.dates if a.baseline[0] <= d <= a.baseline[1]]
    window = [d for d in table.dates if a.window[0] <= d <= a.window[1]]
    signal = ie.detect_metric("cvr", table.total, table.present, base, window, a.sample)
    assert signal.spans
    incident = ie._cluster([("cvr", s) for s in signal.spans], a.window[1])[0]
    dg = ie.Diagnoser(table, base, incident, a.detections, a.sample, a.book).diagnose()
    a.incidents, a.diagnoses = [incident], [dg]
    a.state.has_incident, a.state.undetermined, a.state.conf = True, True, a.cause_conf()
    ie._fill_state_from_evidence(a)
    ie._finalize_actions(a)
    assert dg.cause_state == "undetermined" and dg.top_cause == ""
    assert all(h.confidence < ie.LIKELY_CONFIDENCE for h in dg.hypotheses)
    land = dg.hyp("landing_issue")
    assert land.confidence == pytest.approx(ie.CAP_MISSING_CRITICAL) and land.data_missing                      # 꼭 필요한 근거를 못 봐서 묶였다
    assert dg.undetermined_evidence and dg.missing_evidence and "확인하지 못한 것" in a.evidence[dg.missing_evidence].text
    acts = [p.action_id for p in a.actions]
    assert "check_tracking" in acts and "check_landing" in acts and not set(acts) & set(ie.CHANGE_IDS)
    assert "G5" in {v.rule for v in a.avoid}


def test_one_device_collapse_is_not_asserted_as_tracking_or_landing():
    spec = P.ScenarioSpec("dev1", 31, events=(E("tracking_break", 31, None, {"residual": 0.1, "device": "iOS"}),))
    _, a = run(spec)
    dg = a.diagnoses[0]
    assert dg.cause_state in ("likely", "undetermined") and all(h.confidence < ie.ASSERT_CONFIDENCE for h in dg.hypotheses)
    assert not {p.action_id for p in a.actions} & set(ie.CHANGE_IDS)


def test_missing_reach_and_hour_columns_are_reported_as_unchecked_not_as_failed():
    spec = P.ScenarioSpec("miss", 32, with_reach=False, with_hour=False, events=(E("audience_saturation", 29, None, {"ctr_depth": 0.3, "ramp_days": 8}),))
    _, a = run(spec)
    h = a.diagnoses[0].hyp("audience_saturation")
    assert any("도달" in m for m in h.data_missing) and h.coverage < 1.0 and h.confidence >= ie.LIKELY_CONFIDENCE
    cap = a.diagnoses[0].hyp("budget_cap")
    assert any("노출 종료 시각" in m for m in cap.data_missing)


# ───────────── 확신 공식 ─────────────
def mk(cid, role, weight, passed, critical=False):
    return ie.CheckResult(cid, role, weight, critical, cid, passed, "E01" if passed is not None else "", "" if passed is not None else "없음")


def test_confidence_formula_is_support_times_coverage_times_contradiction_discount():
    score = lambda results: ie.Diagnoser.score(object(), "landing_issue", results)    # noqa: E731
    h = score([mk("a", "for", 3, True), mk("b", "for", 1, False)])
    assert h.confidence == pytest.approx(0.75) and h.support_ratio == pytest.approx(0.75) and h.coverage == 1.0
    h = score([mk("a", "for", 3, True), mk("b", "for", 1, None)])
    assert h.coverage == pytest.approx(0.75) and h.confidence == pytest.approx(1.0 * 0.75)           # 확인하지 못한 근거는 확신을 깎는다
    h = score([mk("a", "for", 2, True), mk("k", "against", 1, True), mk("k2", "against", 1, False)])
    assert h.contradiction_ratio == pytest.approx(0.5) and h.confidence == pytest.approx(1.0 * (1 - 0.6 * 0.5))
    assert score([mk("a", "for", 1, None)]).confidence == 0.0


def test_critical_checks_cap_the_confidence():
    score = lambda results: ie.Diagnoser.score(object(), "landing_issue", results)    # noqa: E731
    assert score([mk("a", "for", 3, True, True), mk("b", "for", 3, None, True)]).confidence <= ie.CAP_MISSING_CRITICAL      # 못 봤다
    failed = score([mk("a", "for", 3, True), mk("b", "for", 1, False, True)])
    assert failed.confidence <= ie.CAP_FAILED_CRITICAL and failed.cap_reason                                              # 어긋났다
    assert score([mk("a", "for", 3, True), mk("b", "for", 1, None, "eval")]).confidence == pytest.approx(0.75)            # 자료가 없으면 묶지 않는 필수 근거
    asserted = score([mk("a", "for", 3, True), mk("b", "for", 1, False, "assert")])
    assert asserted.confidence < ie.ASSERT_CONFIDENCE and asserted.state == "likely"


def test_state_thresholds_are_ordered():
    assert 0 < ie.CANDIDATE_CONFIDENCE < ie.SUSPECT_CONFIDENCE < ie.LIKELY_CONFIDENCE < ie.ASSERT_CONFIDENCE <= 1
    assert [ie.state_of(x) for x in (0.9, 0.75, 0.6, 0.45, 0.3, 0.1)] == ["supported", "supported", "likely", "likely", "candidate", "unlikely"]


def test_rival_hypotheses_that_explain_the_same_pattern_are_not_both_asserted():
    spec = P.ScenarioSpec("rival", 21, events=(E("tracking_break", 31, None, {"residual": 0.03}),))
    _, a = run(spec)
    tr, ld = a.diagnoses[0].hyp("tracking_break"), a.diagnoses[0].hyp("landing_issue")
    assert tr.confidence >= ie.ASSERT_CONFIDENCE and ld.confidence < ie.LIKELY_CONFIDENCE and ld.rival_note and ld.cap_reason


# ───────────── 분해 ─────────────
def test_funnel_decomposition_adds_up_to_the_cpa_change():
    spec = P.ScenarioSpec("funnel", 33, events=(E("landing_issue", 30, None, {"depth": 0.45}),))
    _, a = run(spec)
    ev = next(e for e in a.evidence.values() if e.kind == "funnel")
    nums = {n.key: n.value for n in ev.numbers}
    assert nums["cvr_part"] + nums["cpc_part"] == pytest.approx(100, abs=0.2) and nums["cvr_part"] > 90   # 전환당 비용 악화는 전환율 때문 (부호 있는 기여도의 합은 100)
    m = {k: next(e for e in a.evidence.values() if e.kind == "metric" and e.text.startswith(ie.METRIC_LABEL[k])) for k in ("cpa", "cpc", "cvr")}
    val = lambda k, key: next(n.value for n in m[k].numbers if n.key == key)             # noqa: E731
    dl = math.log(val("cpa", "actual") / val("cpa", "expected"))
    assert dl == pytest.approx(math.log(val("cpc", "actual") / val("cpc", "expected")) - math.log(val("cvr", "actual") / val("cvr", "expected")), abs=0.01)


def test_localized_creative_decline_is_found_at_the_creative_level():
    spec = P.ScenarioSpec("seg", 34, events=(E("creative_fatigue", 29, None, {"depth": 0.5, "ramp_days": 10}),))
    _, a = run(spec)
    segs = [s.segment for i in a.incidents for _, s in i.findings if s.segment]
    assert any("소재A1" in s for s in segs)
    ev = next(e for e in a.evidence.values() if e.kind == "group" and "나머지 소재" in e.text)
    assert "소재A1" in ev.text


# ───────────── 액션: 닫힌 카탈로그와 가드 규칙 (변이 확인) ─────────────
def st(**kw):
    return ie.ActionState(**kw)


def test_catalog_is_closed_and_every_action_is_documented():
    assert {"replace_creative", "add_creative", "raise_budget", "lower_budget", "raise_bid", "lower_bid", "check_landing", "check_tracking",
            "expand_audience", "pause", "keep_watching", "collect_more_data", "review_segment_mix"} == set(ie.CATALOG)
    for spec in ie.CATALOG.values():
        assert spec.label and spec.summary and spec.precondition and spec.effect and spec.watch and spec.risk and spec.forbidden_when
        assert spec.kind in ("change", "check", "wait") and set(spec.watch) <= set(ie.METRICS)
    v = ie.evaluate_action("delete_campaign", st())
    assert not v.allowed and v.rule == "G0"


@pytest.mark.parametrize("state,forbidden,rule", [
    (dict(has_incident=True, conf={"tracking_break": 0.4}), ie.CHANGE_IDS, "G4"),                                  # 전환 추적 의심: 무엇도 바꾸지 않는다
    (dict(has_incident=True, sample_insufficient=True), tuple(a for a in ie.CATALOG if a not in ("collect_more_data", "keep_watching")), "G1"),
    (dict(has_incident=True, sample_partial=True, conf={"budget_cap": 0.9}), ie.MONEY_IDS, "G2"),                  # 표본 부족: 중지·큰 변경 금지
    (dict(has_incident=False), ie.CHANGE_IDS, "G3"),                                                               # 이상 없음·요일 효과: 아무것도 바꾸지 않기
    (dict(has_incident=True, undetermined=True), ie.CHANGE_IDS, "G5"),
    (dict(has_incident=True, conf={"landing_issue": 0.5}), ("raise_budget", "raise_bid", "expand_audience"), "G6"),
    (dict(has_incident=True, conf={"creative_fatigue": 0.9}), ("raise_budget", "raise_bid", "pause"), "G7"),
    (dict(has_incident=True, conf={"bid_competition": 0.9}), ("lower_bid", "pause"), "G8"),
    (dict(has_incident=True, conf={"budget_cap": 0.9}), ("lower_budget", "pause"), "G9"),
    (dict(has_incident=True, conf={"mix_shift": 0.9}), ("pause", "lower_budget", "raise_bid"), "G10"),
])
def test_guard_rules_forbid_by_code(state, forbidden, rule):
    s = st(**state)
    for aid in forbidden:
        v = ie.evaluate_action(aid, s)
        assert not v.allowed and v.hard and v.rule == rule, (aid, v)


def test_guard_rules_do_not_block_the_safe_actions():
    s = st(has_incident=True, conf={"tracking_break": 0.9})
    for aid in ("check_tracking", "check_landing", "keep_watching", "review_segment_mix"):
        assert ie.evaluate_action(aid, s).rule != "G4"
    assert ie.evaluate_action("check_tracking", s).allowed


def test_preconditions_require_a_likely_cause_and_are_not_reported_as_forbidden():
    assert not ie.evaluate_action("raise_budget", st(has_incident=True)).allowed and not ie.evaluate_action("raise_budget", st(has_incident=True)).hard
    assert ie.evaluate_action("raise_budget", st(has_incident=True, conf={"budget_cap": 0.8})).allowed
    assert ie.evaluate_action("replace_creative", st(has_incident=True, conf={"creative_fatigue": 0.6})).allowed
    assert not ie.evaluate_action("replace_creative", st(has_incident=True, conf={"creative_fatigue": 0.3})).allowed
    assert not ie.evaluate_action("pause", st(has_incident=True, conf={"landing_issue": 0.8})).allowed                          # 심각하지 않으면 멈추라고 하지 않는다
    assert ie.evaluate_action("pause", st(has_incident=True, conf={"landing_issue": 0.9}, severe_landing=True)).allowed
    assert ie.evaluate_action("keep_watching", st()).allowed


def test_engine_never_proposes_a_change_when_tracking_is_suspected():
    for seed in (41, 42, 43):
        spec = P.ScenarioSpec("trk", seed, events=(E("tracking_break", 30 + seed % 4, None, {"residual": 0.02 * (seed % 3 + 1)}),))
        _, a = run(spec)
        assert a.state.suspect("tracking_break", ie.TRACKING_SUSPECT_CONFIDENCE)
        assert not {p.action_id for p in a.actions} & set(ie.CHANGE_IDS)
        assert a.actions[0].action_id == "check_tracking" and "G4" in {v.rule for v in a.avoid}


def test_landing_issue_never_proposes_more_traffic():
    _, a = run(P.ScenarioSpec("land", 44, events=(E("landing_issue", 30, None, {"depth": 0.4}),)))
    ids = {p.action_id for p in a.actions}
    assert "check_landing" in ids and not ids & {"raise_budget", "raise_bid", "expand_audience"}


# ───────────── 근거 레코드: 모든 숫자는 id 로 되돌릴 수 있다 ─────────────
def test_every_number_in_every_evidence_text_is_declared_in_its_numbers():
    specs = [P.ScenarioSpec(n, 50 + i, events=(e,)) for i, (n, e, _) in enumerate(CASES)]
    specs += [P.ScenarioSpec("seasonal", 60, dow_amp=3.0), P.ScenarioSpec("low", 61, scale=0.012), P.ScenarioSpec("short", 62, days=12),
              P.ScenarioSpec("mix", 63, first_device_share=0.35, events=(E("mix_shift", 30, None, {"to_share": 0.85, "ramp_days": 2}),))]
    for spec in specs:
        _, a = run(spec)
        assert a.evidence, spec.scenario_id
        for e in a.evidence.values():
            nums = [{"key": n.key, "value": n.value, "unit": n.unit, "year": n.year} for n in e.numbers]
            assert unsupported_numbers(e.text, nums, a.names) == [], (spec.scenario_id, e.id, e.text)
            assert e.id.startswith("E") and e.text.strip()


def test_evidence_ids_are_unique_and_referenced_ids_exist():
    _, a = run(P.ScenarioSpec("ids", 70, events=(CASES[3][1],)))
    ctx = a.to_context()
    ids = [e["id"] for e in ctx["evidence"]]
    assert len(ids) == len(set(ids))
    refs = {i for inc in ctx["incidents"] for h in inc["hypotheses"] for i in h["evidence"] + h["contradicted_by"]} | {i for act in ctx["actions"] for i in act["evidence_ids"]}
    assert refs <= set(ids)
    json.dumps(ctx, ensure_ascii=False)                                                    # JSON 으로 바로 쓸 수 있다


# ───────────── 결정론·견고성 ─────────────
def test_analysis_is_deterministic_and_does_not_mutate_input():
    rows = P.generate(P.ScenarioSpec("det", 71, events=(CASES[2][1],)))
    snapshot = list(rows)
    a1, a2 = ie.analyze(rows), ie.analyze(list(reversed(rows)))
    assert rows == snapshot
    assert json.dumps(a1.to_context(), ensure_ascii=False, sort_keys=True) == json.dumps(a2.to_context(), ensure_ascii=False, sort_keys=True)


def test_missing_days_and_odd_shapes_do_not_crash():
    rows = P.generate(P.ScenarioSpec("odd", 72, devices=("Android",), creatives_per_set=1, n_ad_sets=1, events=(E("landing_issue", 30, None, {"depth": 0.4}),)))
    dropped = {sorted({r.date for r in rows})[i] for i in (3, 10, 33)}
    a = ie.analyze([r for r in rows if r.date not in dropped])
    assert a.status in ("ok", "partial") and a.incidents
    assert a.diagnoses[0].cause_state in ("supported", "likely", "undetermined")


def test_period_argument_selects_the_window():
    spec = P.ScenarioSpec("per", 73, events=(E("landing_issue", 30, None, {"depth": 0.45}),))
    rows = P.generate(spec)
    start = spec.start_date
    early = ie.analyze(rows, (start + dt.timedelta(days=14), start + dt.timedelta(days=27)))
    assert early.window[0] == start + dt.timedelta(days=14) and early.incidents == []
    late = ie.analyze(rows, (start + dt.timedelta(days=28), start + dt.timedelta(days=41)))
    assert late.incidents and late.window[1] == start + dt.timedelta(days=41)


def test_a_change_in_the_good_direction_is_reported_but_never_triggers_a_change():
    rows = P.generate(P.ScenarioSpec("good", 74))
    days = sorted({r.date for r in rows})
    boost = {d for d in days[-9:]}
    better = [dataclasses.replace(r, clicks=min(r.impressions, int(r.clicks * 1.3)), conversions=min(min(r.impressions, int(r.clicks * 1.3)), int(r.conversions * 1.3)),
                                  cost_krw=int(r.cost_krw * 1.3)) if r.date in boost else r for r in rows]
    a = ie.analyze(better)
    assert a.incidents and all(i.polarity == "better" for i in a.incidents if any(m in ("ctr", "clicks", "cvr", "conversions") for m, _ in i.findings))
    assert all(dg.cause_state == "not_applicable" for dg in a.diagnoses if dg.incident.polarity == "better")
    assert not {p.action_id for p in a.actions} & set(ie.CHANGE_IDS)


def test_names_with_digits_are_collected_so_numbers_in_them_are_not_mistaken_for_figures():
    _, a = run(P.ScenarioSpec("names", 75))
    assert "소재A1" in a.names and "세트A_20대" in a.names
    assert unsupported_numbers("소재A1과 세트A_20대, 2026년 캠페인", [], a.names + ["2026년 캠페인"]) == []
