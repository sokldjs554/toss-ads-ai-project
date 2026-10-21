"""Small-workspace PostgreSQL adapter. No database credentials leave the server.

One transaction locks the current state, checks identity and role, applies a
command, and stores its replay receipt. A failed commit is an unknown outcome;
the UI must retry the SAME request ID. There is no local-storage fallback.
"""
from __future__ import annotations

import os
import re

from ops import team_review


class StorageUnavailable(RuntimeError):
    pass


class StorageBusy(StorageUnavailable):
    pass


class PostgresTeamStore:
    def __init__(self, database_url: str, *, schema: str = "preflight_team", lock_timeout_ms: int = 3000):
        if not database_url:
            raise StorageUnavailable("공동 저장 연결이 아직 설정되지 않았습니다.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid database schema")
        if type(lock_timeout_ms) is not int or not 1 <= lock_timeout_ms <= 10000:
            raise ValueError("invalid lock timeout")
        self._database_url = database_url
        self.schema = schema
        self.lock_timeout_ms = lock_timeout_ms

    def __repr__(self) -> str:
        return "PostgresTeamStore(<configured>)"

    @classmethod
    def from_env(cls):
        value = os.environ.get("PREFLIGHT_TEAM_DATABASE_URL", "")
        return cls(value) if value else None

    def _connect(self):
        import psycopg  # Native server only; Pyodide never imports the driver.
        return psycopg.connect(self._database_url, connect_timeout=10, prepare_threshold=None)

    def initialize(self, owner_digest: str) -> None:
        """Migration/bootstrap, run with a DIRECT connection before serving.

        Existing state is never replaced. A changed environment key cannot
        silently resurrect a revoked key or erase work on a restart.
        """
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb

        initial = team_review.new_state(owner_digest)
        try:
            with self._connect() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("preflight-team-init:" + self.schema,))
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} ").format(sql.Identifier(self.schema)))
                conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.workspace (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), revision bigint NOT NULL, document jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())").format(sql.Identifier(self.schema)))
                conn.execute(sql.SQL("INSERT INTO {}.workspace(singleton, revision, document) VALUES(true, 0, %s) ON CONFLICT(singleton) DO NOTHING").format(sql.Identifier(self.schema)), (Jsonb(initial),))
        except psycopg.Error:
            raise StorageUnavailable("공동 저장 초기화를 완료하지 못했습니다. 연결 설정과 권한을 확인해 주세요.") from None

    def execute(self, access_key: str, action: str, payload: dict | None = None, *, request_id: str) -> dict:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb

        team_review.key_digest(access_key)  # Reject malformed keys before any network operation.
        try:
            with self._connect() as conn:
                conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{self.lock_timeout_ms}ms",))
                conn.execute("SET LOCAL statement_timeout = '15s'")
                row = conn.execute(sql.SQL("SELECT revision, document FROM {}.workspace WHERE singleton = true FOR UPDATE").format(sql.Identifier(self.schema))).fetchone()
                if row is None or row[0] != row[1].get("revision"):
                    raise StorageUnavailable("공동 저장 상태를 확인할 수 없습니다. 관리자에게 문의해 주세요.")
                previous = row[1]
                state, result = team_review.execute(previous, access_key, action, {} if payload is None else payload, request_id=request_id)
                if state != previous:
                    cursor = conn.execute(sql.SQL("UPDATE {}.workspace SET revision = %s, document = %s, updated_at = now() WHERE singleton = true AND revision = %s").format(sql.Identifier(self.schema)), (state["revision"], Jsonb(state), row[0]))
                    if cursor.rowcount != 1:
                        raise StorageUnavailable("저장 상태가 바뀌어 요청을 완료하지 못했습니다. 같은 요청으로 다시 확인해 주세요.")
                # The context manager commits BEFORE returning to the caller.
            return result
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
            raise StorageBusy("다른 작업을 저장 중이라 기다리는 시간이 끝났습니다. 같은 요청으로 다시 시도해 주세요.") from None
        except psycopg.Error:
            # Driver messages may contain the DSN. Never expose or log them.
            raise StorageUnavailable("공동 저장 응답을 확인하지 못했습니다. 저장됐을 수도 있으므로 같은 요청으로 다시 확인해 주세요.") from None
