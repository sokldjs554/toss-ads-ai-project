"""Actions 재실행·중단이 전체 예산을 재사용하지 못하는지 확인한다."""
import pytest

from scripts.claim_live_evaluation import reserve


def claim(run_id, cost, previous=None):
    return reserve({"run_id": run_id, "cost_cap_usd": cost}, previous,
                   workflow_run_id="test-only", request_sha256="frozen-manifest")


def test_same_run_cannot_reserve_twice_even_after_failed_execution():
    first = claim("first", 4)
    with pytest.raises(ValueError, match="already reserved"):
        claim("first", 4, first)


def test_total_cap_includes_every_previous_attempt_without_refunds():
    first = claim("first", 4)
    second = claim("second", 4, first)
    assert second["reserved_total_usd"] == "8"
    with pytest.raises(ValueError, match="cumulative"):
        claim("third", "2.01", second)
    assert claim("third", 2, second)["reserved_total_usd"] == "10"


@pytest.mark.parametrize("cap", ["NaN", "Infinity", "-1", "0", "10.01"])
def test_invalid_cap_never_creates_a_reservation(cap):
    with pytest.raises(ValueError):
        claim("invalid", cap)
