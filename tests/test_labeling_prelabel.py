"""사전 라벨 — 업종 분류기(결정론 키워드)와 그 평가, 제안 라벨(자동 확정 금지·숨김/표시 기록), 앵커링 보고.

업종 분류 정확도는 '기존 데이터셋의 category 칸을 얼마나 맞히는가' 이다. 단서(LEXICON_VERSION)를 바꾸면 아래 숫자와 docs/LABELING.md 의 설명을 함께 갱신한다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.schemas import ProductCategory
from ops import prelabel as pl
from ops.labeling import Annotation, LabelDecision
from ops.review_queue import ReviewQueue, VerdictSnapshot
from tests.test_labeling_common import ad, ann, make_queue

DATASETS = Path(__file__).resolve().parents[1] / "evals" / "datasets"
C = ProductCategory


# ═════════════════════════ 업종 분류기 ═════════════════════════
@pytest.mark.parametrize("text,expected", [
    ("직장인 신용대출 연 5.5%~18.0%, 한도 조회", C.LOAN),
    ("코스피200 인덱스 펀드, 총보수 연 0.15%", C.INVESTMENT),
    ("암 진단 시 최대 3,000만원 보험료 확인", C.INSURANCE),
    ("연회비 없는 체크카드 발급", C.CARD),
    ("파킹통장 연 3.0% 예금자 보호", C.SAVINGS),
    ("OO코인 에어드랍 가상자산 거래소", C.CRYPTO),
    ("전 상품 쿠폰 할인 무료 배송 쇼핑", C.ECOMMERCE),
    ("앱 설치하고 7일 무료 체험, 로그인 없이 구독", C.APP),
    ("자격증 취득 강의 수강 신청 커리큘럼", C.EDUCATION),
    ("비타민 영양제 건강 다이어트", C.HEALTH),
    ("신축 아파트 분양 청약 안내", C.REALESTATE),
])
def test_each_category_is_recognised_from_its_own_vocabulary(text, expected):
    cat, conf, evidence = pl.classify_category(text, [])
    assert cat == expected and conf > pl.OTHER_CONFIDENCE and evidence, (cat, conf, evidence)


def test_no_clue_means_other_with_the_unknown_confidence():
    assert pl.classify_category("안녕하세요 오늘도 좋은 하루", []) == (C.OTHER, pl.OTHER_CONFIDENCE, [])
    assert pl.classify_category("", []) == (C.OTHER, pl.OTHER_CONFIDENCE, [])


def test_classification_is_deterministic():
    runs = {repr(pl.classify_category("대출 금리 한도 신청", [{"id": "F1", "text": "연 5.5%"}])) for _ in range(5)}
    assert len(runs) == 1


def test_facts_are_read_too_and_string_facts_are_accepted():
    assert pl.classify_category("지금 확인하세요", [{"id": "F1", "text": "신용대출 한도 5,000만원"}])[0] == C.LOAN
    assert pl.classify_category("지금 확인하세요", ["보험료 월 9,900원 특약 포함"])[0] == C.INSURANCE


def test_spaced_out_and_wide_character_evasions_are_folded_before_matching():
    assert pl.classify_category("대 출 한 도 조 회", [])[0] == C.LOAN
    assert pl.classify_category("신용 대 출", [])[0] == C.LOAN
    assert pl.classify_category("펀​드 수익률", [])[0] == C.INVESTMENT


def test_a_single_weak_clue_never_makes_an_ad_financial():
    """'심사' 한 낱말(가중치 1)만으로 대출이라고 하면 입점 심사·심사 결과 안내까지 금융으로 오인한다 — 금융 업종은 강한 단서(≥2)가 있어야 한다."""
    cat, conf, _ = pl.classify_category("입점 혜택 수수료 3%, 심사 없이 바로 적용", [])
    assert cat not in (C.LOAN, C.CARD, C.INSURANCE, C.INVESTMENT, C.SAVINGS, C.CRYPTO)
    assert pl.classify_category("금리", [])[0] == C.OTHER
    assert pl.classify_category("대출 금리", [])[0] == C.LOAN          # 강한 단서(대출) + 약한 단서(금리)


def test_confidence_grows_with_dominance():
    one = pl.classify_category("대출", [])[1]
    many = pl.classify_category("대출 대환 상환 원리금 신용대출", [])[1]
    contested = pl.classify_category("대출 보험", [])[1]
    assert many > one > contested > pl.OTHER_CONFIDENCE


def test_financial_clues_come_from_the_policy_catalog_the_input_guard_uses():
    lex = pl._lexicon()
    from guardrails.policy import get_catalog

    catalog_cats = set(get_catalog().category_cues)
    for cat in catalog_cats:
        assert any(src == "policy" for _p, _w, src in lex[cat]), cat
    assert catalog_cats == {"loan", "investment", "crypto", "insurance", "savings"}


def test_importing_the_module_builds_no_lexicon():
    import importlib

    import ops.prelabel as mod

    mod._COMPILED = None
    importlib.reload(mod)
    assert mod._COMPILED is None              # 처음 부를 때 한 번 만든다 — import 시점에는 만들지 않는다


# ═════════════════════════ 평가 (기존 데이터셋의 category 칸) ═════════════════════════
def test_the_evaluation_numbers_are_the_documented_ones():
    ev = pl.evaluate_categories()
    by = {s.name: s for s in ev.scores}
    assert (by["golden"].correct, by["golden"].n) == (51, 55)
    assert (by["holdout"].correct, by["holdout"].n) == (10, 12)
    dev = ev.pooled(pl.DEV_DATASETS)
    assert (dev.correct, dev.n) == (204, 284)
    assert ev.lexicon_version == pl.LEXICON_VERSION == "1.1"


def test_accuracy_is_reported_honestly_with_a_baseline_and_per_dataset():
    ev = pl.evaluate_categories(["golden", "conditions_extra"])
    g, ce = ev.scores
    assert g.accuracy > g.baseline * 3                                      # 골든셋: 최빈 업종(25%)보다 훨씬 낫다
    assert ce.accuracy < ce.baseline                                        # 조건 검사용 자리표시 category(대부분 ecommerce)에서는 최빈값보다 못하다 — 숨기지 않는다
    assert ce.confusion["ecommerce"]["other"] > 0 and ce.n == 174
    out = pl.format_eval(ev)
    assert "최빈 업종만 찍는 기준선" in out and "혼동" in out and "golden" in out and "conditions_extra" in out


def test_confusion_and_recall_are_computed_from_the_rows():
    rows = [{"category": "loan", "headline": "신용대출 한도", "body": "", "cta": ""}, {"category": "loan", "headline": "안녕하세요", "body": "", "cta": ""},
            {"category": "app", "headline": "앱 설치 다운로드", "body": "", "cta": ""}]
    s = pl.score_rows("t", rows)
    assert (s.correct, s.n) == (2, 3) and s.confusion["loan"] == {"loan": 1, "other": 1} and s.per_category == {"loan": (1, 2), "app": (1, 1)}
    assert s.baseline_correct == 2 and s.macro_recall == pytest.approx(0.75)


def test_evaluation_only_reads_the_datasets():
    def digest():
        names = ["golden", "holdout", "adversarial", "conditions", "conditions_extra", "contrast_g069", "redteam_seeds"]
        return {n: hashlib.sha256((DATASETS / f"{n}.jsonl").read_bytes()).hexdigest() for n in names}

    before = digest()
    pl.evaluate_categories()
    assert digest() == before


def test_cli_eval_prints_a_report(capsys):
    assert pl.main(["--eval", "--datasets", "golden"]) == 0
    out = capsys.readouterr().out
    assert "업종 분류기(단서 v1.1)" in out and "golden" in out and "0.927" in out
    assert pl.main([]) == 2


# ═════════════════════════ 사전 라벨 ═════════════════════════
def item_with_suggestion(q: ReviewQueue, n: int = 1):
    v = VerdictSnapshot(final_decision="HUMAN_REVIEW", model_decision="REVISE", code_decision="REVISE", confidence=0.55, run_mode="demo",
                        findings=[{"rule_id": "TA-URGENCY-01", "severity": "fix", "layer": "lexical", "quote": "손실 없음", "verified_quote": True},
                                  {"rule_id": "TA-ENDORSE-01", "severity": "warn", "layer": "lexical", "quote": "확정 수익", "verified_quote": True},
                                  {"rule_id": "FIN-RETURN-01", "severity": "block", "layer": "semantic", "quote": "없는 구절", "verified_quote": False}])
    return q.enqueue_ad(ad(headline=f"원금 보장 투자 {n}호"), dataset_id=f"P-{n}", category="investment", verdict=v)


def test_shown_prelabel_carries_the_suggestion_but_it_is_not_a_label():
    q, _ = make_queue()
    it = item_with_suggestion(q)
    p = pl.prelabel(it, hide=False)
    assert p.available and not p.hidden and p.decision == LabelDecision.REVISE and p.mode == "shown"
    assert p.rule_ids == ("TA-URGENCY-01",) and p.cited_spans == ("손실 없음",)         # 참고(warn)와 검증되지 않은 인용은 제안하지 않는다
    assert p.auto_confirmed is False and "확정이 아닙니다" in p.note
    assert not isinstance(p, Annotation)
    with pytest.raises(AttributeError):
        p.decision = LabelDecision.APPROVE                                              # type: ignore[misc]


def test_hidden_prelabel_reveals_nothing_but_remembers_that_a_suggestion_existed():
    q, _ = make_queue()
    it = item_with_suggestion(q)
    p = pl.prelabel(it, hide=True)
    assert p.available and p.hidden and p.decision is None and p.rule_ids == () and p.cited_spans == () and p.confidence is None and p.mode == "hidden"
    assert pl.prelabel(it, pl.PrelabelSettings(hide=True)).mode == "hidden" and pl.prelabel(it).hidden is True        # 기본은 숨김
    assert pl.prelabel(it, pl.PrelabelSettings(hide=False)).mode == "shown"
    assert pl.form_defaults(p) == {"decision": None, "rule_ids": [], "cited_spans": []}


def test_no_suggestion_when_the_item_has_no_usable_verdict():
    q, _ = make_queue()
    it = q.enqueue_ad(ad(), dataset_id="P-0", category="investment", verdict=VerdictSnapshot(final_decision="HUMAN_REVIEW", model_decision="HUMAN_REVIEW", code_decision=""))
    p = pl.prelabel(it, hide=False)
    assert not p.available and p.mode == "none" and p.decision is None


def test_a_prelabel_can_never_become_a_label_without_a_human_annotation():
    q, _ = make_queue()
    it = item_with_suggestion(q)
    q.claim_item("가", it.id, hide_prelabel=False)
    with pytest.raises(TypeError):
        q.submit(pl.prelabel(it, hide=False))                                          # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Annotation(item_id=it.id, annotator="pipeline", decision="REVISE", rule_ids=["TA-URGENCY-01"], cited_spans=["손실 없음"])   # 자동 이름으로는 사람 라벨을 만들 수 없다
    assert q.get(it.id).annotations == [] and not hasattr(pl, "submit")
    assert not any(n for n in dir(pl) if "auto_label" in n or n == "confirm")


def test_the_queue_records_whether_the_suggestion_was_shown_or_hidden_and_what_it_said():
    q, _ = make_queue()
    a, b = item_with_suggestion(q, 1), item_with_suggestion(q, 2)
    va = q.claim_item("가", a.id, hide_prelabel=True)
    vb = q.claim_item("가", b.id, hide_prelabel=False)
    assert va.prelabel is None and va.prelabel_mode == "hidden" and vb.prelabel.decision == LabelDecision.REVISE and vb.prelabel_mode == "shown"
    q.submit(ann(a.id, "가", "REVISE", rules=["TA-URGENCY-01"], spans=["확정 수익"], prelabel_mode="hidden"))
    # 호출자가 제안 판정을 거짓으로 적어 와도 큐가 항목의 실제 제안으로 덮어쓴다
    q.submit(ann(b.id, "가", "REVISE", rules=["TA-URGENCY-01"], spans=["확정 수익"], prelabel_mode="shown", prelabel_decision=LabelDecision.REJECT))
    sa, sb = q.get(a.id).annotations[0], q.get(b.id).annotations[0]
    assert (sa.prelabel_mode, sa.prelabel_decision) == ("hidden", LabelDecision.REVISE) and (sb.prelabel_mode, sb.prelabel_decision) == ("shown", LabelDecision.REVISE)


def test_the_mode_is_none_when_the_item_had_no_suggestion():
    q, _ = make_queue()
    it = q.enqueue_ad(ad(), dataset_id="P-0", category="investment", verdict=VerdictSnapshot(model_decision="HUMAN_REVIEW", code_decision=""))
    q.claim_item("가", it.id)
    q.submit(ann(it.id, "가", prelabel_mode="shown"))
    assert q.get(it.id).annotations[0].prelabel_mode == "none"


# ═════════════════════════ 앵커링 ═════════════════════════
def prelabeled(n: int, mode: str, agree: int, who: str) -> list[Annotation]:
    out = []
    for i in range(n):
        same = i < agree
        out.append(Annotation(item_id=f"{mode}{i}", annotator=who, decision=LabelDecision.REVISE if same else LabelDecision.APPROVE, rule_ids=["TA-URGENCY-01"] if same else [],
                              cited_spans=["손실 없음"] if same else [], prelabel_mode=mode, prelabel_decision=LabelDecision.REVISE))
    return out


def test_anchoring_report_compares_agreement_with_the_suggestion():
    rep = pl.anchoring_report(prelabeled(30, "shown", 27, "가") + prelabeled(30, "hidden", 18, "나"))
    assert (rep.n_shown, rep.n_hidden, rep.agree_shown, rep.agree_hidden) == (30, 30, 27, 18)
    assert rep.rate_shown == pytest.approx(0.9) and rep.rate_hidden == pytest.approx(0.6) and rep.difference == pytest.approx(0.3) and rep.sufficient and rep.warnings == []
    assert rep.ci_shown[0] < 0.9 < rep.ci_shown[1] and "증거가 아니고" in rep.note


def test_anchoring_report_refuses_to_compare_small_groups():
    rep = pl.anchoring_report(prelabeled(5, "shown", 5, "가") + prelabeled(4, "hidden", 2, "나"))
    assert not rep.sufficient and any("표본 부족" in w for w in rep.warnings) and rep.difference == pytest.approx(0.5)
    only = pl.anchoring_report(prelabeled(25, "shown", 20, "가"))
    assert only.difference is None and only.rate_hidden is None and any("모두 있어야" in w for w in only.warnings)


def test_labels_without_a_suggestion_are_left_out_of_the_anchoring_comparison():
    rows = prelabeled(3, "shown", 3, "가") + [Annotation(item_id="x", annotator="가", decision=LabelDecision.APPROVE, prelabel_mode="none")]
    rep = pl.anchoring_report(rows)
    assert rep.n_shown == 3 and rep.n_hidden == 0
