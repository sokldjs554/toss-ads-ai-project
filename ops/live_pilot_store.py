"""Isolated durable LIVE pilot journal. Credentials and authorization stay server-side.

The runtime DB role has access ONLY to this pilot schema, not existing team data.
Reservations are intentionally never refunded: reconnect/redeploy cannot reset cap.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
from uuid import UUID


class PilotDenied(ValueError):
    pass


def token_digest(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise PilotDenied("접근키를 확인하세요.")
    return hashlib.sha256(token.encode()).hexdigest()


class LiveStore:
    def __init__(self, database_url, *, schema="preflight_live_pilot"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid schema")
        self.database_url, self.schema = database_url, schema

    @contextmanager
    def _transaction(self, token):
        import psycopg
        from psycopg import sql
        digest = token_digest(token)
        try:
            with psycopg.connect(self.database_url, connect_timeout=10, prepare_threshold=None) as conn:
                conn.execute("SET LOCAL statement_timeout='10s'")
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
                actor = conn.execute("SELECT id,kind FROM accounts WHERE token_hash=%s AND active AND expires_at>now()", (digest,)).fetchone()
                if not actor:
                    raise PilotDenied("접근키가 만료되었거나 사용할 수 없습니다.")
                yield conn, {"id": actor[0], "kind": actor[1]}
        except psycopg.Error:
            raise PilotDenied("저장 응답을 확인하지 못했습니다. 같은 요청을 조회하세요. 재실행하지 마세요.") from None

    def account(self, token):
        with self._transaction(token) as (_, actor):
            return actor

    def _owned(self, conn, actor, request_id, lock=False):
        try:
            request_id = str(UUID(request_id))
        except (ValueError, TypeError):
            raise PilotDenied("요청을 확인하세요.") from None
        row = conn.execute("SELECT payload,input_hash,result,review FROM runs WHERE request_id=%s AND account_id=%s" + (" FOR UPDATE" if lock else ""),
                           (request_id, actor["id"])).fetchone()
        if not row:
            raise PilotDenied("본인의 요청만 확인할 수 있습니다.")
        return row

    def begin(self, token, request_id, payload, build):
        from psycopg.types.json import Jsonb
        try:
            request_id = str(UUID(request_id))
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if len(serialized.encode()) > 50000 or payload.get("path") not in {"rag", "tools"}:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PilotDenied("입력 형식 또는 크기를 확인하세요.") from None
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self._transaction(token) as (conn, actor):
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema + ":budget",))
            prior = conn.execute("SELECT account_id,input_hash FROM runs WHERE request_id=%s", (request_id,)).fetchone()
            if prior:
                if prior != (actor["id"], digest):
                    raise PilotDenied("요청 ID와 입력이 일치하지 않습니다.")
                created = False
            else:
                reserved = conn.execute("SELECT coalesce(sum(reserved_usd),0), coalesce(sum(reserved_usd) FILTER(WHERE account_id=%s),0), count(*) FILTER(WHERE account_id=%s AND created_at>now()-interval '1 minute') FROM runs", (actor["id"], actor["id"])).fetchone()
                if float(reserved[0]) + .08 > 1.000001 or float(reserved[1]) + .08 > .500001:
                    raise PilotDenied("파일럿의 누적 비용 예약 한도에 도달했습니다.")
                if reserved[2] >= 3:
                    raise PilotDenied("분당 3개 요청 한도입니다. 잠시 후 시도하세요.")
                conn.execute("INSERT INTO runs(request_id,account_id,input_hash,payload,build) VALUES(%s,%s,%s,%s,%s)",
                             (request_id, actor["id"], digest, Jsonb(payload), build[:64]))
                created = True
        return self.get(token, request_id), created

    def start_call(self, token, request_id, stage):
        if stage not in {"rag", "rewrite_tools"}:
            raise PilotDenied("invalid stage")
        with self._transaction(token) as (conn, actor):
            row = self._owned(conn, actor, request_id, lock=True)
            if row[2] is not None:
                raise PilotDenied("완료된 요청을 다시 호출할 수 없습니다.")
            call = conn.execute("UPDATE runs SET call_count=call_count+1 WHERE request_id=%s AND call_count<4 RETURNING call_count", (request_id,)).fetchone()
            if not call:
                raise PilotDenied("호출 횟수 한도에 도달했습니다.")
            conn.execute("INSERT INTO calls(request_id,sequence,stage) VALUES(%s,%s,%s)", (request_id, call[0], stage))
            return call[0]

    def finish_call(self, token, request_id, sequence, record):
        from psycopg.types.json import Jsonb
        # Strict operational allowlist. No prompts, response bodies or SDK error text.
        allowed = {"status", "reason", "model", "input_tokens", "output_tokens", "cost_usd", "latency_ms"}
        if set(record) - allowed:
            raise PilotDenied("invalid metric")
        with self._transaction(token) as (conn, actor):
            self._owned(conn, actor, request_id, lock=True)
            changed = conn.execute("UPDATE calls SET record=%s,finished_at=now() WHERE request_id=%s AND sequence=%s AND finished_at IS NULL", (Jsonb(record), request_id, sequence))
            if changed.rowcount != 1:
                raise PilotDenied("호출 결과가 이미 저장되었거나 없습니다.")

    def finish(self, token, request_id, result):
        from psycopg.types.json import Jsonb
        with self._transaction(token) as (conn, actor):
            row = self._owned(conn, actor, request_id, lock=True)
            if row[2] is not None and row[2] != result:
                raise PilotDenied("저장된 결과를 덮어쓸 수 없습니다.")
            conn.execute("UPDATE runs SET result=%s,finished_at=coalesce(finished_at,now()) WHERE request_id=%s", (Jsonb(result), request_id))

    def review(self, token, request_id, decision, note):
        from psycopg.types.json import Jsonb
        if decision not in {"approve", "reject", "edit"} or not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000:
            raise PilotDenied("검토 결과와 사유를 입력하세요.")
        with self._transaction(token) as (conn, actor):
            row = self._owned(conn, actor, request_id, lock=True)
            if not row[2]:
                raise PilotDenied("실행 결과를 먼저 확인하세요.")
            if decision == "approve" and (row[2].get("status") != "human_review" or row[2].get("decision") != "APPROVE"):
                raise PilotDenied("미해결 위반 또는 보류 상태에서는 승인할 수 없습니다.")
            record = {"decision": decision, "note": note.strip(), "actor_kind": actor["kind"]}
            if row[3] is not None:
                if row[3] != record:
                    raise PilotDenied("저장된 검토는 변경할 수 없습니다. 수정 후 새 요청으로 재검사하세요.")
                return
            conn.execute("UPDATE runs SET review=%s WHERE request_id=%s", (Jsonb(record), request_id))
            conn.execute("INSERT INTO audit(request_id,account_id,record) VALUES(%s,%s,%s)", (request_id, actor["id"], Jsonb(record)))

    def list_runs(self, token):
        with self._transaction(token) as (conn, actor):
            rows = conn.execute("SELECT request_id,created_at,payload->>'path',result->>'status' FROM runs WHERE account_id=%s ORDER BY created_at DESC LIMIT 100", (actor["id"],)).fetchall()
            return [{"request_id": str(r[0]), "created_at": r[1].isoformat(), "path": r[2], "status": r[3] or "outcome_unknown"} for r in rows]

    def get(self, token, request_id):
        with self._transaction(token) as (conn, actor):
            self._owned(conn, actor, request_id)
            row = conn.execute("SELECT request_id,created_at,payload,build,reserved_usd,call_count,result,review,finished_at FROM runs WHERE request_id=%s AND account_id=%s", (request_id, actor["id"])).fetchone()
            calls = conn.execute("SELECT sequence,started_at,stage,record FROM calls WHERE request_id=%s ORDER BY sequence", (request_id,)).fetchall()
            audit = conn.execute("SELECT created_at,record FROM audit WHERE request_id=%s", (request_id,)).fetchall()
            return {"request_id": str(row[0]), "created_at": row[1].isoformat(), "payload": row[2], "build": row[3],
                    "reserved_usd": float(row[4]), "call_count": row[5], "result": row[6], "review": row[7],
                    "finished_at": row[8].isoformat() if row[8] else None, "actor_kind": actor["kind"],
                    "calls": [{"sequence": r[0], "started_at": r[1].isoformat(), "stage": r[2], **r[3]} for r in calls],
                    "audit": [{"at": r[0].isoformat(), **r[1]} for r in audit]}


def operational_summary(row):
    result = row.get("result") or {}
    return {k: row[k] for k in ("request_id", "created_at", "finished_at", "build", "reserved_usd", "call_count", "calls", "actor_kind")} | {
        "mode": "LIVE", "path": row["payload"]["path"], "status": result.get("status", "outcome_unknown"),
        "decision": result.get("decision"), "reason": result.get("reason"),
        "prompt_version": result.get("prompt_version"), "policy_version": result.get("policy_version"),
        "revisions": result.get("revisions", 0), "review_decision": (row.get("review") or {}).get("decision"),
        "audit": [{"at": a["at"], "decision": a["decision"], "actor_kind": a["actor_kind"]} for a in row["audit"]]}
