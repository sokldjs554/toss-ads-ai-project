"""소재 화면 — 배너 시안 만들기, 이미지 검사.

판정·검사 로직은 전부 core/banner.py · agents/vision.py · guardrails/image_checks.py · agents/creative_review.py 의 것을 부르고, 여기서는 그리기만 한다.
세션 상태 키는 모두 `img_` 로 시작한다. 올린 이미지는 이 세션의 메모리에서만 다루고 디스크에 쓰지 않으며, 처리 기록에는 해시·크기만 남긴다.
"""
from __future__ import annotations

import base64
import html
from typing import Iterable, Optional

import streamlit as st

from agents.creative_review import CombinedDecision, combine
from agents.vision import ImageReview, review_image
from core.banner import PALETTES, TEMPLATES, Candidate, recommend
from core.config import IN_BROWSER
from core.images import MAX_RASTER_BYTES, MAX_SVG_BYTES, ImageRejection, load_image, safe_name
from core.schemas import AdCopy, Decision, Fact, ProductFacts
from guardrails import image_checks as ic
from ui import theme
from ui.components import download_or_show
from ui.funding import funded_by_server, server_cap, usd_text
from ui.labels import DECISION_HELP

PREVIEW_MAX_BYTES = 1_500_000
UPLOAD_TYPES = ["png", "jpg", "jpeg", "gif", "webp", "svg"]
SOURCE_TEXT = {
    "svg": "SVG 의 글자 요소를 코드가 직접 읽었습니다 (모델을 부르지 않았습니다).",
    "model": "모델이 이미지에서 읽은 글입니다. 모델이 글자를 빠뜨리거나 잘못 읽었을 수 있습니다.",
    "declared": "이미지에서 읽은 글이 아니라 직접 적어 주신 글입니다. 이미지는 읽지 못했습니다.",
    "none": "읽은 글이 없습니다.",
}
UNREAD_MSG = "이미지 속 글을 읽지 못해 통과로 처리하지 않습니다. 담당자가 이미지를 직접 확인해야 합니다."
TAG_LABEL = {t.tag: t.label for t in ic.IMAGE_TAG_RULES.values()}
SIZE_LABEL = {"landscape": "가로형", "square": "정사각", "strip": "띠형"}


def _k(key: str) -> str:
    """세션 상태 키는 img_ 로 시작한다."""
    return key if key.startswith("img_") else f"img_{key}"


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _img_html(src: str, alt: str) -> str:
    """<img> 로 보여 준다 — <img> 안의 SVG 는 스크립트를 실행하지 못하고 바깥 자원을 불러오지 않는다. 가로 넘침이 없도록 폭을 화면에 맞춘다."""
    return (f'<img alt="{html.escape(alt, quote=True)}" src="{src}" '
            'style="display:block;max-width:100%;height:auto;border:1px solid #E5E8EB;border-radius:10px;background:#fff"/>')


def _facts_and_categories(facts, categories) -> tuple[list[Fact], set[str]]:
    cats = {str(getattr(c, "value", c)) for c in (categories or ())}
    out: list[Fact] = []
    if isinstance(facts, ProductFacts):
        out = list(facts.facts)
        cats.add(facts.category.value)
    else:
        for f in facts or ():
            out.append(f if isinstance(f, Fact) else Fact.model_validate(f))
    return out, cats


def _score_line(label: str, value: str, ok: bool) -> str:
    mark = "통과" if ok else "확인"
    color = "#0B6B37" if ok else "#8A5A00"
    return (f'<div class="pf-kv" style="display:flex;flex-wrap:wrap;gap:4px 8px;justify-content:space-between"><span>{html.escape(label)}</span>'
            f'<span><b>{html.escape(value)}</b> <span style="color:{color};font-weight:700">{mark}</span></span></div>')


# ---------------------------------------------------------------------------
# 배너 시안
# ---------------------------------------------------------------------------
def render_banner_studio(ad_copy_dict_or_AdCopy, facts, key: str = "img_banner", *, categories: Optional[Iterable[str]] = None) -> None:
    """광고 문구로 배너 시안 3개를 만들어 점수·이유와 함께 보여 준다. 이미지 생성 모델은 쓰지 않는다 — 글을 정해진 틀에 배치한 SVG 다."""
    k = _k(key)
    copy = ad_copy_dict_or_AdCopy if isinstance(ad_copy_dict_or_AdCopy, AdCopy) else AdCopy.model_validate(ad_copy_dict_or_AdCopy)
    flist, cats = _facts_and_categories(facts, categories)
    st.markdown("### 배너 시안")
    st.markdown("광고 문구를 그대로 글로 배치한 배너 시안입니다. 이미지를 새로 그리지 않고, 크기·배치·색 조합 중 **글이 잘 읽히는** 것을 점수로 골랐습니다. "
                "점수는 이 프로젝트가 정한 읽기 쉬움 기준(글자 대비·글자 크기·여백·줄 수)으로 센 값이며 광고 성과나 실제 플랫폼 심사 결과가 아닙니다.")
    has_disc = bool([d for d in copy.disclosures if d.strip()])
    with_disc = st.checkbox("필수 안내 문구도 배너에 넣기", value=True, key=f"{k}_disc", disabled=not has_disc,
                            help="안내 문구가 배너에서 잘리거나 가려지면 점수가 낮아지고 경고가 뜹니다. 안내가 문구란에 따로 들어가는 광고라면 끄세요.")
    try:
        cands = recommend(copy, 3, include_disclosures=with_disc)
    except ValueError:
        st.info("배너에 넣을 글이 없습니다. 제목이나 본문을 먼저 입력하세요.")
        return
    cols = st.columns(len(cands))
    for col, cd in zip(cols, cands):
        with col:
            _banner_card(cd, k)
    over = [cd for cd in cands if cd.banner.fit_violation]
    if len(over) == len(cands):
        st.warning("세 시안 모두 글이 칸에 들어가지 않습니다. 제목과 본문을 줄이면 가장 작은 글자 크기에서도 들어갑니다.")
    # 배너의 글은 문구 그대로다 — 코드가 SVG 의 글자를 직접 읽어 같은 검사 기준을 한 번 더 적용한다 (글이 바뀌어 들어가지 않았는지)
    asset = load_image(cands[0].banner.svg.encode("utf-8"), "banner.svg")
    if isinstance(asset, ImageRejection):
        st.error(asset.message)
        return
    rv = review_image(asset, None, None, facts=flist, categories=cats, disclosure_copy=None)
    with st.expander("배너 속 글을 같은 검사 기준으로 다시 확인한 결과"):
        st.markdown(theme.badge(rv.decision), unsafe_allow_html=True)
        for r in rv.reasons:
            st.markdown(f"<div class='pf-reason'>{html.escape(r)}</div>", unsafe_allow_html=True)
        st.caption("배너의 글은 광고 문구와 같으므로 이 결과는 문구 검사 결과와 같은 기준입니다. 점검 통과는 실제 플랫폼 심사 결과가 아닙니다.")


def _banner_card(cd: Candidate, k: str) -> None:
    b, sc = cd.banner, cd.score
    with st.container(border=True):
        title = f"후보 {cd.rank} · {SIZE_LABEL.get(cd.size_key, cd.size_key)} {b.size.width}×{b.size.height} · {TEMPLATES[cd.template_key]} · {PALETTES[cd.palette_key].label}"
        st.markdown(f"**{html.escape(title)}**")
        alt = f"배너 시안: {' '.join(x for x in (b.block('title').text if b.block('title') else '', b.block('body').text if b.block('body') else '') if x)[:120]}"
        st.markdown(_img_html(_data_uri(b.svg.encode("utf-8"), "image/svg+xml"), alt), unsafe_allow_html=True)
        st.markdown(theme.stat("읽기 쉬움 점수", f"{sc.percent}점", "100점 만점 · 이 프로젝트가 정한 기준" + (" · 문제가 있어 점수에 상한이 걸림" if sc.capped else "")), unsafe_allow_html=True)
        for w in sc.warnings:
            st.warning(w.text)
        for r in cd.reasons:
            if not any(r == w.text for w in sc.warnings):
                st.markdown(f"<div class='pf-reason'>{html.escape(r)}</div>", unsafe_allow_html=True)
        with st.expander("점수 자세히"):
            for it in sc.items:
                if it.applicable:
                    st.markdown(_score_line(it.label, it.value_text, it.score >= 0.8), unsafe_allow_html=True)
                    st.caption(it.detail)
            st.caption("글자 폭은 글꼴 없이 추정한 값이라 실제 글꼴에서는 줄바꿈이 달라질 수 있습니다. 배너 크기 규격은 이 프로젝트가 가정한 값이며 실제 광고 콘솔의 규격과 다를 수 있습니다.")
        download_or_show("이 배너 SVG 내려받기", b.svg.encode("utf-8"), f"banner_{cd.size_key}_{cd.template_key}_{cd.palette_key}.svg", "image/svg+xml", f"{k}_dl_{cd.rank}")


# ---------------------------------------------------------------------------
# 이미지 검사
# ---------------------------------------------------------------------------
def _uploaded_file(k: str):
    """업로더 위젯을 그리고 올린 파일(없으면 None)을 돌려준다. 시험에서 바꿔 끼울 수 있도록 따로 둔 작은 함수다."""
    return st.file_uploader("검사할 이미지", type=UPLOAD_TYPES, key=f"{k}_upload",
                            help="PNG · JPEG · GIF · WEBP 는 5MB, SVG 는 1MB 까지. 올린 파일은 이 세션의 메모리에서만 다루고 저장하지 않습니다.")


def render_image_check(make_settings, router_factory, server_status: dict, *, facts=(), categories: Iterable[str] = (), key: str = "img_check",
                       text_decision: Optional[Decision] = None, ad_copy: Optional[AdCopy] = None) -> None:
    """이미지를 올리면 안전하게 받아들이는지 점검하고, 속 글자를 읽어 같은 검사 기준으로 심사한다.

    모델이 이미지를 읽는 것은 서버 키나 사용자 키가 있을 때만이다. 없으면 '읽지 못함' 이고 통과로 처리하지 않는다.
    text_decision 을 넘기면 문구 심사 결과와 합친 종합 판정도 보여 준다.
    """
    ss = st.session_state
    k = _k(key)
    flist, cats = _facts_and_categories(facts, categories)
    st.markdown("### 이미지 검사")
    st.markdown("올린 이미지 **속 글자**도 광고 문구와 같은 기준으로 검사합니다. 이미지에 ‘원금 보장’ 같은 말을 그림으로 넣으면 문구 검사가 보지 못하기 때문입니다. "
                "PNG · JPEG · GIF · WEBP(5MB 까지)와 SVG(1MB 까지)를 받습니다.")
    up = _uploaded_file(k)
    if up is None:
        st.info("이미지를 올리면 안전한 파일인지 점검한 뒤 속 글자를 읽어 검사합니다.")
        return
    limit = MAX_SVG_BYTES if str(up.name).lower().endswith(".svg") else MAX_RASTER_BYTES
    if getattr(up, "size", 0) and up.size > limit:   # 읽기 전에 거른다
        st.error(f"파일이 너무 큽니다 ({up.size / 1048576:.1f}MB). {'SVG 는 1MB' if limit == MAX_SVG_BYTES else '이미지는 5MB'} 까지 올릴 수 있습니다.")
        return
    asset = load_image(up.getvalue(), up.name, claimed_mime=getattr(up, "type", None))
    if isinstance(asset, ImageRejection):
        st.error(asset.message)
        _log(k, {"result": "거부", "reason": asset.code, "bytes": getattr(up, "size", 0)})
        return
    st.markdown(f"<div class='pf-muted'>{html.escape(safe_name(up.name))} · {html.escape(asset.fmt.upper())} · {asset.width}×{asset.height}px · {asset.bytes_len / 1024:.0f}KB</div>", unsafe_allow_html=True)
    if asset.bytes_len <= PREVIEW_MAX_BYTES:
        st.markdown(_img_html(_data_uri(asset.data, asset.mime), f"올린 이미지 ({asset.fmt.upper()} {asset.width}×{asset.height})"), unsafe_allow_html=True)
    else:
        st.caption("파일이 커서 미리보기는 생략합니다.")
    for n in asset.sanitized_notes:
        st.info(f"안전을 위해 파일을 고쳤습니다 — {n}")

    declared = ""
    if asset.kind == "raster":
        declared = st.text_area("이미지 속에 적힌 글 (선택)", key=f"{k}_declared", height=90,
                                help="이미지에 들어 있는 글을 직접 적어 주세요. 모델이 읽은 글과 맞춰 보고, 서로 맞을 때만 ‘점검 통과’ 가 될 수 있습니다.")
        st.caption("직접 적은 글이 있으면 모델이 읽은 글과 대조합니다. 적지 않으면 모델이 읽은 글에서 문제가 없어도 모델이 빠뜨린 글자가 있는지 알 수 없어 ‘담당자 확인 필요’ 로 남깁니다.")
    else:
        declared = st.text_area("이미지 속에 적힌 글 (선택)", key=f"{k}_declared", height=70, help="적으면 SVG 에서 읽은 글과 맞춰 봅니다. 선택 사항입니다.")

    ukeys = ss.get("user_keys", ("", ""))
    settings = make_settings() if IN_BROWSER else make_settings(*ukeys, **ss.get("settings_overrides", {}))
    mode = settings.mode.value
    uses_model = asset.kind == "raster"
    live = uses_model and mode != "demo"
    ready = True
    if uses_model and mode == "demo":
        st.info("예시 실행에서는 이미지 속 글을 읽지 못합니다 — 키가 있을 때만 모델이 읽습니다. 위에 글을 직접 적으면 그 글만 같은 기준으로 검사합니다 (이미지를 읽은 것은 아닙니다).")
    elif asset.kind == "svg":
        st.caption("SVG 는 글자 요소를 코드가 직접 읽으므로 모델을 부르지 않습니다.")
    if live:
        funded = funded_by_server(server_status, *ukeys)
        cap = server_cap(server_status) if funded else None
        if cap is not None and cap <= 0:
            st.error("서버 운영자가 실행당 비용 상한을 $0 으로 정해 두어, 서버 키로는 이미지를 모델로 읽을 수 없습니다. 직접 쓸 API 키를 넣으면 그 키로 읽을 수 있습니다.")
            ready = False
        else:
            st.warning("모델을 부릅니다(보통 1번) — 이미지가 모델 제공자(OpenAI 또는 Anthropic)에게 전송됩니다."
                       + (f" 서버 키로 도는 호출은 서버가 정한 상한 {usd_text(cap)} 에서 멈춥니다." if cap is not None else " 직접 넣은 키로 도는 호출이라 비용은 그 계정에 청구됩니다."), icon="💳")
            sig = f"{asset.sha256}|{mode}"
            if ss.get(f"{k}_consent_sig") != sig:      # 동의는 지금 보이는 이 이미지에 대한 것이다 — 다른 이미지를 올리면 다시 확인한다
                ss[f"{k}_consent_sig"] = sig
                ss[f"{k}_consent"] = False
            ready = st.checkbox("이 이미지를 모델 제공자에게 보내는 것에 동의합니다", key=f"{k}_consent")
    run = True if not live else st.button("이미지 검사", type="primary", disabled=not ready, key=f"{k}_run")

    ck = f"{asset.sha256}|{declared.strip()}|{mode}|{sorted(cats)}|{[(f.id, f.text) for f in flist]}"
    cache = ss.get(f"{k}_result")
    if run and ready and (not cache or cache.get("ck") != ck):
        router = router_factory(settings) if uses_model else None
        with st.spinner("이미지를 검사하는 중…"):
            rv = review_image(asset, router, settings, declared_text=declared, facts=flist, categories=cats, disclosure_copy=ad_copy)
        cache = {"ck": ck, "review": rv}
        ss[f"{k}_result"] = cache
        _log(k, {"result": rv.decision.value, **rv.log_fields()})
    if not cache or cache.get("ck") != ck:
        if live and ready:
            st.caption("‘이미지 검사’ 를 누르면 모델이 이미지를 읽습니다.")
        return
    _render_review(cache["review"], text_decision, k)
    _render_log(k)


def _log(k: str, entry: dict) -> None:
    """처리 기록 — 해시·크기·판정만 남긴다 (이미지 바이트·읽은 글·파일 이름은 남기지 않는다)."""
    log = st.session_state.setdefault(f"{k}_log", [])
    log.append(entry)
    del log[:-20]


def _render_log(k: str) -> None:
    log = st.session_state.get(f"{k}_log") or []
    if log:
        with st.expander("처리 기록 (해시·크기만 남깁니다)"):
            st.dataframe([{kk: vv for kk, vv in e.items()} for e in reversed(log)], width="stretch", hide_index=True)


def _render_review(rv: ImageReview, text_decision: Optional[Decision], k: str) -> None:
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        with h1:
            st.markdown("**이미지 검사 결과**")
        with h2:
            st.markdown(f"<div style='text-align:right'>{theme.badge(rv.decision, big=True)}</div>", unsafe_allow_html=True)
        st.caption(DECISION_HELP[rv.decision])
        if rv.unreadable:
            st.warning(UNREAD_MSG)
        for r in rv.reasons:
            if rv.unreadable and r == UNREAD_MSG:      # 위 경고와 같은 문장을 되풀이하지 않는다
                continue
            st.markdown(f"<div class='pf-reason'>{html.escape(r)}</div>", unsafe_allow_html=True)
        if text_decision is not None:
            comb: CombinedDecision = combine(text_decision, rv)
            st.markdown("**문구와 이미지를 합친 판정**")
            st.markdown(f"{theme.badge(comb.decision)}", unsafe_allow_html=True)
            st.caption("둘 중 더 엄격한 쪽을 따릅니다. 이미지를 읽지 못했으면 문구가 통과여도 담당자 확인이 필요합니다.")
    st.markdown("**읽은 글**")
    if rv.segments:
        st.caption(SOURCE_TEXT.get(rv.text_source, ""))
    if rv.notes:
        st.caption(f"읽기 메모: {rv.notes}")
    if not rv.segments:
        st.markdown("<div class='pf-muted'>읽은 글이 없습니다.</div>", unsafe_allow_html=True)
    for s in rv.segments:
        tag = "" if s.used else theme.chip("지시문처럼 보여 사용하지 않음")
        st.markdown(f"<div class='pf-reason'>{tag}{html.escape(s.text)}</div>", unsafe_allow_html=True)
    if rv.truncated:
        st.warning("읽은 글이 너무 길어 일부만 검사했습니다.")
    if rv.cross_check == "mismatch":
        st.warning("이미지에는 직접 적어 주신 글에 없는 문구가 더 있습니다. 심사를 피하려고 이미지에만 넣은 문구일 수 있어 확인이 필요합니다.")
    elif rv.cross_check == "consistent":
        st.success("모델이 읽은 글과 직접 적어 주신 글이 일치합니다.")
    elif rv.cross_check == "weak":
        st.info("직접 적어 주신 글 중 일부가 이미지에서 읽히지 않아 대조가 충분하지 않습니다.")
    if rv.tags:
        st.markdown("**모델이 이미지에서 봤다고 한 것** " + " ".join(theme.chip(TAG_LABEL.get(t, t)) for t in rv.tags), unsafe_allow_html=True)
        st.caption("모델이 본 것은 코드로 확인할 수 없어 게재 불가의 근거로 쓰지 않고, 담당자가 확인하도록 표시만 합니다.")
    shown = [f for f in rv.findings if f.rule_id != "IMG-READ-01"]
    if shown:
        st.markdown("**확인된 항목**")
        for f in shown:
            st.markdown(f"<div class='pf-reason'>{theme.severity_chip(f.severity)} {html.escape(ic.finding_sentence(f))}</div>", unsafe_allow_html=True)
    for s in rv.skipped_checks:
        st.caption(s)
    with st.expander("상세 기록 — 규칙 코드와 위치"):
        st.dataframe([{"규칙": f.rule_id, "원래 규칙": getattr(f, "source_rule_id", ""), "등급": f.severity.value, "인용": f.quote[:80], "시작": f.start, "끝": f.end,
                       "글 출처": getattr(f, "text_source", "")} for f in rv.findings], width="stretch", hide_index=True)
        st.caption("위치(시작·끝)는 위에서 읽은 글을 줄바꿈으로 이은 글 기준이며 광고 문구의 위치가 아닙니다. 규칙은 모두 이 프로젝트가 정한 것이며 실제 플랫폼 규정이 아닙니다.")
    st.caption("이 결과는 이 프로젝트의 검사 기준에 따른 것으로 실제 플랫폼 심사가 아닙니다. 실제 비전 모델의 정확도는 측정하지 않았습니다.")
