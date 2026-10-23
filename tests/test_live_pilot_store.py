import os
from pathlib import Path
from uuid import uuid4
import pytest
from ops.live_pilot_store import LiveStore, PilotDenied, token_digest


@pytest.fixture
def store():
    url = os.environ.get("PREFLIGHT_TEAM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    import psycopg
    from psycopg import sql
    schema = "pilot_test_" + uuid4().hex
    with psycopg.connect(url) as conn:
        conn.execute(Path("ops/live_pilot_schema.sql").read_text().replace("preflight_live_pilot", schema))
        for name, key in [("alice", "a" * 64), ("bob", "b" * 64)]:
            conn.execute(sql.SQL("INSERT INTO {}.accounts(id,token_hash,kind) VALUES (%s,%s,'automation')").format(sql.Identifier(schema)), (name, token_digest(key)))
    yield LiveStore(url, schema=schema)
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_durable_reservation_idempotency_and_cross_account_denial(store):
    request = str(uuid4())
    run, created = store.begin("a" * 64, request, {"path": "rag"}, "build123")
    assert created and run["reserved_usd"] == .08
    again, created = store.begin("a" * 64, request, {"path": "rag"}, "build123")
    assert not created and again["request_id"] == request
    with pytest.raises(PilotDenied): store.begin("a" * 64, request, {"path": "tools"}, "build123")
    with pytest.raises(PilotDenied): store.get("b" * 64, request)
    with pytest.raises(PilotDenied): store.finish("b" * 64, request, {})
    with pytest.raises(PilotDenied): store.review("b" * 64, request, "reject", "시험")
    assert store.list_runs("b" * 64) == []
    assert LiveStore(store.database_url, schema=store.schema).get("a" * 64, request)["reserved_usd"] == .08


def test_attempts_are_reserved_before_network_and_survive_failed_usage(store):
    request = str(uuid4())
    store.begin("a" * 64, request, {"path": "tools"}, "build123")
    for i in range(4):
        assert store.start_call("a" * 64, request, "rag") == i + 1
        store.finish_call("a" * 64, request, i + 1, {"status": "failed", "reason": "timeout"})
    with pytest.raises(PilotDenied): store.start_call("a" * 64, request, "rag")
    row = store.get("a" * 64, request)
    assert row["call_count"] == 4 and row["reserved_usd"] == .08
    assert len(row["calls"]) == 4


def test_approval_requires_completed_checked_run_and_is_audited_once(store):
    request = str(uuid4())
    store.begin("a" * 64, request, {"path": "rag"}, "build123")
    with pytest.raises(PilotDenied): store.review("a" * 64, request, "approve", "검토")
    store.finish("a" * 64, request, {"status": "human_review", "decision": "APPROVE"})
    store.review("a" * 64, request, "approve", "합성 자동 검증")
    store.review("a" * 64, request, "approve", "합성 자동 검증")
    row = store.get("a" * 64, request)
    assert row["review"]["actor_kind"] == "automation"
    assert len(row["audit"]) == 1
    with pytest.raises(PilotDenied): store.review("a" * 64, request, "reject", "변경")


def test_rate_limit_prevents_new_charge(store):
    for i in range(3): store.begin("a" * 64, str(uuid4()), {"path": "rag", "input": i}, "build123")
    with pytest.raises(PilotDenied): store.begin("a" * 64, str(uuid4()), {"path": "rag"}, "build123")
    assert len(store.list_runs("a" * 64)) == 3


def test_reconnect_same_input_reuses_durable_result_without_new_reservation(store):
    first = str(uuid4())
    store.begin("a" * 64, first, {"path": "rag", "copy": "same input"}, "build123")
    store.finish("a" * 64, first, {"status": "hold", "decision": "HUMAN_REVIEW"})
    row, created = store.begin("a" * 64, str(uuid4()), {"path": "rag", "copy": "same input"}, "build456")
    assert not created and row["request_id"] == first and row["build"] == "build123"
    assert len(store.list_runs("a" * 64)) == 1
