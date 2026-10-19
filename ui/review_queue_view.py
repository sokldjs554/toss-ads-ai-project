"""검수 대기열 — 자동으로 확정하지 못한 광고를 사람이 보고 판정(라벨)을 남기는 곳.

담당자 확인으로 넘어간 광고를 대기열에 모으고, 라벨러가 판정·이유·문제가 된 구절을 남기면 코드가 검증한다(구절이 광고 문구에 그대로 있는지, 판정과 규칙이 모순되지 않는지).
라벨러가 둘 이상이면 일치도를 재고, 갈린 건은 제3자가 조정하고, 확정된 라벨은 평가셋 후보로 내려받는다.
이 화면은 그리기만 한다 — 점유·검증·일치도·내보내기는 `ops/` 가 한다. 세션 상태 키는 모두 `rq_` 로 시작한다.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Optional

import streamlit as st
from pydantic import ValidationError

from core.schemas import AdCopy, Decision, PipelineRun
from guardrails.policy import PolicyCatalog, get_catalog
from ops import dataset_tools as dtools
from ops.labeling import (
    GUIDELINE_VERSION,
    KAPPA_TARGET,
    AdjudicationRecord,
    Annotation,
    AnnotationRejected,
    LabelDecision,
    agreement_report,
)
from ops.prelabel import anchoring_report
from ops.review_queue import (
    CATEGORY_NAME_KO,
    ClaimLimitReached,
    DuplicateAnnotation,
    ItemState,
    NotAllowed,
    NotClaimed,
    QueueError,
    QueueFormatError,
    ReviewQueue,
    build_demo_queue,
)
from ui import theme
from ui.labels import DECISION_LABEL, RUN_MODE_LABEL

SCOPE_REAL, SCOPE_DEMO = "내 대기열", "합성 예시"
DECISION_OPTIONS = [LabelDecision.APPROVE, LabelDecision.REVISE, LabelDecision.REJECT]
GOLD_COUNT = 10
MAX_LIST_ROWS = 50


# ---------------------------------------------------------------------------
# 순수 보조 함수 (화면 밖에서도 테스트한다)
# ---------------------------------------------------------------------------
def decision_name(d: LabelDecision | str) -> str:
    return DECISION_LABEL[Decision(d.value if isinstance(d, LabelDecision) else d)]


def md_escape(s: str) -> str:
    """사용자·모델이 쓴 글을 마크다운·HTML 로 해석하지 않게 한다."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|~>])", r"\\\1", html.escape(str(s)))


def rule_titles(catalog: PolicyCatalog, ids: list[str]) -> str:
    return ", ".join(catalog.get(r).title if catalog.get(r) else r for r in ids) or "없음"


def span_candidates(copy: AdCopy, extra: list[str] | None = None, limit: int = 24) -> list[str]:
    """인용 후보: 제목·본문·버튼·안내 문구 한 줄 전체와 그것을 문장·쉼표로 자른 조각. 모두 광고 문구에 글자 그대로 있는 구절이다."""
    out: list[str] = []

    def add(x: str) -> None:
        x = x.strip()
        if len(x) >= 2 and x not in out and x in copy.full_text():
            out.append(x)

    for x in extra or []:
        add(x)
    for text in [copy.headline, copy.body, copy.cta, *copy.disclosures]:
        if not text:
            continue
        add(text)
        for sent in re.split(r"(?<=[.!?。])\s+", text):
            add(sent)
            for part in re.split(r"(?<!\d)[,，]\s*|[,，](?!\d)\s*|·\s*", sent):    # 숫자 사이의 쉼표(5,000만원)는 자르지 않는다
                if len(part.strip()) >= 4:
                    add(part)
    return out[:limit]


def remaining_text(due: datetime, now: datetime) -> str:
    sec = (due - now).total_seconds()
    if sec < 0:
        return "마감 지남"
    if sec < 3600:
        return f"{int(sec // 60)}분 남음"
    return f"{sec / 3600:.0f}시간 남음"


def priority_text(p: float) -> str:
    level = "높음" if p >= 0.6 else ("보통" if p >= 0.3 else "낮음")
    return f"{level} ({p:.2f})"


def validation_messages(e: ValidationError) -> list[str]:
    """pydantic 오류를 화면용 문장으로 (영문 머리말을 걷어낸다)."""
    out = []
    for err in e.errors():
        msg = str(err.get("msg", ""))
        out.append(re.sub(r"^Value error, ", "", msg))
    return out


def _entries(source: Any) -> list[tuple[PipelineRun, str]]:
    """화면에 넘어온 실행 결과 → [(실행, 랜딩 페이지 글)]. 목록·콜러블·광고 작업의 세션 항목(dict)·PipelineRun 모두 받는다. 처리 기록(Tracer) 같은 그 밖의 것은 비운다."""
    if callable(source) and not isinstance(source, (list, tuple)):
        try:
            source = source()
        except Exception:  # noqa: BLE001 — 원천이 실패해도 화면은 뜬다
            source = []
    out: list[tuple[PipelineRun, str]] = []
    if not isinstance(source, (list, tuple)):
        return out
    for e in source:
        if isinstance(e, PipelineRun):
            out.append((e, ""))
        elif isinstance(e, dict) and isinstance(e.get("run"), PipelineRun):
            out.append((e["run"], str(e.get("landing", "") or "")))
    return out


def pending_human_review(queue: ReviewQueue, entries: list[tuple[PipelineRun, str]]) -> int:
    """아직 대기열에 없는 담당자 확인 보고서 수."""
    from ops.review_queue import make_id

    n = 0
    for run, _ in entries:
        for rep in run.reports:
            if rep.final_decision == Decision.HUMAN_REVIEW:
                try:
                    queue.get(make_id("run", run.run_id, rep.variant_id))
                except QueueError:
                    n += 1
    return n


# ---------------------------------------------------------------------------
# 세션 상태
# ---------------------------------------------------------------------------
def _queue() -> ReviewQueue:
    ss = st.session_state
    if "rq_queue" not in ss:
        ss["rq_queue"] = ReviewQueue(required_labels=int(ss.get("rq_required", 2)))
    return ss["rq_queue"]


def _demo_queue() -> Optional[ReviewQueue]:
    return st.session_state.get("rq_demo_queue")


def _flash(level: str, text: str) -> None:
    st.session_state["rq_msg"] = (level, text)


def _show_flash() -> None:
    msg = st.session_state.pop("rq_msg", None)
    if msg:
        level, text = msg
        {"error": st.error, "warning": st.warning, "success": st.success}.get(level, st.info)(text)


# ---------------------------------------------------------------------------
# 표시 조각
# ---------------------------------------------------------------------------
def _stats_row(q: ReviewQueue) -> None:
    s = q.stats()
    cols = st.columns(3)
    cols[0].markdown(theme.stat("라벨을 기다리는 광고", f"{s.waiting}건", f"의견이 갈려 조정을 기다리는 광고 {s.awaiting_adjudication}건"), unsafe_allow_html=True)
    cols[1].markdown(theme.stat("마감이 지난 광고", f"{s.sla_breached}건", "마감 시간은 이 프로젝트가 정한 값입니다"), unsafe_allow_html=True)
    wait = "—" if s.mean_wait_s is None else (f"{s.mean_wait_s / 60:.0f}분" if s.mean_wait_s < 7200 else f"{s.mean_wait_s / 3600:.1f}시간")
    cols[2].markdown(theme.stat("최근 24시간에 낸 라벨", f"{s.throughput_24h}건", f"처음 받기까지 평균 대기 {wait}"), unsafe_allow_html=True)


def _queue_table(q: ReviewQueue) -> None:
    rows = q.list_open()
    if not rows:
        return
    st.caption("아직 아무도 잡지 않은 광고입니다. 라벨 중인 광고는 아래 ‘지금 라벨할 광고’ 에 보입니다.")
    now = q.now()
    modes = {r.item_id: (q.get(r.item_id).verdict.run_mode if q.get(r.item_id).verdict else "") for r in rows}
    st.dataframe([{"순서": i + 1, "우선순위": priority_text(r.priority), "사유": r.reason_label, "마감": remaining_text(r.due_at, now),
                   "라벨": f"{r.labels}/{r.needed}", "광고 제목": r.headline[:40], "업종": CATEGORY_NAME_KO.get(r.category, r.category),
                   "자동 검사 방식": RUN_MODE_LABEL.get(modes[r.item_id], "코드 검사만 (모델 없음)" if modes[r.item_id] == "code_only" else "—")} for i, r in enumerate(rows[:MAX_LIST_ROWS])],
                 width="stretch", hide_index=True)
    if "demo" in modes.values():
        st.caption("‘예시 실행’ 으로 만든 광고가 있습니다. 그 광고의 자동 검사(제안 포함)는 실제 모델의 판단이 아니라 정해진 규칙대로 움직이는 대역의 출력입니다.")
    if len(rows) > MAX_LIST_ROWS:
        st.caption(f"{len(rows)}건 중 앞의 {MAX_LIST_ROWS}건만 보여 줍니다.")


def _confusion_table(conf: dict[str, dict[str, int]], a: str, b: str) -> list[dict]:
    rows = []
    for ra, row in conf.items():
        item = {f"{a} 라벨러 판정": decision_name(ra)}
        item.update({f"{b} 라벨러: {decision_name(c)}": n for c, n in row.items()})
        rows.append(item)
    return rows


def _agreement_panel(q: ReviewQueue, catalog: PolicyCatalog, *, synthetic: bool) -> None:
    st.markdown("### 라벨러 사이 일치도")
    rep = agreement_report(q.annotations())
    if not rep.measurable:
        st.info("라벨러가 한 명이라 일치도를 잴 수 없습니다. 같은 광고를 두 사람 이상이 따로 라벨해야 잴 수 있습니다." if len(rep.annotators) < 2 else rep.warnings[0])
        return
    o = rep.overall
    assert o is not None
    cols = st.columns(3)
    cols[0].markdown(theme.stat("판정이 같은 비율", f"{o.percent_agreement:.0%}", f"함께 라벨한 광고 {o.n_items}건"), unsafe_allow_html=True)
    if o.kappa is None:
        cols[1].markdown(theme.stat("일치도 (kappa)", "계산 불가", "한 가지 라벨만 쓰였습니다"), unsafe_allow_html=True)
    else:
        ci = f"95% 신뢰구간 {o.ci_low:.2f} ~ {o.ci_high:.2f}" if o.ci_low is not None else "신뢰구간을 낼 수 없음"
        cols[1].markdown(theme.stat("일치도 (kappa)", f"{o.kappa:.2f}", ci), unsafe_allow_html=True)
    cols[2].markdown(theme.stat("참고 해석", o.band_ko or "—", "관례적인 구간일 뿐 기준이 아닙니다"), unsafe_allow_html=True)
    for w in [*o.warnings, *rep.warnings]:
        st.warning(w, icon="⚠️")
    if o.below_target:
        st.warning(f"일치도가 이 프로젝트의 개정 기준(kappa {KAPPA_TARGET:.2f}, 가정한 값)보다 낮습니다. 라벨이 갈린 광고를 보고 가이드라인을 고칠지 검토하세요.", icon="📝")
    if synthetic:
        st.caption("합성 예시의 값입니다. 가상의 두 라벨러가 만든 데이터이므로 실제 라벨러 합의 수준을 보여 주지 않습니다.")
    st.caption(rep.note)
    if rep.primary_pair and rep.confusion:
        a, b = rep.primary_pair
        st.markdown(f"**판정 혼동 표** — 세로: {md_escape(a)}, 가로: {md_escape(b)}")
        st.dataframe(_confusion_table(rep.confusion, "첫째", "둘째"), width="stretch", hide_index=True)
    if rep.per_rule:
        with st.expander("규칙별 일치도"):
            st.caption("규칙마다 ‘이 규칙을 골랐는가’ 를 두 라벨러가 같게 답했는지 봅니다. 고른 건수가 적은 규칙은 값이 불안정합니다.")
            st.dataframe([{"규칙": catalog.get(r.rule_id).title if catalog.get(r.rule_id) else r.rule_id, "둘 다 선택": r.both, "한쪽만 선택": r.only_first + r.only_second,
                           "같게 답한 비율": f"{r.percent:.0%}", "kappa": "—" if r.kappa is None else f"{r.kappa:.2f}"} for r in rep.per_rule],
                         width="stretch", hide_index=True)


def _quality_panel(q: ReviewQueue) -> None:
    quals = q.quality()
    if not quals:
        return
    with st.expander("라벨러별 품질"):
        st.caption("검증용 정답은 가이드라인 작성자의 라벨입니다(독립 검증이 아닙니다). ‘주의’ 는 사람을 평가하는 것이 아니라 가이드라인·교육·표본을 점검하라는 신호입니다.")
        state = {"insufficient": "판단하기엔 표본 부족", "ok": "이상 없음", "watch": "주의"}
        st.dataframe([{"라벨러": x.annotator, "라벨 수": x.n_labeled,
                       "검증용 정확도": "—" if x.gold_accuracy is None else f"{x.gold_accuracy:.0%} ({x.gold_correct}/{x.n_gold})",
                       "평균 처리 시간": "—" if x.mean_seconds is None else f"{x.mean_seconds:.0f}초", "너무 빠른 제출": x.too_fast,
                       "확정 라벨과 다른 비율": "—" if x.deviation_rate is None else f"{x.deviation_rate:.0%} ({x.n_deviating}/{x.n_final})", "상태": state[x.status]} for x in quals],
                     width="stretch", hide_index=True)
        for x in quals:
            for n in x.notes:
                st.caption(f"{md_escape(x.annotator)}: {n}")
    ar = anchoring_report(q.annotations())
    if ar.n_shown + ar.n_hidden:
        with st.expander("자동 제안에 끌려가는지 (앵커링)"):
            st.markdown(f"자동 제안을 **보여 준** 라벨 {ar.n_shown}건 중 제안과 같은 판정 {('—' if ar.rate_shown is None else f'{ar.rate_shown:.0%}')}, "
                        f"**숨긴** 라벨 {ar.n_hidden}건 중 {('—' if ar.rate_hidden is None else f'{ar.rate_hidden:.0%}')}.")
            for w in ar.warnings:
                st.caption(w)
            st.caption(ar.note)


def _label_form(q: ReviewQueue, name: str, item_id: str, catalog: PolicyCatalog, hide: bool) -> None:
    ss = st.session_state
    view = q.view(item_id, name, hide_prelabel=hide)
    item = q.get(item_id)
    st.markdown("### 지금 라벨할 광고")
    meta = f"사유: {view.reason_label} · 마감 {remaining_text(view.due_at, q.now())} · 업종 {CATEGORY_NAME_KO.get(view.category, view.category)}"
    st.caption(meta)
    from ui.components import render_ad

    render_ad(view.ad_copy, None, catalog=catalog)          # 상품명은 보이지 않는다 — 출처가 다른 항목(검증용 등)과 모양이 갈리지 않게
    if view.facts:
        with st.expander("광고주가 제공한 상품 정보"):
            for f in view.facts:
                st.markdown(f"- {md_escape(f.text)}")
    if view.prelabel is not None:
        st.info(f"자동 검사의 제안: {decision_name(view.prelabel.decision)}"
                + (f" · 관련 규칙: {rule_titles(catalog, view.prelabel.rule_ids)}" if view.prelabel.rule_ids else "") + f". {view.prelabel.note}", icon="💡")
    elif view.prelabel_mode == "hidden":
        st.caption("자동 검사의 제안은 숨겨져 있습니다. 먼저 직접 판단한 뒤 필요하면 위의 ‘자동 제안 숨기기’ 를 꺼서 비교하세요.")
    rules = sorted((r for r in catalog.rules_for(set(item.categories) or {item.category})), key=lambda r: r.title)
    rule_ids = [r.id for r in rules]
    titles = {r.id: r.title for r in rules}
    cands = span_candidates(view.ad_copy, list(view.prelabel.cited_spans) if view.prelabel else None)
    nonce = ss.get("rq_nonce", 0)
    with st.form(key=f"rq_form_{nonce}"):
        decision = st.radio("판정", DECISION_OPTIONS, format_func=decision_name, key=f"rq_decision_{nonce}", horizontal=True, index=None,
                            help="애매하면 가장 보수적인(엄격한) 쪽을 고르고 이유에 적어 주세요. ‘담당자 확인’ 은 라벨이 아닙니다.")
        picked_rules = st.multiselect("이유가 된 규칙", rule_ids, format_func=lambda r: titles.get(r, r), key=f"rq_rules_{nonce}",
                                      default=[r for r in (view.prelabel.rule_ids if view.prelabel else []) if r in rule_ids],
                                      help="‘수정 필요’·‘게재 불가’ 일 때 하나 이상 고릅니다. 통과로 라벨하면서 게재 불가 등급 규칙을 고르면 저장되지 않습니다.")
        picked_spans = st.multiselect("문제가 된 구절 (광고 문구에서 고르기)", cands, key=f"rq_spans_{nonce}",
                                      default=[c for c in (view.prelabel.cited_spans if view.prelabel else []) if c in cands])
        free = st.text_input("또는 직접 입력 (광고 문구에 글자 그대로 있어야 합니다)", key=f"rq_span_free_{nonce}")
        why = st.text_area("이유", key=f"rq_why_{nonce}", height=90, max_chars=2000,
                           help="무엇이 문제인지(또는 왜 문제없는지) 한두 문장으로. 빠진 내용이 문제라면 무엇이 빠졌는지 적어 주세요.")
        submitted = st.form_submit_button("라벨 저장", key="rq_submit", type="primary")
    c1, c2 = st.columns(2)
    if c1.button("이 광고 반납하기", key="rq_release"):
        q.release(name, item_id)
        _flash("info", "광고를 반납했습니다.")
        st.rerun()
    if submitted:
        spans = list(picked_spans) + ([free.strip()] if free.strip() else [])
        if decision is None:
            st.error("판정을 먼저 골라 주세요.")
            return
        try:
            ann = Annotation(item_id=item_id, annotator=name, decision=decision, rule_ids=list(picked_rules), cited_spans=spans, rationale=why,
                             prelabel_mode=view.prelabel_mode, guideline_version=GUIDELINE_VERSION)
            res = q.submit(ann)
        except ValidationError as e:
            for m in validation_messages(e):
                st.error(m)
            return
        except AnnotationRejected as e:
            for m in e.issues:
                st.error(m)
            return
        except (NotClaimed, DuplicateAnnotation, QueueError) as e:
            st.error(str(e))
            return
        msg = {ItemState.LABELED: "라벨을 저장했습니다. 이 광고의 라벨이 확정됐습니다.", ItemState.DISAGREED: "라벨을 저장했습니다. 다른 라벨러와 판정이 갈려 조정을 기다립니다."}.get(
            res.state, "라벨을 저장했습니다. 다른 라벨러의 라벨을 기다립니다.")
        if res.warnings:
            msg += " 참고: " + " ".join(res.warnings)
        _flash("success", msg)
        ss["rq_nonce"] = nonce + 1
        st.rerun()


def _labeling_section(q: ReviewQueue, catalog: PolicyCatalog) -> None:
    ss = st.session_state
    st.markdown("## 라벨하기")
    saved = ss.setdefault("rq_saved", {})          # 다른 화면에 다녀오면 위젯 상태가 지워지므로, 설정은 위젯이 아닌 세션 값에도 적어 둔다
    c0, c1 = st.columns([3, 2])
    name = c0.text_input("라벨러 이름", value=saved.get("name", ""), key="rq_annotator", max_chars=40, placeholder="예: 홍길동",
                         help="이 세션 안에서만 쓰입니다. 같은 사람이 같은 광고를 두 번 라벨할 수 없고, 이름이 다른 두 사람이어야 일치도를 잴 수 있습니다.")
    hide = c1.toggle("자동 제안 숨기기", value=bool(saved.get("hide", True)), key="rq_hide_prelabel",
                     help="자동 검사의 제안을 보면 그 제안에 끌려갈 수 있습니다. 켜 두면 먼저 직접 판단하고, 보였는지 숨겼는지는 라벨에 기록됩니다.")
    saved["name"], saved["hide"] = name or "", bool(hide)
    name = (name or "").strip()
    if not name:
        st.info("라벨러 이름을 입력하면 광고를 받을 수 있습니다.")
        return
    try:
        held = q.held_by(name)
    except ValueError as e:
        st.error(str(e))
        return
    if held:
        if len(held) > 1:
            st.caption(f"{md_escape(name)} 님이 잡고 있는 광고 {len(held)}건 중 첫 번째입니다.")
        _label_form(q, name, held[0], catalog, bool(hide))
        return
    if st.button("다음 광고 받기", key="rq_claim", type="primary"):
        try:
            v = q.claim(name, hide_prelabel=bool(hide))
        except ClaimLimitReached as e:
            _flash("warning", str(e))
        except ValueError as e:
            _flash("error", str(e))
        else:
            if v is None:
                _flash("info", "지금 받을 수 있는 광고가 없습니다. 담당자 확인 건이 생기면 위에서 대기열로 보내 주세요.")
        st.rerun()


def _adjudication_section(q: ReviewQueue, catalog: PolicyCatalog) -> None:
    items = q.disagreements()
    if not items:
        return
    st.markdown("## 의견이 갈린 광고 조정")
    st.caption("라벨러의 판정이 갈린 광고는 제3의 조정자가 최종 라벨을 정하고 이유를 남깁니다. 그 광고를 라벨한 사람은 조정할 수 없습니다.")
    by_id = {it.id: it for it in items}
    pick = st.selectbox("조정할 광고", list(by_id), format_func=lambda i: f"{by_id[i].ad_copy.headline[:30]} ({', '.join(by_id[i].labelers())})", key="rq_adj_pick")
    it = by_id[pick]
    from ui.components import render_ad

    render_ad(it.ad_copy, None, catalog=catalog)
    st.dataframe([{"라벨러": a.annotator, "판정": decision_name(a.decision), "규칙": rule_titles(catalog, a.rule_ids), "이유": a.rationale} for a in it.annotations],
                 width="stretch", hide_index=True)
    rules = sorted(catalog.rules_for(set(it.categories) or {it.category}), key=lambda r: r.title)
    titles = {r.id: r.title for r in rules}
    cands = span_candidates(it.ad_copy)
    with st.form(key="rq_adj_form"):
        adj = st.text_input("조정자 이름", key="rq_adj_name", max_chars=40)
        dec = st.radio("최종 판정", DECISION_OPTIONS, format_func=decision_name, key="rq_adj_decision", horizontal=True, index=None)
        rids = st.multiselect("이유가 된 규칙", [r.id for r in rules], format_func=lambda r: titles.get(r, r), key="rq_adj_rules")
        spans = st.multiselect("문제가 된 구절", cands, key="rq_adj_spans")
        reason = st.text_area("조정 이유 (10자 이상)", key="rq_adj_reason", height=80, max_chars=2000)
        go = st.form_submit_button("최종 라벨 확정", key="rq_adj_submit", type="primary")
    if go:
        if dec is None:
            st.error("최종 판정을 골라 주세요.")
            return
        try:
            q.adjudicate(AdjudicationRecord(item_id=it.id, adjudicator=adj, decision=dec, rule_ids=list(rids), cited_spans=list(spans), reason=reason))
        except ValidationError as e:
            for m in validation_messages(e):
                st.error(m)
            return
        except (AnnotationRejected, NotAllowed, QueueError) as e:
            st.error(str(e) if not isinstance(e, AnnotationRejected) else " / ".join(e.issues))
            return
        _flash("success", "최종 라벨을 확정했습니다.")
        st.rerun()


def _leak_sentences(val: dtools.DatasetValidation) -> list[str]:
    out = []
    for nd in val.leaks:
        kind = "같은" if nd.exact else "거의 같은"
        out.append(f"{nd.a}({'개발용' if nd.a_split == 'dev' else '최종 확인용'}) 와 {nd.b}({'개발용' if nd.b_split == 'dev' else '최종 확인용'}) 가 {kind} 문구입니다. "
                   "최종 확인용 평가가 이미 개발에 본 문구의 영향을 받을 수 있습니다.")
    return out


def _existing_rows() -> list[dict]:
    rows: list[dict] = []
    for name in ("golden", "holdout"):
        try:
            rows.extend(dtools.load_jsonl_rows(dtools.DATASETS / f"{name}.jsonl"))
        except (OSError, ValueError):
            continue
    return rows


def _export_section(q: ReviewQueue, *, synthetic: bool, key: str) -> None:
    st.markdown("## 평가셋 후보 내보내기")
    st.caption("라벨이 확정된 광고(라벨러가 일치했거나 조정자가 정한 광고)만 평가셋 행으로 만듭니다. 기존 평가셋 파일은 건드리지 않고, 내려받는 것은 새 파일입니다. "
               "개발용/최종 확인용 분할은 문구로 정해져 같은 문구는 항상 같은 쪽에 들어갑니다.")
    single = st.checkbox("한 명이 라벨한 광고도 포함 (일치도를 확인할 수 없고 독립 확인이 아닙니다)", key=f"{key}_single", value=False) if not synthetic else False
    if st.button("평가셋 후보 만들기", key=f"{key}_make"):
        res = dtools.export_rows_report(q.items(), include_synthetic=synthetic, allow_single_labeler=single, required_labels=q.required_labels)
        val = dtools.validate_dataset(res.rows, existing=_existing_rows() or None) if res.rows else None
        st.session_state[f"{key}_result"] = {"res": res, "val": val}
    out = st.session_state.get(f"{key}_result")
    if not out:
        return
    res, val = out["res"], out["val"]
    if not res.rows:
        st.info("내보낼 수 있는 확정된 라벨이 아직 없습니다." + (f" (제외된 광고 {len(res.skipped)}건: " + "; ".join(sorted({s['reason'] for s in res.skipped})[:3]) + ")" if res.skipped else ""))
        return
    n_dev = sum(1 for r in res.rows if r["split"] == "dev")
    st.success(f"평가셋 후보 {len(res.rows)}건 (개발용 {n_dev}건 · 최종 확인용 {len(res.rows) - n_dev}건)을 만들었습니다. 제외된 광고 {len(res.skipped)}건.")
    for w in res.warnings:
        st.warning(w, icon="⚠️")
    if val is not None:
        for s in _leak_sentences(val):
            st.warning(s, icon="🚧")
        near = [nd for nd in val.near_duplicates if not nd.exact or nd.full_same]
        if near:
            st.warning(f"비슷하거나 같은 문구 {len(near)}쌍이 있습니다. 평가셋에 넣기 전에 확인하세요.", icon="⚠️")
        for i in val.errors:
            st.error(i.message)
    with st.expander("내보낼 행 미리 보기"):
        st.dataframe([{"id": r["id"], "분할": r["split"], "판정": decision_name(r["label"]), "제목": r["headline"][:30], "라벨러": ", ".join(r["labelers"]),
                       "조정 여부": "예" if r["adjudicated"] else "아니오"} for r in res.rows], width="stretch", hide_index=True)
    header = (f"Preflight 검수 대기열에서 내보낸 평가셋 후보 (가이드라인 v{GUIDELINE_VERSION}) — 새 파일이며 기존 평가셋과 합치기 전에 검증하세요."
              + (" 합성 예시이므로 평가셋에 합치지 마세요." if synthetic else ""))
    text = dtools.rows_to_jsonl(res.rows, header)
    ids = list(res.exported_item_ids)

    def _mark() -> None:
        if not synthetic:
            try:
                q.mark_exported(ids)
            except QueueError:
                pass

    st.download_button("평가셋 후보 내려받기 (JSONL)", data=text, file_name="labeling_demo_export.jsonl" if synthetic else "labeling_export.jsonl",
                       mime="application/x-ndjson", key=f"{key}_download", on_click=_mark)


def _demo_section(catalog: PolicyCatalog) -> None:
    ss = st.session_state
    st.warning("합성 예시입니다. 가상의 두 라벨러(‘합성 라벨러 A·B’)와 가상의 조정자가 만든 데이터이고 일부러 의견이 갈리는 광고를 넣었습니다. "
               "실제 사람이 단 라벨이 아니며 평가셋에 쓰지 않습니다. 다인 라벨 기능이 동작하는지 보여 주기 위한 것입니다.", icon="🧪")
    if _demo_queue() is None:
        if st.button("합성 예시 불러오기", key="rq_load_demo", type="primary"):
            try:
                ss["rq_demo_queue"] = build_demo_queue(dtools.load_labeling_demo())
            except (OSError, ValueError, QueueError) as e:
                _flash("error", f"합성 예시를 불러오지 못했습니다: {e}")
            st.rerun()
        return
    q = ss["rq_demo_queue"]
    st.caption(f"합성 예시 {len(q)}건 · 라벨러 {', '.join(sorted({a.annotator for a in q.annotations()}))}")
    _agreement_panel(q, catalog, synthetic=True)
    _quality_panel(q)
    with st.expander("광고별 최종 라벨"):
        finals = q.final_labels()
        st.dataframe([{"광고 제목": it.ad_copy.headline[:30], "최종 판정": decision_name(finals[it.id].decision) if it.id in finals else "—",
                       "조정": "예" if it.adjudication else "", "라벨러 판정": " / ".join(decision_name(a.decision) for a in it.annotations)} for it in q.items()],
                     width="stretch", hide_index=True)
    _export_section(q, synthetic=True, key="rq_demo_export")


def _advanced_section(q: ReviewQueue) -> None:
    with st.expander("고급: 라벨러 품질 점검용 문제 섞기 · 대기열 저장/불러오기"):
        st.caption("정답을 아는 광고(검증용 항목)를 일반 광고와 똑같은 모양으로 섞어, 라벨러가 가이드라인을 같은 뜻으로 읽는지 잽니다. 라벨러는 어느 것이 검증용인지 알 수 없습니다. "
                   "정답은 가이드라인 작성자의 라벨이며 독립 검증이 아닙니다.")
        if st.button(f"검증용 항목 {GOLD_COUNT}개 풀에 넣기", key="rq_add_gold", disabled=q.gold_pool_size > 0):
            try:
                rows = dtools.select_gold_rows(dtools.load_jsonl_rows(dtools.DATASETS / "golden.jsonl"), GOLD_COUNT)
            except (OSError, ValueError):
                rows = []
            n = q.enqueue_gold(rows)
            _flash("success" if n else "warning", f"검증용 항목 {n}개를 넣었습니다." if n else "검증용 항목으로 쓸 데이터를 읽지 못했습니다.")
            st.rerun()
        if q.gold_pool_size:
            st.caption(f"검증용 항목 {q.gold_pool_size}개가 풀에 있습니다 (라벨러마다 따로, 일정 비율로 섞여 나옵니다).")
        st.download_button("대기열 저장 (JSONL)", data=q.to_jsonl(), file_name="review_queue.jsonl", mime="application/x-ndjson", key="rq_save")
        up = st.file_uploader("저장한 대기열 불러오기", type=["jsonl"], key="rq_upload")
        if up is not None and st.button("불러오기 (현재 대기열을 바꿉니다)", key="rq_load"):
            try:
                st.session_state["rq_queue"] = ReviewQueue.from_jsonl(up.getvalue().decode("utf-8"))
            except (QueueFormatError, UnicodeDecodeError) as e:
                st.error(f"불러오지 못했습니다: {e}")
            else:
                _flash("success", "대기열을 불러왔습니다.")
                st.rerun()


def _real_section(entries: list[tuple[PipelineRun, str]], catalog: PolicyCatalog) -> None:
    ss = st.session_state
    q = _queue()
    c0, c1 = st.columns([2, 3])
    c0.radio("광고 하나를 확정하는 데 필요한 라벨 수", [1, 2], index=0 if q.required_labels == 1 else 1, key="rq_required", horizontal=True, disabled=len(q) > 0,
             help="2 로 두면 서로 다른 두 사람이 라벨해야 확정됩니다(일치도를 잴 수 있음). 1 은 한 사람 모드이며 독립 확인이 아닙니다. 대기열에 광고가 있으면 바꿀 수 없습니다.")
    if int(ss.get("rq_required", 2)) != q.required_labels and len(q) == 0:
        ss["rq_queue"] = q = ReviewQueue(required_labels=int(ss["rq_required"]))
    if q.required_labels == 1:
        c1.caption("한 사람 모드: 한 명의 라벨로 확정되며 일치도를 잴 수 없고, 내보낼 때도 ‘독립 확인 없음’ 으로 표시됩니다.")
    n_pending = pending_human_review(q, entries)
    st.markdown("## 대기열")
    if entries:
        label = f"담당자 확인이 필요한 광고 {n_pending}건을 대기열로 보내기" if n_pending else "새로 보낼 담당자 확인 광고가 없습니다"
        if st.button(label, key="rq_enqueue", disabled=n_pending == 0):
            added = dup = 0
            for run, landing in entries:
                r = q.enqueue_from_run(run, landing_text=landing)
                added += len(r.added)
                dup += len(r.duplicates)
            _flash("success", f"{added}건을 대기열에 담았습니다." + (f" 이미 있는 {dup}건은 다시 담지 않았습니다." if dup else ""))
            st.rerun()
    else:
        st.info("아직 광고 작업의 결과가 없습니다. ‘광고 작업’ 에서 광고를 검사하면 담당자 확인이 필요한 광고를 여기로 보낼 수 있습니다.")
    if len(q) == 0:
        st.info("대기열이 비었습니다. 담당자 확인이 필요한 광고가 생기면 위 버튼으로 보내 주세요. 합성 예시로 화면을 먼저 살펴볼 수도 있습니다 (위에서 ‘합성 예시’ 선택).")
    else:
        _stats_row(q)
        _queue_table(q)
    _labeling_section(q, catalog)
    _adjudication_section(q, catalog)
    if q.annotations():
        _agreement_panel(q, catalog, synthetic=False)
        _quality_panel(q)
    _export_section(q, synthetic=False, key="rq_export")
    _advanced_section(q)


def render(source: Any = None, *, catalog: Optional[PolicyCatalog] = None) -> None:
    """검수 대기열 화면. source: 광고 작업의 실행 결과 — `st.session_state.runs` 같은 목록(항목은 {"run": PipelineRun, "landing": str} 또는 PipelineRun),
    또는 그것을 돌려주는 인자 없는 함수. None 이거나 처리 기록(Tracer)처럼 실행 결과를 담지 않은 것이면 세션의 `runs` 를 쓴다."""
    catalog = catalog or get_catalog()
    ss = st.session_state
    entries = _entries(source)
    if not entries and not isinstance(source, (list, tuple)) and not callable(source):
        entries = _entries(ss.get("runs", []))
    st.markdown("## 검수 대기열")
    st.markdown("자동 검사가 확정하지 못한 광고를 사람이 직접 보고 **판정(라벨)** 을 남기는 곳입니다. 남긴 라벨은 코드가 검증하고, 라벨러가 여럿이면 서로 얼마나 같게 판단했는지 재고, "
                "확정된 라벨은 평가셋 후보로 내려받을 수 있습니다.")
    st.info("이 프로젝트에는 사람 라벨러가 한 명뿐입니다. 여러 명이 라벨하는 기능은 만들었지만 합성 예시로만 검증했고, 독립 라벨러가 확보됐다는 뜻이 아닙니다.", icon="ℹ️")
    _show_flash()
    saved = ss.setdefault("rq_saved", {})
    scope = st.radio("보기", [SCOPE_REAL, SCOPE_DEMO], index=1 if saved.get("scope") == SCOPE_DEMO else 0, horizontal=True, key="rq_scope", label_visibility="collapsed")
    saved["scope"] = scope
    if scope == SCOPE_DEMO:
        _demo_section(catalog)
    else:
        _real_section(entries, catalog)
