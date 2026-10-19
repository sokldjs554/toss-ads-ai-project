"""소재 퀄리티 화면 — 광고안 카드에 붙이는 작은 접이식 '소재 퀄리티'와, 두 문구를 나란히 견주는 독립 화면.

화면은 core/creative_quality.py 가 계산한 점수를 그리기만 한다 (계산·비교 로직은 여기 없다). 이 점수는 심사 판정과 별개의 문구 참고 신호이며
클릭률을 예측하지 않는다 — 그 말을 두 화면 맨 위에 항상 적는다. 문구·상품 정보는 사용자가 넣은 글이라 신뢰하지 않는 데이터로 다룬다(모든 출력은 HTML 이스케이프).

공개 함수
- render_quality_card(copy, facts, categories, key): 광고안 카드 아래에 붙이는 접이식 카드 (종합 등급 + 신호 8개의 막대·설명·'이 신호가 보지 않는 것')
- render(facts=None, categories=None, key="cq"): 제목·본문·버튼 두 세트를 입력받아 두 카드와 어느 쪽이 신호상 나은지·이유를 보여 주는 독립 화면
세션 상태 키는 모두 `cq_` 로 시작한다.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Iterable, Optional

import streamlit as st

from core import creative_quality as cq
from core.schemas import AdCopy, Fact, ProductCategory
from ui.presets import parse_facts

NOTE = "문구 품질 참고 신호이며 심사 판정과 별개입니다. 클릭률을 예측하지 않습니다."
GRADE_STYLE = {"상": ("#0B6B37", "#E6F4EC"), "중": ("#8A5A00", "#FFF3D6"), "하": ("#A32116", "#FCE8E6")}
GRADE_HELP = {"상": "신호상 잘 쓴 편이에요", "중": "고칠 만한 곳이 있어요", "하": "신호상 고칠 곳이 많아요"}
CATEGORY_LABELS = {c.value: n for c, n in [(ProductCategory.LOAN, "대출"), (ProductCategory.CARD, "카드"), (ProductCategory.INSURANCE, "보험"), (ProductCategory.INVESTMENT, "투자·증권"),
                                           (ProductCategory.SAVINGS, "예·적금"), (ProductCategory.CRYPTO, "가상자산"), (ProductCategory.ECOMMERCE, "커머스"), (ProductCategory.APP, "앱·서비스"),
                                           (ProductCategory.EDUCATION, "교육"), (ProductCategory.HEALTH, "건강·의료"), (ProductCategory.REALESTATE, "부동산"), (ProductCategory.OTHER, "기타")]}
REPORT_PATH = Path(__file__).resolve().parents[1] / "evals" / "reports" / "quality_signals.json"
SAMPLE_FACTS = "F1: 전 상품 10% 할인, 50,000원 이상 구매 시\nF2: 10월 31일까지 진행\nF3: 쿠폰은 1인 1회 사용 가능"
SAMPLE = {"a": ("전 상품 10% 할인", "5만원 이상 구매 시 10월 31일까지, 1인 1회 쓸 수 있어요.", "쿠폰 받기"),
          "b": ("특별한 혜택을 만나보세요", "지금 아니면 끝! 다양한 즐거움이 가득해요.", "자세히 보기")}

CSS = """<style>
.cq-note { font-size:.84rem; color:#4E5968; background:#F7F8FA; border:1px solid #E5E8EB; border-radius:10px; padding:8px 12px; margin:2px 0 10px; overflow-wrap:anywhere; }
.cq-card { max-width:100%; overflow-wrap:anywhere; word-break:keep-all; }
.cq-head { display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; margin-bottom:8px; }
.cq-grade { display:inline-block; padding:4px 14px; border-radius:999px; font-weight:800; font-size:1rem; white-space:nowrap; }
.cq-overall { font-size:.9rem; color:#191F28; }
.cq-row { display:grid; grid-template-columns:minmax(0,7.2rem) minmax(0,1fr) 3.4rem; gap:4px 10px; align-items:center; margin-top:10px; }
.cq-name { font-weight:700; font-size:.9rem; color:#191F28; }
.cq-bar { height:10px; background:#EEF0F3; border-radius:6px; overflow:hidden; min-width:0; }
.cq-bar > span { display:block; height:100%; border-radius:6px; }
.cq-bar.none { background:repeating-linear-gradient(45deg,#EEF0F3,#EEF0F3 6px,#E2E5E9 6px,#E2E5E9 12px); }
.cq-num { font-size:.86rem; font-weight:700; text-align:right; color:#191F28; white-space:nowrap; }
.cq-expl { grid-column:1 / -1; font-size:.84rem; color:#4E5968; line-height:1.5; }
.cq-scope { grid-column:1 / -1; font-size:.76rem; color:#6B7684; line-height:1.45; }
.cq-excluded { margin-top:12px; font-size:.82rem; color:#4E5968; background:#FAFBFC; border-left:3px solid #E5E8EB; padding:6px 10px; border-radius:0 8px 8px 0; overflow-wrap:anywhere; }
.cq-verdict { border:1px solid #E5E8EB; border-radius:12px; background:#fff; padding:12px 14px; margin:8px 0 12px; overflow-wrap:anywhere; }
.cq-verdict b { font-size:1rem; }
.cq-verdict ul { margin:6px 0 0 1.1rem; padding:0; font-size:.88rem; color:#4E5968; }
@media (max-width: 480px) {
  .cq-row { grid-template-columns:minmax(0,1fr) 3.4rem; }
  .cq-name { grid-column:1 / -1; }
  .cq-bar { grid-column:1 / 2; }
}
</style>"""


def _esc(text: object) -> str:
    """사용자가 넣은 글이 들어갈 수 있는 모든 문자열은 이걸 거친다. 줄바꿈·연속 공백은 한 칸으로 줄이고(빈 줄이 생기면 마크다운이 HTML 덩어리를 끊고 뒷글을 서식으로 읽는다),
    HTML 특수문자는 이스케이프하며, '$' 는 수식으로 읽히지 않게 숫자 참조로 바꾼다."""
    return html.escape(" ".join(str(text).split()), quote=True).replace("$", "&#36;")


def _bar_color(score: float) -> str:
    return "#2B5BD7" if score >= 0.5 else "#C77700"


def _signal_html(s: cq.Signal) -> str:
    if s.score is None:
        bar, num = '<div class="cq-bar none" role="img" aria-label="계산하지 않음"></div>', "계산 안 함"
    else:
        pts = round(s.score * 100)
        bar = f'<div class="cq-bar" role="img" aria-label="{_esc(s.name)} {pts}점"><span style="width:{pts}%;background:{_bar_color(s.score)}"></span></div>'
        num = f"{pts}점"
    return (f'<div class="cq-row"><div class="cq-name">{_esc(s.name)}</div>{bar}<div class="cq-num">{num}</div>'
            f'<div class="cq-expl">{_esc(s.explanation)}</div>'
            f'<div class="cq-scope">이 신호가 보는 것: {_esc(s.measures)}<br>이 신호가 보지 않는 것: {_esc(s.does_not_measure)}</div></div>')


def card_html(card: cq.QualityCard) -> str:
    """카드 한 장의 HTML (종합 등급 + 신호별 막대). 들여쓰기·줄바꿈 없이 한 덩어리로 만든다(마크다운이 코드 블록으로 읽지 않게)."""
    if card.overall is None:
        head = '<div class="cq-head"><span class="cq-overall"><b>종합 등급을 계산할 수 없어요</b> — 읽을 수 있는 문구가 없거나 계산된 신호가 없어요.</span></div>'
    else:
        fg, bg = GRADE_STYLE[card.grade]
        head = (f'<div class="cq-head"><span class="cq-grade" style="color:{fg};background:{bg}" role="status">참고 등급 {_esc(card.grade)}</span>'
                f'<span class="cq-overall">종합 {round(card.overall * 100)}점 · {_esc(GRADE_HELP[card.grade])} (점수 구간으로만 정한 띠예요)</span></div>')
    rows = "".join(_signal_html(s) for s in card.signals)
    excluded = ""
    if card.excluded:
        excluded = ('<div class="cq-excluded"><b>계산하지 못해 종합에서 뺀 신호</b><br>'
                    + "<br>".join(f"· {_esc(cq.SIGNAL_NAME[k])} — {_esc(why)}" for k, why in card.excluded)
                    + f"<br>남은 신호의 가중치를 다시 합 100%로 맞춰 평균했어요.</div>")
    notes = "".join(f'<div class="cq-excluded">{_esc(n)}</div>' for n in card.notes)
    return f'<div class="cq-card">{head}{rows}{excluded}{notes}</div>'


def _inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def _note() -> None:
    st.markdown(f'<div class="cq-note">{_esc(NOTE)}</div>', unsafe_allow_html=True)


def _signature(copy: AdCopy, facts: list[Fact], cats: set[str]) -> tuple:
    return (copy.headline, copy.body, copy.cta, tuple(copy.disclosures), tuple((f.id, f.text) for f in facts), tuple(sorted(cats)))


def _card_for(key: str, copy: AdCopy, facts: list[Fact], cats: set[str]) -> Optional[cq.QualityCard]:
    """같은 입력이면 다시 계산하지 않는다(세션에 한 장씩 보관). 계산이 어떤 이유로든 실패하면 None — 화면에 예외를 내지 않는다."""
    skey = f"cq_card_{key}"
    sig = _signature(copy, facts, cats)
    cached = st.session_state.get(skey)
    if cached and cached[0] == sig:
        return cached[1]
    try:
        card = cq.quality_card(copy, facts, cats)
    except Exception:       # noqa: BLE001 — 참고용 화면이 광고 작업 화면을 깨뜨리면 안 된다
        return None
    st.session_state[skey] = (sig, card)
    return card


def render_quality_card(copy: AdCopy, facts: list[Fact], categories: Iterable[str], key: str) -> None:
    """광고안 카드에 붙이는 작은 접이식 '소재 퀄리티'. 판정 결과와 무관한 참고 신호다.

    호출 예: render_quality_card(report.ad_copy, product.facts, set(run.categories), key=f"{run.run_id}:{report.variant_id}")
    """
    _inject()
    cats = {str(getattr(c, "value", c)) for c in (categories or ())}
    card = _card_for(key, copy, list(facts or []), cats)
    if card is None:
        with st.expander("소재 퀄리티 (참고) · 계산하지 못했어요"):
            _note()
            st.caption("이 문구는 퀄리티 신호를 계산하지 못했어요. 광고 검사 결과에는 영향이 없어요.")
        return
    label = f"소재 퀄리티 (참고) · {card.grade}" if card.overall is not None else "소재 퀄리티 (참고) · 계산할 수 없음"
    with st.expander(label):
        _note()
        st.markdown(card_html(card), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 독립 화면 — 두 문구 나란히 비교
# ---------------------------------------------------------------------------
def _verdict_html(cmp: cq.Comparison) -> str:
    if cmp.winner == "unknown":
        return '<div class="cq-verdict"><b>비교할 수 있는 신호가 없어요</b><br>제목·본문·버튼을 입력하면 비교해요.</div>'
    if cmp.winner == "tie":
        title = f"신호상 두 문구가 비슷해요 (점수 차가 {round(cq.TIE_MARGIN * 100)}점 미만)"
    else:
        title = f"신호상 {'A' if cmp.winner == 'a' else 'B'} 문구가 더 나아요 (종합 {round(cmp.score_a * 100)}점 vs {round(cmp.score_b * 100)}점)"
    items = []
    for key, delta in cmp.reasons[:3]:
        side = "A" if delta > 0 else "B"
        items.append(f"<li>{_esc(cq.SIGNAL_NAME[key])} — {side} 쪽이 더 좋아요 (종합에 {abs(delta) * 100:.0f}점만큼 반영)</li>")
    skipped = [cq.SIGNAL_NAME[k] for k in cq.SIGNAL_KEYS if k not in cmp.basis]
    foot = f"<br><span class='cq-scope'>두 문구에서 모두 계산된 신호 {len(cmp.basis)}개로만 비교했어요" + (f" (빠진 신호: {_esc(', '.join(skipped))})" if skipped else "") + ".</span>"
    return f'<div class="cq-verdict"><b>{_esc(title)}</b><ul>{"".join(items)}</ul>{foot}</div>'


def _trust_expander() -> None:
    with st.expander("이 신호는 얼마나 믿을 만한가요?"):
        try:
            rep = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            counts = rep["dataset"]["counts"]
            rows = []
            for name, sp in (("개발용 (신호를 만들 때 본 쌍)", "dev"), ("최종 확인용 (신호를 정한 뒤에 쓴 쌍)", "holdout")):
                d = rep["splits"][sp]["decisive_agreement"]
                rows.append({"쌍 묶음": name, "고른 쌍": d["n"], "신호가 같은 쪽을 고른 쌍": d["k"], "비율": f"{d['rate']:.0%}", "무작위로 고를 때": f"{rep['splits'][sp]['chance']['level']:.0%}"})
            st.markdown(f"작성자가 직접 어느 쪽이 더 나은지 고른 문구 {counts['all']['pairs']}쌍으로 확인했어요 (확실히 한쪽이 나은 쌍만 표에 셌어요).")
            st.dataframe(rows, width="stretch", hide_index=True)
        except Exception:       # noqa: BLE001 — 저장된 결과를 읽지 못해도 화면은 그대로
            st.caption("저장된 확인 결과를 읽지 못했어요.")
        st.markdown("- 어느 쪽이 나은지는 **작성자 한 사람**이 골랐고, 다른 사람의 확인은 없어요.\n- 문구와 상품 정보는 모두 **가상**이에요.\n"
                    "- 실제 클릭률·전환·광고주 만족과 얼마나 같이 움직이는지는 **측정하지 않았어요**.\n- 한 가지 차이가 뚜렷한 쌍이 많아 쉬운 편이에요. 실제 광고에서는 이보다 낮을 수 있어요.")
        st.caption("재현: python -m evals.quality_signals --split all")


def render(facts: Optional[list[Fact]] = None, categories: Optional[Iterable[str]] = None, *, key: str = "cq") -> None:
    """두 문구를 나란히 견주는 독립 화면. facts·categories 를 주면(광고 작업의 상품 정보) 입력란을 숨기고 그것을 쓴다.

    호출 예: creative_quality_view.render(product.facts, {product.category.value}, key="cq_studio")
    """
    _inject()
    st.markdown("### 소재 퀄리티 비교")
    _note()
    st.caption("두 문구를 규칙으로 읽어 길이·구체성·조건 표시·행동 유도·읽기 쉬움·중복·압박 표현·말투를 견줘요. 모델을 부르지 않고, 같은 입력이면 같은 결과가 나와요. 검사 결과(통과·수정·게재 불가)에는 영향을 주지 않아요.")
    if facts is None:
        text = st.text_area("상품 정보 (한 줄에 하나)", SAMPLE_FACTS, key=f"cq_{key}_facts", height=110,
                            help="문구가 상품 정보를 얼마나 담았는지, 조건을 빼먹지 않았는지 보려고 써요. 예시는 가상이에요.")[:4000]
        use_facts = parse_facts(text)[:30]
    else:
        use_facts = list(facts)
        st.caption(f"광고 작업의 상품 정보 {len(use_facts)}개를 기준으로 비교해요.")
    if categories is None:
        cat = st.selectbox("업종", list(CATEGORY_LABELS), index=list(CATEGORY_LABELS).index("ecommerce"), format_func=CATEGORY_LABELS.get, key=f"cq_{key}_category")
        cats = {cat}
    else:
        cats = {str(getattr(c, "value", c)) for c in categories}
    cols = st.columns(2)
    copies: dict[str, AdCopy] = {}
    for col, side, title in zip(cols, ("a", "b"), ("문구 A", "문구 B")):
        h0, b0, c0 = SAMPLE[side]
        with col:
            st.markdown(f"**{title}** (예시 문구예요. 바꿔 보세요)")
            h = st.text_input("제목", h0, key=f"cq_{key}_{side}_headline", max_chars=200)
            b = st.text_area("본문", b0, key=f"cq_{key}_{side}_body", height=90, max_chars=1000)
            c = st.text_input("버튼", c0, key=f"cq_{key}_{side}_cta", max_chars=100)
        copies[side] = AdCopy(headline=h, body=b, cta=c)
    ca, cb = _card_for(f"{key}_a", copies["a"], use_facts, cats), _card_for(f"{key}_b", copies["b"], use_facts, cats)
    if ca is None or cb is None:
        st.warning("입력한 문구를 읽지 못해 신호를 계산하지 못했어요. 다른 글로 바꿔 보세요.", icon="⚠️")
    else:
        st.markdown(_verdict_html(cq.compare_cards(ca, cb)), unsafe_allow_html=True)
        left, right = st.columns(2)
        with left:
            st.markdown("**문구 A**")
            st.markdown(card_html(ca), unsafe_allow_html=True)
        with right:
            st.markdown("**문구 B**")
            st.markdown(card_html(cb), unsafe_allow_html=True)
    _trust_expander()
