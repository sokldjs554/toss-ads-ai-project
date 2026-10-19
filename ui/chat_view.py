"""대화로 광고 설정 — 여러 번 말하며 목표·예산을 알려 주면, 필요한 것만 되묻고 캠페인 구성(미리보기)까지 만든다.

화면은 그리기만 한다. 이해(규칙 추출·검증)·제약 점검·되묻기·플랜은 전부 `agents/conversation.py` 의 `step` 이 한다.
세션 상태 키는 모두 `chat_` 로 시작한다 (chat_state · chat_last · chat_pending · chat_calls · chat_cost_usd · chat_use_live · chat_confirm_live · chat_applied).

정직하게 보이기
- 키가 없으면 '예시 실행 — 규칙 기반으로 이해합니다' 라고 알린다. 이때 모든 이해는 정해진 규칙이 하고, 플랜도 정해진 규칙의 대역이 만든 구성이다.
- 실제 AI 모델은 사용자가 비용을 확인한 뒤에만 부른다 (광고 작업 화면과 같은 방식).
- 확인되지 않은 조건('추정')은 사용자가 '광고에 쓰기' 를 눌러야 광고에 꼭 들어갈 사실이 된다.
"""
from __future__ import annotations

import dataclasses
import html
from typing import Callable, Optional

import streamlit as st

from agents import conversation as C
from core.config import IN_BROWSER, Settings
from observability.tracer import Tracer
from ui import theme
from ui.funding import DEFAULT_CAP_USD, funded_by_server, server_cap, usd_text
from ui.labels import RUN_MODE_HELP, RUN_MODE_LABEL

PREFIX = "chat_"
GREETING = ("안녕하세요! 광고로 이루고 싶은 **목표**와 **한 달 예산**을 편하게 말씀해 주세요. 상품과 타겟을 함께 알려 주시면 더 좋아요. "
            "모자란 것은 제가 하나씩 여쭤볼게요.\n\n예: “20대 직장인 대상 신용대출, 월 300만원, 신청 전환이에요.”")
STATUS_MARK = {"confirmed": ("✓ 확정", "ok"), "inferred": ("? 추정", "guess"), "conflict": ("⚠ 충돌", "warn")}
SEV_WORD = {"block": "막힘", "warn": "확인", "info": "참고"}

_CSS = """
<style>
.pf-chat { overflow-wrap: anywhere; word-break: break-word; line-height: 1.6; font-size: .95rem; }
.pf-slot { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 10px; padding: 8px 0 2px; border-top: 1px solid var(--pf-line); min-width:0; }
.pf-slot:first-of-type { border-top: none; }
.pf-slot .k { font-size:.8rem; color: var(--pf-ink3); min-width: 76px; }
.pf-slot .v { font-weight: 700; overflow-wrap:anywhere; min-width:0; flex: 1 1 140px; }
.pf-mark { display:inline-block; padding:2px 8px; border-radius:999px; font-size:.76rem; font-weight:700; white-space:nowrap; }
.pf-mark.ok { color:#0B6B37; background:#E6F4EC; }
.pf-mark.guess { color:#8A5A00; background:#FFF3D6; }
.pf-mark.warn { color:#A32116; background:#FCE8E6; }
.pf-ev { flex-basis:100%; font-size:.8rem; color: var(--pf-ink3); overflow-wrap:anywhere; }
.pf-note { overflow-wrap:anywhere; }
.pf-issue { border-left: 3px solid var(--pf-line); padding: 6px 12px; margin: 6px 0; background:#FAFBFC; border-radius: 0 8px 8px 0; font-size:.9rem; overflow-wrap:anywhere; }
.pf-issue.block { border-left-color:#A32116; }
.pf-issue.warn { border-left-color:#8A5A00; }
.pf-set { border:1px solid var(--pf-line); border-radius:12px; padding:10px 12px; margin:6px 0; background:#fff; overflow-wrap:anywhere; }
.pf-set .t { font-weight:700; }
.pf-set .m { font-size:.84rem; color: var(--pf-ink2); }
.pf-diff { background: var(--pf-accent-weak); border-radius: 12px; padding: 8px 12px; margin: 6px 0; font-size:.9rem; overflow-wrap:anywhere; }
</style>
"""


def _e(text: object) -> str:
    return html.escape(str(text))


def _para(text: str) -> str:
    """사용자·모델이 쓴 글을 안전하게: HTML 이스케이프 + 줄바꿈만 <br>. 빈 줄을 만들지 않아 마크다운이 끼어들지 않는다."""
    return "<br>".join(_e(line) for line in text.split("\n"))


def _init() -> None:
    ss = st.session_state
    if "chat_state" not in ss:
        ss.chat_state = C.new_state()
    ss.setdefault("chat_last", None)
    ss.setdefault("chat_pending", None)
    ss.setdefault("chat_calls", 0)
    ss.setdefault("chat_mock_calls", 0)
    ss.setdefault("chat_cost_usd", 0.0)
    ss.setdefault("chat_cost_estimated", False)
    ss.setdefault("chat_applied", False)


def reset_conversation() -> None:
    """대화 초기화: chat_ 로 시작하는 값만 지운다 (입력 위젯의 키는 건드리지 않는다)."""
    ss = st.session_state
    for k in list(ss.keys()):
        if k.startswith(PREFIX) and k not in ("chat_input", "chat_use_live", "chat_confirm_live", "chat_reset"):
            del ss[k]


# ---------------------------------------------------------------------------
# 설정 · 모드
# ---------------------------------------------------------------------------
def _live_possible(server_status: dict) -> tuple[bool, tuple[str, str]]:
    keys = st.session_state.get("user_keys", ("", "")) or ("", "")   # 광고 작업 화면에서 직접 넣은 키(있다면). 키 값은 여기서 화면에 쓰지 않는다
    has = bool(server_status.get("openai") or server_status.get("anthropic") or (keys[0] or "").strip() or (keys[1] or "").strip())
    return has and not IN_BROWSER, (keys[0] or "", keys[1] or "")


def _mode_settings(make_settings: Callable[..., Settings], server_status: dict, live_on: bool) -> tuple[Settings, str, float]:
    """(설정, 'demo'|실제 모드, 이 대화에 남은 비용 상한 USD)."""
    ss = st.session_state
    possible, (ko, ka) = _live_possible(server_status)
    if live_on and possible:
        by_server = funded_by_server(server_status, ko, ka)
        cap = server_cap(server_status) if by_server else DEFAULT_CAP_USD
        remaining = max(0.0, cap - float(ss.get("chat_cost_usd", 0.0)))
        settings = make_settings(ko.strip(), ka.strip(), run_budget_usd=remaining)
        return settings, settings.mode.value, remaining
    base = make_settings()
    return dataclasses.replace(base, force_demo=True, run_budget_usd=DEFAULT_CAP_USD), "demo", DEFAULT_CAP_USD


# ---------------------------------------------------------------------------
# 한 번 말하기
# ---------------------------------------------------------------------------
def _process(msg: str, make_settings: Callable[..., Settings], router_factory: Callable, server_status: dict, live_on: bool) -> None:
    ss = st.session_state
    settings, mode, _remaining = _mode_settings(make_settings, server_status, live_on)
    router = router_factory(settings)
    with st.spinner("말씀을 이해하는 중…"):
        res = C.step(ss.chat_state, msg, router, settings)
    ss.chat_state = res.state
    ss.chat_last = res
    ss.chat_calls = int(ss.chat_calls) + res.usage.calls
    if res.usage.mock:
        ss.chat_mock_calls = int(ss.chat_mock_calls) + res.usage.calls
    ss.chat_cost_usd = float(ss.chat_cost_usd) + res.usage.cost_usd
    ss.chat_cost_estimated = bool(ss.chat_cost_estimated) or res.usage.estimated
    ss.chat_applied = False


# ---------------------------------------------------------------------------
# 그리기
# ---------------------------------------------------------------------------
def _history(state: C.ConversationState) -> None:
    with st.chat_message("assistant"):
        st.markdown(GREETING)
    for t in state.turns:
        with st.chat_message(t.role):
            st.markdown(f'<div class="pf-chat">{_para(t.text)}</div>', unsafe_allow_html=True)


def _chips(res: Optional[C.TurnResult]) -> None:
    if res is None or not res.questions:
        return
    for qi, q in enumerate(res.questions):
        if not q.choices:
            continue
        st.caption(f"바로 답하기 · {C.SLOT_LABEL.get(q.slot, q.slot)}")
        cols = st.columns(min(len(q.choices), 4))
        for ci, choice in enumerate(q.choices):
            with cols[ci % len(cols)]:
                if st.button(choice, key=f"chat_chip_{res.state.user_turns}_{qi}_{ci}", width="stretch"):
                    st.session_state.chat_pending = choice
                    st.rerun()


def _understood(state: C.ConversationState) -> None:
    st.markdown("### 지금까지 이해한 내용")
    rows = [s for s in C.SLOT_ORDER if s in state.slots]
    if not rows:
        st.caption("아직 이해한 내용이 없어요. 아래 입력칸에 상품·목표·예산 중 아는 것부터 말씀해 주세요.")
        return
    html_rows = []
    for name in rows:
        sl = state.slots[name]
        mark, cls = STATUS_MARK[sl.status]
        if name == "key_conditions":
            continue
        ev = " · ".join(f"“{_e(e.quote)}” ({e.turn}번째 말씀)" for e in sl.evidence[:2])
        extra = f' · {_e(sl.note)}' if sl.note and sl.status != "confirmed" else ""
        confirm_hint = " · 맞는지 확인해 주세요" if sl.needs_confirmation else ""
        html_rows.append(
            f'<div class="pf-slot"><span class="k">{_e(C.SLOT_LABEL[name])}</span><span class="v">{_e(C.slot_text(name, sl))}</span>'
            f'<span class="pf-mark {cls}">{mark}</span><span class="pf-ev">{ev}{extra}{_e(confirm_hint)}</span></div>')
    if html_rows:
        st.markdown("".join(html_rows), unsafe_allow_html=True)
    sl = state.slots.get("key_conditions")
    if sl is not None:
        st.markdown(f'<div class="pf-slot"><span class="k">{_e(C.SLOT_LABEL["key_conditions"])}</span><span class="v">{len(sl.value)}개 찾음</span>'
                    f'<span class="pf-ev">사용자가 확인한 조건만 광고에 꼭 들어갈 사실로 써요.</span></div>', unsafe_allow_html=True)
        for i, text in enumerate(sl.value):
            ok = text in sl.confirmed_items
            ev = sl.evidence[i] if i < len(sl.evidence) else None
            st.markdown(f'<div class="pf-chat">{"✓ 확정" if ok else "? 추정"} · {_e(text)}'
                        + (f' <span class="pf-ev">({ev.turn}번째 말씀)</span>' if ev else "") + "</div>", unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            with c1:
                if not ok and st.button("광고에 꼭 넣을 조건으로 쓰기", key=f"chat_cond_ok_{i}", width="stretch"):
                    st.session_state.chat_state = C.confirm_condition(st.session_state.chat_state, i, True)
                    st.rerun()
            with c2:
                if st.button("이 조건 빼기", key=f"chat_cond_no_{i}", width="stretch"):
                    st.session_state.chat_state = C.confirm_condition(st.session_state.chat_state, i, False)
                    st.rerun()
    missing = [C.SLOT_LABEL[s] for s in C.REQUIRED if s not in state.slots]
    if missing:
        st.caption("아직 필요한 것: " + ", ".join(missing))


def _issues(state: C.ConversationState) -> None:
    shown = [i for i in state.issues if i.severity in ("block", "warn")]
    infos = [i for i in state.issues if i.severity == "info"]
    if shown:
        st.markdown("### 확인할 점")
        for i in shown:
            st.markdown(f'<div class="pf-issue {i.severity}"><b>{SEV_WORD[i.severity]}</b> · {_e(i.message)}</div>', unsafe_allow_html=True)
    if infos:
        with st.expander("참고 사항", expanded=False):
            for i in infos:
                st.markdown(f'<div class="pf-issue">{_e(i.message)}</div>', unsafe_allow_html=True)


def _plan(state: C.ConversationState, res: Optional[C.TurnResult]) -> None:
    if res is not None and res.plan_diff is not None and res.plan_diff.changed:
        st.markdown("### 바뀐 점")
        st.markdown('<div class="pf-diff">' + "<br>".join(_e(x) for x in res.plan_diff.lines) + "</div>", unsafe_allow_html=True)
    plan = state.plan_preview
    if plan is None:
        return
    st.markdown("### 캠페인 구성 미리보기")
    demo = state.plan_provider == "mock"
    stale = " · 값이 바뀌어 다시 만들어야 해요" if state.plan_stale else ""
    st.caption(("예시 실행: 정해진 규칙의 대역이 만든 구성이에요. 실제 모델이 만든 것도, 실제 성과 예측도 아니에요." if demo
                else "AI 모델이 만든 구성을 코드가 목표·예산·플랫폼 규칙에 맞는지 확인했어요. 성과를 보장하지 않아요.") + stale)
    st.markdown(f'<div class="pf-chat"><b>목표</b> {_e(C.OBJECTIVE_SHORT.get(plan.objective.value, plan.objective.value))} · <b>월 예산</b> {_e(C.fmt_krw(plan.total_budget_krw))} · '
                f'<b>광고 세트</b> {len(plan.ad_sets)}개</div>', unsafe_allow_html=True)
    for i, a in enumerate(plan.ad_sets, start=1):
        st.markdown(f'<div class="pf-set"><div class="t">세트 {i} · {_e(a.name)}</div><div class="m">예산 {_e(C.fmt_krw(a.budget_krw))} · {_e(a.bid_strategy)} · {_e(a.placement)}</div>'
                    f'<div class="m">타겟: {_e(a.audience)}</div></div>', unsafe_allow_html=True)
    if state.plan_notes:
        st.markdown('<div class="pf-note pf-muted">' + "<br>".join("· " + _e(n) for n in state.plan_notes) + "</div>", unsafe_allow_html=True)
    with st.expander("구성 이유와 가정", expanded=False):
        st.markdown(f'<div class="pf-chat">{_para(plan.planner_reasoning[:600])}</div>', unsafe_allow_html=True)
        if plan.assumptions:
            st.markdown("**가정**")
            st.markdown("<br>".join("· " + _e(x) for x in plan.assumptions[:8]), unsafe_allow_html=True)
        if plan.risks:
            st.markdown("**심사에서 걸릴 수 있는 점**")
            st.markdown("<br>".join("· " + _e(x) for x in plan.risks[:8]), unsafe_allow_html=True)


def _details(state: C.ConversationState, res: Optional[C.TurnResult], tracer: Optional[Tracer]) -> None:
    with st.expander("상세 기록", expanded=False):
        ss = st.session_state
        mock_all = ss.chat_calls and ss.chat_calls == ss.chat_mock_calls
        st.caption(("대역 응답 " + f"{ss.chat_mock_calls}회 — 실제 모델 호출 없음" if (mock_all or not ss.chat_calls) else
                    f"모델 호출 {ss.chat_calls}회 · 비용 {'≈ ' if ss.chat_cost_estimated else ''}${ss.chat_cost_usd:.4f}"))
        if res is not None:
            if res.dropped_updates:
                st.markdown("**모델이 제안했지만 쓰지 않은 값**")
                for d in res.dropped_updates:
                    st.markdown(f'<div class="pf-note">· {_e(C.SLOT_LABEL.get(d.slot, d.slot))}: {_e(d.value)} — 이유 {_e(d.reason)} (근거 구절 “{_e(d.evidence_quote)}”)</div>', unsafe_allow_html=True)
            if res.failures:
                st.markdown("**이번 말에서 일어난 일**")
                for f in res.failures:
                    st.markdown(f'<div class="pf-note">· {_e(f.code)} · {_e(f.message)} → {_e(f.fallback)}</div>', unsafe_allow_html=True)
        if state.issue_log:
            st.markdown("**대화 중 나온 확인 사항 (코드별 한 번)**")
            for i in state.issue_log:
                st.markdown(f'<div class="pf-note">· {_e(i.code)} ({SEV_WORD[i.severity]}) {_e(i.message[:160])}</div>', unsafe_allow_html=True)
        if tracer is not None and getattr(tracer, "traces", None):
            last = tracer.latest()
            if last is not None:
                st.caption(f"최근 처리 기록: {last.trace_id}")


def _actions(state: C.ConversationState, on_apply: Optional[Callable[[dict], None]]) -> None:
    ss = st.session_state
    ready = state.status == "ready"
    if on_apply is not None:
        st.caption("필요한 정보가 모이면 광고 작업 화면의 입력칸을 이 내용으로 채워 드려요. 아직 검사나 광고안 생성은 하지 않아요." if not ready else
                   "아래 버튼을 누르면 광고 작업 화면의 입력칸이 이 내용으로 채워져요. 확인되지 않은 조건은 넣지 않아요.")
        if st.button("이 내용으로 광고 작업 채우기", type="primary", width="stretch", key="chat_apply", disabled=not ready):
            try:
                on_apply(C.brief_for_workbench(state))
                ss.chat_applied = True
            except C.NotReady:
                st.error("아직 준비되지 않았어요. 모자란 내용을 먼저 알려 주세요.")
        if ss.get("chat_applied"):
            st.success("광고 작업 화면에 채웠어요. ‘광고 작업’ 화면으로 가서 확인해 주세요.")
    if st.button("대화 초기화", width="stretch", key="chat_reset"):
        reset_conversation()
        st.rerun()


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------
def render(make_settings: Callable[..., Settings], tracer: Optional[Tracer], router_factory: Callable, server_status: dict,
           on_apply: Optional[Callable[[dict], None]] = None) -> None:
    """대화 화면. make_settings(user_openai_key, user_anthropic_key, **overrides) → Settings, router_factory(settings) → Router,
    server_status = {"openai": bool, "anthropic": bool, "run_budget_usd": float}, on_apply(brief_dict) 는 '광고 작업 채우기' 버튼이 부른다.
    brief_dict 키: advertiser_input · product_name · category · facts · budget · description · landing_text (광고 작업의 예시 항목과 같은 키) + landing_url · unconfirmed_conditions."""
    st.markdown(_CSS, unsafe_allow_html=True)
    _init()
    ss = st.session_state
    st.markdown("## 대화로 광고 설정")
    st.markdown("목표와 예산을 말로 알려 주세요. 모자란 것만 하나씩 여쭤보고, 필요한 정보가 모이면 캠페인 구성을 미리 보여 드려요.")

    # 모드: 기본은 예시 실행(규칙 기반). 실제 모델은 비용을 확인한 뒤에만.
    possible, (ko, ka) = _live_possible(server_status)
    live_on = False
    if possible:
        by_server = funded_by_server(server_status, ko, ka)
        cap = server_cap(server_status) if by_server else DEFAULT_CAP_USD
        if by_server and cap <= 0:
            st.caption("서버 운영자가 실제 모델 실행을 허용하지 않아(비용 상한 $0) 예시 실행만 쓸 수 있어요.")
        else:
            with st.expander("실제 AI 모델로 이해하기 (선택 · 비용 발생)", expanded=False):
                use = st.checkbox("말을 실제 AI 모델로도 이해합니다", key="chat_use_live",
                                  help="모델이 값을 제안하면 코드가 근거 구절·숫자를 원문과 대조해 통과한 것만 씁니다.")
                if use:
                    st.warning(f"말할 때마다 모델 호출이 최대 약 2회(이해 1 + 캠페인 구성 1) 일어나고, 이 대화의 비용은 {usd_text(cap)} 에서 멈춥니다. "
                               + ("서버 키가 쓰이므로 비용은 서버 운영자 계정에 청구됩니다." if by_server else "직접 넣은 키만 쓰이므로 비용은 그 계정에 청구됩니다."), icon="💳")
                    live_on = st.checkbox("위 호출 수와 비용 상한을 확인했고 실제 모델로 진행합니다", key="chat_confirm_live")
    settings, mode, remaining = _mode_settings(make_settings, server_status, live_on)
    pill_kind = "demo" if mode == "demo" else "live"
    label = "예시 실행 — 규칙 기반으로 이해합니다" if mode == "demo" else RUN_MODE_LABEL.get(mode, mode)
    st.markdown(theme.mode_pill(pill_kind, label), unsafe_allow_html=True)
    st.caption("API 키가 없어 정해진 규칙이 말을 읽고, 캠페인 구성은 정해진 규칙의 대역이 만듭니다. 실제 AI 모델의 이해력이 아니에요." if mode == "demo"
               else RUN_MODE_HELP.get(mode, ""))

    state: C.ConversationState = ss.chat_state
    limit = state.user_turns >= C.MAX_TURNS
    typed = st.chat_input("예: 20대 직장인 대상 신용대출, 월 300만원, 신청 전환이에요", max_chars=C.MAX_USER_CHARS, key="chat_input", disabled=limit)
    pending = ss.pop("chat_pending", None)       # 칩을 눌러 보낸 답 (입력칸에 직접 쓴 말이 있으면 그것이 먼저)
    msg = typed or pending
    if msg:
        _process(str(msg), make_settings, router_factory, server_status, live_on)
        state = ss.chat_state

    res: Optional[C.TurnResult] = ss.get("chat_last")
    st.caption(f"대화 {state.user_turns}/{C.MAX_TURNS}번 · 한 번에 {C.MAX_USER_CHARS:,}자까지 읽어요")
    left, right = st.columns([3, 2], gap="large")
    with left:
        _history(state)
        _chips(res)
        if limit:
            st.info(f"대화는 최대 {C.MAX_TURNS}번까지예요. 지금까지 이해한 내용으로 진행하거나 ‘대화 초기화’ 를 눌러 다시 시작해 주세요.", icon="ℹ️")
    with right:
        with st.container(border=True):
            _understood(state)
        _issues(state)
        _plan(state, res)
        _actions(state, on_apply)
        _details(state, res, tracer)
