"""Supplemental rewrite-path measurement with bounded, controlled providers; no API."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from core.config import Settings
from core.schemas import AdCopy, Decision, ProductFacts
from evals import live_features as live
from evals.live_guard import BoundedProvider, LiveLedger
from guardrails import output_guard
from llm.base import LLMResponse
from llm.errors import BudgetExceeded, ProviderError
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals/datasets/rewrite_regression_20261021.json"
GOOD = {
    "RW01": {"headline": "300ml 도자기 머그컵", "body": "판매가는 18,000원이에요. 5만원 이상 구매 시 무료 배송해요.", "cta": "머그컵 보기", "disclosures": []},
    "RW02": {"headline": "8주 온라인 영어 수업", "body": "주 2회, 회당 30분 온라인 수업이에요.", "cta": "강의 확인하기", "disclosures": ["개인의 성과는 노력에 따라 달라질 수 있습니다."]},
    "RW03": {"headline": "전 상품 10% 할인", "body": "5만원 이상 구매 시 10월 31일까지 1인 1회 사용할 수 있어요.", "cta": "쿠폰 받기", "disclosures": []},
    "RW04": {"headline": "입주 청소 24평 18만원부터", "body": "하자가 생기면 7일 이내 다시 청소해 드려요.", "cta": "견적 확인하기", "disclosures": []},
}


def _module():
    assert importlib.util.find_spec("evals.live_rewrite") is not None, "rewrite supplement must measure the actual production path"
    return importlib.import_module("evals.live_rewrite")


def _controlled(case, *, rewrite=None, omit=False, fail_recheck=False):
    settings = Settings.from_env(force_demo=False, openai_api_key="test-only", anthropic_api_key="")
    ledger = LiveLedger(1, 12)

    def respond(req):
        is_rewrite = req.label.endswith(":rewrite")
        if is_rewrite and fail_recheck:
            raise ProviderError("private details must not enter report", provider="openai")
        findings = req.context.get("lexical_findings", [])
        decision = "APPROVE" if is_rewrite or omit else "REJECT" if any(f["severity"] == "block" for f in findings) else "REVISE"
        raw = {"decision": decision, "confidence": 0.99,
               "findings": [{"rule_id": f["rule_id"], "quote": f["quote"], "explanation": "Controlled test response"} for f in findings],
               "rationale": "Controlled test response, not a real model measurement",
               "rewrite": None if is_rewrite or omit else (rewrite or GOOD[case["id"]])}
        return LLMResponse(text=json.dumps(raw, ensure_ascii=False), parsed=req.schema.model_validate(raw),
                           provider="openai", model=live.OPENAI_MODEL, input_tokens=40, output_tokens=20)

    provider = FakeProvider("openai", model=live.OPENAI_MODEL, behavior=respond)
    router = Router(settings, providers={"openai": BoundedProvider(provider, ledger)}, tracer=Tracer())
    return settings, ledger, router


def test_every_frozen_original_really_violates_the_current_same_policy():
    q = _module()
    cases = q.load_cases(FIXTURE)
    assert len(cases) == 4
    for case in cases:
        det = output_guard.check(AdCopy(**case["original"]), ProductFacts(**case["product"]), set(case["categories"]))
        assert det.decision in {Decision.REJECT, Decision.REVISE}
        assert set(case["expected_original_rules"]) <= {f.rule_id for f in det.findings}


@pytest.mark.parametrize("case_id", ["RW01", "RW02", "RW03", "RW04"])
def test_success_requires_original_and_rewrite_provider_calls_plus_same_policy_approval(case_id):
    q = _module()
    case = next(c for c in q.load_cases(FIXTURE) if c["id"] == case_id)
    settings, ledger, router = _controlled(case)
    result = q.measure_case(case, settings, router, ledger)
    assert all(result["checks"].values()), result
    assert result["rewrite_status"] == "verified"
    assert result["original_decision"] != "APPROVE"
    assert result["verified_rewrite"] is not None
    assert len(result["original_review_call_ordinals"]) == len(result["rewrite_review_call_ordinals"]) == 1
    assert len(result["model_observations"]) == 2 and ledger.summary()["attempts"] == 2
    assert result["policy_version"]


def test_original_approval_without_a_rewrite_never_counts_as_success():
    q = _module()
    case = q.load_cases(FIXTURE)[0]
    settings, ledger, router = _controlled(case, omit=True)
    result = q.measure_case(case, settings, router, ledger)
    assert result["checks"]["original_not_approved"] is True  # Code block cannot be overridden.
    assert result["checks"]["model_rewrite_proposed"] is False
    assert result["checks"]["real_rewrite_review"] is False
    assert result["rewrite_status"] == "none" and result["verified_rewrite"] is None
    assert not all(result["checks"].values())


def test_unsafe_rewrite_stays_unexposed_and_code_block_skip_is_not_model_recheck_success():
    q = _module()
    case = q.load_cases(FIXTURE)[0]
    unsafe = {k: case["original"][k] for k in ("headline", "body", "cta", "disclosures")}
    settings, ledger, router = _controlled(case, rewrite=unsafe)
    result = q.measure_case(case, settings, router, ledger)
    assert result["rewrite_status"] == "failed_recheck" and result["verified_rewrite"] is None
    assert result["checks"]["no_unsafe_rewrite_exposed"] is True
    assert result["checks"]["real_rewrite_review"] is False
    assert result["rewrite_recheck_executed"] is True and len(result["model_observations"]) == 1


def test_failed_semantic_recheck_retains_proposal_without_secret_or_success_credit():
    q = _module()
    case = q.load_cases(FIXTURE)[0]
    settings, ledger, router = _controlled(case, fail_recheck=True)
    result = q.measure_case(case, settings, router, ledger)
    assert result["rewrite_status"] == "unchecked" and result["verified_rewrite"] is None
    assert result["proposed_rewrite"] is not None
    assert result["checks"]["real_rewrite_review"] is False and result["checks"]["no_failed_stages"] is False
    assert "private details" not in json.dumps(result)
    assert result["rewrite_error_type"] == "ProviderUnavailable"


def test_compliant_source_is_rejected_before_any_provider_call():
    q = _module()
    case = q.load_cases(FIXTURE)[0]
    case["original"] = GOOD[case["id"]]
    settings, ledger, router = _controlled(case)
    with pytest.raises(ValueError, match="original violation"):
        q.measure_case(case, settings, router, ledger)
    assert ledger.summary()["attempts"] == 0


def test_supplement_records_are_separate_from_the_original_five_groups(tmp_path):
    q = _module()
    cases = q.load_cases(FIXTURE)
    run = object.__new__(live.FeatureRun)
    run.request = {"rewrite_fixture_path": str(FIXTURE.relative_to(ROOT))}
    run.output = tmp_path / "controlled-supplement.json"
    run.report = {"kind": "controlled-test-not-real-api", "results": {g: [] for g in live.GROUPS}}
    run.settings, run.ledger, _ = _controlled(cases[0])
    index = 0

    def make_router(**kwargs):
        nonlocal index
        _, _, router = _controlled(cases[index])
        router.providers["openai"].ledger = run.ledger
        index += 1
        return router

    run.router = make_router
    q.run_supplement(run)
    saved = json.loads(run.output.read_text())
    assert tuple(saved["results"]) == live.GROUPS
    assert all(not rows for rows in saved["results"].values())
    assert len(saved["supplemental_results"]["rewrite"]) == 4
    assert saved["supplemental_summaries"]["rewrite"]["all_checks_passed"] == 4
    assert saved["ledger"]["attempts"] == 8


def test_supplement_budget_stop_preserves_proposal_and_stops_before_next_case(tmp_path):
    q = _module()
    run = object.__new__(live.FeatureRun)
    run.request = {"rewrite_fixture_path": str(FIXTURE.relative_to(ROOT))}
    run.output = tmp_path / "controlled-budget-stop.json"
    run.report = {"kind": "controlled-test-not-real-api", "results": {g: [] for g in live.GROUPS}}
    run.settings, run.ledger, router = _controlled(q.load_cases(FIXTURE)[0])
    run.ledger.max_calls = 1
    run.router = lambda **kwargs: router
    with pytest.raises(BudgetExceeded):
        q.run_supplement(run)
    saved = json.loads(run.output.read_text())
    rows = saved["supplemental_results"]["rewrite"]
    assert len(rows) == 1 and rows[0]["execution"] == "budget_stopped"
    assert rows[0]["proposed_rewrite"] is not None and rows[0]["verified_rewrite"] is None
    assert rows[0]["model_observations"][0]["response"]["rewrite"]
    assert saved["ledger"]["attempts"] == 1 and saved["ledger"]["stopped"] is True


def test_manifest_requires_hash_and_planned_count_for_supplement():
    request = json.loads((ROOT / "evals/live_request_20261020.json").read_text())
    request["source_sha256"] = live.source_fingerprints()
    request["rewrite_fixture_path"] = str(FIXTURE.relative_to(ROOT))
    request["planned_supplemental_cases"] = {"rewrite": 4}
    with pytest.raises(ValueError, match="input manifest is incomplete"):
        live.validate_request(request)
    request["input_sha256"][request["rewrite_fixture_path"]] = live.digest(FIXTURE)
    assert live.validate_request(request) is request
    request["planned_supplemental_cases"]["rewrite"] = 3
    with pytest.raises(ValueError, match="planned count"):
        live.validate_request(request)
