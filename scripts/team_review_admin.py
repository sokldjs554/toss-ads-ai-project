"""Explicit, server-side credential administration. Never print credentials.

Run as ``python -m scripts.team_review_admin``. The private operation file is
durable BEFORE any key mutation; ``resume`` reuses its key and request ID.
Only bootstrap/recovery/inspection/API installation use the explicitly selected DB-admin path.
Application/UI startup must never invoke this module automatically.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from uuid import UUID, uuid4, uuid5

from ops import team_review as team
from ops.labeling import validate_annotator_name
from ops.team_review_store import PostgresTeamStore, StorageBusy, StorageUnavailable

FORMAT = "preflight-team-admin-operation-v1"
MAX_FILE_BYTES = 65_536
ADMIN_OPERATIONS = {"bootstrap", "recover-owner"}
REGULAR_OPERATIONS = {"issue", "replace", "rotate-owner"}
ACTION = {"issue": "create_account", "replace": "replace_key", "rotate-owner": "replace_key"}


class AdminError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _timestamp(now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise AdminError("invalid_time")
    return now.astimezone(timezone.utc)


def _checked_fd(fd):
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or metadata.st_size > MAX_FILE_BYTES):
        raise AdminError("insecure_file")


def _read_fd(fd):
    _checked_fd(fd)
    with os.fdopen(fd, "rb", closefd=False) as file:
        value = file.read(MAX_FILE_BYTES + 1)
    if len(value) > MAX_FILE_BYTES:
        raise AdminError("invalid_file")
    return value


def read_private_file(path) -> bytes:
    """Accept only owned, single-link regular 0600 files; never follow a symlink."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        return _read_fd(fd)
    except OSError:
        raise AdminError("insecure_file") from None
    finally:
        if fd is not None:
            os.close(fd)


class OperationFile:
    """Per-path process lock plus atomic, fsynced 0600 writes in an owned directory.

    The lock file intentionally remains: unlinking a lock permits two processes
    to lock different inodes. The key file is never overwritten by a new command.
    """
    def __init__(self, path):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.directory_fd = None
        self.lock_fd = None
        self._contents = None

    def __enter__(self):
        try:
            self.directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            metadata = os.fstat(self.directory_fd)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise AdminError("insecure_directory")
            self.lock_fd = os.open(self.path.name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                   0o600, dir_fd=self.directory_fd)
            _checked_fd(self.lock_fd)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AdminError("operation_busy") from None
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None

    def _read(self):
        fd = None
        try:
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.directory_fd)
            return _read_fd(fd)
        except OSError:
            raise AdminError("insecure_file") from None
        finally:
            if fd is not None:
                os.close(fd)

    def load(self):
        self._contents = self._read()
        try:
            return json.loads(self._contents)
        except (ValueError, UnicodeError):
            raise AdminError("invalid_file") from None

    def create(self, value):
        self._write(value, replace=False)

    def save(self, value):
        if self._contents is None or self._read() != self._contents:
            raise AdminError("operation_file_changed")
        self._write(value, replace=True)

    def _write(self, value, *, replace):
        data = _json(value)
        if len(data) > MAX_FILE_BYTES:
            raise AdminError("invalid_file")
        temporary = self.path.name + ".tmp." + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.directory_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as file:
                file.write(data)
                file.flush()
                os.fsync(fd)
            if replace:
                os.replace(temporary, self.path.name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
            else:
                # Atomic no-clobber publication, unlike replace().
                try:
                    os.link(temporary, self.path.name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd, follow_symlinks=False)
                except FileExistsError:
                    raise AdminError("result_file_exists") from None
                os.unlink(temporary, dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
            self._contents = data
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass


def _environment_value(environ, name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise AdminError("invalid_environment_name")
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AdminError("missing_secret_input")
    return value.strip()


def read_access_key(environ, *, file=None, env_name="PREFLIGHT_TEAM_OWNER_KEY", optional=False):
    if file is None and optional and not environ.get(env_name):
        return None
    value = read_private_file(file).decode("utf-8").strip() if file is not None else _environment_value(environ, env_name)
    if value.startswith("{"):
        try:
            value = json.loads(value)["access_key"]
        except (ValueError, KeyError, TypeError):
            raise AdminError("invalid_key_file") from None
    team.key_digest(value)
    return value


def database_binding(dsn, schema, environ):
    """Bind retries to connection identity, excluding password/TLS key material."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        supplied = conninfo_to_dict(dsn)
        values = {name: supplied.get(name, environ.get(env, "")) for name, env in {
            "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT",
            "dbname": "PGDATABASE", "user": "PGUSER", "service": "PGSERVICE",
        }.items()}
        values["schema"] = schema
        return hashlib.sha256(_json(values)).hexdigest()
    except Exception:
        # libpq parse errors can quote connection strings.
        raise AdminError("invalid_database_input") from None


def _fingerprint(record):
    return hashlib.sha256(_json({name: record[name] for name in (
        "operation", "schema", "payload", "expected_revision", "reason",
    )})).hexdigest()


def new_operation(kind, *, schema, binding, name=None, role=None, account_id=None, expected_revision=None, reason=None):
    if kind not in ADMIN_OPERATIONS | REGULAR_OPERATIONS:
        raise AdminError("invalid_operation")
    access_key = team.new_access_key()
    payload = {"key_digest": team.key_digest(access_key)}
    if kind == "issue":
        try:
            name = validate_annotator_name(name)
        except (TypeError, ValueError):
            raise AdminError("invalid_account") from None
        if role not in {"reviewer", "adjudicator"}:
            raise AdminError("invalid_account")
        payload.update(account_id="acct_" + uuid4().hex, name=name, role=role)
    elif kind == "replace":
        if not isinstance(account_id, str) or not re.fullmatch(r"acct_[0-9a-f]{32}", account_id):
            raise AdminError("invalid_account")
        payload["account_id"] = account_id
    else:
        payload["account_id"] = team.OWNER_ID
    if kind == "recover-owner":
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240:
            raise AdminError("invalid_recovery_request")
        reason = reason.strip()
    elif expected_revision is not None or reason is not None:
        raise AdminError("invalid_operation")
    record = {"format": FORMAT, "operation": kind, "schema": schema, "database_binding": binding,
              "request_id": str(uuid4()), "access_key": access_key, "payload": payload,
              "expected_revision": expected_revision, "reason": reason,
              "created_at": _timestamp().isoformat(), "status": "prepared"}
    record["fingerprint"] = _fingerprint(record)
    validate_operation(record)
    return record


def validate_operation(record):
    try:
        required = {"format", "operation", "schema", "database_binding", "request_id", "access_key", "payload",
                    "expected_revision", "reason", "created_at", "status", "fingerprint"}
        if (not isinstance(record, dict) or not required <= record.keys() or set(record) - required - {"result"}
                or record["format"] != FORMAT or record["operation"] not in ADMIN_OPERATIONS | REGULAR_OPERATIONS
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", record["schema"])
                or not re.fullmatch(r"[0-9a-f]{64}", record["database_binding"])
                or str(UUID(record["request_id"])) != record["request_id"]
                or record["status"] not in {"prepared", "confirmed", "superseded", "applied_inactive"}
                or datetime.fromisoformat(record["created_at"]).tzinfo is None):
            raise ValueError
        p, kind = record["payload"], record["operation"]
        expected = {"account_id", "key_digest", "name", "role"} if kind == "issue" else {"account_id", "key_digest"}
        if not isinstance(p, dict) or set(p) != expected or p["key_digest"] != team.key_digest(record["access_key"]):
            raise ValueError
        if kind in ADMIN_OPERATIONS | {"rotate-owner"}:
            if p["account_id"] != team.OWNER_ID:
                raise ValueError
        elif not re.fullmatch(r"acct_[0-9a-f]{32}", p["account_id"]):
            raise ValueError
        if kind == "issue" and (p["role"] not in {"reviewer", "adjudicator"} or validate_annotator_name(p["name"]) != p["name"]):
            raise ValueError
        if kind == "recover-owner":
            if (type(record["expected_revision"]) is not int or record["expected_revision"] < 0
                    or not isinstance(record["reason"], str) or not 1 <= len(record["reason"].strip()) <= 240):
                raise ValueError
        elif record["expected_revision"] is not None or record["reason"] is not None:
            raise ValueError
        if record["fingerprint"] != _fingerprint(record):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AdminError("invalid_operation_file") from None


def execute_operation(record, store, owner_key):
    """Normal operations remain subject to the application's owner checks."""
    validate_operation(record)
    kind = record["operation"]
    if kind not in REGULAR_OPERATIONS:
        raise AdminError("database_admin_required")
    candidates = [owner_key] if owner_key else []
    if kind == "rotate-owner" and record["access_key"] not in candidates:
        candidates.append(record["access_key"])
    if not candidates:
        raise AdminError("missing_secret_input")
    result = None
    for index, key in enumerate(candidates):
        try:
            result = store.execute(key, ACTION[kind], record["payload"], request_id=record["request_id"])
            break
        except team.AuthenticationError:
            if index == len(candidates) - 1:
                raise
    # A saved receipt may precede a later replacement. Verify the saved new key
    # still authenticates; replay must never make an old key active again.
    status = "confirmed"
    try:
        check = store.execute(record["access_key"], "overview", {}, request_id=str(uuid5(UUID(record["request_id"]), "verify-issued-key")))
        if check["account"]["id"] != record["payload"]["account_id"]:
            raise AdminError("invalid_response")
    except team.AuthenticationError:
        status = "superseded"
        if owner_key and kind != "rotate-owner":
            accounts = store.execute(owner_key, "accounts", {}, request_id=str(uuid4()))["accounts"]
            target = next((a for a in accounts if a["id"] == record["payload"]["account_id"]), None)
            if target and not target["active"]:
                status = "applied_inactive"
    return {"status": status, "account_id": result["account_id"], "revision": result["revision"]}


def _validate_state(state):
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1
            or type(state.get("revision")) is not int or state["revision"] < 0
            or not isinstance(state.get("queue"), str) or not isinstance(state.get("accounts"), dict)
            or not isinstance(state.get("keys"), dict) or not isinstance(state.get("receipts"), dict)):
        raise AdminError("invalid_workspace")
    owner = state["accounts"].get(team.OWNER_ID)
    if not isinstance(owner, dict) or owner.get("id") != team.OWNER_ID or owner.get("role") != "owner":
        raise AdminError("invalid_workspace")
    if any(not isinstance(account, dict) or account.get("id") != aid or account.get("role") not in team.ROLES
           for aid, account in state["accounts"].items()):
        raise AdminError("invalid_workspace")
    try:
        for digest, key in state["keys"].items():
            if (not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(key, dict)
                    or key.get("account_id") not in state["accounts"]
                    or datetime.fromisoformat(key["expires_at"]).tzinfo is None):
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise AdminError("invalid_workspace") from None


def _row_state(row):
    if row is None:
        return None
    if len(row) != 2 or type(row[0]) is not int:
        raise AdminError("invalid_workspace")
    _validate_state(row[1])
    if row[0] != row[1]["revision"]:
        raise AdminError("invalid_workspace")
    return row[1]


def _admin_result(state, receipt, record, now):
    try:
        team.authenticate(state, record["access_key"], now=now)
        status = "confirmed"
    except team.AuthenticationError:
        status = "superseded"
    return {**deepcopy(receipt["result"]), "status": status, "current_revision": state["revision"]}


def apply_admin(state, record, *, database_user, now=None):
    """Pure privileged transition; caller MUST hold the database row lock.

    This is deliberately not registered as an application/UI action. It requires
    explicit direct database authority in AdminDatabase below.
    """
    validate_operation(record)
    if record["operation"] not in ADMIN_OPERATIONS:
        raise AdminError("invalid_operation")
    now = _timestamp(now)
    receipt_id = "database_admin:" + record["request_id"]
    if state is not None:
        _validate_state(state)
        previous = state["receipts"].get(receipt_id)
        if previous is not None:
            if not isinstance(previous, dict) or previous.get("fingerprint") != record["fingerprint"]:
                raise AdminError("request_conflict")
            return state, _admin_result(state, previous, record, now)
    if record["operation"] == "bootstrap":
        if state is not None:
            raise AdminError("workspace_exists")
        updated = team.new_state(record["payload"]["key_digest"], now=now)
        previous_revision, revoked = 0, 0
    else:
        if state is None:
            raise AdminError("workspace_missing")
        if state["revision"] != record["expected_revision"]:
            raise AdminError("revision_conflict")
        digest = record["payload"]["key_digest"]
        if digest in state["keys"]:
            raise AdminError("key_already_installed")
        updated = deepcopy(state)
        owners = {aid for aid, account in updated["accounts"].items() if account["role"] == "owner"}
        retained = {key: value for key, value in updated["keys"].items() if value["account_id"] not in owners}
        revoked = len(updated["keys"]) - len(retained)
        updated["keys"] = retained
        updated["keys"][digest] = {"account_id": team.OWNER_ID, "expires_at": (now + timedelta(days=90)).isoformat()}
        updated["accounts"][team.OWNER_ID]["active"] = True
        previous_revision = state["revision"]
    updated["revision"] = previous_revision + 1
    receipt = {"fingerprint": record["fingerprint"], "action": record["operation"], "at": now.isoformat(),
               "authority": "database_admin", "database_user": database_user,
               "reason": record["reason"] or "explicit_initial_bootstrap", "previous_revision": previous_revision,
               "revoked_owner_keys": revoked,
               "result": {"account_id": team.OWNER_ID, "revision": updated["revision"],
                          "expires_at": updated["keys"][record["payload"]["key_digest"]]["expires_at"]}}
    updated["receipts"][receipt_id] = receipt
    return updated, _admin_result(updated, receipt, record, now)


class AdminDatabase:
    """Only the explicitly invoked CLI creates this privileged adapter."""
    def __init__(self, database_url, *, schema="preflight_team"):
        PostgresTeamStore(database_url, schema=schema)  # Shared identifier validation.
        self._database_url, self.schema = database_url, schema

    def __repr__(self):
        return "AdminDatabase(<configured>)"

    def _connect(self):
        import psycopg
        return psycopg.connect(self._database_url, connect_timeout=10, prepare_threshold=None)

    def inspect(self):
        import psycopg
        from psycopg import sql
        try:
            with self._connect() as conn:
                conn.execute("SET LOCAL statement_timeout = '15s'")
                row = conn.execute(sql.SQL("SELECT revision, document FROM {}.workspace WHERE singleton = true").format(sql.Identifier(self.schema))).fetchone()
                state = _row_state(row)
                if state is None:
                    raise AdminError("workspace_missing")
                return {"schema_version": state["schema_version"], "revision": state["revision"],
                        "accounts": len(state["accounts"]), "receipts": len(state["receipts"]),
                        "owner_active": state["accounts"][team.OWNER_ID].get("active") is True,
                        "owner_key_expiries": [value["expires_at"] for value in state["keys"].values() if value["account_id"] == team.OWNER_ID]}
        except psycopg.Error:
            raise StorageUnavailable("공동 저장 상태를 확인하지 못했습니다.") from None

    def configure_api(self, runtime_role, *, install=False):
        """Explicit privileged CLI only; never creates a runtime LOGIN/password."""
        import psycopg
        from ops import team_review_db_api
        try:
            with self._connect() as conn:
                conn.execute("SET LOCAL statement_timeout = '15s'")
                operation = team_review_db_api.install_api if install else team_review_db_api.verify_api
                result = operation(conn, schema=self.schema, runtime_role=runtime_role)
            return result
        except ValueError:
            raise AdminError("api_configuration_invalid") from None
        except psycopg.Error:
            raise StorageUnavailable("DB API 설정 결과를 확인하지 못했습니다.") from None

    def apply(self, record):
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb

        validate_operation(record)
        if record["schema"] != self.schema or record["operation"] not in ADMIN_OPERATIONS:
            raise AdminError("invalid_operation")
        try:
            with self._connect() as conn:
                conn.execute("SELECT set_config('lock_timeout', %s, true)", ("3000ms",))
                conn.execute("SET LOCAL statement_timeout = '15s'")
                if record["operation"] == "bootstrap":
                    # Match initialize()'s lock so legacy bootstrap cannot race.
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("preflight-team-init:" + self.schema,))
                    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.workspace (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), revision bigint NOT NULL, document jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())").format(sql.Identifier(self.schema)))
                row = conn.execute(sql.SQL("SELECT revision, document FROM {}.workspace WHERE singleton = true FOR UPDATE").format(sql.Identifier(self.schema))).fetchone()
                previous = _row_state(row)
                database_user = conn.execute("SELECT current_user").fetchone()[0]
                updated, result = apply_admin(previous, record, database_user=database_user)
                if previous is None:
                    conn.execute(sql.SQL("INSERT INTO {}.workspace(singleton, revision, document) VALUES(true, %s, %s)").format(sql.Identifier(self.schema)), (updated["revision"], Jsonb(updated)))
                elif updated != previous:
                    cursor = conn.execute(sql.SQL("UPDATE {}.workspace SET revision = %s, document = %s, updated_at = now() WHERE singleton = true AND revision = %s").format(sql.Identifier(self.schema)), (updated["revision"], Jsonb(updated), previous["revision"]))
                    if cursor.rowcount != 1:
                        raise AdminError("revision_conflict")
            # A context manager commit can itself lose the response. The caller
            # keeps its already-fsynced file and retries exactly this record.
            return result
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
            raise StorageBusy("다른 작업을 저장 중입니다. 같은 요청으로 다시 확인해 주세요.") from None
        except psycopg.Error:
            raise StorageUnavailable("저장 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Default argparse errors echo invalid argv values, potentially secrets.
        raise AdminError("invalid_arguments")


def _parser():
    parser = _Parser(description="공동 검수 키 관리. 비밀값은 환경 변수 또는 본인 소유 0600 파일로만 입력합니다.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("bootstrap", "inspect", "issue", "replace", "rotate-owner", "recover-owner", "resume", "install-api", "verify-api"):
        command = commands.add_parser(name)
        database = command.add_mutually_exclusive_group()
        database.add_argument("--database-env", metavar="ENV_NAME")
        database.add_argument("--database-file", type=Path)
        command.add_argument("--schema", default="preflight_team")
        command.add_argument("--out", type=Path, required=True)
        if name in ADMIN_OPERATIONS | {"inspect", "resume", "install-api", "verify-api"}:
            command.add_argument("--database-admin", action="store_true", required=name != "resume")
        if name in REGULAR_OPERATIONS | {"resume"}:
            key = command.add_mutually_exclusive_group()
            key.add_argument("--owner-key-env", default="PREFLIGHT_TEAM_OWNER_KEY", metavar="ENV_NAME")
            key.add_argument("--owner-key-file", type=Path)
        if name == "issue":
            command.add_argument("--name", required=True)
            command.add_argument("--role", choices=("reviewer", "adjudicator"), required=True)
        if name == "replace":
            command.add_argument("--account-id", required=True)
        if name == "recover-owner":
            command.add_argument("--expected-revision", type=int, required=True)
            command.add_argument("--reason", required=True)
        if name in {"install-api", "verify-api"}:
            command.add_argument("--runtime-role", required=True)
    return parser


def main(argv=None, *, environ=None, store_factory=None, admin_factory=None):
    """Return a status code. stdout contains only status and result-file path."""
    environ = os.environ if environ is None else environ
    store_factory = store_factory or PostgresTeamStore
    admin_factory = admin_factory or AdminDatabase
    output = None
    mutation_started = False
    try:
        args = _parser().parse_args(argv)
        output = str(Path(os.path.abspath(args.out)))
        with OperationFile(args.out) as file:
            record = file.load() if args.command == "resume" else None
            if record is not None:
                validate_operation(record)
            kind = record["operation"] if record is not None else args.command
            privileged = kind in ADMIN_OPERATIONS | {"inspect", "install-api", "verify-api"}
            if privileged and not getattr(args, "database_admin", False):
                raise AdminError("database_admin_required")
            if not privileged and getattr(args, "database_admin", False):
                raise AdminError("invalid_arguments")
            dsn = (read_private_file(args.database_file).decode("utf-8").strip() if args.database_file else
                   _environment_value(environ, args.database_env or ("PREFLIGHT_TEAM_ADMIN_DATABASE_URL" if privileged else "PREFLIGHT_TEAM_DATABASE_URL")))
            binding = database_binding(dsn, args.schema, environ)
            if record is not None and (record["schema"] != args.schema or record["database_binding"] != binding):
                raise AdminError("database_binding_mismatch")
            if kind in {"install-api", "verify-api"}:
                report = {"format": "preflight-team-api-inspection-v1", "schema": args.schema,
                          "database_binding": binding, "runtime_role": args.runtime_role,
                          "created_at": _timestamp().isoformat(), "status": "prepared"}
                file.create(report)
                mutation_started = kind == "install-api"
                result = admin_factory(dsn, schema=args.schema).configure_api(args.runtime_role, install=mutation_started)
                status = ("api_installed" if mutation_started else "api_verified") if result.get("boundary_valid") is True else "api_invalid"
                report.update(status=status, result=result)
                file.save(report)
            elif kind == "inspect":
                result = admin_factory(dsn, schema=args.schema).inspect()
                file.create({"format": "preflight-team-admin-inspection-v1", "schema": args.schema,
                             "database_binding": binding, "created_at": _timestamp().isoformat(), **result})
                status = "inspected"
            else:
                owner_key = None if privileged else read_access_key(environ, file=args.owner_key_file, env_name=args.owner_key_env,
                                                                     optional=args.command == "resume" and kind == "rotate-owner")
                if record is None:
                    record = new_operation(kind, schema=args.schema, binding=binding, name=getattr(args, "name", None),
                                           role=getattr(args, "role", None), account_id=getattr(args, "account_id", None),
                                           expected_revision=getattr(args, "expected_revision", None), reason=getattr(args, "reason", None))
                    file.create(record)  # File and containing directory fsynced before mutation.
                else:
                    # A visible prepared file may follow a failed directory fsync.
                    # Re-establish file AND directory durability before retrying a key mutation.
                    file.save(record)
                mutation_started = True
                result = (admin_factory(dsn, schema=args.schema).apply(record) if privileged else
                          execute_operation(record, store_factory(dsn, schema=args.schema), owner_key))
                status = result["status"]
                record.update(status=status, result=result)
                file.save(record)
        print(json.dumps({"status": status, "result_file": output}, ensure_ascii=False))
        return 0 if status in {"confirmed", "inspected", "applied_inactive", "api_installed", "api_verified"} else 3
    except SystemExit as exc:  # --help is the only parser exit; errors are sanitized above.
        return int(exc.code or 0)
    except AdminError as exc:
        status = exc.code
    except team.AuthenticationError:
        status = "authentication_failed"
    except team.PermissionDenied:
        status = "permission_denied"
    except team.CapacityReached:
        status = "capacity_reached"
    except team.RequestConflict:
        status = "request_conflict"
    except team.TeamReviewError:
        status = "invalid_command"
    except StorageUnavailable:
        status = "outcome_unknown"
    except KeyboardInterrupt:
        status = "outcome_unknown" if mutation_started else "interrupted"
    except Exception:
        # Includes TypeError and all driver/JSON/OS messages; none are printed.
        status = "outcome_unknown" if mutation_started else "invalid_input"
    print(json.dumps({"status": status, "result_file": output}, ensure_ascii=False))
    return 4 if status == "outcome_unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())
