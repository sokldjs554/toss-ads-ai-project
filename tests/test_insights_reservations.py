"""실제 2차 응답에서 발견한 유보 표현의 경계. 새 모델 평가가 아닌 회귀 검사다."""
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


@pytest.mark.parametrize("text", [
    "원인을 단정할 수 없습니다.",
    "원인은 단정할 수 없어요.",
    "다만 이것만으로 원인을 단정할 수는 없습니다.",
    "다만 이것만으로 정상이라고 단정할 수는 없습니다.",
    "현재 관측만으로 원인을 단정하기 어렵습니다.",
    "다만 원인은 단정할 수 없고, 전환 추적 오류 가설과 맞는 신호가 있습니다.",
    "다만 이는 원인 단정이 아니라 확인이 필요한 가설입니다.",
])
def test_reserving_judgment_is_not_itself_a_causal_assertion(text):
    assert ins.causal_issue(text, set(), {"tracking_break": 0.9}) == ""


@pytest.mark.parametrize("text", [
    "원인을 단정할 수 없습니다. 하지만 원인은 전환 추적 오류입니다.",
    "원인을 단정할 수는 없지만 전환 추적 오류로 전환이 줄었습니다.",
    "원인을 단정할 수 없고 전환 추적 오류로 전환이 줄었습니다.",
    "원인을 단정할 수 없다는 뜻은 아닙니다.",
    "원인을 단정할 수 없다는 것은 아닙니다.",
    "원인을 단정할 수 없다는 건 아닙니다.",
    "원인을 단정할 수 없다는 말은 사실이 아닙니다.",
    "원인을 단정하기 어렵다는 뜻은 아닙니다.",
    "원인을 단정할 수 없지 않습니다.",
    "전환 추적 오류로 전환이 줄어서 원인을 단정할 수 없습니다.",
    "이는 원인 단정이 아니라 전환 추적 오류가 원인입니다.",
    "전환 추적 오류가 원인입니다 (원인을 단정할 수 없습니다).",
])
def test_reservation_cannot_hide_an_assertion_or_reverse_its_own_meaning(text):
    assert ins.causal_issue(text, {"tracking_break"}, {"tracking_break": 1.0}).startswith("cause_overclaim")


@pytest.mark.parametrize("case_id", ["h26-p040", "h26-p073"])
def test_saved_second_run_summary_keeps_a_supported_reservation(case_id):
    root = Path(__file__).resolve().parents[1]
    path = root / "evals/reports/live_features/features-20261020-run2.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    measured = json.loads(path.read_text(encoding="utf-8"))
    saved = next(item["report"] for item in measured["results"]["insights"] if item["id"] == case_id)
    summary = next(item["text"] for item in saved["dropped_sentences"] if item["where"] == "summary")
    scenario = next(s for s in load_scenarios(path=root / "evals/datasets/perf_holdout_20261020.jsonl") if s.id == case_id)
    rows = generate(scenario.spec)
    analysis = ie.analyze(rows)
    # 저장된 summary의 전체 문장을 돌려놓는다. 저장되지 않은 finding의 인용 ID는 추측하지 않는다.
    parsed = InsightOut(
        headline=saved["headline"], summary=summary, findings=saved["findings"],
        actions=[{key: action[key] for key in ("action_id", "why", "evidence_ids")} for action in saved["actions"]],
    )
    router = SimpleNamespace(complete=lambda request: LLMResponse(
        text="", parsed=parsed, provider="openai", model="offline-saved-output-replay"))
    report = ins.build_report(rows, router=router, analysis=analysis, data_origin="synthetic")
    assert report.summary == summary
    assert report.narrative_source == "model"
    assert ins.audit_report(report) == []
    assert not any(item.where == "summary" for item in report.dropped_sentences)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
