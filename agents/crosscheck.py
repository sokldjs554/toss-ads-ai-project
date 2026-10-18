"""경계 사례의 교차 확인(다른 모델 계열의 2차 의견) — 원문(pipeline)·자동 수정안(rewrite_check)·직접 재검사(recheck)가 같은 조건·같은 처리를 쓴다.

경계 사례: 검토 모델의 판정이 게재 불가(REJECT)가 아니고, 확신이 τ+margin 보다 낮거나 참고(warn) 지적이 있는 경우.
교차 확인이 필요한데 그 호출이 실패하면 일치 여부를 모르므로 `second_opinion_failed` 를 표시해 판정 함수가 담당자 확인으로 보낸다 (통과로 흐르지 않는다).
평가·방식 비교는 교차 확인을 쓰지 않는 범위(리포트의 scope)라 호출하는 쪽에서 끈다.
"""
from __future__ import annotations

from typing import Optional

from core.schemas import AdCopy, Decision, ReviewVerdict
from guardrails.output_guard import OutputGuardResult
from guardrails.policy import PolicyCatalog
from llm.base import Task
from llm.router import Router

from . import reviewer

DEFAULT_MARGIN = 0.1


def is_borderline(verdict: ReviewVerdict, threshold: float, margin: float = DEFAULT_MARGIN) -> bool:
    if verdict.decision == Decision.REJECT:
        return False  # 검증된 반려는 교차 검증 불필요
    return verdict.confidence < threshold + margin or any(f.severity.value == "warn" for f in verdict.findings)


def apply_second_opinion(copy: AdCopy, det: OutputGuardResult, categories: set[str], facts_text: str, landing_text: str, catalog: PolicyCatalog,
                         router: Router, verdict: ReviewVerdict) -> ReviewVerdict:
    """2차 의견을 받아 verdict 에 기록한다. 호출이 실패하면 예외를 그대로 올리고(호출한 쪽이 실패 사건으로 기록), 그 전에 second_opinion_failed 를 표시한다."""
    try:
        so = reviewer.review(copy, det, categories, facts_text, landing_text, catalog, router, task=Task.SECOND_OPINION)
    except Exception:
        verdict.second_opinion_failed = True
        raise
    verdict.second_opinion = so.verdict.decision
    verdict.second_opinion_provider = so.verdict.provider
    verdict.second_opinion_independent = so.verdict.provider != verdict.provider
    verdict.disagreement = so.verdict.decision != verdict.decision
    return verdict
