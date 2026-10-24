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


def test_concurrent_identical_inputs_reserve_and_execute_only_once(store):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.begin("a"*64, str(uuid4()), {"path":"rag", "concurrent":True}, "build123"), range(4)))
    assert sum(created for _,created in results) == 1
    assert len({row["request_id"] for row,_ in results}) == 1


def test_expired_account_cannot_read_or_charge(store):
    import psycopg
    from psycopg import sql
    with psycopg.connect(store.database_url) as conn:
        conn.execute(sql.SQL("UPDATE {}.accounts SET expires_at=now()-interval '1 minute' WHERE id='alice'").format(sql.Identifier(store.schema)))
    with pytest.raises(PilotDenied): store.list_runs("a"*64)
    with pytest.raises(PilotDenied): store.begin("a"*64, str(uuid4()), {"path":"rag"}, "build123")


def test_durable_user_and_global_reservation_limits(store):
    import psycopg
    from psycopg import sql
    with psycopg.connect(store.database_url) as conn:
        for name, token in (("third", "c"*64), ("fourth", "d"*64)):
            conn.execute(sql.SQL("INSERT INTO {}.accounts(id,token_hash,kind) VALUES(%s,%s,'automation')").format(sql.Identifier(store.schema)), (name, token_digest(token)))
    for token, count in (("a"*64, 7), ("b"*64, 7), ("c"*64, 5)):
        for i in range(count):
            store.begin(token, str(uuid4()), {"path":"rag", "sample":i}, "build123")
            with psycopg.connect(store.database_url) as conn:
                conn.execute(sql.SQL("UPDATE {}.runs SET created_at=now()-interval '2 minutes'").format(sql.Identifier(store.schema)))
        # First two accounts are stopped at $0.56 by the $0.60 user cap;
        # the third is stopped by the approved $1.52 global cap at $1.52 total.
        with pytest.raises(PilotDenied): store.begin(token, str(uuid4()), {"path":"rag", "sample":8}, "build123")
    # The approved 19th reservation is allowed; the 20th is denied.
    with pytest.raises(PilotDenied): store.begin("d"*64, str(uuid4()), {"path":"rag"}, "build123")
    with psycopg.connect(store.database_url) as conn:
        count, reserved = conn.execute(sql.SQL("SELECT count(*), sum(reserved_usd) FROM {}.runs").format(sql.Identifier(store.schema))).fetchone()
    assert count == 19 and str(reserved) == "1.52"


@pytest.mark.parametrize("failure", ["timeout", "schema", "unknown"])
def test_gateway_failure_survives_real_store_and_reconnect(store, failure):
    """Provider diagnostics must not be rejected by the real journal boundary."""
    from types import SimpleNamespace as NS
    from llm.live_pilot_gateway import LiveGateway
    from agents.live_pilot import run_workflow
    from core.schemas import AdCopy, Fact, ProductFacts, ProductCategory
    class BadRequestError(Exception):
        status_code = 400
        body = {"code": "invalid_json_schema", "param": "response_format", "message": "private original"}
    error = {"timeout": TimeoutError("private original"), "schema": BadRequestError("private original"),
             "unknown": RuntimeError("private original")}[failure]
    def create(**kwargs):
        raise error
    request = str(uuid4())
    store.begin("a"*64, request, {"path":"rag", "failure":failure}, "drill")
    gateway = LiveGateway("fake", store, "a"*64, request,
                          client=NS(chat=NS(completions=NS(create=create))))
    copy = AdCopy(headline="상품 안내", body="300ml 텀블러입니다.", cta="상품 보기")
    product = ProductFacts(name="합성 텀블러", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="300ml 텀블러입니다.")])
    result = run_workflow("rag", copy, product, gateway)
    store.finish("a"*64, request, result)
    row = LiveStore(store.database_url, schema=store.schema).get("a"*64, request)
    assert result["status"] == "hold"
    assert result["reason"] == ("provider_timeout" if failure == "timeout" else "provider_or_storage_failure")
    assert row["call_count"] == 1 and row["reserved_usd"] == .08
    assert row["result"] == result
    recorded = row["calls"][0]
    assert recorded["status"] == "failed" and recorded["cost_usd"] is None
    assert recorded["reason"] == ("provider_timeout" if failure == "timeout" else "provider_failure")
    assert recorded["error_type"] == {"timeout":"TimeoutError", "schema":"BadRequestError", "unknown":"OtherError"}[failure]
    if failure == "schema":
        assert recorded["http_status"] == 400 and recorded["provider_code"] == "invalid_json_schema"
        assert recorded["provider_param"] == "response_format"
    assert "private original" not in str(row)
    with pytest.raises(PilotDenied): store.review("a"*64, request, "approve", "must not approve")


@pytest.mark.parametrize("field,value", [
    ("error_type", "private original"), ("provider_code", "private original"),
    ("provider_param", "private original"), ("http_status", True),
    ("http_status", 999), ("message", "private original"),
])
def test_diagnostic_fields_reject_arbitrary_text_before_persistence(store, field, value):
    request = str(uuid4())
    store.begin("a"*64, request, {"path":"rag"}, "drill")
    store.start_call("a"*64, request, "rag")
    with pytest.raises(PilotDenied):
        store.finish_call("a"*64, request, 1, {"status":"failed", field:value})
    assert store.get("a"*64, request)["calls"][0]["status"] == "outcome_unknown"
