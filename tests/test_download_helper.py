"""파일 내려받기 도우미: 서버 환경에서는 내려받기 단추, 브라우저 실행판에서는 내용을 그대로 보여 준다(그곳에는 파일을 내려 줄 서버가 없다)."""
import pytest
from streamlit.testing.v1 import AppTest

from ui import components


def _run(script: str) -> AppTest:
    at = AppTest.from_string(script, default_timeout=30)
    at.run()
    assert not at.exception, [e.value[:200] for e in at.exception]
    return at


@pytest.mark.parametrize("in_browser", [False, True])
def test_download_or_show(monkeypatch, in_browser):
    monkeypatch.setattr(components, "IN_BROWSER", in_browser)
    at = _run('from ui import components\ncomponents.download_or_show("내려받기", "안녕\\n두 번째 줄", "a.txt", "text/plain", "k1")')
    buttons = list(at.get("download_button"))
    if in_browser:
        assert not buttons and len(at.code) == 1 and "두 번째 줄" in at.code[0].value
        assert any("내려받기 대신 내용을 보여 드립니다" in str(c.value) for c in at.caption)
    else:
        assert len(buttons) == 1 and not at.code


def test_a_very_long_text_is_cut_in_the_browser_build(monkeypatch):
    monkeypatch.setattr(components, "IN_BROWSER", True)
    at = _run(f'from ui import components\ncomponents.download_or_show("x", "가" * ({components.MAX_SHOWN_CHARS} + 50), "big.txt", "text/plain", "k2")')
    assert len(at.code[0].value) == components.MAX_SHOWN_CHARS
    assert any("앞" in str(c.value) and "자만 보여" in str(c.value) for c in at.caption)


def test_the_on_click_hint_button_appears_only_when_a_callback_is_given(monkeypatch):
    monkeypatch.setattr(components, "IN_BROWSER", True)
    at = _run('from ui import components\ncomponents.download_or_show("x", "t", "f.txt", "text/plain", "k3", on_click=lambda: None)\n'
              'components.download_or_show("y", "t", "g.txt", "text/plain", "k4")')
    assert [b.key for b in at.button] == ["k3_shown"]
