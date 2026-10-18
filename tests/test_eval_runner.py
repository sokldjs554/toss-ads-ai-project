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
    rep = run_eval("holdout", s, use_llm=True, router=router, breaker_wait_s=0, abort_after=0)   # 전체 데이터셋
    assert rep["provider_failures"] == {"n": 0, "cases": [], "by_error": {}, "by_stage": {"reviewer": 0, "rewrite_recheck": 0}}
    assert rep["recovered_failures"]["n"] == 0 and rep["failed_requests"] == 0
    assert all(c["error"] == "" and c["rewrite_error"] == "" for c in rep["cases"])
    assert baseline_allowed(rep) == (True, "")


def test_partial_dataset_run_is_not_adopted_as_baseline(tracer):
    """--limit 로 일부 사례만 돌린 결과는 장애가 없어도 기준선이 될 수 없다 (전체 기준선을 덮어쓰지 않는다)."""
    s, router = _demo_router(tracer)
    rep = run_eval("golden", s, use_llm=True, router=router, limit=3, breaker_wait_s=0, abort_after=0)
    ok, why = baseline_allowed(rep)
    assert not ok and "부분 실행" in why and rep["run_settings"]["limit"] == 3
    assert not baseline_allowed(run_eval("golden", s, use_llm=False, limit=3, breaker_wait_s=0, abort_after=0))[0]


def test_baseline_with_limit_is_not_trusted_for_a_recommended_threshold(monkeypatch):
    from evals import runner

    full = run_eval("holdout", Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True), use_llm=False, breaker_wait_s=0, abort_after=0)
    part = dict(full, n=3, run_settings={**full["run_settings"], "limit": 3})
    monkeypatch.setattr(runner, "load_report", lambda name: part)
    rec = runner.recommendation_for(Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True), dataset="holdout")
    assert rec["tau"] is None and any("일부 사례만 평가된 결과" in m for m in rec["mismatches"])


def test_fallback_provider_answers_make_the_report_unsavable():
    """기준 제공자(OpenAI)가 429 로 실패해 Anthropic 이 대신 답한 사례는 오류가 남지 않아도 그 모델의 측정이 아니다."""
    from llm.errors import RateLimited
    from tests.fakes import scripted_review_provider

    s = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="sk-ant-fake", force_demo=False)
    ok = {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "x", "rewrite": None}
    router = Router(s, providers={"openai": scripted_review_provider(RateLimited("429", provider="openai"), "openai"), "anthropic": scripted_review_provider(ok, "anthropic")}, tracer=Tracer())
    rep = run_eval("holdout", s, use_llm=True, router=router, breaker_wait_s=0, abort_after=0)
    assert rep["provider_failures"]["n"] == 0 and rep["failed_requests"] > 0                 # 오류로는 남지 않는다
    assert rep["served_by"] == {"anthropic": 12}
    ok_, why = baseline_allowed(rep)
    assert not ok_ and "다른 제공자" in why and "anthropic" in why


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


def test_eval_stops_at_once_when_the_cost_cap_is_hit(tracer):
    """FM-20: 비용 상한에 닿으면 나머지를 '담당자 확인' 으로 채우며 이어 가지 않고 첫 실패 사례에서 멈춘다 (연속 3건 실패를 기다리지 않는다)."""
    s, router = _demo_router(tracer)
    with pytest.raises(EvalAborted) as ei:
        run_eval("golden", s, use_llm=True, router=router, limit=10, breaker_wait_s=0, abort_after=3, budget_cap_usd=0.0)
    partial = ei.value.report
    assert partial["aborted"]["done"] == 1 and "FM-20" in partial["aborted"]["reason"] and "FM-20" in str(ei.value)
    assert baseline_allowed(partial)[0] is False


def test_wait_for_breaker_sleeps_until_cooldown(tracer):
    s = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="", force_demo=False)
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


# ---------------------------------------------------------------- 레드팀 (FM-24 를 레드팀에도)
def _real_eval(case, settings, router, catalog, use_llm=True, **kw):
    from evals.runner import evaluate_case
    return evaluate_case(case, settings, router, catalog, use_llm=use_llm)


def test_redteam_records_attack_failures_and_never_counts_them_as_blocked_or_evaded(tracer, monkeypatch, tmp_path):
    """장애로 심사하지 못한 공격은 '잡았다' 도 '뚫렸다' 도 아니다. 집계·수확에서 빠지고 provider_failures 로 남는다."""
    import json

    from evals import redteam
    from guardrails.policy import get_catalog

    s, router = _demo_router(tracer)
    seen = {"n": 0}

    def flaky(case, settings, router, catalog, use_llm=True, breaker_wait_s=0, log=None):
        r = _real_eval(case, settings, router, catalog, use_llm)
        seen["n"] += 1
        if seen["n"] <= 2:  # 처음 두 공격은 제공자 장애. 첫째는 (있어서는 안 되지만) APPROVE 로 읽히는 상태까지 가정한다 — 그래도 '뚫림' 으로 세면 안 된다
            r.error, r.predicted, r.confidence = "ProviderUnavailable: 모든 제공자 실패 → openai: RateLimited: 429 no credits", ("APPROVE" if seen["n"] == 1 else "HUMAN_REVIEW"), 0.0
        return r

    monkeypatch.setattr(redteam, "evaluate_with_retry", flaky)
    rep = redteam.run_redteam(s, router=router, catalog=get_catalog(), limit_seeds=1, breaker_wait_s=0, abort_after=0)
    errored = [a for a in rep["attacks"] if a["error"]]
    assert len(errored) == 2 and all(not a["evaded"] for a in errored)
    pf = rep["provider_failures"]
    assert pf["n"] == 2 and pf["cases"] == [f"{a['seed_id']}:{a['strategy']}" for a in errored] and pf["by_error"] == {"ProviderUnavailable": 2}
    assert sum(v["errors"] for v in rep["by_strategy"].values()) == 2
    # 수확: 오류 난 공격은 HUMAN_REVIEW 라도 회귀셋에 넣지 않는다
    path = tmp_path / "adv.jsonl"
    before = redteam.harvest({**rep, "attacks": [a for a in rep["attacks"] if not a["error"]]}, path=path)
    path2 = tmp_path / "adv2.jsonl"
    assert redteam.harvest(rep, path=path2) == before
    ids = [json.loads(l)["id"] for l in path2.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert not any(f"{a['seed_id']}-{a['strategy']}" in i for a in errored for i in ids)
    from evals.runner import baseline_allowed
    assert baseline_allowed(rep)[0] is False


def test_redteam_generation_failure_is_retried_once_then_recorded(tracer, monkeypatch):
    from evals import redteam
    from guardrails.policy import get_catalog

    s, router = _demo_router(tracer)
    real = redteam.generate_attacks
    calls = {"n": 0}

    def gen(seed, router, strategies):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated 429")
        return real(seed, router, strategies)

    monkeypatch.setattr(redteam, "generate_attacks", gen)
    rep = redteam.run_redteam(s, router=router, catalog=get_catalog(), limit_seeds=1, breaker_wait_s=30, abort_after=3)
    assert calls["n"] == 2 and rep["provider_failures"]["n"] == 0 and rep["attacks"]  # 재시도로 회복

    def always_fail(seed, router, strategies):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(redteam, "generate_attacks", always_fail)
    rep = redteam.run_redteam(s, router=router, catalog=get_catalog(), limit_seeds=2, breaker_wait_s=30, abort_after=0)
    from evals.runner import load_dataset

    first_two = [c.id for c in load_dataset("redteam_seeds")[:2]]
    assert rep["attacks"] == [] and rep["provider_failures"]["cases"] == [f"{sid}:generate" for sid in first_two]
    assert rep["provider_failures"]["n"] == 2 and rep["provider_failures"]["by_error"] == {"RuntimeError": 2}


def test_redteam_aborts_after_consecutive_failures_and_on_cost_cap(tracer, monkeypatch):
    from evals import redteam
    from guardrails.policy import get_catalog

    s, router = _demo_router(tracer)

    def failing(case, settings, router, catalog, use_llm=True, breaker_wait_s=0, log=None):
        r = _real_eval(case, settings, router, catalog, use_llm)
        r.error, r.predicted = "ProviderUnavailable: down", "HUMAN_REVIEW"
        return r

    monkeypatch.setattr(redteam, "evaluate_with_retry", failing)
    with pytest.raises(EvalAborted) as ei:
        redteam.run_redteam(s, router=router, catalog=get_catalog(), breaker_wait_s=0, abort_after=3)
    ab = ei.value.report["aborted"]
    assert ab["consecutive_provider_failures"] == 3 and ab["done"] == 3 and ab["planned"] > 3
    assert ei.value.report["provider_failures"]["n"] == 3

    s, router = _demo_router(Tracer())

    def costly(case, settings, router, catalog, use_llm=True, breaker_wait_s=0, log=None):
        r = _real_eval(case, settings, router, catalog, use_llm)
        router.budget.spent_usd += 0.6  # 공격 하나에 $0.6 가 든다고 치자
        return r

    monkeypatch.setattr(redteam, "evaluate_with_retry", costly)
    with pytest.raises(EvalAborted) as ei:
        redteam.run_redteam(s, router=router, catalog=get_catalog(), breaker_wait_s=0, abort_after=3, cost_cap_usd=1.0)
    assert "상한" in str(ei.value) and ei.value.report["aborted"]["done"] == 2 and ei.value.report["aborted"]["cost_usd"] >= 1.2


def test_cli_redteam_refuses_harvest_and_baseline_when_failures_or_aborted(monkeypatch, tmp_path):
    degraded = {"mode": "partial", "seeds": 1, "attacks": [], "by_strategy": {}, "evasion_rate": 0.0, "cost_usd": 0.0,
                "provider_failures": {"n": 1, "cases": ["S-1:spacing"], "by_error": {"ProviderUnavailable": 1}}}
    harvested: list = []
    written: list[str] = []
    monkeypatch.setattr(cli, "save_report", lambda rep, name="latest.json": written.append(name) or tmp_path / name)
    monkeypatch.setattr("evals.redteam.harvest", lambda rep, *a, **k: harvested.append(rep) or 0)
    monkeypatch.setattr("evals.redteam.run_redteam", lambda *a, **k: degraded)
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    assert cli.main(["--redteam", "--harvest", "--save-baseline"]) == 2
    assert written == ["redteam_latest.json"] and harvested == []

    def aborted(*a, **k):
        raise EvalAborted("boom", {**degraded, "aborted": {"reason": "x", "done": 3, "planned": 32}})

    monkeypatch.setattr("evals.redteam.run_redteam", aborted)
    written.clear()
    assert cli.main(["--redteam", "--harvest", "--save-baseline"]) == 2
    assert written == ["redteam_latest.json"] and harvested == []

    clean = {**degraded, "provider_failures": {"n": 0, "cases": [], "by_error": {}}}
    monkeypatch.setattr("evals.redteam.run_redteam", lambda *a, **k: clean)
    written.clear()
    assert cli.main(["--redteam", "--harvest", "--save-baseline"]) == 0
    assert written == ["redteam_latest.json", "baseline_redteam_partial.json"] and len(harvested) == 1


# ---------------------------------------------------------------- 트레이스 기록
def _record_traces_module():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("record_traces_under_test", Path(__file__).resolve().parents[1] / "scripts" / "record_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_record_traces_writes_all_files_when_clean(tmp_path):
    rt = _record_traces_module()
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    out = tmp_path / "traces"
    assert rt.main(settings=s, out=out, log=lambda m: None) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(f"sample_demo_{k}.json" for k in rt.SCENARIOS)


def test_record_traces_writes_nothing_when_any_scenario_has_provider_failure(tmp_path):
    from core.schemas import FailureEvent

    rt = _record_traces_module()
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    out = tmp_path / "traces"
    out.mkdir()
    sentinel = out / "sample_demo_malicious.json"
    sentinel.write_text("OLD GOOD TRACE", encoding="utf-8")
    messages: list[str] = []
    calls = {"n": 0}

    def pipeline(brief, settings, options=None, tracer=None, **kw):
        run = rt.run_pipeline(brief, settings, options=options, tracer=tracer, **kw)
        calls["n"] += 1
        if calls["n"] == 3:  # 세 번째 시나리오(injection)에서만 제공자 장애가 기록됐다고 치자
            run.failures.append(FailureEvent(code="FM-07", stage="reviewer", message="ProviderUnavailable: 429", fallback="HUMAN_REVIEW"))
        return run

    rc = rt.main(settings=s, out=out, pipeline=pipeline, log=messages.append)
    assert rc == 2
    assert sentinel.read_text(encoding="utf-8") == "OLD GOOD TRACE"          # 기존 샘플을 덮어쓰지 않았다
    assert [p.name for p in out.iterdir()] == ["sample_demo_malicious.json"]   # 새 파일도 쓰지 않았다
    assert any("FM-07@reviewer" in m for m in messages)


def test_record_traces_also_refuses_failures_hidden_inside_the_trace(tmp_path):
    """run.failures 에 남지 않는 실패(입력 검사의 되묻기 호출 실패, 대체 제공자가 대신 답한 호출)도 저장을 막는다."""
    rt = _record_traces_module()
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    out = tmp_path / "traces"
    calls = {"n": 0}

    def pipeline(brief, settings, options=None, tracer=None, **kw):
        calls["n"] += 1
        run = rt.run_pipeline(brief, settings, options=options, tracer=tracer, **kw)
        if calls["n"] == 2:
            sp = tracer.latest().llm_spans()[0]
            sp.status = "error"
            sp.events.append({"name": "provider_error", "provider": "openai", "type": "RateLimited"})
        return run

    messages: list[str] = []
    assert rt.main(settings=s, out=out, pipeline=pipeline, log=messages.append) == 2
    assert not out.exists() or not list(out.iterdir())
    assert any("provider_error@" in m for m in messages) and any("error@" in m for m in messages)
