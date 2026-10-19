"""상품 페이지에서 정보 가져오기 — 페이지에서 읽은 내용은 '후보'이고, 사용자가 직접 고른 것만 광고 작업에 들어간다.

판정·읽기는 `core/url_ingest.py` 가 하고 이 화면은 그리기만 한다.
- 외부 페이지에서 온 글은 전부 `html.escape` 한 뒤에만 HTML 로 낸다 (마크다운으로 그대로 내지 않는다: 이미지·링크가 열리지 않게).
- 사실 후보 체크박스는 기본이 '선택 안 함' 이다. 하나도 고르지 않으면 광고 작업에 채울 수 없다.
- 화면에는 전문 용어를 쓰지 않는다 (규칙 코드·내부 이름은 상세 기록에만).
세션 키는 모두 `urlimp_` 로 시작한다.
"""
from __future__ import annotations

import html
from typing import Callable

import streamlit as st

from core import config
from core import url_ingest as ingest
from core.schemas import ProductCategory

KEY = "urlimp_"
MODES = ("sample", "paste", "url")
MODE_LABEL = {"sample": "예시 페이지 고르기", "paste": "페이지 소스 붙여넣기", "url": "상품 주소로 가져오기"}
CATEGORY_LABEL = {
    ProductCategory.ECOMMERCE: "쇼핑(커머스)", ProductCategory.LOAN: "대출", ProductCategory.CARD: "카드", ProductCategory.INSURANCE: "보험",
    ProductCategory.INVESTMENT: "투자·펀드", ProductCategory.SAVINGS: "예금·적금", ProductCategory.CRYPTO: "가상자산", ProductCategory.APP: "앱·서비스",
    ProductCategory.EDUCATION: "교육", ProductCategory.HEALTH: "건강기능식품·의료", ProductCategory.REALESTATE: "부동산", ProductCategory.OTHER: "기타",
}
SOURCE_LABEL = {"jsonld": "상품 정보 데이터", "og": "페이지 소개 정보", "body": "본문"}
KIND_LABEL = {"price": "가격", "condition": "조건", "description": "설명", "attribute": "정보"}
PLACEHOLDER_CATEGORY = None
OVERFLOW = "overflow-wrap:anywhere;word-break:break-all"   # 긴 주소·붙여 쓴 글이 휴대폰 화면(390px)에서 가로로 넘치지 않게


def _esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _note(text: str, cls: str = "pf-muted") -> None:
    st.markdown(f'<div class="{cls}" style="{OVERFLOW}">{_esc(text)}</div>', unsafe_allow_html=True)


def _set_mode(mode: str) -> None:        # 버튼 콜백에서만 위젯 값을 바꿀 수 있다
    st.session_state[KEY + "mode"] = mode


def _clear_widget_state() -> None:
    ss = st.session_state
    for k in [k for k in ss if isinstance(k, str) and k.startswith((KEY + "pick_", KEY + "edit_", KEY + "name_", KEY + "cat_", KEY + "landing_"))]:
        del ss[k]


def _read(mode: str, sample: str, pasted: str, url: str) -> None:
    ss = st.session_state
    _clear_widget_state()
    ss[KEY + "nonce"] = ss.get(KEY + "nonce", 0) + 1
    for k in ("error", "applied", "draft", "source"):
        ss.pop(KEY + k, None)
    try:
        if mode == "sample":
            draft = ingest.ingest_url(f"{ingest.SAMPLE_PREFIX}{sample}")
            ss[KEY + "source"] = "예시 페이지 (이 프로젝트가 만든 가짜 페이지)"
        elif mode == "paste":
            draft = ingest.ingest_html(pasted, plain_text_ok=True)
            ss[KEY + "source"] = "붙여넣은 내용"
        else:
            draft = ingest.ingest_url(url.strip())
            ss[KEY + "source"] = "입력한 주소"
    except ingest.FetchError as e:
        ss[KEY + "error"] = e.user_message
        return
    ss[KEY + "draft"] = draft


def _flag_messages(draft: ingest.ProductDraft) -> list[tuple[str, str]]:
    """(수준, 쉬운 말). 수준: warning · info."""
    F = ingest.DraftFlag
    out: list[tuple[str, str]] = []
    rejected = draft.rejected_text
    if F.INJECTION_SUSPECTED in draft.flags:
        if any(r.why == "instruction" and r.where == "hidden" for r in rejected):
            out.append(("warning", "화면에 보이지 않는 글에서 AI 에게 내리는 지시처럼 보이는 문장을 찾아 사용하지 않았습니다."))
        if any(r.why == "instruction" and r.where != "hidden" for r in rejected) or not rejected:
            out.append(("warning", "페이지 본문이나 정보 칸에서 AI 에게 내리는 지시처럼 보이는 문장을 찾아 사용하지 않았습니다."))
    if F.HIDDEN_TEXT_FOUND in draft.flags and not any(r.why == "instruction" and r.where == "hidden" for r in rejected):
        out.append(("info", "화면에 보이지 않는 글이 있어 사용하지 않았습니다."))
    if F.PRICE_CONFLICT in draft.flags:
        out.append(("warning", "가격이 페이지 안에서 서로 다르게 적혀 있습니다. 아래 후보에서 맞는 가격을 직접 골라 주세요."))
    if F.TRUNCATED in draft.flags:
        out.append(("info", "페이지가 너무 커서 앞부분만 읽었습니다."))
    if F.LOW_CONTENT in draft.flags:
        out.append(("info", "페이지에 글이 적어 읽은 내용이 부족할 수 있습니다."))
    return out


def _show_notes(draft: ingest.ProductDraft) -> None:
    for level, text in _flag_messages(draft):
        (st.warning if level == "warning" else st.info)(text)
    if draft.rejected_text:
        with st.expander(f"사용하지 않은 글 보기 ({len(draft.rejected_text)}개)"):
            st.caption("아래 글은 광고 사실로 쓰지 않았습니다. 확인용으로만 보여 드립니다.")
            for r in draft.rejected_text:
                what = "AI 에게 내리는 지시처럼 보이는 문장" if r.why == "instruction" else "화면에 보이지 않는 글"
                st.markdown(f'<div class="pf-reason" style="{OVERFLOW}"><span class="pf-muted">{what}</span><br>{_esc(r.text)}</div>', unsafe_allow_html=True)


def _price_block(draft: ingest.ProductDraft) -> None:
    if draft.price is not None:
        st.markdown(f'<div class="pf-kv">읽은 가격: <b>{_esc(draft.price.text)}</b></div>', unsafe_allow_html=True)
        _note("가격은 광고에 그대로 쓰이므로, 아래 후보에서 가격 항목을 체크해 직접 확인해 주세요.")
    elif draft.price_candidates:
        shown = " / ".join(p.text for p in draft.price_candidates)
        st.markdown(f'<div class="pf-kv">가격을 하나로 정하지 못했습니다: <b>{_esc(shown)}</b></div>', unsafe_allow_html=True)
    else:
        _note("가격을 찾지 못했습니다.")


def _facts_block(draft: ingest.ProductDraft, nonce: int) -> tuple[list[str], dict[str, str], bool]:
    """(고른 id, 고친 글, 비어 있는 글이 있는가)."""
    ss = st.session_state
    st.markdown(f"### 광고에 쓸 수 있는 사실 후보 ({len(draft.facts)}개)")
    st.caption("페이지에서 읽은 내용이라 아직 확인되지 않았습니다. 맞다고 확인한 것만 체크하세요 — 아무것도 체크하지 않으면 광고 작업에 채울 수 없습니다. 글은 고칠 수 있습니다.")
    if not draft.facts:
        st.info("사실 후보를 찾지 못했습니다. 페이지 소스나 상품 설명을 직접 붙여 넣어 보세요.")
    picked: list[str] = []
    edits: dict[str, str] = {}
    blank = False
    for i, f in enumerate(draft.facts, start=1):
        with st.container(border=True):
            on = st.checkbox(f"후보 {i} · {KIND_LABEL.get(f.kind, '정보')} · {SOURCE_LABEL.get(f.source, '본문')}", key=f"{KEY}pick_{nonce}_{f.id}")
            text = st.text_input(f"후보 {i} 내용", value=f.text, max_chars=ingest.FACT_MAX, key=f"{KEY}edit_{nonce}_{f.id}", label_visibility="collapsed")
            _note(f"읽은 곳: {f.evidence_snippet}")
        if on:
            picked.append(f.id)
            edits[f.id] = text
            blank = blank or not text.strip()
    return picked, edits, blank


def _images_block(draft: ingest.ProductDraft) -> None:
    if not draft.image_urls:
        return
    st.markdown("### 이미지 후보")
    st.caption("이미지는 가져오지 않고 주소만 보여 드립니다." + (" 이 브라우저 실행판에서는 이미지를 가져올 수 없습니다." if config.IN_BROWSER else ""))
    st.markdown("".join(f'<div class="pf-muted" style="{OVERFLOW}">{_esc(u)}</div>' for u in draft.image_urls), unsafe_allow_html=True)


def _payload(pf, draft: ingest.ProductDraft) -> dict:
    return {"product_name": pf.name, "category": pf.category.value, "facts": [f.text for f in pf.facts], "fact_lines": [f"{f.id}: {f.text}" for f in pf.facts],
            "landing_text": pf.raw_description, "image_urls": list(draft.image_urls), "landing_url": pf.landing_url, "product": pf, "source": "url_import"}


def _result(draft: ingest.ProductDraft, on_apply: Callable[[dict], None]) -> None:
    ss = st.session_state
    nonce = ss.get(KEY + "nonce", 0)
    if ss.get(KEY + "source"):
        st.caption(f"읽은 곳: {ss[KEY + 'source']}")
    if draft.status != ingest.ReadStatus.OK:
        st.warning(draft.reason or "이 페이지는 읽지 못했습니다.")
        if draft.candidates:
            st.markdown("**페이지에서 발견된 상품**")
            st.markdown("".join(f'<div class="pf-reason" style="{OVERFLOW}">{_esc(c)}</div>' for c in draft.candidates), unsafe_allow_html=True)
        _show_notes(draft)
        st.button("붙여넣기로 바꾸기", key=KEY + "to_paste", on_click=_set_mode, args=("paste",))
        return
    st.success("페이지에서 상품 정보를 읽었습니다. 아래 내용은 아직 확인되지 않은 후보입니다.")
    _show_notes(draft)
    name = st.text_input("상품 이름", value=draft.name, max_chars=ingest.TITLE_MAX, key=f"{KEY}name_{nonce}")
    options = ([PLACEHOLDER_CATEGORY] if draft.needs_category_confirmation else []) + list(CATEGORY_LABEL)
    index = 0 if draft.needs_category_confirmation else options.index(draft.category_guess)
    category = st.selectbox("업종", options, index=index, key=f"{KEY}cat_{nonce}", format_func=lambda c: "업종을 골라 주세요" if c is None else CATEGORY_LABEL[c])
    if draft.needs_category_confirmation:
        st.caption("업종을 확신하지 못했습니다. 업종에 따라 검사 기준이 달라지니 직접 골라 주세요.")
    else:
        st.caption("읽은 내용으로 추정한 업종입니다. 맞는지 확인하세요 — 업종에 따라 검사 기준이 달라집니다.")
    _price_block(draft)
    picked, edits, blank = _facts_block(draft, nonce)
    with st.expander("랜딩 문구 보기·고치기"):
        st.caption("광고 문구가 상품 페이지와 맞는지 확인할 때 쓰는 본문 요약입니다. 화면에 보이지 않는 글과 지시처럼 보이는 문장은 빠져 있습니다.")
        landing = st.text_area("랜딩 문구", value=draft.landing_text, height=160, max_chars=ingest.LANDING_MAX, key=f"{KEY}landing_{nonce}", label_visibility="collapsed")
    _images_block(draft)
    missing = []
    if not picked:
        missing.append("사실 후보를 하나 이상 체크하세요.")
    if blank:
        missing.append("체크한 후보 중 글이 비어 있는 것이 있습니다.")
    if category is None:
        missing.append("업종을 골라 주세요.")
    if not name.strip():
        missing.append("상품 이름을 입력하세요.")
    clicked = st.button("선택한 사실로 광고 작업 채우기", type="primary", disabled=bool(missing), key=KEY + "apply")
    for m in missing:
        st.caption(m)
    if clicked:
        try:
            pf = ingest.confirm(draft, picked, edits, name=name, category=category, landing_text=landing)
        except ingest.DraftError as e:
            st.error(str(e))
            return
        payload = _payload(pf, draft)
        ss[KEY + "applied"] = {"n": len(pf.facts), "name": pf.name}      # on_apply 안에서 st.rerun() 을 불러도 완료 표시가 남도록 먼저 기록한다
        on_apply(payload)
    if ss.get(KEY + "applied"):
        st.success(f"선택한 사실 {ss[KEY + 'applied']['n']}개로 광고 작업을 채웠습니다. 광고 작업 화면에서 한 번 더 확인하세요.")


def render(on_apply: Callable[[dict], None]) -> None:
    """상품 페이지 읽기 화면. `on_apply(dict)` 는 사용자가 '선택한 사실로 광고 작업 채우기' 를 눌렀을 때 한 번 불린다.

    dict 키: product_name · category(업종 값 문자열) · facts(확정한 사실 글 목록) · fact_lines(["F1: …"]) · landing_text · image_urls · landing_url · product(ProductFacts) · source.
    확정되지 않은 후보는 어떤 키에도 들어가지 않는다."""
    ss = st.session_state
    st.markdown("## 상품 페이지에서 정보 가져오기")
    st.markdown("상품 주소나 페이지 소스에서 이름·가격·설명을 읽어 광고 작업에 쓸 **후보**를 만듭니다. 읽은 내용은 페이지를 만든 사람이 쓴 글이며 아직 확인되지 않았습니다 — "
                "아래에서 직접 골라 확인한 것만 광고 작업에 들어갑니다.")
    mode = st.radio("가져오는 방법", MODES, key=KEY + "mode", format_func=lambda m: MODE_LABEL[m] + (" (이 실행판에서는 사용할 수 없음)" if m == "url" and config.IN_BROWSER else ""))
    sample = pasted = url = ""
    can_read = True
    if mode == "sample":
        samples = ingest.SampleFetcher().list_samples()
        names = [n for n, _ in samples]
        labels = dict(samples)
        sample = st.selectbox("예시 페이지", names, key=KEY + "sample", format_func=lambda n: labels.get(n, n)) if names else ""
        st.caption("예시 페이지는 이 프로젝트가 만든 가짜 페이지입니다. 실제 쇼핑몰이 아니며, 인터넷에 접속하지 않습니다.")
        can_read = bool(sample)
    elif mode == "paste":
        pasted = st.text_area("페이지 소스 또는 상품 설명", height=160, max_chars=200_000, key=KEY + "paste",
                              help="브라우저에서 페이지 소스 보기로 복사한 HTML 이나, 상품 설명 글을 그대로 붙여 넣을 수 있습니다.")
        can_read = bool(pasted.strip())
    else:
        url = st.text_input("상품 페이지 주소", placeholder="https://", max_chars=ingest.MAX_URL_LEN, key=KEY + "url", disabled=config.IN_BROWSER)
        if config.IN_BROWSER:
            st.info("이 브라우저 실행판에서는 주소로 가져올 수 없습니다. 예시 페이지를 고르거나 페이지 소스를 붙여 넣어 주세요.")
        else:
            st.caption("공개된 웹 페이지(http, https)만 읽습니다. 회사 안쪽 주소나 로그인이 필요한 페이지는 읽지 않습니다. 가져오는 데는 최대 8초가 걸립니다.")
        can_read = bool(url.strip()) and not config.IN_BROWSER
    if st.button("읽어 오기", key=KEY + "read", disabled=not can_read):
        _read(mode, sample, pasted, url)
    if ss.get(KEY + "error"):
        st.warning(ss[KEY + "error"])
    draft = ss.get(KEY + "draft")
    if draft is not None:
        _result(draft, on_apply)
