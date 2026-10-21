"""Small-workspace PostgreSQL adapter. No database credentials leave the server.

One transaction locks the current state, checks identity and role, applies a
command, and stores its replay receipt. A failed commit is an unknown outcome;
the UI must retry the SAME request ID. There is no local-storage fallback.
"""
from __future__ import annotations

import json
import os
import re
from uuid import UUID

from ops import team_review


class StorageUnavailable(RuntimeError):
    pass


class StorageBusy(StorageUnavailable):
    pass


class PostgresTeamStore:
    def __init__(self, database_url: str, *, schema: str = "preflight_team", lock_timeout_ms: int = 3000, admin_mode: bool = False):
        if not database_url:
            raise StorageUnavailable("공동 저장 연결이 아직 설정되지 않았습니다.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid database schema")
        if type(lock_timeout_ms) is not int or not 1 <= lock_timeout_ms <= 10000:
            raise ValueError("invalid lock timeout")
        if type(admin_mode) is not bool:
            raise ValueError("invalid administrator mode")
        self._database_url = database_url
        self.schema = schema
        self.lock_timeout_ms = lock_timeout_ms
        # Explicit test/migration seam only. from_env() never enables it and
        # the runtime path never falls back after a missing/denied SQL function.
        self.admin_mode = admin_mode

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
        from ops.team_review_db_api import install_api

        initial = team_review.new_state(owner_digest)
        try:
            with self._connect() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("preflight-team-init:" + self.schema,))
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} ").format(sql.Identifier(self.schema)))
                conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.workspace (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), revision bigint NOT NULL, document jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())").format(sql.Identifier(self.schema)))
                conn.execute(sql.SQL("INSERT INTO {}.workspace(singleton, revision, document) VALUES(true, 0, %s) ON CONFLICT(singleton) DO NOTHING").format(sql.Identifier(self.schema)), (Jsonb(initial),))
                install_api(conn, schema=self.schema)
        except psycopg.Error:
            raise StorageUnavailable("공동 저장 초기화를 완료하지 못했습니다. 연결 설정과 권한을 확인해 주세요.") from None

    def execute(self, access_key: str, action: str, payload: dict | None = None, *, request_id: str) -> dict:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb

        team_review.key_digest(access_key)  # Reject malformed keys before any network operation.
        payload = {} if payload is None else payload
        try:
            if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
                raise ValueError
            if not isinstance(action, str) or not isinstance(payload, dict):
                raise ValueError
            command = json.dumps({"action": action, "payload": payload}, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise team_review.InvalidCommand("요청 ID 또는 내용이 올바르지 않습니다.") from None
        if len(command.encode("utf-8")) > team_review.MAX_COMMAND_BYTES:
            raise team_review.CapacityReached("한 번에 보낼 수 있는 광고 정보 크기를 넘었습니다.")
        try:
            with self._connect() as conn:
                conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{self.lock_timeout_ms}ms",))
                conn.execute("SET LOCAL statement_timeout = '15s'")
                if self.admin_mode:
                    row = conn.execute(sql.SQL("SELECT revision, document FROM {}.workspace WHERE singleton = true FOR UPDATE").format(sql.Identifier(self.schema))).fetchone()
                else:
                    row = conn.execute(sql.SQL("SELECT revision, document, db_now FROM {}.lock_workspace(%s, %s::uuid, %s)").format(sql.Identifier(self.schema)), (access_key, request_id, command)).fetchone()
                if row is None or not isinstance(row[1], dict) or row[0] != row[1].get("revision"):
                    raise StorageUnavailable("공동 저장 상태를 확인할 수 없습니다. 관리자에게 문의해 주세요.")
                previous = row[1]
                state, result = team_review.execute(previous, access_key, action, payload, request_id=request_id,
                                                   now=None if self.admin_mode else row[2])
                if state != previous:
                    if self.admin_mode:
                        cursor = conn.execute(sql.SQL("UPDATE {}.workspace SET revision = %s, document = %s, updated_at = now() WHERE singleton = true AND revision = %s").format(sql.Identifier(self.schema)), (state["revision"], Jsonb(state), row[0]))
                        saved = state["revision"] if cursor.rowcount == 1 else None
                    else:
                        saved_row = conn.execute(sql.SQL("SELECT {}.save_workspace(%s, %s::uuid, %s, %s, %s)").format(sql.Identifier(self.schema)), (access_key, request_id, command, row[0], Jsonb(state))).fetchone()
                        saved = saved_row[0] if saved_row else None
                    if saved != state["revision"]:
                        raise StorageUnavailable("저장 상태가 바뀌어 요청을 완료하지 못했습니다. 같은 요청으로 다시 확인해 주세요.")
                # The context manager commits BEFORE returning to the caller.
            return result
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
            raise StorageBusy("다른 작업을 저장 중이라 기다리는 시간이 끝났습니다. 같은 요청으로 다시 시도해 주세요.") from None
        except psycopg.Error as exc:
            mapped = {
                "PT401": (team_review.AuthenticationError, "접근키가 만료됐거나 사용할 수 없습니다. 관리자에게 새 키를 요청해 주세요."),
                "PT403": (team_review.PermissionDenied, "이 작업을 할 권한이 없습니다."),
                "PT409": (team_review.RequestConflict, "현재 저장 상태와 요청이 일치하지 않습니다. 같은 요청을 다시 확인해 주세요."),
                "PT422": (team_review.InvalidCommand, "요청 내용 또는 저장할 상태가 올바르지 않습니다."),
                "PT507": (team_review.CapacityReached, "공동 저장의 정상 작업 한도에 도달했습니다."),
            }.get(exc.sqlstate)
            if mapped:
                raise mapped[0](mapped[1]) from None
            # Driver messages may contain the DSN. Never expose or log them.
            raise StorageUnavailable("공동 저장 응답을 확인하지 못했습니다. 저장됐을 수도 있으므로 같은 요청으로 다시 확인해 주세요.") from None
