from streamlit.testing.v1 import AppTest
from ui import live_pilot_view


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
