"""고정된 위반 원문 → 실제 모델 수정안 → 같은 정책 재검사의 별도 보조평가.

기존 5개 기능 점수의 분모에 섞지 않는다. 생산 recheck 경로와 같은 라우터/비용
장부를 사용하며 원문만 통과했거나 수정안 의미 검사를 생략한 경우는 성공이 아니다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel

from agents.recheck import recheck, recheck_categories
from agents.rewrite_check import rewrite_categories
from core.schemas import AdCopy, Decision, ProductFacts, RewriteStatus
from guardrails import output_guard
from guardrails.policy import get_catalog


def _original(case: dict):
    catalog = get_catalog()
    original, product = AdCopy(**case["original"]), ProductFacts(**case["product"])
    categories = recheck_categories(catalog, product, original, case["categories"])
    det = output_guard.check(original, product, categories, catalog=catalog)
    expected = case.get("expected_original_rules", [])
    if not expected or det.decision not in {Decision.REVISE, Decision.REJECT} or not set(expected) <= {f.rule_id for f in det.findings}:
        raise ValueError("frozen original violation is absent or differs from its required rules")
    return catalog, original, product, categories, det


def load_cases(path: Path) -> list[dict]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "public_synthetic_rewrite_regression":
        raise ValueError("invalid supplemental rewrite fixture")
    cases = manifest.get("cases", [])
    seen = set()
    if not cases:
        raise ValueError("rewrite fixture needs source cases")
    for case in cases:
        if not re.fullmatch(r"RW\d{2}", case.get("id", "")) or case["id"] in seen:
            raise ValueError("invalid or duplicate rewrite case id")
        seen.add(case["id"])
        _original(case)  # 무료 검증 단계에서 원문 위반부터 확인한다.
    return cases


class _RecordedRouter:
    """실제 라우터를 그대로 호출하고 성공한 단계의 구조화 응답만 보관한다."""
    def __init__(self, router):
        self.router = router
        self.observations = []

    def __getattr__(self, name):
        return getattr(self.router, name)

    def complete(self, request):
        response = self.router.complete(request)
        parsed = response.parsed.model_dump(mode="json") if isinstance(response.parsed, BaseModel) else None
        self.observations.append({"task": request.task.value, "label": request.label, "provider": response.provider,
                                  "model": response.model, "repaired": response.repaired,
                                  "origin_provider": response.origin_provider, "fallback_chain": list(response.fallback_chain),
                                  "response": parsed})
        return response


def measure_case(case: dict, settings, router, ledger) -> dict:
    catalog, original, product, categories, det = _original(case)
    start = len(ledger.calls)
    observed = _RecordedRouter(router)
    result = recheck(original, product, categories, settings, router=observed, catalog=catalog,
                     tracer=router.tracer, enable_second_opinion=True)
    report = result.report
    proposed, verified = report.verdict.proposed_rewrite, report.verified_rewrite
    calls = ledger.calls[start:]
    original_label = f"review:{original.variant_id}"
    rewrite_label = f"review:{proposed.variant_id}:rewrite" if proposed is not None else ""
    original_ordinals = [c["ordinal"] for c in calls if c["status"] == "success" and c["task"] == "review" and c["label"] == original_label]
    rewrite_ordinals = [c["ordinal"] for c in calls if c["status"] == "success" and c["task"] == "review" and c["label"] == rewrite_label]
    verified_det = (output_guard.check(verified, product, rewrite_categories(catalog, product, verified, categories), catalog=catalog)
                    if verified is not None else None)
    code_passed = verified_det is not None and verified_det.decision == Decision.APPROVE
    changed = proposed is not None and proposed.full_text() != original.full_text()
    budget_stopped = ledger.stopped or any(f.code == "FM-20" for f in result.failures)
    return {
        "checks": {
            "original_violation_confirmed": True,
            "original_not_approved": report.final_decision != Decision.APPROVE,
            "real_original_review": bool(original_ordinals),
            "model_rewrite_proposed": changed,
            "real_rewrite_review": bool(rewrite_ordinals),
            "same_policy_recheck_approved": report.rewrite_status == RewriteStatus.VERIFIED,
            "verified_copy_passes_code": code_passed,
            "no_unsafe_rewrite_exposed": verified is None or (code_passed and report.rewrite_status == RewriteStatus.VERIFIED),
            "no_failed_stages": not result.failures,
        },
        "fixture_focus": case["focus"], "policy_version": catalog.version,
        "threshold": settings.auto_decision_confidence, "second_opinion_enabled": True,
        "original_categories": sorted(categories), "original_copy": original.model_dump(mode="json"),
        "product": product.model_dump(mode="json"), "original_code_decision": det.decision.value,
        "original_rules": sorted({f.rule_id for f in det.findings}), "original_decision": report.final_decision.value,
        "original_review": report.verdict.model_dump(mode="json", exclude={"proposed_rewrite"}),
        "proposed_rewrite": proposed.model_dump(mode="json") if proposed is not None else None,
        "verified_rewrite": verified.model_dump(mode="json") if verified is not None else None,
        "rewrite_status": report.rewrite_status.value,
        "rewrite_recheck_executed": report.rewrite_recheck_decision is not None,
        "rewrite_recheck_decision": report.rewrite_recheck_decision.value if report.rewrite_recheck_decision is not None else None,
        "rewrite_recheck_findings": [f.model_dump(mode="json") for f in report.rewrite_recheck_findings],
        "verified_copy_code_decision": verified_det.decision.value if verified_det is not None else None,
        "original_review_call_ordinals": original_ordinals, "rewrite_review_call_ordinals": rewrite_ordinals,
        "model_observations": observed.observations,
        "failures": [{"stage": f.stage, "code": f.code} for f in result.failures],
        "review_error_type": result.review_error.split(":", 1)[0] if result.review_error else "",
        "rewrite_error_type": result.rewrite_error.split(":", 1)[0] if result.rewrite_error else "",
        "budget_stopped": budget_stopped,
        "execution": "budget_stopped" if budget_stopped else "completed",
    }


def run_supplement(run) -> None:
    # FeatureRun owns safe path resolution, provider configuration, checkpoint and
    # budget termination. This module never reads credentials or creates a provider.
    from evals.live_features import safe_path

    cases = load_cases(safe_path(run.request["rewrite_fixture_path"]))
    for case in cases:
        run.case("rewrite", case["id"],
                 lambda case=case: measure_case(case, run.settings, run.router(both=True), run.ledger), supplemental=True)
