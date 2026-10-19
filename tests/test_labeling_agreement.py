"""라벨러 일치도 — kappa 를 알려진 예제와 대조하고, 표본이 적을 때의 신뢰구간·경고, 규칙별 일치도, 혼동 행렬, 세 명 이상.

일치도는 '라벨러끼리 같게 읽었는가' 이지 '라벨이 옳은가' 가 아니다. 여기서는 계산이 맞는지만 본다.
"""
from __future__ import annotations

import pytest

from ops.labeling import (
    ASSUMED_MIN_ITEMS_FOR_KAPPA,
    LabelDecision,
    agreement_report,
    bootstrap_kappa_ci,
    cohen_report,
    cohens_kappa,
    collect_ratings,
    confusion_matrix,
    fleiss_kappa,
    fleiss_kappa_from_ratings,
    fleiss_report,
    landis_koch,
    per_rule_agreement,
    percent_agreement,
)
from tests.test_labeling_common import ann

A, R, X = LabelDecision.APPROVE, LabelDecision.REVISE, LabelDecision.REJECT


# --- 알려진 예제 ------------------------------------------------------------------------------------
def two_by_two():
    """위키백과의 Cohen's kappa 예: 제안서 50건, 둘 다 승인 20 · 첫째만 승인 5 · 둘째만 승인 10 · 둘 다 불승인 15. 관측 일치 0.7, 우연 일치 0.5 → kappa 0.4."""
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    return a, b


def test_cohens_kappa_matches_the_textbook_two_by_two_example():
    a, b = two_by_two()
    assert percent_agreement(a, b) == pytest.approx(0.7)
    assert cohens_kappa(a, b) == pytest.approx(0.4, abs=1e-12)


def test_cohens_kappa_matches_a_hand_computed_three_category_table():
    """3×3 표 (행=첫째, 열=둘째): [[9,2,1],[1,8,1],[0,1,7]] → po=24/30, pe=(12·10+10·11+8·9)/900=302/900, kappa=(0.8−302/900)/(1−302/900)."""
    table = [[9, 2, 1], [1, 8, 1], [0, 1, 7]]
    cats = [A, R, X]
    a, b = [], []
    for i, row in enumerate(table):
        for j, n in enumerate(row):
            a += [cats[i]] * n
            b += [cats[j]] * n
    expected = (24 / 30 - 302 / 900) / (1 - 302 / 900)
    assert cohens_kappa(a, b) == pytest.approx(expected, abs=1e-12) and expected == pytest.approx(0.69899, abs=1e-5)
    assert confusion_matrix(a, b)["APPROVE"] == {"APPROVE": 9, "REVISE": 2, "REJECT": 1}


def test_kappa_extremes():
    assert cohens_kappa([A, R, X, A], [A, R, X, A]) == pytest.approx(1.0)
    assert cohens_kappa(["x", "x", "y", "y"], ["x", "y", "x", "y"]) == pytest.approx(0.0)         # 우연 수준
    assert cohens_kappa(["x", "y"] * 5, ["y", "x"] * 5) == pytest.approx(-1.0)                    # 체계적으로 반대
    assert cohens_kappa([A] * 5, [A] * 5) is None                                                  # 한 가지 라벨만 → 정의되지 않음


def test_kappa_rejects_mismatched_or_empty_input():
    with pytest.raises(ValueError):
        cohens_kappa([A], [A, R])
    with pytest.raises(ValueError):
        percent_agreement([], [])


FLEISS_WIKI = [[0, 0, 0, 0, 14], [0, 2, 6, 4, 2], [0, 0, 3, 5, 6], [0, 3, 9, 2, 0], [2, 2, 8, 1, 1],
               [7, 7, 0, 0, 0], [3, 2, 6, 3, 0], [2, 5, 3, 2, 2], [6, 5, 2, 1, 0], [0, 2, 2, 3, 7]]


def test_fleiss_kappa_matches_the_published_example():
    """위키백과의 Fleiss' kappa 예: 10개 항목 · 평가자 14명 · 5개 범주 → kappa ≈ 0.210."""
    assert round(fleiss_kappa(FLEISS_WIKI), 3) == 0.210


def test_fleiss_with_two_raters_equals_scotts_pi_not_cohens_kappa():
    a = [A, A, R, R, X, X, A, R, X, A]
    b = [A, R, R, X, X, A, A, R, R, A]
    labels = [A, R, X]
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pe_pi = sum(((sum(1 for v in a if v == c) + sum(1 for v in b if v == c)) / (2 * n)) ** 2 for c in labels)
    scott = (po - pe_pi) / (1 - pe_pi)
    assert fleiss_kappa_from_ratings([[x, y] for x, y in zip(a, b)], labels) == pytest.approx(scott, abs=1e-12)
    assert cohens_kappa(a, b) != pytest.approx(scott, abs=1e-6)       # 두 값은 다르다 — 어느 것을 썼는지 보고서에 method 로 남긴다


def test_fleiss_input_validation():
    with pytest.raises(ValueError):
        fleiss_kappa([[1, 1], [2, 1]])          # 항목마다 평가자 수가 다름
    with pytest.raises(ValueError):
        fleiss_kappa([[1, 0], [0, 1]])          # 평가자 1명
    assert fleiss_kappa([[3, 0], [3, 0]]) is None


def test_landis_koch_bands_are_reference_labels_only():
    cases = [(-0.1, "poor"), (0.0, "slight"), (0.2, "slight"), (0.21, "fair"), (0.4, "fair"), (0.41, "moderate"), (0.6, "moderate"), (0.61, "substantial"),
             (0.8, "substantial"), (0.81, "almost_perfect"), (1.0, "almost_perfect")]
    for k, key in cases:
        assert landis_koch(k)[0] == key, k
    assert landis_koch(None)[0] == "undefined"


# --- 신뢰구간 · 표본 부족 경고 --------------------------------------------------------------------------
def test_bootstrap_interval_is_reproducible_and_contains_the_point_estimate():
    a, b = two_by_two()
    r1 = bootstrap_kappa_ci(a, b, seed=7)
    r2 = bootstrap_kappa_ci(a, b, seed=7)
    assert r1 == r2                                             # 시드가 같으면 같은 구간
    assert bootstrap_kappa_ci(a, b, seed=8)[:2] != r1[:2]       # 시드를 바꾸면 달라진다 (난수를 쓴다는 증거)
    lo, hi, valid = r1
    assert lo < cohens_kappa(a, b) < hi and valid > 900


def test_interval_narrows_as_the_sample_grows():
    a, b = two_by_two()
    pairs = list(zip(a, b))
    small = cohen_report(*zip(*pairs[::4]))            # 13건 (두 라벨이 고루 섞이도록 간격을 두고 뽑는다)
    large = cohen_report(a * 4, b * 4)                 # 같은 분포의 200건
    assert (large.ci_high - large.ci_low) < (small.ci_high - small.ci_low)


def test_small_sample_gets_the_insufficient_sample_warning_and_an_interval():
    a, b = two_by_two()
    small = cohen_report(*zip(*list(zip(a, b))[::4]))
    assert not small.sufficient and small.ci_low is not None and small.n_items == 13
    assert any("표본 부족" in w for w in small.warnings)
    big = cohen_report(a, b)
    assert big.n_items >= ASSUMED_MIN_ITEMS_FOR_KAPPA and big.sufficient and not any("표본 부족" in w for w in big.warnings)


def test_skewed_labels_and_undefined_kappa_get_their_own_warnings():
    a = [A] * 38 + [R, R]
    b = [A] * 37 + [R, A, R]
    rep = cohen_report(a, b)
    assert any("치우쳐" in w for w in rep.warnings)
    same = cohen_report([A] * 40, [A] * 40)
    assert same.kappa is None and any("계산할 수 없습니다" in w for w in same.warnings)


def test_below_target_signals_guideline_revision():
    a, b = two_by_two()
    rep = cohen_report(a, b)              # kappa 0.4 < 0.6
    assert rep.below_target is True
    assert cohen_report([A, R, X] * 10, [A, R, X] * 10).below_target is False


def test_fleiss_report_carries_method_and_interval():
    ratings = [[A, A, A], [A, R, A], [R, R, R], [X, X, R], [X, X, X], [A, A, A]] * 6
    rep = fleiss_report(ratings)
    assert rep.method == "fleiss" and rep.n_raters == 3 and rep.ci_low is not None and rep.n_items == 36


# --- 라벨 모음 → 보고서 ---------------------------------------------------------------------------------
def anns(decs_a, decs_b, who=("가", "나"), rules_a=None, rules_b=None):
    out = []
    for i, (x, y) in enumerate(zip(decs_a, decs_b)):
        out.append(ann(f"it{i}", who[0], x, rules=(rules_a or {}).get(i)))
        out.append(ann(f"it{i}", who[1], y, rules=(rules_b or {}).get(i)))
    return out


def test_a_single_labeler_cannot_be_measured():
    rep = agreement_report([ann(f"it{i}", "가", A) for i in range(5)])
    assert not rep.measurable and rep.overall is None and "한 명" in rep.warnings[0]


def test_two_labelers_who_never_saw_the_same_ad_cannot_be_measured():
    rep = agreement_report([ann("it1", "가", A), ann("it2", "나", A)])
    assert not rep.measurable and "함께 라벨한 것이 없어" in rep.warnings[0]


def test_two_labeler_report_matches_the_pure_functions():
    a, b = ([A] * 10 + [R] * 6 + [X] * 4), ([A] * 8 + [R] * 2 + [R] * 5 + [X] * 1 + [X] * 4)
    rep = agreement_report(anns(a, b))
    assert rep.overall.method == "cohen" and rep.overall.n_items == 20
    assert rep.overall.kappa == pytest.approx(cohens_kappa(a, b)) and rep.overall.percent_agreement == pytest.approx(percent_agreement(a, b))
    assert rep.primary_pair == ("가", "나") and rep.confusion["APPROVE"]["APPROVE"] == 8
    assert any("표본 부족" in w for w in rep.overall.warnings)             # 20건 < 30건


def test_three_labelers_use_fleiss_on_items_all_three_labeled():
    out = []
    pattern = [(A, A, A), (A, A, R), (R, R, R), (X, X, R), (X, X, X), (A, R, A)] * 6
    for i, (x, y, z) in enumerate(pattern):
        out += [ann(f"it{i}", "가", x), ann(f"it{i}", "나", y), ann(f"it{i}", "다", z)]
    out.append(ann("extra", "가", A))                   # 혼자 라벨한 항목은 쓰지 않는다
    rep = agreement_report(out)
    assert rep.overall.method == "fleiss" and rep.overall.n_raters == 3 and rep.overall.n_items == 36
    expected = fleiss_kappa_from_ratings([[x, y, z] for x, y, z in pattern])
    assert rep.overall.kappa == pytest.approx(expected)
    assert len(rep.pairs) == 3 and any("3명이 모두 라벨한 36건" in w for w in rep.overall.warnings)


def test_duplicate_labels_by_the_same_person_are_an_error_in_the_collector():
    with pytest.raises(ValueError):
        collect_ratings([ann("it1", "가", A), ann("it1", "가", R)])


def test_per_rule_agreement_counts_who_picked_each_rule():
    items = [("it1", ["FIN-GUARANTEE-01"], ["FIN-GUARANTEE-01"]), ("it2", ["FIN-GUARANTEE-01"], ["FIN-RETURN-01"]),
             ("it3", ["FIN-RETURN-01"], ["FIN-RETURN-01"]), ("it4", ["FIN-GUARANTEE-01"], ["FIN-GUARANTEE-01", "FIN-RETURN-01"])]
    out = []
    for iid, ra, rb in items:
        out += [ann(iid, "가", "REJECT", rules=ra), ann(iid, "나", "REJECT", rules=rb)]
    rows = {r.rule_id: r for r in per_rule_agreement(out)}
    g, r = rows["FIN-GUARANTEE-01"], rows["FIN-RETURN-01"]
    # 보장 규칙: 둘 다 it1, it4 / 가만 it2 / 둘 다 안 고른 it3
    assert (g.both, g.only_first, g.only_second, g.neither, g.n_items) == (2, 1, 0, 1, 4) and g.percent == pytest.approx(0.75)
    # 수익 규칙: 둘 다 it3 / 나만 it2, it4 / 가만 0 / 둘 다 안 고른 it1
    assert (r.both, r.only_first, r.only_second, r.neither) == (1, 0, 2, 1)
    assert g.kappa == pytest.approx(cohens_kappa([True, True, False, True], [True, False, False, True]))


def test_confusion_matrix_orientation_and_totals():
    a, b = [A, A, R, X], [A, R, R, A]
    m = confusion_matrix(a, b)
    assert m["APPROVE"]["REVISE"] == 1 and m["REJECT"]["APPROVE"] == 1 and sum(sum(r.values()) for r in m.values()) == 4
