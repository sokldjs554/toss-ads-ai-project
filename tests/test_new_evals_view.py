"""'품질 확인' 의 새 기능 평가 패널: 저장된 리포트를 그대로 보여 주고, 한계 문장이 함께 나오며, 리포트가 없어도 화면이 깨지지 않는다."""
import json
import os

import pytest
from streamlit.testing.v1 import AppTest

from ui import new_evals


@pytest.fixture
def quality_view(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    at = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"), default_timeout=120)
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    assert not at.exception, [e.value[:200] for e in at.exception]
    return at


def _all_text(at) -> str:
    out = []
    for kind in ("markdown", "caption", "info", "expander"):
        out += [str(getattr(e, "value", "") or getattr(e, "label", "")) for e in getattr(at, kind)]
    return "\n".join(out)


def test_the_panel_lists_the_five_features_and_the_synthetic_data_warning(quality_view):
    text = _all_text(quality_view)
    for title, _ in new_evals.SECTIONS:
        assert title in text
    assert "새 기능 평가 (합성 데이터)" in text and "독립 검증은 없으며" in text


def test_every_section_states_that_the_numbers_are_not_real_model_or_real_data(quality_view):
    for title, fn in new_evals.SECTIONS:
        at = AppTest.from_string(f"from ui import new_evals\nnew_evals.{fn.__name__}()", default_timeout=60)
        at.run()
        assert not at.exception, (title, [e.value[:200] for e in at.exception])
        text = _all_text(at)
        assert "측정하지 않았" in text or "정확도가 아닙니다" in text or "합성" in text, title


def test_numbers_come_from_the_stored_reports_not_from_the_view():
    """화면의 개발용·최종 확인용 숫자가 저장된 리포트의 값과 같다 (여기서 새로 계산하지 않는다)."""
    rep = new_evals._load("insights_controlled.json")
    dev, ho = new_evals._two(rep["splits"], lambda s: new_evals._str_frac(s["cause_top1_n"]))
    k, n = rep["splits"]["dev"]["cause_top1_n"].split("/")
    assert dev.startswith(f"{k}/{n}")
    k, n = rep["splits"]["holdout"]["cause_top1_n"].split("/")
    assert ho.startswith(f"{k}/{n}")


def test_a_missing_report_shows_a_message_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    at = AppTest.from_string("from ui import new_evals\nnew_evals.render()", default_timeout=60)
    at.run()
    assert not at.exception, [e.value[:200] for e in at.exception]


def test_a_broken_report_shape_degrades_to_dashes(monkeypatch, tmp_path):
    for name in ("dialogs_controlled.json", "url_ingest_eval.json", "image_eval.json", "quality_signals.json", "insights_controlled.json"):
        (tmp_path / name).write_text(json.dumps({"splits": {"dev": {"x": 1}, "holdout": {}}, "by_split": {"dev": {}, "holdout": {"y": 2}}}), encoding="utf-8")
    monkeypatch.setattr(new_evals, "REPORTS", tmp_path)
    for _, fn in new_evals.SECTIONS:
        fn()   # 예외 없이 끝나야 한다 (AppTest 밖이라 st 호출은 무시된다)
    assert new_evals._frac(None, 3) == "—" and new_evals._str_frac("x") == "—" and new_evals._pct("a") == "—"
