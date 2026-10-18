"""3차 검토(승인받은 실제 모델 비교 직전의 읽기 전용 점검)에서 재현된 로직 결함의 회귀 테스트 — 유사 광고 판정의 기준 글, 형식 수리와 교차 확인의 독립성.

통제된 응답으로 판단 규칙을 확인한다 (실제 모델 품질 측정이 아니다).
"""
from __future__ import annotations

from agents.adjudicator import adjudicate  # noqa: F401  (import 확인용)
from core.schemas import AdCopy, Decision, Fact, ProductCategory, ProductFacts
from guardrails import duplicate, output_guard
from guardrails.policy import get_catalog
from llm.base import Task
from llm.errors import ProviderError, SchemaMismatch
from tests.test_round2_logic import LIVE, review, run_original

CAT = get_catalog()
LOAN = ProductFacts(name="OO은행 대출", category=ProductCategory.LOAN, facts=[Fact(id="F1", text="연 4.5%~15.0% 신용대출, 한도 최대 5,000만원")])
DISC = ["연 4.5%~15.0% (2026.10.1 기준), 중도상환수수료 없음", "대출 상환 능력에 비해 대출금이 과다할 경우 개인신용평점이 하락할 수 있습니다", "과도한 대출은 연체 시 불이익이 따를 수 있습니다"]


def _ad(headline, body, disclosures=DISC):
    return AdCopy(headline=headline, body=body, cta="한도 조회", disclosures=list(disclosures))


def test_core_text_leaves_out_the_required_disclosures():
    ad = _ad("모바일로 한도 조회", "최대 5,000만원")
    assert ad.core_text() == "모바일로 한도 조회\n최대 5,000만원\n한도 조회"
    assert all(d not in ad.core_text() for d in DISC) and all(d in ad.full_text() for d in DISC)


def test_an_unrelated_ad_is_not_a_re_registration_just_because_both_carry_the_same_disclosures():
    rejected = _ad("원금 보장 대출", "누구나 무조건 승인, 손실 없는 투자 대출")
    clean = _ad("모바일로 한도 조회", "연 4.5%~15.0%, 최대 5,000만원, 중도상환수수료 없음")
    assert duplicate.similarity(rejected.full_text(), clean.full_text()) >= 0.6, "고지 문구까지 보면 닮은 것으로 읽히는 입력이어야 이 테스트가 의미가 있다"
    for history in ([rejected.core_text()], [rejected.full_text()]):                  # 새 형식의 이력과 고지까지 담은 옛 형식의 이력 모두
        res = output_guard.check(clean, LOAN, {"loan"}, "", history, CAT)
        assert "TA-DUP-01" not in {f.rule_id for f in res.findings} and res.duplicate_score < 0.3, history


def test_a_real_near_duplicate_is_still_caught_even_when_its_disclosures_changed():
    rejected = _ad("누구나 무조건 당일 승인", "신용불량자 OK, 5분 입금", DISC)
    again = _ad("누구나 무조건 당일 승인!", "신용불량자 OK, 3분 입금", DISC[:1])
    res = output_guard.check(again, LOAN, {"loan"}, "", [rejected.core_text()], CAT)
    assert "TA-DUP-01" in {f.rule_id for f in res.findings} and res.duplicate_score >= 0.6


# ── 형식 수리와 교차 확인의 독립성 ─────────────────────────────────────────────────
def test_a_response_repaired_by_the_other_provider_still_counts_as_the_original_providers_judgement():
    """OpenAI 의 심사 응답이 스키마를 어겨 Anthropic 이 형식만 수리하고, 이어 Anthropic 의 교차 확인이 실패해 OpenAI 가 대신 답했다면
    두 판단 모두 OpenAI 의 것이다 — 교차 확인이 아니므로 통과시키지 않는다."""
    good = review("APPROVE", 0.85)
    raw = '{"decision": "APPROVE", "confidence": 0.85, "findings": [], "rationale": "통제된 응답", "rewrite": null, "extra": 1}'
    run, oa, an = run_original(
        LIVE,
        {Task.REVIEW: SchemaMismatch("extra field", provider="openai", raw_text=raw), Task.SECOND_OPINION: review("APPROVE", 0.95)},
        {Task.REPAIR: good, Task.SECOND_OPINION: ProviderError("outage", provider="anthropic")},
        enable_second_opinion=True)
    rep = run.reports[0]
    assert rep.verdict.provider == "anthropic" and rep.verdict.origin_provider == "openai"          # 수리한 쪽과 판단한 쪽을 따로 적는다
    assert rep.verdict.second_opinion_provider == "openai" and not rep.verdict.second_opinion_independent
    assert rep.final_decision == Decision.HUMAN_REVIEW and rep.verdict.second_opinion_failed


def test_independent_cross_check_still_passes_when_the_review_was_repaired_by_the_other_provider():
    """OpenAI 응답을 Anthropic 이 수리했어도 교차 확인을 Anthropic 이 해 준다면 판단한 쪽은 OpenAI 대 Anthropic 이다. 이 경우는 독립이 아니라고 보지 않는다."""
    raw = '{"decision": "APPROVE", "confidence": 0.85, "findings": [], "rationale": "통제된 응답", "rewrite": null, "extra": 1}'
    run, oa, an = run_original(
        LIVE,
        {Task.REVIEW: SchemaMismatch("extra field", provider="openai", raw_text=raw)},
        {Task.REPAIR: review("APPROVE", 0.85), Task.SECOND_OPINION: review("APPROVE", 0.9)},
        enable_second_opinion=True)
    rep = run.reports[0]
    assert rep.verdict.second_opinion_provider == "anthropic" and rep.verdict.second_opinion_independent
    assert rep.final_decision == Decision.APPROVE
