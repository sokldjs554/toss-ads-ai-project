"""실제 평가의 입력 고정·실패 분모·결과 보존. 네트워크 호출은 하지 않는다."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from evals import live_features as live
from evals.live_guard import BoundedProvider, LiveLedger
from llm.anthropic_provider import AnthropicProvider
from llm.base import LLMRequest, LLMResponse, Task
from llm.errors import BudgetExceeded
from llm.openai_provider import OpenAIProvider
from core.config import Settings
from llm.mock_provider import MockProvider
from llm.router import Router
from observability.tracer import Tracer


def test_failed_cases_stay_in_the_denominator_and_are_not_empty_successes():
    rows = [{"id": "a", "execution": "completed", "checks": {"safe": True}, "successful_calls": 1},
            {"id": "b", "execution": "failed", "checks": {}},
            {"id": "c", "execution": "completed", "checks": {"safe": False}, "successful_calls": 1}]
    summary = live.case_summary(rows)
    assert summary == {"attempted_cases": 3, "completed_cases": 2, "all_checks_passed": 1,
                       "cases_with_real_calls": 2, "failed_case_ids": ["b", "c"]}


def test_incomplete_case_is_checkpointed_without_exception_secret(tmp_path):
    run = object.__new__(live.FeatureRun)
    run.output = tmp_path / "result.json"
    run.report = {"results": {group: [] for group in live.GROUPS}}
    run.ledger = LiveLedger(1, 3)

    def bad():
        raise RuntimeError("secret-key-must-not-be-exported")

    run.case("vision", "a", bad)
    stored = json.loads(run.output.read_text())
    assert stored["summaries"]["vision"]["completed_cases"] == 0
    assert stored["results"]["vision"][0]["error_type"] == "RuntimeError"
    assert "secret-key" not in run.output.read_text()


def test_input_hashes_include_raster_bytes_and_labels():
    path = live.ROOT / "evals/datasets/live_vision_20261020/manifest.json"
    data = json.loads(path.read_text())
    assert len(data["cases"]) == 12
    assert {c["must_not_approve"] for c in data["cases"]} == {True, False}
    for row in data["cases"]:
        assert live.digest(live.safe_path(row["image"])) == row["sha256"]


def test_report_write_preserves_previous_result_when_json_is_invalid(tmp_path):
    target = tmp_path / "report.json"
    live.write_json(target, {"measured": 1})
    with pytest.raises(ValueError):
        live.write_json(target, {"invalid": float("nan")})
    assert json.loads(target.read_text()) == {"measured": 1}


def test_source_hash_detects_code_drift(monkeypatch):
    monkeypatch.setattr(live, "source_fingerprints", lambda: {"agents/a.py": "changed"})
    request = {"version": 1, "run_id": "test", "cost_cap_usd": 1, "max_calls": 20,
               "groups": list(live.GROUPS), "models": {"openai": live.OPENAI_MODEL, "anthropic": live.ANTHROPIC_MODEL},
               "source_sha256": {"agents/a.py": "frozen"}}
    with pytest.raises(ValueError, match="source differs"):
        live.validate_request(request)


def test_openai_records_resolved_model_and_explicit_effort_without_sampling_params():
    captured = {}

    class Client:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(model="server-returned-snapshot", usage=types.SimpleNamespace(prompt_tokens=40, completion_tokens=8),
                choices=[types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(content="{}", refusal=None))])

    provider = OpenAIProvider("test-only", live.OPENAI_MODEL, max_retries=0, reasoning_effort="none")
    provider._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Client()))
    result = provider.complete(LLMRequest(Task.JUDGE, "s", "u", max_tokens=400))
    assert captured["reasoning_effort"] == "none" and "temperature" not in captured
    assert result.model == "server-returned-snapshot"


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
@pytest.mark.parametrize("model_field", [{}, {"model": None}, {"model": ""}], ids=["absent", "null", "empty"])
def test_missing_returned_model_stops_evaluation_and_retains_reservation(provider_name, model_field, monkeypatch):
    """SDK 응답의 모델 누락을 요청 모델명으로 채워 실제 사용 모델이라고 주장하면 안 된다."""
    if provider_name == "openai":
        provider = OpenAIProvider("test-only", live.OPENAI_MODEL, max_retries=0)
        raw = types.SimpleNamespace(
            **model_field, usage=types.SimpleNamespace(prompt_tokens=40, completion_tokens=8),
            choices=[types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(content="{}", refusal=None))])
    else:
        provider = AnthropicProvider("test-only", live.ANTHROPIC_MODEL, max_retries=0)
        raw = types.SimpleNamespace(
            **model_field, usage=types.SimpleNamespace(input_tokens=40, output_tokens=8), stop_reason="end_turn",
            content=[types.SimpleNamespace(type="text", text="{}")])

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return raw

    endpoint = types.SimpleNamespace(create=create)
    client = (types.SimpleNamespace(chat=types.SimpleNamespace(completions=endpoint)) if provider_name == "openai"
              else types.SimpleNamespace(messages=endpoint))
    monkeypatch.setattr(provider, "_get_client", lambda: client)  # SDK 클라이언트·네트워크를 만들지 않는다.
    snapshots = []
    ledger = LiveLedger(1, 2, checkpoint=snapshots.append)

    with pytest.raises(BudgetExceeded, match="model/usage"):
        BoundedProvider(provider, ledger).complete(LLMRequest(Task.JUDGE, "s", "u", max_tokens=400))

    assert len(calls) == 1
    final = snapshots[-1]
    rec = final["calls"][0]
    assert final["stopped"] is True
    assert final["successful_calls"] == 0 and final["unknown_cost_calls"] == 1
    assert rec["status"] == "unverified_usage"
    assert rec["usage_cost_usd"] is None
    assert final["accounted_usd"] == rec["accounted_usd"] == rec["reserved_usd"] > 0
    assert rec["reserved_usd"] == snapshots[0]["calls"][0]["reserved_usd"]


def test_unreadable_vision_response_does_not_get_ocr_credit_for_declared_text(tmp_path, monkeypatch):
    """V05–V08의 선언문 대체 경로를 실제 읽기 성공으로 세지 않는다. 모든 응답은 로컬 대역이다."""
    from core.llm_schemas_vision import VisionOut

    run = object.__new__(live.FeatureRun)
    run.output = tmp_path / "unreadable-vision-harness.json"
    run.request = {"vision_manifest": "evals/datasets/live_vision_20261020/manifest.json"}
    run.report = {"kind": "controlled-test-not-real-model", "results": {group: [] for group in live.GROUPS}}
    run.ledger = LiveLedger(1, 12)
    run.settings = Settings.from_env(force_demo=True, openai_api_key="", anthropic_api_key="")
    run.providers = {"openai": object()}

    def unreadable_response(req):
        return LLMResponse(
            text="{}", parsed=VisionOut(readable=False, text_segments=[], tags=[], notes="unreadable"),
            provider="openai", model=live.OPENAI_MODEL, input_tokens=40, output_tokens=8)

    router = types.SimpleNamespace(tracer=Tracer(), complete=unreadable_response)
    monkeypatch.setattr(run, "router", lambda: router)
    run.vision()

    saved = json.loads(run.output.read_text())
    rows = {row["id"]: row for row in saved["results"]["vision"]}
    for case_id in ("V05", "V06", "V07", "V08"):
        row = rows[case_id]
        assert row["execution"] == "completed"
        assert row["text_source"] == "declared" and row["unreadable"] is True
        assert row["read_segments"]  # 대체 선언문이 있어도 OCR 정답으로 사용할 수 없다.
        assert row["checks"]["real_response"] is True
        assert row["checks"]["workflow_decision"] is True
        assert row["checks"]["visible_text_recovered"] is False
        assert row["recovered_lines"] and not any(row["recovered_lines"])
        assert case_id in saved["summaries"]["vision"]["failed_case_ids"]
    assert saved["ledger"]["attempts"] == 0  # 실제 제공자나 API는 호출하지 않았다.


def test_validation_never_accepts_a_file_outside_the_repository():
    with pytest.raises(ValueError, match="repository file"):
        live.safe_path("/etc/hosts")


def test_all_feature_adapters_execute_without_losing_results_in_controlled_harness(tmp_path, monkeypatch):
    """평가 adapter의 연결 검증이다. 생성된 파일은 테스트 임시 폴더에만 있고 모델 성능으로 쓰지 않는다."""
    run = object.__new__(live.FeatureRun)
    run.output = tmp_path / "controlled-harness.json"
    run.request = {"vision_manifest": "evals/datasets/live_vision_20261020/manifest.json",
                   "dialogs_path": "evals/datasets/dialogs_regression_v2.jsonl",
                   "insight_ids": ["holdout-no_event-1", "holdout-tracking_break-1"]}
    run.report = {"kind": "controlled-test-not-real-model", "results": {group: [] for group in live.GROUPS}}
    run.ledger = LiveLedger(1, 5)
    run.settings = Settings.from_env(force_demo=True, openai_api_key="", anthropic_api_key="")
    run.providers = {"mock": MockProvider()}
    monkeypatch.setattr(run, "router", lambda **kwargs: Router(run.settings, tracer=Tracer()))
    # 새 품질 계약을 연결하는 통제된 응답이다. 대역 점수로 선호를 만들어
    # 실제 모델 성공처럼 세지 않고, 출처가 있는 명시적 ABSTAIN을 보낸다.
    def abstaining_judge(self, req):
        return {"dimensions": [
            {"criterion": criterion, "judgment": "UNKNOWN",
             "evidence_a": [{"field": field, "quote": req.context["copies"]["A"][field]}],
             "evidence_b": [{"field": field, "quote": req.context["copies"]["B"][field]}],
             "fact_ids": [], "reason": "통제된 연결 검사이며 문구 선호를 평가하지 않음"}
            for criterion, field in (("information", "body"), ("conditions", "body"), ("action", "cta"), ("expression", "body"))
        ], "winner": "ABSTAIN", "decisive_criteria": [], "reason": "통제된 연결 검사"}

    monkeypatch.setattr(MockProvider, "_judge", abstaining_judge)
    # 대화·품질 adapter의 모든 조합을 반복하기보다 한 입력이 결과까지 연결되는지 확인한다.
    from evals import dialogs, quality_signals
    load_dialogs, load_pairs = dialogs.load_dialogs, quality_signals.load_pairs
    monkeypatch.setattr(dialogs, "load_dialogs", lambda **kwargs: load_dialogs(**kwargs)[:1])
    monkeypatch.setattr(quality_signals, "load_pairs", lambda **kwargs: load_pairs(**kwargs)[:1])
    result = run.run()
    assert result["status"] == "completed"
    expected = {"vision": 12, "conversation": 1, "insights": 2, "quality": 1, "pipeline": 4}
    assert {group: summary["completed_cases"] for group, summary in result["summaries"].items()} == expected
    assert result["ledger"]["attempts"] == 0  # 실제 제공자 호출은 전혀 없었다.
    assert result["quality_audit_summary"]["accepted_cases"] == 0
    conversation = result["results"]["conversation"][0]
    assert conversation["plan"]["total_budget_krw"] == conversation["plan_total"]
    assert all(ad_set["audience"] for ad_set in conversation["plan"]["ad_sets"])
    assert conversation["slots"]["monthly_budget_krw"]["evidence"]
    assert conversation["turns"][0]["plan_snapshot"] == conversation["plan"]


def test_dialog_records_do_not_lose_the_plan_that_was_warned_about():
    """최종 숫자만으로 실제 타겟과 검사 오류를 구분할 수 없던 관측 누락을 막는다."""
    from evals.dialogs import run_dialog

    run = run_dialog({"id": "local-observation", "turns": [
        "상품명은 소담 커피, 카페 방문이 목표예요. 월 예산은 120만원, 타겟은 20대 직장인이에요.",
        "월 예산을 200만원으로 바꿔주세요.",
    ]})
    assert not run.error
    first, second = run.turns
    assert first.plan_snapshot["total_budget_krw"] == 1_200_000
    assert second.plan_snapshot["total_budget_krw"] == 2_000_000
    assert first.slot_evidence["monthly_budget_krw"]["value"] == 1_200_000
    assert second.slot_evidence["monthly_budget_krw"]["value"] == 2_000_000
    assert first.slot_evidence["monthly_budget_krw"]["evidence"][0]["quote"]
    # 저장 뒤 상태를 바꿔도 앞선 턴의 판단 근거를 덮어쓰지 않는다.
    run.final.plan_preview.total_budget_krw = 9_000_000
    run.final.slots["monthly_budget_krw"].value = 9_000_000
    assert second.plan_snapshot["total_budget_krw"] == 2_000_000
    assert second.slot_evidence["monthly_budget_krw"]["value"] == 2_000_000
