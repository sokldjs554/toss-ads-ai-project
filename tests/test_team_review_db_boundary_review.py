"""Independent attacks against the PostgreSQL credential/receipt boundary.

These tests use a real, isolated runtime LOGIN with no workspace table grants.
They do not claim that SQL revalidates the Python queue's full semantics when
both a valid application key and the runtime database credential are possessed.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from ops import team_review as team
from test_team_review_db_api import api_space  # Shared isolated PostgreSQL fixture.


def _snapshot(space):
    from psycopg import sql

    with space["admin_store"]._connect() as conn:
        return conn.execute(
            sql.SQL("SELECT revision, document FROM {}.workspace WHERE singleton = true").format(
                sql.Identifier(space["schema"]),
            ),
        ).fetchone()


def _reviewer(space):
    key, account_id = team.new_access_key(), "acct_" + uuid4().hex
    space["store"].execute(
        space["owner"], "create_account",
        {"account_id": account_id, "name": "독립 경계 검수자", "role": "reviewer", "key_digest": team.key_digest(key)},
        request_id=str(uuid4()),
    )
    return key, account_id


def _queries(space):
    from psycopg import sql

    schema = sql.Identifier(space["schema"])
    return (
        sql.SQL("SELECT * FROM {}.lock_workspace(%s, %s::uuid, %s)").format(schema),
        sql.SQL("SELECT {}.save_workspace(%s, %s::uuid, %s, %s, %s)").format(schema),
    )


def _reject_proposal(space, key, *, action="claim", payload=None, mutate=None, null_argument=None):
    import psycopg
    from psycopg.types.json import Jsonb

    payload = {} if payload is None else payload
    request_id = str(uuid4())
    command = team._json({"action": action, "payload": payload})
    before = _snapshot(space)
    load, save = _queries(space)
    with space["runtime_connect"]() as conn:
        revision, original, db_now = conn.execute(load, (key, request_id, command)).fetchone()
        # Loading and calculating the legitimate transition must succeed before
        # the attack is applied; unrelated installation errors cannot pass a test.
        candidate, _ = team.execute(original, key, action, payload, request_id=request_id, now=db_now)
        if mutate is not None:
            candidate = mutate(candidate, original, request_id)
        arguments = [key, request_id, command, revision, Jsonb(candidate)]
        if null_argument is not None:
            arguments[null_argument] = None
        with pytest.raises(psycopg.Error) as rejected:
            conn.execute(save, arguments)
        assert rejected.value.sqlstate in {"PT401", "PT403", "PT409", "PT422", "PT500"}
        conn.rollback()
    assert _snapshot(space) == before


def test_sql_null_key_cannot_load_private_workspace(api_space):
    import psycopg

    load, _ = _queries(api_space)
    before = _snapshot(api_space)
    with api_space["runtime_connect"]() as conn:
        with pytest.raises(psycopg.Error) as rejected:
            conn.execute(load, (None, str(uuid4()), team._json({"action": "overview", "payload": {}})))
        assert rejected.value.sqlstate == "PT401"
        conn.rollback()
    assert _snapshot(api_space) == before


@pytest.mark.parametrize("argument", [0, 1, 2, 3], ids=["key", "request_id", "command", "expected_revision"])
def test_sql_null_cannot_bypass_save_authentication_or_cas(api_space, argument):
    key, _ = _reviewer(api_space)
    _reject_proposal(api_space, key, null_argument=argument)


@pytest.mark.parametrize("attack", ["role", "key", "accounts_null", "keys_null", "document_null", "owner_ordinary_action"])
def test_ordinary_action_cannot_change_authentication_state(api_space, attack):
    key, account_id = _reviewer(api_space)
    action, payload = "claim", {}
    if attack == "owner_ordinary_action":
        key = api_space["owner"]
        action = "enqueue_ad"
        payload = {"dataset_id": "independent-db-boundary", "category": "other", "synthetic": True,
                   "copy": {"headline": "상품 구성", "body": "구성을 확인하세요", "cta": "확인"}}

    def mutate(candidate, _original, _request_id):
        if attack == "document_null":
            return None
        if attack == "role":
            candidate["accounts"][account_id]["role"] = "owner"
        elif attack in {"key", "owner_ordinary_action"}:
            forged_digest = team.key_digest(team.new_access_key())
            candidate["keys"][forged_digest] = deepcopy(next(iter(candidate["keys"].values())))
            candidate["keys"][forged_digest]["account_id"] = team.OWNER_ID
        elif attack == "accounts_null":
            candidate["accounts"] = None
        elif attack == "keys_null":
            candidate["keys"] = None
        return candidate

    _reject_proposal(api_space, key, action=action, payload=payload, mutate=mutate)


@pytest.mark.parametrize("attack", ["issue_owner", "retain_previous_key", "future_clock", "deactivate_owner"])
def test_owner_management_action_requires_exact_credential_delta(api_space, attack):
    reviewer_key, account_id = _reviewer(api_space)
    new_digest = team.key_digest(team.new_access_key())
    action = "replace_key"
    payload = {"account_id": account_id, "key_digest": new_digest}
    if attack == "issue_owner":
        action = "create_account"
        payload = {"account_id": "acct_" + uuid4().hex, "name": "추가 검수자", "role": "reviewer", "key_digest": new_digest}
    elif attack == "deactivate_owner":
        action, payload = "set_active", {"account_id": account_id, "active": False}

    def mutate(candidate, original, request_id):
        if attack == "issue_owner":
            candidate["accounts"][payload["account_id"]]["role"] = "owner"
        elif attack == "retain_previous_key":
            old_digest = team.key_digest(reviewer_key)
            candidate["keys"][old_digest] = deepcopy(original["keys"][old_digest])
        elif attack == "future_clock":
            receipt = candidate["receipts"][team.OWNER_ID + ":" + request_id]
            receipt["at"] = (datetime.fromisoformat(receipt["at"]) + timedelta(days=365)).isoformat()
            candidate["keys"][new_digest]["expires_at"] = (
                datetime.fromisoformat(candidate["keys"][new_digest]["expires_at"]) + timedelta(days=365)
            ).isoformat()
        else:
            candidate["accounts"][team.OWNER_ID]["active"] = False
        return candidate

    _reject_proposal(api_space, api_space["owner"], action=action, payload=payload, mutate=mutate)


@pytest.mark.parametrize("attack", ["edit_existing", "remove_existing", "other_actor", "extra_receipt", "fingerprint", "receipt_revision"])
def test_save_cannot_rewrite_history_or_forge_receipt_identity(api_space, attack):
    key, account_id = _reviewer(api_space)

    def mutate(candidate, original, request_id):
        own_id = account_id + ":" + request_id
        previous_id = next(iter(original["receipts"]))
        if attack == "edit_existing":
            candidate["receipts"][previous_id]["result"] = {"forged": True}
        elif attack == "remove_existing":
            del candidate["receipts"][previous_id]
        elif attack == "other_actor":
            candidate["receipts"][team.OWNER_ID + ":" + request_id] = candidate["receipts"].pop(own_id)
        elif attack == "extra_receipt":
            candidate["receipts"][account_id + ":" + str(uuid4())] = deepcopy(candidate["receipts"][own_id])
        elif attack == "fingerprint":
            candidate["receipts"][own_id]["fingerprint"] = "f" * 64
        else:
            candidate["receipts"][own_id]["result"]["revision"] = original["revision"]
        return candidate

    _reject_proposal(api_space, key, mutate=mutate)


def test_key_expiring_after_load_cannot_save_using_transaction_start_time(api_space):
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb

    key, _ = _reviewer(api_space)
    digest = team.key_digest(key)
    request_id = str(uuid4())
    command = team._json({"action": "claim", "payload": {}})
    load, save = _queries(api_space)
    with api_space["runtime_connect"]() as conn:
        # Begin the transaction while this key is valid; transaction_timestamp()
        # remains frozen even after the real clock passes the key's expiry.
        conn.execute("SELECT transaction_timestamp()")
        with api_space["admin_store"]._connect() as admin_conn:
            db_now = admin_conn.execute("SELECT clock_timestamp()").fetchone()[0]
            expiration = (db_now + timedelta(seconds=1)).isoformat()
            admin_conn.execute(
                sql.SQL("UPDATE {}.workspace SET document = jsonb_set(document, %s, %s) WHERE singleton = true").format(
                    sql.Identifier(api_space["schema"]),
                ),
                (["keys", digest, "expires_at"], Jsonb(expiration)),
            )
        revision, original, db_now = conn.execute(load, (key, request_id, command)).fetchone()
        candidate, _ = team.execute(original, key, "claim", {}, request_id=request_id, now=db_now)
        expires_at = original["keys"][digest]["expires_at"]
        conn.execute("SELECT pg_sleep(greatest(0, extract(epoch FROM (%s::timestamptz - clock_timestamp()))) + 0.05)", (expires_at,))
        with pytest.raises(psycopg.Error) as rejected:
            conn.execute(save, (key, request_id, command, revision, Jsonb(candidate)))
        assert rejected.value.sqlstate == "PT401"
        conn.rollback()
    assert _snapshot(api_space) == (revision, original)


def test_verifier_rejects_table_access_hidden_behind_noinherit_set_role(api_space):
    from psycopg import sql
    from ops import team_review_db_api as api

    writer = "team_hidden_writer_" + uuid4().hex
    writer_identifier = sql.Identifier(writer)
    runtime_identifier = sql.Identifier(api_space["runtime_role"])
    schema_identifier = sql.Identifier(api_space["schema"])
    with api_space["admin_store"]._connect() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT").format(writer_identifier))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_identifier, writer_identifier))
        conn.execute(sql.SQL("GRANT SELECT, UPDATE ON {}.workspace TO {}").format(schema_identifier, writer_identifier))
        conn.execute(sql.SQL("GRANT {} TO {}").format(writer_identifier, runtime_identifier))
    try:
        with api_space["runtime_connect"]() as conn:
            # NOINHERIT conceals these grants from a current-role privilege
            # check, while SET ROLE still exposes the table without an app key.
            assert conn.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", (api_space["schema"] + ".workspace",)).fetchone()[0] is False
            conn.execute(sql.SQL("SET ROLE {}").format(writer_identifier))
            assert conn.execute(sql.SQL("SELECT revision FROM {}.workspace").format(schema_identifier)).fetchone() is not None
        with api_space["admin_store"]._connect() as conn:
            with pytest.raises(ValueError):
                api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
    finally:
        with api_space["admin_store"]._connect() as conn:
            conn.execute(sql.SQL("REVOKE {} FROM {}").format(writer_identifier, runtime_identifier))
            conn.execute(sql.SQL("DROP OWNED BY {}").format(writer_identifier))
            conn.execute(sql.SQL("DROP ROLE {}").format(writer_identifier))


def test_verifier_detects_column_privilege_added_after_installation(api_space):
    from psycopg import sql
    from ops import team_review_db_api as api

    schema_identifier = sql.Identifier(api_space["schema"])
    runtime_identifier = sql.Identifier(api_space["runtime_role"])
    # Initial REVOKE ALL ON TABLE also removes prior column grants in PG 16.
    # This deliberately simulates a later grant before a standalone verify-api.
    with api_space["admin_store"]._connect() as conn:
        conn.execute(sql.SQL("GRANT SELECT(document) ON {}.workspace TO {}").format(schema_identifier, runtime_identifier))
    try:
        with api_space["runtime_connect"]() as conn:
            assert conn.execute("SELECT has_table_privilege(current_user, %s, 'SELECT')", (api_space["schema"] + ".workspace",)).fetchone()[0] is False
            assert conn.execute(sql.SQL("SELECT document->>'schema_version' FROM {}.workspace").format(schema_identifier)).fetchone()[0] == "1"
        with api_space["admin_store"]._connect() as conn:
            report = api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
        assert report["boundary_valid"] is False
        assert "SELECT" in report["runtime_column_privileges"]
    finally:
        with api_space["admin_store"]._connect() as conn:
            conn.execute(sql.SQL("REVOKE SELECT(document) ON {}.workspace FROM {}").format(schema_identifier, runtime_identifier))
