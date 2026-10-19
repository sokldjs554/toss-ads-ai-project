"""소재 퀄리티 화면(ui/creative_quality_view.py) 테스트 — Streamlit AppTest, 네트워크·모델 없음.

- 두 화면(접이식 카드 · 두 문구 비교)이 예외 없이 뜨고, 맨 위 한 줄(심사와 별개·클릭률 예측 아님)과 신호 8개의 막대·설명·'이 신호가 보지 않는 것'이 있다
- 사용자가 넣은 글은 HTML 이스케이프되고 줄바꿈으로 마크다운 구조를 깰 수 없다
- 입력을 바꾸면 결과가 바뀌고(비슷함·더 나음), 빈 입력·긴 입력·계산 실패에서도 예외 표시가 없다
- 세션 상태 키는 `cq_` 로 시작하고, 화면 글에 전문 용어가 없다
"""
from __future__ import annotations

import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import creative_quality as cq

ROOT = Path(__file__).resolve().parents[1]
NOTE = "문구 품질 참고 신호이며 심사 판정과 별개입니다. 클릭률을 예측하지 않습니다."
JARGON = ("오탐", "미탐", "fact_id", "finding", "Finding", "Grounding", "Guardrail", "HUMAN_REVIEW", "APPROVE", "REVISE", "REJECT", "GRND-", "TA-", "holdout", "ablation", "Wilson")


def compare_app():
    from ui import creative_quality_view as v

    v.render()


def compare_app_with_job_facts():
    from core.schemas import Fact
    from ui import creative_quality_view as v

    v.render([Fact(id="F1", text="전 상품 10% 할인, 50,000원 이상 구매 시"), Fact(id="F2", text="10월 31일까지 진행")], {"ecommerce"}, key="cq_job")


def card_app():
    import streamlit as st

    from core.schemas import AdCopy, Fact
    from ui import creative_quality_view as v

    facts = [Fact(id="F1", text="전 상품 10% 할인, 50,000원 이상 구매 시"), Fact(id="F2", text="10월 31일까지 진행")]
    v.render_quality_card(AdCopy(headline="전 상품 10% 할인", body="5만원 이상 구매 시 10월 31일까지 쓸 수 있어요.", cta="쿠폰 받기"), facts, {"ecommerce"}, key="run1:var1")
    v.render_quality_card(AdCopy(headline="특별한 혜택을 만나보세요", body="지금 아니면 끝!!", cta="자세히 보기"), facts, {"ecommerce"}, key="run1:var2")
    st.write("끝")


def empty_card_app():
    from core.schemas import AdCopy
    from ui import creative_quality_view as v

    v.render_quality_card(AdCopy(headline="", body="", cta=""), [], set(), key="empty")


def broken_card_app():
    from core.schemas import AdCopy
    from ui import creative_quality_view as v

    v.render_quality_card(AdCopy(headline="전 상품 10% 할인", body="x", cta="y"), [], {"other"}, key="broken")


def _md(at) -> list[str]:
    return [str(m.value) for m in at.markdown]


def _text(at) -> str:
    parts = _md(at) + [str(c.value) for c in at.caption] + [str(w.value) for w in at.warning] + [str(i.value) for i in at.info] + [str(e.label) for e in at.expander]
    return "\n".join(parts)


def _run(fn, **kw) -> AppTest:
    at = AppTest.from_function(fn, default_timeout=60, **kw).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


# ───────────────────────── 두 문구 비교 화면 ─────────────────────────
def test_compare_screen_renders_note_verdict_and_two_cards():
    at = _run(compare_app)
    md = _md(at)
    assert any(NOTE in m and "cq-note" in m for m in md)
    assert any("신호상 A 문구가 더 나아요" in m for m in md)                            # 기본 예시: 구체적인 A 가 막연한 B 보다 낫다
    cards = [m for m in md if m.startswith('<div class="cq-card">')]
    assert len(cards) == 2
    for card in cards:
        assert card.count('class="cq-row"') == 8 and card.count("이 신호가 보지 않는 것") == 8 and card.count("이 신호가 보는 것") == 8
        for name in cq.SIGNAL_NAME.values():
            assert name in card
    assert any(e.label == "이 신호는 얼마나 믿을 만한가요?" for e in at.expander)
    assert [t.key for t in at.text_input][:1] == ["cq_cq_a_headline"]


def test_trust_expander_states_who_labeled_and_what_was_not_measured():
    text = "\n".join(str(m.value) for m in _run(compare_app).markdown)
    for needle in ("작성자 한 사람", "가상", "측정하지 않았어요", "쉬운 편"):
        assert needle in text
    df = _run(compare_app).dataframe
    assert len(df) == 1 and set(df[0].value.columns) >= {"쌍 묶음", "고른 쌍", "신호가 같은 쪽을 고른 쌍", "비율", "무작위로 고를 때"}
    assert len(df[0].value) == 2


def test_changing_inputs_changes_the_verdict():
    at = _run(compare_app)
    at.text_input(key="cq_cq_b_headline").set_value(at.text_input(key="cq_cq_a_headline").value)           # B 를 A 와 똑같이
    at.text_area(key="cq_cq_b_body").set_value(at.text_area(key="cq_cq_a_body").value)
    at.text_input(key="cq_cq_b_cta").set_value(at.text_input(key="cq_cq_a_cta").value)
    at.run()
    assert not at.exception
    assert any("두 문구가 비슷해요" in m for m in _md(at))
    at.text_input(key="cq_cq_a_headline").set_value("특별한 혜택을 만나보세요")
    at.text_area(key="cq_cq_a_body").set_value("지금 아니면 끝!! 정말 정말 좋아요")
    at.text_input(key="cq_cq_a_cta").set_value("클릭")
    at.run()
    assert not at.exception
    assert any("신호상 B 문구가 더 나아요" in m for m in _md(at))


def test_both_empty_says_nothing_to_compare_without_error():
    at = _run(compare_app)
    for side in "ab":
        at.text_input(key=f"cq_cq_{side}_headline").set_value("")
        at.text_area(key=f"cq_cq_{side}_body").set_value("")
        at.text_input(key=f"cq_cq_{side}_cta").set_value("")
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("비교할 수 있는 신호가 없어요" in m for m in _md(at))


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for name, _ in attrs:
            assert not name.startswith("on"), f"이벤트 속성 {name}"


def test_escape_helper_neutralizes_markup_newlines_and_dollar_signs():
    from ui import creative_quality_view as v

    out = v._esc("<script>x</script>\n\n# 제목\r\n    들여쓴 줄 $5$ \"따옴표\" 'a'")
    assert "<" not in out and ">" not in out and "\n" not in out and "\r" not in out and "$" not in out and "  " not in out
    assert "&lt;script&gt;" in out and "&#36;5&#36;" in out and "&quot;" in out


def test_user_text_is_escaped_and_cannot_break_the_markup():
    attack_body = "<script>alert(1)</script><img src=x onerror=alert(2)> $5$ *굵게* `코드`\n\n# 제목\n\n    들여쓴 줄\n</div><div class=\"x\">"
    at = _run(compare_app)
    at.text_area(key="cq_cq_a_body").set_value(attack_body)
    at.text_input(key="cq_cq_a_headline").set_value("<b>굵게</b> 역대급!! 지금 아니면")
    at.text_input(key="cq_cq_a_cta").set_value("<a href='javascript:x'>$5$ 클릭</a>")
    at.text_area(key="cq_cq_facts").set_value("F1: <script>x</script> 10% 할인 $9$")
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    blocks = [m for m in _md(at) if m.startswith("<div")]
    cards = [m for m in blocks if m.startswith('<div class="cq-card">')]
    assert len(cards) == 2 and len(blocks) == 4                              # 맨 위 한 줄 1 + 판정 상자 1 + 카드 2 — 입력이 HTML 덩어리를 쪼개지 못했다
    parser = _Tags()
    for m in blocks:
        assert "\n" not in m and "\r" not in m, "빈 줄이 생기면 마크다운이 HTML 덩어리를 끊고 뒷글을 서식으로 읽는다"
        assert "$" not in m
        parser.feed(m)
    assert parser.tags <= {"div", "span", "br", "ul", "li", "b"}, parser.tags            # script·img·a 같은 태그가 살아 있지 않다
    assert "&lt;a href=" in cards[0] and "&#36;5&#36;" in cards[0]                    # 버튼 글이 설명에 인용될 때 이스케이프된다
    assert "역대급" in cards[0]                                                        # 진짜 표현은 그대로 읽힌다


def test_long_input_still_renders():
    at = _run(compare_app)
    at.text_area(key="cq_cq_a_body").set_value("가나다 " * 400)
    at.text_area(key="cq_cq_facts").set_value("\n".join(f"F{i}: 사실 {i} 10% 할인" for i in range(200)))
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len([m for m in _md(at) if m.startswith('<div class="cq-card">')]) == 2


def test_job_facts_hide_the_fact_box_and_say_so():
    at = _run(compare_app_with_job_facts)
    assert not [t for t in at.text_area if t.key.endswith("_facts")]
    assert any("광고 작업의 상품 정보 2개를 기준으로 비교해요" in str(c.value) for c in at.caption)
    assert not at.selectbox                                          # 업종도 넘겨받았다
    assert any("신호상" in m for m in _md(at))


def test_session_keys_use_the_cq_prefix_and_widgets_too():
    at = _run(compare_app)
    internal = ("$$", "_streamlit", "FormSubmitter")
    keys = [k for k in at.session_state if not k.startswith(internal)]
    assert keys and all(k.startswith("cq_") for k in keys), keys
    at = _run(card_app)
    keys = [k for k in at.session_state if not k.startswith(internal)]
    assert keys and all(k.startswith("cq_") for k in keys), keys


def test_screen_text_has_no_jargon():
    for fn in (compare_app, card_app):
        at = _run(fn)
        shown = "\n".join(re.sub(r"<style>.*?</style>", "", m, flags=re.S) for m in _md(at)) + _text(at)
        for word in JARGON:
            assert word not in shown, (fn.__name__, word)


# ───────────────────────── 접이식 카드 ─────────────────────────
def test_card_expander_has_grade_note_and_eight_signals():
    at = _run(card_app)
    labels = [e.label for e in at.expander]
    assert labels[0].startswith("소재 퀄리티 (참고) · ") and labels[0].endswith("상") and labels[1].startswith("소재 퀄리티 (참고) · ")
    assert labels[1].split("· ")[-1] in ("중", "하")
    md = _md(at)
    assert sum(NOTE in m for m in md) == 2                              # 카드마다 맨 위 한 줄
    cards = [m for m in md if m.startswith('<div class="cq-card">')]
    assert len(cards) == 2 and all(c.count('class="cq-bar"') + c.count('class="cq-bar none"') == 8 for c in cards)
    assert 'role="img"' in cards[0] and "참고 등급 상" in cards[0]
    assert "압박·과장 표현" in cards[1] and "정책 위반인지는 심사가 따로 판단해요" in cards[1]


def test_card_is_cached_per_key_in_the_session():
    at = _run(card_app)
    assert {"cq_card_run1:var1", "cq_card_run1:var2"} <= set(at.session_state)
    first = at.session_state["cq_card_run1:var1"][1]
    at.run()
    assert at.session_state["cq_card_run1:var1"][1] is first             # 같은 입력이면 다시 계산하지 않는다


def test_empty_copy_card_says_it_cannot_be_computed():
    at = _run(empty_card_app)
    assert at.expander[0].label.endswith("계산할 수 없음")
    card = next(m for m in _md(at) if m.startswith('<div class="cq-card">'))
    assert "종합 등급을 계산할 수 없어요" in card and "계산하지 못해 종합에서 뺀 신호" in card and card.count("계산 안 함") == 8


def test_a_failing_calculation_never_shows_an_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("계산 실패")

    monkeypatch.setattr(cq, "quality_card", boom)          # AppTest 는 같은 프로세스에서 돌므로 화면 모듈이 쓰는 함수가 바뀐다 (테스트가 끝나면 원래대로)
    at = AppTest.from_function(broken_card_app, default_timeout=60).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.expander[0].label.endswith("계산하지 못했어요")
    assert any("광고 검사 결과에는 영향이 없어요" in str(c.value) for c in at.caption)


# ───────────────────────── 모바일·구조 ─────────────────────────
def test_css_keeps_390px_from_overflowing():
    from ui import creative_quality_view as v

    css = v.CSS
    assert "@media (max-width: 480px)" in css and "grid-template-columns:minmax(0,1fr) 3.4rem" in css      # 좁은 화면에서는 이름 줄을 따로 쓰고 막대가 남는 폭을 쓴다
    for token in ("overflow-wrap:anywhere", "min-width:0", "flex-wrap:wrap", "minmax(0,"):
        assert token in css
    assert not re.search(r"(?<!max-)(?<!min-)\bwidth:\s*\d{3,}px", css) and css.count("white-space:nowrap") <= 3     # 고정 폭 없음, 줄바꿈 금지는 짧은 배지·숫자에만
    assert not re.search(r"[^-]\bwidth:\s*\d+(?:\.\d+)?(?:rem|px)", css.replace("@media (max-width: 480px)", ""))


def test_view_imports_only_display_helpers_and_the_quality_core():
    tree = ast.parse((ROOT / "ui" / "creative_quality_view.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert mods <= {"__future__", "html", "json", "pathlib", "typing", "streamlit", "core", "core.schemas", "ui.presets"}, mods
    assert not [m for m in mods if m.split(".")[0] in ("agents", "llm", "guardrails", "evals", "observability")]
    assert "creative_quality" in (ROOT / "ui" / "creative_quality_view.py").read_text(encoding="utf-8")


def test_verdict_explains_which_signals_decided_and_which_were_left_out():
    from core.schemas import AdCopy
    from ui import creative_quality_view as v

    good = cq.quality_card(AdCopy(headline="전 상품 10% 할인", body="5만원 이상 구매 시 쓸 수 있어요.", cta="쿠폰 받기"), [], set())
    bad = cq.quality_card(AdCopy(headline="특별한 혜택", body="지금 아니면 끝!!", cta="클릭"), [], set())
    text = v._verdict_html(cq.compare_cards(good, bad))
    assert "신호상 A 문구가 더 나아요" in text and "빠진 신호" in text          # 상품 정보가 없어 구체성·조건 표시는 비교에서 빠진다
    assert "비슷해요" in v._verdict_html(cq.compare_cards(good, good)) and "비교할 수 있는 신호가 없어요" in v._verdict_html(cq.Comparison("unknown", None, None, None, (), ()))
