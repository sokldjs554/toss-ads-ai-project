"""'품질 확인' 의 새 기능 평가 패널: 저장된 리포트를 그대로 보여 주고, 한계 문장이 함께 나오며, 리포트가 없어도 화면이 깨지지 않는다."""
import json
import os

import pytest
from streamlit.testing.v1 import AppTest

from ui import new_evals


@pytest.fixture
def quality_view(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    at = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"), default_timeout=120)
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    assert not at.exception, [e.value[:200] for e in at.exception]
    return at


def _all_text(at) -> str:
    out = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "metric", "expander"):
        out += [str(getattr(e, "value", "") or getattr(e, "label", "")) for e in getattr(at, kind)]
    return "\n".join(out)


def test_the_panel_lists_the_five_features_and_the_synthetic_data_warning(quality_view):
    text = _all_text(quality_view)
    for title, _ in new_evals.SECTIONS:
        assert title in text
    assert "새 기능 평가 (합성 데이터)" in text and "독립 검증은 없으며" in text


def test_every_section_states_that_the_numbers_are_not_real_model_or_real_data(quality_view):
    for title, fn in new_evals.SECTIONS:
        at = AppTest.from_string(f"from ui import new_evals\nnew_evals.{fn.__name__}()", default_timeout=60)
        at.run()
        assert not at.exception, (title, [e.value[:200] for e in at.exception])
        text = _all_text(at)
        assert "측정하지 않았" in text or "정확도가 아닙니다" in text or "합성" in text, title


def test_numbers_come_from_the_stored_reports_not_from_the_view():
    """화면의 개발용·최종 확인용 숫자가 저장된 리포트의 값과 같다 (여기서 새로 계산하지 않는다)."""
    rep = new_evals._load("insights_controlled.json")
    dev, ho = new_evals._two(rep["splits"], lambda s: new_evals._str_frac(s["cause_top1_n"]))
    k, n = rep["splits"]["dev"]["cause_top1_n"].split("/")
    assert dev.startswith(f"{k}/{n}")
    k, n = rep["splits"]["holdout"]["cause_top1_n"].split("/")
    assert ho.startswith(f"{k}/{n}")


def test_a_missing_report_shows_a_message_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    at = AppTest.from_string("from ui import new_evals\nnew_evals.render()", default_timeout=60)
    at.run()
    assert not at.exception, [e.value[:200] for e in at.exception]


def test_a_broken_report_shape_degrades_to_dashes(monkeypatch, tmp_path):
    for name in ("dialogs_controlled.json", "url_ingest_eval.json", "image_eval.json", "quality_signals.json", "insights_controlled.json"):
        (tmp_path / name).write_text(json.dumps({"splits": {"dev": {"x": 1}, "holdout": {}}, "by_split": {"dev": {}, "holdout": {"y": 2}}}), encoding="utf-8")
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    for _, fn in new_evals.SECTIONS:
        fn()   # 예외 없이 끝나야 한다 (AppTest 밖이라 st 호출은 무시된다)
    assert new_evals._frac(None, 3) == "—" and new_evals._str_frac("x") == "—" and new_evals._pct("a") == "—"


def test_insights_distinguishes_followup_regression_from_the_historical_record(monkeypatch, tmp_path):
    original = new_evals._load("insights_controlled.json")
    followup = json.loads(json.dumps(original))
    followup["holdout_role"] = "regression"
    followup["engine_changed_after_holdout"] = True
    followup["splits"]["holdout"]["control_false_alarm_n"] = "0/4"
    (tmp_path / "insights_controlled.json").write_text(json.dumps(original), encoding="utf-8")
    (tmp_path / "insights_followup_regression.json").write_text(json.dumps(followup), encoding="utf-8")
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    at = AppTest.from_string("from ui import new_evals\nnew_evals._insights()", default_timeout=60).run()
    assert not at.exception
    text = _all_text(at)
    assert "후속 엔진 회귀 검사" in text and "처음 저장한 평가 기록" in text
    assert "0/4" in text and "2/4" in text and "새 독립 평가가 아닙니다" in text
    assert all(word not in text for word in ("오탐", "미탐", "오경보율"))


@pytest.fixture
def stored_live_report(monkeypatch, tmp_path):
    """저장 파일 표시만 검사한다. 제공자·라우터 호출은 실패시키고 호출 여부도 확인한다."""
    from llm.anthropic_provider import AnthropicProvider
    from llm.openai_provider import OpenAIProvider
    from llm.router import Router

    attempted_network = []

    def forbidden_call(*args, **kwargs):
        attempted_network.append(True)
        raise AssertionError("stored report views must not call a model")

    for cls in (Router, OpenAIProvider, AnthropicProvider):
        monkeypatch.setattr(cls, "complete", forbidden_call)
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    directory = tmp_path / "live_features"
    directory.mkdir()
    path = directory / "offline-partial.json"
    report = {
        "schema_version": 1, "run_id": "offline-partial-synthetic", "source_sha": "test-only-source",
        "status": "incomplete_budget_or_provider_failure", "configured_providers": ["openai", "anthropic"],
        "manifest": {"planned_cases": {"vision": 12, "conversation": 20, "insights": 12, "quality": 28, "pipeline": 4},
                     "models": {"openai": "configured-openai-model", "anthropic": "unused-anthropic-model"}},
        "results": {"vision": [{"id": "V01"}, {"id": "V02"}]},
        "summaries": {"vision": {"attempted_cases": 2, "completed_cases": 2,
                                 "cases_with_real_calls": 1, "all_checks_passed": 1}},
        "ledger": {"attempts": 2, "successful_calls": 1, "unknown_cost_calls": 1,
                   "usage_cost_usd": 0.0134, "accounted_usd": 0.0878,
                   "calls": [
                       {"status": "success", "requested_model": "configured-openai-model",
                        "returned_model": "verified-returned-model"},
                       {"status": "unverified_usage", "requested_model": "unused-anthropic-model",
                        "returned_model": "unverified-returned-model"}]},
        "limitations": ["테스트용 합성 저장 자료이며 실제 모델 측정 결과가 아닙니다."],
    }
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    yield report, path
    assert not attempted_network


def _live_app():
    at = AppTest.from_string("from ui import new_evals\nnew_evals._live()", default_timeout=60).run()
    assert not at.exception, [e.value[:200] for e in at.exception]
    return at


def test_partial_live_report_shows_planned_denominator_and_reserved_unknown_cost(stored_live_report):
    text = _all_text(_live_app())
    assert "일부만 실행" in text
    assert "2/12" in text and "1/2" in text
    assert "$0.0134" in text and "$0.0878" in text


def test_live_report_completed_flag_cannot_hide_unattempted_planned_cases(stored_live_report):
    report, path = stored_live_report
    report["status"] = "completed"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    at = _live_app()
    assert at.expander[0].label.endswith("일부만 실행")
    assert "2/12" in _all_text(at) and "0/20" in _all_text(at)
    assert any("시도하지 않은 사례" in warning.value for warning in at.warning)


@pytest.mark.parametrize("status,label", [
    ("completed", "완료"),
    ("incomplete_budget_or_provider_failure", "일부만 실행"),
    ("interrupted", "실행 중단"),
    ("running", "실행 중"),
])
def test_live_report_status_is_shown_in_korean(stored_live_report, status, label):
    report, path = stored_live_report
    report["status"] = status
    if status == "completed":
        report["manifest"]["planned_cases"] = {"vision": 2}
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    text = _all_text(_live_app())
    assert label in text
    assert status not in text


def test_live_report_lists_only_models_from_verified_successful_responses(stored_live_report):
    text = _all_text(_live_app())
    assert "verified-returned-model" in text
    for unused in ("configured-openai-model", "unused-anthropic-model", "unverified-returned-model"):
        assert unused not in text


def test_live_report_numbers_follow_changes_to_the_stored_json(stored_live_report):
    report, path = stored_live_report
    at = _live_app()
    before = _all_text(at)
    assert "1/2" in before and "$0.0134" in before
    report["summaries"]["vision"].update(attempted_cases=3, completed_cases=3,
                                         cases_with_real_calls=2, all_checks_passed=2)
    report["ledger"].update(attempts=3, successful_calls=2, usage_cost_usd=0.0251, accounted_usd=0.1013)
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    at.run()
    assert not at.exception
    after = _all_text(at)
    assert "3/12" in after and "2/3" in after
    assert "$0.0251" in after and "$0.1013" in after
    assert "$0.0134" not in after and "$0.0878" not in after


def test_url_followup_is_displayed_before_the_preserved_historical_record(monkeypatch, tmp_path):
    def report(price):
        split = {"pages": 12, "name_accuracy": 1.0, "price_accuracy": price,
                 "fact_recall": 0.8, "fact_precision": 0.9, "leak_count": 0, "forbidden_total": 4,
                 "known_gap": {"leaked": 0, "total": 3}}
        return {"by_split": {"dev": dict(split), "holdout": dict(split)}}

    historical, followup = report(0.25), report(0.875)
    (tmp_path / "url_ingest_eval.json").write_text(json.dumps(historical), encoding="utf-8")
    (tmp_path / "url_ingest_followup_20261020.json").write_text(json.dumps(followup), encoding="utf-8")
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    at = AppTest.from_string("from ui import new_evals\nnew_evals._urls()", default_timeout=60).run()
    assert not at.exception
    text = _all_text(at)
    assert "수정 후 같은 페이지 재검사" in text
    assert "처음 저장한 페이지 평가 기록" in [expander.label for expander in at.expander]
    tables = [element.value for element in at.markdown if "pf-tbl" in element.value]
    assert len(tables) == 2
    assert "87.5%" in tables[0] and "25.0%" in tables[1]
    assert new_evals.ROWS["urls"][0] == "url_ingest_eval.json"
    assert new_evals.rows_for("urls") == new_evals.rows_urls(historical)
