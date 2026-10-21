"""Independent boundary review: no credential or DSN is printed by test fixtures."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ops import team_review as team
from ops.review_queue import ReviewQueue


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


class _TestKey(str):
    def __repr__(self):
        return "<redacted test access key>"


def _key():
    return _TestKey(team.new_access_key())


class _Workspace:
    def __init__(self):
        self.owner = _key()
        self.state = team.new_state(team.key_digest(self.owner), now=NOW)

    def __repr__(self):
        return "<test workspace; credentials redacted>"

    def call(self, action, payload=None, *, key=None, now=NOW, request_id=None):
        updated, response = team.execute(self.state, key or self.owner, action, {} if payload is None else payload,
                                         request_id=request_id or str(uuid4()), now=now)
        self.state = updated
        return response

    def reviewer(self):
        key, aid = _key(), "acct_" + uuid4().hex
        self.call("create_account", {"account_id": aid, "name": "검수자 " + aid[-6:], "role": "reviewer", "key_digest": team.key_digest(key)})
        return key, aid

    def add(self):
        return self.call("enqueue_ad", {"dataset_id": str(uuid4()), "category": "other", "synthetic": True,
                                        "copy": {"headline": "상품 구성 확인", "body": "제품 구성을 확인하세요.", "cta": "보기"}})["item_id"]


@pytest.fixture
def workspace():
    return _Workspace()


@pytest.mark.parametrize("security_action", ["set_active", "replace_key"])
def test_a_reviewer_cannot_exhaust_the_receipts_needed_to_revoke_their_access(workspace, monkeypatch, security_action):
    reviewer, aid = workspace.reviewer()
    monkeypatch.setattr(team, "MAX_RECEIPTS", len(workspace.state["receipts"]) + 1)
    # Even an empty claim currently consumes a workspace-wide receipt slot.
    assert workspace.call("claim", key=reviewer)["item"] is None
    with pytest.raises(team.CapacityReached):
        workspace.call("claim", key=reviewer)
    replacement = _key()
    payload = {"account_id": aid, "active": False} if security_action == "set_active" else {"account_id": aid, "key_digest": team.key_digest(replacement)}
    workspace.call(security_action, payload)
    with pytest.raises(team.AuthenticationError):
        team.authenticate(workspace.state, reviewer, now=NOW)


@pytest.mark.parametrize("security_action", ["set_active", "replace_key"])
def test_storage_capacity_must_not_prevent_security_administration(workspace, monkeypatch, security_action):
    reviewer, aid = workspace.reviewer()
    # A state accepted at the byte boundary must still permit revoking access.
    monkeypatch.setattr(team, "MAX_STATE_BYTES", len(team._json(workspace.state).encode("utf-8")))
    replacement = _key()
    payload = {"account_id": aid, "active": False} if security_action == "set_active" else {"account_id": aid, "key_digest": team.key_digest(replacement)}
    workspace.call(security_action, payload)
    with pytest.raises(team.AuthenticationError):
        team.authenticate(workspace.state, reviewer, now=NOW)


@pytest.mark.parametrize("read_action", ["overview", "accounts"])
def test_owner_can_still_open_recovery_views_after_security_exceeds_admission_capacity(workspace, monkeypatch, read_action):
    _, aid = workspace.reviewer()
    monkeypatch.setattr(team, "MAX_STATE_BYTES", len(team._json(workspace.state).encode("utf-8")))
    workspace.call("set_active", {"account_id": aid, "active": False})
    assert len(team._json(workspace.state).encode("utf-8")) > team.MAX_STATE_BYTES
    # The UI must load these views before it offers another security command.
    response = workspace.call(read_action)
    assert response["revision"] == workspace.state["revision"]


def test_overview_counts_match_the_expiry_transition_saved_by_that_same_request(workspace):
    reviewer, _ = workspace.reviewer()
    iid = workspace.add()
    workspace.call("claim", key=reviewer)
    response = workspace.call("overview", key=reviewer, now=NOW + timedelta(minutes=31))
    saved = ReviewQueue.from_jsonl(workspace.state["queue"])
    assert response["held_items"] == []
    assert saved.get(iid).state.value == "queued"
    assert response["stats"]["by_state"]["queued"] == 1
    assert response["stats"]["by_state"]["claimed"] == 0


@pytest.mark.parametrize("action,payload", [
    ([], {}),
    ("replace_key", {"account_id": [], "key_digest": "0" * 64}),
    ("set_active", {"account_id": {}, "active": False}),
])
def test_json_shaped_but_malformed_commands_are_rejected_as_commands(workspace, action, payload):
    original = deepcopy(workspace.state)
    with pytest.raises(team.InvalidCommand):
        workspace.call(action, payload)
    assert workspace.state == original


@pytest.mark.parametrize("action,field,bad_value", [
    ("create_account", "role", []),
    ("create_account", "name", ["name"]),
    ("create_account", "name", "pipeline"),
    ("enqueue_ad", "category", []),
    ("enqueue_ad", "facts", None),
    ("enqueue_ad", "facts", 1),
    ("enqueue_ad", "landing_text", 1),
    ("release", "item_id", []),
])
def test_outer_payload_validation_is_a_correctable_atomic_rejection(workspace, action, field, bad_value):
    """An invalid field must not become an unknown commit outcome in the UI."""
    if action == "create_account":
        key = workspace.owner
        payload = {"account_id": "acct_" + uuid4().hex, "name": "검수자", "role": "reviewer", "key_digest": team.key_digest(_key())}
    elif action == "enqueue_ad":
        key = workspace.owner
        payload = {"dataset_id": "invalid-field", "category": "other", "synthetic": True,
                   "copy": {"headline": "상품 확인", "body": "제품 구성을 확인하세요.", "cta": "보기"}}
    else:
        key, _ = workspace.reviewer()
        payload = {"item_id": "unassigned"}
    payload[field] = bad_value
    original = deepcopy(workspace.state)
    with pytest.raises(team.InvalidCommand):
        workspace.call(action, payload, key=key)
    assert workspace.state == original


def test_owner_key_rotation_can_be_retried_with_the_new_key_without_new_identity(workspace):
    new_key = _key()
    rid = str(uuid4())
    payload = {"account_id": team.OWNER_ID, "key_digest": team.key_digest(new_key)}
    response = workspace.call("replace_key", payload, request_id=rid)
    previous = deepcopy(workspace.state)
    assert workspace.call("replace_key", payload, key=new_key, request_id=rid) == response
    assert workspace.state == previous
    with pytest.raises(team.AuthenticationError):
        workspace.call("overview")


def test_expired_keys_cannot_replay_a_previously_authorized_claim(workspace):
    reviewer, _ = workspace.reviewer()
    workspace.add()
    rid = str(uuid4())
    workspace.call("claim", key=reviewer, request_id=rid)
    original = deepcopy(workspace.state)
    with pytest.raises(team.AuthenticationError):
        workspace.call("claim", key=reviewer, request_id=rid, now=NOW + timedelta(days=30))
    assert workspace.state == original
