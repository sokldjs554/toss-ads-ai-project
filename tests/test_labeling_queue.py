"""검수 대기열 — 상태 전이, 점유 규칙(우선순위·SLA·중복 금지·상한·만료), 우선순위 단조성, 중복 적재, 검증용 항목 비노출, JSONL 저장.

시계는 가짜 시계, 모델 호출 없음. 대기열은 세션 단위(락 없음)라는 가정을 쓴다.
"""
from __future__ import annotations

import json
from datetime import timedelta
from itertools import product

import pytest

from agents.pipeline import RunOptions, run_pipeline
from core.config import Settings
from core.schemas import AdCopy, Claim, Decision, Fact
from observability.tracer import Tracer
from ops.labeling import AdjudicationRecord, AnnotationRejected, LabelDecision
from ops.review_queue import (
    PRIORITY_WEIGHTS,
    TRANSITIONS,
    ClaimLimitReached,
    DuplicateAnnotation,
    DuplicateItem,
    IllegalTransition,
    ItemState,
    NotAllowed,
    NotClaimed,
    QueueFormatError,
    QueueReason,
    ReviewQueue,
    UnknownItem,
    compute_priority,
    reasons_for,
    sla_hours,
)
from tests.test_labeling_common import add_item, ann, label, make_queue, snap
from ui.presets import PRESETS, build_brief

S = ItemState


# ═════════════════════════ 상태 전이 ═════════════════════════
def test_the_happy_path_walks_every_state_in_order():
    q, clock = make_queue()
    it = add_item(q, 1)
    assert it.state == S.QUEUED
    q.claim_item("가", it.id)
    assert q.get(it.id).state == S.CLAIMED
    q.submit(ann(it.id, "가", "REJECT"))
    assert q.get(it.id).state == S.QUEUED                          # 두 번째 라벨을 기다린다
    label(q, "나", it.id, "APPROVE", rules=[], spans=[], why="문제 없음")
    assert q.get(it.id).state == S.DISAGREED
    q.adjudicate(AdjudicationRecord(item_id=it.id, adjudicator="다", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], reason="원금 보장 표현은 게재 불가"))
    assert q.get(it.id).state == S.ADJUDICATED
    q.mark_exported([it.id])
    assert q.get(it.id).state == S.EXPORTED
    assert [h.to_state for h in q.get(it.id).history] == ["claimed", "queued", "claimed", "disagreed", "adjudicated", "exported"]


def test_agreeing_labels_end_in_labeled():
    q, _ = make_queue()
    it = add_item(q, 1)
    label(q, "가", it.id)
    res = label(q, "나", it.id)
    assert res.state == S.LABELED and res.final.decision == LabelDecision.REJECT and not res.final.adjudicated


def test_one_label_mode_confirms_immediately_and_marks_it_single():
    q, _ = make_queue(required=1)
    it = add_item(q, 1)
    res = label(q, "가", it.id)
    assert res.state == S.LABELED and res.final.single_labeler


def test_with_three_required_labels_any_difference_goes_to_adjudication():
    q, _ = make_queue(required=3)
    it = add_item(q, 1)
    label(q, "가", it.id)
    label(q, "나", it.id)
    assert q.get(it.id).state == S.QUEUED
    label(q, "다", it.id, "REVISE", rules=["TA-URGENCY-01"], spans=["월 3% 확정 수익"])
    assert q.get(it.id).state == S.DISAGREED


ILLEGAL = [
    ("export a queued item", lambda q, i: q.mark_exported([i.id])),
    ("skip then adjudicate", lambda q, i: (q.skip(i.id), q.adjudicate(AdjudicationRecord(item_id=i.id, adjudicator="다", decision="APPROVE", reason="판정이 갈리지도 않았는데 조정")))),
    ("reopen an item that is not skipped", lambda q, i: q.reopen(i.id)),
]


@pytest.mark.parametrize("name,act", ILLEGAL, ids=[n for n, _ in ILLEGAL])
def test_illegal_transitions_raise(name, act):
    q, _ = make_queue()
    it = add_item(q, 1)
    with pytest.raises(IllegalTransition):
        act(q, it)


def test_a_finished_item_cannot_be_claimed_skipped_adjudicated_or_labeled_again():
    q, _ = make_queue()
    it = add_item(q, 1)
    label(q, "가", it.id)
    label(q, "나", it.id)                                          # labeled
    with pytest.raises(IllegalTransition):
        q.claim_item("다", it.id)
    with pytest.raises(IllegalTransition):
        q.skip(it.id)
    with pytest.raises(IllegalTransition):
        q.adjudicate(AdjudicationRecord(item_id=it.id, adjudicator="다", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], reason="이미 일치한 항목은 조정 대상이 아님"))
    q.mark_exported([it.id])
    for act in (lambda: q.mark_exported([it.id]), lambda: q.claim_item("다", it.id), lambda: q.reopen(it.id)):
        with pytest.raises(IllegalTransition):
            act()


def test_the_transition_table_has_no_way_out_of_exported_and_only_skipped_reopens():
    assert set(TRANSITIONS) == set(ItemState)
    assert TRANSITIONS[S.EXPORTED] == set()
    assert [s for s, to in TRANSITIONS.items() if S.QUEUED in to and s != S.CLAIMED] == [S.SKIPPED]
    assert TRANSITIONS[S.DISAGREED] == {S.ADJUDICATED} and TRANSITIONS[S.LABELED] == {S.EXPORTED}


def test_skip_and_reopen():
    q, _ = make_queue()
    it = add_item(q, 1)
    q.claim_item("가", it.id)
    q.skip(it.id, actor="관리자", reason="광고주가 철회")
    assert q.get(it.id).state == S.SKIPPED and q.get(it.id).claims == [] and q.list_open() == []
    q.reopen(it.id)
    assert q.get(it.id).state == S.QUEUED and len(q.list_open()) == 1


def test_adjudicator_who_labeled_the_item_is_refused():
    q, _ = make_queue()
    it = add_item(q, 1)
    label(q, "가", it.id, "REJECT")
    label(q, "나", it.id, "APPROVE", rules=[], spans=[], why="문제 없음")
    with pytest.raises(NotAllowed):
        q.adjudicate(AdjudicationRecord(item_id=it.id, adjudicator="가", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], reason="내가 라벨한 항목을 내가 조정"))
    assert q.get(it.id).state == S.DISAGREED


def test_an_adjudication_citation_must_be_in_the_copy():
    q, _ = make_queue()
    it = add_item(q, 1)
    label(q, "가", it.id, "REJECT")
    label(q, "나", it.id, "APPROVE", rules=[], spans=[], why="문제 없음")
    with pytest.raises(AnnotationRejected):
        q.adjudicate(AdjudicationRecord(item_id=it.id, adjudicator="다", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["없는 구절입니다"], reason="인용이 문구에 없음"))


def test_unknown_item_and_non_annotation_inputs():
    q, _ = make_queue()
    with pytest.raises(UnknownItem):
        q.get("rq_nope")
    with pytest.raises(UnknownItem):
        q.claim_item("가", "rq_nope")
    it = add_item(q, 1)
    q.claim_item("가", it.id)
    with pytest.raises(TypeError):
        q.submit({"item_id": it.id})                                # type: ignore[arg-type]


# ═════════════════════════ 점유 규칙 ═════════════════════════
def test_submitting_without_a_claim_is_refused():
    q, _ = make_queue()
    it = add_item(q, 1)
    with pytest.raises(NotClaimed):
        q.submit(ann(it.id, "가"))


def test_a_rejected_annotation_keeps_the_claim_so_it_can_be_fixed():
    q, _ = make_queue()
    it = add_item(q, 1)
    q.claim_item("가", it.id)
    with pytest.raises(AnnotationRejected):
        q.submit(ann(it.id, "가", spans=["문구에 없는 구절"]))
    assert q.held_by("가") == [it.id] and q.get(it.id).annotations == []
    q.submit(ann(it.id, "가"))
    assert q.held_by("가") == []


def test_the_same_person_can_never_label_the_same_ad_twice():
    q, _ = make_queue()
    it = add_item(q, 1)
    label(q, "가", it.id)
    with pytest.raises(DuplicateAnnotation):
        q.claim_item("가", it.id)
    with pytest.raises(DuplicateAnnotation):
        q.submit(ann(it.id, "가", "APPROVE", rules=[], spans=[], why="다시 낸 라벨"))
    assert q.claim("가") is None                                      # 자동 배정도 이 사람에게 이 항목을 주지 않는다
    assert len(q.get(it.id).annotations) == 1


def test_auto_claim_never_hands_out_an_ad_the_person_already_labeled():
    q, _ = make_queue()
    items = [add_item(q, i, conf=0.2 + i / 10) for i in range(3)]
    seen: list[str] = []
    for _ in range(3):
        v = q.claim("가")
        seen.append(v.item_id)
        q.submit(ann(v.item_id, "가"))
    assert sorted(seen) == sorted(i.id for i in items) and q.claim("가") is None
    assert q.claim("나") is not None                                    # 다른 사람에게는 같은 광고가 나간다


def test_an_ad_is_not_handed_to_more_people_than_it_still_needs():
    q, _ = make_queue()
    it = add_item(q, 1)
    q.claim_item("가", it.id)
    q.claim_item("나", it.id)                                           # 필요한 라벨 2개 = 점유 2명
    assert q.claim("다") is None
    with pytest.raises(IllegalTransition):
        q.claim_item("다", it.id)


def test_claim_limit_per_person_and_release_frees_a_slot():
    q, _ = make_queue(max_concurrent_claims=3)
    for i in range(5):
        add_item(q, i, conf=0.1 * (i + 1))
    got = [q.claim("가").item_id for _ in range(3)]
    with pytest.raises(ClaimLimitReached):
        q.claim("가")
    q.release("가", got[0])
    assert q.claim("가") is not None


def test_release_requires_holding_the_claim():
    q, _ = make_queue()
    it = add_item(q, 1)
    with pytest.raises(NotClaimed):
        q.release("가", it.id)


def test_expired_claims_are_reclaimed_and_a_late_submit_is_refused():
    q, clock = make_queue()
    it = add_item(q, 1)
    q.claim_item("가", it.id)
    clock.advance(minutes=29)
    assert q.reap_expired() == []
    clock.advance(minutes=2)
    assert q.reap_expired() == [(it.id, "가")]
    assert q.get(it.id).state == S.QUEUED and q.get(it.id).claims == []
    with pytest.raises(NotClaimed):
        q.submit(ann(it.id, "가"))
    assert q.claim("나") is not None                                    # 다른 사람이 받을 수 있다


def test_expiry_keeps_the_other_claimants():
    q, clock = make_queue()
    it = add_item(q, 1)
    q.claim_item("가", it.id)
    clock.advance(minutes=20)
    q.claim_item("나", it.id)
    clock.advance(minutes=15)                                           # 가: 35분(만료) · 나: 15분
    q.reap_expired()
    assert [c.annotator for c in q.get(it.id).claims] == ["나"] and q.get(it.id).state == S.CLAIMED


def test_claims_come_in_priority_order():
    q, _ = make_queue()
    low = add_item(q, 1, conf=0.95, category="ecommerce")
    high = add_item(q, 2, conf=0.2, category="loan", disagreement=True)
    mid = add_item(q, 3, conf=0.6, category="app")
    order = []
    for _ in range(3):
        v = q.claim("가")
        order.append(v.item_id)
        q.submit(ann(v.item_id, "가"))
    assert order == [high.id, mid.id, low.id]
    assert q.get(high.id).priority > q.get(mid.id).priority > q.get(low.id).priority


def test_an_ad_near_its_deadline_jumps_ahead_of_higher_priority_ones():
    q, clock = make_queue()
    old_low = add_item(q, 1, conf=0.95, category="ecommerce")           # 낮은 우선순위 → 마감 72시간
    assert q.get(old_low.id).due_at - clock() == timedelta(hours=72)
    clock.advance(hours=71, minutes=30)                                 # 마감까지 30분
    fresh_high = add_item(q, 2, conf=0.1, category="loan", disagreement=True)
    assert q.get(fresh_high.id).priority > q.get(old_low.id).priority
    assert q.claim("가").item_id == old_low.id                          # 마감 임박이 먼저
    assert q.claim("가").item_id == fresh_high.id


def test_overdue_items_are_served_first_and_counted_as_breaches():
    q, clock = make_queue()
    a = add_item(q, 1, conf=0.9, category="app")
    clock.advance(hours=100)
    b = add_item(q, 2, conf=0.05, category="loan", disagreement=True)
    assert q.stats().sla_breached == 1
    assert q.claim("가").item_id == a.id


def test_sla_tiers():
    assert [sla_hours(p) for p in (0.0, 0.29, 0.3, 0.59, 0.6, 1.0)] == [72, 72, 24, 24, 4, 4]


# ═════════════════════════ 우선순위 (불확실성 표집) ═════════════════════════
def test_priority_weights_sum_to_one_and_the_formula_is_the_documented_one():
    assert sum(PRIORITY_WEIGHTS.values()) == pytest.approx(1.0)
    p, parts = compute_priority(confidence=0.4, disagreement=True, categories=["loan"], conflict=True)
    assert parts == {"uncertainty": 0.27, "disagreement": 0.2, "risk": 0.2, "conflict": 0.15} and p == pytest.approx(0.82)
    assert compute_priority(confidence=1.0, disagreement=False, categories=["ecommerce"], conflict=False)[0] == pytest.approx(0.04)
    assert compute_priority(confidence=None, disagreement=False, categories=[], conflict=False)[0] == pytest.approx(0.045 * 5 + 0.02, abs=1e-4)   # 확신 정보 없음 = 0.5


def test_priority_is_monotone_in_confidence_and_bounded():
    """다른 조건이 같을 때 확신이 낮을수록 우선순위는 같거나 높다. 모든 조건 조합에서 확인한다."""
    grid = [i / 20 for i in range(21)]
    for dis, conflict, cats in product([False, True], [False, True], [["loan"], ["ecommerce"], ["crypto", "app"], []]):
        vals = [compute_priority(confidence=c, disagreement=dis, categories=cats, conflict=conflict)[0] for c in grid]
        assert all(0.0 <= v <= 1.0 for v in vals)
        assert all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)), (dis, conflict, cats, vals)
        assert vals[0] > vals[-1]


def test_each_uncertainty_signal_raises_priority():
    base = compute_priority(confidence=0.7, disagreement=False, categories=["ecommerce"], conflict=False)[0]
    assert compute_priority(confidence=0.5, disagreement=False, categories=["ecommerce"], conflict=False)[0] > base
    assert compute_priority(confidence=0.7, disagreement=True, categories=["ecommerce"], conflict=False)[0] > base
    assert compute_priority(confidence=0.7, disagreement=False, categories=["loan"], conflict=False)[0] > base
    assert compute_priority(confidence=0.7, disagreement=False, categories=["ecommerce"], conflict=True)[0] > base


def test_a_conflict_between_code_and_the_merged_decision_is_part_of_the_priority():
    q, _ = make_queue()
    same = q.enqueue_ad(add_item.__globals__["ad"](headline="가 광고"), dataset_id="S-1", category="app", verdict=snap(0.6, code="REVISE", model="REVISE"))
    diff = q.enqueue_ad(add_item.__globals__["ad"](headline="나 광고"), dataset_id="S-2", category="app", verdict=snap(0.6, code="APPROVE", model="REJECT"))
    assert diff.priority - same.priority == pytest.approx(PRIORITY_WEIGHTS["conflict"])
    assert diff.priority_parts["conflict"] == pytest.approx(0.15) and same.priority_parts["conflict"] == 0


def test_reason_tags_and_primary_reason():
    v = snap(0.9, disagreement=True, reason="두 모델 계열의 판정 불일치 (APPROVE vs REVISE)")
    tags = reasons_for(v, ["loan"], base=QueueReason.HUMAN_REVIEW)
    assert tags[0] == QueueReason.CROSSCHECK_DISAGREE and QueueReason.HIGH_RISK_CATEGORY in tags and QueueReason.HUMAN_REVIEW in tags
    low = reasons_for(snap(0.5, reason="심사 신뢰도 0.50 < 자동 판정 임계값 0.80"), ["app"], base=QueueReason.HUMAN_REVIEW)
    assert low[0] == QueueReason.LOW_CONFIDENCE
    failed = reasons_for(snap(0.0, model="HUMAN_REVIEW", reason="심사 모델 단계가 실패해 자동 판정을 하지 않음"), ["app"], base=QueueReason.HUMAN_REVIEW)
    assert QueueReason.LOW_CONFIDENCE not in failed          # 호출 실패는 낮은 확신이 아니다
    assert reasons_for(None, ["ecommerce"]) == [QueueReason.RANDOM_QC]


# ═════════════════════════ 적재 ═════════════════════════
def _hr_run(key: str = "loan_ok"):
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True, auto_decision_confidence=0.99)
    return run_pipeline(build_brief(PRESETS[key]), s, tracer=Tracer(), options=RunOptions(n_variants=3))


def test_only_human_review_reports_are_queued_and_never_twice():
    run = _hr_run()
    hr = [r for r in run.reports if r.final_decision == Decision.HUMAN_REVIEW]
    assert hr and len(hr) < len(run.reports)                    # 담당자 확인이 아닌 보고서가 섞여 있어야 의미 있는 검사다
    q, _ = make_queue()
    res = q.enqueue_from_run(run)
    assert len(res.added) == len(hr) and res.not_eligible == len(run.reports) - len(hr) and res.duplicates == []
    again = q.enqueue_from_run(run)
    assert again.added == [] and sorted(again.duplicates) == sorted(res.added) and len(q) == len(hr)
    with pytest.raises(DuplicateItem):
        q.add(q.get(res.added[0]).model_copy(deep=True))
    it = q.get(res.added[0])
    assert it.source.run_id == run.run_id and it.verdict.final_decision == "HUMAN_REVIEW" and it.verdict.run_mode == "demo"
    assert it.facts and it.categories == sorted(set(run.categories) | {run.brief.product.category.value}) and it.ad_copy.headline


def test_random_qc_sampling_is_deterministic_and_off_by_default():
    run = _hr_run()
    q, _ = make_queue()
    assert q.enqueue_from_run(run, qc_rate=0.0).not_eligible >= 1
    q2, _ = make_queue()
    q2.enqueue_from_run(run, qc_rate=1.0)
    assert len(q2) == len(run.reports) and any(i.queued_reason == QueueReason.RANDOM_QC for i in q2.items())
    q3, _ = make_queue()
    q3.enqueue_from_run(run, qc_rate=0.5, qc_seed="x")
    q4, _ = make_queue()
    q4.enqueue_from_run(run, qc_rate=0.5, qc_seed="x")
    assert sorted(i.id for i in q3.items()) == sorted(i.id for i in q4.items())


def test_the_snapshot_survives_changes_to_the_original_report():
    run = _hr_run()
    q, _ = make_queue()
    q.enqueue_from_run(run)
    it = q.items()[0]
    headline = it.ad_copy.headline
    run.reports[0].ad_copy.headline = "바뀐 제목"
    assert it.ad_copy.headline == headline


def test_long_external_text_is_capped():
    q, _ = make_queue()
    it = q.enqueue_ad(add_item.__globals__["ad"](headline="가" * 9000), dataset_id="L-1", category="app", landing_text="나" * 20000)
    assert len(it.ad_copy.headline) == 3000 and len(it.landing_text) == 5000


# ═════════════════════════ 검증용 항목 (gold) ═════════════════════════
GOLD_ROWS = [
    {"id": f"G-9{i}", "category": "loan", "headline": f"누구나 무조건 승인 {i}", "body": "신용 걱정 없이 5분 입금", "cta": "신청", "label": "REJECT", "rule_ids": ["FIN-APPROVAL-01"],
     "disclosures": [], "facts": [{"id": "F1", "text": "연 5.5%"}], "claims": [], "landing_text": ""} for i in range(4)
] + [{"id": "G-95", "category": "ecommerce", "headline": "전 상품 10% 할인", "body": "10월 31일까지, 1인 1회", "cta": "쇼핑하기", "label": "APPROVE", "rule_ids": [],
      "disclosures": ["10월 31일까지, 1인 1회"], "facts": [{"id": "F1", "text": "전 상품 10% 할인 (10월 31일까지, 1인 1회)"}], "claims": [], "landing_text": ""}]


def answer(view, who: str, *, correct: bool = True):
    """검증용 항목(GOLD_ROWS)에 대한 라벨. correct=False 면 일부러 반대로 답한다."""
    flag = "무조건 승인" in view.ad_copy.headline
    if flag == correct:
        span = "무조건 승인" if flag else view.ad_copy.headline            # 정답이 통과인데 반대로 답하는 경우엔 제목 전체를 인용한다
        return ann(view.item_id, who, "REJECT", rules=["FIN-APPROVAL-01"], spans=[span], why="무조건 승인 표현")
    return ann(view.item_id, who, "APPROVE", rules=[], spans=[], why="문제 없음")


def gold_queue(rate: float = 1.0):
    q, clock = make_queue(gold_rate=rate)
    assert q.enqueue_gold(GOLD_ROWS) == 5 and q.enqueue_gold(GOLD_ROWS) == 0         # 같은 id 는 다시 넣지 않는다
    for i in range(6):
        add_item(q, i, conf=0.3 + 0.1 * i)
    return q, clock


def test_gold_items_look_exactly_like_normal_items_to_the_labeler():
    q, _ = gold_queue(rate=1.0)
    view = q.claim("가")                                              # gold_rate=1 → 검증용이 나온다
    gold_ids = {i.id for i in q.items(include_gold=True) if i.is_gold}
    assert view.item_id in gold_ids
    q2, _ = gold_queue(rate=0.0)
    normal = q2.claim("가")
    assert normal.item_id not in {i.id for i in q2.items(include_gold=True) if i.is_gold}
    assert set(view.model_dump()) == set(normal.model_dump())          # 필드 구성이 같다
    dumped = view.model_dump_json()
    for needle in ("gold", "G-9", "APPROVE", "REJECT", "FIN-APPROVAL-01", "dataset", "queued_reason"):
        assert needle not in dumped, needle
    assert view.reason_label == normal.reason_label                                # 사유 문구도 같다
    assert view.ad_copy.variant_id.startswith("var_") and "G-9" not in view.ad_copy.variant_id and "G-9" not in view.product_name


@pytest.mark.parametrize("gold", [False, True])
def test_blinded_fact_ids_preserve_claim_links_without_leaking_or_mutating_sources(gold):
    q, _ = make_queue(gold_rate=1.0 if gold else 0.0)
    facts = [Fact(id="G-901-source-fact", text="전 상품 10% 할인"), Fact(id="F1", text="10월 31일까지")]
    copy = AdCopy(headline="전 상품 10% 할인", body="10월 31일까지", cta="쇼핑하기", claims=[
        Claim(text="전 상품 10% 할인", fact_id="G-901-source-fact"),
        Claim(text="10월 31일까지", fact_id="F1"),
        Claim(text="추가 혜택", fact_id="G-901-missing-fact"),
        Claim(text="배송 혜택", fact_id=None),
    ])
    if gold:
        q.enqueue_gold([{"id": "G-901", "category": "ecommerce", "label": "APPROVE", "rule_ids": [],
                         **copy.model_dump(), "facts": [f.model_dump() for f in facts]}])
        add_item(q, 1)
    else:
        q.enqueue_ad(copy, dataset_id="G-901", category="ecommerce", facts=facts)
    view = q.claim("가")
    original = q.get(view.item_id).model_dump(mode="json")
    assert [f.id for f in view.facts] == ["F1", "F2"]
    assert [c.fact_id for c in view.ad_copy.claims] == ["F1", "F2", None, None]
    assert [f.text for f in view.facts] == [f.text for f in facts]
    assert [c.text for c in view.ad_copy.claims] == [c.text for c in copy.claims]
    assert "G-901" not in view.model_dump_json()
    view.facts[0].text = "changed view"
    view.ad_copy.claims[0].fact_id = "changed view"
    assert q.get(view.item_id).model_dump(mode="json") == original
    assert q.view(view.item_id, "가").facts[0].id == "F1"


def test_the_reason_shown_to_the_labeler_is_the_same_for_every_item():
    """사유가 검증용 항목과 일반 항목을 가르는 단서가 되면 안 된다 — 점유한 항목의 사유는 모두 같은 말이다 (실제 사유는 점유 전 목록에만 있다)."""
    from ops.review_queue import REASON_LABEL, VIEW_REASON_LABEL

    q, _ = make_queue(gold_rate=1.0)
    q.enqueue_gold([GOLD_ROWS[-1], GOLD_ROWS[0]])                # 커머스와 대출
    real = [add_item(q, 1, category="loan", disagreement=True), add_item(q, 2, category="app")]
    seen = []
    for _ in range(2):
        v = q.claim("가")
        assert q.get(v.item_id).is_gold
        seen.append(v.reason_label)
        q.release("가", v.item_id)
    q.gold_rate = 0.0
    for _ in range(2):
        v = q.claim("가")
        seen.append(v.reason_label)
    assert set(seen) == {VIEW_REASON_LABEL} and len({r.queued_reason for r in real}) == 2         # 실제 사유는 항목마다 다르지만 화면은 같다
    assert all("검증" not in label for label in REASON_LABEL.values()) and REASON_LABEL[QueueReason.GOLD] == REASON_LABEL[QueueReason.RANDOM_QC]


def test_the_waiting_list_hides_claimed_ads_so_a_claimed_ad_never_reveals_whether_it_was_a_quality_check():
    """점유한 광고가 목록에서 사라지는 것은 일반 항목이나 검증용 항목이나 같다 — '목록에 있던 광고' 인지로 검증용을 가릴 수 없다."""
    q, _ = gold_queue(rate=0.0)
    ids = {r.item_id for r in q.list_open()}
    v = q.claim("가")
    assert v.item_id in ids and v.item_id not in {r.item_id for r in q.list_open()}
    assert v.item_id in {r.item_id for r in q.list_open(include_claimed=True)}
    q.gold_rate = 1.0
    g = q.claim("가")
    assert q.get(g.item_id).is_gold and g.item_id not in {r.item_id for r in q.list_open()}
    assert {r.item_id for r in q.list_open()} == ids - {v.item_id}                    # 두 경우 모두 목록에서는 같은 모양으로 빠져 있다


def test_gold_items_are_not_listed_or_counted_before_and_after_labeling():
    q, _ = gold_queue(rate=1.0)
    assert len(q.list_open()) == 6 and q.stats().total == 6                    # 풀에 있을 뿐 목록·통계에 없다
    v = q.claim("가")
    assert q.get(v.item_id).state == S.CLAIMED and v.item_id not in {r.item_id for r in q.list_open(include_claimed=True)}          # 점유 중에도 관리용 목록에 없다
    q.submit(answer(v, "가"))
    assert q.stats().total == 6 and q.stats(include_gold=True).total == 7
    assert v.item_id not in {r.item_id for r in q.list_open()}
    assert q.annotations() == [] and len(q.annotations(include_gold=True)) == 1
    assert q.final_labels() == {} and v.item_id in q.final_labels(include_gold=True)


def test_each_gold_item_goes_to_a_person_only_once_and_not_to_the_export():
    q, _ = gold_queue(rate=1.0)
    ids = []
    for _ in range(5):
        v = q.claim("가")
        ids.append(v.item_id)
        q.submit(answer(v, "가"))
    assert len(set(ids)) == 5
    v6 = q.claim("가")                                                   # 풀이 바닥나면 일반 항목
    assert v6.item_id not in ids
    assert q.claim("나").item_id not in ids                              # 다른 사람에게는 같은 검증용 항목이 새 사본으로 나간다 (다른 id)
    from ops.dataset_tools import export_rows
    assert export_rows(q.items(include_gold=True), required_labels=2) == []


def test_a_released_gold_item_comes_back_to_the_same_person_so_it_cannot_be_dodged():
    q, _ = gold_queue(rate=1.0)
    v = q.claim("가")
    q.release("가", v.item_id)
    assert q.claim("가").item_id == v.item_id


def test_gold_quality_is_measured_per_labeler():
    q, _ = gold_queue(rate=1.0)
    for _ in range(5):
        q.submit(answer(q.claim("가"), "가"))
    for _ in range(5):
        q.submit(answer(q.claim("나"), "나", correct=False))               # 일부러 전부 반대로
    quals = {x.annotator: x for x in q.quality()}
    assert quals["가"].n_gold == 5 and quals["가"].gold_correct == 5 and quals["가"].gold_accuracy == 1.0 and quals["가"].status == "ok"
    assert quals["나"].gold_correct == 0 and quals["나"].status == "watch" and quals["나"].confirmed_low and quals["나"].unsafe_misses == 4 and quals["나"].over_flags == 1


# ═════════════════════════ 통계 ═════════════════════════
def test_stats_report_waiting_breaches_throughput_and_wait_times():
    q, clock = make_queue()
    a = add_item(q, 1, conf=0.2, category="loan")
    b = add_item(q, 2, conf=0.9, category="app")
    s0 = q.stats()
    assert s0.total == 2 and s0.waiting == 2 and s0.sla_breached == 0 and s0.labels_submitted == 0 and s0.mean_wait_s is None
    clock.advance(minutes=10)
    assert q.stats().oldest_wait_s == 600
    label(q, "가", a.id, seconds=40.0)
    s1 = q.stats()
    assert s1.labels_submitted == 1 and s1.throughput_24h == 1 and s1.mean_wait_s == 600 and s1.mean_label_s == 40.0 and s1.by_state["queued"] == 2
    clock.advance(hours=30)
    s2 = q.stats()
    assert s2.sla_breached >= 1 and s2.throughput_24h == 0 and s2.labels_submitted == 1
    assert b.id in {r.item_id for r in q.list_open()} and any(r.overdue for r in q.list_open())


# ═════════════════════════ JSONL 저장 ═════════════════════════
def populated():
    q, clock = make_queue()
    a, b, c = add_item(q, 1), add_item(q, 2), add_item(q, 3)
    label(q, "가", a.id)
    label(q, "나", a.id)                                                     # labeled
    label(q, "가", b.id)
    label(q, "나", b.id, "APPROVE", rules=[], spans=[], why="문제 없음")           # disagreed
    q.claim_item("가", c.id)                                                  # claimed
    q.enqueue_gold(GOLD_ROWS[:2])
    return q, clock, (a, b, c)


def test_jsonl_roundtrip_preserves_everything():
    q, clock, (a, b, c) = populated()
    text = q.to_jsonl()
    q2 = ReviewQueue.from_jsonl(text, clock=clock)
    assert [i.model_dump(mode="json") for i in q2.items(include_gold=True)] == [i.model_dump(mode="json") for i in q.items(include_gold=True)]
    assert q2.required_labels == 2 and q2.gold_pool_size == 2
    assert q2.get(a.id).state == S.LABELED and q2.get(b.id).state == S.DISAGREED and q2.get(c.id).state == S.CLAIMED
    assert q2.to_jsonl() == text


def test_jsonl_without_gold_has_no_gold_data():
    q, clock, _ = populated()
    v = q.claim("라")
    q2 = ReviewQueue(required_labels=2, clock=clock, gold_rate=1.0)
    q2.enqueue_gold(GOLD_ROWS[:1])
    add_item(q2, 1)
    q2.claim("가")                                                              # 검증용 사본이 만들어진다
    text = q2.to_jsonl(include_gold=False)
    assert "gold_template" not in text and '"gold": {' not in text and ReviewQueue.from_jsonl(text, clock=clock).gold_pool_size == 0
    assert '"gold_template"' in q2.to_jsonl(include_gold=True)


def lines_of(text: str) -> list[dict]:
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def with_line(text: str, idx: int, fn) -> str:
    ls = lines_of(text)
    fn(ls[idx])
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in ls) + "\n"


def test_loading_rejects_wrong_format_version_or_garbage():
    q, _, _ = populated()
    text = q.to_jsonl()
    for bad, why in [("", "빈 파일"), ("not json\n", "1번째 줄"), (json.dumps({"format": "other", "version": 1}) + "\n", "검수 대기열 파일이 아닙니다"),
                     (json.dumps({"format": "preflight.review_queue", "version": 99}) + "\n", "지원하지 않는 형식 버전")]:
        with pytest.raises(QueueFormatError) as e:
            ReviewQueue.from_jsonl(bad)
        assert why in str(e.value), (bad, e.value)
    with pytest.raises(QueueFormatError) as e:
        ReviewQueue.from_jsonl(text + "{broken\n")
    assert "번째 줄" in str(e.value)


def test_loading_rejects_a_tampered_state_or_label():
    q, _, (a, b, c) = populated()
    text = q.to_jsonl()
    idx = {d["data"]["id"]: i for i, d in enumerate(lines_of(text)) if i and d["kind"] == "item"}
    # labeled 인데 라벨이 하나뿐
    with pytest.raises(QueueFormatError) as e:
        ReviewQueue.from_jsonl(with_line(text, idx[a.id], lambda d: d["data"]["annotations"].pop()))
    assert "labeled" in str(e.value)
    # disagreed 인데 판정이 같음
    with pytest.raises(QueueFormatError):
        ReviewQueue.from_jsonl(with_line(text, idx[b.id], lambda d: d["data"]["annotations"][1].update(decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"])))
    # 인용이 광고 문구에 없음
    with pytest.raises(QueueFormatError) as e:
        ReviewQueue.from_jsonl(with_line(text, idx[a.id], lambda d: d["data"]["annotations"][0].update(cited_spans=["없는 구절입니다"])))
    assert "라벨 검증 실패" in str(e.value)
    # 알 수 없는 상태 · 점유 기록이 있는데 점유 상태가 아님
    with pytest.raises(QueueFormatError):
        ReviewQueue.from_jsonl(with_line(text, idx[a.id], lambda d: d["data"].update(state="done")))
    with pytest.raises(QueueFormatError):
        ReviewQueue.from_jsonl(with_line(text, idx[a.id], lambda d: d["data"].update(claims=[{"annotator": "가", "claimed_at": "2026-10-19T09:00:00Z", "expires_at": "2026-10-19T09:30:00Z"}])))
    # 같은 id 두 번
    ls = text.splitlines()
    with pytest.raises(QueueFormatError) as e:
        ReviewQueue.from_jsonl("\n".join(ls + [ls[idx[a.id]]]) + "\n")
    assert "중복" in str(e.value)
    # 같은 사람이 두 번 라벨한 기록
    with pytest.raises(QueueFormatError):
        ReviewQueue.from_jsonl(with_line(text, idx[a.id], lambda d: d["data"]["annotations"][1].update(annotator=d["data"]["annotations"][0]["annotator"])))


def test_loading_rejects_adjudication_by_a_labeler():
    q, _, (a, b, c) = populated()
    q.adjudicate(AdjudicationRecord(item_id=b.id, adjudicator="다", decision="REJECT", rule_ids=["FIN-GUARANTEE-01"], cited_spans=["원금 보장"], reason="원금 보장 표현은 게재 불가"))
    text = q.to_jsonl()
    idx = {d["data"]["id"]: i for i, d in enumerate(lines_of(text)) if i and d["kind"] == "item"}
    assert ReviewQueue.from_jsonl(text).get(b.id).state == S.ADJUDICATED
    with pytest.raises(QueueFormatError):
        ReviewQueue.from_jsonl(with_line(text, idx[b.id], lambda d: d["data"]["adjudication"].update(adjudicator="가")))
    with pytest.raises(QueueFormatError):
        ReviewQueue.from_jsonl(with_line(text, idx[b.id], lambda d: d["data"].update(adjudication=None)))


def test_constructor_validation():
    with pytest.raises(ValueError):
        ReviewQueue(required_labels=0)
    with pytest.raises(ValueError):
        ReviewQueue(max_concurrent_claims=0)
    with pytest.raises(ValueError):
        ReviewQueue(gold_rate=1.5)
