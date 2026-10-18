"""GitHub Actions 워크플로 정의 검사 — 실제로 돌려 보지 않고도 '유료 실행의 결과가 저장되는 구조인가' 를 확인한다.

과거에 `live` 작업이 만들지 않는 파일을 `git add` 해 커밋 단계가 실패하면서 유료 호출의 결과가 전부 버려진 적이 있다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(doc: dict) -> dict:
    return doc.get("on") or doc.get(True) or {}   # YAML 1.1 은 키 `on` 을 True 로 읽는다


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_workflow_parses_and_has_jobs(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["jobs"] and triggers(doc), path.name


def _steps_text(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


def test_live_eval_commit_step_only_adds_files_its_job_can_produce():
    doc = load("live-eval.yml")
    live, compare = doc["jobs"]["live"], doc["jobs"]["compare"]
    live_text, compare_text = _steps_text(live), _steps_text(compare)
    # live 작업은 방식 비교를 돌리지 않으므로 그 결과 파일을 git add 하면 안 된다 (없는 경로를 add 하면 커밋 단계가 실패한다)
    add_line = next(l for l in live_text.splitlines() if l.strip().startswith("git add"))
    assert "compare_live.json" not in add_line and "evals.compare" not in live_text
    # compare 작업이 자기 결과만, 파일이 있을 때만 add 한다
    assert re.search(r"if \[ -f evals/reports/compare_live\.json \]; then git add evals/reports/compare_live\.json; fi", compare_text)
    assert "evals.compare" in compare_text and "--cost-cap" in compare_text and "--out compare_live.json" in compare_text


def test_live_eval_compare_is_off_by_default_and_capped_within_the_approved_budget():
    doc = load("live-eval.yml")
    inputs = triggers(doc)["workflow_dispatch"]["inputs"]
    assert str(inputs["compare"]["default"]).lower() == "false"
    assert float(inputs["compare_cost_cap"]["default"]) <= 3.5
    compare = doc["jobs"]["compare"]
    assert "inputs.compare == 'true'" in compare["if"]            # 입력으로 켠 경우에만 돈다 — 다른 실행에 딸려 돌지 않는다
    assert compare["needs"] == "live" and "always()" in compare["if"]   # live 가 실패해도 비교를 따로 돌릴 수 있다


def test_live_eval_compare_job_does_not_run_without_a_key():
    steps = _steps_text(load("live-eval.yml")["jobs"]["compare"])
    assert "OPENAI_API_KEY" in steps and "exit 1" in steps          # 키가 없으면 DEMO 로 돌아가 '실제 결과' 처럼 저장되지 않게 멈춘다


def test_render_check_can_compare_the_deployed_build_with_a_commit():
    inputs = triggers(load("render-check.yml"))["workflow_dispatch"]["inputs"]
    assert "build" in inputs and "expect" in inputs and inputs["expect"]["default"] == "광고 검사"


def test_ci_runs_both_condition_regression_sets_and_python_311():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "--dataset conditions --no-llm" in text and "--dataset conditions_extra --no-llm" in text
    assert '"3.11"' in text or "'3.11'" in text or "3.11" in text
