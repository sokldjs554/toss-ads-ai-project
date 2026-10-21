"""Credential lifecycle tests: controlled stores, private files and optional PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

import pytest

from ops import team_review as team
from ops.team_review_store import StorageUnavailable
from scripts import team_review_admin as admin


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
BINDING = "a" * 64
DSN = "postgresql://operator:never-print-this-password@localhost/test_admin"


def operation(kind, **kwargs):
    return admin.new_operation(kind, schema="preflight_team", binding=BINDING, **kwargs)


def state_with_work():
    key = team.new_access_key()
    state = team.new_state(team.key_digest(key), now=NOW)
    state, result = team.execute(state, key, "enqueue_ad", {
        "dataset_id": "controlled-admin-fixture", "category": "other", "synthetic": True,
        "copy": {"headline": "상품 구성", "body": "구성을 살펴보세요.", "cta": "확인"},
    }, request_id=str(uuid4()), now=NOW)
    return state, key


class MemoryStore:
    def __init__(self):
        self.owner = team.new_access_key()
        self.state = team.new_state(team.key_digest(self.owner))
        self.calls = []
        self.lose_after = None
        self.before_execute = None

    def execute(self, key, action, payload=None, *, request_id):
        if self.before_execute:
            self.before_execute(action)
        self.calls.append((action, deepcopy(payload), request_id))
        self.state, result = team.execute(self.state, key, action, payload or {}, request_id=request_id)
        if action == self.lose_after:
            self.lose_after = None
            raise StorageUnavailable("postgresql://sensitive:user-password@private-host lost response")
        return result


def private_text(path, value):
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_operation_file_is_private_atomic_and_never_overwrites(tmp_path):
    path = tmp_path / "owner.json"
    record = operation("rotate-owner")
    with admin.OperationFile(path) as file:
        file.create(record)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert file.load() == record
        with pytest.raises(admin.AdminError):
            file.create(operation("rotate-owner"))
    assert json.loads(path.read_text()) == record


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400])
def test_secret_file_requires_exact_0600(tmp_path, mode):
    path = private_text(tmp_path / "secret", "value")
    path.chmod(mode)
    with pytest.raises(admin.AdminError):
        admin.read_private_file(path)


def test_secret_file_rejects_symlinks_hardlinks_and_nonfiles(tmp_path):
    target = private_text(tmp_path / "target", "value")
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(admin.AdminError):
        admin.read_private_file(alias)
    alias.unlink()
    os.link(target, alias)
    with pytest.raises(admin.AdminError):
        admin.read_private_file(target)
    with pytest.raises(admin.AdminError):
        admin.read_private_file(tmp_path)


def test_output_lock_rejects_simultaneous_same_operation_file(tmp_path):
    path = tmp_path / "owner.json"
    with admin.OperationFile(path):
        with pytest.raises(admin.AdminError) as caught:
            with admin.OperationFile(path):
                pass
    assert caught.value.code == "operation_busy"


def test_output_rejects_shared_writable_directory(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    with pytest.raises(admin.AdminError):
        with admin.OperationFile(directory / "owner.json"):
            pass


def test_output_rejects_symlink_without_changing_target(tmp_path):
    target = private_text(tmp_path / "target", "keep")
    path = tmp_path / "result"
    path.symlink_to(target)
    with admin.OperationFile(path) as file:
        with pytest.raises(admin.AdminError):
            file.create(operation("rotate-owner"))
    assert target.read_text() == "keep"


def test_durable_file_precedes_self_rotation_and_lost_response_can_resume(tmp_path, capsys):
    store = MemoryStore()
    old = store.owner
    path = tmp_path / "owner.json"
    durable_calls = []
    store.before_execute = lambda action: durable_calls.append(json.loads(path.read_text()))
    store.lose_after = "replace_key"
    env = {"PREFLIGHT_TEAM_DATABASE_URL": DSN, "PREFLIGHT_TEAM_OWNER_KEY": old}
    args = ["rotate-owner", "--out", str(path)]
    assert admin.main(args, environ=env, store_factory=lambda *a, **kw: store) != 0
    prepared = json.loads(path.read_text())
    assert durable_calls[0]["access_key"] == prepared["access_key"]
    assert prepared["access_key"] != old
    with pytest.raises(team.AuthenticationError):
        team.authenticate(store.state, old)
    assert admin.main(["resume", "--out", str(path)], environ=env, store_factory=lambda *a, **kw: store) == 0
    final = json.loads(path.read_text())
    assert final["status"] == "confirmed"
    assert final["request_id"] == prepared["request_id"]
    assert final["access_key"] == prepared["access_key"]
    assert len(store.state["receipts"]) == 1
    output = capsys.readouterr()
    assert old not in output.out + output.err
    assert final["access_key"] not in output.out + output.err
    assert "never-print-this-password" not in output.out + output.err
    assert "user-password" not in output.out + output.err


def test_prepared_file_failure_never_calls_database(tmp_path, monkeypatch, capsys):
    store = MemoryStore()
    def failed(_fd):
        raise OSError("secret-value-in-exception")
    monkeypatch.setattr(os, "fsync", failed)
    result = admin.main(["rotate-owner", "--out", str(tmp_path / "owner.json")],
                        environ={"PREFLIGHT_TEAM_DATABASE_URL": DSN, "PREFLIGHT_TEAM_OWNER_KEY": store.owner},
                        store_factory=lambda *a, **kw: store)
    assert result != 0
    assert store.calls == []
    assert "secret-value-in-exception" not in capsys.readouterr().err


def test_both_file_and_directory_are_fsynced_before_database_mutation(tmp_path, monkeypatch):
    store = MemoryStore()
    synced = []
    original = os.fsync
    def track(fd):
        synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
        original(fd)
    monkeypatch.setattr(os, "fsync", track)
    def before(_action):
        assert stat.S_IFREG in synced and stat.S_IFDIR in synced
    store.before_execute = before
    assert admin.main(["rotate-owner", "--out", str(tmp_path / "owner.json")],
                      environ={"PREFLIGHT_TEAM_DATABASE_URL": DSN, "PREFLIGHT_TEAM_OWNER_KEY": store.owner},
                      store_factory=lambda *a, **kw: store) == 0


def test_status_write_failure_after_commit_can_resume_durable_key(tmp_path, monkeypatch):
    store = MemoryStore()
    path = tmp_path / "owner.json"
    env = {"PREFLIGHT_TEAM_DATABASE_URL": DSN, "PREFLIGHT_TEAM_OWNER_KEY": store.owner}
    original = admin.OperationFile.save
    def broken(*args):
        raise OSError("private filesystem details")
    monkeypatch.setattr(admin.OperationFile, "save", broken)
    assert admin.main(["rotate-owner", "--out", str(path)], environ=env, store_factory=lambda *a, **kw: store) == 4
    record = json.loads(path.read_text())
    assert record["status"] == "prepared"
    assert team.authenticate(store.state, record["access_key"])["role"] == "owner"
    monkeypatch.setattr(admin.OperationFile, "save", original)
    assert admin.main(["resume", "--out", str(path)], environ=env, store_factory=lambda *a, **kw: store) == 0
    assert len(store.state["receipts"]) == 1


def test_issue_retry_retains_one_identity_and_one_key(tmp_path):
    store = MemoryStore()
    store.lose_after = "create_account"
    out = tmp_path / "reviewer.json"
    env = {"PREFLIGHT_TEAM_DATABASE_URL": DSN, "PREFLIGHT_TEAM_OWNER_KEY": store.owner}
    assert admin.main(["issue", "--name", "검토 담당", "--role", "reviewer", "--out", str(out)], environ=env, store_factory=lambda *a, **kw: store) != 0
    before = json.loads(out.read_text())
    assert admin.main(["resume", "--out", str(out)], environ=env, store_factory=lambda *a, **kw: store) == 0
    after = json.loads(out.read_text())
    assert before["access_key"] == after["access_key"]
    assert before["payload"]["account_id"] == after["payload"]["account_id"]
    assert len(store.state["accounts"]) == 2
    assert len(store.state["receipts"]) == 1
    assert team.authenticate(store.state, after["access_key"])["role"] == "reviewer"


def test_resume_old_issue_does_not_resurrect_a_replaced_key():
    store = MemoryStore()
    record = operation("issue", name="검토 담당", role="reviewer")
    admin.execute_operation(record, store, store.owner)
    replacement = operation("replace", account_id=record["payload"]["account_id"])
    admin.execute_operation(replacement, store, store.owner)
    assert admin.execute_operation(record, store, store.owner)["status"] == "superseded"
    with pytest.raises(team.AuthenticationError):
        team.authenticate(store.state, record["access_key"])


def test_owner_file_can_supply_new_key_and_resume_without_old_key(tmp_path):
    store = MemoryStore()
    record = operation("rotate-owner")
    admin.execute_operation(record, store, store.owner)
    assert admin.execute_operation(record, store, None)["status"] == "confirmed"
    path = private_text(tmp_path / "owner.json", json.dumps(record))
    assert admin.read_access_key({}, file=path) == record["access_key"]


def test_reviewer_cannot_issue_or_rotate_keys():
    store = MemoryStore()
    issue = operation("issue", name="검토 담당", role="reviewer")
    admin.execute_operation(issue, store, store.owner)
    before = deepcopy(store.state)
    with pytest.raises(team.PermissionDenied):
        admin.execute_operation(operation("rotate-owner"), store, issue["access_key"])
    assert store.state == before


def test_security_rotation_works_when_normal_admission_capacity_is_exhausted(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(team, "MAX_RECEIPTS", 0)
    monkeypatch.setattr(team, "MAX_STATE_BYTES", 1)
    assert admin.execute_operation(operation("rotate-owner"), store, store.owner)["status"] == "confirmed"


def test_bootstrap_existing_workspace_is_never_replaced():
    state, _ = state_with_work()
    original = deepcopy(state)
    with pytest.raises(admin.AdminError) as caught:
        admin.apply_admin(state, operation("bootstrap"), database_user="db_operator", now=NOW)
    assert caught.value.code == "workspace_exists"
    assert state == original


def test_bootstrap_retry_preserves_data_and_reuses_receipt():
    record = operation("bootstrap")
    state, first = admin.apply_admin(None, record, database_user="db_operator", now=NOW)
    changed, repeat = admin.apply_admin(state, record, database_user="db_operator", now=NOW)
    assert changed == state
    assert first == repeat
    assert len(state["receipts"]) == 1
    assert team.authenticate(state, record["access_key"], now=NOW)["id"] == team.OWNER_ID


def test_recovery_preserves_work_revokes_every_previous_owner_key_and_audits():
    state, old = state_with_work()
    old2 = team.new_access_key()
    state["keys"][team.key_digest(old2)] = {"account_id": team.OWNER_ID, "expires_at": (NOW + timedelta(days=90)).isoformat()}
    queue, accounts, receipts = state["queue"], deepcopy(state["accounts"]), deepcopy(state["receipts"])
    record = operation("recover-owner", expected_revision=state["revision"], reason="담당 관리자 키 유실")
    recovered, result = admin.apply_admin(state, record, database_user="db_operator", now=NOW)
    assert recovered["queue"] == queue
    assert recovered["accounts"] == accounts
    assert all(recovered["receipts"][k] == v for k, v in receipts.items())
    assert len(recovered["keys"]) == 1
    assert result["status"] == "confirmed"
    assert result["revision"] == state["revision"] + 1
    for old_key in (old, old2):
        with pytest.raises(team.AuthenticationError):
            team.authenticate(recovered, old_key, now=NOW)
    assert team.authenticate(recovered, record["access_key"], now=NOW)["role"] == "owner"
    receipt = recovered["receipts"]["database_admin:" + record["request_id"]]
    assert receipt["authority"] == "database_admin"
    assert receipt["database_user"] == "db_operator"
    assert receipt["revoked_owner_keys"] == 2
    assert receipt["reason"] == "담당 관리자 키 유실"
    assert record["access_key"] not in json.dumps(recovered)


def test_recovery_of_expired_owner_and_inactive_owner_keeps_identity():
    state, _ = state_with_work()
    state["accounts"][team.OWNER_ID]["active"] = False
    record = operation("recover-owner", expected_revision=state["revision"], reason="만료 키 복구")
    recovered, _ = admin.apply_admin(state, record, database_user="db_operator", now=NOW + timedelta(days=100))
    assert recovered["accounts"][team.OWNER_ID]["created_at"] == state["accounts"][team.OWNER_ID]["created_at"]
    assert team.authenticate(recovered, record["access_key"], now=NOW + timedelta(days=100))["id"] == team.OWNER_ID


def test_concurrent_recovery_revision_conflict_cannot_silently_replace_first_key():
    state, _ = state_with_work()
    left = operation("recover-owner", expected_revision=state["revision"], reason="왼쪽 복구")
    right = operation("recover-owner", expected_revision=state["revision"], reason="오른쪽 복구")
    recovered, _ = admin.apply_admin(state, left, database_user="db_operator", now=NOW)
    with pytest.raises(admin.AdminError) as caught:
        admin.apply_admin(recovered, right, database_user="db_operator", now=NOW)
    assert caught.value.code == "revision_conflict"
    assert team.authenticate(recovered, left["access_key"], now=NOW)["role"] == "owner"


def test_recovery_retry_requires_same_id_and_fingerprint_and_never_resurrects():
    state, _ = state_with_work()
    first = operation("recover-owner", expected_revision=state["revision"], reason="최초 복구")
    recovered, result = admin.apply_admin(state, first, database_user="db_operator", now=NOW)
    retry, repeated = admin.apply_admin(recovered, first, database_user="db_operator", now=NOW)
    assert retry == recovered and result == repeated
    altered = deepcopy(first)
    altered["reason"] = "다른 요청"
    with pytest.raises(admin.AdminError):
        admin.apply_admin(recovered, altered, database_user="db_operator", now=NOW)
    second = operation("recover-owner", expected_revision=recovered["revision"], reason="후속 복구")
    latest, _ = admin.apply_admin(recovered, second, database_user="db_operator", now=NOW)
    unchanged, old_result = admin.apply_admin(latest, first, database_user="db_operator", now=NOW)
    assert unchanged == latest
    assert old_result["status"] == "superseded"
    with pytest.raises(team.AuthenticationError):
        team.authenticate(unchanged, first["access_key"], now=NOW)


def test_recovery_receipt_rejects_changed_valid_command_with_same_request_id():
    state, _ = state_with_work()
    first = operation("recover-owner", expected_revision=state["revision"], reason="최초 요청")
    recovered, _ = admin.apply_admin(state, first, database_user="db_operator", now=NOW)
    other = operation("recover-owner", expected_revision=state["revision"], reason="다른 요청")
    other["request_id"] = first["request_id"]
    with pytest.raises(admin.AdminError) as caught:
        admin.apply_admin(recovered, other, database_user="db_operator", now=NOW)
    assert caught.value.code == "request_conflict"


def test_recovery_keeps_reviewer_keys_accounts_and_receipts():
    state, owner = state_with_work()
    reviewer_key = team.new_access_key()
    aid = "acct_" + uuid4().hex
    state, _ = team.execute(state, owner, "create_account", {"account_id": aid, "name": "기존 검수자", "role": "reviewer",
                            "key_digest": team.key_digest(reviewer_key)}, request_id=str(uuid4()), now=NOW)
    state, _ = team.execute(state, reviewer_key, "claim", {}, request_id=str(uuid4()), now=NOW)
    record = operation("recover-owner", expected_revision=state["revision"], reason="관리자 복구")
    updated, _ = admin.apply_admin(state, record, database_user="db_operator", now=NOW)
    assert updated["queue"] == state["queue"]
    assert updated["accounts"] == state["accounts"]
    assert updated["keys"][team.key_digest(reviewer_key)] == state["keys"][team.key_digest(reviewer_key)]
    assert all(updated["receipts"][rid] == receipt for rid, receipt in state["receipts"].items())


class MemoryAdmin:
    def __init__(self):
        self.state = None
        self.lose_after_commit = False
        self.calls = []

    def apply(self, record):
        self.calls.append(deepcopy(record))
        self.state, result = admin.apply_admin(self.state, record, database_user="controlled_test_database_user")
        if self.lose_after_commit:
            self.lose_after_commit = False
            raise StorageUnavailable("private-dsn-and-password")
        return result


@pytest.mark.parametrize("kind", ["bootstrap", "recover-owner"])
def test_privileged_commit_response_loss_retries_same_saved_key_and_hash(tmp_path, capsys, kind):
    database = MemoryAdmin()
    extra = []
    if kind == "recover-owner":
        database.state, _ = state_with_work()
        extra = ["--expected-revision", str(database.state["revision"]), "--reason", "통제된 복구 테스트"]
    database.lose_after_commit = True
    out = tmp_path / "operation.json"
    env = {"PREFLIGHT_TEAM_ADMIN_DATABASE_URL": DSN}
    args = [kind, "--database-admin", "--out", str(out), *extra]
    assert admin.main(args, environ=env, admin_factory=lambda *a, **kw: database) == 4
    saved = json.loads(out.read_text())
    assert saved["status"] == "prepared"
    revision = database.state["revision"]
    assert admin.main(["resume", "--database-admin", "--out", str(out)], environ=env, admin_factory=lambda *a, **kw: database) == 0
    assert database.state["revision"] == revision
    assert database.calls[0]["request_id"] == database.calls[1]["request_id"]
    assert database.calls[0]["fingerprint"] == database.calls[1]["fingerprint"]
    assert database.calls[0]["access_key"] == database.calls[1]["access_key"]
    output = capsys.readouterr()
    assert saved["access_key"] not in output.out + output.err
    assert "private-dsn-and-password" not in output.out + output.err


def test_admin_resume_requires_explicit_flag_without_automatic_database_access(tmp_path):
    database = MemoryAdmin()
    out = tmp_path / "bootstrap.json"
    record = operation("bootstrap")
    private_text(out, json.dumps(record))
    assert admin.main(["resume", "--out", str(out)], environ={"PREFLIGHT_TEAM_ADMIN_DATABASE_URL": DSN},
                      admin_factory=lambda *a, **kw: database) != 0
    assert database.calls == []


def test_database_binding_allows_password_change_but_rejects_another_target():
    first = admin.database_binding(DSN, "preflight_team", {})
    assert first == admin.database_binding(DSN.replace("never-print-this-password", "changed-password"), "preflight_team", {})
    assert first != admin.database_binding(DSN.replace("/test_admin", "/other_workspace"), "preflight_team", {})


def test_normal_database_and_owner_inputs_can_use_private_files(tmp_path):
    store = MemoryStore()
    database = private_text(tmp_path / "database.secret", DSN)
    key = private_text(tmp_path / "owner.secret", store.owner)
    out = tmp_path / "adjudicator.json"
    assert admin.main(["issue", "--database-file", str(database), "--owner-key-file", str(key),
                       "--role", "adjudicator", "--name", "조정 담당", "--out", str(out)],
                      environ={}, store_factory=lambda *a, **kw: store) == 0
    assert team.authenticate(store.state, json.loads(out.read_text())["access_key"])["role"] == "adjudicator"


@pytest.mark.parametrize("field,value", [("schema_version", 99), ("revision", "1"), ("accounts", []), ("receipts", [])])
def test_recovery_rejects_invalid_existing_state_without_repairing_it(field, value):
    state, _ = state_with_work()
    revision = state["revision"]
    state[field] = value
    before = deepcopy(state)
    with pytest.raises(admin.AdminError):
        admin.apply_admin(state, operation("recover-owner", expected_revision=revision, reason="복구"), database_user="db_operator", now=NOW)
    assert state == before


def test_privileged_commands_require_explicit_flag_and_revision(tmp_path, capsys):
    secret = team.new_access_key()
    for args in (["recover-owner", "--out", str(tmp_path / "recovery.json")],
                 ["bootstrap", "--out", str(tmp_path / "bootstrap.json")],
                 ["rotate-owner", "--key", secret, "--out", str(tmp_path / "owner.json")]):
        assert admin.main(args, environ={}) != 0
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_main_sanitizes_type_errors_and_never_echoes_credentials(tmp_path, capsys):
    store = MemoryStore()
    def broken(*args, **kwargs):
        raise TypeError("invalid " + DSN + " " + store.owner)
    store.execute = broken
    assert admin.main(["rotate-owner", "--out", str(tmp_path / "owner.json")],
                      environ={"PREFLIGHT_TEAM_DATABASE_URL": DSN, "PREFLIGHT_TEAM_OWNER_KEY": store.owner},
                      store_factory=lambda *a, **kw: store) != 0
    output = capsys.readouterr()
    assert DSN not in output.out + output.err
    assert store.owner not in output.out + output.err


def test_resume_rejects_other_database_binding_before_network(tmp_path):
    store = MemoryStore()
    record = operation("rotate-owner")
    path = private_text(tmp_path / "owner.json", json.dumps(record))
    assert admin.main(["resume", "--out", str(path)], environ={"PREFLIGHT_TEAM_DATABASE_URL": DSN},
                      store_factory=lambda *a, **kw: store) != 0
    assert store.calls == []


@pytest.fixture
def admin_database():
    dsn = os.environ.get("PREFLIGHT_TEAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration DSN not configured; CI supplies an isolated database")
    schema = "team_admin_test_" + uuid4().hex
    db = admin.AdminDatabase(dsn, schema=schema)
    yield db, schema
    if os.environ.get("PREFLIGHT_KEEP_TEST_SCHEMAS") != "1":
        from psycopg import sql
        with db._connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_bootstrap_recovery_and_duplicate_receipts(admin_database):
    db, schema = admin_database
    boot = admin.new_operation("bootstrap", schema=schema, binding=BINDING)
    result = db.apply(boot)
    assert db.apply(boot) == result
    assert db.inspect()["revision"] == result["revision"]
    recovery = admin.new_operation("recover-owner", schema=schema, binding=BINDING,
                                   expected_revision=result["revision"], reason="통제된 PostgreSQL 검증")
    first = db.apply(recovery)
    assert db.apply(recovery) == first
    assert first["revision"] == result["revision"] + 1


def test_postgres_concurrent_recovery_has_one_winner(admin_database):
    db, schema = admin_database
    boot = db.apply(admin.new_operation("bootstrap", schema=schema, binding=BINDING))
    records = [admin.new_operation("recover-owner", schema=schema, binding=BINDING,
                                   expected_revision=boot["revision"], reason=f"통제된 동시 복구 {n}") for n in range(2)]
    def invoke(record):
        try:
            return db.apply(record)["status"]
        except admin.AdminError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, records))
    assert sorted(results) == ["confirmed", "revision_conflict"]


def test_postgres_revision_column_mismatch_blocks_recovery(admin_database):
    from psycopg import sql
    db, schema = admin_database
    boot = db.apply(admin.new_operation("bootstrap", schema=schema, binding=BINDING))
    with db._connect() as conn:
        conn.execute(sql.SQL("UPDATE {}.workspace SET revision = revision + 1").format(sql.Identifier(schema)))
    record = admin.new_operation("recover-owner", schema=schema, binding=BINDING,
                                 expected_revision=boot["revision"], reason="불일치 거절 검증")
    with pytest.raises(admin.AdminError) as caught:
        db.apply(record)
    assert caught.value.code == "invalid_workspace"
