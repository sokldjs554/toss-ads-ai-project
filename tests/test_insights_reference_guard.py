"""이전 기준 기간의 문장 경계 회귀. 로컬 응답만 쓰며 API를 호출하지 않는다."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core import performance as P
from core.llm_schemas_insight import InsightOut
from llm.base import LLMResponse


@pytest.fixture(scope="module")
def earlier_reference():
    rows = P.generate(P.ScenarioSpec(
        "reference-guard-regression", 94019, scale=2.0, noise=0.8,
        events=(P.EventSpec("budget_cap", 19, None, {"cap_frac": 0.45}),)))
    analysis = ie.analyze(rows)
    assert analysis.reference_evidence
    assert analysis.baseline[1] < analysis.window[0]
    assert analysis.incidents[0].start == analysis.window[0]
    return rows, analysis


def _parsed(text, ids):
    return InsightOut(
        headline="관측 자료를 확인했습니다.", summary="기준 기간과 최근 자료를 비교했습니다.",
        findings=[{"text": text, "evidence_ids": ids}], actions=[])


def _report(rows, analysis, parsed):
    router = SimpleNamespace(complete=lambda req: LLMResponse(
        text="", parsed=parsed, provider="openai", model="offline-scripted-response"))
    return ins.build_report(rows, router=router, analysis=analysis, data_origin="synthetic")


@pytest.mark.parametrize("claim", [
    "변화는 {date}에 시작했습니다.",
    "실제 변화는 {date}부터 발생했습니다.",
    "최근 기간 첫날에 실제 변화가 시작됐습니다.",
])
def test_unknown_actual_onset_is_not_inferred_from_the_observation_boundary(earlier_reference, claim):
    rows, analysis = earlier_reference
    claim = claim.format(date=ie.fmt_date(analysis.window[0]))
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]
    parsed = _parsed(claim, ids)

    _, _, kept, _, dropped = ins.validate_model_output(parsed, analysis)
    assert kept == []
    assert any(item.where == "finding" and item.text == claim for item in dropped)
    report = _report(rows, analysis, parsed)
    assert claim not in [finding.text for finding in report.findings]
    assert ins.audit_report(report) == []


def test_reverse_date_actual_onset_assertion_is_rejected(earlier_reference):
    rows, analysis = earlier_reference
    claim = f"{ie.fmt_date(analysis.window[0])}이 실제 시작일입니다."
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]
    parsed = _parsed(claim, ids)

    _, _, kept, _, dropped = ins.validate_model_output(parsed, analysis)
    assert kept == []
    assert any(item.where == "finding" and item.text == claim for item in dropped)
    report = _report(rows, analysis, parsed)
    assert claim not in [finding.text for finding in report.findings]
    assert ins.audit_report(report) == []


@pytest.mark.parametrize("where", ["headline", "summary"])
def test_unknown_onset_assertions_are_rejected_in_headline_and_summary(earlier_reference, where):
    rows, analysis = earlier_reference
    start, end = (ie.fmt_date(day) for day in analysis.window)
    claim = f"변화는 {start}에 시작했습니다."
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]
    parsed = _parsed(f"{start}부터 {end}까지 변화가 관측됐습니다.", ids)
    setattr(parsed, where, claim)

    headline, summary, _, _, dropped = ins.validate_model_output(parsed, analysis)
    assert {"headline": headline, "summary": summary}[where] is None
    assert any(item.where == where and item.text == claim for item in dropped)
    report = _report(rows, analysis, parsed)
    assert claim not in [report.headline, report.summary]
    assert ins.audit_report(report) == []


def test_a_bare_reference_citation_cannot_suppress_the_baseline_limitation(earlier_reference):
    rows, analysis = earlier_reference
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]
    report = _report(rows, analysis, _parsed("자료를 점검했습니다.", ids))

    canonical = analysis.evidence[analysis.reference_evidence].text
    assert any(finding.text == canonical and analysis.reference_evidence in finding.evidence_ids
               for finding in report.findings)
    assert "reference" in report.filled_from_template
    assert ins.audit_report(report) == []


@pytest.mark.parametrize("statement", [
    "관측 구간은 {start}에 시작합니다.",
    "{start}부터 {end}까지 변화가 관측됐습니다.",
])
def test_supported_observation_window_wording_remains_allowed(earlier_reference, statement):
    rows, analysis = earlier_reference
    statement = statement.format(start=ie.fmt_date(analysis.window[0]), end=ie.fmt_date(analysis.window[1]))
    parsed = _parsed(statement, [analysis.diagnoses[0].incident_evidence])

    _, _, kept, _, dropped = ins.validate_model_output(parsed, analysis)
    assert [finding.text for finding in kept] == [statement]
    assert not any(item.where == "finding" for item in dropped)
    assert ins.audit_report(_report(rows, analysis, parsed)) == []


@pytest.mark.parametrize("statement", [
    "관측 구간은 {start}에 시작했습니다.",
    "실제 시작일은 확인할 수 없습니다.",
], ids=["past-observation-window", "unknown-onset-disclaimer"])
def test_past_observation_and_unknown_onset_disclaimer_remain_allowed(earlier_reference, statement):
    rows, analysis = earlier_reference
    statement = statement.format(start=ie.fmt_date(analysis.window[0]))
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]
    parsed = _parsed(statement, ids)

    _, _, kept, _, dropped = ins.validate_model_output(parsed, analysis)
    assert [finding.text for finding in kept] == [statement]
    assert not any(item.where == "finding" for item in dropped)
    assert ins.audit_report(_report(rows, analysis, parsed)) == []


def test_unknown_onset_speed_disclaimer_remains_allowed(earlier_reference):
    rows, analysis = earlier_reference
    statement = "표시 기간 첫날에 이미 변화가 있어 서서히 시작했는지 단번에 시작했는지 확인하지 못했습니다."
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]
    parsed = _parsed(statement, ids)

    _, _, kept, _, dropped = ins.validate_model_output(parsed, analysis)
    assert [finding.text for finding in kept] == [statement]
    assert not any(item.where == "finding" for item in dropped)
    assert ins.audit_report(_report(rows, analysis, parsed)) == []


def test_onset_assertion_is_not_excused_by_a_later_uncertainty_sentence(earlier_reference):
    _, analysis = earlier_reference
    statement = f"변화는 {ie.fmt_date(analysis.window[0])}에 시작했습니다. 실제 시작일은 확인할 수 없습니다."
    ids = [analysis.diagnoses[0].incident_evidence, analysis.reference_evidence]

    _, _, kept, _, dropped = ins.validate_model_output(_parsed(statement, ids), analysis)
    assert kept == []
    assert any(item.where == "finding" and item.text == statement for item in dropped)


def test_canonical_reference_note_is_accepted_without_duplicate_fallback(earlier_reference):
    rows, analysis = earlier_reference
    canonical = analysis.evidence[analysis.reference_evidence].text
    parsed = _parsed(canonical, [analysis.reference_evidence])

    _, _, kept, _, dropped = ins.validate_model_output(parsed, analysis)
    assert [finding.text for finding in kept] == [canonical]
    assert not any(item.where == "finding" for item in dropped)
    report = _report(rows, analysis, parsed)
    assert sum(finding.text == canonical for finding in report.findings) == 1
    assert "reference" not in report.filled_from_template
    assert ins.audit_report(report) == []
