"""New quality contract in live-run storage/UI, with controlled local responses only."""
from __future__ import annotations

import json

import pytest

from streamlit.testing.v1 import AppTest

from core.config import Settings
from core.schemas import AdCopy
from evals import live_features as live, quality_signals
from evals.live_guard import BoundedProvider, LiveLedger
from llm.base import LLMResponse
from llm.errors import BudgetExceeded
from llm.router import Router
from tests.fakes import FakeProvider
from tests.test_quality_judge import _judgment, _wire_fixture
from ui import new_evals


def controlled_run(tmp_path, monkeypatch, *, contradict=False):
    run = object.__new__(live.FeatureRun)
    run.output = tmp_path / "controlled-quality.json"
    run.request = {"max_calls": 10}
    run.report = {"schema_version": 1, "kind": "controlled-test-not-real-api", "results": {g: [] for g in live.GROUPS}}
    run.ledger = LiveLedger(1, 10)
    run.settings = Settings.from_env(force_demo=False, openai_api_key="test-only", anthropic_api_key="")
    count = 0

    def respond(req):
        nonlocal count
        count += 1
        payload = json.loads(req.user.split("\n", 1)[1].split("\n</untrusted_data>", 1)[0])
        a, b = AdCopy(**payload["A"]), AdCopy(**payload["B"])
        raw = _judgment(a, b, "A" if a.cta == "견적 확인하기" else "B")
        if contradict and count == 2:
            raw["dimensions"][0].update(preference="A", impact="material")
        raw = _wire_fixture(raw)
        return LLMResponse(text=json.dumps(raw, ensure_ascii=False), parsed=req.schema.model_validate(raw),
                           provider="openai", model=live.OPENAI_MODEL, input_tokens=40, output_tokens=20)

    provider = FakeProvider("openai", model=live.OPENAI_MODEL, behavior=respond)
    run.providers = {"openai": BoundedProvider(provider, run.ledger)}
    run.primary = "openai"
    pair = next(p for p in quality_signals.load_pairs(split="holdout") if p.id == "QP-H05")
    monkeypatch.setattr(quality_signals, "load_pairs", lambda **kwargs: [pair])
    return run


def test_live_adapter_preserves_old_metric_and_new_acceptance_evidence(tmp_path, monkeypatch):
    run = controlled_run(tmp_path, monkeypatch)
    run.quality()
    stored = json.loads(run.output.read_text())
    row = stored["results"]["quality"][0]
    assert row["checks"] == {"position_consistent": True, "agrees_with_author_label": True}
    assert row["model_winner"] == row["accepted_winner"] == "a"
    assert row["quality_checks"]["evidence_valid"] is True
    assert row["quality_checks"]["dimension_consistent"] is True
    assert row["aggregation_status"] == "consensus"
    assert [o["displayed_winner"] for o in row["orders"]] == ["A", "B"]
    assert all(o["response"]["dimensions"] for o in row["orders"])
    assert all(o["wire_response"]["dimensions"] and o["wire_text"] and o["wire_contract_version"] for o in row["orders"])
    assert stored["summaries"]["quality"]["all_checks_passed"] == 1
    assert stored["quality_audit_summary"]["accepted_cases"] == 1
    assert row["call_ordinals"] == [1, 2]


def test_material_contradiction_is_not_accepted_even_when_historical_metric_is_green(tmp_path, monkeypatch):
    run = controlled_run(tmp_path, monkeypatch, contradict=True)
    run.quality()
    stored = json.loads(run.output.read_text())
    row = stored["results"]["quality"][0]
    assert all(row["checks"].values())  # Same raw metric as historical runs.
    assert row["accepted_winner"] == "abstain" and row["model_winner"] == "a"
    assert row["aggregation_status"] == "rubric_disagreement"
    summary = stored["quality_audit_summary"]
    assert summary["raw_stable_author_matches"] == 1
    assert summary["accepted_cases"] == summary["accepted_author_matches"] == 0
    assert summary["abstained_cases"] == 1 and summary["by_status"] == {"rubric_disagreement": 1}


def test_view_exposes_abstention_separately_from_raw_author_agreement(tmp_path, monkeypatch):
    run = controlled_run(tmp_path, monkeypatch, contradict=True)
    run.quality()
    report = json.loads(run.output.read_text())
    report.update(run_id="controlled-quality-test", status="completed", limitations=["통제된 테스트, 실제 API 결과 아님"])
    report["manifest"] = {"planned_cases": {"quality": 1}}
    directory = tmp_path / "live_features"
    directory.mkdir()
    (directory / "quality.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    monkeypatch.setattr(Router, "complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("display must not call a provider")))
    at = AppTest.from_string("from ui import new_evals\nnew_evals._live()", default_timeout=60).run()
    assert not at.exception
    text = "\n".join(str(v.value) for v in (*at.markdown, *at.caption, *at.warning))
    for phrase in ("문구 비교 · 채택과 보류", "기존 비교 지표", "판단 보류", "중요한 근거가 달라짐", "0/1", "1/1"):
        assert phrase in text


def test_budget_stop_keeps_the_first_order_in_live_checkpoint(tmp_path, monkeypatch):
    run = controlled_run(tmp_path, monkeypatch)
    run.ledger.max_calls = 1
    with pytest.raises(BudgetExceeded):
        run.quality()
    stored = json.loads(run.output.read_text())
    row = stored["results"]["quality"][0]
    assert row["execution"] == "budget_stopped"
    assert row["orders"][0]["response"]["winner"] == "A"
    assert row["orders"][1]["error_type"] == "BudgetExceeded"
    assert row["accepted_winner"] == "abstain"
    assert stored["ledger"]["attempts"] == 1 and stored["ledger"]["stopped"] is True


def test_missing_planned_supplement_cannot_be_shown_as_a_completed_run(tmp_path, monkeypatch):
    directory = tmp_path / "live_features"
    directory.mkdir()
    report = {"schema_version": 1, "run_id": "controlled-unattempted", "status": "completed", "results": {}, "summaries": {},
              "manifest": {"planned_cases": {}, "planned_supplemental_cases": {"rewrite": 4}}, "ledger": {}, "limitations": []}
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    at = AppTest.from_string("from ui import new_evals\nnew_evals._live()", default_timeout=60).run()
    assert not at.exception
    assert at.expander[0].label.endswith("일부만 실행")
    assert any("위반 원문의 수정과 재검사" in str(m.value) for m in at.markdown)
