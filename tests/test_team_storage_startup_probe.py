"""The startup probe must never print credentials or mutate the workspace."""
import contextlib
import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch
import pytest

SPEC = importlib.util.spec_from_file_location("team_storage_probe", Path(__file__).parents[1] / "scripts" / "check_team_storage.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
URL = "postgresql://preflight_runtime:SECRET_SENTINEL@db.example/preflight?sslmode=require"

@pytest.mark.parametrize("value,status", [
    ("", "not_configured"),
    ("PREFLIGHT_TEAM_DATABASE_URL", "invalid_url"),
    ("psql '" + URL + "'", "command_not_url"),
    ("'" + URL + "'", "quoted_url"),
    ("\"" + URL + "\"", "quoted_url"),
    (" " + URL, "surrounding_whitespace"),
    (URL + "\n", "surrounding_whitespace"),
    (URL.replace("SECRET_SENTINEL", "[YOUR_PASSWORD]"), "password_placeholder"),
    (URL.replace("SECRET_SENTINEL", "%5BYOUR_PASSWORD%5D"), "password_placeholder"),
    (URL.replace("SECRET_SENTINEL", "********"), "password_placeholder"),
    (URL.replace("SECRET_SENTINEL", ""), "password_missing"),
    (URL.replace("preflight_runtime", "preflight_owner"), "unexpected_role"),
    (URL.replace("/preflight?", "/neondb?"), "unexpected_database"),
    (URL.replace("db.example", ""), "host_missing"),
    (URL.replace("require", "disable"), "tls_not_required"),
    (URL.replace("db.example", "db.example:bad"), "invalid_url"),
    (URL.replace("postgresql:", "https:"), "invalid_url"),
])
def test_configuration_rejects_unsafe_or_wrong_values_without_network(value, status):
    def forbidden(*a, **kw):
        raise AssertionError("configuration failures must not connect")
    with patch.dict(sys.modules, {"psycopg": types.SimpleNamespace(connect=forbidden)}):
        report = probe.diagnose(value)
    assert report["status"] == status
    assert "SECRET_SENTINEL" not in json.dumps(report)
    assert "postgresql://" not in json.dumps(report)

class DriverFailure(Exception):
    def __init__(self, sqlstate):
        super().__init__("SECRET_SENTINEL postgresql://private/password")
        self.sqlstate = sqlstate

@pytest.mark.parametrize("state,status", [
    ("28P01", "database_authentication_failed"),
    ("28000", "database_authentication_failed"),
    ("3D000", "database_missing"),
    ("42501", "permission_denied"),
    ("53300", "connection_capacity"),
    ("57014", "query_timeout"),
    ("08001", "connection_failed"),
    (None, "connection_failed"),
    ("SECRET_SENTINEL", "connection_failed"),
])
def test_driver_failure_reports_only_allowlisted_category(state, status):
    def fail(*a, **kw):
        raise DriverFailure(state)
    with patch.dict(sys.modules, {"psycopg": types.SimpleNamespace(connect=fail)}):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            report = probe.diagnose(URL)
    assert report["stage"] == "connect"
    assert report["status"] == status
    assert "SECRET_SENTINEL" not in json.dumps(report) + out.getvalue() + err.getvalue()

class FakeConnection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, statement):
        self.statements.append(statement)
        return self
    def fetchone(self):
        return next(self.rows)

@pytest.mark.parametrize("rows,status", [
    ([("preflight", "preflight_runtime"), (True, True), (True, True, False, False)], "connection_and_permissions_ok"),
    ([("other", "preflight_runtime")], "unexpected_connected_identity"),
    ([("preflight", "preflight_owner")], "unexpected_connected_identity"),
    ([("preflight", "preflight_runtime"), (False, True)], "api_functions_missing"),
    ([("preflight", "preflight_runtime"), (True, True), (False, True, False, False)], "permission_contract_failed"),
    ([("preflight", "preflight_runtime"), (True, True), (True, True, True, False)], "permission_contract_failed"),
])
def test_probe_uses_only_read_only_statements_and_checks_permissions(rows, status):
    conn = FakeConnection(rows)
    def connect(value, **kwargs):
        assert value == URL
        assert kwargs == {"connect_timeout": 10, "prepare_threshold": None}
        return conn
    with patch.dict(sys.modules, {"psycopg": types.SimpleNamespace(connect=connect)}):
        report = probe.diagnose(URL)
    assert report["status"] == status
    assert conn.statements[0] == "SET TRANSACTION READ ONLY"
    assert all(s.startswith(("SET ", "SELECT ")) for s in conn.statements)
    assert not any("FOR UPDATE" in s or "save_workspace(" in s and "has_function_privilege" not in s and "to_regprocedure" not in s for s in conn.statements)
    assert "SECRET_SENTINEL" not in json.dumps(report)

def test_main_always_emits_secret_free_json_and_does_not_fail_app_start():
    with patch.dict("os.environ", {"PREFLIGHT_TEAM_DATABASE_URL": "psql '" + URL + "'"}):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = probe.main()
    assert result == 0
    assert json.loads(out.getvalue())["status"] == "command_not_url"
    assert "SECRET_SENTINEL" not in out.getvalue()
