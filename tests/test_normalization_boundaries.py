"""Visible separators must not manufacture a prohibited word from two word fragments.

Policy: keep literal/hidden-character matches and separated terms anchored at
either end of a source token. Both-end fragments created only by tight joining
are not sufficient deterministic evidence. This is not semantic segmentation.
"""
import pytest

from agents import adjudicator
from agents.pipeline import RunOptions, run_pipeline
from agents.recheck import build_edited_copy, recheck
from agents.rewrite_check import check_rewrite
from core.schemas import AdCopy, Decision, Severity
from guardrails import output_guard
from guardrails.lexical import scan
from guardrails.policy import get_catalog
from llm.router import Router
from ui.presets import PRESETS, build_brief


RESTRICTED = "TA-RESTRICTED-01"
ORIGINAL = "업계 최저 OO몰 가을 쿠폰! 지금 아니면 못 받는 조건"


@pytest.mark.parametrize("text", [
    "쿠폰! 지금 확인하세요", "쿠폰 지금 확인하세요", "쿠폰. 지금 확인하세요",
    "쿠폰을 지금 확인하세요", "쿠폰\n지금 확인하세요", "쿠폰\u00a0지금 확인하세요",
    "쿠폰!\u200b 지금 확인하세요", "최다 단계별 안내", "매일 수령하세요",
])
def test_normal_words_do_not_manufacture_a_restricted_term(text):
    assert RESTRICTED not in {f.rule_id for f in scan(text, {"ecommerce"})}


@pytest.mark.parametrize("term", [
    "폰지", "폰 지", "폰.지", "폰! 지", "폰 . - * 지", "폰\n지",
    "폰\u00a0지", "폰\u200b지", "폰\x00지", "다 단 계", "다.단계",
])
def test_restricted_terms_and_evasions_keep_exact_original_spans(term):
    text = "안내: " + term + " 사기"
    findings = [f for f in scan(text, {"ecommerce"}) if f.rule_id == RESTRICTED]
    assert len(findings) == 1
    f = findings[0]
    assert (f.quote, f.start, f.end) == (term, 4, 4 + len(term))
    assert text[f.start:f.end] == term
    assert f.verified_quote and f.severity == Severity.BLOCK


@pytest.mark.parametrize("text,quote", [
    ("폰 지사기를 홍보", "폰 지"), ("폰 지는 위험", "폰 지"),
    ("신종폰지사기", "폰지"), ("신종폰 지 사기", "폰 지"),
    ("쿠폰 지", "폰 지"),  # ambiguous: one whole endpoint, retain conservative block
])
def test_korean_suffixes_and_one_sided_ambiguous_fragments_remain_blocked(text, quote):
    findings = [f for f in scan(text, {"ecommerce"}) if f.rule_id == RESTRICTED]
    assert len(findings) == 1
    assert findings[0].quote == quote
    assert findings[0].severity == Severity.BLOCK


@pytest.mark.parametrize("text,rule", [
    ("원 금 보 장 상품", "FIN-GUARANTEE-01"),
    ("元.金.保.障 상품", "FIN-GUARANTEE-01"),
    ("원금\u200b보장 상품", "FIN-GUARANTEE-01"),
    ("업 계 최 저 금리", "TA-SUPERLATIVE-01"),
    ("무 조 건 승 인", "FIN-APPROVAL-01"),
])
def test_shared_matcher_keeps_other_policy_evasions(text, rule):
    assert rule in {f.rule_id for f in scan(text, {"loan", "investment"})}


def test_false_fragment_does_not_hide_a_later_real_match():
    text = "쿠폰! 지금 확인. 폰 . 지 사기"
    findings = [f for f in scan(text, {"ecommerce"}) if f.rule_id == RESTRICTED]
    assert [(f.quote, f.start, f.end) for f in findings] == [("폰 . 지", 11, 16)]


def test_original_keeps_other_violations_and_fail_closed_decision():
    product = build_brief(PRESETS["commerce_landing"]).product
    copy = AdCopy(headline=ORIGINAL, body="", cta="")
    det = output_guard.check(copy, product, {"ecommerce"})
    ids = {f.rule_id for f in det.findings}
    assert RESTRICTED not in ids
    assert {"TA-SUPERLATIVE-01", "TA-URGENCY-01"} <= ids
    # Removing the false block must never approve without a reviewer.
    assert adjudicator.adjudicate(copy, det, None, 0.8, None, None).final_decision == Decision.HUMAN_REVIEW


def test_commerce_preset_pipeline_preserves_remaining_revise(demo_settings, tracer):
    preset = PRESETS["commerce_landing"]
    run = run_pipeline(build_brief(preset), demo_settings,
                       options=RunOptions(landing_text=preset["landing_text"]), tracer=tracer)
    reports = [r for r in run.reports if r.ad_copy.headline == ORIGINAL]
    assert len(reports) == 1
    report = reports[0]
    assert RESTRICTED not in {f.rule_id for f in report.verdict.findings}
    assert {"TA-SUPERLATIVE-01", "TA-URGENCY-01"} <= {f.rule_id for f in report.verdict.findings}
    assert report.final_decision == Decision.REVISE
    assert run.total_cost_usd == 0


@pytest.mark.parametrize("text,expected", [
    ("쿠폰! 지금 확인하세요", Decision.APPROVE),
    ("폰! 지 사기", Decision.REJECT),
])
def test_manual_edit_and_proposed_rewrite_use_same_policy(text, expected, demo_settings, tracer):
    product = build_brief(PRESETS["commerce_landing"]).product
    original = AdCopy(headline=ORIGINAL, body="", cta="")
    edited = build_edited_copy(original, text, "", "", [])
    result = recheck(edited, product, {"ecommerce"}, demo_settings, tracer=tracer)
    rewrite = check_rewrite(edited, product, {"ecommerce"}, "", [], get_catalog(),
                            Router(demo_settings, tracer=tracer), "", tracer,
                            demo_settings.auto_decision_confidence)
    assert result.report.final_decision == rewrite.decision == expected
    assert (RESTRICTED in {f.rule_id for f in result.report.verdict.findings}) == (expected == Decision.REJECT)
    assert result.cost_usd == 0
