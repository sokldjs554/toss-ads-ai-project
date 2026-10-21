"""Independent boundary review: no credential or DSN is printed by test fixtures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

from ops import team_review as team
from ops.review_queue import ReviewQueue
from ops.team_review_store import PostgresTeamStore, StorageUnavailable


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


def test_reviewer_overview_releases_expired_claims_without_disclosing_stats(workspace):
    reviewer, _ = workspace.reviewer()
    iid = workspace.add()
    workspace.call("claim", key=reviewer)
    response = workspace.call("overview", key=reviewer, now=NOW + timedelta(minutes=31))
    saved = ReviewQueue.from_jsonl(workspace.state["queue"])
    assert response["held_items"] == []
    assert "stats" not in response
    assert saved.get(iid).state.value == "queued"
    assert saved.get(iid).claims == []


def test_owner_overview_counts_match_the_expiry_transition_saved_by_that_same_request(workspace):
    reviewer, _ = workspace.reviewer()
    iid = workspace.add()
    workspace.call("claim", key=reviewer)
    response = workspace.call("overview", now=NOW + timedelta(minutes=31))
    saved = ReviewQueue.from_jsonl(workspace.state["queue"])
    assert saved.get(iid).state.value == "queued"
    assert saved.get(iid).claims == []
    assert response["stats"]["by_state"]["queued"] == 1
    assert response["stats"]["by_state"]["claimed"] == 0


@pytest.mark.parametrize("role", ["reviewer", "adjudicator"])
@pytest.mark.parametrize("second_decision,final_state", [("APPROVE", "labeled"), ("REVISE", "disagreed")])
def test_non_owner_overview_does_not_expose_single_item_agreement_statistics(workspace, role, second_decision, final_state):
    first, _ = workspace.reviewer()
    second, _ = workspace.reviewer()
    observer = second
    if role == "adjudicator":
        observer = _key()
        workspace.call("create_account", {"account_id": "acct_" + uuid4().hex, "name": "조정자", "role": role,
                                           "key_digest": team.key_digest(observer)})
    iid = workspace.call("enqueue_ad", {"dataset_id": "single-item", "category": "other", "synthetic": True,
                          "copy": {"headline": "국내 최고 상품", "body": "구성을 확인하세요.", "cta": "보기"}})["item_id"]
    workspace.call("claim", key=first)
    workspace.call("submit", {"item_id": iid, "decision": "APPROVE"}, key=first)
    workspace.call("claim", key=second)
    payload = {"item_id": iid, "decision": second_decision}
    if second_decision == "REVISE":
        payload.update(rule_ids=["TA-SUPERLATIVE-01"], cited_spans=["국내 최고"], rationale="최상급 표현을 입증할 상품 근거가 없어 수정이 필요합니다.")
    workspace.call("submit", payload, key=second)

    response = workspace.call("overview", key=observer)
    assert set(response) == {"account", "held_items", "required_labels", "revision"}
    assert response["account"]["role"] == role
    assert response["held_items"] == []
    assert response["required_labels"] == 2
    # Owners still need the accurate operational summary for both outcomes.
    assert workspace.call("overview")["stats"]["by_state"][final_state] == 1


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


def test_deactivation_frees_only_that_accounts_current_review_slots(workspace):
    first, aid = workspace.reviewer()
    second, bid = workspace.reviewer()
    third, cid = workspace.reviewer()
    iid = workspace.add()
    workspace.call("claim", key=first)
    workspace.call("claim", key=second)
    assert workspace.call("claim", key=third)["item"] is None
    workspace.call("set_active", {"account_id": aid, "active": False})
    assert workspace.call("claim", key=third)["item"]["item_id"] == iid
    queue = ReviewQueue.from_jsonl(workspace.state["queue"])
    assert {claim.annotator for claim in queue.get(iid).claims} == {bid, cid}


def test_expired_keys_cannot_replay_a_previously_authorized_claim(workspace):
    reviewer, _ = workspace.reviewer()
    workspace.add()
    rid = str(uuid4())
    workspace.call("claim", key=reviewer, request_id=rid)
    original = deepcopy(workspace.state)
    with pytest.raises(team.AuthenticationError):
        workspace.call("claim", key=reviewer, request_id=rid, now=NOW + timedelta(days=30))
    assert workspace.state == original


class _BoundaryConnection:
    """Argument-forwarding seam only; this does not simulate DB transactions."""

    def __init__(self, state):
        self.state = state
        self.writes = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, *_):
        if not isinstance(statement, str) and statement.as_string().startswith("UPDATE "):
            self.writes += 1
        return self

    def fetchone(self):
        return self.state["revision"], deepcopy(self.state)

    @property
    def rowcount(self):
        return 1


@pytest.mark.parametrize("payload", [[], "", 0, False])
def test_store_does_not_turn_falsey_invalid_payload_into_a_valid_claim(workspace, monkeypatch, payload):
    reviewer, _ = workspace.reviewer()
    workspace.add()
    connection = _BoundaryConnection(workspace.state)
    store = PostgresTeamStore("unused-boundary-test", admin_mode=True)
    monkeypatch.setattr(store, "_connect", lambda: connection)
    with pytest.raises(team.InvalidCommand):
        store.execute(reviewer, "claim", payload, request_id=str(uuid4()))
    assert connection.writes == 0


@pytest.fixture
def pg_adversarial_space():
    dsn = os.environ.get("PREFLIGHT_TEAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration DSN not configured; run with the CI PostgreSQL service")
    # The fixture creates and drops only its own randomly named isolated schema.
    store = PostgresTeamStore(dsn, schema="team_adversarial_" + uuid4().hex, admin_mode=True)
    owner = _key()
    store.initialize(team.key_digest(owner))
    try:
        yield store, owner
    finally:
        if os.environ.get("PREFLIGHT_KEEP_TEST_SCHEMAS") != "1":
            from psycopg import sql
            with store._connect() as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(store.schema)))


class _FaultAfterWrite:
    """Inject transport failure around a REAL PostgreSQL transaction boundary."""

    def __init__(self, connection, phase):
        self.connection = connection
        self.phase = phase

    def __enter__(self):
        self.connection.__enter__()
        return self

    def execute(self, statement, *args):
        import psycopg
        result = self.connection.execute(statement, *args)
        if self.phase == "before_commit" and not isinstance(statement, str) and statement.as_string().startswith("UPDATE "):
            raise psycopg.OperationalError("Injected connection failure after a write, before commit")
        return result

    def __exit__(self, exc_type, exc, traceback):
        import psycopg
        result = self.connection.__exit__(exc_type, exc, traceback)
        if exc_type is None and self.phase == "after_commit":
            raise psycopg.OperationalError("Injected loss of response after the actual commit")
        return result


@pytest.mark.parametrize("phase,held_after_failure", [("before_commit", 0), ("after_commit", 1)])
def test_postgres_same_request_recovers_across_real_commit_boundary(pg_adversarial_space, monkeypatch, phase, held_after_failure):
    from psycopg import sql
    store, owner = pg_adversarial_space
    reviewer = _key()
    aid = "acct_" + uuid4().hex
    store.execute(owner, "create_account", {"account_id": aid, "name": "검수자", "role": "reviewer", "key_digest": team.key_digest(reviewer)}, request_id=str(uuid4()))
    for label in ("one", "two"):
        store.execute(owner, "enqueue_ad", {"dataset_id": label, "category": "other", "synthetic": True,
                      "copy": {"headline": "상품 구성 확인", "body": "제품 구성을 확인하세요.", "cta": "보기"}}, request_id=str(uuid4()))
    rid = str(uuid4())
    original_connect = store._connect
    with monkeypatch.context() as patch:
        patch.setattr(store, "_connect", lambda: _FaultAfterWrite(original_connect(), phase))
        with pytest.raises(StorageUnavailable):
            store.execute(reviewer, "claim", {}, request_id=rid)

    # A separate connection sees either both the claim and receipt, or neither.
    observer = PostgresTeamStore(store._database_url, schema=store.schema, admin_mode=True)
    before_retry = observer.execute(reviewer, "overview", {}, request_id=str(uuid4()))
    assert len(before_retry["held_items"]) == held_after_failure
    with observer._connect() as connection:
        document = connection.execute(sql.SQL("SELECT document FROM {}.workspace WHERE singleton = true").format(sql.Identifier(store.schema))).fetchone()[0]
    assert (aid + ":" + rid in document["receipts"]) is bool(held_after_failure)

    result = observer.execute(reviewer, "claim", {}, request_id=rid)
    assert result["item"] is not None
    assert store.execute(reviewer, "claim", {}, request_id=rid) == result
    after_retry = observer.execute(reviewer, "overview", {}, request_id=str(uuid4()))
    assert after_retry["held_items"] == [result["item"]["item_id"]]


def test_postgres_concurrent_reviewers_cannot_overclaim_or_lose_an_independent_label(pg_adversarial_space):
    from psycopg import sql
    store, owner = pg_adversarial_space
    reviewers = []
    for index in range(3):
        key, aid = _key(), "acct_" + uuid4().hex
        store.execute(owner, "create_account", {"account_id": aid, "name": f"검수자 {index + 1}", "role": "reviewer", "key_digest": team.key_digest(key)}, request_id=str(uuid4()))
        reviewers.append((key, aid))
    iid = store.execute(owner, "enqueue_ad", {"dataset_id": "concurrent", "category": "other", "synthetic": True,
                        "copy": {"headline": "상품 구성 확인", "body": "제품 구성을 확인하세요.", "cta": "보기"}}, request_id=str(uuid4()))["item_id"]

    def claim(index):
        result = store.execute(reviewers[index][0], "claim", {}, request_id=str(uuid4()))
        return index, result["item"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(claim, range(3)))
    selected = [index for index, item in claims if item is not None]
    assert len(selected) == 2
    assert all(item["item_id"] == iid for _, item in claims if item is not None)

    def submit(index):
        return store.execute(reviewers[index][0], "submit", {"item_id": iid, "decision": "APPROVE"}, request_id=str(uuid4()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = list(pool.map(submit, selected))
    assert all(result["submitted"] for result in submitted)
    with store._connect() as connection:
        document = connection.execute(sql.SQL("SELECT document FROM {}.workspace WHERE singleton = true").format(sql.Identifier(store.schema))).fetchone()[0]
    item = ReviewQueue.from_jsonl(document["queue"]).get(iid)
    assert item.state.value == "labeled"
    assert {annotation.annotator for annotation in item.annotations} == {reviewers[index][1] for index in selected}
    assert len(item.annotations) == 2 and item.claims == []
