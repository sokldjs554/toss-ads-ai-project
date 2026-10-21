"""Independent paid-evaluation boundary review; no provider or remote Git calls."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from evals import live_features as live
from evals.live_guard import LiveLedger
from scripts import claim_remaining_evaluation as reservation


PRIOR_SHA = "a" * 64


def _request(**changes):
    return {"run_id": "review-first", "cost_cap_usd": 2,
            "budget_scope": reservation.SCOPE,
            "prior_reservation_ledger_sha256": PRIOR_SHA, **changes}


def _reserve(request=None, previous=None):
    return reservation.reserve(_request() if request is None else request, previous,
                               workflow_run_id="local-review-no-provider",
                               request_sha256="b" * 64, prior_ledger_sha256=PRIOR_SHA)


def _frozen_request():
    request = json.loads((live.ROOT / "evals/live_request_20261021.json").read_text(encoding="utf-8"))
    # Isolate the contract under test from unrelated development source changes.
    request["source_sha256"] = live.source_fingerprints()
    return request


@pytest.mark.parametrize("claimed_prior", [None, "c" * 64])
def test_first_reservation_requires_the_prior_ledger_frozen_by_the_request(claimed_prior):
    request = _request(prior_reservation_ledger_sha256=claimed_prior)
    if claimed_prior is None:
        request.pop("prior_reservation_ledger_sha256")
    with pytest.raises(ValueError):
        _reserve(request)


@pytest.mark.parametrize("claimed_prior", [None, "c" * 64])
def test_validate_only_checks_the_supplemental_requests_prior_ledger(claimed_prior):
    request = _frozen_request()
    request["prior_reservation_ledger_sha256"] = claimed_prior
    if claimed_prior is None:
        request.pop("prior_reservation_ledger_sha256")
    with pytest.raises(ValueError):
        live.validate_request(request)


def test_truncated_empty_reservation_rows_do_not_refund_an_existing_total():
    previous = _reserve()
    damaged = deepcopy(previous)
    damaged["reservations"] = []
    assert damaged["reserved_total_usd"] == "2"
    with pytest.raises(ValueError):
        _reserve(_request(run_id="review-second"), damaged)
    assert len(previous["reservations"]) == 1


def test_duplicate_insight_selection_cannot_silently_reduce_the_frozen_denominator():
    request = _frozen_request()
    request["insight_ids"][1] = request["insight_ids"][0]
    with pytest.raises(ValueError):
        live.validate_request(request)


def test_original_planned_count_must_match_the_actual_frozen_inputs():
    request = _frozen_request()
    request["planned_cases"]["vision"] = 0
    with pytest.raises(ValueError):
        live.validate_request(request)


@pytest.mark.parametrize("path_field", ["followup_dialogs_path", "rewrite_fixture_path"])
def test_planned_supplement_cannot_disappear_when_its_path_is_missing(path_field):
    request = _frozen_request()
    request.pop(path_field)
    with pytest.raises(ValueError):
        live.validate_request(request)


@pytest.mark.parametrize("missing_group", ["vision", "rewrite"])
def test_an_adapter_that_records_no_planned_case_cannot_finish_the_run(missing_group, tmp_path, monkeypatch):
    from evals import live_rewrite

    run = object.__new__(live.FeatureRun)
    run.output = tmp_path / "controlled-missing-case.json"
    run.request = {"planned_cases": {group: 1 for group in live.GROUPS},
                   "planned_supplemental_cases": {"rewrite": 1},
                   "rewrite_fixture_path": "unused-in-controlled-harness"}
    run.report = {"kind": "controlled-test-not-real-model", "manifest": run.request,
                  "status": "running", "results": {group: [] for group in live.GROUPS}}
    run.ledger = LiveLedger(1, 10)

    def measure(group, *, supplemental=False):
        if group != missing_group:
            run.case(group, "controlled-case", lambda: {"checks": {"recorded": True}}, supplemental=supplemental)

    for group in live.GROUPS:
        monkeypatch.setattr(run, group, lambda group=group: measure(group))
    monkeypatch.setattr(live_rewrite, "run_supplement", lambda target: measure("rewrite", supplemental=True))
    result = run.run()
    assert result["status"] != "completed"
    saved = json.loads(run.output.read_text(encoding="utf-8"))
    assert saved["status"] != "completed"


def test_moved_branch_is_rejected_before_reading_or_writing_the_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REF_NAME", reservation.BRANCH)
    monkeypatch.setattr(reservation.subprocess, "check_output",
                        lambda args, **kwargs: {"HEAD": "old-head", "FETCH_HEAD": "new-head"}[args[-1]])
    monkeypatch.setattr(reservation, "REQUEST", tmp_path / "must-not-be-read.json")
    ledger = tmp_path / "must-not-be-written.json"
    monkeypatch.setattr(reservation, "LEDGER", ledger)
    with pytest.raises(ValueError, match="branch moved"):
        reservation.main()
    assert not ledger.exists()


def test_unapproved_branch_stops_before_any_git_operation(monkeypatch):
    monkeypatch.setenv("GITHUB_REF_NAME", "unapproved")

    def unexpected_git(*args, **kwargs):
        raise AssertionError("An unapproved branch must stop before Git or provider calls")

    monkeypatch.setattr(reservation.subprocess, "check_output", unexpected_git)
    with pytest.raises(ValueError, match="authorized work branch"):
        reservation.main()
