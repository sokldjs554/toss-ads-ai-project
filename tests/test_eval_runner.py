"""평가 실행기가 제공자 장애를 '측정' 으로 둔갑시키지 않는지 (5차 실제 모델 실행에서 재현된 문제).

실제 사건: 골든셋 55건 중 G-047 부터 검토 모델 호출이 실패했고, 서킷이 열린 뒤 나머지 사례가 1~2ms 만에 '담당자 확인' 으로
떨어졌는데도 그 결과가 기준선으로 저장됐다. 실패 이유는 어디에도 남지 않았다.
"""
from __future__ import annotations

import pytest

from core.config import Settings
from evals import run as cli
from evals.runner import EvalAborted, _wait_for_breaker, baseline_allowed, run_eval
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider


def _demo_router(tracer: Tracer) -> tuple[Settings, Router]:
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    return s, Router(s, tracer=tracer)


def test_provider_failure_is_recorded_per_case_and_tallied(tracer):
    s, router = _demo_router(tracer)
    router.simulate_failure("mock", "timeout")
    rep = run_eval("golden", s, use_llm=True, router=router, limit=2, breaker_wait_s=0, abort_after=0)
    # 코드 검사가 게재 불가를 확정한 사례는 모델 없이도 REJECT, 나머지는 담당자 확인 — 어느 쪽이든 실패 이유가 남는다
    for c in rep["cases"]:
        assert c["predicted"] == ("REJECT" if c["inputs"]["det_block"] else "HUMAN_REVIEW"), c
        assert "ProviderUnavailable" in c["error"] and "Timeout" in c["error"], c["error"]
    pf = rep["provider_failures"]
    assert pf["n"] == 2 and pf["cases"] == [c["id"] for c in rep["cases"]] and pf["by_error"] == {"ProviderUnavailable": 2}
    ok, why = baseline_allowed(rep)
    assert not ok and "2건" in why


def test_clean_run_has_no_failures_and_may_be_saved(tracer):
    s, router = _demo_router(tracer)
    rep = run_eval("golden", s, use_llm=True, router=router, limit=3, breaker_wait_s=0, abort_after=0)
    assert rep["provider_failures"] == {"n": 0, "cases": [], "by_error": {}}
    assert all(c["error"] == "" for c in rep["cases"])
    assert baseline_allowed(rep) == (True, "")


def test_transient_failure_is_retried_once_after_waiting(tracer, monkeypatch):
    """첫 시도가 실패하면 서킷이 닫히길 기다렸다가 같은 사례를 한 번 더 — 일시 장애가 결과에 남지 않는다."""
    s, router = _demo_router(tracer)
    router.simulate_failure("mock", "timeout")
    waits: list[float] = []

    def fake_wait(r, max_wait_s, log=None):
        waits.append(max_wait_s)
        r.simulate_failure("mock", None)  # 장애가 걷혔다
        return 0.0

    monkeypatch.setattr("evals.runner._wait_for_breaker", fake_wait)
    seen = []
    rep = run_eval("golden", s, use_llm=True, router=router, limit=1, breaker_wait_s=30, abort_after=3,
                   progress=lambda i, n, r: seen.append(r.case.id))
    assert rep["provider_failures"]["n"] == 0 and rep["cases"][0]["error"] == ""
    assert len(seen) == 1 and waits  # 한 사례를 두 번 시도했지만 결과는 하나


def test_abort_after_consecutive_provider_failures(tracer):
    s, router = _demo_router(tracer)
    router.simulate_failure("mock", "timeout")
    with pytest.raises(EvalAborted) as ei:
        run_eval("golden", s, use_llm=True, router=router, limit=10, breaker_wait_s=0, abort_after=3)
    partial = ei.value.report
    assert partial["aborted"]["consecutive_provider_failures"] == 3 and partial["aborted"]["done"] == 3 and partial["aborted"]["planned"] == 10
    assert partial["provider_failures"]["n"] == 3
    assert "Timeout" in str(ei.value)
    assert baseline_allowed(partial)[0] is False


def test_wait_for_breaker_sleeps_until_cooldown(tracer):
    s = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="")
    router = Router(s, providers={"openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic", available=False)}, tracer=tracer)
    b = router.breakers["openai"]
    b.threshold, b.cooldown_s = 1, 0.3
    b.failure()
    assert b.is_open
    waited = _wait_for_breaker(router, max_wait_s=5.0)
    assert 0.1 <= waited <= 0.3 and not b.is_open
    b.failure()
    assert _wait_for_breaker(router, max_wait_s=0.05) <= 0.05  # 상한을 넘기지 않는다
    assert _wait_for_breaker(None, 5.0) == 0.0


def test_cli_refuses_to_save_degraded_baseline(monkeypatch, tmp_path):
    s, router = _demo_router(Tracer())
    clean = run_eval("golden", s, use_llm=True, router=router, limit=2, breaker_wait_s=0, abort_after=0)
    degraded = dict(clean)
    degraded["provider_failures"] = {"n": 1, "cases": ["G-001"], "by_error": {"ProviderUnavailable": 1}}
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: degraded)
    written: list[tuple[dict, str]] = []
    monkeypatch.setattr(cli, "save_report", lambda rep, name="latest.json": written.append((rep, name)) or tmp_path / name)
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    rc = cli.main(["--dataset", "golden", "--save-baseline", "--baseline", "should_not_exist.json"])
    assert rc == 2
    assert [n for _, n in written] == ["latest.json"]  # 기준선 파일은 쓰지 않았다

    def aborted(*a, **k):
        raise EvalAborted("boom", {**degraded, "aborted": {"after_case": "G-003", "consecutive_provider_failures": 3, "done": 3, "planned": 55}})

    monkeypatch.setattr(cli, "run_eval", aborted)
    written.clear()
    assert cli.main(["--dataset", "golden", "--save-baseline"]) == 2
    assert [n for _, n in written] == ["latest.json"]
