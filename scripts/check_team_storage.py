"""Read-only startup diagnosis. Never emit a DSN, password, key, or exception text.

This probe runs once when the native container starts. It does not log in to the
review app, write a workspace, issue credentials, or change any configuration.
A diagnostic failure must not prevent the public demo from starting.
"""
from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, unquote, urlsplit


def _result(stage: str, status: str) -> dict[str, str]:
    return {"component": "preflight_team_storage", "stage": stage, "status": status}


def _configuration_issue(value: str) -> str | None:
    if not value:
        return "not_configured"
    if value != value.strip():
        return "surrounding_whitespace"
    if value.startswith("psql "):
        return "command_not_url"
    if value[0] in "\"'" or value[-1] in "\"'":
        return "quoted_url"
    # An unescaped placeholder can make urlsplit mistake brackets for IPv6.
    if any(":" + token + "@" in value.casefold() for token in ("[your_password]", "<your_password>", "[password]", "<password>")):
        return "password_placeholder"
    try:
        parts = urlsplit(value)
        _ = parts.port  # Validate the port without printing parser errors.
        if parts.scheme not in ("postgresql", "postgres"):
            return "invalid_url"
        if not parts.hostname:
            return "host_missing"
        if unquote(parts.username or "") != "preflight_runtime":
            return "unexpected_role"
        if unquote(parts.path) != "/preflight":
            return "unexpected_database"
        password = unquote(parts.password or "")
        if not password:
            return "password_missing"
        if password.casefold().strip("[]<>{}") in (
            "password", "your_password", "your_new_random_password", "password_here",
        ) or set(password) == {"*"}:
            return "password_placeholder"
        sslmode = parse_qs(parts.query).get("sslmode", [""])[-1]
        if sslmode in ("disable", "allow", "prefer"):
            return "tls_not_required"
    except (TypeError, ValueError):
        return "invalid_url"
    return None


def diagnose(value: str) -> dict[str, str]:
    issue = _configuration_issue(value)
    if issue:
        return _result("configuration", issue)
    try:
        import psycopg
    except ImportError:
        return _result("driver", "driver_unavailable")
    stage = "connect"
    try:
        with psycopg.connect(value, connect_timeout=10, prepare_threshold=None) as conn:
            stage = "permissions"
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            identity = conn.execute("SELECT current_database(), current_user").fetchone()
            if identity != ("preflight", "preflight_runtime"):
                return _result(stage, "unexpected_connected_identity")
            functions = conn.execute(
                "SELECT to_regprocedure('preflight_team.lock_workspace(text,uuid,text)') IS NOT NULL, "
                "to_regprocedure('preflight_team.save_workspace(text,uuid,text,bigint,jsonb)') IS NOT NULL"
            ).fetchone()
            if functions != (True, True):
                return _result(stage, "api_functions_missing")
            privileges = conn.execute(
                "SELECT has_function_privilege(current_user, 'preflight_team.lock_workspace(text,uuid,text)', 'EXECUTE'), "
                "has_function_privilege(current_user, 'preflight_team.save_workspace(text,uuid,text,bigint,jsonb)', 'EXECUTE'), "
                "has_table_privilege(current_user, 'preflight_team.workspace', 'SELECT'), "
                "has_schema_privilege(current_user, 'preflight_team', 'CREATE')"
            ).fetchone()
            if privileges != (True, True, False, False):
                return _result(stage, "permission_contract_failed")
        return _result("complete", "connection_and_permissions_ok")
    except Exception as exc:
        # SQLSTATE is only compared to constants. Never echo untrusted values,
        # exception class/message, DSNs, driver diagnostics, or tracebacks.
        status = {
            "28P01": "database_authentication_failed",
            "28000": "database_authentication_failed",
            "3D000": "database_missing",
            "42501": "permission_denied",
            "53300": "connection_capacity",
            "57014": "query_timeout",
        }.get(getattr(exc, "sqlstate", None),
              "connection_failed" if stage == "connect" else "query_failed")
        return _result(stage, status)


def main() -> int:
    try:
        report = diagnose(os.environ.get("PREFLIGHT_TEAM_DATABASE_URL", ""))
    except Exception:
        report = _result("probe", "diagnostic_failed")
    print(json.dumps(report, ensure_ascii=True, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
