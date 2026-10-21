"""새 호출 승인을 과거 비용 기록 재사용과 혼동하지 않도록 검증한다."""
from copy import deepcopy
import pytest

from scripts.claim_remaining_evaluation import reserve, SCOPE


def claim(run_id="remaining1", cost=4, previous=None, **changes):
    return reserve({"run_id": run_id, "cost_cap_usd": cost, "budget_scope": SCOPE,
                    "prior_reservation_ledger_sha256": "immutable-original-ledger", **changes},
                   previous, workflow_run_id="test-only", request_sha256="manifest-digest",
                   prior_ledger_sha256="immutable-original-ledger")


def test_previous_scope_is_fingerprinted_and_not_reused():
    row = claim()
    assert row["total_cap_usd"] == row["reserved_total_usd"] == "4"
    assert row["prior_ledger_sha256"] == "immutable-original-ledger"
    with pytest.raises(ValueError, match="already reserved"):
        claim(previous=row)
    with pytest.raises(ValueError, match="cumulative"):
        claim("another", .01, row)


def test_failed_or_canceled_runs_still_consume_the_whole_reservation():
    first = claim("first", 1)
    second = claim("second", 3, first)
    assert second["reserved_total_usd"] == "4"
    assert len(first["reservations"]) == 1
    with pytest.raises(ValueError):
        claim("third", 1, second)


@pytest.mark.parametrize("bad", [0, -1, 4.01, "NaN", "Infinity", "bad", None])
def test_invalid_amount_is_rejected(bad):
    with pytest.raises(ValueError):
        claim(cost=bad)


@pytest.mark.parametrize("field,value", [("scope", "old"), ("version", 2),
    ("total_cap_usd", 10), ("prior_ledger_sha256", "rewritten"), ("reserved_total_usd", "0")])
def test_rewritten_ledger_is_rejected(field, value):
    first = claim("first", 1)
    first[field] = value
    with pytest.raises(ValueError):
        claim("second", 1, first)


def test_duplicate_rows_cannot_be_replayed_or_refunded():
    first = claim("first", 1)
    first["reservations"].append(deepcopy(first["reservations"][0]))
    first["reserved_total_usd"] = "2"
    with pytest.raises(ValueError, match="already reserved"):
        claim("second", 1, first)
    first = claim("first", 1)
    first["reservations"][0]["reserved_cap_usd"] = "-1"
    with pytest.raises(ValueError):
        claim("second", 1, first)


def test_old_scope_request_cannot_claim_the_additional_cap():
    with pytest.raises(ValueError, match="scope"):
        claim(budget_scope="original")
