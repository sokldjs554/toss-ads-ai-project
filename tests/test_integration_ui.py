"""새 기능이 앱에 제대로 이어졌는지: 여섯 화면이 열리고, 광고 작업의 세 가지 시작 방법이 입력 칸을 채우고,
광고안마다 소재 퀄리티·배너·이미지 검사가 붙는다. 실제 모델·네트워크 호출 없이 예시 실행(DEMO)으로 본다."""
import os

import pytest
from streamlit.testing.v1 import AppTest

VIEWS = ("광고 작업", "성과 리포트", "검사 사례", "품질 확인", "운영", "LIVE 검토")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    at = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def _text(at) -> str:
    parts = []
    for kind in ("markdown", "caption", "info", "success", "warning", "error", "expander"):
        parts += [str(getattr(e, "value", "") or getattr(e, "label", "")) for e in getattr(at, kind)]
    return "\n".join(parts)


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_opens_without_an_exception(app, view):
    app.session_state["nav_request"] = view
    app.run()
    assert not app.exception, [e.value[:200] for e in app.exception]
    assert app.session_state["nav"] == view


def test_all_views_include_the_explicit_live_workflow(app):
    seg = list(app.get("segmented_control"))
    assert seg and tuple(seg[0].options) == VIEWS


def test_start_modes_are_offered_and_example_mode_is_the_default(app):
    r = app.radio(key="start_mode")
    assert tuple(r.options) == ("예시로 시작", "대화로 시작", "상품 페이지에서 가져오기") and r.value == "예시로 시작"


def test_chat_start_fills_the_form_and_goes_back_to_the_form(app):
    app.radio(key="start_mode").set_value("대화로 시작").run()
    assert not app.exception and len(app.chat_input) == 1
    app.chat_input[0].set_value("20대 직장인 대상 신용대출, 월 300만원, 신청 전환이에요").run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    apply_btn = [b for b in app.button if b.label == "이 내용으로 광고 작업 채우기"]
    assert apply_btn, [b.label for b in app.button]
    apply_btn[0].click().run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    ss = app.session_state
    assert ss["form_budget"] == 3_000_000 and ss["form_category"] == "loan"
    assert "300만원" in ss["form_input"] or "3,000,000" in ss["form_input"]
    assert app.radio(key="start_mode").value == "예시로 시작"          # 채운 뒤에는 입력 칸이 있는 화면으로 돌아온다
    assert any("대화에서 이해한 내용" in str(s.value) for s in app.success)


def test_url_import_fills_only_the_facts_the_user_confirmed(app):
    app.radio(key="start_mode").set_value("상품 페이지에서 가져오기").run()
    assert not app.exception
    app.button(key="urlimp_read").click().run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    boxes = [c for c in app.checkbox if str(c.key).startswith("urlimp_pick_")]
    assert boxes, [c.key for c in app.checkbox]
    assert not any(c.value for c in boxes)                              # 기본은 아무것도 고르지 않는다
    boxes[0].check().run()
    apply_btn = [b for b in app.button if "광고 작업 채우기" in b.label]
    assert apply_btn
    apply_btn[0].click().run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    facts = app.session_state["form_facts"].splitlines()
    assert len(facts) == 1 and facts[0].startswith("F1:")
    assert app.radio(key="start_mode").value == "예시로 시작"
    assert any("상품 페이지에서 확인한 내용" in str(s.value) for s in app.success)


def test_each_ad_gets_quality_signals_banner_and_image_check(app):
    app.radio(key="start_mode").set_value("예시로 시작").run()
    app.button(key="example_run").click().run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    labels = [str(e.label) for e in app.expander]
    assert any(l.startswith("소재 퀄리티 (참고)") for l in labels), labels
    toggles = [t for t in app.toggle if str(t.key).startswith("cr_open_")]
    assert toggles
    toggles[0].set_value(True).run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    text = _text(app)
    assert "배너 시안" in text and "이미지 검사" in text


def test_judgments_do_not_change_when_creative_extras_are_opened(app):
    """퀄리티·배너·이미지 영역은 판정을 바꾸지 않는다."""
    app.button(key="example_run").click().run()
    before = [(r.final_decision.value, r.verdict.confidence) for r in app.session_state["runs"][-1]["run"].reports]
    for t in [t for t in app.toggle if str(t.key).startswith("cr_open_")]:
        t.set_value(True)
    app.run()
    after = [(r.final_decision.value, r.verdict.confidence) for r in app.session_state["runs"][-1]["run"].reports]
    assert before == after and not app.exception


def test_review_queue_tab_receives_the_human_review_items_of_the_last_run(app):
    app.radio(key="start_mode").set_value("예시로 시작").run()
    app.session_state["preset_pick"] = "malicious"
    app.button(key="example_run").click().run()
    app.session_state["nav_request"] = "운영"
    app.run()
    assert not app.exception, [e.value[:300] for e in app.exception]
    assert "검수 대기열" in _text(app) or any("검수" in str(t.label) for t in app.tabs)
