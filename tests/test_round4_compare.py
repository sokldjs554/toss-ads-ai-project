"""방식 비교 4차 점검 — 돈을 쓴 행을 잃지 않는다: 중단 신호, 잘못된 --systems·--datasets, 리포트 정리 실패, 응답을 못 받은 요청이 있을 때의 누적 비용 표시.

가짜 제공자(tests/fakes.py)로만 돌린다: 네트워크도 실제 모델도 쓰지 않는다. 신호 테스트는 이 프로세스(os.kill)와 별도 프로세스(subprocess)에서 진짜 SIGINT·SIGTERM 을 보낸다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

from core.config import Settings
from evals import compare
from llm.base import LLMResponse
from llm.errors import ProviderError
from tests.fakes import FakeProvider
from tests.test_round3_compare import APPROVE, LIVE, REVISE_WITH_REWRITE, _render, _text, env, files, read, router_of  # noqa: F401  (env: 픽스처)

ROOT = Path(__file__).resolve().parents[1]
INC, LIVE_JSON = compare.INCOMPLETE_NAME, compare.LIVE_NAME
GOLDEN = [c.id for c in compare.load_dataset("golden")]                                              # 사례 id 는 연번이 아니다
PARTIAL = Settings.from_env(openai_api_key="sk-fake-openai", anthropic_api_key="", force_demo=False)       # OpenAI 키만 → PARTIAL, 기준 제공자 openai


def provider(name="openai", *, cost=0.001, fail=None, exc=None, response=APPROVE, estimated=False):
    """통제된 심사 응답을 내는 가짜 제공자. fail(n, req) 가 True 면 그 호출에서 exc(n, req)(없으면 429 ProviderError)를 던진다. estimated: 사용량을 못 받아 비용을 추정한 응답."""
    state = {"n": 0}

    def behave(req):
        state["n"] += 1
        if fail is not None and fail(state["n"], req):
            raise (exc(state["n"], req) if exc else ProviderError("429 quota exceeded", provider=name))
        r = response(req) if callable(response) else response
        return LLMResponse(text="{}", parsed=req.schema.model_validate(r), provider=name, model="controlled", input_tokens=10, output_tokens=5, latency_ms=1.0, cost_usd=cost,
                           finish_reason="stop", cost_estimated=estimated)

    return FakeProvider(name, model="controlled", behavior=behave)


def spent_matches_rows(rep: dict) -> bool:
    """누적 비용 = 행들이 쓴 비용 + 끊긴 시점에 처리 중이던 사례가 쓴 비용 — 쓴 돈이 기록에서 빠지지 않았다."""
    flight = (rep.get("aborted") or {}).get("in_flight") or {}
    return round(sum(r["cost_usd"] for r in rep["rows"]) + flight.get("cost_usd", 0.0), 6) == rep["cost"]["spent_usd"]


def kbd(n, req):
    return KeyboardInterrupt()


def run_main(argv):
    """main 을 돌려 (종료 코드 또는 끝낸 예외 이름) 을 돌려준다."""
    try:
        return compare.main(argv)
    except BaseException as e:  # noqa: BLE001
        return type(e).__name__


# ───────────── (0) 중단 신호: 측정한 행을 잃지 않는다 ─────────────
def test_keyboard_interrupt_in_the_loop_keeps_the_rows_measured_so_far_as_an_incomplete_report():
    """진행 표시 쪽에서 Ctrl-C: 예전에는 KeyboardInterrupt 가 그대로 올라가 리포트를 만들지 못했다 (돈을 쓴 행 전부 소실)."""
    seen = {"n": 0}

    def cb(ds, system, i, n):
        seen["n"] += 1
        if seen["n"] == 7:
            raise KeyboardInterrupt()

    op = provider()
    rep = compare.run_compare(["golden"], LIVE, router=router_of(op), progress=cb, approved_scope=True)
    assert len(rep["rows"]) == 7 and op.calls == 4                                                  # 코드 검사 3행 + 모델 4행까지 마쳤다
    assert rep["aborted"]["kind"] == "interrupted" and rep["aborted"]["exception"] == "KeyboardInterrupt" and rep["aborted"]["after_case"] == f"golden:{GOLDEN[2]}"
    assert rep["run_kind"] == "live_incomplete" and rep["complete"] is False and "interrupted" in rep["incomplete_reason"]
    assert compare.compare_allowed(rep)[0] is False and "중단" in rep["incomplete_note"] and "세 방식 비교의 결과가 아닙니다" in rep["incomplete_note"]
    assert rep["cost"]["spent_usd"] == 0.004 and spent_matches_rows(rep) and rep["cost"]["lower_bound"] is True       # 끊긴 실행의 비용은 ‘이상’


def test_without_an_interrupt_the_same_run_has_no_interrupted_mark():
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider()), approved_scope=True)
    assert rep["aborted"] is None and "interrupted" not in rep["incomplete_reason"] and rep["cost"]["lower_bound"] is False and rep["complete"] is False   # 완료가 아닌 것은 범위(partial_scope) 때문
    assert rep["incomplete_reason"] == ["partial_scope"]


class _Odd(BaseException):
    pass


@pytest.mark.parametrize("raised,name,code", [(SystemExit(3), "SystemExit", 3), (SystemExit(0), "SystemExit", 1), (_Odd("x"), "_Odd", None)])
def test_any_base_exception_in_the_paid_loop_is_recorded_not_swallowed_silently(raised, name, code):
    seen = {"n": 0}

    def cb(ds, system, i, n):
        seen["n"] += 1
        if seen["n"] == 5:
            raise raised

    rep = compare.run_compare(["golden"], LIVE, router=router_of(provider()), progress=cb, approved_scope=True)
    ab = rep["aborted"]
    assert ab["kind"] == "interrupted" and ab["exception"] == name and len(rep["rows"]) == 5 and ab.get("exit_code") == code
    assert rep["run_kind"] == "live_incomplete"


def test_main_saves_first_then_ends_interrupted_when_a_provider_call_is_interrupted(env, capsys):
    """KeyboardInterrupt(Ctrl-C·취소된 Actions 실행의 SIGINT)가 40번째 모델 호출 중에 온 경우: compare_live_incomplete.json 에 측정한 행을 남기고,
    compare_live.json 은 건드리지 않고, 프로세스는 중단된 채로 끝난다 (KeyboardInterrupt 가 다시 올라간다)."""
    op = provider(cost=0.014, fail=lambda n, req: n == 40, exc=kbd)
    r = env(router_of(op))
    (env.reports / LIVE_JSON).write_text("SENTINEL", encoding="utf-8")
    with pytest.raises(KeyboardInterrupt):
        compare.main([])
    assert files(env.reports) == [LIVE_JSON, INC] and (env.reports / LIVE_JSON).read_text(encoding="utf-8") == "SENTINEL"      # 임시 파일이 남지 않는다
    rep = read(env.reports, INC)
    assert rep["run_kind"] == "live_incomplete" and rep["complete"] is False and rep["incomplete_reason"] == ["interrupted"]
    assert rep["aborted"]["kind"] == "interrupted" and rep["aborted"]["exception"] == "KeyboardInterrupt" and rep["aborted"]["after_case"] == f"golden:{GOLDEN[19]}"
    assert len(rep["rows"]) == 20 + 39 and not any(x["error"] for x in rep["rows"])                    # 코드 검사 20행 + 끝낸 모델 호출 39건: 한 행도 잃지 않았다
    assert rep["aborted"]["in_flight"]["system"] == "combined" and rep["aborted"]["in_flight"]["unanswered_requests"] == 1
    assert spent_matches_rows(rep) and r.budget.spent_usd == pytest.approx(rep["cost"]["spent_usd"]) and rep["cost"]["lower_bound"] is True
    assert rep["case_counts"]["golden"] == {"total": 55, "completed": 19, "failed_rows": 0}            # 끊긴 사례(G-020)는 마친 사례로 세지 않는다
    out = capsys.readouterr().out
    assert f"evals/reports/{INC}" in out and "중단된 채로 끝낸다" in out and "이상" in out


def test_interrupt_in_the_middle_of_the_combined_systems_multi_call_case(env):
    """합친 방식은 한 사례에서 호출을 여러 번 한다(검토 → 수정안 의미 재검사). 두 번째 호출 중에 끊겨도 첫 호출에 쓴 돈은 기록에 남는다."""
    op = provider(cost=0.004, response=REVISE_WITH_REWRITE, fail=lambda n, req: req.label.endswith(":rewrite"), exc=kbd)
    r = env(router_of(op))
    assert run_main([]) == "KeyboardInterrupt"
    rep = read(env.reports, INC)
    flight = rep["aborted"]["in_flight"]
    assert flight["system"] == "combined" and flight["calls"] == 1 and flight["attempts"] == 2 and flight["unanswered_requests"] == 1 and flight["cost_usd"] == 0.004
    assert [x["system"] for x in rep["rows"]] == ["code_only", "llm_only"]                              # 첫 사례의 코드 검사·검토 모델만 행만 끝났고, 끊긴 합친 방식은 행으로 세지 않는다
    assert spent_matches_rows(rep) and r.budget.spent_usd == pytest.approx(0.008) and rep["cost"]["lower_bound"] is True
    assert rep["case_counts"]["golden"]["completed"] == 0                                              # 합친 방식까지 끝낸 사례가 없다


def test_main_survives_a_sigterm_and_ends_with_exit_code_143(env):
    """취소된 Actions 실행은 SIGINT 뒤에 SIGTERM 도 보낸다. SIGTERM 도 저장한 뒤 종료 코드 143 으로 끝나고, 처리기는 원래대로 돌아온다."""
    op = provider(fail=lambda n, req: n == 5, exc=lambda n, req: (os.kill(os.getpid(), signal.SIGTERM), ProviderError("x"))[1])
    env(router_of(op))
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as ei:
        compare.main([])
    assert ei.value.code == 143 and signal.getsignal(signal.SIGTERM) == before
    rep = read(env.reports, INC)
    assert rep["aborted"]["kind"] == "interrupted" and rep["aborted"]["exception"] == "TerminatedBySignal" and rep["aborted"]["exit_code"] == 143
    assert files(env.reports) == [INC] and len([x for x in rep["rows"] if x["system"] != "code_only"]) == 4


def test_a_real_sigint_during_the_run_is_handled_the_same_way(env):
    op = provider(fail=lambda n, req: n == 6, exc=lambda n, req: (os.kill(os.getpid(), signal.SIGINT), ProviderError("x"))[1])
    env(router_of(op))
    with pytest.raises(KeyboardInterrupt):
        compare.main([])
    assert files(env.reports) == [INC] and read(env.reports, INC)["aborted"]["exception"] == "KeyboardInterrupt"


def test_a_second_interrupt_while_saving_is_held_until_the_rows_are_on_disk(env, monkeypatch):
    """저장하는 도중 두 번째 Ctrl-C 가 와도 새 파일은 끝까지 쓰이고 옛 파일은 history/ 에 남는다. 신호는 저장이 끝난 뒤에 한 번만 올라간다."""
    env(router_of(provider(fail=lambda n, req: n == 10, exc=kbd)))
    (env.reports / INC).write_text(json.dumps({"run_kind": "live_incomplete", "generated_at": "2026-10-01T09:30:00+00:00", "marker": "older"}), encoding="utf-8")
    real, hits = compare._keep_previous, []

    def keep_then_interrupt(*a, **k):
        hits.append(1)
        os.kill(os.getpid(), signal.SIGINT)                 # 저장 한가운데에 도착한 두 번째 신호
        return real(*a, **k)

    monkeypatch.setattr(compare, "_keep_previous", keep_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        compare.main([])
    assert hits == [1]
    rep = read(env.reports, INC)
    assert rep["aborted"]["kind"] == "interrupted" and len(rep["rows"]) > 0 and "marker" not in rep                          # 새 결과가 온전히 쓰였다
    kept = list((env.reports / "history").glob("compare_live_incomplete_*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text(encoding="utf-8"))["marker"] == "older"
    assert files(env.reports) == [INC]


def test_persist_never_corrupts_an_existing_paid_file_when_the_write_is_interrupted_or_fails(env, monkeypatch):
    """쓰는 도중 끊기거나 실패해도 같은 이름의 옛 파일은 그대로고 임시 파일이 남지 않는다 (예전에는 같은 파일을 바로 열어 쓰다 끊기면 반쯤 쓴 파일이 남았다)."""
    old = json.dumps({"run_kind": "live", "marker": "paid-result", "rows": list(range(500))}, ensure_ascii=False)
    (env.reports / LIVE_JSON).write_text(old, encoding="utf-8")
    rep = {"run_kind": "live", "rows": [{"x": "한글"}] * 50}

    def untouched():
        return (env.reports / LIVE_JSON).read_text(encoding="utf-8") == old and not [f for f in files(env.reports) if f.endswith(".tmp")]

    for boom in (KeyboardInterrupt(), OSError("disk full")):
        with monkeypatch.context() as m:
            m.setattr(compare.os, "replace", lambda *a, **k: (_ for _ in ()).throw(boom))
            with pytest.raises(type(boom)):
                compare.persist(rep, LIVE_JSON)
        assert untouched()
    with monkeypatch.context() as m:
        m.setattr(compare.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("io error")))
        with pytest.raises(OSError):
            compare.persist(rep, LIVE_JSON)
    assert untouched()
    path = compare.persist(rep, LIVE_JSON)                                                         # 대조: 평소에는 정상 저장 (UTF-8 한글 그대로)
    assert json.loads(path.read_text(encoding="utf-8")) == rep and "한글" in path.read_text(encoding="utf-8") and files(env.reports) == [LIVE_JSON]
    kept = list((env.reports / "history").glob("compare_live_*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == old                            # 덮어쓰기 전 옛 결과는 history/ 에


def test_a_failed_save_still_leaves_the_paid_rows_somewhere(env, monkeypatch, tmp_path, capsys):
    """reports 폴더에 쓸 수 없어도(디스크·권한) 돈을 쓴 행은 임시 폴더에 남고 종료 코드는 2."""
    env(router_of(provider()))
    spare = tmp_path / "spare"
    spare.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(spare))
    monkeypatch.setattr(compare, "save_report", lambda rep, name="x.json": (_ for _ in ()).throw(OSError("read-only file system")))
    assert compare.main([]) == 2
    kept = json.loads((spare / LIVE_JSON).read_text(encoding="utf-8"))
    assert kept["complete"] is True and len(kept["rows"]) == 330 and files(env.reports) == []
    out = capsys.readouterr().out
    assert "저장 실패" in out and str(spare) in out and "saved evals/reports" not in out


# ───────────── 진짜 신호를 받는 별도 프로세스 ─────────────
CHILD = r'''
import os, signal, sys, time
sys.path.insert(0, {root!r})
sys.dont_write_bytecode = True
signal.signal(signal.SIGINT, signal.default_int_handler)      # 부모가 SIGINT 를 무시한 채로 띄웠더라도 평소 파이썬처럼
os.environ.pop("OpenAI_API_KEY", None)
os.environ.update(OPENAI_API_KEY="sk-fake-openai", ANTHROPIC_API_KEY="", PREFLIGHT_FORCE_DEMO="0")
from pathlib import Path
from core.config import Settings
from evals import compare
import evals.runner as runner
from llm.base import LLMResponse
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider

runner.REPORTS = Path({reports!r})
APPROVE = {{"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "ok", "rewrite": None}}
state = {{"n": 0}}


def behave(req):
    state["n"] += 1
    print("CALL", state["n"], flush=True)
    time.sleep(0.03)
    return LLMResponse(text="{{}}", parsed=req.schema.model_validate(APPROVE), provider="openai", model="controlled", input_tokens=10, output_tokens=5, latency_ms=1.0, cost_usd=0.01, finish_reason="stop")


settings = Settings.from_env()
router = Router(settings, providers={{"openai": FakeProvider("openai", model="controlled", behavior=behave), "anthropic": FakeProvider("anthropic", available=False)}}, tracer=Tracer())
router.new_run()
compare.Router = lambda *a, **k: router
sys.exit(compare.main([]))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 신호")
@pytest.mark.parametrize("sig,returncode", [(signal.SIGINT, -signal.SIGINT), (signal.SIGTERM, 128 + signal.SIGTERM)])
def test_a_real_signal_to_the_running_cli_saves_the_rows_and_ends_interrupted(tmp_path, sig, returncode):
    """취소된 Actions 실행이 보내는 SIGINT(그리고 뒤따르는 SIGTERM)를 진짜 프로세스에 보낸다: 측정한 행이 compare_live_incomplete.json 에 남고, 프로세스는 중단된 채로 끝난다."""
    reports = tmp_path / "reports"
    reports.mkdir()
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=str(ROOT), reports=str(reports)), encoding="utf-8")
    env_ = {k: v for k, v in os.environ.items() if k.upper() not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}
    proc = subprocess.Popen([sys.executable, str(script)], cwd=str(ROOT), env=env_, stdout=subprocess.PIPE, stderr=(tmp_path / "err.txt").open("w"), text=True)
    watchdog = threading.Timer(180, proc.kill)
    watchdog.start()
    try:
        for line in proc.stdout:
            if line.startswith("CALL 12"):
                break
        proc.send_signal(sig)
        rest = proc.stdout.read()
        assert proc.wait(timeout=120) == returncode, (proc.returncode, (tmp_path / "err.txt").read_text(encoding="utf-8")[-800:])
    finally:
        watchdog.cancel()
        proc.kill()
    assert files(reports) == [INC]                                                                  # 임시 파일도 남지 않는다
    rep = json.loads((reports / INC).read_text(encoding="utf-8"))
    assert rep["run_kind"] == "live_incomplete" and rep["incomplete_reason"] == ["interrupted"] and rep["complete"] is False
    assert rep["aborted"]["exception"] == ("KeyboardInterrupt" if sig == signal.SIGINT else "TerminatedBySignal")
    model_rows = [r for r in rep["rows"] if r["system"] != "code_only"]
    assert len(model_rows) >= 11 and all(not r["error"] for r in rep["rows"])                      # 12번째 호출 중에 신호를 보냈다 — 그 앞의 11건은 모두 남았다
    assert spent_matches_rows(rep) and "중단된 채로 끝낸다" in rest


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="POSIX 셸 필요")
def test_a_cancel_signal_sent_only_to_the_step_shell_still_reaches_python(tmp_path):
    """워크플로의 실제 실행 줄을 셸 스크립트로 돌리되 evals.compare 호출만 가짜 제공자 프로세스로 바꿔 끼우고, SIGINT 를 파이썬이 아니라 셸에만 보낸다.
    exec 가 없으면 셸은 자식이 끝날 때까지 신호를 미뤄 취소가 파이썬에 닿지 않는다(실행이 끝까지 돌아 compare_live.json 이 만들어진다)."""
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "live-eval.yml").read_text(encoding="utf-8"))
    run = next(st for st in wf["jobs"]["compare"]["steps"] if st.get("id") == "run_compare")["run"]
    reports = tmp_path / "reports"
    reports.mkdir()
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(root=str(ROOT), reports=str(reports)), encoding="utf-8")
    (tmp_path / "step.sh").write_text("echo start\n" + re.sub(r"python -m evals\.compare .*", lambda m: f"{sys.executable} {child}", run) + "\n", encoding="utf-8")
    env_ = {k: v for k, v in os.environ.items() if k.upper() not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}
    proc = subprocess.Popen(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(tmp_path / "step.sh")], cwd=str(ROOT), env=env_, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    watchdog = threading.Timer(180, proc.kill)
    watchdog.start()
    try:
        for line in proc.stdout:
            if line.startswith("CALL 12"):
                break
        os.kill(proc.pid, signal.SIGINT)                    # 셸에만
        proc.stdout.read()
        code = proc.wait(timeout=120)
    finally:
        watchdog.cancel()
        proc.kill()
    assert code == -signal.SIGINT and files(reports) == [INC]
    assert json.loads((reports / INC).read_text(encoding="utf-8"))["aborted"]["kind"] == "interrupted"


# ───────────── (1) --systems · --datasets 는 호출 전에 검사한다 ─────────────
@pytest.mark.parametrize("systems,needle", [
    ("code_only,llm_only,combine", "알 수 없는 이름: combine"),         # 오타: 예전에는 합친 방식으로 흘러 8번 호출한 뒤 KeyError 로 죽고 아무것도 저장하지 않았다
    ("Code_Only,LLM_ONLY,Combined", "알 수 없는 이름: Code_Only, LLM_ONLY, Combined"),
    ("bogus", "알 수 없는 이름: bogus"),
    ("code_only,llm_only,combined,combined", "같은 이름이 두 번 이상 있다: combined"),
    ("code_only,,combined", "빈 이름이 있다"),
    ("code_only,llm_only,combined,", "빈 이름이 있다"),
    ("", "--systems 가 비어 있다"),
])
def test_bad_systems_are_refused_before_any_call(env, capsys, systems, needle):
    op = provider()
    env(router_of(op))
    assert compare.main(["--datasets", "contrast_g069", "--systems", systems]) == 2
    out = capsys.readouterr().out
    assert op.calls == 0 and files(env.reports) == [] and out.startswith("실행하지 않음 — ") and needle in out
    assert ("가능한 값: code_only, llm_only, combined" in out) is needle.startswith("알 수 없는")


@pytest.mark.parametrize("datasets,needle", [
    ("golden,golden", "같은 이름이 두 번 이상 있다: golden"),          # 예전에는 두 배로 호출하고도 완료된 결과로 저장될 수 있었다
    ("golden,holdout,contrast_g069,conditions,conditions", "같은 이름이 두 번 이상 있다: conditions"),
    ("golden,", "빈 이름이 있다"),
    ("", "--datasets 가 비어 있다"),
    ("Golden", "승인되지 않은 데이터"),
    ("golden,conditions_extra", "conditions_extra"),
])
def test_bad_datasets_are_refused_before_any_call(env, capsys, datasets, needle):
    op = provider()
    env(router_of(op))
    assert compare.main(["--datasets", datasets]) == 2
    assert op.calls == 0 and files(env.reports) == [] and needle in capsys.readouterr().out


def test_names_are_trimmed_and_exact_names_run_normally(env, capsys):
    """대조: 이름 앞뒤 공백만 걷어 내고 정확한 이름은 평소처럼 돈다."""
    op = provider()
    env(router_of(op))
    assert compare.main(["--datasets", " contrast_g069 , golden ", "--systems", " code_only , combined ", "--limit", "2"]) == 2        # 일부 범위라 완료된 결과가 아님
    rep = read(env.reports, INC)
    assert rep["scope"]["datasets"] == ["contrast_g069", "golden"] and rep["scope"]["systems"] == ["code_only", "combined"] and op.calls == 4 and len(rep["rows"]) == 8
    assert rep["incomplete_reason"] == ["partial_scope"]


def test_estimate_and_demo_runs_refuse_unknown_names_too(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("evals.compare.Router", lambda *a, **k: (_ for _ in ()).throw(AssertionError("호출하지 않는다")))
    assert compare.main(["--estimate", "--systems", "combine"]) == 2 and "알 수 없는 이름: combine" in capsys.readouterr().out
    assert compare.main(["--estimate", "--datasets", "golden,golden"]) == 2 and "두 번 이상" in capsys.readouterr().out
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    monkeypatch.setattr("evals.runner.REPORTS", tmp_path)
    assert compare.main(["--datasets", "no_such_dataset"]) == 2 and "알 수 없는 이름: no_such_dataset" in capsys.readouterr().out       # 대역 실행: 데이터 파일이 있는 이름만
    assert files(tmp_path) == []
    assert compare.main(["--estimate", "--datasets", "golden"]) == 0                                # 대조: 정상 입력은 그대로


def test_run_compare_itself_refuses_wrong_names_before_any_call():
    op = provider()
    r = router_of(op)
    for bad in (("code_only", "llm_only", "combine"), ("combined", "combined"), (), "combined"):
        with pytest.raises(ValueError):
            compare.run_compare(["contrast_g069"], LIVE, router=r, systems=bad)
    with pytest.raises(ValueError, match="두 번 이상"):
        compare.run_compare(["golden", "golden"], LIVE, router=r)
    with pytest.raises(ValueError, match="비어"):
        compare.run_compare([], LIVE, router=r)
    assert op.calls == 0
    row_case = compare.load_dataset("contrast_g069")[0]
    with pytest.raises(ValueError, match="알 수 없는 방식"):                                         # 방식마다 갈래가 하나씩이다: 모르는 이름이 합친 방식으로 흘러가지 않는다
        compare._run_one("combine", row_case, LIVE, r, compare.get_catalog(), 0.0)
    assert op.calls == 0
    with pytest.raises(ValueError, match="알 수 없는 방식"):
        compare.build_report([], {"contrast_g069": {"n": 4, "split": "dev"}}, LIVE, compare.get_catalog(), ("combine",), "live")


def test_a_failure_while_building_the_report_still_keeps_every_measured_row(env, monkeypatch, capsys):
    """리포트를 정리하는 단계(지표 계산 등)에서 오류가 나도 돈을 쓴 행은 최소 리포트로 남고 완료된 결과로 저장되지 않는다. 예전에는 KeyError 등이 그대로 올라가 아무것도 저장하지 않았다."""
    op = provider()
    env(router_of(op))
    monkeypatch.setattr(compare, "summarize_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("지표 계산 오류")))
    assert compare.main([]) == 2
    assert files(env.reports) == [INC]
    rep = read(env.reports, INC)
    assert len(rep["rows"]) == 330 and op.calls == 220 and rep["complete"] is False and rep["run_kind"] == "live_incomplete"
    assert rep["incomplete_reason"] == ["report_error"] and rep["report_error"].startswith("RuntimeError: 지표 계산 오류") and "원본만" in rep["incomplete_note"]
    assert rep["cost"]["spent_usd"] == pytest.approx(0.22) and spent_matches_rows(rep) and set(rep["systems"]) == set(compare.SYSTEMS)
    assert "saved" not in capsys.readouterr().out


def test_even_if_the_minimal_report_fails_the_raw_rows_are_still_returned_and_saved(env, monkeypatch):
    env(router_of(provider()))
    monkeypatch.setattr(compare, "summarize_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("1")))
    monkeypatch.setattr(compare, "fallback_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("2")))
    assert compare.main([]) == 2
    rep = read(env.reports, INC)
    assert files(env.reports) == [INC] and len(rep["rows"]) == 330 and rep["run_kind"] == "live_incomplete" and rep["complete"] is False
    assert rep["incomplete_reason"] == ["report_error"] and rep["cost"]["spent_usd"] == pytest.approx(0.22) and rep["cost"]["lower_bound"] is True


def test_a_failing_side_section_does_not_even_degrade_the_report(monkeypatch):
    """대표 사례·라벨 변경 이력·G-069 분리는 곁가지다: 만들다 실패해도 지표와 행은 정상이다."""
    for name in ("pick_examples", "label_changes", "separate_g069_effects"):
        monkeypatch.setattr(compare, name, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider()), approved_scope=True)
    assert rep["examples"] == {} and rep["label_changes"] == [] and rep["g069_effects"] is None and "report_error" not in rep and len(rep["rows"]) == 12
    assert rep["overall_by_split"]["dev"]["combined"]["n"] == 4


def test_a_harness_error_row_keeps_what_it_spent(monkeypatch):
    """하네스 쪽 오류로 실패한 행도 그 행이 이미 쓴 비용·요청을 적는다 (예전에는 0 으로 적혀 누적 비용과 행 합계가 어긋났다)."""
    real = compare.run_combined

    def late_failure(case, settings, router, *a, **k):
        real(case, settings, router, *a, **k)                                                      # 호출은 끝났는데
        raise RuntimeError("행을 정리하다 난 오류")

    monkeypatch.setattr(compare, "run_combined", late_failure)
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.01)), approved_scope=True)
    bad = [r for r in rep["rows"] if r["error"]]
    assert len(bad) == 4 and all(r["cost_usd"] == 0.01 and r["calls"] == 1 for r in bad) and spent_matches_rows(rep)


# ───────────── (14) 누적 비용: 응답을 못 받은 요청이 있으면 ‘이상’ ─────────────
def failed_run(**kw):
    """모든 요청이 실패해 연속 실패로 멈춘 실행 (검토 지적의 재현)."""
    return compare.run_compare(["golden"], LIVE, router=router_of(provider(fail=lambda n, req: True)), systems=("llm_only",), limit=4, cost_cap_usd=1.0, run_kind="live", abort_after=3, **kw)


def captions(at):
    return " ".join(c.value for c in at.caption)


def test_cost_block_is_a_lower_bound_when_requests_failed_and_keeps_the_old_keys():
    rep = failed_run()
    c = rep["cost"]
    assert rep["run_kind"] == "live_incomplete" and rep["usage"]["llm_only"]["failed_requests"] == 3
    assert c["spent_usd"] == 0.0 and c["cap_usd"] == 1.0 and c["over_cap"] is False                  # 예전 키는 그대로
    assert c["lower_bound"] is True and c["failed_requests"] == 3 and c["estimated_calls"] == 0
    assert compare.report_cost_is_lower_bound(rep) is True and compare.format_cost(c["spent_usd"], True) == "$0.00 이상"


def test_cost_block_is_exact_when_every_request_was_answered():
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.0625)), cost_cap_usd=0.25, approved_scope=True)
    assert rep["aborted"]["kind"] == "cost_cap" and rep["cost"]["lower_bound"] is False and rep["cost"]["failed_requests"] == 0
    assert compare.report_cost_is_lower_bound(rep) is False and compare.format_cost(0.25, False) == "$0.25"
    assert rep["aborted"]["reason"] == "누적 비용 $0.25 가 상한 $0.25 에 닿음"


def test_a_cost_estimated_from_text_length_is_also_a_lower_bound():
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(estimated=True)), approved_scope=True)
    assert rep["cost"]["estimated_calls"] == 8 and rep["cost"]["lower_bound"] is True and rep["results"]["contrast_g069"]["llm_only"]["cost_is_lower_bound"] is True


def test_cap_reached_after_failed_requests_does_not_state_the_cost_as_exact():
    op = provider(cost=0.5, fail=lambda n, req: n == 1)
    rep = compare.run_compare(["golden"], LIVE, router=router_of(op), systems=("llm_only",), cost_cap_usd=1.0, approved_scope=True)
    assert rep["aborted"]["kind"] == "cost_cap" and rep["cost"]["lower_bound"] is True and "이상" in rep["aborted"]["reason"]


def test_view_prints_an_inexact_spent_cost_with_a_lower_bound_mark(monkeypatch):
    """모든 요청이 실패한 실행의 ‘완료되지 않은 실행’ 칸: 예전에는 ‘쓴 비용 $0.00’ 이라고 정확한 값처럼 적었다."""
    at = _render(monkeypatch, {compare.INCOMPLETE_NAME: failed_run()})
    cap = captions(at)
    assert "쓴 비용 $0.00 이상" in cap and "응답을 받지 못한 요청의 비용은 알 수 없어" in cap and "상한 $1.00" in cap
    assert "쓴 비용 $0.00 ·" not in cap


def test_view_keeps_an_exact_cost_exact_when_nothing_failed(monkeypatch):
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.0625)), cost_cap_usd=0.25, approved_scope=True)
    cap = captions(_render(monkeypatch, {compare.INCOMPLETE_NAME: rep}))
    assert "쓴 비용 $0.25 · 상한 $0.25" in cap and "이상" not in cap.split("쓴 비용")[1].split("모델")[0]


def test_view_marks_the_total_when_only_some_requests_failed(monkeypatch):
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.01, fail=lambda n, req: n == 3)), approved_scope=True)
    assert rep["cost"]["failed_requests"] >= 1 and rep["cost"]["lower_bound"] is True
    cap = captions(_render(monkeypatch, {compare.INCOMPLETE_NAME: rep}))
    assert f"쓴 비용 ${rep['cost']['spent_usd']:.2f} 이상" in cap


def test_reports_stored_before_the_flag_existed_still_render_and_read_the_failures(monkeypatch):
    """lower_bound 키가 없는 옛 리포트: 방식별 사용량의 실패 요청에서 읽고, 실패가 없으면 정확한 값으로 둔다."""
    old_bad = failed_run()
    for k in ("lower_bound", "failed_requests", "estimated_calls"):
        old_bad["cost"].pop(k)
    assert compare.report_cost_is_lower_bound(old_bad) is True
    assert "쓴 비용 $0.00 이상" in captions(_render(monkeypatch, {compare.INCOMPLETE_NAME: old_bad}))
    old_ok = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.0625)), cost_cap_usd=0.25, approved_scope=True)
    old_ok["cost"] = {"spent_usd": 0.25, "cap_usd": 0.25, "over_cap": False}
    assert compare.report_cost_is_lower_bound(old_ok) is False
    assert "쓴 비용 $0.25 · 상한" in captions(_render(monkeypatch, {compare.INCOMPLETE_NAME: old_ok}))
    stored = json.loads((ROOT / "evals" / "reports" / "compare_controlled.json").read_text(encoding="utf-8"))          # 저장된 대역 결과도 그대로 읽힌다
    assert "lower_bound" not in stored["cost"] and compare.report_cost_is_lower_bound(stored) is False
    at = _render(monkeypatch, {compare.CONTROLLED_NAME: stored})
    assert any("하네스 검증용" in e.label for e in at.expander)


def test_view_renders_the_minimal_reports_that_keep_only_the_raw_rows(monkeypatch):
    """리포트를 정리하다 실패해 만든 최소 리포트도 ‘완료되지 않은 실행’ 칸에 오류 없이 보인다."""
    with monkeypatch.context() as m:
        m.setattr(compare, "summarize_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("지표 계산 오류")))
        minimal = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.01)), approved_scope=True)
    assert minimal["report_error"] and minimal["overall_by_split"] == {} and len(minimal["rows"]) == 12
    t = _text(_render(monkeypatch, {compare.INCOMPLETE_NAME: minimal}))
    assert "완료되지 않은 실행" in t and "결과를 정리하다 오류가 나서" in t and "쓴 비용 $0.08 · 모델" in t      # 지표는 없어도 쓴 돈은 정확히
    bare = compare._bare_report([], "live", None, 0.0, RuntimeError("x"))
    t2 = _text(_render(monkeypatch, {compare.INCOMPLETE_NAME: bare}))
    assert "완료되지 않은 실행" in t2 and "원본만" in t2 and "쓴 비용 $0.00 이상" in t2      # 행 원본만 남은 리포트의 비용은 ‘이상’


def test_view_and_summary_show_an_interrupted_run_as_a_lower_bound(monkeypatch, capsys):
    rep = compare.run_compare(["golden"], LIVE, router=router_of(provider(cost=0.004, response=REVISE_WITH_REWRITE, fail=lambda n, req: req.label.endswith(":rewrite"), exc=kbd)),
                              approved_scope=True)
    assert rep["aborted"]["kind"] == "interrupted" and rep["cost"]["lower_bound"] is True
    at = _render(monkeypatch, {compare.INCOMPLETE_NAME: rep})
    cap = captions(at)
    assert "쓴 비용 $0.01 이상" in cap and f"끊긴 시점에 처리 중이던 사례 golden:{GOLDEN[0]}" in cap
    assert any("실행이 중단됨" in str(m.value) for m in at.markdown)
    compare.print_summary(rep)
    assert "누적 비용 $0.01 이상" in capsys.readouterr().out


def test_summary_prints_the_total_cost_with_the_mark_only_when_it_is_a_lower_bound(capsys):
    compare.print_summary(failed_run())
    assert "누적 비용 $0.00 이상 · 상한 $1.00" in capsys.readouterr().out
    clean = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider(cost=0.0625)), cost_cap_usd=0.25, approved_scope=True)
    compare.print_summary(clean)
    out = capsys.readouterr().out
    assert "누적 비용 $0.25 · 상한 $0.25" in out and "누적 비용 $0.25 이상" not in out


def test_over_cap_note_also_marks_the_total():
    rep = {"aborted": None, "cost": {"spent_usd": 1.2, "cap_usd": 1.0, "over_cap": True, "lower_bound": True}, "provider": "openai", "case_counts": {}}
    assert "누적 비용이 $1.20 이상으로 상한 $1.00 를 넘었다" in compare._incomplete_note(rep)
    exact = dict(rep, cost={"spent_usd": 1.2, "cap_usd": 1.0, "over_cap": True, "lower_bound": False})
    assert "누적 비용 $1.20 가 상한 $1.00 를 넘었다" in compare._incomplete_note(exact) and "이상" not in compare._incomplete_note(exact)


# ───────────── 다섯 가지 결과: 파일·종료 코드·행 ─────────────
def _scenarios():
    return {
        "complete": dict(op=dict(), argv=[], code=0, names=[LIVE_JSON], kind="live", reasons=[], rows=330, lower=False),
        "cost_cap": dict(op=dict(cost=0.0625), argv=["--cost-cap", "1.0"], code=2, names=[INC], kind="live_incomplete", reasons=["cost_cap_reached"], rows=24, lower=False),
        "failed_rows": dict(op=dict(fail=lambda n, req: "G-005" in req.label), argv=[], code=2, names=[INC], kind="live_incomplete", reasons=["failed_rows"], rows=330, lower=True),
        "interrupt": dict(op=dict(fail=lambda n, req: n == 40, exc=kbd), argv=[], code="KeyboardInterrupt", names=[INC], kind="live_incomplete", reasons=["interrupted"], rows=59, lower=True),
    }


@pytest.mark.parametrize("name", ["complete", "cost_cap", "failed_rows", "interrupt"])
def test_each_outcome_writes_exactly_the_right_file_and_keeps_every_row(env, name):
    sc = _scenarios()[name]
    r = env(router_of(provider(**sc["op"])))
    assert run_main(sc["argv"]) == sc["code"]
    assert files(env.reports) == sc["names"]
    rep = read(env.reports, sc["names"][0])
    assert rep["run_kind"] == sc["kind"] and rep["incomplete_reason"] == sc["reasons"] and rep["complete"] is (not sc["reasons"])
    assert len(rep["rows"]) == sc["rows"] and rep["cost"]["lower_bound"] is sc["lower"] and spent_matches_rows(rep)
    assert r.budget.spent_usd == pytest.approx(rep["cost"]["spent_usd"])                           # 라우터가 센 돈 = 리포트의 누적 비용 (빠진 행 없음)


def test_mixed_provider_outcome_keeps_every_row_and_the_attribution(env):
    r = env(router_of(provider(fail=lambda n, req: "G-005" in req.label), provider("anthropic")))
    assert compare.main([]) == 2 and files(env.reports) == [INC]
    rep = read(env.reports, INC)
    assert rep["incomplete_reason"] == ["mixed_provider"] and len(rep["rows"]) == 330 and rep["mixed_provider"]["n"] == 2
    assert rep["cost"]["lower_bound"] is True and spent_matches_rows(rep) and r.budget.spent_usd == pytest.approx(rep["cost"]["spent_usd"])       # 실패한 요청이 있어 ‘이상’


def test_a_clean_run_with_only_an_openai_key_is_complete_in_partial_mode(env, monkeypatch):
    """OpenAI 키만 있는 PARTIAL 모드: 기준 제공자는 openai 이고 깨끗하게 끝나면 완료된 결과(compare_live.json)다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    op = provider()
    r = env(router_of(op, settings=PARTIAL))
    assert Settings.from_env().mode.value == "partial" and compare.main([]) == 0
    assert files(env.reports) == [LIVE_JSON]
    rep = read(env.reports, LIVE_JSON)
    assert rep["run_kind"] == "live" and rep["complete"] is True and rep["settings"]["mode"] == "partial" and rep["provider"] == "openai" and rep["served_by"] == {"openai": 220}
    assert len(rep["rows"]) == 330 and rep["cost"]["lower_bound"] is False and spent_matches_rows(rep) and op.calls == 220 and r.budget.spent_usd == pytest.approx(0.22)
