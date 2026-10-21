"""Operating verifier detects catalog drift without exposing credentials."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ops import team_review_db_api as api
from test_team_review_db_api import api_space  # Isolated CI PostgreSQL only.


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class _Catalog:
    """Catalog transport double; verify_api itself remains the real code."""
    schema = "preflight_team"
    runtime = "runtime_test"

    def __init__(self):
        self.owner_create = False
        self.functions = {}
        for oid, (name, args, definition) in enumerate(api.function_definitions(self.schema), 1):
            source = definition.split("AS $fn$", 1)[1].rsplit("$fn$;", 1)[0]
            self.functions[api._function_signature(self.schema, name, args)] = [
                name in {"lock_workspace", "save_workspace"},
                ["search_path=pg_catalog, pg_temp", "TimeZone=UTC"],
                api.owner_role(self.schema), False, source, "plpgsql", "v", "u", oid,
            ]
        self.surface = [(row[8],) for row in self.functions.values()]

    def execute(self, query, params=()):
        if query.startswith("SELECT rolcanlogin"):
            return _Rows([(True, False, False, False, False, False)
                          if params[0] == self.runtime else (False,) * 6])
        if query.startswith("SELECT EXISTS"):
            return _Rows([(False,)])
        if "has_table_privilege" in query or "has_any_column_privilege" in query:
            return _Rows([(False,)])
        if "has_schema_privilege" in query:
            return _Rows([("'USAGE'" in query or (params[0] == api.owner_role(self.schema) and self.owner_create),)])
        if query.startswith("SELECT p.prosecdef"):
            return _Rows([tuple(self.functions[params[0]])])
        if query.startswith("SELECT p.oid"):
            return _Rows(self.surface)
        if "has_function_privilege" in query:
            return _Rows([(self.functions[params[1]][0],)])
        if "current_setting" in query:
            return _Rows([("none" if params[0] == "log_statement" else "0",)])
        raise AssertionError("Unexpected catalog query")


def _verify(catalog):
    return api.verify_api(catalog, schema=catalog.schema, runtime_role=catalog.runtime)


def test_verifier_accepts_the_installed_catalog_contract():
    result = _verify(_Catalog())
    assert result["boundary_valid"] is True
    assert result["failure_reasons"] == []
    assert result["function_surface_matches"] is True
    assert result["function_owner_schema_create"] is False
    assert all(function["failure_reasons"] == [] for function in result["functions"])


@pytest.mark.parametrize("drift,reason", [
    ("timezone_missing", "timezone_not_utc"),
    ("timezone_changed", "timezone_not_utc"),
    ("volatility", "volatility_mismatch"),
    ("parallel", "parallel_mismatch"),
])
def test_verifier_rejects_drift_in_each_installed_function(drift, reason):
    catalog = _Catalog()
    for signature in catalog.functions:
        changed = deepcopy(catalog)
        row = changed.functions[signature]
        if drift.startswith("timezone"):
            row[1].remove("TimeZone=UTC")
            if drift == "timezone_changed":
                row[1].append("TimeZone=Asia/Seoul")
        elif drift == "volatility":
            row[6] = "s"
        else:
            row[7] = "s"
        result = _verify(changed)
        assert result["boundary_valid"] is False
        failed = [function for function in result["functions"] if not function["valid"]]
        assert len(failed) == 1
        assert reason in failed[0]["failure_reasons"]


def test_verifier_rejects_api_owner_schema_create():
    catalog = _Catalog()
    catalog.owner_create = True
    result = _verify(catalog)
    assert result["boundary_valid"] is False
    assert "function_owner_schema_create" in result["failure_reasons"]


@pytest.mark.parametrize("extra_oid", [8, 9], ids=["extra_function", "overload"])
def test_verifier_rejects_additional_function_surface(extra_oid):
    catalog = _Catalog()
    catalog.surface.append((extra_oid,))
    result = _verify(catalog)
    assert result["boundary_valid"] is False
    assert result["function_surface_matches"] is False
    assert "function_surface_mismatch" in result["failure_reasons"]


@pytest.mark.parametrize("alteration", [
    "RESET TimeZone", "SET TimeZone = 'Asia/Seoul'", "STABLE", "PARALLEL SAFE",
])
def test_postgres_verifier_rejects_function_metadata_drift(api_space, alteration):
    from psycopg import sql
    with api_space["admin_store"]._connect() as conn:
        conn.execute(sql.SQL("ALTER FUNCTION {}.lock_workspace(text,uuid,text) " + alteration)
                     .format(sql.Identifier(api_space["schema"])))
        result = api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
        assert result["boundary_valid"] is False
        # Installer restores the contract and its normal path remains usable.
        assert api.install_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])["boundary_valid"] is True


def test_postgres_verifier_rejects_owner_schema_create_and_installer_repairs(api_space):
    from psycopg import sql
    with api_space["admin_store"]._connect() as conn:
        conn.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}")
                     .format(sql.Identifier(api_space["schema"]), sql.Identifier(api_space["api_owner"])))
        result = api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
        assert result["boundary_valid"] is False
        assert "function_owner_schema_create" in result["failure_reasons"]
        assert api.install_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])["boundary_valid"] is True


@pytest.mark.parametrize("name,args", [("unexpected_api", ""), ("lock_workspace", "text")])
def test_postgres_verifier_rejects_extra_function_and_overload(api_space, name, args):
    from psycopg import sql
    with api_space["admin_store"]._connect() as conn:
        conn.execute(sql.SQL("CREATE FUNCTION {}.{}(" + args + ") RETURNS integer LANGUAGE sql AS 'SELECT 1'")
                     .format(sql.Identifier(api_space["schema"]), sql.Identifier(name)))
        result = api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
        assert result["boundary_valid"] is False
        assert "function_surface_mismatch" in result["failure_reasons"]
        # Never silently drop an unrecognized function during installation.
        with pytest.raises(ValueError, match="boundary verification failed"):
            api.install_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])
        conn.execute(sql.SQL("DROP FUNCTION {}.{}(" + args + ")")
                     .format(sql.Identifier(api_space["schema"]), sql.Identifier(name)))
        assert api.verify_api(conn, schema=api_space["schema"], runtime_role=api_space["runtime_role"])["boundary_valid"] is True


def test_public_neon_template_matches_current_source_and_frozen_function_sql():
    root = Path(__file__).resolve().parents[1]
    template = json.loads((root / "docs/deployment/team-review-neon-template.json").read_text())
    for path, expected in template["source_sha256"].items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected, path
    definitions = api.function_definitions(template["schema"])
    assert len(definitions) == len(template["functions"]) == 7
    for (name, args, definition), frozen in zip(definitions, template["functions"]):
        assert name == frozen["name"]
        assert api._function_signature(template["schema"], name, args) == frozen["signature"]
        assert hashlib.sha256(definition.encode()).hexdigest() == frozen["definition_sha256"]
        body = definition.split("AS $fn$", 1)[1].rsplit("$fn$;", 1)[0]
        assert hashlib.sha256(body.encode()).hexdigest() == frozen["body_sha256"]
        assert template["statements"].count(definition) == 1
    assert len(template["statements"]) == 51
    statements = "\n".join(template["statements"])
    assert set(template["placeholders"]) == {
        "__PREPARED_WORKSPACE_JSON_SQL_LITERAL__", "__NEON_RUNTIME_PASSWORD_SQL_LITERAL__",
    }
    for marker, contract in template["placeholders"].items():
        assert statements.count(marker) == contract["occurrences"]
    import re
    assert set(re.findall(r"__[A-Z_]+__", statements)) == set(template["placeholders"])
