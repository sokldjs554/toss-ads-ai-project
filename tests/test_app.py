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


def _latest_recheck(at: AppTest, vid: str):
    """세션에 쌓인 재검사 시도 기록(RecheckHistory) 중 이 광고안의 가장 최근 시도."""
    hists = [h for k, h in at.session_state["rechecks"].items() if k.endswith(f":{vid}")]
    assert hists, at.session_state["rechecks"].keys()
    return hists[-1].latest()


def test_three_views_render_without_exceptions(app):
    at = app.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "광고 만들기" in _all_text(at)
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
    for needle in ("검사 결과", "광고안 1", "가장 중요한 이유", "수정 제안", "다음에 할 수 있는 것", "예시 실행 · 실제 AI 호출 없음"):
        assert needle in text, needle
    # 전문 용어가 기본 화면에 없다
    for jargon in ("오탐", "Grounding", "Guardrail", "HUMAN_REVIEW", "APPROVE"):
        assert jargon not in text, jargon


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
