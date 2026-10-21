"""Explicit PostgreSQL privilege boundary; live tests require the CI service."""
import hashlib
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ops import team_review as team
from ops import team_review_db_api as api
from ops.team_review_store import PostgresTeamStore, StorageUnavailable


def test_installer_definitions_use_pinned_path_and_no_untrusted_identifier():
    definitions = api.function_definitions("preflight_team")
    assert definitions
    assert all("SET search_path = pg_catalog, pg_temp" in sql for _, _, sql in definitions)
    assert any("SECURITY DEFINER" in sql for _, _, sql in definitions)
    with pytest.raises(ValueError):
        api.function_definitions('bad"; DROP SCHEMA public')


def test_every_installable_function_has_a_safe_signature():
    signatures = [api._function_signature("preflight_team", name, args)
                  for name, args, _ in api.function_definitions("preflight_team")]
    assert len(signatures) == 7
    assert signatures[0] == '"preflight_team"."_api_stamp"(text)'
    with pytest.raises(ValueError):
        api._function_signature("_invalid_schema", "_api_stamp", "text")
    with pytest.raises(ValueError):
        api.owner_role("_invalid_schema")
    with pytest.raises(ValueError):
        api._function_signature("preflight_team", '_api_stamp"; SELECT 1', "text")


def test_runtime_is_api_by_default_and_explicit_admin_has_no_environment_switch(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_TEAM_DATABASE_URL", "configured-placeholder")
    monkeypatch.setenv("PREFLIGHT_TEAM_ADMIN_MODE", "1")
    assert PostgresTeamStore.from_env().admin_mode is False
    assert PostgresTeamStore("configured-placeholder", admin_mode=True).admin_mode is True


def test_runtime_adapter_binds_secrets_and_uses_database_time(monkeypatch):
    owner = team.new_access_key()
    stamp = datetime(2026, 10, 21, tzinfo=timezone.utc)
    original = team.new_state(team.key_digest(owner), now=stamp)
    queries = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            query = query.as_string() if hasattr(query, "as_string") else query
            queries.append((query, params))
            if "lock_workspace" in query:
                self.row = (0, original, stamp)
            elif "save_workspace" in query:
                self.row = (1,)
            else:
                self.row = None
            return self

        def fetchone(self):
            return self.row

    store = PostgresTeamStore("never-disclose-runtime-dsn")
    monkeypatch.setattr(store, "_connect", Connection)
    rid = str(uuid4())
    key = team.new_access_key()
    payload = {"account_id": "acct_" + uuid4().hex, "name": "DB 시간", "role": "reviewer", "key_digest": team.key_digest(key)}
    result = store.execute(owner, "create_account", payload, request_id=rid)
    assert result["account"]["created_at"] == stamp.isoformat()
    assert all(owner not in query and "never-disclose-runtime-dsn" not in query for query, _ in queries)
    save_query, params = next((query, params) for query, params in queries if "save_workspace" in query)
    assert params[:4] == (owner, rid, team._json({"action": "create_account", "payload": payload}), 0)
    assert params[4].obj["receipts"][team.OWNER_ID + ":" + rid]["at"] == stamp.isoformat()
    assert not any("UPDATE " in query or "FROM \"preflight_team\".workspace" in query for query, _ in queries)


def test_runtime_missing_api_fails_without_direct_table_fallback(monkeypatch):
    import psycopg
    queries = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            query = query.as_string() if hasattr(query, "as_string") else query
            queries.append(query)
            if "lock_workspace" in query:
                raise psycopg.errors.UndefinedFunction("sensitive database message")
            return self

    store = PostgresTeamStore("configured-placeholder")
    monkeypatch.setattr(store, "_connect", Connection)
    with pytest.raises(StorageUnavailable) as rejected:
        store.execute(team.new_access_key(), "overview", request_id=str(uuid4()))
    assert "sensitive" not in str(rejected.value)
    assert not any(".workspace " in query for query in queries)


@pytest.fixture
def api_space():
    dsn = os.environ.get("PREFLIGHT_TEAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration DSN not configured; required in CI")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    suffix = uuid4().hex
    schema = "team_api_test_" + suffix
    role = "team_runtime_" + suffix
    password = team.new_access_key()  # Disposable test DB login, never a production credential.
    owner = team.new_access_key()
    admin_store = PostgresTeamStore(dsn, schema=schema, admin_mode=True)
    admin_store.initialize(team.key_digest(owner))
    with admin_store._connect() as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        api.install_api(conn, schema=schema, runtime_role=role)
    runtime_dsn = make_conninfo(dsn, user=role, password=password)
    store = PostgresTeamStore(runtime_dsn, schema=schema)
    runtime_connect = lambda: psycopg.connect(runtime_dsn, prepare_threshold=None)
    yield {"schema": schema, "owner": owner, "store": store, "admin_store": admin_store,
           "runtime_connect": runtime_connect, "runtime_role": role, "api_owner": api.owner_role(schema)}
    if os.environ.get("PREFLIGHT_KEEP_TEST_SCHEMAS") != "1":
        with admin_store._connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(api.owner_role(schema))))


def test_postgres_builtin_sha256_needs_no_pgcrypto(api_space):
    with api_space["runtime_connect"]() as conn:
        digest = conn.execute("SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(%s, 'UTF8')), 'hex')", (api_space["owner"],)).fetchone()[0]
        assert digest == hashlib.sha256(api_space["owner"].encode("ascii")).hexdigest()
        assert conn.execute("SELECT pg_catalog.encode(pg_catalog.sha256('abc'::bytea), 'hex')").fetchone()[0] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize("verb", ["SELECT * FROM", "UPDATE"])
def test_runtime_cannot_access_workspace_directly(api_space, verb):
    import psycopg
    from psycopg import sql
    query = sql.SQL("SELECT * FROM {}.workspace").format(sql.Identifier(api_space["schema"])) if verb.startswith("SELECT") else sql.SQL("UPDATE {}.workspace SET revision = revision").format(sql.Identifier(api_space["schema"]))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with api_space["runtime_connect"]() as conn:
            conn.execute(query)


def test_runtime_without_app_key_cannot_load_state(api_space):
    with pytest.raises(team.AuthenticationError):
        api_space["store"].execute(team.new_access_key(), "overview", request_id=str(uuid4()))


def test_owner_issues_reviewer_and_rotation_retains_request_contract(api_space):
    store, owner = api_space["store"], api_space["owner"]
    reviewer = team.new_access_key()
    aid, rid = "acct_" + uuid4().hex, str(uuid4())
    payload = {"account_id": aid, "name": "통제된 검수자", "role": "reviewer", "key_digest": team.key_digest(reviewer)}
    first = store.execute(owner, "create_account", payload, request_id=rid)
    assert store.execute(owner, "create_account", payload, request_id=rid) == first
    assert store.execute(reviewer, "overview", request_id=str(uuid4()))["account"]["id"] == aid
    next_key, rotation = team.new_access_key(), str(uuid4())
    change = {"account_id": team.OWNER_ID, "key_digest": team.key_digest(next_key)}
    applied = store.execute(owner, "replace_key", change, request_id=rotation)
    assert store.execute(next_key, "replace_key", change, request_id=rotation) == applied
    with pytest.raises(team.AuthenticationError):
        store.execute(owner, "overview", request_id=str(uuid4()))


def test_successive_mutations_append_one_receipt_and_preserve_previous_receipts(api_space):
    """Exercise JSON extraction before subtraction in the actual save function."""
    from psycopg import sql

    store, owner = api_space["store"], api_space["owner"]
    previous_receipts = {}
    for revision in (1, 2):
        rid = str(uuid4())
        payload = {"account_id": "acct_" + uuid4().hex, "name": "영수증 보존",
                   "role": "reviewer", "key_digest": team.key_digest(team.new_access_key())}
        store.execute(owner, "create_account", payload, request_id=rid)
        with api_space["admin_store"]._connect() as conn:
            stored_revision, document = conn.execute(
                sql.SQL("SELECT revision, document FROM {}.workspace WHERE singleton = true")
                .format(sql.Identifier(api_space["schema"]))
            ).fetchone()
        assert stored_revision == revision
        receipts = document["receipts"]
        receipt_id = team.OWNER_ID + ":" + rid
        assert set(receipts) == set(previous_receipts) | {receipt_id}
        assert {key: value for key, value in receipts.items() if key != receipt_id} == previous_receipts
        assert receipts[receipt_id]["result"]["revision"] == revision
        previous_receipts = receipts


def test_installed_api_permissions_and_logging_are_reviewable(api_space):
    with api_space["admin_store"]._connect() as conn:
        result = api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
        assert result["boundary_valid"] is True
        assert result["runtime_table_privileges"] == []
        assert result["function_owner_login"] is False
        assert "parameter_logging" in result


def test_runtime_queue_operations_remain_available(api_space):
    store, owner = api_space["store"], api_space["owner"]
    reviewer = team.new_access_key()
    aid = "acct_" + uuid4().hex
    store.execute(owner, "create_account", {"account_id": aid, "name": "통제된 배정", "role": "reviewer", "key_digest": team.key_digest(reviewer)}, request_id=str(uuid4()))
    added = store.execute(owner, "enqueue_ad", {"dataset_id": "sql-boundary-test", "category": "other", "synthetic": True,
                         "copy": {"headline": "상품", "body": "구성을 살펴보세요", "cta": "확인"}}, request_id=str(uuid4()))
    assert store.execute(reviewer, "claim", request_id=str(uuid4()))["item"]["item_id"] == added["item_id"]


def test_nonsuperuser_creator_installs_and_restores_temporary_set_membership(api_space):
    """Matches the CREATEROLE, non-superuser administrator deployment contract."""
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    suffix = uuid4().hex
    administrator, schema = "team_migrator_" + suffix, "team_nonsuper_" + suffix
    password = team.new_access_key()
    base = api_space["admin_store"]
    with base._connect() as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Identifier(administrator), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(administrator)))
    migration = PostgresTeamStore(make_conninfo(base._database_url, user=administrator, password=password), schema=schema, admin_mode=True)
    try:
        migration.initialize(team.key_digest(team.new_access_key()))
        with migration._connect() as conn:
            assert conn.execute("SELECT pg_has_role(current_user,%s,'SET')", (api.owner_role(schema),)).fetchone()[0] is False
            # Re-install must also work after the temporary grant was removed.
            result = api.install_api(conn, schema=schema, runtime_role=api_space["runtime_role"])
            assert result["boundary_valid"] is True
            assert conn.execute("SELECT pg_has_role(current_user,%s,'SET')", (api.owner_role(schema),)).fetchone()[0] is False
    finally:
        with base._connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(api.owner_role(schema))))
            conn.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(sql.Identifier(database), sql.Identifier(administrator)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(administrator)))
