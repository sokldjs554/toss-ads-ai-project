"""라벨(Annotation) 검증 — 인용 존재, 판정·규칙 일관성, 이름 규칙, 너무 빠른 제출, 조정 기록, 확정 라벨 계산.

사람 입력도 모델 출력처럼 믿지 않는다: 인용이 광고 문구에 글자 그대로 없으면 받지 않는다.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ops.labeling import (
    AdjudicationRecord,
    Annotation,
    AnnotationRejected,
    LabelDecision,
    check_annotation,
    ensure_annotation,
    fast_threshold_seconds,
    locate_span,
    resolve_final,
    validate_annotator_name,
)
from tests.test_labeling_common import ad, ann


def mk(**kw) -> Annotation:
    base = dict(item_id="rq_1", annotator="가나", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], rationale="이유", seconds_spent=30.0)
    base.update(kw)
    return Annotation(**base)


# --- 인용 존재 검증 ---------------------------------------------------------------------------------
def test_a_citation_that_is_in_the_copy_is_accepted_and_located():
    copy = ad()
    res = ensure_annotation(mk(), copy)
    assert res.ok and res.located["원금 보장"][0] == "headline"


def test_a_citation_that_is_not_in_the_copy_is_rejected_with_a_plain_message():
    copy = ad()
    with pytest.raises(AnnotationRejected) as e:
        ensure_annotation(mk(cited_spans=["원금 100% 보장"]), copy)
    assert "광고 문구에 그대로 없어" in str(e.value) and "원금 100% 보장" in str(e.value)


def test_citation_must_match_letter_for_letter_not_after_normalisation():
    copy = ad(headline="원금보장 투자")                       # 공백 없는 표기
    assert locate_span(copy, "원금 보장") is None              # 띄어 쓴 인용은 같은 글자가 아니다
    assert locate_span(copy, "원금보장") is not None


def test_a_citation_cannot_span_two_fields():
    copy = ad(headline="원금 보장 투자", body="월 3% 확정 수익")
    assert locate_span(copy, "투자\n월 3%") is None            # 제목 끝 + 본문 앞을 이어 붙인 문장은 광고에 없다
    assert locate_span(copy, "투자 월 3%") is None


def test_citations_in_disclosures_are_found():
    assert locate_span(ad(), "원금 손실 가능성")[0] == "disc0"


def test_one_character_citations_are_refused_because_they_prove_nothing():
    with pytest.raises(ValidationError) as e:
        mk(cited_spans=["원"])
    assert "2자 이상" in str(e.value)


def test_citation_limits():
    with pytest.raises(ValidationError):
        mk(cited_spans=["가" * 201])
    with pytest.raises(ValidationError):
        mk(cited_spans=[f"원금 보장{i}" for i in range(21)])


# --- 판정과 규칙의 일관성 -------------------------------------------------------------------------------
def test_flagging_without_a_rule_is_refused():
    for dec in ("REVISE", "REJECT"):
        with pytest.raises(ValidationError) as e:
            mk(decision=dec, rule_ids=[])
        assert "규칙을 하나 이상" in str(e.value)


def test_flagging_without_a_citation_is_refused():
    with pytest.raises(ValidationError) as e:
        mk(cited_spans=[])
    assert "인용" in str(e.value)


def test_approve_with_a_block_level_rule_is_refused():
    with pytest.raises(ValidationError) as e:
        mk(decision="APPROVE", rule_ids=["FIN-GUARANTEE-01"], cited_spans=[])
    assert "게재 불가 등급 규칙" in str(e.value)


def test_approve_with_a_fix_level_rule_is_accepted_with_a_warning():
    a = mk(decision="APPROVE", rule_ids=["TA-URGENCY-01"], cited_spans=[])
    res = check_annotation(a, ad())
    assert res.ok and any("수정 필요 등급" in w for w in res.warnings)


def test_unknown_rule_ids_are_refused():
    with pytest.raises(ValidationError) as e:
        mk(rule_ids=["NOPE-01"])
    assert "알 수 없는 규칙" in str(e.value)


def test_reject_without_any_block_rule_warns_but_is_accepted():
    a = mk(decision="REJECT", rule_ids=["TA-URGENCY-01"], cited_spans=["월 3% 확정 수익"])
    res = check_annotation(a, ad())
    assert res.ok and any("게재 불가 등급 규칙이 하나도 없습니다" in w for w in res.warnings)


def test_revise_with_a_block_rule_warns():
    a = mk(decision="REVISE")
    assert any("게재 불가 등급 규칙이 들어 있습니다" in w for w in check_annotation(a, ad()).warnings)


def test_omission_rules_may_skip_the_citation_but_need_a_reason():
    ok = mk(decision="REVISE", rule_ids=["GRND-COND-01"], cited_spans=[], rationale="체험 후 자동결제 조건이 빠졌습니다")
    assert check_annotation(ok, ad()).ok
    with pytest.raises(ValidationError) as e:
        mk(decision="REVISE", rule_ids=["GRND-COND-01"], cited_spans=[], rationale="짧음")
    assert "이유를" in str(e.value)
    # 고지 누락 규칙도 같다
    assert check_annotation(mk(decision="REVISE", rule_ids=["FIN-DISCLOSE-LOAN-01"], cited_spans=[], rationale="연체·상환 안내가 모두 없음"), ad()).ok


def test_mixing_an_omission_rule_with_a_quote_rule_still_needs_a_citation():
    with pytest.raises(ValidationError):
        mk(decision="REVISE", rule_ids=["GRND-COND-01", "TA-URGENCY-01"], cited_spans=[], rationale="조건이 빠졌고 긴급성 표현도 있음")


def test_rule_ids_and_citations_are_deduplicated_and_trimmed():
    a = mk(rule_ids=[" FIN-GUARANTEE-01", "FIN-GUARANTEE-01"], cited_spans=["  원금 보장 ", "원금 보장"])
    assert a.rule_ids == ["FIN-GUARANTEE-01"] and a.cited_spans == ["원금 보장"]


# --- 이름 -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", "pipeline", "System", "MODEL", "자동", "가" * 41])
def test_unusable_annotator_names_are_refused(bad):
    """자동 처리용 이름으로는 사람 라벨을 만들 수 없다 — 사전 라벨이 사람 라벨로 둔갑하는 길을 막는다."""
    with pytest.raises(ValueError):
        validate_annotator_name(bad)
    with pytest.raises(ValidationError):
        mk(annotator=bad)


def test_names_are_normalised():
    assert validate_annotator_name("  홍​길동   님 ") == "홍길동 님"


# --- 너무 빠른 제출 -------------------------------------------------------------------------------------
def test_a_submission_faster_than_the_assumed_reading_time_is_flagged_not_rejected():
    copy = ad()
    limit = fast_threshold_seconds(copy)
    assert limit >= 3.0 and limit == pytest.approx(max(3.0, len(copy.full_text()) / 20.0))
    fast = check_annotation(mk(seconds_spent=limit - 0.5), copy)
    assert fast.ok and "too_fast" in fast.flags and any("매우 빨랐습니다" in w for w in fast.warnings)
    assert "too_fast" not in check_annotation(mk(seconds_spent=limit + 0.5), copy).flags


def test_unknown_time_is_not_flagged_by_the_check():
    assert "too_fast" not in check_annotation(mk(seconds_spent=None), ad()).flags


# --- 조정 기록 ----------------------------------------------------------------------------------------
def test_adjudication_needs_a_reason_and_the_same_consistency_rules():
    with pytest.raises(ValidationError) as e:
        AdjudicationRecord(item_id="rq_1", adjudicator="조정자", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], reason="짧다")
    assert "조정 이유" in str(e.value)
    with pytest.raises(ValidationError):
        AdjudicationRecord(item_id="rq_1", adjudicator="조정자", decision="APPROVE", rule_ids=["FIN-GUARANTEE-01"], reason="게재 불가 규칙을 골랐는데 통과")
    with pytest.raises(ValidationError):
        AdjudicationRecord(item_id="rq_1", adjudicator="pipeline", decision="APPROVE", reason="자동 이름으로는 조정할 수 없음")


# --- 확정 라벨 ----------------------------------------------------------------------------------------
def test_agreeing_labels_are_final_and_use_the_common_rules():
    a = ann("rq_1", "가", "REJECT", rules=["FIN-GUARANTEE-01", "FIN-RETURN-01"], spans=["원금 보장", "확정 수익"])
    b = ann("rq_1", "나", "REJECT", rules=["FIN-GUARANTEE-01"], spans=["원금 보장"])
    f = resolve_final([a, b], None, 2)
    assert f.decision == LabelDecision.REJECT and f.rule_ids == ["FIN-GUARANTEE-01"] and not f.adjudicated and not f.rules_unresolved
    assert f.agreement == 1.0 and f.rule_agreement == pytest.approx(0.5) and f.labelers == ["가", "나"]


def test_agreeing_labels_with_no_common_rule_use_the_union_and_say_so():
    a = ann("rq_1", "가", "REVISE", rules=["GRND-COND-01"], spans=[], why="조건이 빠졌습니다 (체험 후 결제)")
    b = ann("rq_1", "나", "REVISE", rules=["TA-OMISSION-01"], spans=[], why="혜택 조건 생략입니다 (결제)")
    f = resolve_final([a, b], None, 2)
    assert set(f.rule_ids) == {"GRND-COND-01", "TA-OMISSION-01"} and f.rules_unresolved


def test_disagreeing_labels_are_not_final_until_adjudicated():
    a, b = ann("rq_1", "가", "REJECT"), ann("rq_1", "나", "APPROVE")
    assert resolve_final([a, b], None, 2) is None
    adj = AdjudicationRecord(item_id="rq_1", adjudicator="다", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], reason="원금 보장 표현은 게재 불가 등급이다")
    f = resolve_final([a, b], adj, 2)
    assert f.adjudicated and f.decision == LabelDecision.REJECT and f.agreement == 0.0 and f.adjudicator == "다"


def test_too_few_labels_are_not_final_and_a_single_labeler_is_marked():
    a = ann("rq_1", "가", "REJECT")
    assert resolve_final([a], None, 2) is None
    f = resolve_final([a], None, 1)
    assert f.single_labeler and f.agreement is None


def test_flags_and_prelabel_fields_have_safe_defaults():
    a = mk()
    assert a.prelabel_mode == "none" and a.prelabel_decision is None and a.flags == [] and a.guideline_version == "1.0"
