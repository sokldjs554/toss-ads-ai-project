"""평가셋 도구 — 내보내기 스키마, 확정된 라벨만, 문구 해시 분할의 안정성, 근접 중복·누수 탐지, 데이터셋 검증, 라벨 오류 의심 사례, 합성 예시.

내보내기는 새 행만 만든다. 기존 평가셋 파일을 읽기만 하고, 덮어쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pytest

from ops import dataset_tools as dt
from ops.labeling import AdjudicationRecord, agreement_report
from ops.review_queue import build_demo_queue
from tests.test_labeling_common import ad, add_item, label, make_queue

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "evals" / "datasets"
GOLDEN_FIELDS = ["id", "category", "headline", "body", "cta", "label", "rule_ids", "disclosures", "facts", "claims", "landing_text", "difficulty", "notes", "source", "split"]


def confirmed_queue():
    q, _ = make_queue()
    agree = add_item(q, 1)
    label(q, "가", agree.id)
    label(q, "나", agree.id)                                                   # 일치 → labeled
    adj = add_item(q, 2, headline="원금 보장 무조건 승인 대출 2호")
    label(q, "가", adj.id, "REJECT", rules=["FIN-APPROVAL-01"], spans=["무조건 승인"])
    label(q, "나", adj.id, "REVISE", rules=["FIN-SPEED-01"], spans=["월 3% 확정 수익"])
    q.adjudicate(AdjudicationRecord(item_id=adj.id, adjudicator="다", decision="REJECT", rule_ids=["FIN-APPROVAL-01"], cited_spans=["무조건 승인"], reason="무조건 승인은 게재 불가 등급이다"))
    undecided = add_item(q, 3, headline="원금 보장 결론 안 난 광고 3호")
    label(q, "가", undecided.id, "REJECT")
    label(q, "나", undecided.id, "APPROVE", rules=[], spans=[], why="문제 없음")           # disagreed — 조정 전
    waiting = add_item(q, 4, headline="원금 보장 아직 대기 4호")                         # queued
    half = add_item(q, 5, headline="원금 보장 한 명만 라벨 5호")
    label(q, "가", half.id)
    ok = add_item(q, 6, headline="가을 신상 입고 6호", body="무료 배송 (5만원 이상)", disclosures=["5만원 이상 무료 배송"])
    label(q, "가", ok.id, "APPROVE", rules=[], spans=[], why="조건 병기")
    label(q, "나", ok.id, "APPROVE", rules=[], spans=[], why="문제 없음")
    return q, dict(agree=agree, adj=adj, undecided=undecided, waiting=waiting, half=half, ok=ok)


# ═════════════════════════ 내보내기 ═════════════════════════
def test_rows_follow_the_golden_schema_field_for_field_and_add_provenance_after_it():
    first = json.loads(next(l for l in (DATASETS / "golden.jsonl").read_text(encoding="utf-8").splitlines() if l.startswith("{")))
    assert [k for k in first if k != "label_history"] == GOLDEN_FIELDS == dt.golden_fields()
    q, _ = confirmed_queue()
    rows = dt.export_rows(q.items(), guideline_version="1.0", split_seed="s")
    assert rows
    for r in rows:
        assert list(r)[:len(GOLDEN_FIELDS)] == GOLDEN_FIELDS
        assert list(r)[len(GOLDEN_FIELDS):] == dt.PROVENANCE_FIELDS
        assert r["split"] in ("dev", "holdout") and r["source"] == "labeling_queue" and r["guideline_version"] == "1.0" and r["synthetic"] is False


def test_only_confirmed_labels_are_exported_and_the_reasons_for_skipping_are_kept():
    q, d = confirmed_queue()
    res = dt.export_rows_report(q.items())
    exported = set(res.exported_item_ids)
    assert exported == {d["agree"].id, d["adj"].id, d["ok"].id}
    skipped = {s["item_id"]: s["reason"] for s in res.skipped}
    assert set(skipped) == {d["undecided"].id, d["waiting"].id, d["half"].id}
    assert "확정되지 않음(disagreed)" in skipped[d["undecided"].id] and "확정되지 않음(queued)" in skipped[d["waiting"].id]


def test_agreed_and_adjudicated_rows_carry_who_labeled_and_how_it_was_settled():
    q, d = confirmed_queue()
    rows = {r["queue_item_id"]: r for r in dt.export_rows(q.items())}
    a, j, ok = rows[d["agree"].id], rows[d["adj"].id], rows[d["ok"].id]
    assert a["labelers"] == ["가", "나"] and a["adjudicated"] is False and a["agreement"] == 1.0 and a["label"] == "REJECT" and a["rule_ids"] == ["FIN-GUARANTEE-01"]
    assert a["difficulty"] == "unrated" and a["single_labeler"] is False
    assert j["adjudicated"] is True and j["agreement"] == 0.0 and j["label"] == "REJECT" and j["rule_ids"] == ["FIN-APPROVAL-01"] and j["difficulty"] == "gray"
    assert "조정" in j["notes"] and "게재 불가 등급" in j["notes"]
    assert ok["label"] == "APPROVE" and ok["rule_ids"] == [] and ok["disclosures"] == ["5만원 이상 무료 배송"] and ok["facts"] == [{"id": "F1", "text": "총보수 연 0.15%"}]
    assert ok["headline"] == "가을 신상 입고 6호" and ok["cta"] == "가입하기"


def test_a_one_labeler_item_is_excluded_unless_asked_and_then_marked():
    q, _ = make_queue(required=1)
    it = add_item(q, 1)
    label(q, "가", it.id)
    assert dt.export_rows(q.items()) == []
    res = dt.export_rows_report(q.items(), allow_single_labeler=True, required_labels=1)
    assert res.rows[0]["single_labeler"] is True and res.rows[0]["agreement"] is None and "독립 확인 없음" in res.rows[0]["notes"]
    assert any("라벨러가 한 명" in w for w in res.warnings)


def test_synthetic_demo_rows_are_excluded_by_default_and_marked_when_included():
    q = build_demo_queue(dt.load_labeling_demo())
    assert dt.export_rows(q.items()) == []
    res = dt.export_rows_report(q.items(), include_synthetic=True)
    assert len(res.rows) == 28 and all(r["synthetic"] and r["source"] == "labeling_synthetic_demo" for r in res.rows)
    assert any("합성 예시" in w for w in res.warnings)


def test_gold_items_are_never_exported():
    q, _ = make_queue(gold_rate=1.0)
    q.enqueue_gold([{"id": "G-9", "category": "loan", "headline": "누구나 무조건 승인", "body": "5분 입금", "cta": "신청", "label": "REJECT", "rule_ids": ["FIN-APPROVAL-01"]}])
    add_item(q, 1)
    v = q.claim("가")
    q.submit(__import__("tests.test_labeling_common", fromlist=["ann"]).ann(v.item_id, "가", "REJECT", rules=["FIN-APPROVAL-01"], spans=["무조건 승인"]))
    res = dt.export_rows_report(q.items(include_gold=True), required_labels=1, allow_single_labeler=True)
    assert res.rows == [] and any("검증용" in s["reason"] for s in res.skipped)


def test_identical_ads_are_exported_once_and_conflicting_labels_are_called_out():
    q, _ = make_queue()
    a = q.enqueue_ad(ad(), dataset_id="D-1", category="loan")
    b = q.enqueue_ad(ad(), dataset_id="D-2", category="loan")                  # 같은 문구·고지·사실
    for it, dec, kw in ((a, "REJECT", {}), (b, "APPROVE", dict(rules=[], spans=[], why="문제 없음"))):
        label(q, "가", it.id, dec, **kw)
        label(q, "나", it.id, dec, **kw)
    res = dt.export_rows_report(q.items())
    assert len(res.rows) == 1 and any("라벨이 다른 항목" in w for w in res.warnings)
    assert any("문구·고지·사실·랜딩 문구가 같음" in s["reason"] for s in res.skipped)


def test_row_ids_differ_when_only_the_disclosures_differ():
    q, _ = make_queue()
    a = q.enqueue_ad(ad(disclosures=["5,000원 이상 구매 시"]), dataset_id="D-1", category="ecommerce")
    b = q.enqueue_ad(ad(disclosures=["50,000원 이상 구매 시"]), dataset_id="D-2", category="ecommerce")
    for it in (a, b):
        label(q, "가", it.id)
        label(q, "나", it.id)
    rows = dt.export_rows(q.items())
    assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"] and rows[0]["split"] == rows[1]["split"]       # 제목·본문·버튼이 같으면 같은 분할


# ═════════════════════════ 분할: 문구 해시 ═════════════════════════
def test_the_same_wording_always_lands_in_the_same_split_whatever_else_is_present():
    key = dt.copy_key("전 상품 10% 할인", "지금 구매", "자세히 보기")
    first = dt.split_for(key)
    assert all(dt.split_for(key) == first for _ in range(20))
    variants = [("전 상품 10％ 할인", "지금 구매", "자세히 보기"), ("전 상품 10% 할인", "지금  구매", "자세히  보기"), ("전​ 상품 10% 할인", "지금 구매", "자세히 보기"),
                ("전 상품 10% 할인.", "지금 구매!", "자세히 보기")]
    assert {dt.copy_key(*v) for v in variants} == {key}                         # 표기 변형은 같은 문구로 접힌다
    assert {dt.split_for(dt.copy_key(*v)) for v in variants} == {first}
    # 전각 숫자·한자로 쓴 우회 표기도 같은 문구다 (정규화를 거치지 않으면 다른 분할로 갈라져 누수가 된다)
    assert dt.copy_key("원금 보장 투자", "월 ３% 확정 수익", "가입") == dt.copy_key("元金 保障 투자", "월 3% 確定 收益", "가입")


def test_adding_new_items_never_changes_the_split_of_existing_ones():
    q, _ = make_queue()
    items = []
    for i in range(12):
        it = add_item(q, i, headline=f"원금 보장 스플릿 안정성 광고 {i}호")
        label(q, "가", it.id)
        label(q, "나", it.id)
        items.append(it)
    before = {r["queue_item_id"]: r["split"] for r in dt.export_rows(q.items())}
    for i in range(12, 40):
        it = add_item(q, i, headline=f"원금 보장 스플릿 안정성 광고 {i}호")
        label(q, "가", it.id)
        label(q, "나", it.id)
    after = {r["queue_item_id"]: r["split"] for r in dt.export_rows(q.items())}
    assert len(after) == 40 and all(after[k] == v for k, v in before.items())
    assert {r["queue_item_id"]: r["split"] for r in dt.export_rows(reversed(q.items()))} == after          # 순서와도 무관


def test_split_ratio_and_seed():
    keys = [f"광고문구{i}" for i in range(3000)]
    holdout = sum(dt.split_for(k) == "holdout" for k in keys) / len(keys)
    assert abs(holdout - dt.DEFAULT_HOLDOUT_FRACTION) < 0.03
    other = [dt.split_for(k, seed="다른 시드") for k in keys]
    assert other != [dt.split_for(k) for k in keys]                              # 시드를 바꾸면 분할이 바뀐다 (그래서 시드는 고정해 둔다)
    assert dt.split_for("x", holdout_fraction=0.0) == "dev" and dt.split_for("x", holdout_fraction=1.0) == "holdout"


# ═════════════════════════ 근접 중복·누수 ═════════════════════════
def _row(i, headline, body, split="dev", label="REJECT", **kw):
    r = {"id": f"R-{i}", "category": "loan", "headline": headline, "body": body, "cta": "신청", "label": label, "rule_ids": ["FIN-APPROVAL-01"] if label != "APPROVE" else [],
         "disclosures": [], "facts": [], "claims": [], "landing_text": "", "difficulty": "clear", "notes": "", "source": "authored", "split": split}
    r.update(kw)
    return r


NEAR_A = ("누구나 무조건 당일 승인 받는 신용대출", "서류 없이 모바일로 5분이면 입금되는 대출을 지금 바로 신청하세요")
NEAR_B = ("누구나 무조건 당일 승인 받는 신용대출", "서류 없이 모바일로 5분이면 입금되는 대출을 지금 바로 신청하세요!!")
NEAR_C = ("누구나 무조건 당일 승인 받는 신용대출", "서류 없이 모바일로 5분이면 입금되는 대출을 지금 신청하세요")
OTHER = ("연 4.5%~15.0% 직장인 신용대출", "심사 결과에 따라 한도와 금리가 달라질 수 있어요")


def test_similarity_helper_and_threshold():
    ka, kc, ko = (dt.copy_key(*t, "신청") for t in (NEAR_A, NEAR_C, OTHER))
    assert dt.jaccard(dt.shingles(ka), dt.shingles(kc)) >= dt.NEAR_DUP_THRESHOLD > dt.jaccard(dt.shingles(ka), dt.shingles(ko))


def test_near_duplicates_are_found_and_cross_split_pairs_are_flagged_as_leaks():
    rows = [_row(1, *NEAR_A, split="dev"), _row(2, *NEAR_C, split="holdout"), _row(3, *OTHER, split="dev")]
    v = dt.validate_dataset(rows)
    assert [(n.a, n.b) for n in v.near_duplicates] == [("R-1", "R-2")]
    assert [(n.a, n.b, n.same_split) for n in v.leaks] == [("R-1", "R-2", False)]
    assert any(i.code == "split_leak" and "누수 위험" in i.message for i in v.issues)


def test_same_split_near_duplicates_warn_but_are_not_leaks():
    rows = [_row(1, *NEAR_A, split="dev"), _row(2, *NEAR_C, split="dev")]
    v = dt.validate_dataset(rows)
    assert len(v.near_duplicates) == 1 and v.leaks == [] and any(i.code == "near_duplicate" and i.level == "warn" for i in v.issues)


def test_a_new_dev_row_that_nearly_duplicates_an_existing_holdout_row_is_a_leak():
    existing = [_row(9, *NEAR_A, split="holdout")]
    new = [_row(1, *NEAR_C, split="dev"), _row(2, *OTHER, split="dev")]
    v = dt.validate_dataset(new, existing=existing)
    assert len(v.leaks) == 1 and v.leaks[0].from_existing and {v.leaks[0].a, v.leaks[0].b} == {"R-1", "R-9"}
    # 기존 행끼리의 유사성은 새 데이터의 문제가 아니므로 보고하지 않는다
    assert dt.validate_dataset([_row(2, *OTHER)], existing=[_row(8, *NEAR_A, split="dev"), _row(9, *NEAR_C, split="holdout")]).near_duplicates == []


def test_identical_rows_with_different_labels_are_an_error_and_contrast_pairs_are_not():
    rows = [_row(1, *NEAR_A, label="REJECT"), _row(2, *NEAR_A, label="APPROVE")]
    v = dt.validate_dataset(rows)
    assert any(i.level == "error" and "완전히 같은 행인데 라벨이 다릅니다" in i.message for i in v.issues) and not v.ok
    contrast = [_row(1, *NEAR_A, label="REVISE", disclosures=["5,000원 이상 구매 시"], rule_ids=["GRND-COND-02"]), _row(2, *NEAR_A, label="APPROVE", disclosures=["50,000원 이상 구매 시"])]
    v2 = dt.validate_dataset(contrast)
    assert v2.ok and [i.level for i in v2.issues if i.code == "near_duplicate"] == ["info"]


def test_sharded_search_finds_the_same_pairs_as_the_exact_search():
    rng = random.Random(3)
    words = ["대출", "승인", "금리", "한도", "입금", "투자", "수익", "보장", "할인", "쿠폰", "배송", "체험", "구독", "보험", "카드", "적립", "혜택", "신청", "조건", "확인"]
    texts = [" ".join(rng.choice(words) + str(rng.randrange(100)) for _ in range(12)) for _ in range(300)]
    planted = []
    for i in range(0, 300, 25):                       # 12쌍을 일부러 심는다 (한 낱말 끝에 글자 하나만 더)
        t = texts[i].split()
        t[3] = t[3] + "요"
        texts.append(" ".join(t))
        planted.append((i, len(texts) - 1))
    keys = [dt.normalize_key(t) for t in texts]
    exact = dt.near_duplicate_pairs(keys, exact_limit=10_000)
    sharded = dt.near_duplicate_pairs(keys, exact_limit=0)
    assert {(i, j) for i, j, _ in exact} >= set(planted)
    assert {(i, j) for i, j, _ in sharded} == {(i, j) for i, j, _ in exact}        # 이 표본에서는 놓친 쌍이 없다 (확률적 방법이라 일반적 보장은 아님)
    assert all(s >= dt.NEAR_DUP_THRESHOLD for _, _, s in sharded)


def test_very_short_copy_is_compared_only_when_identical():
    keys = [dt.normalize_key(t) for t in ("지금 구매", "지금 구매!", "지금 구매하세요")]
    assert [(i, j) for i, j, _ in dt.near_duplicate_pairs(keys)] == [(0, 1)]


# ═════════════════════════ 데이터셋 검증 ═════════════════════════
@pytest.mark.parametrize("name", ["golden", "holdout", "contrast_g069", "conditions", "conditions_extra", "adversarial", "redteam_seeds"])
def test_existing_datasets_pass_validation_without_errors(name):
    v = dt.validate_dataset(dt.load_jsonl_rows(DATASETS / f"{name}.jsonl"))
    assert v.ok, v.errors


def test_golden_and_holdout_have_no_leak_between_them():
    v = dt.validate_dataset(dt.load_jsonl_rows(DATASETS / "holdout.jsonl"), existing=dt.load_jsonl_rows(DATASETS / "golden.jsonl"))
    assert v.leaks == [] and v.ok


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r.pop("headline"), "missing_field"),
    (lambda r: r.update(label="MAYBE"), "bad_label"),
    (lambda r: r.update(category="finance"), "bad_category"),
    (lambda r: r.update(split="test"), "bad_split"),
    (lambda r: r.update(rule_ids=["NOPE-01"]), "unknown_rule"),
    (lambda r: r.update(label="REJECT", rule_ids=[]), "flag_without_rule"),
    (lambda r: r.update(label="APPROVE", rule_ids=["FIN-GUARANTEE-01"]), "approve_with_block"),
    (lambda r: r.update(facts=[{"id": "F1"}]), "bad_fact"),
    (lambda r: r.update(rule_ids="FIN-APPROVAL-01"), "bad_type"),
    (lambda r: r.update(body="가" * 6000), "too_long"),
])
def test_validation_catches_schema_and_label_rule_problems(mutate, code):
    row = _row(1, *OTHER)
    mutate(row)
    v = dt.validate_dataset([row])
    assert not v.ok and code in {i.code for i in v.errors}, [(i.code, i.message) for i in v.issues]


def test_duplicate_ids_and_label_skew_are_reported():
    v = dt.validate_dataset([_row(1, *OTHER), _row(1, "다른 제목", "다른 본문")])
    assert any(i.code == "duplicate_id" for i in v.errors)
    skew = dt.validate_dataset([_row(i, f"제목 {i} 번 광고입니다", f"본문 {i} 번 내용입니다 고유한 문장 {i * 7}") for i in range(12)])
    assert any(i.code == "label_skew" for i in skew.warnings) and skew.label_distribution == {"REJECT": 12}


def test_exported_rows_pass_validation_including_against_the_real_datasets():
    q, _ = confirmed_queue()
    rows = dt.export_rows(q.items())
    v = dt.validate_dataset(rows, existing=dt.load_jsonl_rows(DATASETS / "golden.jsonl") + dt.load_jsonl_rows(DATASETS / "holdout.jsonl"))
    assert v.ok, v.errors


def test_exported_rows_survive_a_file_roundtrip_with_a_header_comment(tmp_path):
    q, _ = confirmed_queue()
    rows = dt.export_rows(q.items())
    text = dt.rows_to_jsonl(rows, "헤더 줄")
    assert text.startswith("# 헤더 줄\n")
    p = dt.write_jsonl_new(tmp_path / "out.jsonl", text)
    assert dt.load_jsonl_rows(p) == rows


# ═════════════════════════ 기존 파일을 건드리지 않는다 ═════════════════════════
READ_ONLY_FILES = ["golden.jsonl", "holdout.jsonl", "adversarial.jsonl", "conditions.jsonl", "conditions_extra.jsonl", "contrast_g069.jsonl", "redteam_seeds.jsonl", "PROVENANCE.json"]


def _digest() -> dict[str, str]:
    """이 도구가 읽는 기존 평가셋 파일들의 해시 (다른 작업이 폴더에 새 파일을 만드는 것과 무관하게 비교한다)."""
    return {n: hashlib.sha256((DATASETS / n).read_bytes()).hexdigest() for n in READ_ONLY_FILES}


def test_exporting_and_validating_leave_existing_dataset_files_untouched(tmp_path):
    before = _digest()
    q, _ = confirmed_queue()
    rows = dt.export_rows(q.items())
    dt.validate_dataset(rows, existing=dt.load_jsonl_rows(DATASETS / "golden.jsonl"))
    out = tmp_path / "export.jsonl"
    dt.write_jsonl_new(out, dt.rows_to_jsonl(rows))
    assert _digest() == before


def test_write_jsonl_new_refuses_to_overwrite(tmp_path):
    p = tmp_path / "x.jsonl"
    dt.write_jsonl_new(p, "a\n")
    with pytest.raises(FileExistsError):
        dt.write_jsonl_new(p, "b\n")
    assert p.read_text(encoding="utf-8") == "a\n"
    with pytest.raises(FileExistsError):
        dt.write_jsonl_new(DATASETS / "golden.jsonl", "덮어쓰기 시도")


def test_revise_label_keeps_the_old_label_and_reason_without_touching_the_original():
    row = _row(1, *OTHER, label="APPROVE")
    new = dt.revise_label(row, new_label="REVISE", new_rule_ids=["GRND-COND-01"], reason="대조 사례로 결제 조건 누락이 확인됨", guideline_version="1.1")
    assert new["label"] == "REVISE" and new["rule_ids"] == ["GRND-COND-01"]
    assert new["label_history"] == [{"version": "1.1", "label": "APPROVE", "rule_ids": [], "reason": "대조 사례로 결제 조건 누락이 확인됨"}]
    assert row["label"] == "APPROVE" and "label_history" not in row
    with pytest.raises(ValueError):
        dt.revise_label(row, new_label="REVISE", new_rule_ids=["GRND-COND-01"], reason="  ", guideline_version="1.1")
    with pytest.raises(ValueError):
        dt.revise_label(row, new_label="MAYBE", new_rule_ids=[], reason="이유", guideline_version="1.1")


def test_gold_row_selection_is_deterministic_and_limited_to_clear_cases():
    rows = dt.load_jsonl_rows(DATASETS / "golden.jsonl")
    a, b = dt.select_gold_rows(rows, 10, seed="s"), dt.select_gold_rows(rows, 10, seed="s")
    assert [r["id"] for r in a] == [r["id"] for r in b] and len(a) == 10 and {r["difficulty"] for r in a} <= {"clear", "benign_scary"}
    assert [r["id"] for r in dt.select_gold_rows(rows, 10, seed="t")] != [r["id"] for r in a]


# ═════════════════════════ 라벨 오류 의심 사례 ═════════════════════════
def rec(cid, label, pred, conf, *, rules=(), label_rules=(), ds="golden", report="r1", **kw):
    return {"id": cid, "label": label, "predicted": pred, "confidence": conf, "label_rules": list(label_rules), "predicted_rules": list(rules), "_dataset": ds, "_report": report, "error": "", **kw}


def test_suspect_scores_rank_confident_repeated_disagreements_first():
    recs = []
    for run in range(4):
        recs.append(rec("A", "APPROVE", "REVISE", 0.85, rules=["TA-OMISSION-01"], report=f"r{run}"))          # 4번 모두 한 단계 어긋남
        recs.append(rec("B", "APPROVE", "REJECT", 0.9, report=f"r{run}") if run == 0 else rec("B", "APPROVE", "APPROVE", 0.9, report=f"r{run}"))    # 1/4 만 두 단계 어긋남
        recs.append(rec("C", "APPROVE", "REVISE", 0.6, report=f"r{run}"))                                        # 확신이 낮음 → 후보 아님
        recs.append(rec("D", "REJECT", "REJECT", 0.95, report=f"r{run}"))                                        # 일치
    out = dt.label_noise_suspects(recs)
    assert [s.case_id for s in out] == ["A", "B"]
    a, b = out
    assert a.score == pytest.approx(0.85 * 0.6) and a.n_runs == 4 and a.n_disagree == 4 and a.extra_rules == {"TA-OMISSION-01": 4}
    assert b.score == pytest.approx(0.9 * 1.0 / 4) and b.n_disagree == 1 and b.predicted == {"REJECT": 1}
    assert "사람이 원문을 다시 읽어" in a.note


def test_two_step_disagreement_outranks_one_step_at_the_same_confidence():
    out = dt.label_noise_suspects([rec("one", "APPROVE", "REVISE", 0.9), rec("two", "APPROVE", "REJECT", 0.9), rec("three", "REJECT", "APPROVE", 0.9)])
    assert out[0].score == out[1].score == pytest.approx(0.9) and out[2].case_id == "one" and out[2].score == pytest.approx(0.54)


def test_runs_without_a_judgement_are_not_counted():
    recs = [rec("A", "APPROVE", "HUMAN_REVIEW", 0.95), rec("A", "APPROVE", "REVISE", 0.9, report="r2"), rec("A", "APPROVE", "REVISE", 0.9, report="r3", error="timeout"),
            rec("A", "APPROVE", "REVISE", 0.9, report="r4", rewrite_error="x")]
    [s] = dt.label_noise_suspects(recs)
    assert s.n_runs == 1 and s.n_disagree == 1                  # 담당자 확인과 장애로 끝난 기록은 분모에서도 뺀다
    assert dt.label_noise_suspects([rec("A", "APPROVE", "HUMAN_REVIEW", 0.95)]) == []


def test_a_changed_label_version_is_counted_separately():
    recs = [rec("G", "APPROVE", "REVISE", 0.9, report="old"), rec("G", "REVISE", "REVISE", 0.9, report="new")]
    out = dt.label_noise_suspects(recs)
    assert [(s.case_id, s.label) for s in out] == [("G", "APPROVE")] and out[0].n_runs == 1


def test_reports_that_are_demo_or_aborted_are_not_read_as_model_judgements():
    base = {"dataset": "golden", "cases": [{"id": "X", "label": "APPROVE", "predicted": "REVISE", "confidence": 0.9, "label_rules": [], "predicted_rules": []}]}
    assert dt.load_report_cases({**base, "mode": "demo"}) == []
    assert dt.load_report_cases({**base, "mode": "partial", "aborted": {"done": 3}}) == []
    got = dt.load_report_cases({**base, "mode": "partial", "name": "mine"})
    assert got and got[0]["_report"] == "mine" and got[0]["_dataset"] == "golden" and got[0]["_mode"] == "partial"
    assert dt.load_report_cases({**base, "mode": "demo"}, include_demo=True)


def test_the_g069_label_error_is_found_in_the_real_stored_reports():
    """v1.1 까지 G-069 의 라벨은 APPROVE 였고, 1~4차 실제 모델 실행(저장된 결과)이 4번 모두 REVISE(TA-OMISSION-01)로 판정했다 — 라벨을 REVISE 로 고치게 된 바로 그 사례."""
    hist = sorted((ROOT / "evals" / "reports" / "history").glob("baseline_partial_run[1-4]*.json"))
    if len(hist) < 4:
        pytest.skip("저장된 1~4차 실행 결과가 없는 저장소")
    sus = dt.suspects_from_files(hist)
    ids = [s.case_id for s in sus]
    assert "G-069" in ids
    g = next(s for s in sus if s.case_id == "G-069")
    assert g.label == "APPROVE" and g.n_runs == 4 and g.n_disagree == 4 and g.predicted == {"REVISE": 4} and "TA-OMISSION-01" in g.extra_rules
    assert ids.index("G-069") == 0 or sus[0].score == g.score                      # 1위이거나 같은 점수의 공동 1위
    # 라벨을 고친 뒤의 저장된 결과(v1.2)에는 같은 사례가 의심 목록에 없다
    latest = dt.suspects_from_files([ROOT / "evals" / "reports" / "baseline_partial.json"])
    assert all(s.case_id != "G-069" for s in latest)


# ═════════════════════════ 합성 예시 ═════════════════════════
def test_demo_file_is_synthetic_and_every_label_passes_the_real_checks():
    rows = dt.load_labeling_demo()
    assert len(rows) == 28 and all(r["synthetic"] is True for r in rows)
    q = build_demo_queue(rows)                         # 실제 점유·제출 경로를 거치므로 인용·규칙 검사가 모든 라벨에 적용됐다
    assert len(q) == 28 and all(i.synthetic for i in q.items())
    states = q.stats().by_state
    assert states["labeled"] + states["adjudicated"] == 28 and states["adjudicated"] == 7
    assert all(len(i.annotations) == 2 for i in q.items())
    assert {a.annotator for a in q.annotations()} == {"합성 라벨러 A", "합성 라벨러 B"}
    assert all(i.adjudication.adjudicator == "합성 조정자" for i in q.items() if i.adjudication)


def test_demo_agreement_numbers_are_the_documented_ones():
    q = build_demo_queue(dt.load_labeling_demo())
    o = agreement_report(q.annotations()).overall
    assert o.method == "cohen" and o.n_items == 28 and not o.sufficient
    assert o.percent_agreement == pytest.approx(0.75) and o.kappa == pytest.approx(0.6245, abs=1e-3)
    assert o.ci_low == pytest.approx(0.359, abs=0.02) and o.ci_high == pytest.approx(0.838, abs=0.02)        # 시드 고정 부트스트랩
    assert any("표본 부족" in w for w in o.warnings)
    assert sum(1 for x in q.quality() if x.too_fast) == 1                      # 일부러 넣은 너무 빠른 제출 1건


def test_demo_file_has_the_synthetic_marker_check():
    bad = DATASETS / "labeling_demo.jsonl"
    assert bad.read_text(encoding="utf-8").startswith("# 합성 라벨링 예시")
