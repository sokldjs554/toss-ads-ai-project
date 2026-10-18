"""실패 뒤 복구와 연결 — 같은 문구 재시도, 재사용 키, 수정안 검사 실패의 집계, 재시도 뒤 남는 실패 기록, 확인 요청문의 출처.

실제 모델 품질이 아니라 '실패해도 통과로 흐르지 않고, 복구 경로가 사실을 지우지 않는다' 는 동작을 통제된 제공자로 재현한다.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agents import recheck as recheck_mod
from agents.pipeline import RunOptions, run_pipeline
from agents.recheck import RecheckHistory, recheck, recheck_categories, recheck_key
from core.config import Settings
from core.schemas import AdCopy, Claim, Decision, Fact, ProductCategory, ProductFacts, RewriteStatus
from evals.runner import baseline_allowed, evaluate_with_retry, load_dataset, run_eval
from guardrails.failsafe import PROVIDER_FAILURE_CODES, STEP_FAILURE_CODES
from guardrails.policy import get_catalog
from llm.errors import ProviderError, ProviderUnavailable
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider, scripted_review_provider
from ui.presets import PRESETS, build_brief

CAT = get_catalog()
SETTINGS = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="", force_demo=False)
PRODUCT = ProductFacts(name="OO몰 쿠폰", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)")])
COPY = AdCopy(headline="전 상품 10% 할인 쿠폰", body="10월 31일까지, 1인 1회 사용할 수 있어요.", cta="자세히 보기", disclosures=["10월 31일까지, 1인 1회"])
APPROVE = {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "통제된 응답", "rewrite": None}


def router_with(response, settings=SETTINGS, tracer=None):
    r = Router(settings, providers={"openai": scripted_review_provider(response), "anthropic": FakeProvider("anthropic", available=False)}, tracer=tracer or Tracer())
    r.new_run()
    return r


def do_recheck(response, copy=COPY, product=PRODUCT, settings=SETTINGS, landing=""):
    return recheck(copy, product, {"ecommerce"}, settings, router=router_with(response, settings), landing_text=landing, catalog=CAT, tracer=Tracer())


# ───────────── 재사용 키 ─────────────
def key_of(copy=COPY, product=PRODUCT, landing="", settings=SETTINGS, catalog=CAT, history=None, second_opinion=False, categories=None, margin=None):
    """화면이 키를 만드는 방식과 같다 — 적용 업종은 처음 검사의 업종({'ecommerce'}) + 고친 문구가 가져오는 단서."""
    cats = recheck_categories(catalog, product, copy, {"ecommerce"}) if categories is None else categories
    return recheck_key(copy, product, landing, settings, catalog, history, second_opinion, cats, *(() if margin is None else (margin,)))


def test_reuse_key_changes_with_everything_that_can_change_the_result(monkeypatch):
    base = key_of()
    assert key_of() == base                                                      # 같은 조건은 같은 키
    changed = {
        "text": key_of(copy=dataclasses.replace(COPY, headline="전 상품 5% 할인 쿠폰") if dataclasses.is_dataclass(COPY) else COPY.model_copy(update={"headline": "전 상품 5% 할인 쿠폰"})),
        "fact": key_of(product=PRODUCT.model_copy(update={"facts": [Fact(id="F1", text="전 상품 10% 할인 쿠폰 (10월 31일까지)")]})),
        "product name": key_of(product=PRODUCT.model_copy(update={"name": "다른 몰"})),
        "category": key_of(product=PRODUCT.model_copy(update={"category": ProductCategory.APP})),
        "landing": key_of(landing="랜딩 문구"),
        "threshold": key_of(settings=dataclasses.replace(SETTINGS, auto_decision_confidence=0.9)),
        "openai model": key_of(settings=dataclasses.replace(SETTINGS, openai_model="gpt-4o-mini")),
        "anthropic model": key_of(settings=dataclasses.replace(SETTINGS, anthropic_model="claude-haiku-5-5")),
        "mode": key_of(settings=dataclasses.replace(SETTINGS, force_demo=True)),
        "rejected history": key_of(history=["이미 반려된 광고"]),
        "cross-check on": key_of(second_opinion=True),
        "cross-check margin": key_of(second_opinion=True, margin=0.2),
        "applied categories": key_of(categories={"ecommerce", "finance"}),
        "carried facts": key_of(copy=COPY.model_copy(update={"carried_fact_ids": ["F1"]})),
        "claims": key_of(copy=COPY.model_copy(update={"claims": [Claim(text="전 상품 10% 할인", fact_id="F1")]})),
    }
    for name, k in changed.items():
        assert k != base, f"{name} 가 바뀌어도 이전 결과를 재사용하게 된다"
    assert len(set(changed.values())) == len(changed)
    monkeypatch.setattr(CAT, "version", "9999.0.0")
    assert key_of() != base, "검사 기준 버전이 바뀌어도 이전 결과를 재사용한다"
    monkeypatch.undo()
    monkeypatch.setattr(recheck_mod, "PROMPT_VERSION", "9999.0.0")
    assert key_of() != base, "프롬프트 버전이 바뀌어도 이전 결과를 재사용한다"


# ───────────── 실패는 재시도, 성공은 재사용 ─────────────
def test_provider_failure_is_recorded_as_failure_and_never_reusable():
    res = do_recheck(ProviderError("simulated outage", provider="openai"))
    assert res.failed and not res.reusable and "ProviderUnavailable" in res.review_error
    assert res.report.final_decision == Decision.HUMAN_REVIEW                       # 통과로 흐르지 않는다
    assert res.failures and res.failures[0].code in PROVIDER_FAILURE_CODES and res.failures[0].stage == "reviewer"
    assert res.failed_attempts >= 1 and res.cost_estimated                            # 응답을 못 받은 요청은 비용을 알 수 없다 — 0원이 아니라 추정
    assert res.trace_id and res.key == key_of()


def test_same_text_retry_after_failure_then_reuse_after_success():
    hist = RecheckHistory()
    k = key_of()
    first = do_recheck(ProviderError("simulated outage", provider="openai"))
    hist.add(first)
    assert hist.reusable_for(k) is None                    # 실패는 재사용하지 않는다 → 같은 문구를 다시 시도해야 한다
    assert hist.latest_for(k) is first
    second = do_recheck(APPROVE)
    hist.add(second)
    assert second.reusable and second.report.final_decision == Decision.APPROVE and second.key == first.key
    assert hist.reusable_for(k) is second                 # 성공하면 같은 조건에서는 모델을 다시 부르지 않는다
    u = hist.usage_for(k)
    assert u["tries"] == 2 and u["failed_tries"] == 1                                  # 성공한 재시도가 실패한 시도를 지우지 않는다
    assert u["failed_attempts"] >= 1 and u["cost_estimated"] is True                   # 실패한 요청의 비용은 알 수 없음이 남는다
    assert u["cost_usd"] == pytest.approx(first.cost_usd + second.cost_usd) and u["calls"] == first.calls + second.calls
    assert len(hist.attempts_for(k)) == 2


def test_changed_text_never_shows_the_previous_pass():
    hist = RecheckHistory()
    ok = do_recheck(APPROVE)
    hist.add(ok)
    edited = COPY.model_copy(update={"headline": "전 상품 15% 할인 쿠폰"})
    k2 = key_of(copy=edited)
    assert hist.reusable_for(k2) is None and hist.latest_for(k2) is None
    assert hist.latest() is ok and ok.key != k2           # 화면은 ok 를 '이전에 검사한 문구' 로만 보여 줄 수 있다


def test_same_text_under_another_product_or_settings_is_not_reused():
    hist = RecheckHistory()
    hist.add(do_recheck(APPROVE))
    other = PRODUCT.model_copy(update={"facts": [Fact(id="F1", text="전 상품 10% 할인 쿠폰 (11월 30일까지, 1인 1회)")]})
    assert hist.reusable_for(key_of(product=other)) is None
    assert hist.reusable_for(key_of(settings=dataclasses.replace(SETTINGS, auto_decision_confidence=0.95))) is None
    assert hist.reusable_for(key_of()) is not None


# ───────────── 수정안 의미 검사 실패 ─────────────
def _fail_rewrite_semantic(monkeypatch):
    import agents.rewrite_check as rc

    real = rc.reviewer.review

    def flaky(*a, **k):
        if k.get("label_suffix") == "rewrite":
            raise ProviderUnavailable("simulated outage on rewrite check")
        return real(*a, **k)

    monkeypatch.setattr(rc.reviewer, "review", flaky)


def test_rewrite_semantic_failure_is_counted_in_run_quality(monkeypatch, demo_settings, tracer):
    from scripts.record_traces import provider_failures
    from ui.workbench import run_state

    _fail_rewrite_semantic(monkeypatch)
    p = PRESETS["loan_ok"]
    run = run_pipeline(build_brief(p), demo_settings, options=RunOptions(landing_text=p["landing_text"]), tracer=tracer)
    with_rewrite = [r for r in run.reports if r.verdict.proposed_rewrite is not None]
    assert with_rewrite, "예시는 수정안을 내는 광고안을 포함해야 한다"
    rw_fail = [f for f in run.failures if f.stage == "rewrite_recheck" and f.code in PROVIDER_FAILURE_CODES]
    assert len(rw_fail) == len(with_rewrite)                                          # 수정안 하나당 실패 기록이 하나
    assert all(r.rewrite_status == RewriteStatus.UNCHECKED and r.verified_rewrite is None for r in with_rewrite)
    assert provider_failures(run), "기록용 실행에서 제공자 장애로 세어져야 한다"
    assert run_state({"run": run}) == "partial"                                      # 정상 실행과 구별


def test_clean_run_is_not_partial(demo_settings, tracer):
    from ui.workbench import run_state

    p = PRESETS["loan_ok"]
    run = run_pipeline(build_brief(p), demo_settings, options=RunOptions(landing_text=p["landing_text"]), tracer=tracer)
    assert not [f for f in run.failures if f.code in STEP_FAILURE_CODES]
    assert run_state({"run": run}) == "done"


def test_eval_counts_rewrite_semantic_failure_and_refuses_the_baseline(monkeypatch, demo_settings, tracer):
    _fail_rewrite_semantic(monkeypatch)
    router = Router(demo_settings, tracer=tracer)
    rep = run_eval("golden", demo_settings, use_llm=True, router=router, limit=20, breaker_wait_s=0, abort_after=0)
    pf = rep["provider_failures"]
    assert pf["n"] > 0 and pf["by_stage"]["rewrite_recheck"] == pf["n"] and pf["by_stage"]["reviewer"] == 0
    failed = [c for c in rep["cases"] if c["rewrite_error"]]
    assert failed and all(c["error"] == "" for c in failed) and pf["cases"] == [c["id"] for c in failed]
    ok, why = baseline_allowed(rep)
    assert not ok and "심사 모델 단계 실패" in why


# ───────────── 재시도가 성공해도 첫 실패와 사용량은 남는다 ─────────────
def test_eval_retry_keeps_the_failed_attempt_and_its_usage(monkeypatch):
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return ProviderError("simulated 500", provider="openai")
        return {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "통제된 응답", "rewrite": None}

    router = router_with(flaky)
    router.budget.cap_usd, router.budget.max_calls = 5.0, 10_000
    monkeypatch.setattr("evals.runner._wait_for_breaker", lambda *a, **k: 0.0)
    case = next(c for c in load_dataset("golden") if c.label == "APPROVE" and c.difficulty != "semantic_only")
    r = evaluate_with_retry(case, SETTINGS, router, CAT, use_llm=True, breaker_wait_s=30)
    assert not r.failed and r.predicted in ("APPROVE", "HUMAN_REVIEW", "REVISE")
    assert len(r.prior_attempts) == 1 and "ProviderUnavailable" in r.prior_attempts[0]["error"]
    assert r.failed_requests >= 1 and r.cost_estimated is True                         # 첫 시도의 실패 요청은 비용 미확인 → 추정
    assert r.cost_usd == pytest.approx(router.budget.spent_usd)                         # 두 시도의 알려진 비용을 모두 합산 (첫 시도 비용이 사라지지 않음)
    assert calls["n"] >= 2


def test_eval_report_lists_recovered_failures_and_unknown_cost(monkeypatch):
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return ProviderError("simulated 500", provider="openai") if calls["n"] == 1 else {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "x", "rewrite": None}

    router = router_with(flaky)
    monkeypatch.setattr("evals.runner._wait_for_breaker", lambda *a, **k: 0.0)
    rep = run_eval("holdout", SETTINGS, use_llm=True, router=router, breaker_wait_s=30, abort_after=0)   # 전체 데이터셋
    assert rep["provider_failures"]["n"] == 0 and baseline_allowed(rep) == (True, "")   # 재시도로 모델 판정을 받았으므로 측정에 쓸 수 있다
    rf = rep["recovered_failures"]
    assert rf["n"] == 1 and rf["cases"] == [rep["cases"][0]["id"]] and rf["by_error"] == {"ProviderUnavailable": 1}
    assert rep["failed_requests"] >= 1 and rep["cost_estimated"] is True
    assert rep["cases"][0]["prior_attempts"] and rep["cases"][1]["prior_attempts"] == []
    assert rep["cases"][0]["failed_requests"] >= 1 and rep["cases"][1]["failed_requests"] == 0   # 추정 표시는 실패가 있었던 사례에만 붙는다


# ───────────── 확인 요청문: 최신 수정 문구 + 그 재검사 + 그 처리 기록 ─────────────
def test_handoff_request_is_built_from_the_latest_recheck_not_the_original(demo_settings, tracer):
    from ui.components import human_review_request_text, recheck_request_text

    p = PRESETS["loan_ok"]
    run = run_pipeline(build_brief(p), demo_settings, options=RunOptions(landing_text=p["landing_text"]), tracer=tracer)
    rep = run.reports[0]
    edited = rep.ad_copy.model_copy(update={"headline": "고쳐 쓴 새 제목입니다"})
    res = do_recheck(ProviderError("simulated outage", provider="openai"), copy=edited, product=run.brief.product)
    text = recheck_request_text(res.report.ad_copy, res, rep.variant_id, run.run_id, CAT)
    assert "고쳐 쓴 새 제목입니다" in text and rep.ad_copy.headline not in text          # 원문이 아니라 수정한 문구
    assert f"처리 기록(이 재검사): {res.trace_id}" in text
    assert run.trace_id not in text                                                      # 원본 실행의 처리 기록 ID 를 이 요청의 근거로 적지 않는다
    assert f"참고: 수정 전 원본이 나온 실행 {run.run_id}" in text                       # 출처는 참고로만
    assert "수정본" in text and "검토 대기열·담당자 지정·권한 관리는 없으며" in text      # 범위를 정확히 적는다
    assert "확정하지 못한 이유" in text and "검토 모델 단계가 실패" in text
    orig = human_review_request_text(rep, run, CAT)
    assert run.trace_id in orig and "수정 전" in orig and "검토 대기열·담당자 지정·권한 관리는 없으며" in orig


# ───────────── 화면 흐름 (실패 → 같은 문구 재시도 → 재사용 → 문구 변경) ─────────────
@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    return AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180)


def _texts(at):
    return "\n".join(str(x.value) for group in (at.markdown, at.caption, at.info, at.warning, at.error, at.success) for x in group)


def _hist(at, vid):
    hs = [h for k, h in at.session_state["rechecks"].items() if k.endswith(f":{vid}")]
    return hs[-1] if hs else None


def test_ui_same_text_retry_after_failure_reuse_after_success_and_stale_text(app):
    at = app.run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    vid = at.session_state["runs"][0]["run"].reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("조건을 확인하고 신청하세요").run()

    # 1) 모델 호출이 실패하는 상황에서 검사 → 통과로 흐르지 않고 실패로 기록, 버튼은 '다시 시도'
    at.selectbox(key="opt_drill").set_value("mock: timeout").run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    h = _hist(at, vid)
    assert len(h.attempts) == 1 and h.attempts[0].failed and h.attempts[0].report.final_decision == Decision.HUMAN_REVIEW
    assert "확인하지 못했습니다" in _texts(at) and "통과로 보지 않습니다" in _texts(at)
    assert at.button(key=f"recheck_{vid}").label == "같은 문구로 다시 시도"
    assert "검사 기준을 통과했습니다" not in _texts(at)

    # 2) 장애가 걷힌 뒤 같은 문구를 다시 시도 → '이미 검사했습니다' 로 막히지 않고 실제로 다시 검사
    at.selectbox(key="opt_drill").set_value("없음").run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception
    h = _hist(at, vid)
    assert len(h.attempts) == 2 and h.attempts[0].failed and not h.attempts[1].failed
    assert "이 문구 검사 2번 시도 · 실패 1번" in _texts(at)                            # 첫 실패가 지워지지 않는다
    assert at.button(key=f"recheck_{vid}").label == "수정한 문구 다시 검사"

    # 3) 성공한 같은 문구는 다시 부르지 않고 재사용
    at.button(key=f"recheck_{vid}").click().run()
    assert len(_hist(at, vid).attempts) == 2
    assert "이미 검사했습니다" in _texts(at)

    # 4) 문구를 바꾸면 이전 결과를 '현재 결과' 로 보여 주지 않는다
    at.text_input(key=f"edit_h_{vid}").set_value("전혀 다른 제목").run()
    t = _texts(at)
    assert "이전에 검사한 문구" in t and "이 문구는 검사 기준을 통과했습니다" not in t and "이 문구 검사" not in t


def test_ui_handoff_request_uses_edited_text_and_latest_recheck_and_disappears_when_stale(app):
    at = app.run()
    at.button(key="run_button").click().run()
    run = at.session_state["runs"][0]["run"]
    vid = run.reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("고쳐 쓴 새 제목입니다").run()
    at.selectbox(key="opt_drill").set_value("mock: timeout").run()
    at.button(key=f"recheck_{vid}").click().run()
    h = _hist(at, vid)
    res = h.latest()
    assert res.report.final_decision == Decision.HUMAN_REVIEW
    codes = [c.value for c in at.code]
    request = next((c for c in codes if "[담당자 확인 요청]" in c and "수정본 재검사" in c), None)
    assert request, "재검사 결과에 대한 확인 요청문이 만들어져야 한다"
    assert "고쳐 쓴 새 제목입니다" in request and res.trace_id in request and run.trace_id not in request
    # 문구를 바꾸면 이전 결과로는 요청문을 만들지 않는다
    at.text_input(key=f"edit_h_{vid}").set_value("또 바꾼 제목").run()
    assert not any("고쳐 쓴 새 제목입니다" in c.value for c in at.code if "수정본 재검사" in c.value)
    assert "이전 결과로는 확인 요청문을 만들지 않습니다" in _texts(at)


# ───────────── 품질 확인의 평가 실행: 실제 설정 표시 · 방금 실행한 결과 · 기록 ─────────────
def _go_quality(at):
    at.session_state["nav_request"] = "품질 확인"
    return at.run()


def _settings_table(at) -> dict:
    for df in at.dataframe:
        v = df.value
        if "항목" in list(v.columns) and "설정 출처" in list(v["항목"]):
            return dict(zip(v["항목"], v["값"]))
    return {}


def test_eval_panel_uses_workbench_settings_and_shows_the_fresh_result(app):
    at = app.run()
    at.session_state["opt_tau"] = 0.91                       # 광고 작업의 자동 판정 확신 기준을 바꾸고 한 번 실행
    at.run()
    at.button(key="run_button").click().run()
    assert at.session_state["settings_overrides"]["auto_decision_confidence"] == pytest.approx(0.91)
    _go_quality(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    t = _settings_table(at)
    assert t["설정 출처"] == "광고 작업에서 마지막으로 쓴 설정" and t["자동 판정 확신 기준(τ)"] == "0.91"
    assert t["검사 기준 버전"] == CAT.version and t["프롬프트 버전"]
    assert "예시 실행" in t["실행 방식"] and t["평가할 사례"] != ""
    # 서버 기본 설정으로 바꾸면 표가 바뀐다 (두 설정은 따로)
    at.radio(key="eval_settings_src").set_value("default").run()
    t2 = _settings_table(at)
    assert t2["설정 출처"] == "서버 기본 설정" and t2["자동 판정 확신 기준(τ)"] == "0.80"
    at.radio(key="eval_settings_src").set_value("work").run()
    # 실행 → 방금 실행한 결과가 바로 보이고, 그 설정이 리포트에 기록된다
    at.number_input(key="eval_limit").set_value(3).run()
    at.button(key="eval_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.selectbox(key="eval_pick").value == "이 세션에서 실행한 결과"
    rep = at.session_state["eval_report"]
    assert rep["n"] == 3 and rep["threshold"] == pytest.approx(0.91) and rep["policy_version"] == CAT.version and rep["prompt_version"]
    assert rep["run_settings"]["source"] == "광고 작업에서 마지막으로 쓴 설정" and rep["run_settings"]["limit"] == 3 and rep["dataset_version"]
    assert "방금 이 세션에서 실행한 결과입니다" in "\n".join(str(x.value) for x in at.success)
    assert "OPENAI_API_KEY" not in str(rep) and "sk-" not in str(rep)                  # 설정 기록에 키 값이 들어가지 않는다


def test_viewing_stored_results_never_calls_a_model(app, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("저장된 결과를 보는 것만으로 모델/평가가 실행됐다")

    monkeypatch.setattr("ui.quality.run_eval", boom)
    monkeypatch.setattr("llm.router.Router.complete", boom)
    at = app.run()
    _go_quality(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    for label in at.selectbox(key="eval_pick").options:
        if label != "이 세션에서 실행한 결과":
            at.selectbox(key="eval_pick").set_value(label).run()
            assert not at.exception, (label, [str(e.value) for e in at.exception])


def test_live_eval_requires_confirmation_and_uses_the_server_cap(monkeypatch):
    """실제 모델을 부르는 평가는 호출 수·예상 비용을 보여 주고 확인해야 실행되며, 서버 키로 도는 평가는 서버 상한에서 멈춘다."""
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-FAKE-KEY-never-shown-0123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("PREFLIGHT_RUN_BUDGET_USD", "0.30")
    seen = {}

    def fake_run_eval(ds, settings, **kw):
        seen.update(kw, ds=ds, mode=settings.mode.value)
        raise AssertionError("확인 없이 실행됨")

    monkeypatch.setattr("ui.quality.run_eval", fake_run_eval)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180)
    at.run()
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    warn = "\n".join(str(w.value) for w in at.warning)
    assert "실제 모델을 부릅니다" in warn and "모델 호출 약" in warn and "$0.30" in warn
    assert at.button(key="eval_run").disabled is True       # 확인 전에는 실행할 수 없다
    assert "sk-test-FAKE-KEY" not in warn


def test_eval_retry_sums_the_cost_the_first_attempt_already_spent(monkeypatch):
    """첫 시도에서 검토 호출은 성공(비용 발생)했고 수정안 의미 검사만 실패한 뒤 재시도로 끝난 경우, 두 시도의 알려진 비용이 모두 합쳐진다."""
    seq = {"n": 0}
    rewrite = {"headline": "전 상품 10% 할인 쿠폰", "body": "10월 31일까지, 1인 1회", "cta": "자세히 보기", "disclosures": ["10월 31일까지, 1인 1회"]}

    def scripted(req):
        seq["n"] += 1
        if seq["n"] == 1:   # 첫 시도 — 검토 성공: 수정을 제안
            return {"decision": "REVISE", "confidence": 0.95, "findings": [], "rationale": "x", "rewrite": rewrite}
        if seq["n"] == 2:   # 첫 시도 — 수정안 의미 검사 실패
            return ProviderError("simulated 500", provider="openai")
        if seq["n"] == 3:   # 재시도 — 검토 성공
            return {"decision": "REVISE", "confidence": 0.95, "findings": [], "rationale": "x", "rewrite": rewrite}
        return {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "x", "rewrite": None}   # 재시도 — 수정안 검사 성공

    router = router_with(scripted)
    router.budget.cap_usd, router.budget.max_calls = 5.0, 10_000
    monkeypatch.setattr("evals.runner._wait_for_breaker", lambda *a, **k: 0.0)
    case = next(c for c in load_dataset("golden") if c.label == "REVISE" and c.difficulty == "gray")
    r = evaluate_with_retry(case, SETTINGS, router, CAT, use_llm=True, breaker_wait_s=30)
    assert not r.failed and len(r.prior_attempts) == 1 and r.prior_attempts[0]["rewrite_error"]    # 실패 단계가 수정안 검사임이 남는다
    assert r.prior_attempts[0]["cost_usd"] == pytest.approx(0.001)                              # 첫 시도의 알려진 비용
    assert r.cost_usd == pytest.approx(0.003) == pytest.approx(router.budget.spent_usd)            # 성공 응답 3회 전부 합산
    assert r.failed_requests >= 1 and r.cost_estimated
