"""Real queue transitions behind a server identity and command boundary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4
import pytest
from ops import team_review as team
from ops.review_queue import ReviewQueue

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)

@pytest.fixture
def space():
    key = team.new_access_key()
    return {"state": team.new_state(team.key_digest(key), now=NOW), "owner": key}

def call(s, key, action, payload=None, rid=None, now=NOW):
    state, result = team.execute(s["state"], key, action, payload or {}, request_id=rid or str(uuid4()), now=now)
    s["state"] = state
    return result

def account(s, name, role="reviewer"):
    key = team.new_access_key()
    result = call(s, s["owner"], "create_account", {"name": name, "role": role, "key_digest": team.key_digest(key), "account_id": "acct_" + uuid4().hex})
    return key, result["account_id"]

def add(s, name="sample"):
    return call(s, s["owner"], "enqueue_ad", {"dataset_id": name, "category": "other", "product_name": "상품", "copy": {"headline": "상품 구성 확인", "body": "제품 구성과 사용 방법을 확인하세요.", "cta": "자세히 보기"}, "facts": [], "synthetic": True})["item_id"]

def test_bad_key_and_role_leave_state_unchanged(space):
    key, _ = account(space, "검수 A")
    old = deepcopy(space["state"])
    with pytest.raises(team.AuthenticationError):
        call(space, team.new_access_key(), "overview")
    with pytest.raises(team.PermissionDenied):
        call(space, key, "enqueue_ad")
    assert space["state"] == old

def test_annotation_uses_account_identity_and_blinded_view(space):
    a, aid = account(space, "검수 A")
    b, _ = account(space, "검수 B")
    iid = add(space)
    call(space, a, "claim")
    with pytest.raises(team.InvalidCommand):
        call(space, a, "submit", {"item_id": iid, "decision": "APPROVE", "annotator": "다른 사람"})
    call(space, a, "submit", {"item_id": iid, "decision": "APPROVE", "rationale": "다른 사람에게 숨길 판정 이유"})
    q = ReviewQueue.from_jsonl(space["state"]["queue"])
    assert q.get(iid).annotations[0].annotator == aid
    assert q.get(iid).annotations[0].prelabel_mode == "hidden"
    with pytest.raises(team.PermissionDenied):
        call(space, b, "view", {"item_id": iid})
    text = json.dumps(call(space, b, "claim")["item"], ensure_ascii=False)
    for secret in ("다른 사람에게 숨길 판정 이유", "annotations", "gold", "suggested_label"):
        assert secret not in text
    for action in ("export", "disagreements", "accounts"):
        with pytest.raises(team.PermissionDenied):
            call(space, b, action)

def test_key_rotation_keeps_identity_and_cannot_label_twice(space):
    a, aid = account(space, "검수 A")
    iid = add(space)
    call(space, a, "claim")
    call(space, a, "submit", {"item_id": iid, "decision": "APPROVE"})
    new = team.new_access_key()
    call(space, space["owner"], "replace_key", {"account_id": aid, "key_digest": team.key_digest(new)})
    with pytest.raises(team.AuthenticationError):
        call(space, a, "overview")
    assert call(space, new, "overview")["account"]["id"] == aid
    assert call(space, new, "claim")["item"] is None

def test_response_loss_retries_do_not_claim_a_second_item(space):
    a, _ = account(space, "검수 A")
    add(space, "one")
    add(space, "two")
    rid = str(uuid4())
    first = call(space, a, "claim", rid=rid)
    rev = space["state"]["revision"]
    assert call(space, a, "claim", rid=rid) == first
    assert space["state"]["revision"] == rev
    assert len(call(space, a, "overview")["held_items"]) == 1
    with pytest.raises(team.RequestConflict):
        call(space, a, "release", {"item_id": first["item"]["item_id"]}, rid=rid)

def test_revoked_account_cannot_replay_a_cached_result(space):
    a, aid = account(space, "검수 A")
    add(space)
    rid = str(uuid4())
    call(space, a, "claim", rid=rid)
    call(space, space["owner"], "set_active", {"account_id": aid, "active": False})
    with pytest.raises(team.AuthenticationError):
        call(space, a, "claim", rid=rid)

def test_two_reviewers_share_work_and_synthetic_labels_are_not_real_evidence(space):
    a, _ = account(space, "검수 A")
    b, _ = account(space, "검수 B")
    iid = add(space)
    for key in (a, b):
        call(space, key, "claim")
        call(space, key, "submit", {"item_id": iid, "decision": "APPROVE"})
    assert call(space, space["owner"], "export")["rows"] == []
    example = call(space, space["owner"], "export", {"include_synthetic": True})
    assert len(example["rows"]) == 1
    assert "synthetic" in json.dumps(example["rows"]).lower()

def test_owner_cannot_review_and_no_tokens_persist(space):
    key, _ = account(space, "검수 A")
    add(space)
    with pytest.raises(team.PermissionDenied):
        call(space, space["owner"], "claim")
    with pytest.raises(team.AuthenticationError):
        call(space, key, "overview", now=NOW + timedelta(days=31))
    text = json.dumps(space["state"])
    assert key not in text and space["owner"] not in text

def test_invalid_request_and_capacity_rejection_are_atomic(space, monkeypatch):
    old = deepcopy(space["state"])
    with pytest.raises(team.InvalidCommand):
        call(space, space["owner"], "restore", {"queue": "client-side labels"})
    with pytest.raises(team.InvalidCommand):
        call(space, space["owner"], "overview", rid="not-a-uuid")
    assert space["state"] == old
    monkeypatch.setattr(team, "MAX_ITEMS", 1)
    add(space, "first")
    old = deepcopy(space["state"])
    with pytest.raises(team.CapacityReached):
        add(space, "second")
    assert space["state"] == old

def test_same_request_uuid_is_scoped_to_account(space):
    a, _ = account(space, "검수 A")
    b, _ = account(space, "검수 B")
    add(space)
    rid = str(uuid4())
    assert call(space, a, "claim", rid=rid)["item"]
    assert call(space, b, "claim", rid=rid)["item"]


def test_disagreement_requires_third_role_and_saves_the_final_reason(space):
    a, _ = account(space, "검수 A")
    b, _ = account(space, "검수 B")
    c, cid = account(space, "조정 C", "adjudicator")
    iid = call(space, space["owner"], "enqueue_ad", {
        "dataset_id": "disagreement", "category": "other", "synthetic": True,
        "copy": {"headline": "국내 최고 상품", "body": "구성을 확인하세요.", "cta": "자세히 보기"},
    })["item_id"]
    call(space, a, "claim")
    call(space, a, "submit", {"item_id": iid, "decision": "APPROVE"})
    call(space, b, "claim")
    fix = {"item_id": iid, "decision": "REVISE", "rule_ids": ["TA-SUPERLATIVE-01"], "cited_spans": ["국내 최고"], "rationale": "최상급 표현을 입증할 상품 근거가 없어 표현을 수정해야 합니다."}
    call(space, b, "submit", fix)
    with pytest.raises(team.PermissionDenied):
        call(space, b, "adjudicate", fix)
    assert len(call(space, c, "disagreements")["items"]) == 1
    call(space, c, "adjudicate", fix)
    q = ReviewQueue.from_jsonl(space["state"]["queue"])
    assert q.get(iid).adjudication.adjudicator == cid
    assert q.get(iid).adjudication.reason == fix["rationale"]
    assert q.get(iid).state.value == "adjudicated"
