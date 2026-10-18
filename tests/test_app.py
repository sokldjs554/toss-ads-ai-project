"""화면 흐름 테스트 (Streamlit AppTest, 네트워크 없음, 예시 실행).

- 세 화면이 예외 없이 뜬다
- 기본 예시 실행 → 결과·수정 제안·다음 행동이 보인다
- 수정한 문구 다시 검사가 '현재 편집한 문구' 를 쓴다
- 서버 환경변수의 API 키가 입력창 기본값·세션 상태·처리 기록 어디에도 나타나지 않는다
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

FAKE_KEY = "sk-test-FAKE-KEY-never-shown-0123456789"
PRESET_NAME = {"commerce_landing": __import__("ui.presets", fromlist=["PRESETS"]).PRESETS["commerce_landing"]["product_name"]}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180)
    return at


def _all_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info] + [w.value for w in at.warning] + [e.value for e in at.error]
    return "\n".join(str(p) for p in parts)


def _visible_text(at: AppTest) -> str:
    """접힌 ‘상세 기록’ 같은 expander 안을 뺀, 기본 화면에 바로 보이는 글자."""
    out: list[str] = []

    def walk(node):
        kids = getattr(node, "children", None)
        if kids is None:
            val = getattr(node, "value", None)
            if isinstance(val, str):
                out.append(val)
            return
        if getattr(node, "type", "") == "expander":
            out.append(getattr(node, "label", ""))   # 제목만 보인다
            return
        for k in kids.values():
            walk(k)

    walk(at.main)
    return "\n".join(out)


def _latest_recheck(at: AppTest, vid: str):
    """세션에 쌓인 재검사 시도 기록(RecheckHistory) 중 이 광고안의 가장 최근 시도."""
    hists = [h for k, h in at.session_state["rechecks"].items() if k.endswith(f":{vid}")]
    assert hists, at.session_state["rechecks"].keys()
    return hists[-1].latest()


def test_three_views_render_without_exceptions(app):
    at = app.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "광고 검사" in _all_text(at)
    for view in ("검사 사례", "품질 확인"):
        at.session_state["nav_request"] = view
        at.run()
        assert not at.exception, (view, [str(e.value) for e in at.exception])
    assert "검사 사례" in _all_text(at) or "품질 확인" in _all_text(at)


def test_default_example_runs_end_to_end(app):
    at = app.run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.session_state["runs"]) == 1
    text = _all_text(at)
    for needle in ("검사 결과", "광고안 1", "수정할 문구", "수정 내역", "다음 작업", "예시 실행 · 실제 AI 호출 없음"):
        assert needle in text, needle
    assert any(e.label.startswith("상세 기록") for e in at.expander)          # 세부 내용은 ‘상세 기록’ 안으로
    # 전문 용어가 기본 화면에 없다 (접힌 상세 기록 안은 제외)
    visible = _visible_text(at)
    assert "광고안 1" in visible and "수정할 문구" in visible
    for jargon in ("오탐", "미탐", "fact_id", "finding", "Finding", "Grounding", "Guardrail", "HUMAN_REVIEW", "APPROVE", "REVISE", "REJECT", "GRND-", "TA-", "가장 중요한 이유"):
        assert jargon not in visible, jargon


def test_result_sections_come_in_the_promised_order_and_reasons_are_not_repeated(app):
    """결과 → 수정할 문구 → 수정 내역(수정안) → 다음 작업(재검사) → 상세 기록 순서. 같은 이유 문장이 한 광고안 안에서 두 번 나오지 않는다."""
    at = app.run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    run = at.session_state["runs"][0]["run"]
    md = [str(m.value) for m in at.markdown]
    for i, rep in enumerate(run.reports, start=1):
        start = next(k for k, m in enumerate(md) if m.strip() == f"### 광고안 {i}")
        end = next((k for k, m in enumerate(md) if k > start and m.strip().startswith("### 광고안 ")), len(md))
        block = md[start:end]
        if rep.final_decision.value == "APPROVE" and not [f for f in rep.verdict.findings if f.verified_quote]:
            assert any(m.strip() == "**광고 원문**" for m in block) and any(m.strip() == "**다음 작업**" for m in block)   # 고칠 것이 없으면 빈 섹션 대신 원문만
            continue
        idx = {name: next((k for k, m in enumerate(block) if m.strip() == f"**{name}**"), -1) for name in ("수정할 문구", "수정 내역", "다음 작업")}
        assert all(v >= 0 for v in idx.values()), (i, idx)
        assert idx["수정할 문구"] < idx["수정 내역"] < idx["다음 작업"], (i, idx)
        reasons = [m for m in block if "pf-reason" in m]
        assert len(reasons) == len(set(reasons)), (i, "이유가 중복됨")
        joined = "\n".join(block)
        assert joined.count("검사 기준상 쓸 수 없는 표현입니다") <= 1


def test_one_click_example_runs_as_example_even_when_server_has_a_key(monkeypatch):
    """‘예시로 바로 실행’ 은 서버에 API 키가 있어도 AI 호출 없는 예시 실행이다. 새 실제 모델 실행은 확인 체크 뒤에만 가능하다."""
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "0")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    called = {"live": 0}

    import llm.openai_provider as op

    def no_network(self, *a, **k):
        called["live"] += 1
        raise AssertionError("예시 실행이 실제 제공자를 불렀다")

    monkeypatch.setattr(op.OpenAIProvider, "complete", no_network)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180).run()
    assert "새 실제 모델 실행입니다" in _all_text(at) and at.button(key="run_button").disabled is True   # 확인 전에는 실제 모델 실행 불가
    at.selectbox(key="preset_pick").set_value("commerce_landing").run()
    at.button(key="example_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    entry = at.session_state["runs"][-1]
    assert entry["mode"] == "demo" and entry["run"].mode == "demo" and called["live"] == 0
    assert at.session_state["form_name"] == PRESET_NAME["commerce_landing"]
    assert "예시 실행 · 실제 AI 호출 없음" in _all_text(at)
    # 그 광고안의 재검사도 예시 실행이다
    vid = entry["run"].reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("조건을 확인하고 신청하세요").run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception and called["live"] == 0
    # 확인 체크를 하면 실제 모델 실행 버튼이 열린다 (이 테스트는 누르지 않는다)
    at.checkbox(key="confirm_live_run").check().run()
    assert at.button(key="run_button").disabled is False


def test_recheck_uses_currently_edited_text(app):
    at = app.run()
    at.button(key="run_button").click().run()
    run = at.session_state["runs"][0]["run"]
    vid = run.reports[0].variant_id
    at.text_input(key=f"edit_h_{vid}").set_value("원금을 보장하는 대출").run()  # 게재 불가 표현으로 편집
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    rc = _latest_recheck(at, vid)
    assert rc.report.ad_copy.headline == "원금을 보장하는 대출"
    assert rc.report.final_decision.value == "REJECT"
    assert "재검사 결과" in _all_text(at) and "게재 불가" in _all_text(at)
    # 문구를 다시 바꾸면 '이전 문구에 대한 결과' 안내가 붙는다
    at.text_input(key=f"edit_h_{vid}").set_value("조건 확인 후 신청").run()
    assert "이전에 검사한 문구" in _all_text(at)


def test_server_key_never_exposed(app):
    at = app.run()
    at.button(key="run_button").click().run()
    assert not at.exception
    # 1) 입력창 기본값
    for ti in at.text_input:
        assert FAKE_KEY not in str(ti.value or ""), ti.label
    # 2) 화면 텍스트
    assert FAKE_KEY not in _all_text(at)
    # 3) 세션 상태 (서버 키는 보관하지 않는다 — 사용자가 직접 넣은 키만, 여기선 빈 값)
    assert at.session_state["user_keys"] == ("", "")
    assert FAKE_KEY not in json.dumps(at.session_state["settings_overrides"], ensure_ascii=False, default=str)
    # 4) 처리 기록(JSON 내보내기)
    tracer = at.session_state["tracer"]
    for t in tracer.traces:
        assert FAKE_KEY not in t.to_json()
    # 5) 담당자 확인 요청문 등 내려받기 텍스트
    from guardrails.policy import get_catalog
    from ui.components import human_review_request_text

    for rep in at.session_state["runs"][0]["run"].reports:
        assert FAKE_KEY not in human_review_request_text(rep, at.session_state["runs"][0]["run"], get_catalog())


def test_form_survives_switching_views(app):
    at = app.run()
    at.text_input(key="form_name").set_value("내가 직접 쓴 상품명").run()
    at.button(key="run_button").click().run()
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    at.session_state["nav_request"] = "광고 작업"
    at.run()
    assert not at.exception
    assert at.text_input(key="form_name").value == "내가 직접 쓴 상품명"
    assert "이전 입력" not in _all_text(at)  # 입력을 바꾸지 않았으니 '이전 입력' 경고가 없다


def test_recheck_does_not_treat_own_text_as_rejected_duplicate(app):
    """게재 불가 광고를 조금만 고쳐 다시 검사할 때, 방금 반려된 '자기 자신' 과 비슷하다는 이유(TA-DUP-01)로 또 걸리면 안 된다."""
    from guardrails.duplicate import similarity

    at = app.run()
    at.session_state["pending_load"] = "commerce_landing"  # 사례 화면의 '이 사례로 광고 만들기' 와 같은 경로
    at.run()
    at.button(key="run_button").click().run()
    run = at.session_state["runs"][-1]["run"]
    rejected = [r for r in run.reports if r.final_decision.value == "REJECT"]
    assert rejected, "커머스 예시의 세 번째 광고안은 게재 불가여야 한다"
    rep = rejected[0]
    vid = rep.variant_id
    assert rep.ad_copy.full_text() in at.session_state["rejected_history"]
    new_head = rep.ad_copy.headline.replace("업계 최저 ", "")  # 최소 수정 — 원문과 여전히 매우 비슷하다
    assert similarity(new_head + " " + rep.ad_copy.body, rep.ad_copy.full_text()) >= 0.6
    at.text_input(key=f"edit_h_{vid}").set_value(new_head).run()
    at.button(key=f"recheck_{vid}").click().run()
    assert not at.exception
    res = _latest_recheck(at, vid)
    report = res.report
    hits = [f.rule_id for f in report.verdict.findings] if report.verdict else []
    hits += [f.rule_id for f in getattr(res, "det", None).findings] if getattr(res, "det", None) else []
    assert "TA-DUP-01" not in hits, hits


def test_autorun_is_not_persisted_in_url(app):
    at = app.run()
    at.session_state["nav_request"] = "검사 사례"
    at.run()
    at.button(key="run_loan_ok").click().run()
    assert "autorun" not in dict(at.query_params)
    assert len(at.session_state["runs"]) == 1


def test_link_autorun_never_calls_a_paid_model(monkeypatch):
    """?autorun=1 링크와 검사 사례의 ‘예시로 바로 실행’ 은 서버에 키가 있어도 AI 호출 없는 예시 실행이다 — 링크를 여는 것만으로 비용이 나가지 않는다."""
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "0")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    import llm.openai_provider as op

    def no_network(self, *a, **k):
        raise AssertionError("링크 실행이 실제 제공자를 불렀다")

    monkeypatch.setattr(op.OpenAIProvider, "complete", no_network)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180)
    at.query_params["preset"] = "malicious"
    at.query_params["autorun"] = "1"
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["runs"] and at.session_state["runs"][-1]["mode"] == "demo"
    # 검사 사례 → ‘예시로 바로 실행’ 도 같다
    at2 = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180).run()
    at2.session_state["nav_request"] = "검사 사례"
    at2.run()
    at2.button(key="run_loan_ok").click().run()
    assert not at2.exception, [str(e.value) for e in at2.exception]
    assert at2.session_state["runs"][-1]["mode"] == "demo"


def test_comparison_section_is_view_only_and_labels_the_controlled_run(app, monkeypatch):
    """품질 확인의 ‘방식 비교’: 실제 모델 결과가 없으면 승인 대기와 호출 수·예상 비용을 알리고, 대역으로 돌린 결과는 모델 품질이 아니라고 적는다. 보는 것만으로 모델을 부르지 않는다."""
    def boom(*a, **k):
        raise AssertionError("방식 비교를 보는 것만으로 모델/평가가 실행됐다")

    monkeypatch.setattr("llm.router.Router.complete", boom)
    monkeypatch.setattr("evals.compare.run_compare", boom)
    at = app.run()
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    t = _all_text(at)
    assert "방식 비교" in t and "실제 모델로 돌린 비교 결과는 아직 없습니다" in t and "모델 호출 약" in t
    assert any("하네스 검증용" in e.label and "모델 품질 아님" in e.label for e in at.expander)
    assert "모델 품질이 아니다" in t or any("모델 품질이 아니다" in str(w.value) for w in at.warning)


# ───────────── 내부 재점검(self_review)에서 확인된 화면 결함 ─────────────
def test_warning_events_that_are_not_failures_do_not_make_a_run_partial(app):
    """가상 사용자 반응이 서로 비슷했다는 경고(FM-15)나 계획을 코드가 고쳤다는 기록(FM-02)은 단계 실패가 아니다 — '일부 단계 실패' 배너를 띄우지 않는다."""
    from ui.workbench import run_state

    at = app.run()
    at.selectbox(key="preset_pick").set_value("commerce_landing").run()
    at.button(key="example_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    entry = at.session_state["runs"][-1]
    run = entry["run"]
    assert run.plan is not None and all(r.audience is not None for r in run.reports)
    assert {f.code for f in run.failures} <= {"FM-15", "FM-02", "FM-19", "FM-17", "FM-04", "FM-23", "FM-03"}          # 실패가 아니라 경고·기록뿐인 실행
    assert run_state(entry) == "done"
    assert "단계가 실패해" not in _all_text(at)


def test_a_variant_sent_to_a_person_never_says_no_edit_is_needed(app):
    """자동으로 확정하지 못한 광고안(확신 기준을 높게 둔 경우)에 ‘수정이 필요하지 않습니다’·‘표시할 구절이 없습니다’ 를 쓰지 않는다."""
    at = app.run()
    at.slider(key="opt_tau").set_value(0.99).run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    reports = at.session_state["runs"][-1]["run"].reports
    held = [r for r in reports if r.final_decision.value == "HUMAN_REVIEW" and r.rewrite_status.value == "none"]
    assert held, [r.final_decision.value for r in reports]
    text = _all_text(at)
    assert "수정이 필요하지 않습니다" not in text and "표시할 구절이 없습니다" not in text
    assert "자동으로 확정하지 못해 수정 제안을 만들지 않았습니다" in text


def test_halted_run_banner_uses_plain_words_and_says_nothing_was_handed_over(app):
    at = app.run()
    at.selectbox(key="opt_drill").set_value("mock: timeout").run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    run = at.session_state["runs"][-1]["run"]
    assert run.halted and not run.reports
    shown = "\n".join(str(e.value) for e in at.error)
    assert "실행을 끝내지 못했습니다" in shown
    assert "FM-" not in shown and "사람에게 넘김" not in shown


def test_hard_block_shows_its_message_once(app):
    at = app.run()
    at.selectbox(key="preset_pick").set_value("prohibited").run()
    at.button(key="example_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    run = at.session_state["runs"][-1]["run"]
    assert run.intercept.hard_block
    msgs = [str(e.value) for e in at.error]
    assert sum(1 for m in msgs if run.intercept.message and run.intercept.message in m) == 1, msgs
    assert not any("실행을 끝내지 못했습니다" in m for m in msgs)


def test_clicking_the_active_tab_does_not_deselect_the_view(app):
    at = app.run()
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    group = at.get("button_group")[0]
    group.set_value(None).run()           # 같은 탭을 다시 눌러 선택을 푼 것과 같다
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["nav"] == "품질 확인"                  # 마지막으로 보던 화면이 그대로 선택돼 있다
    assert "품질 확인" in _all_text(at)


def test_demo_run_does_not_call_stand_in_replies_model_calls(app):
    at = app.run()
    at.button(key="run_button").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    stats = "\n".join(str(m.value) for m in at.markdown if "pf-stat" in str(m.value))
    assert "대역 응답" in stats and "실제 모델 호출 없음" in stats


def test_failure_drill_that_example_runs_ignore_is_announced(app):
    at = app.run()
    at.selectbox(key="opt_drill").set_value("openai: timeout").run()
    assert "예시 실행에는 ‘mock: timeout’ 만 적용됩니다" in _all_text(at)
    at.selectbox(key="opt_drill").set_value("mock: timeout").run()
    assert "예시 실행에는 ‘mock: timeout’ 만 적용됩니다" not in _all_text(at)


def test_plain_wording_helpers_leave_no_internal_terms():
    from agents.adjudicator import DecisionInputs, decide
    from ui.components import plain_decision_note

    inputs = [DecisionInputs(det_block=True), DecisionInputs(verdict_available=False), DecisionInputs(model_decision="REJECT"),
              DecisionInputs(unverified_quotes=2), DecisionInputs(unsupported_decision=True), DecisionInputs(confidence=0.4),
              DecisionInputs(disagreement=True, model_decision="APPROVE", second_opinion="REVISE"), DecisionInputs(second_opinion_missing=True),
              DecisionInputs(det_fix=True), DecisionInputs(confidence=0.95)]
    for inp in inputs:
        reason = plain_decision_note(decide(inp, 0.8).reason)
        for word in ("finding", "fix 등급", "FM-", "fail-closed", "block)", "사람 검토"):
            assert word not in reason, (inp, reason)
    assert plain_decision_note("실행 비용·호출 상한 초과로 검사하지 못함 (FM-20) → 담당자 확인") == "실행 비용·호출 상한에 닿아 검사하지 못해 담당자 확인으로 넘겼습니다."


def test_handoff_for_an_ad_never_reviewed_by_a_model_does_not_ask_to_judge_the_models_reasoning():
    from agents.adjudicator import adjudicate
    from core.schemas import AdCopy, Decision, Fact, ProductCategory, ProductFacts
    from guardrails import output_guard
    from guardrails.policy import get_catalog
    from ui.components import human_review_reason, what_to_check

    cat = get_catalog()
    P = ProductFacts(name="p", category=ProductCategory.ECOMMERCE, facts=[Fact(id="F1", text="전 상품 10% 할인")])
    copy = AdCopy(headline="전 상품 10% 할인", body="지금 구매", cta="자세히 보기")
    rep = adjudicate(copy, output_guard.check(copy, P, {"ecommerce"}, "", [], cat), None, 0.8)
    rep.decision_reason = "실행 비용·호출 상한 초과로 검사하지 못함 (FM-20) → 담당자 확인"
    assert rep.final_decision == Decision.HUMAN_REVIEW
    assert "상한" in human_review_reason(rep) and "FM-" not in human_review_reason(rep)
    items = what_to_check(rep, cat)
    assert not any("상세 보기" in x or "검토 모델의 이유" in x for x in items), items
    assert any("상한" in x for x in items)


def test_regression_gate_messages_use_screen_words():
    from ui.quality import _plain_gate

    assert _plain_gate("human_review: 0.018 → 0.500 (허용 +0.15)").startswith("담당자 확인으로 넘긴 비율")
    assert "unsafe_approve" not in _plain_gate("unsafe_approve: 0.000 → 0.100 (허용 +0.00)")
    assert "recall" not in _plain_gate("TA-X-01 recall 1.0 → 0.5")


def test_css_targets_the_container_streamlit_actually_renders():
    from ui.theme import CSS

    assert 'data-testid="stMainBlockContainer"' in CSS


def test_mock_rate_disclosure_keeps_the_whole_rate_phrase():
    from llm.mock_provider import _mock_disclosure

    first = _mock_disclosure("loan", [{"text": "금리 연 4.5%~15.0% (심사 결과에 따라 다름)"}])[0]
    assert first == "연 4.5%~15.0% (심사 결과에 따라 다름)"
    assert _mock_disclosure("loan", [{"text": "연 7.9% 고정"}])[0] == "연 7.9%"
