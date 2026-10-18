"""금액이 둘 이상 들어간 문장이 화면에서 수식으로 깨지지 않는다 — Streamlit 의 st.markdown 계열은 `$ … $` 한 쌍을 수식으로 그린다.

4차 점검 뒤 방식 비교 화면의 비용 추정 문장('$3.10 ~ $3.97 (중간 $3.57, 상한 $3.50)')이 실제 화면에서 깨져 보였다. 렌더된 글 상자 전부에서 이스케이프되지 않은 달러 기호가 둘 이상인 것을 찾는다.
"""
from __future__ import annotations

import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")
KINDS = ("markdown", "info", "warning", "error", "success", "caption")


def unescaped_dollars(text: str) -> int:
    text = re.sub(r"`[^`]*`", "", text)            # 코드 조각 안의 $ 는 수식이 아니다
    return len(re.findall(r"(?<!\\)\$", text))


def offenders(at) -> list[str]:
    out = []
    for kind in KINDS:
        for el in getattr(at, kind, []):
            v = str(getattr(el, "value", "") or "")
            if unescaped_dollars(v) >= 2:
                out.append(f"{kind}: {v[:120]}")
    return out


def test_the_comparison_screen_estimate_and_warnings_do_not_render_as_math():
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.session_state["nav_request"] = "품질 확인"
    at.run()
    assert not at.exception
    texts = " ".join(str(getattr(el, "value", "")) for kind in KINDS for el in getattr(at, kind, []))
    assert "추정 비용" in texts and "3.5" in texts                          # 이 화면이 실제로 그려졌다 (빈 화면 통과 방지)
    assert not offenders(at), offenders(at)


def _incomplete_page(rep):
    from ui import compare_view

    compare_view._incomplete(rep)


def test_the_incomplete_run_panel_does_not_render_money_as_math():
    from ui import compare_view

    import json

    # 저장된 통제 비교 보고서(모양이 실제와 같다)를 '중단된 실제 실행' 으로 바꿔 쓴다
    rep = json.load(open(Path(__file__).resolve().parents[1] / "evals" / "reports" / "compare_controlled.json", encoding="utf-8"))
    rep = dict(rep, run_kind="live_incomplete", complete=False, incomplete_reason=["interrupted"], incomplete_note="끝까지 마치지 못했습니다",
               cost={"spent_usd": 0.5, "cap_usd": 3.5, "over_cap": False, "lower_bound": True, "failed_requests": 1, "estimated_calls": 0},
               aborted={"kind": "interrupted", "in_flight": {"case": "golden:G-004", "system": "combined", "cost_usd": 0.0123}})
    at = AppTest.from_function(_incomplete_page, args=(rep,), default_timeout=60).run()
    assert not at.exception, at.exception
    texts = [str(getattr(el, "value", "")) for kind in KINDS for el in getattr(at, kind, [])]
    assert any("쓴 비용" in t for t in texts), texts                     # 이 패널이 실제로 그려졌다
    assert not offenders(at), offenders(at)
    assert compare_view._md("$3.10 ~ $3.97") == "\\$3.10 ~ \\$3.97"
