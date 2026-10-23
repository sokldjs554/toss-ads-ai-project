from streamlit.testing.v1 import AppTest
from ui import live_pilot_view
import pytest


class Store:
    reservations = 0
    def __init__(self, *a): pass
    def account(self, token): return {"id":"test", "kind":"automation"}
    def list_runs(self, token): return []
    def begin(self, *a):
        Store.reservations += 1
        raise AssertionError("invalid input must not reserve")


def app():
    from ui.live_pilot_view import render
    render()


@pytest.mark.parametrize("missing,expected", [("OPENAI_API_KEY","모델 연결"), ("PREFLIGHT_LIVE_DATABASE_URL","저장소 연결")])
def test_missing_configuration_identifies_only_missing_component(monkeypatch, missing, expected):
    monkeypatch.setenv("PREFLIGHT_LIVE_DATABASE_URL", "test-not-a-real-url")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.delenv(missing)
    at = AppTest.from_function(app).run()
    assert any("미등록: " + expected in x.value for x in at.warning)
    assert not at.text_input and not at.exception


def test_login_form_reaches_authenticated_view_and_logout_clears_credential(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_LIVE_DATABASE_URL", "test-not-a-real-url")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr(live_pilot_view, "LiveStore", Store)
    at = AppTest.from_function(app).run()
    next(w for w in at.text_input if w.label == "LIVE 검토 접근키").set_value("a" * 64)
    next(b for b in at.button if b.label == "LIVE 검토 접속").click().run()
    assert at.session_state["pilot_token"] == "a" * 64
    assert any(b.label == "LIVE 로그아웃" for b in at.button)
    assert not at.exception and not at.error
    next(b for b in at.button if b.label == "LIVE 로그아웃").click().run()
    assert "pilot_token" not in at.session_state
    assert any(w.label == "LIVE 검토 접근키" for w in at.text_input)


def test_invalid_fact_count_does_not_reserve_or_invoke_provider(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_LIVE_DATABASE_URL", "test-not-a-real-url")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr(live_pilot_view, "LiveStore", Store)
    Store.reservations = 0
    at = AppTest.from_function(app)
    at.session_state["pilot_token"] = "a" * 64
    at.session_state["pilot_loaded"] = {"headline":"가을 쿠폰", "body":"이용 조건을 확인하세요", "cta":"확인하기", "facts":["조건을 확인하세요"]*21}
    at.run()
    next(b for b in at.button if b.label == "실제 LLM 실행").click().run()
    assert Store.reservations == 0 and not at.exception
    assert any("호출·예약하지 않았습니다" in x.value for x in at.error)


def test_disclosures_survive_loading_into_manual_edit_form(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_LIVE_DATABASE_URL", "test-not-a-real-url")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr(live_pilot_view, "LiveStore", Store)
    at = AppTest.from_function(app)
    at.session_state["pilot_token"] = "a" * 64
    at.session_state["pilot_loaded"] = {"headline":"가을 쿠폰", "body":"이용 조건을 확인하세요", "cta":"확인하기", "facts":["쿠폰을 제공합니다"], "disclosures":["대상 상품에 한함", "중복 사용 불가"]}
    at.run()
    assert at.text_area(key="pilot_disclosures").value == "대상 상품에 한함\n중복 사용 불가"
    assert not at.exception
