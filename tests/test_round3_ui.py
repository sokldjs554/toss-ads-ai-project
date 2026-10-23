"""3차 점검에서 확인된 화면 상태·문구 결함의 회귀 테스트 (Streamlit AppTest, 네트워크 없음).

실제 모델 품질이 아니라 화면의 동작을 확인한다: 실제 모델 실행 경로는 통제된 가짜 제공자로 돌리고(키는 가짜), 예시 실행은 제공자를 부르면 실패하게 막아 둔다.
항목마다 '옛 동작에서는 실패하는 단언' 과 '정상 입력은 그대로 통과하는 대조 단언' 을 함께 둔다.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import llm.openai_provider as op
from core.schemas import Decision, RewriteStatus
from llm.base import Task
from observability.tracer import Trace, Tracer
from tests.fakes import TaskScriptedProvider
from ui import funding
from ui.trace_redact import AD_HIDDEN, REWRITE_HIDDEN, redact_trace_dict, redacted_copy

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
FAKE_KEY = "sk-test-FAKE-KEY-never-shown-0123456789"
FAKE_ACCESS = "test-only-server-access-0123456789abcdef"


# ───────────── 공통 도구 ─────────────
def _env(monkeypatch, *, live: bool, budget: str | None = None) -> None:
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "0" if live else "1")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY if live else "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("PREFLIGHT_LIVE_ACCESS_TOKEN", FAKE_ACCESS)
    if budget is not None:
        monkeypatch.setenv("PREFLIGHT_RUN_BUDGET_USD", budget)


def _openai(monkeypatch, handlers: dict | None = None) -> TaskScriptedProvider:
    """OpenAI 제공자를 통제된 가짜로 바꾼다 (키는 가짜라 네트워크는 어차피 쓰지 않는다). handlers 에 없는 작업은 대역(mock)이 맡는다."""
    prov = TaskScriptedProvider("openai", handlers or {})

    def complete(self, req):
        resp = prov.complete(req)
        resp.model = self.model  # Exercise the default provider's verified model/usage contract.
        if resp.parsed is not None and resp.text in ("", "{}"):
            resp.text = resp.parsed.model_dump_json(indent=2)   # 실제 제공자처럼 응답 원문(JSON)이 처리 기록에 남게 한다
        return resp

    monkeypatch.setattr(op.OpenAIProvider, "complete", complete)
    return prov


def _no_model_calls(monkeypatch) -> dict:
    seen = {"n": 0}

    def boom(self, req):
        seen["n"] += 1
        raise AssertionError("예시 실행/가려진 화면이 실제 제공자를 불렀다")

    monkeypatch.setattr(op.OpenAIProvider, "complete", boom)
    return seen


def _fresh() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["live_access_key"] = FAKE_ACCESS
    return at


def _texts(at) -> str:
    return "\n".join(str(x.value) for group in (at.markdown, at.caption, at.info, at.warning, at.error, at.success) for x in group)


def _rendered(at) -> str:
    """화면 트리 전체의 글자 (접힌 상자 안 포함). 표는 문자열로 바꾼다."""
    out: list[str] = []

    def walk(node):
        kids = getattr(node, "children", None)
        if kids is None:
            val = getattr(node, "value", None)
            if isinstance(val, str):
                out.append(val)
            elif val is not None and hasattr(val, "to_string"):
                out.append(val.to_string())
            return
        for k in kids.values():
            walk(k)

    walk(at.main)
    return "\n".join(out)


def _coupon_form(at: AppTest) -> None:
    """커머스 쿠폰 입력 — 광고안 1개, 가상 반응·교차 확인 끔 (호출 수를 단순하게)."""
    at.session_state["form_name"] = "OO몰 쿠폰"
    at.selectbox(key="form_category").set_value("ecommerce")
    at.text_area(key="form_facts").set_value("F1: 전 상품 10% 할인 쿠폰 (10월 31일까지, 1인 1회)")
    at.text_area(key="form_input").set_value("가을 쿠폰 캠페인, 월 200만원, 구매 전환")
    at.text_area(key="form_desc").set_value("")
    at.text_area(key="form_landing").set_value("")
    at.slider(key="opt_n_variants").set_value(1)
    at.toggle(key="opt_audience").set_value(False)
    at.toggle(key="opt_second").set_value(False)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]


def _copy_response(headline: str) -> dict:
    return {"variants": [{"headline": headline, "body": "전 상품 10% 할인 쿠폰, 10월 31일까지 1인 1회.", "cta": "쿠폰 받기",
                          "claims": [{"text": "전 상품 10% 할인", "fact_id": "F1"}], "disclosures": ["10월 31일까지, 1인 1회"], "tone": "x"}]}


def _review(decision="APPROVE", confidence=0.95, rewrite=None, findings=()) -> dict:
    return {"decision": decision, "confidence": confidence, "findings": [{"rule_id": r, "quote": q, "explanation": "통제된 응답"} for r, q in findings],
            "rationale": "통제된 응답", "rewrite": rewrite}


CLEAN_REWRITE = {"headline": "전 상품 10% 할인 쿠폰", "body": "10월 31일까지, 1인 1회 사용할 수 있어요.", "cta": "자세히 보기", "claims": [],
                 "disclosures": ["10월 31일까지, 1인 1회"], "tone": "x"}


def _reviewer(original_findings, rewrite, rewrite_check):
    """첫 호출(원문 검토)은 수정을 제안하고, 수정안 검사(:rewrite)와 그 뒤의 직접 재검사는 rewrite_check 를 돌려준다."""
    state = {"orig_done": False}

    def handler(req):
        if req.label.endswith(":rewrite") or state["orig_done"]:
            return rewrite_check(req) if callable(rewrite_check) else rewrite_check
        state["orig_done"] = True
        return _review("REVISE", 0.95, rewrite, original_findings)

    return handler


def _live_run(monkeypatch, handlers, *, budget: str | None = None) -> AppTest:
    """가짜 키로 '실제 모델 실행' 경로를 한 번 돌린다 (확인 체크 → 광고 초안 만들기)."""
    _env(monkeypatch, live=True, budget=budget)
    _openai(monkeypatch, handlers)
    at = _fresh().run()
    _coupon_form(at)
    at.checkbox(key="confirm_live_run").check().run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.session_state["runs"]) == 1 and at.session_state["runs"][0]["mode"] != "demo"
    return at


def test_server_funded_ui_denies_wrong_access_before_provider_call(monkeypatch):
    _env(monkeypatch, live=True)
    seen = _no_model_calls(monkeypatch)
    at = _fresh()
    at.session_state["live_access_key"] = "wrong-test-access"
    at.run()
    _coupon_form(at)
    at.checkbox(key="confirm_live_run").check().run()
    at.button(key="run_button").click().run()
    assert not at.exception and seen["n"] == 0
    assert len(at.session_state["runs"]) == 0
    assert "모델 실행 접근키가 필요합니다" in _texts(at)


def _recheck_with(at: AppTest, vid: str, rewrite) -> object:
    """수정안 문구를 그대로 '문구 고쳐서 다시 검사' 칸에 넣고 (실제 모델 재검사 확인 체크 뒤) 다시 검사한다. 시도 기록의 마지막 결과를 돌려준다."""
    at.text_input(key=f"edit_h_{vid}").set_value(rewrite.headline)
    at.text_area(key=f"edit_b_{vid}").set_value(rewrite.body)
    at.text_input(key=f"edit_c_{vid}").set_value(rewrite.cta)
    at.text_area(key=f"edit_d_{vid}").set_value("\n".join(rewrite.disclosures))
    at.run()
    at.checkbox(key=f"confirm_recheck_{vid}").check().run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    hist = [h for k, h in at.session_state["rechecks"].items() if k.endswith(f":{vid}")][-1]
    return hist.latest()


# ───────────── 3. 같은 수정안 문구는 자동 수정안 검사와 직접 재검사가 같은 결론 ─────────────
@pytest.mark.parametrize("cue", ["대출", ""])
def test_same_rewrite_text_gets_the_same_conclusion_from_the_pipeline_check_and_the_ui_recheck(monkeypatch, cue):
    """커머스 상품인데 원문 문구에 금융 단서('대출')가 있으면 그 광고안의 업종에 금융이 더해진다 — 자동 수정안 검사는 그 업종으로 수정안을 본다.
    직접 재검사도 같은 업종(실행 업종 + 원문의 단서 + 고친 문구의 단서)으로 같은 문구를 봐야 한다. 단서가 없는 원문이면 둘 다 통과한다 (대조)."""
    head = f"쿠폰 받고 {cue} 걱정 없이 쇼핑" if cue else "쿠폰 받고 걱정 없이 쇼핑"
    handlers = {Task.COPY: _copy_response(head),
                Task.REVIEW: _reviewer([("TA-OMISSION-01", head)], CLEAN_REWRITE, _review("APPROVE", 0.95))}
    at = _live_run(monkeypatch, handlers)
    run = at.session_state["runs"][0]["run"]
    rep = run.reports[0]
    assert run.categories == ["ecommerce"]                       # 실행 업종에는 금융이 없다 — 단서는 광고 문구에만 있다
    rw = rep.verdict.proposed_rewrite
    assert rw is not None
    res = _recheck_with(at, rep.variant_id, rw)
    assert res.report.ad_copy.full_text() == rw.full_text()      # 정확히 같은 문구를 검사했다
    assert not res.failed
    if cue:
        assert rep.rewrite_status == RewriteStatus.FAILED_RECHECK and rep.rewrite_recheck_decision == Decision.REVISE
        assert res.report.final_decision == rep.rewrite_recheck_decision == Decision.REVISE
        assert "FIN-DISCLOSE-LOAN-01" in {f.rule_id for f in res.report.verdict.findings} == {f.rule_id for f in rep.rewrite_recheck_findings}
    else:
        assert rep.rewrite_status == RewriteStatus.VERIFIED and rep.rewrite_recheck_decision == Decision.APPROVE
        assert res.report.final_decision == Decision.APPROVE and not res.report.verdict.findings


# ───────────── 16. 비용 확인 체크: 첫 체크가 지워지지 않고, 실행 직후에는 확인이 풀린다 ─────────────
def test_first_tick_after_an_example_run_is_kept(monkeypatch):
    """서버에 키가 있어 새 실제 모델 실행 확인 칸이 보이는 상태에서 예시 실행을 한 번 하고 나서 확인 칸을 처음 누르면, 누른 것이 그대로 남는다."""
    _env(monkeypatch, live=True)
    calls = _no_model_calls(monkeypatch)
    at = _fresh().run()
    at.button(key="example_run").click().run()
    assert not at.exception and len(at.session_state["runs"]) == 1 and at.session_state["runs"][0]["mode"] == "demo"
    assert at.checkbox(key="confirm_live_run").value is False and at.button(key="run_button").disabled is True
    at.checkbox(key="confirm_live_run").check().run()
    assert at.checkbox(key="confirm_live_run").value is True        # 옛 동작: 첫 체크가 지워져 False
    assert at.button(key="run_button").disabled is False
    assert calls["n"] == 0                                           # 확인만 했을 뿐 모델은 부르지 않았다


def test_confirmation_is_cleared_right_after_a_paid_run_and_the_next_tick_is_kept(monkeypatch):
    _env(monkeypatch, live=True)
    prov = _openai(monkeypatch)
    at = _fresh().run()
    _coupon_form(at)
    at.checkbox(key="confirm_live_run").check().run()
    assert at.button(key="run_button").disabled is False             # 대조: 확인하면 실행 버튼이 열린다
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.session_state["runs"]) == 1 and at.session_state["runs"][0]["mode"] != "demo" and prov.calls
    # 실행이 끝난 바로 그 화면에서 확인은 풀려 있다 (옛 동작: 체크가 켜진 채, 버튼도 열린 채)
    assert at.checkbox(key="confirm_live_run").value is False
    assert at.button(key="run_button").disabled is True
    n_calls = len(prov.calls)
    # 다시 확인하면 그 체크는 그대로 남고, 같은 입력이어도(3초 중복 방지만 풀어 주면) 한 번 더 실행된다
    at.checkbox(key="confirm_live_run").check().run()
    assert at.checkbox(key="confirm_live_run").value is True and at.button(key="run_button").disabled is False
    at.session_state["last_run_t"] = 0
    at.button(key="run_button").click().run()
    assert not at.exception and len(at.session_state["runs"]) == 2 and len(prov.calls) > n_calls
    assert at.checkbox(key="confirm_live_run").value is False and at.button(key="run_button").disabled is True


def test_a_run_that_was_not_started_does_not_consume_the_confirmation(monkeypatch):
    """3초 안에 같은 입력으로 다시 누르면 실행하지 않는다 — 실행하지 않았으니 확인도 그대로다 (대조)."""
    _env(monkeypatch, live=True)
    _openai(monkeypatch)
    at = _fresh().run()
    _coupon_form(at)
    at.checkbox(key="confirm_live_run").check().run()
    at.button(key="run_button").click().run()
    at.checkbox(key="confirm_live_run").check().run()
    at.session_state["last_run_t"] = time.time()                         # 방금 실행한 것으로 본다 (느린 환경에서도 같은 조건이 되게)
    at.button(key="run_button").click().run()                          # 방금 같은 입력으로 실행함 → 실행하지 않는다
    assert len(at.session_state["runs"]) == 1
    assert "같은 입력으로 방금 실행했습니다" in _texts(at)
    assert at.checkbox(key="confirm_live_run").value is True


# ───────────── 17. 재검사 결과가 같은 이유를 두 번 적지 않는다 ─────────────
def _recheck_region(at, vid: str) -> list[str]:
    """그 광고안의 ‘문구 고쳐서 다시 검사’ 상자 안에서 ‘재검사 결과’ 줄부터 보이는 이유 상자(pf-reason)·안내·통과 상자의 글자."""
    import re

    box = next(e for e in at.expander if e.label == "문구 고쳐서 다시 검사" and any(t.key == f"edit_h_{vid}" for t in e.text_input))
    md = [str(m.value) for m in box.markdown]
    start = next(i for i, m in enumerate(md) if m.startswith("재검사 결과"))
    region = [re.sub(r"<[^>]+>", "", m).strip() for m in md[start:] if "pf-reason" in m]
    region += [str(i.value) for i in box.info] + [str(s.value) for s in box.success]
    return region


def test_recheck_result_prints_each_reason_once_when_rejected(monkeypatch):
    _env(monkeypatch, live=False)
    at = _fresh().run()
    at.button(key="run_button").click().run()
    vid = at.session_state["runs"][-1]["run"].reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("원금을 보장하는 대출").run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    region = _recheck_region(at, vid)
    hits = [m for m in region if "원금 보장·손실 없음 표현" in m]
    assert len(hits) == 1, hits                                       # 옛 동작: 한 줄 요약과 구절별 줄에서 두 번
    assert "원금을 보장" in hits[0] and "확인이 필요한 구절" in "\n".join(str(m.value) for m in at.markdown)
    assert "게재 불가" in _texts(at)                                   # 대조: 판정 배지와 이유는 여전히 보인다


def test_recheck_result_prints_the_human_review_reason_once(monkeypatch):
    _env(monkeypatch, live=False)
    at = _fresh().run()
    at.slider(key="opt_tau").set_value(0.99).run()
    at.button(key="run_button").click().run()
    vid = at.session_state["runs"][-1]["run"].reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("조건을 확인하고 신청하세요").run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    res = [h for k, h in at.session_state["rechecks"].items() if k.endswith(f":{vid}")][-1].latest()
    assert res.report.final_decision == Decision.HUMAN_REVIEW and not res.failed
    sentence = "검토 모델의 확신이 자동 판정 기준보다 낮았습니다."
    region = "\n".join(_recheck_region(at, vid))
    assert region.count(sentence) == 1, region                        # 옛 동작: 이유 상자와 안내 상자에서 두 번
    assert "재검사 결과도 담당자 확인이 필요합니다" in region             # 대조: 안내는 그대로 있고 이유가 그 안에 들어 있다


def test_recheck_result_passed_text_says_it_once(monkeypatch):
    """대조: 통과한 문구는 통과 안내 한 줄만 보이고 같은 뜻의 이유 줄을 따로 쓰지 않는다."""
    _env(monkeypatch, live=False)
    at = _fresh().run()
    at.button(key="run_button").click().run()
    vid = at.session_state["runs"][-1]["run"].reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("조건 확인 후 신청").run()
    at.button(key=f"recheck_{vid}").click().run()
    res = [h for k, h in at.session_state["rechecks"].items() if k.endswith(f":{vid}")][-1].latest()
    if res.report.final_decision != Decision.APPROVE:
        pytest.skip("예시 실행 대역이 이 문구를 통과시키지 않았다")
    region = "\n".join(_recheck_region(at, vid))
    assert region.count("통과했습니다") == 1, region


# ───────────── 18. 처리 기록 화면은 통과하지 못한 수정안 문구를 보여 주지 않는다 ─────────────
MARK = "수정안표식제목"


def _marked_rewrite() -> dict:
    return dict(CLEAN_REWRITE, headline=MARK)


def _go_quality(at):
    at.session_state["nav_request"] = "품질 확인"
    return at.run()


def _pick_span(at, name: str) -> None:
    sel = next(s for s in at.selectbox if s.label == "단계 선택")
    sel.set_value(next(o for o in sel.options if f"{name} · llm" in o)).run()


def test_trace_viewer_hides_the_text_of_a_rewrite_that_failed_recheck(monkeypatch):
    """모델이 제안한 수정안이 재검사에서 걸리면 그 문구는 어느 화면에도 나오지 않는다 — 처리 기록의 모델 응답·수정안 검사 프롬프트도 마찬가지."""
    handlers = {Task.COPY: _copy_response("쿠폰 받고 걱정 없이 쇼핑"),
                Task.REVIEW: _reviewer([("TA-OMISSION-01", "쿠폰 받고 걱정 없이 쇼핑")], _marked_rewrite(), _review("REVISE", 0.95, None, [("TA-OMISSION-01", "10월 31일까지")]))}
    at = _live_run(monkeypatch, handlers)
    run = at.session_state["runs"][0]["run"]
    rep = run.reports[0]
    assert rep.rewrite_status == RewriteStatus.FAILED_RECHECK and rep.verified_rewrite is None
    stored = [t for t in at.session_state["tracer"].traces if t.trace_id == run.trace_id][0]
    assert MARK in stored.to_json()                                 # 저장된 기록 원본에는 그대로 있다 (화면에서만 가린다)
    assert MARK not in _rendered(at)                                # 광고 작업 화면에는 처음부터 없다
    _go_quality(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    _pick_span(at, f"review:{rep.variant_id}")
    shown = _rendered(at)
    assert MARK not in shown, "검토 호출의 응답 원문에 통과하지 못한 수정안 문구가 보인다"
    assert REWRITE_HIDDEN in shown and '"decision": "REVISE"' in shown   # 대조: 응답의 나머지(판정·이유)는 그대로 보인다
    _pick_span(at, f"review:{rep.verdict.proposed_rewrite.variant_id}:rewrite")
    shown = _rendered(at)
    assert MARK not in shown, "수정안 검사 호출의 프롬프트에 통과하지 못한 수정안 문구가 보인다"
    assert AD_HIDDEN in shown and "[광고주 사실 목록]" in shown              # 대조: 프롬프트의 나머지(사실 목록·검사 결과)는 그대로 보인다
    assert MARK in stored.to_json()                                  # 화면을 그린 뒤에도 원본은 그대로


def test_trace_viewer_still_shows_a_rewrite_that_passed_recheck(monkeypatch):
    """대조: 재검사를 통과해 ‘수정안 확인 완료’ 로 이미 화면에 나오는 수정안은 처리 기록에서도 그대로 보인다."""
    handlers = {Task.COPY: _copy_response("쿠폰 받고 걱정 없이 쇼핑"),
                Task.REVIEW: _reviewer([("TA-OMISSION-01", "쿠폰 받고 걱정 없이 쇼핑")], _marked_rewrite(), _review("APPROVE", 0.95))}
    at = _live_run(monkeypatch, handlers)
    rep = at.session_state["runs"][0]["run"].reports[0]
    assert rep.rewrite_status == RewriteStatus.VERIFIED and rep.verified_rewrite is not None and rep.verified_rewrite.headline == MARK
    assert MARK in _rendered(at)                                    # 광고 작업 화면에 수정안 확인 완료로 보인다
    _go_quality(at)
    _pick_span(at, f"review:{rep.variant_id}")
    shown = _rendered(at)
    assert MARK in shown and REWRITE_HIDDEN not in shown


def _trace_dict(status, response_rewrite=True, adjudicate=True) -> dict:
    llm = {"name": "review:var_a", "kind": "llm", "trace_id": "t", "span_id": "s3", "parent_id": "s2", "start_ms": 1, "end_ms": 2, "status": "ok", "events": [], "children": [],
           "attributes": {"prompt_user": "[소재]\n헤드라인: 원문\n\n[광고주 사실 목록]\n- F1",
                          "response_text": json.dumps({"decision": "REVISE", "rewrite": {"headline": MARK} if response_rewrite else None}, ensure_ascii=False)}}
    check = {"name": "review:var_b:rewrite", "kind": "llm", "trace_id": "t", "span_id": "s5", "parent_id": "s4", "start_ms": 1, "end_ms": 2, "status": "ok", "events": [], "children": [],
             "attributes": {"prompt_user": f"[소재]\n헤드라인: {MARK}\n본문: x\n\n[광고주 사실 목록]\n- F1", "response_text": json.dumps({"decision": "APPROVE", "rewrite": None})}}
    kids = [{"name": "reviewer", "kind": "stage", "trace_id": "t", "span_id": "s2", "parent_id": "s1", "start_ms": 1, "end_ms": 2, "status": "ok", "attributes": {}, "events": [], "children": [llm]},
            {"name": "rewrite_recheck", "kind": "guardrail", "trace_id": "t", "span_id": "s4", "parent_id": "s1", "start_ms": 1, "end_ms": 2, "status": "ok", "attributes": {}, "events": [], "children": [check]}]
    if adjudicate:
        kids.append({"name": "adjudicate", "kind": "guardrail", "trace_id": "t", "span_id": "s6", "parent_id": "s1", "start_ms": 1, "end_ms": 2, "status": "ok",
                     "attributes": ({"rewrite_status": status} if status is not None else {}), "events": [], "children": []})
    root = {"name": "variant:var_a", "kind": "stage", "trace_id": "t", "span_id": "s1", "parent_id": None, "start_ms": 0, "end_ms": 3, "status": "ok", "attributes": {}, "events": [], "children": kids}
    return {"name": "preflight", "trace_id": "t", "metadata": {}, "root": root}


def _responses(d: dict) -> dict:
    return {s["name"]: s["attributes"] for s in _walk(d["root"]) if s["kind"] == "llm"}


def _walk(node):
    yield node
    for c in node["children"]:
        yield from _walk(c)


@pytest.mark.parametrize("status,adjudicate", [("failed_recheck", True), ("unchecked", True), ("none", True), (None, True), ("", False)])
def test_redaction_hides_the_rewrite_unless_its_status_is_verified(status, adjudicate):
    """수정안 상태가 verified 가 아니면 — 상태를 알 수 없는 과거 기록(상태 없음·판정 단계 없음)을 포함해 — 응답의 rewrite 와 수정안 검사 프롬프트를 가린다."""
    d = _trace_dict(status, adjudicate=adjudicate)
    before = copy.deepcopy(d)
    out = redact_trace_dict(d)
    assert d == before                                              # 원본은 건드리지 않는다
    a = _responses(out)
    assert json.loads(a["review:var_a"]["response_text"])["rewrite"] == REWRITE_HIDDEN
    assert json.loads(a["review:var_a"]["response_text"])["decision"] == "REVISE"
    assert MARK not in a["review:var_b:rewrite"]["prompt_user"] and AD_HIDDEN in a["review:var_b:rewrite"]["prompt_user"]
    assert "[광고주 사실 목록]" in a["review:var_b:rewrite"]["prompt_user"]
    assert MARK not in json.dumps(out, ensure_ascii=False)


def test_redaction_keeps_a_verified_rewrite_and_a_response_without_one():
    out = redact_trace_dict(_trace_dict("verified"))
    a = _responses(out)
    assert json.loads(a["review:var_a"]["response_text"])["rewrite"] == {"headline": MARK}      # 확인 완료 수정안은 화면에 이미 나온다
    assert MARK in a["review:var_b:rewrite"]["prompt_user"]                                      # 확인 완료 수정안을 검사한 요청도 화면에 나오는 문구 그대로
    out2 = redact_trace_dict(_trace_dict("failed_recheck", response_rewrite=False))
    assert json.loads(_responses(out2)["review:var_a"]["response_text"])["rewrite"] is None      # 수정안이 없는 응답은 그대로


def test_redaction_hides_a_cross_check_rewrite_even_when_the_first_reviews_rewrite_is_verified():
    """교차 확인 호출이 낸 수정안은 어디에도 채택되지 않는다 — 1차 검토의 수정안이 확인 완료여도 그 응답의 rewrite 는 가린다."""
    d = _trace_dict("verified")
    second = {"name": "second_opinion:var_a", "kind": "llm", "trace_id": "t", "span_id": "s7", "parent_id": "s1", "start_ms": 1, "end_ms": 2, "status": "ok", "events": [], "children": [],
              "attributes": {"task": "second_opinion", "response_text": json.dumps({"decision": "REVISE", "rewrite": {"headline": "교차확인표식"}}, ensure_ascii=False)}}
    d["root"]["children"].append({"name": "second_opinion", "kind": "stage", "trace_id": "t", "span_id": "s8", "parent_id": "s1", "start_ms": 1, "end_ms": 2, "status": "ok",
                                  "attributes": {}, "events": [], "children": [second]})
    out = _responses(redact_trace_dict(d))
    assert json.loads(out["second_opinion:var_a"]["response_text"])["rewrite"] == REWRITE_HIDDEN
    assert json.loads(out["review:var_a"]["response_text"])["rewrite"] == {"headline": MARK}      # 대조: 1차 검토의 확인 완료 수정안은 그대로


def test_redaction_hides_an_unreadable_response_that_may_hold_a_rewrite():
    d = _trace_dict("failed_recheck")
    d["root"]["children"][0]["children"][0]["attributes"]["response_text"] = '{"decision": "REVISE", "rewrite": {"headline": "' + MARK + '", "bo'   # 잘린 응답
    out = redact_trace_dict(d)
    assert MARK not in json.dumps(out, ensure_ascii=False)
    d2 = _trace_dict("failed_recheck")
    d2["root"]["children"][0]["children"][0]["attributes"]["response_text"] = "죄송합니다. 형식을 지킬 수 없습니다."   # 수정안이 든 흔적이 없는 응답은 그대로
    assert _responses(redact_trace_dict(d2))["review:var_a"]["response_text"] == "죄송합니다. 형식을 지킬 수 없습니다."


def test_stored_sample_traces_are_redacted_for_display_and_left_untouched():
    stored = sorted((ROOT / "traces").glob("sample_*.json"))
    assert stored
    hidden = 0
    for p in stored:
        d = json.loads(p.read_text(encoding="utf-8"))
        before = copy.deepcopy(d)
        out = redact_trace_dict(d)
        assert d == before
        for variant in (s for s in _walk(out["root"]) if s["name"].startswith("variant:")):
            status = next((c["attributes"].get("rewrite_status") for c in variant["children"] if c["name"] == "adjudicate"), None)
            for s in _walk(variant):
                if s["kind"] != "llm" or not s["attributes"].get("response_text"):
                    continue
                try:
                    rewrite = json.loads(s["attributes"]["response_text"]).get("rewrite")
                except ValueError:
                    continue
                if status != "verified":
                    assert not isinstance(rewrite, dict), (p.name, s["name"])      # 통과하지 못한 수정안은 객체로 남지 않는다
                    hidden += rewrite == REWRITE_HIDDEN
        assert redacted_copy(Trace.from_dict(d)).trace_id == d["trace_id"]
    assert hidden, "저장된 기록 중에 통과하지 못한 수정안이 하나도 없어 이 확인이 비어 있다"


# ───────────── 19. 우회 표현 검사 결과 선택: 이 세션의 결과와 서버에 저장된 CLI 결과를 섞지 않는다 ─────────────
def _fake_reports(monkeypatch, names: dict):
    reports = {n: {"mode": "demo", "attacks": [], "by_strategy": {}, "cost_usd": 0.0, "generated_at": g} for n, g in names.items()}
    monkeypatch.setattr("ui.redteam_view.load_report", lambda name: reports.get(name))
    return reports


def test_redteam_selector_does_not_call_a_cli_file_this_sessions_result(monkeypatch):
    _env(monkeypatch, live=False)
    _fake_reports(monkeypatch, {"baseline_redteam_partial.json": "2026-10-17T15:21:01+00:00", "baseline_redteam_demo.json": "2026-10-17T15:17:48+00:00",
                                "redteam_latest.json": "2026-10-17T15:17:48+00:00"})
    at = _fresh().run()
    _go_quality(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    options = list(at.selectbox(key="redteam_pick").options)
    assert "이 세션에서 실행한 결과" not in options                                  # 옛 동작: 디스크의 CLI 파일이 이 이름으로 나옴
    cli = [o for o in options if o.startswith("이 서버에 저장된 마지막 CLI 실행 결과")]
    assert cli == ["이 서버에 저장된 마지막 CLI 실행 결과 (2026-10-17)"]           # 날짜가 이름에 들어 있다
    assert "저장된 실제 모델 결과 (5차, gpt-4o)" in options and "예시 실행 (실제 AI 호출 없음)" in options    # 대조: 나머지 둘은 그대로
    assert len(set(options)) == 3
    at.selectbox(key="redteam_pick").set_value(cli[0]).run()
    assert not at.exception and any("심사한 변형" in str(m.value) for m in at.markdown)


def test_redteam_selector_without_a_cli_file_offers_only_the_stored_results(monkeypatch):
    _env(monkeypatch, live=False)
    _fake_reports(monkeypatch, {"baseline_redteam_partial.json": "2026-10-17T15:21:01+00:00", "baseline_redteam_demo.json": "2026-10-17T15:17:48+00:00"})
    at = _fresh().run()
    _go_quality(at)
    options = list(at.selectbox(key="redteam_pick").options)
    assert options == ["저장된 실제 모델 결과 (5차, gpt-4o)", "예시 실행 (실제 AI 호출 없음)"]


def test_redteam_session_result_appears_only_after_this_session_ran_it(monkeypatch):
    """화면에서 우회 표현 검사를 돌리면 그 결과는 세션에만 남고 ‘이 세션에서 실행한 결과’ 로 따로 나온다. 명령줄 파일(redteam_latest.json)은 쓰지 않는다."""
    _env(monkeypatch, live=False)
    reports = _fake_reports(monkeypatch, {"baseline_redteam_demo.json": "2026-10-17T15:17:48+00:00", "redteam_latest.json": "2026-10-17T15:17:48+00:00"})
    written = []
    ran = {"n": 0}

    def fake_run_redteam(settings, router=None, **kw):
        ran["n"] += 1
        return {"mode": "demo", "seeds": 1, "attacks": [], "by_strategy": {}, "cost_usd": 0.0, "generated_at": "2026-10-18T09:00:00+00:00"}

    monkeypatch.setattr("evals.redteam.run_redteam", fake_run_redteam)
    monkeypatch.setattr("evals.redteam.harvest", lambda rep: [])
    monkeypatch.setattr("evals.runner.save_report", lambda rep, name="latest.json": written.append(name))

    def app():
        import streamlit as st

        from ui import redteam_view

        redteam_view.render(None, lambda settings: None)

    at = AppTest.from_function(app, default_timeout=60).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "이 세션에서 실행한 결과" not in at.selectbox(key="redteam_pick").options
    at.button[0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert ran["n"] == 1 and written == []                                           # 명령줄 파일을 덮어쓰지 않는다
    options = list(at.selectbox(key="redteam_pick").options)
    assert "이 세션에서 실행한 결과" in options and any(o.startswith("이 서버에 저장된 마지막 CLI 실행 결과") for o in options)
    assert at.selectbox(key="redteam_pick").value == "이 세션에서 실행한 결과"             # 방금 돌린 결과가 먼저 보인다
    assert reports["redteam_latest.json"]["generated_at"].startswith("2026-10-17")      # 파일 쪽 이름·내용은 그대로


# ───────────── 20. 처리 기록이 지워졌는데 ‘볼 수 있습니다’ 라고 하지 않는다 ─────────────
def test_summary_caption_says_so_when_the_runs_trace_was_evicted(monkeypatch):
    _env(monkeypatch, live=False)
    at = _fresh().run()
    at.session_state["tracer"] = Tracer(max_traces=3)                                    # 앱의 보관 개수(30)와 같은 동작을 작게 줄여 확인
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    run = at.session_state["runs"][-1]["run"]
    vid = run.reports[0].variant_id

    def caption():
        return [str(c.value) for c in at.caption if run.trace_id in str(c.value)]

    assert caption() and "단계별로 볼 수 있습니다" in caption()[0] and "지워졌습니다" not in caption()[0]    # 대조: 아직 남아 있을 때는 그대로
    _go_quality(at)
    assert any(run.trace_id in o for o in next(s for s in at.selectbox if s.label == "처리 기록 선택").options)
    at.session_state["nav_request"] = "광고 작업"
    at.run()
    for i in range(3):                                                                   # 다시 검사도 기록 하나 — 보관 개수를 넘기면 오래된 것부터 지워진다
        at.text_input(key=f"edit_h_{vid}").set_value(f"조건 확인 후 신청 {i}").run()
        at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert run.trace_id not in [t.trace_id for t in at.session_state["tracer"].traces]
    cap = caption()
    assert cap and "단계별로 볼 수 있습니다" not in cap[0]                                   # 옛 동작: 지워진 기록을 볼 수 있다고 안내
    assert "지워졌습니다" in cap[0] and "최근 3개" in cap[0]
    _go_quality(at)
    assert not any(run.trace_id in o for o in next(s for s in at.selectbox if s.label == "처리 기록 선택").options)    # 안내와 실제가 같다


# ───────────── 21. 서버 비용 상한의 가장자리 값 ─────────────
def test_server_cap_reads_zero_as_no_paid_run_and_keeps_the_default_only_for_a_missing_setting():
    assert funding.server_cap({"run_budget_usd": 0}) == 0.0 and funding.server_cap({"run_budget_usd": 0.0}) == 0.0     # 옛 동작: 0.5
    assert funding.server_cap({"run_budget_usd": -1}) == 0.0 and funding.server_cap({"run_budget_usd": float("nan")}) == 0.0
    assert funding.server_cap({"run_budget_usd": "abc"}) == 0.0
    assert funding.server_cap({"run_budget_usd": 0.005}) == 0.005 and funding.server_cap({"run_budget_usd": 0.3}) == 0.3     # 대조
    assert funding.server_cap({}) == 0.5 and funding.server_cap({"run_budget_usd": None}) == 0.5
    assert funding.usd_text(0.3) == "$0.30" and funding.usd_text(0.005) == "$0.0050" and funding.usd_text(0) == "$0.00"


def test_zero_server_cap_blocks_paid_runs_and_the_screen_still_works(monkeypatch):
    _env(monkeypatch, live=True, budget="0")
    calls = _no_model_calls(monkeypatch)
    at = _fresh().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    err = "\n".join(str(e.value) for e in at.error)
    assert "서버 운영자가 실행당 비용 상한을 $0 으로 정해 두어" in err and "$0.50" not in _texts(at)          # 옛 동작: '$0.50 에서 멈춥니다'
    assert at.button(key="run_button").disabled is True
    assert not [c for c in at.checkbox if c.key == "confirm_live_run"]                              # 확인할 것이 없으니 확인 칸도 없다
    budget = at.number_input(key="opt_budget")
    assert budget.value == 0 and budget.disabled is True
    # 예시 실행은 그대로 된다 (모델을 부르지 않는다)
    at.button(key="example_run").click().run()
    assert not at.exception and len(at.session_state["runs"]) == 1 and at.session_state["runs"][0]["mode"] == "demo"
    assert not at.session_state["runs"][0]["run"].halted and at.session_state["runs"][0]["run"].reports      # 광고안이 실제로 나온다 (상한 $0 에서 FM-20 으로 0개로 끝나지 않는다)
    assert at.button(key="run_button").disabled is True and calls["n"] == 0
    # 직접 넣은 키(두 제공자 모두)는 서버 상한과 무관하게 쓸 수 있다
    at.text_input(key="user_openai_key").set_value("sk-user-own-key-0123456789")
    at.text_input(key="user_anthropic_key").set_value("sk-ant-user-own-key-0123456789").run()
    assert not at.exception and at.number_input(key="opt_budget").disabled is False and at.number_input(key="opt_budget").value >= 0.01
    assert any(c.key == "confirm_live_run" for c in at.checkbox)


def test_server_cap_below_one_cent_does_not_crash_the_workbench(monkeypatch):
    _env(monkeypatch, live=True, budget="0.005")
    _openai(monkeypatch)
    at = _fresh().run()
    assert not at.exception, [str(e.value) for e in at.exception]                                  # 옛 동작: 최솟값 0.01 > 최댓값 0.005 예외
    budget = at.number_input(key="opt_budget")
    assert budget.value == pytest.approx(0.005) and budget.max == pytest.approx(0.005) and budget.min == pytest.approx(0.005)
    assert "$0.0050 에서 멈춥니다" in _texts(at)                                                   # 0.01 로 부풀려 적지 않는다
    at.checkbox(key="confirm_live_run").check().run()
    assert at.button(key="run_button").disabled is False
    at.button(key="example_run").click().run()                                                     # 예시 실행도 문제없다
    assert not at.exception
    assert at.session_state["settings_overrides"]["run_budget_usd"] == pytest.approx(0.005)


def test_normal_server_cap_is_unchanged(monkeypatch):
    """대조: 평범한 상한($0.30)은 예전과 같다 — 확인 칸이 있고, 입력 칸은 0.01~0.30."""
    _env(monkeypatch, live=True, budget="0.30")
    _openai(monkeypatch)
    at = _fresh().run()
    assert not at.exception
    budget = at.number_input(key="opt_budget")
    assert budget.min == pytest.approx(0.01) and budget.max == pytest.approx(0.30) and budget.disabled is False
    assert "$0.30 에서 멈춥니다" in _texts(at)
    assert at.button(key="run_button").disabled is True
    at.checkbox(key="confirm_live_run").check().run()
    assert at.button(key="run_button").disabled is False


def test_zero_server_cap_disables_the_live_evaluation_run(monkeypatch):
    _env(monkeypatch, live=True, budget="0")
    seen = {"n": 0}
    monkeypatch.setattr("ui.quality.run_eval", lambda *a, **k: seen.__setitem__("n", seen["n"] + 1))
    at = _fresh().run()
    _go_quality(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    err = "\n".join(str(e.value) for e in at.error)
    assert "서버 키로는 실제 모델 평가를 돌릴 수 없습니다" in err
    assert at.button(key="eval_run").disabled is True and not [c for c in at.checkbox if c.key == "eval_confirm"]
    assert seen["n"] == 0
