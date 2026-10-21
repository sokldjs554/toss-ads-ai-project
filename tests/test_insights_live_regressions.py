"""저장된 첫 실호출 보고서를 로컬로 재생한다. 새 API 실행이나 모델 성능 측정이 아니다."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import insights as ins
from core import insight_engine as ie
from core.llm_schemas_insight import InsightOut
from core.performance import generate
from evals.insights_eval import load_scenarios
from llm.base import LLMResponse


ROOT = Path(__file__).resolve().parents[1]
FIRST_REPORT = ROOT / "evals/reports/live_features/features-20261020-run1.json"
DATASET = ROOT / "evals/datasets/perf_holdout_20261020.jsonl"


@pytest.fixture(scope="module")
def saved_outputs():
    original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (FIRST_REPORT, DATASET)}
    report = json.loads(FIRST_REPORT.read_text(encoding="utf-8"))
    saved = {item["id"]: item["report"] for item in report["results"]["insights"]}
    scenarios = {scenario.id: scenario for scenario in load_scenarios(path=DATASET)}
    yield saved, scenarios
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in original} == original


def _replay(saved, scenario, *, summary=None):
    rows = generate(scenario.spec)
    analysis = ie.analyze(rows)
    # 첫 보고서의 남은 문장과 인용을 그대로 응답으로 사용한다. 삭제된 원본을 추측하지 않는다.
    parsed = InsightOut(
        headline=saved["headline"], summary=summary if summary is not None else saved["summary"],
        findings=saved["findings"],
        actions=[{key: action[key] for key in ("action_id", "why", "evidence_ids")} for action in saved["actions"]],
    )
    router = SimpleNamespace(complete=lambda request: LLMResponse(
        text="", parsed=parsed, provider="openai", model="offline-saved-output-replay"))
    return ins.build_report(rows, router=router, analysis=analysis, data_origin="synthetic")


@pytest.mark.parametrize("case_id,field,evidence_id", [
    ("h26-p056", "summary", "E25"),
    ("h26-p077", "headline", "E02"),
])
def test_saved_live_failure_preserves_supported_text_and_restores_its_evidence(saved_outputs, case_id, field, evidence_id):
    saved, scenarios = saved_outputs
    report = _replay(saved[case_id], scenarios[case_id])

    assert ins.audit_report(report) == []
    assert report.unsupported_numbers == 0
    assert getattr(report, field) == saved[case_id][field]
    assert report.narrative_source == "model"
    evidence = report.analysis.evidence[evidence_id]
    assert any(finding.text == evidence.text and evidence_id in finding.evidence_ids for finding in report.findings)
    assert {finding["text"] for finding in saved[case_id]["findings"]} <= {finding.text for finding in report.findings}


def test_invented_threshold_is_rejected_without_creating_evidence(saved_outputs):
    saved, scenarios = saved_outputs
    invented = "우선 확인 기준은 97.3%입니다."
    report = _replay(saved["h26-p056"], scenarios["h26-p056"], summary=invented)

    assert ins.audit_report(report) == [] and report.unsupported_numbers == 0
    assert report.summary != invented
    assert any(item.where == "summary" and item.text == invented and item.reason == "number_unsupported"
               for item in report.dropped_sentences)
    assert all("97.3%" not in finding.text for finding in report.findings)
    assert all(number.value != 97.3 for evidence in report.analysis.evidence.values() for number in evidence.numbers)


def test_all_twelve_saved_outputs_remain_auditable_after_assembly(saved_outputs):
    saved, scenarios = saved_outputs
    assert len(saved) == 12
    for case_id, output in saved.items():
        report = _replay(output, scenarios[case_id])
        assert ins.audit_report(report) == [], case_id
        assert report.unsupported_numbers == 0, case_id
