"""Actual PostgreSQL transactions; CI supplies an isolated service database."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest

from ops.team_review import new_access_key, key_digest, AuthenticationError
from ops.team_review_store import PostgresTeamStore, StorageBusy, StorageUnavailable


@pytest.fixture
def db_space():
    dsn = os.environ.get("PREFLIGHT_TEAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration DSN not configured; mandatory in CI")
    schema = "team_test_" + uuid4().hex
    store = PostgresTeamStore(dsn, schema=schema, lock_timeout_ms=100)
    owner = new_access_key()
    store.initialize(key_digest(owner))
    yield store, owner
    # The only objects removed are this fixture's randomly named test schema.
    if os.environ.get("PREFLIGHT_KEEP_TEST_SCHEMAS") != "1":
        from psycopg import sql
        with store._connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def invoke(store, token, action, payload=None, rid=None):
    return store.execute(token, action, payload, request_id=rid or str(uuid4()))


def reviewer(store, owner, name):
    token = new_access_key()
    aid = "acct_" + uuid4().hex
    invoke(store, owner, "create_account", {"account_id": aid, "name": name, "role": "reviewer", "key_digest": key_digest(token)})
    return token, aid


def add(store, owner, name):
    return invoke(store, owner, "enqueue_ad", {"dataset_id": name, "category": "other", "synthetic": True, "copy": {"headline": "상품 보기", "body": "구성을 확인하세요", "cta": "확인"}})


def test_two_connections_keep_both_concurrent_writes(db_space):
    store, owner = db_space
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: add(store, owner, name), ["left", "right"]))
    assert len({r["item_id"] for r in results}) == 2
    assert invoke(store, owner, "overview")["stats"]["total"] == 2


def test_concurrent_duplicate_claim_returns_same_saved_receipt(db_space):
    store, owner = db_space
    token, _ = reviewer(store, owner, "담당자")
    add(store, owner, "one")
    add(store, owner, "two")
    rid = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(store, token, "claim", rid=rid), range(2)))
    assert results[0] == results[1]
    assert len(invoke(store, token, "overview")["held_items"]) == 1
    # A new store/connection represents another device after response loss.
    other = PostgresTeamStore(store._database_url, schema=store.schema)
    assert invoke(other, token, "claim", rid=rid) == results[0]


def test_lock_timeout_leaves_no_partial_receipt_and_retry_works(db_space):
    from psycopg import sql
    store, owner = db_space
    rid = str(uuid4())
    with store._connect() as held:
        held.execute(sql.SQL("SELECT document FROM {}.workspace FOR UPDATE").format(sql.Identifier(store.schema)))
        with pytest.raises(StorageBusy):
            invoke(store, owner, "enqueue_ad", {"dataset_id": "locked", "category": "other", "synthetic": True, "copy": {"headline": "상품", "body": "구성을 확인", "cta": "보기"}}, rid=rid)
    assert invoke(store, owner, "overview")["stats"]["total"] == 0
    assert add(store, owner, "locked")["item_id"]


def test_bootstrap_restart_never_changes_owner_or_erases_work(db_space):
    store, owner = db_space
    add(store, owner, "keep")
    other = new_access_key()
    store.initialize(key_digest(other))
    with pytest.raises(AuthenticationError):
        invoke(store, other, "overview")
    assert invoke(store, owner, "overview")["stats"]["total"] == 1


def test_revocation_applies_to_other_connection_and_cached_requests(db_space):
    store, owner = db_space
    token, aid = reviewer(store, owner, "담당자")
    add(store, owner, "one")
    rid = str(uuid4())
    invoke(store, token, "claim", rid=rid)
    invoke(store, owner, "set_active", {"account_id": aid, "active": False})
    with pytest.raises(AuthenticationError):
        invoke(PostgresTeamStore(store._database_url, schema=store.schema), token, "claim", rid=rid)


def test_broken_database_is_reported_without_local_success_or_credentials():
    store = PostgresTeamStore("postgresql://test:not-a-real-secret@127.0.0.1:1/no_db?connect_timeout=1")
    assert "not-a-real-secret" not in repr(store)
    with pytest.raises(StorageUnavailable) as error:
        invoke(store, new_access_key(), "overview")
    assert "not-a-real-secret" not in str(error.value)
