"""합성 성과 생성기 — 결정론, 값의 불변식, 사건이 데이터에 남기는 흔적, 시나리오 정답(truth).

이 테스트가 보장하는 것은 '생성기가 설계한 대로 동작한다' 까지다. 엔진이 그 흔적을 찾는지는 test_insights_engine.py 가 본다.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import pytest

from core import performance as P
from core.insight_engine import CATALOG

E = P.EventSpec


def daily(rows, key=None, field="clicks"):
    out = defaultdict(float)
    for r in rows:
        if key is None or key(r):
            out[r.date] += getattr(r, field)
    return out


def span(rows, a, b, field, key=None):
    d = daily(rows, key, field)
    days = sorted(d)
    return [d[x] for x in days[a:b + 1]]


def test_same_spec_same_rows_and_other_seed_differs():
    spec = P.ScenarioSpec("d", 3, events=(E("landing_issue", 30, None, {"depth": 0.4}),))
    assert P.generate(spec) == P.generate(spec)
    assert P.generate(spec) != P.generate(spec, seed=4)
    assert P.generate(spec, seed=4) == P.generate(P.ScenarioSpec.from_dict({**spec.to_dict(), "seed": 4}))


def test_shape_and_invariants_hold_for_every_kind():
    kinds = {"budget_cap": {"cap_frac": 0.6}, "creative_fatigue": {}, "tracking_break": {}, "landing_issue": {}, "bid_competition": {}, "audience_saturation": {}, "mix_shift": {}}
    for kind, params in kinds.items():
        spec = P.ScenarioSpec(kind, 9, events=(E(kind, 30, None, params),))
        rows = P.generate(spec)
        assert len(rows) == 42 * 2 * 2 * 2                                            # 날짜 × 세트 2 × 소재 2 × 기기 2
        for r in rows:
            assert 0 <= r.conversions <= r.clicks <= r.impressions and r.cost_krw >= 0
            assert r.reach is not None and r.reach <= r.impressions and 0 <= r.delivery_end_hour <= 23


def test_spec_round_trip_and_collapse_and_optional_columns():
    spec = P.ScenarioSpec("c", 5, days=20, collapse=("device", "creative"), with_reach=False, with_hour=False, events=(E("tracking_break", 12, 15, {"residual": 0.05}),))
    again = P.ScenarioSpec.from_dict(spec.to_dict())
    assert again == spec
    rows = P.generate(spec)
    assert {r.device for r in rows} == {P.NO_DIM} and {r.creative for r in rows} == {P.NO_DIM} and {r.ad_set for r in rows} != {P.NO_DIM}
    assert all(r.reach is None and r.delivery_end_hour is None for r in rows)


def test_weekday_pattern_exists_and_is_stronger_when_amplified():
    def weekend_ratio(amp):
        rows = P.generate(P.ScenarioSpec("w", 4, dow_amp=amp))
        d = daily(rows)
        we = [v for k, v in d.items() if k.weekday() >= 5]
        wk = [v for k, v in d.items() if k.weekday() < 5]
        return (sum(we) / len(we)) / (sum(wk) / len(wk))
    assert weekend_ratio(1.0) < 0.97 and weekend_ratio(3.0) < weekend_ratio(1.0) - 0.15
    assert abs(weekend_ratio(0.0) - 1.0) < 0.08


def test_tracking_break_zeroes_conversions_but_leaves_clicks_untouched():
    base = P.generate(P.ScenarioSpec("b", 6))
    brk = P.generate(P.ScenarioSpec("b", 6, events=(E("tracking_break", 32, None, {"residual": 0.02}),)))
    assert [r.clicks for r in base] == [r.clicks for r in brk]                       # 사건 종류와 상관없이 난수열이 같은 자리에서 소비된다
    pre, post = span(brk, 0, 31, "conversions"), span(brk, 32, 41, "conversions")
    assert sum(post) / len(post) < 0.08 * sum(pre) / len(pre)


def test_landing_issue_cuts_cvr_partially_without_touching_clicks():
    base = P.generate(P.ScenarioSpec("l", 7))
    low = P.generate(P.ScenarioSpec("l", 7, events=(E("landing_issue", 30, None, {"depth": 0.4}),)))
    assert [r.clicks for r in base] == [r.clicks for r in low]
    cvr = lambda rows, a, b: sum(span(rows, a, b, "conversions")) / sum(span(rows, a, b, "clicks"))   # noqa: E731
    ratio = cvr(low, 30, 41) / cvr(base, 30, 41)
    assert 0.5 < ratio < 0.7


def test_creative_fatigue_lowers_only_the_target_creative_and_gradually():
    spec = P.ScenarioSpec("f", 8, events=(E("creative_fatigue", 28, None, {"depth": 0.5, "ramp_days": 10}),))
    rows = P.generate(spec)
    target = lambda r: r.creative == "소재A1"                                            # noqa: E731
    ctr = lambda a, b, key: sum(span(rows, a, b, "clicks", key)) / sum(span(rows, a, b, "impressions", key))   # noqa: E731
    others = lambda r: r.creative != "소재A1"                                            # noqa: E731
    assert ctr(0, 27, target) > 0 and ctr(34, 41, target) < 0.62 * ctr(0, 27, target)
    assert abs(ctr(34, 41, others) / ctr(0, 27, others) - 1) < 0.08
    early, late = ctr(28, 30, target), ctr(36, 41, target)
    assert late < early                                                              # 서서히: 시작 직후보다 나중이 더 낮다


def test_budget_cap_flattens_daily_cost_and_ends_delivery_early():
    rows = P.generate(P.ScenarioSpec("c", 10, events=(E("budget_cap", 30, None, {"cap_frac": 0.6}),)))
    pre, post = span(rows, 0, 29, "cost_krw"), span(rows, 31, 41, "cost_krw")
    cv = lambda v: (sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v)) ** 0.5 / (sum(v) / len(v))   # noqa: E731
    assert cv(post) < 0.02 < cv(pre) and sum(post) / len(post) < 0.75 * sum(pre) / len(pre)
    hours = [r.delivery_end_hour for r in rows if r.date >= dt.date(2026, 9, 7) + dt.timedelta(days=31)]
    assert max(hours) <= 13 and min(r.delivery_end_hour for r in rows if r.date < dt.date(2026, 10, 1)) == 23


def test_bid_competition_raises_cpc_and_cuts_impressions():
    base = P.generate(P.ScenarioSpec("b", 11))
    ev = P.generate(P.ScenarioSpec("b", 11, events=(E("bid_competition", 30, None, {"cpc_up": 0.3, "imp_down": 0.2}),)))
    cpc = lambda rows: sum(span(rows, 31, 41, "cost_krw")) / sum(span(rows, 31, 41, "clicks"))   # noqa: E731
    assert cpc(ev) / cpc(base) > 1.2 and sum(span(ev, 31, 41, "impressions")) / sum(span(base, 31, 41, "impressions")) < 0.85


def test_mix_shift_moves_click_share_but_not_per_device_cvr():
    spec = P.ScenarioSpec("m", 12, first_device_share=0.4, events=(E("mix_shift", 30, None, {"to_share": 0.85, "ramp_days": 2}),))
    rows = P.generate(spec)
    dev = lambda name: (lambda r: r.device == name)                                  # noqa: E731
    share = lambda a, b: sum(span(rows, a, b, "clicks", dev("Android"))) / sum(span(rows, a, b, "clicks"))   # noqa: E731
    cvr = lambda a, b, name: sum(span(rows, a, b, "conversions", dev(name))) / sum(span(rows, a, b, "clicks", dev(name)))   # noqa: E731
    assert share(32, 41) > share(0, 27) + 0.25
    assert abs(cvr(32, 41, "Android") / cvr(0, 27, "Android") - 1) < 0.12 and abs(cvr(32, 41, "iOS") / cvr(0, 27, "iOS") - 1) < 0.12


def test_audience_saturation_drops_ctr_everywhere_and_reach_lags():
    rows = P.generate(P.ScenarioSpec("s", 13, events=(E("audience_saturation", 28, None, {"ctr_depth": 0.3, "ramp_days": 8, "reach_down": 0.25}),)))
    ctr = lambda a, b: sum(span(rows, a, b, "clicks")) / sum(span(rows, a, b, "impressions"))   # noqa: E731
    freq = lambda a, b: sum(span(rows, a, b, "impressions")) / sum(span(rows, a, b, "reach"))   # noqa: E731
    assert ctr(36, 41) < 0.8 * ctr(0, 27) and freq(36, 41) > 1.15 * freq(0, 27)


def test_seasonal_dip_only_amplifies_the_weekly_pattern():
    rows = P.generate(P.ScenarioSpec("seasonal", 14, dow_amp=3.0))
    d = daily(rows)
    sat = [v for k, v in d.items() if k.weekday() == 5]
    assert max(sat[-2:]) < 0.85 * (sum(d.values()) / len(d))                           # 주말은 낮지만
    assert truth(P.ScenarioSpec("seasonal", 14, dow_amp=3.0))["incidents"] == []     # 정답은 '이상 없음'


def truth(spec):
    return P.truth_for(spec)


# ───────────── 정답(truth) 정의 ─────────────
def test_truth_intervals_causes_and_action_sets():
    spec = P.ScenarioSpec("t", 1, events=(E("landing_issue", 30, None, {}), E("budget_cap", 32, 38, {})))
    t = truth(spec)
    assert t["incidents"][0] == {"kind": "landing_issue", "start": "2026-10-07", "end": "2026-10-18"}
    assert t["incidents"][1] == {"kind": "budget_cap", "start": "2026-10-09", "end": "2026-10-15"}
    assert t["main_causes"] == ["landing_issue", "budget_cap"]
    assert not set(t["allowed_actions"]) & set(t["forbidden_actions"])               # 동시 사건: 한쪽이 금지한 것은 허용에서 빠진다
    assert "raise_budget" in t["forbidden_actions"] and "raise_budget" not in t["allowed_actions"]   # 랜딩 문제가 예산 상향을 막는다
    assert "check_landing" in t["allowed_actions"]


def test_controls_forbid_every_campaign_change():
    for spec in (P.ScenarioSpec("n", 1), P.ScenarioSpec("seasonal", 1, dow_amp=3)):
        t = truth(spec)
        assert t["is_control"] and t["incidents"] == [] and t["allowed_actions"] == ["keep_watching"]
        assert set(t["forbidden_actions"]) == set(P.CHANGE_ACTIONS)


def test_tracking_break_forbids_all_changes_and_ambiguous_uses_candidate_causes():
    t = truth(P.ScenarioSpec("tr", 1, events=(E("tracking_break", 31, None, {}),)))
    assert set(t["forbidden_actions"]) == set(P.CHANGE_ACTIONS) and t["allowed_actions"] == ["check_tracking"]
    amb = truth(P.ScenarioSpec("a", 1, events=(E("landing_issue", 31, None, {}),), ambiguous=True, candidate_causes=("landing_issue", "tracking_break")))
    assert amb["main_causes"] == [] and amb["ambiguous"] and "check_tracking" in amb["allowed_actions"] and "raise_budget" in amb["forbidden_actions"]


def test_all_truth_actions_exist_in_the_engine_catalog():
    used = set(P.CHANGE_ACTIONS) | set(P.SAFE_ACTIONS)
    for allowed, forbidden in P.CAUSE_ACTIONS.values():
        used |= set(allowed) | set(forbidden)
    assert used <= set(CATALOG)
    assert {a for a, s in CATALOG.items() if s.kind == "change"} == set(P.CHANGE_ACTIONS)


def test_event_kinds_cover_the_spec_list():
    assert set(P.EVENT_KINDS) == {"budget_cap", "creative_fatigue", "tracking_break", "landing_issue", "bid_competition", "audience_saturation", "mix_shift"}
    assert set(P.CONTROL_KINDS) == {"seasonal_dip", "no_event"} and "multi" in P.ALL_KINDS
    with pytest.raises(KeyError):
        P.truth_for(P.ScenarioSpec("x", 1, events=(E("unknown_kind", 3, None, {}),)))
