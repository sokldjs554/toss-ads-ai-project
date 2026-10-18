"""GitHub Actions 워크플로 정의 검사 — 실제로 돌려 보지 않고도 '유료 실행의 결과가 저장되는 구조인가' 를 확인한다.

과거에 `live` 작업이 만들지 않는 파일을 `git add` 해 커밋 단계가 실패하면서 유료 호출의 결과가 전부 버려진 적이 있다.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
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
    assert "compare_live" not in add_line and "evals.compare" not in live_text
    # compare 작업은 자기 결과만, 파일이 있을 때만 add 한다. compare_live.json 은 종료 코드 0 인 실행의 것만, 완료되지 못한 실행의 파일은 있으면 add
    assert re.search(r'if \[ "\$RUN_OUTCOME" = "success" \] && \[ -f evals/reports/compare_live\.json \]; then git add evals/reports/compare_live\.json; fi', compare_text)
    assert re.search(r"if \[ -f evals/reports/compare_live_incomplete\.json \]; then git add evals/reports/compare_live_incomplete\.json; fi", compare_text)
    assert "evals.compare" in compare_text and "--cost-cap" in compare_text and "--out compare_live.json" in compare_text


def test_live_eval_compare_is_off_by_default_and_capped_within_the_approved_budget():
    doc = load("live-eval.yml")
    inputs = triggers(doc)["workflow_dispatch"]["inputs"]
    assert str(inputs["compare"]["default"]).lower() == "false"
    assert float(inputs["compare_cost_cap"]["default"]) <= 3.5
    compare = doc["jobs"]["compare"]
    assert "inputs.compare == 'true'" in compare["if"]            # 입력으로 켠 경우에만 돈다 — 다른 실행에 딸려 돌지 않는다
    assert float(re.search(r"\|\| '([\d.]+)'", compare["env"]["COMPARE_COST_CAP"]).group(1)) <= 3.5     # 입력을 비워도 승인된 상한을 넘지 않는다


def _gate(expr: str, compare_input: str) -> bool:
    """`${{ github.event.inputs.compare == 'true' }}` / `!= 'true'` 꼴의 조건만 푼다 (이 워크플로가 쓰는 꼴)."""
    m = re.fullmatch(r"\$\{\{\s*(always\(\)\s*&&\s*)?github\.event\.inputs\.compare\s*(==|!=)\s*'true'\s*\}\}", expr.strip())
    assert m, expr
    return (compare_input == "true") == (m.group(2) == "==")


@pytest.mark.parametrize("compare_input,live_runs,compare_runs", [("true", False, True), ("false", True, False), ("", True, False)])
def test_dispatching_compare_runs_only_the_compare_job(compare_input, live_runs, compare_runs):
    """compare=true 만 켜도 평가·레드팀·트레이스(유료 4단계, 약 $2.4)가 딸려 돌던 결함: 이제 live 작업은 건너뛰고 compare 작업만 돈다."""
    jobs = load("live-eval.yml")["jobs"]
    assert _gate(jobs["live"]["if"], compare_input) is live_runs
    assert _gate(jobs["compare"]["if"], compare_input) is compare_runs
    assert "needs" not in jobs["compare"]                          # live 작업을 기다리지 않는다 (건너뛴 작업에 매이지 않는다)


def test_dispatch_input_descriptions_say_compare_runs_alone():
    inputs = triggers(load("live-eval.yml"))["workflow_dispatch"]["inputs"]
    for name in ("evals", "limit", "redteam", "traces"):
        assert "compare" in inputs[name]["description"] and "무시" in inputs[name]["description"], name
    d = inputs["compare"]["description"]
    assert "방식 비교만" in d and "live 작업" in d and "돌지 않고" in d
    assert "compare_live_incomplete.json" in inputs["compare_cost_cap"]["description"] and "즉시" in inputs["compare_cost_cap"]["description"]


def _compare_command(step_text: str) -> list[str]:
    """step 의 `python -m evals.compare ...` 줄을 셸이 쪼개는 대로 쪼갠다 (환경변수는 워크플로가 주는 값으로 바꿔서)."""
    env = load("live-eval.yml")["jobs"]["compare"]["env"]
    cap = re.search(r"\|\| '([\d.]+)'", env["COMPARE_COST_CAP"]).group(1)
    assert "github.event.inputs.compare_cost_cap" in env["COMPARE_COST_CAP"]          # 비용 상한 입력이 그대로 들어간다
    line = next(l for l in step_text.splitlines() if "python -m evals.compare" in l)
    return shlex.split(line.replace("$COMPARE_COST_CAP", cap).replace('"', "").split("python -m evals.compare", 1)[1].strip())


def test_live_eval_compare_command_line_matches_the_cli_exactly():
    """워크플로의 실행 줄을 evals.compare 의 실제 인자 해석기로 읽어, 데이터·비용 상한·출력 이름이 의도대로 들어가는지 본다."""
    from evals import compare as cmp

    steps = load("live-eval.yml")["jobs"]["compare"]["steps"]
    run = next(s for s in steps if s.get("id") == "run_compare")
    args = cmp.build_parser().parse_args(_compare_command(run["run"]))
    assert [d for d in args.datasets.split(",")] == list(cmp.APPROVED_DATASETS) == ["golden", "holdout", "contrast_g069", "conditions"]
    assert args.cost_cap == 3.5 and args.out == "compare_live.json" and not args.estimate and args.limit is None
    assert args.systems == ",".join(cmp.SYSTEMS)
    est = next(s for s in steps if "--estimate" in str(s.get("run", "")))
    e_args = cmp.build_parser().parse_args(_compare_command(est["run"]))
    assert e_args.estimate and e_args.datasets == args.datasets and e_args.cost_cap == args.cost_cap        # 추정도 같은 데이터·같은 상한으로 낸다


def test_conditions_extra_cannot_enter_the_live_comparison_through_the_workflow_or_defaults():
    from evals import compare as cmp

    text = (WORKFLOWS / "live-eval.yml").read_text(encoding="utf-8")
    assert "conditions_extra" not in text                                                  # 워크플로 어디에도 승인 밖 데이터가 없다
    assert "conditions_extra" not in cmp.build_parser().parse_args([]).datasets             # 기본 데이터 목록에도 없다
    assert "conditions_extra" not in cmp.APPROVED_DATASETS


def test_compare_commit_step_runs_even_after_an_incomplete_run_and_keeps_the_measured_rows():
    """종료 코드 2 로 끝난 실행의 측정 행(compare_live_incomplete.json)을 커밋 단계가 건너뛰지 않는다. 결과 파일 업로드도 항상 한다."""
    steps = load("live-eval.yml")["jobs"]["compare"]["steps"]
    commit = next(s for s in steps if s.get("name", "").startswith("결과 커밋"))
    assert "always()" in commit["if"] and commit["env"]["RUN_OUTCOME"] == "${{ steps.run_compare.outcome }}"
    upload = steps[-1]
    assert upload["if"] == "always()" and "evals/reports/compare_*.json" in upload["with"]["path"]


@pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="git·bash 필요")
@pytest.mark.parametrize("outcome,pre,written,committed", [
    ("failure", {}, {"compare_live_incomplete.json": "{}"}, {"compare_live_incomplete.json"}),                       # 완료되지 못한 실행: 측정 행만 올린다
    ("failure", {"compare_live.json": '{"old": 1}'}, {"compare_live_incomplete.json": "{}", "compare_live.json": '{"stray": 1}'},
     {"compare_live_incomplete.json"}),                                                                              # 실패한 실행 뒤의 compare_live.json 은 어떤 경우에도 올리지 않는다
    ("success", {}, {"compare_live.json": "{}"}, {"compare_live.json"}),                                             # 완료된 실행: compare_live.json
    ("failure", {}, {}, set()),                                                                                      # 아무 파일도 없어도 단계가 실패하지 않는다
    ("success", {}, {}, set()),
])
def test_compare_commit_step_script_commits_the_right_files(tmp_path, outcome, pre, written, committed):
    """워크플로의 커밋 단계 스크립트를 로컬 저장소에서 실제로 돌려 본다 (원격은 로컬 bare 저장소)."""
    step = next(s for s in load("live-eval.yml")["jobs"]["compare"]["steps"] if s.get("name", "").startswith("결과 커밋"))
    (tmp_path / "step.sh").write_text(step["run"].replace("${{ github.run_id }}", "1"), encoding="utf-8")
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    env = {**os.environ, "RUN_OUTCOME": outcome, "GIT_TERMINAL_PROMPT": "0", "HOME": str(tmp_path)}

    def sh(cmd, cwd):
        return subprocess.run(cmd, cwd=cwd, env=env, shell=True, capture_output=True, text=True)

    assert sh(f"git init -q --bare -b main {remote} && git clone -q {remote} {work}", tmp_path).returncode == 0
    reports = work / "evals" / "reports"
    reports.mkdir(parents=True)
    (work / "README.md").write_text("x", encoding="utf-8")
    for k, v in pre.items():
        (reports / k).write_text(v, encoding="utf-8")
    prep = sh("git config user.name t && git config user.email t@t && git checkout -q -B main && git add -A && git commit -q -m init && git push -q origin main", work)
    assert prep.returncode == 0, prep.stderr
    head_before = sh("git rev-parse HEAD", work).stdout.strip()
    for k, v in written.items():
        (reports / k).write_text(v, encoding="utf-8")
    r = sh(f"bash -e {tmp_path / 'step.sh'}", work)
    assert r.returncode == 0, r.stderr
    head_after = sh("git rev-parse HEAD", work).stdout.strip()
    names = {Path(c).name for c in sh("git show --name-only --format= HEAD", work).stdout.split()} if head_after != head_before else set()
    assert names == committed, (names, r.stdout, r.stderr)
    if committed:
        assert sh("git rev-parse origin/main", work).stdout.strip() == head_after          # 원격까지 올라갔다


def test_live_eval_compare_job_does_not_run_without_a_key():
    steps = _steps_text(load("live-eval.yml")["jobs"]["compare"])
    assert "OPENAI_API_KEY" in steps and "exit 1" in steps          # 키가 없으면 DEMO 로 돌아가 '실제 결과' 처럼 저장되지 않게 멈춘다


def test_live_eval_compare_job_stops_when_only_the_other_providers_key_exists():
    """승인받은 비교는 OpenAI 모델로 재는 것이다 — Anthropic 키만 있어도 도는 키 확인이면 다른 모델을 재게 된다."""
    check = next(st for st in load("live-eval.yml")["jobs"]["compare"]["steps"] if "키 확인" in st.get("name", ""))["run"]
    assert 'if [ -z "$OPENAI_API_KEY" ]' in check and "ANTHROPIC_API_KEY\" ]" not in check.split("exit 1")[0]


def test_render_check_can_compare_the_deployed_build_with_a_commit():
    inputs = triggers(load("render-check.yml"))["workflow_dispatch"]["inputs"]
    assert "build" in inputs and "expect" in inputs and inputs["expect"]["default"] == "광고 검사"


def test_ci_runs_both_condition_regression_sets_and_python_311():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "--dataset conditions --no-llm" in text and "--dataset conditions_extra --no-llm" in text
    assert '"3.11"' in text or "'3.11'" in text or "3.11" in text
