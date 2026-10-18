"""방식 비교 3차 점검 — 누가 판정했는가, 돈을 쓴 측정을 버리지 않는 저장 규칙, 승인 범위, 추정 비용 범위, 화면.

가짜 제공자(tests/fakes.py)로만 돌린다: 네트워크도 실제 모델도 쓰지 않는다. 실제 모델 품질을 보증하는 테스트가 아니라 하네스가 어떻게 움직이는지 확인한다.
"""
from __future__ import annotations

import json

import pytest

from core.config import Settings
from evals import compare
from evals.runner import EvalCase, load_dataset, load_report
from guardrails.policy import get_catalog
from llm.base import LLMResponse
from llm.errors import ProviderError
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider

CAT = get_catalog()
LIVE = Settings.from_env(openai_api_key="sk-fake-openai", anthropic_api_key="sk-fake-anthropic", force_demo=False)     # 두 키 → LIVE, 기준 제공자 openai
APPROVE = {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "통제된 응답: 문제 없음", "rewrite": None}
REVISE_WITH_REWRITE = {"decision": "REVISE", "confidence": 0.9, "findings": [], "rationale": "통제된 응답: 수정 제안",
                       "rewrite": {"headline": "가을 신상품", "body": "자세한 조건은 상세 페이지에서 확인하세요", "cta": "자세히 보기", "disclosures": []}}
APPROVED = ["golden", "holdout", "contrast_g069", "conditions"]


def provider(name, *, cost=0.001, fail=None, response=APPROVE, repaired_from=None):
    """통제된 심사 응답을 내는 가짜 제공자. fail(n, req) 가 True 면 그 호출은 429 로 실패한다. repaired_from: 응답이 형식 수리를 거친 것으로 표시할 판단 제공자."""
    state = {"n": 0}

    def behave(req):
        state["n"] += 1
        if fail is not None and fail(state["n"], req):
            raise ProviderError("429 quota exceeded", provider=name)
        r = response(req) if callable(response) else response
        resp = LLMResponse(text="{}", parsed=req.schema.model_validate(r), provider=name, model="controlled", input_tokens=10, output_tokens=5, latency_ms=1.0, cost_usd=cost, finish_reason="stop")
        if repaired_from:
            resp.repaired, resp.origin_provider = True, repaired_from
        return resp

    return FakeProvider(name, model="controlled", behavior=behave)


def router_of(openai, anthropic=None, settings=LIVE):
    anthropic = anthropic or FakeProvider("anthropic", available=False)
    r = Router(settings, providers={"openai": openai, "anthropic": anthropic}, tracer=Tracer())
    r.new_run()
    return r


def case(id="X-1", label="APPROVE", **kw):
    return EvalCase(id=id, category="ecommerce", headline="가을 신상품", body="자세한 조건은 상세 페이지에서 확인하세요", cta="자세히 보기", label=label, rule_ids=[], **kw)


def fast_wait(router, *a, **k):
    """서킷이 닫힐 때까지 기다리는 대신 바로 닫는다 (테스트가 60초를 기다리지 않게)."""
    for b in router.breakers.values():
        b.success()
    return 0.0


@pytest.fixture
def env(monkeypatch, tmp_path):
    """main() 을 키가 둘 있는 환경으로 돌리되 reports 폴더는 임시 폴더로, 서킷 대기는 즉시로 바꾼다."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-anthropic")
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "0")
    monkeypatch.setattr("evals.runner.REPORTS", tmp_path)
    monkeypatch.setattr("evals.compare._wait_for_breaker", fast_wait)
    monkeypatch.setattr("evals.runner._wait_for_breaker", fast_wait)

    def install(router):
        monkeypatch.setattr(compare, "Router", lambda *a, **k: router)
        return router

    install.reports = tmp_path
    return install


def files(dirpath):
    return sorted(p.name for p in dirpath.iterdir() if p.is_file())


def read(dirpath, name):
    return json.loads((dirpath / name).read_text(encoding="utf-8"))


# ───────────── (1) 누가 판정했는가 ─────────────
def test_every_model_row_records_who_judged_and_a_clean_run_is_one_provider():
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider("openai")))
    llm = [r for r in rep["rows"] if r["system"] != "code_only"]
    assert llm and all(r["served_by"] == "openai" and r["judged_by"] == {"openai": 1} for r in llm)
    assert all(r["served_by"] == "" and r["judged_by"] == {} for r in rep["rows"] if r["system"] == "code_only")
    assert rep["provider"] == "openai" and rep["served_by"] == {"openai": 8} and rep["mixed_provider"]["n"] == 0
    assert rep["run_kind"] == "live" and rep["complete"] is True and rep["incomplete_reason"] == [] and compare.compare_allowed(rep) == (True, "")


def test_rows_judged_by_the_fallback_provider_make_the_result_incomplete_not_live():
    """기준 제공자(openai)가 중간에 429 로 막혀 anthropic 이 답한 사례가 있는 결과: 예전에는 정상 live 결과로 저장됐다."""
    openai = provider("openai", fail=lambda n, req: n >= 3)
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(openai, provider("anthropic")))
    assert rep["failures"]["n"] == 0                                           # 호출 실패 행은 없다 — 대체 경로가 답했을 뿐이다
    assert rep["served_by"]["anthropic"] >= 1 and rep["mixed_provider"]["n"] >= 1
    by_key = {f"{r['dataset']}:{r['case_id']}:{r['system']}": r for r in rep["rows"]}
    assert all(by_key[k]["served_by"] == "anthropic" and "anthropic" in by_key[k]["judged_by"] for k in rep["mixed_provider"]["cases"])      # 섞인 행마다 누가 답했는지 남는다
    assert any(r["served_by"] == "openai" for r in rep["rows"]) and rep["mixed_provider"]["by_provider"].keys() == {"anthropic"}
    ok, why = compare.compare_allowed(rep)
    assert ok is False and "기준 제공자(openai)" in why and "anthropic" in why
    assert rep["run_kind"] == "live_incomplete" and rep["complete"] is False and "mixed_provider" in rep["incomplete_reason"]
    assert rep["incomplete_note"].startswith("완료되지 않은 실행") and "세 방식 비교의 결과가 아닙니다" in rep["incomplete_note"]


def test_format_repair_by_another_provider_keeps_the_original_judge():
    """형식 수리를 다른 제공자가 했어도 판단한 쪽은 수리 전 텍스트를 만든 제공자(origin_provider)다. 반대로 대체 제공자가 판단하고 기준 제공자가 수리했다면 섞인 것이다."""
    ok_row = compare.run_llm_only(case(), LIVE, router_of(provider("openai", repaired_from="openai")), CAT)
    r = compare.run_llm_only(case(), LIVE, router_of(provider("anthropic", repaired_from="openai")), CAT)         # 수리는 anthropic, 판단은 openai
    assert ok_row.served_by == "openai" and r.served_by == "openai" and r.judged_by == {"openai": 1} and r.repaired_by == {"anthropic": 1}
    r.dataset = "d"
    rep = compare.build_report([r], {"d": {"n": 1, "split": "dev"}}, LIVE, CAT, ("llm_only",), "live")
    assert rep["mixed_provider"]["n"] == 0 and compare.compare_allowed(rep)[0] is True and rep["repaired_by"] == {"anthropic": 1} and rep["served_by"] == {"openai": 1}
    swapped = compare.run_llm_only(case(), LIVE, router_of(provider("openai", repaired_from="anthropic")), CAT)        # 수리는 openai, 판단은 anthropic
    swapped.dataset = "d"
    rep2 = compare.build_report([swapped], {"d": {"n": 1, "split": "dev"}}, LIVE, CAT, ("llm_only",), "live")
    assert swapped.served_by == "anthropic" and rep2["mixed_provider"]["n"] == 1 and compare.compare_allowed(rep2)[0] is False


def test_a_rewrite_recheck_answered_by_another_provider_is_counted_but_is_not_the_main_verdict():
    """수정안 재검사(두 번째 호출)만 대체 제공자가 답해도 그 행은 섞인 행이다. 주 판정(served_by)은 첫 호출의 제공자로 남긴다."""
    openai = provider("openai", response=REVISE_WITH_REWRITE, fail=lambda n, req: req.label.endswith(":rewrite"))
    anthropic = provider("anthropic", response=APPROVE)
    row = compare.run_combined(case(label="REVISE"), LIVE, router_of(openai, anthropic), CAT)
    assert row.served_by == "openai" and row.judged_by == {"openai": 1, "anthropic": 1} and not row.error
    row.dataset = "d"
    rep = compare.build_report([row], {"d": {"n": 1, "split": "dev"}}, LIVE, CAT, ("combined",), "live")
    assert rep["mixed_provider"]["n"] == 1 and rep["run_kind"] == "live_incomplete"


def test_sustained_fallback_stops_the_run_instead_of_spending_on_an_unapproved_model():
    """기준 제공자가 계속 막혀 대체 제공자가 연속으로 답하면 중단한다 — 승인 밖 모델에 예산을 계속 쓰지 않는다."""
    openai = provider("openai", fail=lambda n, req: True)
    anthropic = provider("anthropic")
    rep = compare.run_compare(["golden"], LIVE, router=router_of(openai, anthropic), abort_after=3)
    assert rep["aborted"]["kind"] == "consecutive_fallback" and "연속" in rep["aborted"]["reason"]
    assert anthropic.calls == 3 and len([r for r in rep["rows"] if r["system"] != "code_only"]) == 3
    assert "consecutive_fallback" in rep["incomplete_reason"] and "mixed_provider" in rep["incomplete_reason"] and rep["run_kind"] == "live_incomplete"


def test_one_fallback_row_does_not_stop_the_run_and_keeps_the_rest_of_the_measurement():
    """한 건만 대체 제공자가 답했다면 중단하지 않고 끝까지 돌리되, 그 행을 표시해 완료된 결과로는 세지 않는다."""
    openai = provider("openai", fail=lambda n, req: n == 2)
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(openai, provider("anthropic")), abort_after=3)
    assert rep["aborted"] is None and len(rep["rows"]) == 12 and rep["mixed_provider"]["n"] == 1
    assert rep["incomplete_reason"] == ["mixed_provider"] and rep["case_counts"]["contrast_g069"] == {"total": 4, "completed": 4, "failed_rows": 0}


# ───────────── (2) 돈을 쓴 측정을 버리지 않는 저장 ─────────────
def test_a_complete_clean_run_is_saved_as_compare_live_json_only(env, capsys):
    env(router_of(provider("openai")))
    assert compare.main(["--datasets", ",".join(APPROVED), "--cost-cap", "3.5", "--out", "compare_live.json"]) == 0
    assert files(env.reports) == ["compare_live.json"]
    rep = read(env.reports, "compare_live.json")
    assert rep["run_kind"] == "live" and rep["complete"] is True and rep["incomplete_reason"] == [] and rep["incomplete_note"] == ""
    assert rep["served_by"] == {"openai": 220} and rep["provider"] == "openai" and len(rep["rows"]) == 330
    assert {ds: (c["completed"], c["total"]) for ds, c in rep["case_counts"].items()} == {"golden": (55, 55), "holdout": (12, 12), "contrast_g069": (4, 4), "conditions": (39, 39)}
    assert list(rep["datasets"]) == APPROVED and rep["cost"]["over_cap"] is False and rep["scope"]["full_approved_scope"] is True
    assert "saved evals/reports/compare_live.json" in capsys.readouterr().out


def test_cost_cap_stops_spending_at_once_and_keeps_the_rows_measured_so_far(env):
    """상한에 닿은 그 행에서 멈춘다(다음 호출을 하지 않는다). 측정한 행은 버리지 않고 완료되지 않은 실행 파일에 남기며 종료 코드는 2."""
    openai = provider("openai", cost=0.0625)                                    # 16번째 호출에서 누적 정확히 $1.00
    env(router_of(openai))
    (env.reports / "compare_live.json").write_text("SENTINEL", encoding="utf-8")
    assert compare.main(["--cost-cap", "1.0"]) == 2
    assert openai.calls == 16                                                   # 상한에 닿은 뒤 호출 0
    assert (env.reports / "compare_live.json").read_text(encoding="utf-8") == "SENTINEL"      # 완료되지 못한 실행은 compare_live.json 을 만들지도 덮어쓰지도 않는다
    assert "compare_failed.json" not in files(env.reports) and "compare_live_incomplete.json" in files(env.reports)
    rep = read(env.reports, "compare_live_incomplete.json")
    assert rep["run_kind"] == "live_incomplete" and rep["complete"] is False and rep["incomplete_reason"] == ["cost_cap_reached"]
    assert rep["aborted"]["kind"] == "cost_cap" and len(rep["rows"]) == 24 and rep["failures"]["n"] == 0
    assert rep["incomplete_note"].startswith("완료되지 않은 실행") and "상한" in rep["incomplete_note"]
    assert rep["case_counts"]["golden"] == {"total": 55, "completed": 8, "failed_rows": 0}
    assert rep["case_counts"]["conditions"]["completed"] == 0 and rep["datasets"]["conditions"]["n"] == 39       # 닿지 못한 데이터의 전체 건수도 남는다
    assert rep["cost"] == {"spent_usd": 1.0, "cap_usd": 1.0, "over_cap": False, "lower_bound": False, "failed_requests": 0, "estimated_calls": 0}      # 실패한 요청이 없으면 정확한 값


def test_a_fully_completed_run_that_ends_just_over_the_cap_is_kept_not_discarded(env):
    """마지막 호출이 상한을 살짝 넘긴 완주 결과: 예전에는 중단으로 보고 compare_failed.json 에만 남겼다. 이제 모든 행을 incomplete 파일에 남기고 이유(cost_over_cap)를 적는다."""
    env(router_of(provider("openai", cost=0.0625)))                             # 220회 × 0.0625 = 13.75, 마지막 호출 전까지는 13.6875 로 상한 13.7 아래
    assert compare.main(["--cost-cap", "13.7"]) == 2
    assert files(env.reports) == ["compare_live_incomplete.json"]
    rep = read(env.reports, "compare_live_incomplete.json")
    assert rep["run_kind"] == "live_incomplete" and rep["incomplete_reason"] == ["cost_over_cap"] and rep["aborted"] is None
    assert len(rep["rows"]) == 330 and all(c["completed"] == c["total"] for c in rep["case_counts"].values())
    assert rep["cost"]["spent_usd"] == 13.75 and rep["cost"]["over_cap"] is True and "넘었" in rep["incomplete_note"]


def test_one_failed_row_is_recorded_and_the_rest_of_the_measurement_is_kept(env):
    """사례 하나가 끝내 실패해도 나머지 측정은 버리지 않는다: 끝까지 돌려 모든 행을 incomplete 파일에 남기고 종료 코드 2 (compare_live.json 은 만들지 않는다)."""
    openai = provider("openai", fail=lambda n, req: "G-005" in req.label)
    env(router_of(openai))
    assert compare.main([]) == 2
    assert files(env.reports) == ["compare_live_incomplete.json"]
    rep = read(env.reports, "compare_live_incomplete.json")
    assert rep["incomplete_reason"] == ["failed_rows"] and rep["aborted"] is None and rep["failures"]["n"] == 2
    assert len(rep["rows"]) == 330 and sorted(rep["failures"]["cases"]) == ["golden:G-005:combined", "golden:G-005:llm_only"]
    assert rep["case_counts"]["golden"] == {"total": 55, "completed": 55, "failed_rows": 2}
    assert "2건" in rep["incomplete_note"]


def test_consecutive_failures_abort_and_the_measured_rows_go_to_the_incomplete_file(env):
    openai = provider("openai", fail=lambda n, req: n > 2)
    env(router_of(openai))
    assert compare.main([]) == 2
    assert files(env.reports) == ["compare_live_incomplete.json"]
    rep = read(env.reports, "compare_live_incomplete.json")
    assert rep["aborted"]["kind"] == "consecutive_failures" and rep["aborted"]["after_case"].startswith("golden:")
    assert set(rep["incomplete_reason"]) == {"consecutive_failures", "failed_rows"} and rep["run_kind"] == "live_incomplete"
    assert any(not r["error"] for r in rep["rows"] if r["system"] != "code_only")        # 앞서 측정한 행은 남아 있다


def test_a_mixed_provider_run_is_saved_as_incomplete_with_the_attribution(env):
    openai = provider("openai", fail=lambda n, req: "G-005" in req.label)
    anthropic = provider("anthropic")
    env(router_of(openai, anthropic))
    assert compare.main([]) == 2
    assert files(env.reports) == ["compare_live_incomplete.json"]
    rep = read(env.reports, "compare_live_incomplete.json")
    assert rep["incomplete_reason"] == ["mixed_provider"] and rep["failures"]["n"] == 0
    assert rep["served_by"] == {"openai": 218, "anthropic": 2} and sorted(rep["mixed_provider"]["cases"]) == ["golden:G-005:combined", "golden:G-005:llm_only"]
    assert rep["provider"] == "openai" and rep["mixed_provider"]["by_provider"] == {"anthropic": 2}


def test_incomplete_run_never_overwrites_the_live_result_and_keeps_the_previous_incomplete_file(env):
    """compare_live.json 은 완료된 실행만 만들고, 같은 이름의 이전 결과는 덮어쓰기 전에 history/ 에 남긴다."""
    env(router_of(provider("openai", cost=0.0625)))
    live_text = json.dumps({"run_kind": "live", "marker": "approved-result"})
    (env.reports / "compare_live.json").write_text(live_text, encoding="utf-8")
    (env.reports / "compare_live_incomplete.json").write_text(json.dumps({"run_kind": "live_incomplete", "generated_at": "2026-10-01T09:30:00+00:00", "marker": "older"}), encoding="utf-8")
    assert compare.main(["--cost-cap", "1.0"]) == 2
    assert (env.reports / "compare_live.json").read_text(encoding="utf-8") == live_text
    assert read(env.reports, "compare_live_incomplete.json")["run_kind"] == "live_incomplete"
    kept = list((env.reports / "history").glob("compare_live_incomplete_*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text(encoding="utf-8"))["marker"] == "older"


def test_a_new_complete_live_result_keeps_the_previous_one_in_history(env):
    env(router_of(provider("openai")))
    (env.reports / "compare_live.json").write_text(json.dumps({"run_kind": "live", "generated_at": "2026-10-01T00:00:00+00:00", "marker": "first"}), encoding="utf-8")
    assert compare.main([]) == 0
    assert read(env.reports, "compare_live.json")["run_kind"] == "live"
    kept = list((env.reports / "history").glob("compare_live_*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text(encoding="utf-8"))["marker"] == "first"


def test_controlled_and_live_results_never_take_each_others_file_names(monkeypatch, tmp_path, env):
    # 대역(통제된) 실행은 --out compare_live.json 을 줘도 compare_controlled.json 으로만 저장된다
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    assert compare.main(["--datasets", "contrast_g069", "--out", "compare_live.json"]) == 0
    assert files(env.reports) == ["compare_controlled.json"] and read(env.reports, "compare_controlled.json")["run_kind"] == "controlled"
    controlled_text = (env.reports / "compare_controlled.json").read_text(encoding="utf-8")
    # 실제 모델 실행은 --out compare_controlled.json 을 줘도 대역 결과를 건드리지 않고 compare_live.json 으로 저장된다
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-anthropic")
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "0")
    env(router_of(provider("openai")))
    assert compare.main(["--out", "compare_controlled.json"]) == 0
    assert (env.reports / "compare_controlled.json").read_text(encoding="utf-8") == controlled_text
    assert read(env.reports, "compare_live.json")["run_kind"] == "live"
    # 완료되지 못한 실행의 파일 이름은 --out 으로 바꿀 수 없다
    assert compare._result_file("live", "compare_live_incomplete.json")[0] == "compare_live.json"
    assert compare._result_file("controlled", "compare_live.json")[0] == "compare_controlled.json"
    assert compare._result_file("live", "../x/custom.json")[0] == "custom.json"


def test_partial_scope_live_run_is_kept_but_never_becomes_the_headline_result(env):
    """일부 사례만(--limit) 돌린 실제 모델 실행은 compare_live.json 을 대신할 수 없다 — 측정은 incomplete 파일에 남긴다."""
    env(router_of(provider("openai")))
    assert compare.main(["--limit", "3"]) == 2
    rep = read(env.reports, "compare_live_incomplete.json")
    assert rep["incomplete_reason"] == ["partial_scope"] and rep["scope"]["limit"] == 3 and len(rep["rows"]) == 4 * 3 * 3
    assert "compare_live.json" not in files(env.reports)


def test_live_run_without_a_cost_cap_argument_still_stops_at_the_approved_cap(env):
    """실제 모델 실행에서 --cost-cap 을 비워도 승인된 $3.5 가 상한이다 (예전에는 상한 없이 $5 까지 갔다)."""
    openai = provider("openai", cost=0.5)
    r = env(router_of(openai))
    assert compare.main([]) == 2
    assert r.budget.cap_usd == compare.APPROVED_COST_CAP_USD == 3.5 and openai.calls == 7        # 3.5 에서 멈춘다
    assert read(env.reports, "compare_live_incomplete.json")["aborted"]["kind"] == "cost_cap"


def test_a_harness_bug_on_one_row_or_in_the_loop_does_not_throw_away_the_paid_rows(env, monkeypatch):
    """하네스 쪽 예외(검사 코드 오류, 진행 표시 오류 등)가 나도 지금까지 쓴 비용의 측정 행은 리포트와 incomplete 파일에 남는다."""
    openai = provider("openai")
    real = compare.run_combined

    def flaky(case, *a, **k):
        if case.id == "G-004":
            raise RuntimeError("검사 코드 오류")
        return real(case, *a, **k)

    monkeypatch.setattr(compare, "run_combined", flaky)
    rep = compare.run_compare(["golden"], LIVE, router=router_of(openai), systems=("llm_only", "combined"))
    bad = [r for r in rep["rows"] if r["error"]]
    assert len(rep["rows"]) == 110 and [r["case_id"] for r in bad] == ["G-004"] and bad[0]["error"].startswith("하네스 오류 RuntimeError")
    assert "failed_rows" in rep["incomplete_reason"] and rep["run_kind"] == "live_incomplete"
    # 반복문 바깥쪽(진행 표시)에서 난 오류: 지금까지의 행을 남기고 harness_error 로 멈춘다
    monkeypatch.setattr(compare, "run_combined", real)
    seen = {"n": 0}

    def progress(ds, system, i, n):
        seen["n"] += 1
        if seen["n"] == 7:
            raise OSError("pipe closed")

    rep2 = compare.run_compare(["golden"], LIVE, router=router_of(openai), progress=progress)
    assert rep2["aborted"]["kind"] == "harness_error" and len(rep2["rows"]) == 7 and rep2["incomplete_reason"] == ["harness_error"]
    assert "하네스 오류" in rep2["incomplete_note"] and rep2["run_kind"] == "live_incomplete"
    env(router_of(provider("openai")))
    monkeypatch.setattr(compare, "print_summary", lambda rep: (_ for _ in ()).throw(KeyError("출력 오류")))
    assert compare.main([]) == 0 and files(env.reports) == ["compare_live.json"]            # 요약 출력이 실패해도 저장은 이미 끝났다


def test_estimate_does_not_reuse_a_measured_cost_when_the_dataset_changed_since(monkeypatch):
    same = compare.estimate(["golden"], ("combined",))
    monkeypatch.setattr("evals.compare.dataset_fingerprint", lambda d: {"dataset_version": "9", "dataset_sha256": "changed"})
    changed = compare.estimate(["golden"], ("combined",))
    assert same["systems"]["combined"]["calls"] == 66 and changed["systems"]["combined"]["calls"] != 66      # 바뀐 데이터는 잰 값 대신 라벨별 계산으로


# ───────────── 승인 범위: conditions_extra 는 들어갈 수 없다 ─────────────
def test_conditions_extra_cannot_enter_a_live_comparison(env, capsys):
    assert "conditions_extra" not in compare.APPROVED_DATASETS and "conditions_extra" not in compare.build_parser().parse_args([]).datasets
    openai = provider("openai")
    r = env(router_of(openai))
    assert compare.main(["--datasets", "golden,conditions_extra"]) == 2
    assert openai.calls == 0 and files(env.reports) == [] and "conditions_extra" in capsys.readouterr().out
    with pytest.raises(ValueError, match="conditions_extra"):
        compare.run_compare(["golden", "conditions_extra"], LIVE, router=r)
    assert openai.calls == 0
    # 코드 검사만(모델 호출 없음)은 승인 범위와 상관없이 돌 수 있다
    rep = compare.run_compare(["contrast_g069"], LIVE, systems=("code_only",))
    assert rep["run_kind"] == "live" and rep["rows"]


def test_unknown_dataset_name_fails_before_any_spending(env):
    openai = provider("openai")
    r = env(router_of(openai))
    assert compare.main(["--datasets", "golden,no_such_dataset"]) == 2                  # 승인된 이름이 아니라 실행하지 않는다
    with pytest.raises(FileNotFoundError):                                              # 대역 실행이라 승인 검사를 건너뛰더라도, 모든 데이터를 먼저 읽으므로 호출 전에 실패한다
        compare.run_compare(["golden", "no_such_dataset"], LIVE, router=r, run_kind="controlled", systems=("code_only",))
    assert openai.calls == 0 and files(env.reports) == []


# ───────────── (4) --estimate: 낮게·중간·높게 ─────────────
def test_estimate_is_a_calibrated_range_not_one_understated_number():
    e = compare.estimate(APPROVED, ("code_only", "llm_only", "combined"))
    r = e["cost_range_usd"]
    assert r["low"] < r["central"] < r["high"] and e["total_cost_usd"] == r["central"]
    assert abs(e["total_calls"] - 253) <= 3 and e["calls_range"]["low"] <= e["total_calls"] <= e["calls_range"]["high"]
    assert 3.2 < r["central"] < 3.9 and r["high"] > 3.5 and e["cap_usd"] == 3.5            # 예전 추정 $2.96 은 실제 측정치에 비해 낮았다
    assert e["systems"]["code_only"] == {"calls": 0, "cost_usd": 0.0} and e["systems"]["llm_only"]["calls"] == 110


def test_estimate_combined_cost_comes_from_the_committed_real_model_reports():
    reports = [load_report(n) for n in ("baseline_partial.json", "holdout_partial.json", "contrast_g069_partial.json")]
    measured = sum(c["cost_usd"] for rep in reports for c in rep["cases"])
    e = compare.estimate(["golden", "holdout", "contrast_g069"], ("combined",))
    g = compare.PROMPT_GROWTH
    for x in ("low", "central", "high"):
        assert e["cost_by_system_usd"]["combined"][x] == pytest.approx(measured * g[x], abs=0.001)          # 이미 잰 데이터는 잰 비용에 프롬프트 증가분만 더한다
    calls = sum(1 + (1 if (c["inputs"].get("has_rewrite") and c["inputs"].get("rewrite_semantic") is not None) else 0) for rep in reports for c in rep["cases"])
    assert e["systems"]["combined"]["calls"] == calls == 90


def test_llm_only_prompt_ratio_assumption_matches_the_prompts_and_the_reports():
    """‘검토 모델만’ 호출 비용은 검토 호출의 몇 배인가의 가정(0.9)을 실제 프롬프트 길이와 저장된 리포트로 확인한다. 프롬프트가 바뀌어 어긋나면 이 테스트가 다시 맞추라고 알려 준다."""
    from agents import reviewer
    from agents.prompts import llm_only_system, reviewer_system
    from guardrails import output_guard

    lo = rv = 0
    for ds in APPROVED:
        for c in load_dataset(ds):
            copy, product = c.to_copy(), c.to_product()
            cats = CAT.categories_for(product, copy.full_text())
            det = output_guard.check(copy, product, cats, c.landing_text, [], CAT)
            facts = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
            lo += len(llm_only_system(CAT, cats)) + len(compare._llm_only_user(copy, facts, c.landing_text))
            rv += len(reviewer_system(CAT, cats)) + len(reviewer._user_prompt(copy, det, facts, c.landing_text))
    ratio = lo / rv
    ra = compare.LLM_ONLY_PROMPT_RATIO
    assert ra["low"] <= ratio <= ra["high"] and abs(ratio - ra["central"]) <= 0.05, f"실제 프롬프트 길이 비 {ratio:.3f} — 가정 {ra} 을 다시 맞춘다"
    assert ratio > 0.6                                                                          # 예전 가정(0.6배)은 실제보다 한참 낮았다
    # 리포트 쪽: 수정안 없이 검토 호출 한 번으로 끝난 사례의 실측 평균 비용 대비 ‘검토 모델만’ 호출 추정 비용
    cal = compare._calibration()
    e = compare.estimate(["golden"], ("llm_only",))
    per_call = e["systems"]["llm_only"]["cost_usd"] / e["cases"]
    assert 0.85 * cal["one_call"] < per_call < 1.1 * cal["one_call"] * compare.PROMPT_GROWTH["central"]


def test_estimate_warns_when_the_high_estimate_passes_the_cap_or_the_central_one_is_near_it():
    sets = (APPROVED, ("code_only", "llm_only", "combined"))
    base = compare.estimate(*sets)
    r = base["cost_range_usd"]
    assert r["central"] / r["high"] > 0.85                                                                     # 아래 경계 검사의 전제: 범위가 상한 15% 안에 들어 있다
    assert len(base["warnings"]) >= 1 and "높게 잡은 추정" in base["warnings"][0] and "상한" in base["warnings"][0]        # 기본 상한 $3.5: 높게 추정이 상한을 넘는다
    # 높게 추정은 상한 안이지만 중간 추정이 상한의 85% 이상(15% 이내)이면 그 경고만
    near = compare.estimate(*sets, cost_cap_usd=round(r["high"] * 1.02, 2))
    assert len(near["warnings"]) == 1 and "15% 이내" in near["warnings"][0] and "높게 잡은" not in near["warnings"][0]
    # 중간 추정이 상한의 85% 미만이고 높게 추정도 상한 안이면 경고 없음
    assert compare.estimate(*sets, cost_cap_usd=10.0)["warnings"] == []
    assert compare.estimate(*sets, cost_cap_usd=round(r["central"] / 0.84, 2))["warnings"] == []
    # 중간 추정이 상한을 넘으면 따로 알린다
    over = compare.estimate(*sets, cost_cap_usd=3.0)
    assert any("중간 추정" in w and "넘습니다" in w for w in over["warnings"]) and any("높게 잡은" in w for w in over["warnings"])
    assert all("추정" in w for w in base["warnings"])                                                          # 보장처럼 쓰지 않는다


def test_estimate_command_prints_the_range_the_calls_the_cap_and_a_warning_in_korean(capsys, monkeypatch):
    monkeypatch.setattr("evals.compare.Router", lambda *a, **k: (_ for _ in ()).throw(AssertionError("estimate 는 호출하지 않는다")))
    assert compare.main(["--estimate"]) == 0
    out = capsys.readouterr().out
    for needle in ("추정", "낮게 $", "중간 $", "높게 $", "모델 호출 약", "비용 상한 $3.50", "경고:", "실제 값은 달라질 수 있다", "0.9"):
        assert needle in out, needle
    assert "보장" not in out
    assert compare.main(["--estimate", "--cost-cap", "9", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["cap_usd"] == 9.0 and j["warnings"] == [] and set(j["cost_range_usd"]) == {"low", "central", "high"}


def test_cost_cap_argument_must_be_a_positive_number(capsys):
    for bad in ("0", "-1", "abc", "nan"):
        with pytest.raises(SystemExit):
            compare.build_parser().parse_args(["--cost-cap", bad])
    capsys.readouterr()


# ───────────── 화면: 세 종류의 결과를 서로 섞지 않는다 ─────────────
def _incomplete_report():
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider("openai", cost=0.0625)), cost_cap_usd=0.25, approved_scope=True)
    assert rep["run_kind"] == "live_incomplete"
    return rep


def _render(monkeypatch, reports):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr("ui.compare_view.load_report", lambda name: reports.get(name))

    def app():
        from ui import compare_view

        compare_view.render()

    at = AppTest.from_function(app, default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at):
    return "\n".join(str(x.value) for x in list(at.markdown) + list(at.caption) + list(at.info) + list(at.warning) + list(at.error))


def test_view_shows_an_incomplete_run_in_its_own_block_and_never_as_the_real_result(monkeypatch):
    at = _render(monkeypatch, {compare.INCOMPLETE_NAME: _incomplete_report()})
    t = _text(at)
    assert "완료되지 않은 실행" in t and "세 방식 비교의 결과가 아님" in t and "실제 모델로 돌린 비교 결과는 아직 없습니다" in t
    assert "실제 모델 결과 (끝까지 마친 실행)" not in t and "끝까지 마친 실행이 없습니다" in t
    counts = [df for df in at.dataframe if "마친 사례" in list(df.value.columns)]
    assert counts and counts[0].value.iloc[0]["데이터"] == "contrast_g069" and int(counts[0].value.iloc[0]["전체 사례"]) == 4
    assert any("세 방식 비교의 결과로 읽지 마세요" in e.label for e in at.expander)
    assert any("판정한 제공자" in c.value for c in at.caption)


def test_view_keeps_real_incomplete_and_stand_in_results_apart(monkeypatch):
    live = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider("openai")))
    ctrl = compare.run_compare(["contrast_g069"], Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True))
    assert live["run_kind"] == "live" and ctrl["run_kind"] == "controlled"
    at = _render(monkeypatch, {compare.LIVE_NAME: live, compare.INCOMPLETE_NAME: _incomplete_report(), compare.CONTROLLED_NAME: ctrl})
    heads = [m.value for m in at.markdown if str(m.value).startswith("### ")]
    assert "### 실제 모델 결과 (끝까지 마친 실행)" in heads and any(h.startswith("### 완료되지 않은 실행") for h in heads)
    assert any("하네스 검증용" in e.label and "모델 품질 아님" in e.label for e in at.expander)         # 대역 결과는 접힌 상자에 따로
    assert heads.index("### 실제 모델 결과 (끝까지 마친 실행)") < next(i for i, h in enumerate(heads) if h.startswith("### 완료되지 않은 실행"))


def test_view_refuses_to_show_a_mislabelled_file_as_the_real_result(monkeypatch):
    """compare_live.json 자리에 완료되지 않은 실행이나 대역 결과가 들어가 있어도 ‘실제 모델 결과’ 로 보이지 않는다."""
    ctrl = compare.run_compare(["contrast_g069"], Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True))
    for bad in (_incomplete_report(), ctrl):
        at = _render(monkeypatch, {compare.LIVE_NAME: bad})
        t = _text(at)
        assert "실제 모델 결과 (끝까지 마친 실행)" not in t and "실제 모델로 돌린 비교 결과는 아직 없습니다" in t and "보여 주지 않습니다" in t
    relabelled = dict(_incomplete_report(), run_kind="live")                      # 이름표만 live 로 고친 파일도 내용(사유)으로 걸러진다
    at = _render(monkeypatch, {compare.LIVE_NAME: relabelled})
    assert "실제 모델 결과 (끝까지 마친 실행)" not in _text(at)


def test_view_shows_which_provider_judged_and_flags_rows_from_another_one(monkeypatch):
    openai = provider("openai", fail=lambda n, req: n == 2)
    rep = compare.run_compare(["contrast_g069"], LIVE, router=router_of(openai, provider("anthropic", repaired_from="openai")), approved_scope=True)
    at = _render(monkeypatch, {compare.INCOMPLETE_NAME: rep})
    caps = " ".join(c.value for c in at.caption)
    assert "판정한 제공자: openai" in caps and "(기준 제공자 openai)" in caps and "형식만 다른 제공자가 고친 응답" in caps
    assert any("기준 제공자가 아닌 제공자가 판정한 행이" in w.value for w in at.warning) is False                  # 수리 경로는 판단이 기준 제공자의 것이라 섞인 행이 아니다
    mixed = compare.run_compare(["contrast_g069"], LIVE, router=router_of(provider("openai", fail=lambda n, req: n == 2), provider("anthropic")), approved_scope=True)
    at2 = _render(monkeypatch, {compare.INCOMPLETE_NAME: mixed})
    assert any("기준 제공자가 아닌 제공자가 판정한 행이 1건" in w.value for w in at2.warning)
    assert "기준 제공자가 아닌 제공자가 판정한 행이 있음" in _text(at2) or "기준 제공자가 아닌 제공자가 판정한 행이 있음" in " ".join(str(m.value) for m in at2.markdown)


def test_view_estimate_text_gives_a_range_with_its_warning_when_nothing_is_stored(monkeypatch):
    at = _render(monkeypatch, {})
    t = _text(at)
    assert "실제 모델로 돌린 비교 결과는 아직 없습니다" in t and "모델 호출 약" in t and "추정 비용 $" in t and "~ $" in t and "모두 추정" in t
    assert any("비용 상한" in w.value for w in at.warning)
