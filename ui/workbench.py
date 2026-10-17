"""광고 작업 — 상품 정보 입력 → 광고안 생성 → 검사 결과 → 수정 제안 → 직접 고쳐서 다시 검사.

상태: 입력 전 / 실행 중 / 완료 / 일부 단계 실패 / 전체 실행 실패 / 결과 없음 / 입력 변경 후 재실행 필요 / 담당자 확인 필요.
판정·검사 로직은 전부 agents/·guardrails/ 의 것을 부른다. 여기서는 그리기만 한다.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import time
from datetime import datetime

import streamlit as st

from agents.pipeline import RunOptions, run_pipeline
from agents.recheck import build_edited_copy, copy_fingerprint, recheck
from core.config import IN_BROWSER, Settings
from core.schemas import Decision, PipelineRun, PreflightReport, ProductCategory, RewriteStatus, Severity
from evals.runner import recommendation_for
from guardrails.failure_modes import BY_CODE
from guardrails.policy import get_catalog
from llm.router import Router
from observability.tracer import Tracer
from ui import theme
from ui.components import (
    checklist_rows,
    evidence_lines,
    finding_sentence,
    human_review_reason,
    human_review_request_text,
    plain_reason,
    render_ad,
    rule_title,
    what_to_check,
)
from ui.labels import DECISION_HELP, DECISION_LABEL, NEXT_ACTION, REWRITE_STATUS_LABEL, RUN_MODE_HELP, RUN_MODE_LABEL, TERM
from ui.presets import PRESETS, build_brief

CATEGORY_LABELS = {c.value: n for c, n in [(ProductCategory.LOAN, "대출"), (ProductCategory.CARD, "카드"), (ProductCategory.INSURANCE, "보험"),
                                            (ProductCategory.INVESTMENT, "투자·증권"), (ProductCategory.SAVINGS, "예·적금"), (ProductCategory.CRYPTO, "가상자산"),
                                            (ProductCategory.ECOMMERCE, "커머스"), (ProductCategory.APP, "앱·서비스"), (ProductCategory.EDUCATION, "교육"),
                                            (ProductCategory.HEALTH, "건강·의료"), (ProductCategory.REALESTATE, "부동산"), (ProductCategory.OTHER, "기타")]}
PRESET_TITLES = {
    "loan_ok": "정상 요청 — 신용대출 (가상 상품)",
    "malicious": "문제 있는 요청 — '원금 보장·무조건 승인' 을 써 달라고 함",
    "injection": "숨은 지시문 — 상품 설명에 심사 조작 문장",
    "prohibited": "광고 불가 업종 — 리딩방 유도",
    "investment": "투자 상품 — 필수 안내 문구가 필요한 경우",
    "commerce_landing": "커머스 쿠폰 — 랜딩 페이지와 조건이 다름",
}
FORM_KEYS = ("form_input", "form_name", "form_category", "form_facts", "form_desc", "form_landing", "form_budget")
STEPS = [("intercept", "요청 검사"), ("planner", "캠페인 구성"), ("copywriter", "광고 초안 작성"), ("review", "광고안 검사·판정")]


# --- 상태 ---------------------------------------------------------------------
def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("runs", [])
    ss.setdefault("rechecks", {})
    ss.setdefault("rejected_history", [])
    ss.setdefault("running", False)
    if "form_input" not in ss:
        _load_preset("loan_ok")


def _load_preset(key: str) -> None:
    p = PRESETS[key]
    ss = st.session_state
    ss.form_input = p["advertiser_input"]
    ss.form_name = p["product_name"]
    ss.form_category = p["category"]
    ss.form_facts = "\n".join(p["facts"])
    ss.form_desc = p.get("description", "")
    ss.form_landing = p.get("landing_text", "")
    ss.form_budget = int(p.get("budget") or 0)
    ss.loaded_preset = key


def _form() -> dict:
    ss = st.session_state
    return {"advertiser_input": ss.form_input, "product_name": ss.form_name or "상품", "category": ss.form_category, "facts": ss.form_facts,
            "budget": ss.form_budget or None, "description": ss.form_desc, "landing_text": ss.form_landing}


def input_fingerprint(form: dict) -> str:
    raw = "\x1e".join(str(form.get(k, "")) for k in ("advertiser_input", "product_name", "category", "facts", "budget", "description", "landing_text"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def run_state(entry: dict) -> str:
    run: PipelineRun = entry["run"]
    if run.halted and not run.reports:
        return "failed"
    if not run.reports:
        return "empty"
    stage_fail = any(f.code in ("FM-05", "FM-06", "FM-07", "FM-08", "FM-09", "FM-10") or f.stage in ("planner", "audience") for f in run.failures) or run.plan is None
    if stage_fail:
        return "partial"
    return "done"


# --- 입력 ---------------------------------------------------------------------
def _inputs(make_settings, server_status: dict) -> tuple[Settings, RunOptions, str, bool]:
    """입력 폼 + 상세 옵션. (settings, options, 실행 방식, 실행 버튼 눌림)"""
    ss = st.session_state
    st.markdown("## 광고 만들기")
    st.markdown("상품 정보와 꼭 들어가야 할 조건을 적으면 광고안을 만들고, 이 프로젝트의 검사 기준으로 **확인이 필요한 구절**을 찾아 이유와 수정 제안을 보여 줍니다. "
                "예시를 골라 한 번에 전체 흐름을 볼 수도 있습니다.")
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            preset = st.selectbox("예시 선택", options=list(PRESETS.keys()), format_func=lambda k: PRESET_TITLES.get(k, k), key="preset_pick",
                                  help="예시를 불러온 뒤 내용을 고쳐도 되고, 처음부터 직접 입력해도 됩니다.")
        with c2:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("예시 불러오기", width="stretch"):
                _load_preset(preset)
                st.rerun()
        st.caption("모든 상품·수치는 가상입니다. 직접 입력할 때는 아래 세 칸만 채우면 됩니다.")

    with st.container(border=True):
        st.markdown("### 상품 정보")
        st.caption("광고에 쓸 수 있는 말의 범위를 정합니다. 광고안의 모든 숫자·주장은 아래 정보에서만 나옵니다.")
        a, b = st.columns([2, 1])
        with a:
            st.text_input("상품명", key="form_name", placeholder="예: 가상은행 신용대출")
        with b:
            st.selectbox("업종", options=list(CATEGORY_LABELS.keys()), format_func=lambda k: CATEGORY_LABELS[k], key="form_category",
                         help="업종에 따라 필수 안내 문구와 검사 기준이 달라집니다.")
        d, e = st.columns(2)
        with d:
            st.text_area("상품 설명 (선택)", key="form_desc", height=70, placeholder="예: 모바일에서 한도 조회 후 신청하는 신용대출 상품입니다.",
                         help="자유 텍스트입니다. 이 안의 문장은 '지시' 가 아니라 '자료' 로만 다루며, 숨은 지시문이 있으면 걸러냅니다.")
        with e:
            st.text_area("랜딩 페이지 문구 (선택)", key="form_landing", height=70, placeholder="예: 연 4.5%~15.0% (심사 결과에 따라 다름). 문의 1588-0000",
                         help="광고와 랜딩 페이지의 숫자·표현이 다르면 '확인 필요' 로 잡습니다 (플랫폼이 밝힌 가장 흔한 반려 사유).")
    with st.container(border=True):
        st.markdown("### 반드시 포함해야 할 조건")
        st.caption("한 줄에 하나씩, `F1: 내용` 형식. 금리·한도·기간·자동결제 같은 조건을 여기 적으면 광고에서 빠졌을 때 잡아냅니다.")
        st.text_area("사실 목록", key="form_facts", height=120, label_visibility="collapsed",
                     placeholder="F1: 연 4.5%~15.0% (심사 결과에 따라 다름)\nF2: 최대 한도 5,000만원 (심사 결과에 따라 다름)\nF3: 중도상환수수료 없음")
    with st.container(border=True):
        st.markdown("### 광고 요청")
        st.caption("누구에게, 무엇을 위해, 얼마로 광고할지 말로 적으세요. 검사 기준에 어긋나는 요청(예: '원금 보장이라고 써줘')은 고쳐서 진행하고 그 사실을 알려 드립니다.")
        a, b = st.columns([3, 1])
        with a:
            st.text_area("요청 내용", key="form_input", height=80, label_visibility="collapsed",
                         placeholder="예: 20대 사회초년생에게 신용대출 상품을 알리고 싶어요. 월 예산 300만원, 목표는 대출 신청 전환이에요.")
        with b:
            st.number_input("월 예산 (원)", key="form_budget", min_value=0, step=100_000)

    # ---- 상세 옵션 ---------------------------------------------------------------
    with st.expander("상세 옵션 — 광고안 수, 판정 기준, 모델, API 키", expanded=False):
        o1, o2, o3 = st.columns(3)
        with o1:
            n_variants = st.slider("광고안 수", 1, 5, 3)
        with o2:
            enable_audience = st.toggle("가상 사용자 반응 포함", value=True, help=TERM["audience"])
        with o3:
            enable_second = st.toggle("다른 모델 계열의 교차 확인", value=True, help="확신이 낮거나 참고 사항이 있는 광고안만 다른 모델 계열에 한 번 더 묻습니다.")
        user_oa, user_an = "", ""
        oa_model = st.session_state.get("opt_oa_model", "gpt-4o")
        an_model = st.session_state.get("opt_an_model", "claude-sonnet-5-5")
        if IN_BROWSER:
            st.caption("브라우저 실행판은 서버가 없어 API 키를 쓸 수 없고 항상 예시 실행입니다. 실제 모델 결과는 '검사 사례' 에 저장돼 있습니다.")
        else:
            st.markdown("**모델과 API 키**")
            st.caption(f"서버에 설정된 키: OpenAI {'있음' if server_status['openai'] else '없음'} · Anthropic {'있음' if server_status['anthropic'] else '없음'}. "
                       "서버 키 값은 화면에 보이지 않습니다. 직접 키를 넣으면 이 세션에서만 그 키를 씁니다.")
            k1, k2 = st.columns(2)
            with k1:
                user_oa = st.text_input("내 OpenAI API 키 (선택)", type="password", value="", key="user_openai_key", autocomplete="off")
                oa_model = st.text_input("OpenAI 모델", value=oa_model, key="opt_oa_model")
            with k2:
                user_an = st.text_input("내 Anthropic API 키 (선택)", type="password", value="", key="user_anthropic_key", autocomplete="off")
                an_model = st.text_input("Anthropic 모델", value=an_model, key="opt_an_model")
        provisional = make_settings(user_oa.strip(), user_an.strip(), openai_model=oa_model.strip() or "gpt-4o", anthropic_model=an_model.strip() or "claude-sonnet-5-5")
        rec = recommendation_for(provisional)
        st.markdown("**판정 기준**")
        default_tau = float(rec["tau"]) if rec["tau"] is not None else float(provisional.auto_decision_confidence)
        tau = st.slider("자동 판정에 필요한 검토 모델의 최소 확신", 0.5, 0.99, default_tau, 0.01,
                        help="검토 모델의 확신이 이 값보다 낮으면 자동으로 확정하지 않고 담당자 확인으로 넘깁니다. 게재 불가 사유가 코드로 확인된 광고는 이 값과 무관하게 게재 불가입니다.")
        if rec["tau"] is not None:
            st.caption(f"기본값 {rec['tau']:.2f} 은 같은 제공자·모델·프롬프트·검사 기준·평가 데이터로 평가해 '문제 있는 광고를 통과시킨 사례 0건·문제없는 광고에 수정 요청 0건' 을 만족한 가장 낮은 값입니다.")
        else:
            st.caption("이 설정의 검증된 추천값 없음 — " + (", ".join(rec["mismatches"]) if rec["mismatches"] else rec["reason"]) + ". 기본값은 설정값(0.80)을 씁니다.")
        b1, b2 = st.columns(2)
        with b1:
            budget = st.number_input("실행당 비용 상한 (USD)", min_value=0.01, value=0.50, step=0.05, help="실제 모델 호출 비용이 이 값을 넘으면 실행을 멈추고 남은 광고안은 담당자 확인으로 둡니다. 실패한 요청의 비용은 알 수 없어 '추정' 으로 표시합니다.")
        with b2:
            drill = st.selectbox("장애 시뮬레이션 (기술 시연용)", ["없음", "openai: timeout", "openai: schema", "anthropic: timeout", "anthropic: refusal", "mock: timeout"],
                                 help="모델 호출을 일부러 실패시켜 대체 경로와 '실패해도 통과로 흐르지 않음' 을 보여 줍니다.")
        ss.opt_drill = drill
    overrides = dict(openai_model=oa_model.strip() or "gpt-4o", anthropic_model=an_model.strip() or "claude-sonnet-5-5",
                     auto_decision_confidence=tau, run_budget_usd=float(budget))
    settings = make_settings(user_oa.strip(), user_an.strip(), **overrides)
    # 세션에는 키 값을 남기지 않는다 — 사용자가 직접 넣은 키만 (서버 키는 절대) 재검사용으로 보관한다
    ss.settings_overrides = overrides
    ss.user_keys = (user_oa.strip(), user_an.strip())
    options = RunOptions(n_variants=n_variants, landing_text=ss.form_landing, rejected_history=list(ss.rejected_history),
                         enable_audience=enable_audience, enable_second_opinion=enable_second)
    mode = settings.mode.value
    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown(theme.mode_pill("demo" if mode == "demo" else "live", RUN_MODE_LABEL[mode]), unsafe_allow_html=True)
        st.caption(RUN_MODE_HELP[mode])
    with c2:
        clicked = st.button("광고 초안 만들기", type="primary", width="stretch", disabled=bool(ss.running), key="run_button")
    return settings, options, mode, clicked


# --- 실행 ---------------------------------------------------------------------
def _execute(settings: Settings, options: RunOptions, tracer: Tracer, router_factory, mode: str) -> None:
    ss = st.session_state
    form = _form()
    fp = input_fingerprint(form)
    last = ss.get("last_run_fp")
    if ss.running:
        st.info("이미 실행 중입니다. 끝나면 결과가 아래에 나타납니다.")
        return
    if last == fp and time.time() - ss.get("last_run_t", 0) < 3:
        st.info("같은 입력으로 방금 실행했습니다. 결과는 아래에 있습니다.")
        return
    try:
        brief = build_brief(form)
    except Exception as e:  # noqa: BLE001
        st.error(f"입력을 해석할 수 없습니다: {e}. 입력은 그대로 남아 있으니 고친 뒤 다시 눌러 주세요.")
        return
    ss.running = True
    router: Router = router_factory(settings)
    done_steps: list[str] = []
    with st.status("광고 초안을 만들고 검사하는 중…", expanded=True) as status:
        box = st.empty()

        def draw(current: str) -> None:
            rows = []
            for i, (k, label) in enumerate(STEPS, start=1):
                if k == current:
                    rows.append(f'<div class="pf-step"><span class="n">{i}</span><span><b>{html.escape(label)}</b> — 진행 중…</span></div>')
                elif k in done_steps:
                    rows.append(f'<div class="pf-step"><span class="n">✓</span><span>{html.escape(label)}</span></div>')
                else:
                    rows.append(f'<div class="pf-step"><span class="n">{i}</span><span class="pf-muted">{html.escape(label)}</span></div>')
            box.markdown("".join(rows), unsafe_allow_html=True)

        def on_progress(key: str, label: str) -> None:
            for k, _ in STEPS:
                if k == key:
                    break
                if k not in done_steps:
                    done_steps.append(k)
            draw(key)
            if key == "review":
                status.update(label=f"{label} 중…")

        options.on_progress = on_progress
        draw("intercept")
        try:
            run = run_pipeline(brief, settings, router=router, options=options, tracer=tracer)
        finally:
            ss.running = False
        for k, _ in STEPS:
            if k not in done_steps:
                done_steps.append(k)
        draw("")
        n = len(run.reports)
        cost = f"${run.total_cost_usd:.4f}" + (" (추정)" if router.budget.estimated else "")
        status.update(label=f"완료 — 광고안 {n}개 · {cost} · {run.total_latency_ms/1000:.1f}초", state="complete" if not run.halted else "error", expanded=False)
    entry = {"run": run, "fingerprint": fp, "mode": mode, "at": datetime.now(), "brief": brief, "landing": options.landing_text,
             "cost_estimated": router.budget.estimated, "attempts": router.budget.attempts, "calls": router.budget.calls,
             "failed_attempts": router.budget.failed_attempts, "overrides": dict(ss.get("settings_overrides", {})), "n_variants": options.n_variants}
    ss.runs.append(entry)
    ss.last_run_fp, ss.last_run_t = fp, time.time()
    for r in run.reports:
        if r.final_decision == Decision.REJECT:
            ss.rejected_history.append(r.ad_copy.full_text())


# --- 결과 ---------------------------------------------------------------------
def _state_banner(entry: dict, stale: bool) -> None:
    run: PipelineRun = entry["run"]
    state = run_state(entry)
    if stale:
        st.warning("아래 결과는 **이전 입력**에 대한 결과입니다. 입력이 바뀌었으니 '광고 초안 만들기' 를 다시 눌러 주세요.", icon="⚠️")
    if state == "failed":
        st.error(f"실행을 끝내지 못했습니다 — {run.halt_reason}  \n입력은 그대로 남아 있습니다. 요청을 고치거나 상세 옵션을 바꿔 다시 실행하세요.", icon="⛔")
    elif state == "empty":
        st.warning("광고안이 만들어지지 않았습니다. 상품 정보와 사실 목록을 조금 더 구체적으로 적고 다시 실행해 보세요.", icon="ℹ️")
    elif state == "partial":
        parts = sorted({f.stage for f in run.failures if f.stage in ("planner", "audience")})
        names = {"planner": "캠페인 구성", "audience": "가상 사용자 반응"}
        what = ", ".join(names.get(p, p) for p in parts) or "일부 단계"
        st.info(f"{what} 단계가 실패해 그 부분 없이 진행했습니다. 광고안 검사와 판정은 정상적으로 끝났습니다.", icon="ℹ️")


def _summary(entry: dict) -> None:
    run: PipelineRun = entry["run"]
    decisions = [r.final_decision for r in run.reports]
    cost = f"${run.total_cost_usd:.4f}"
    if entry.get("cost_estimated"):
        cost = "≈ " + cost
    cols = st.columns(4)
    cols[0].markdown(theme.stat("광고안", f"{len(run.reports)}개", f"{entry['mode'] and RUN_MODE_LABEL[entry['mode']]}"), unsafe_allow_html=True)
    cols[1].markdown(theme.stat("결과", " · ".join(f"{DECISION_LABEL[d]} {decisions.count(d)}" for d in (Decision.APPROVE, Decision.REVISE, Decision.REJECT, Decision.HUMAN_REVIEW) if decisions.count(d)) or "—", ""), unsafe_allow_html=True)
    cols[2].markdown(theme.stat("모델 호출", f"{entry.get('calls', 0)}회", f"요청 {entry.get('attempts', 0)}회" + (f" · 실패 {entry['failed_attempts']}회" if entry.get("failed_attempts") else "")), unsafe_allow_html=True)
    cols[3].markdown(theme.stat("비용 · 시간", f"{cost} · {run.total_latency_ms/1000:.1f}초", "실패한 요청이 있어 추정치" if entry.get("cost_estimated") else ("실제 호출 없음" if entry["mode"] == "demo" else "제공자 사용량 기준")), unsafe_allow_html=True)
    st.caption(f"실행 {entry['at'].strftime('%Y-%m-%d %H:%M:%S')} · 처리 기록 {run.trace_id} (품질 확인 › 처리 기록에서 단계별로 볼 수 있습니다)")


def _intercept(entry: dict, catalog) -> None:
    run: PipelineRun = entry["run"]
    n = run.intercept
    if not n.triggered:
        return
    kind = {"policy_intent": "요청에 검사 기준에 어긋나는 표현이 있어 고쳐서 진행했습니다", "injection": "입력에 숨은 지시문이 있어 걷어내고 진행했습니다",
            "prohibited_category": "광고할 수 없는 업종·내용이라 진행하지 않았습니다"}.get(n.kind.value if n.kind else "", "요청을 고쳐서 진행했습니다")
    box = st.error if n.hard_block else st.warning
    box(f"**{kind}**  \n{n.message}", icon="🛡️")
    with st.expander("무엇을 어떻게 고쳤나", expanded=not n.hard_block):
        if n.removed_phrases:
            st.markdown("뺀 표현: " + " ".join(f"`{p}`" for p in n.removed_phrases))
        if n.suggested_input:
            st.markdown("**원래 요청**"); st.code(run.brief.advertiser_input, language=None)
            st.markdown("**고친 요청** (이 요청으로 진행)"); st.code(n.suggested_input, language=None)
        if n.findings:
            for f in n.findings:
                st.markdown(f"- {finding_sentence(f, catalog)}")


def _plan(entry: dict) -> None:
    run: PipelineRun = entry["run"]
    if run.plan is None:
        return
    p = run.plan
    obj = {"awareness": "인지", "traffic": "방문", "conversion": "전환", "app_install": "설치", "lead": "리드"}.get(p.objective.value, p.objective.value)
    with st.expander(f"캠페인 구성 근거 — 목표 {obj} · {p.kpi} · 월 {p.total_budget_krw:,}원" + (" · 플랫폼 규칙에 맞게 코드가 고침" if p.repaired else ""), expanded=False):
        st.markdown(p.planner_reasoning)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**가정**")
            for a in p.assumptions:
                st.markdown(f"- {a}")
        with c2:
            st.markdown("**검사에서 걸릴 수 있는 지점**")
            for r in p.risks:
                st.markdown(f"- {r}")
        st.dataframe([{"광고 세트": a.name, "대상": a.audience, "지면": a.placement, "예산(원)": f"{a.budget_krw:,}", "입찰": a.bid_strategy, "근거": a.rationale} for a in p.ad_sets],
                     width="stretch", hide_index=True)


def _rewrite_block(rep: PreflightReport, catalog) -> None:
    st.markdown("**수정 제안**")
    if rep.rewrite_status == RewriteStatus.VERIFIED and rep.verified_rewrite is not None:
        st.caption(REWRITE_STATUS_LABEL[rep.rewrite_status] + " — 검토 모델이 제안한 문구를 같은 검사 기준으로 다시 검사해 통과한 것만 보여 줍니다.")
        render_ad(rep.verified_rewrite, title="검사를 통과한 수정 제안", catalog=catalog)
        _diff_explained(rep, catalog)
    elif rep.rewrite_status in (RewriteStatus.FAILED_RECHECK, RewriteStatus.UNCHECKED):
        why = ""
        if rep.rewrite_recheck_findings:
            why = " 재검사에서 걸린 것: " + "; ".join(sorted({rule_title(f.rule_id, catalog) for f in rep.rewrite_recheck_findings}))
        st.info(REWRITE_STATUS_LABEL[rep.rewrite_status] + "." + why + " 아래 '문구 고쳐서 다시 검사' 에서 직접 고칠 수 있습니다.", icon="ℹ️")
        _hints(rep, catalog)
    elif rep.final_decision in (Decision.REVISE, Decision.REJECT):
        st.caption("자동 수정 제안이 없습니다. 아래 수정 힌트를 참고해 직접 고치세요.")
        _hints(rep, catalog)
    else:
        st.caption("수정이 필요하지 않습니다.")


def _hints(rep: PreflightReport, catalog) -> None:
    hints = []
    for f in rep.verdict.findings:
        r = catalog.get(f.rule_id)
        if r and r.fix_hint and r.fix_hint not in hints and (f.verified_quote or f.layer.value != "semantic"):
            hints.append(r.fix_hint)
    for h in hints:
        st.markdown(f"- {h}")


def _diff_explained(rep: PreflightReport, catalog) -> None:
    before, after = rep.ad_copy, rep.verified_rewrite
    changes = []
    for name, a, b in (("제목", before.headline, after.headline), ("본문", before.body, after.body), ("버튼", before.cta, after.cta)):
        if a != b:
            changes.append(f"- **{name}**: “{a}” → “{b}”")
    added = [d for d in after.disclosures if d not in before.disclosures]
    removed = [d for d in before.disclosures if d not in after.disclosures]
    if added:
        changes.append("- **안내 문구 추가**: " + " / ".join(added))
    if removed:
        changes.append("- **안내 문구 삭제**: " + " / ".join(removed))
    reasons = sorted({rule_title(f.rule_id, catalog) for f in rep.verdict.findings if f.severity != Severity.WARN and (f.verified_quote or f.layer.value != "semantic")})
    st.markdown("**무엇이 왜 바뀌었나**")
    st.markdown("\n".join(changes) if changes else "- 바뀐 문구가 없습니다.")
    if reasons:
        st.caption("고친 이유: " + " · ".join(reasons))


def _human_review_block(rep: PreflightReport, entry: dict, catalog) -> None:
    run: PipelineRun = entry["run"]
    with st.container(border=True):
        st.markdown("**담당자 확인이 필요합니다**")
        st.markdown(f"- 확정하지 못한 부분: {human_review_reason(rep)}")
        st.markdown("- 담당자가 확인할 것:")
        for x in what_to_check(rep, catalog):
            st.markdown(f"  - {x}")
        st.markdown("- 다음 단계 진행: **불가** — 담당자 확인 전에는 게재할 수 없습니다.")
        st.caption("이 화면에는 검토 대기열이나 담당자 처리 기능이 없습니다. 아래 내용을 복사해 담당자에게 직접 전달하세요.")
        text = human_review_request_text(rep, run, catalog)
        with st.expander("확인 요청 내용 (복사용)"):
            st.code(text, language=None)
            if not IN_BROWSER:
                st.download_button("확인 요청 내용 내려받기 (.txt)", data=text, file_name=f"review_request_{rep.variant_id}.txt", mime="text/plain", key=f"dl_{rep.variant_id}")


def _edit_and_recheck(rep: PreflightReport, entry: dict, catalog, tracer: Tracer, router_factory, make_settings) -> None:
    ss = st.session_state
    vid = rep.variant_id
    base = rep.verified_rewrite if rep.verified_rewrite is not None else rep.ad_copy
    with st.expander("문구 고쳐서 다시 검사", expanded=rep.final_decision in (Decision.REVISE, Decision.REJECT)):
        st.caption("아래 칸을 고치고 '수정한 문구 다시 검사' 를 누르면 같은 검사 기준으로 다시 검사합니다. 결과는 그 시점의 문구에만 해당합니다.")
        h = st.text_input("제목", value=base.headline, key=f"edit_h_{vid}")
        b = st.text_area("본문", value=base.body, key=f"edit_b_{vid}", height=80)
        c1, c2 = st.columns([1, 2])
        with c1:
            c = st.text_input("버튼 문구", value=base.cta, key=f"edit_c_{vid}")
        with c2:
            d = st.text_area("안내 문구 (한 줄에 하나)", value="\n".join(base.disclosures), key=f"edit_d_{vid}", height=80)
        edited = build_edited_copy(rep.ad_copy, h, b, c, d.splitlines())
        fp = copy_fingerprint(edited)
        prev = ss.rechecks.get(vid)
        if st.button("수정한 문구 다시 검사", key=f"recheck_{vid}", type="primary", disabled=bool(ss.running)):
            if prev and prev["fingerprint"] == fp:
                st.info("같은 문구를 이미 검사했습니다. 결과는 아래에 있습니다.")
            else:
                ss.running = True
                try:
                    settings: Settings = make_settings(*ss.get("user_keys", ("", "")), **entry.get("overrides", {}))
                    brief = entry["brief"]
                    categories = catalog.effective_categories(brief.product.category, edited.full_text(), brief.product.raw_description)
                    with st.spinner("다시 검사하는 중…"):
                        res = recheck(edited, brief.product, categories, settings, router=router_factory(settings), landing_text=entry.get("landing", ""),
                                      rejected_history=list(ss.rejected_history), catalog=catalog, tracer=tracer)
                    ss.rechecks[vid] = {"fingerprint": fp, "result": res, "at": datetime.now(), "copy": edited}
                    prev = ss.rechecks[vid]
                except Exception as e:  # noqa: BLE001
                    st.error(f"다시 검사하지 못했습니다: {e}. 편집한 문구는 그대로 남아 있습니다.")
                finally:
                    ss.running = False
        if prev:
            res = prev["result"]
            r2: PreflightReport = res.report
            current = prev["fingerprint"] == fp
            if not current:
                st.warning("문구가 바뀌었습니다. 아래 결과는 **이전에 검사한 문구**에 대한 것입니다. 다시 검사하세요.", icon="⚠️")
            st.markdown(f"재검사 결과 {theme.badge(r2.final_decision, big=True)} &nbsp; <span class='pf-muted'>{html.escape(RUN_MODE_LABEL[res.mode])} · {prev['at'].strftime('%H:%M:%S')}"
                        f"{' · ≈ $%.4f (추정)' % res.cost_usd if res.cost_estimated else ' · $%.4f' % res.cost_usd}</span>", unsafe_allow_html=True)
            st.markdown(f"<div class='pf-reason'>{html.escape(plain_reason(r2, catalog))}</div>", unsafe_allow_html=True)
            render_ad(prev["copy"], r2.verdict.findings, title="검사한 문구", catalog=catalog)
            fs = [f for f in r2.verdict.findings if f.verified_quote or f.layer.value != "semantic"]
            if fs:
                st.markdown("**확인이 필요한 구절**")
                for f in fs:
                    st.markdown(f"<div class='pf-reason'>{theme.severity_chip(f.severity)} {html.escape(finding_sentence(f, catalog))}</div>", unsafe_allow_html=True)
            if r2.final_decision == Decision.HUMAN_REVIEW:
                st.info("재검사 결과도 담당자 확인이 필요합니다: " + human_review_reason(r2), icon="ℹ️")
            if r2.final_decision == Decision.APPROVE and current:
                st.success("이 문구는 검사 기준을 통과했습니다. (이 프로젝트의 검사 기준 기준이며 실제 플랫폼 심사가 아닙니다.)", icon="✅")


def _details(rep: PreflightReport, entry: dict, catalog) -> None:
    with st.expander("상세 — 검사 항목, 가상 사용자 반응, 검토 모델, 규칙 ID"):
        st.markdown("**검사 항목**")
        st.dataframe(checklist_rows(rep), width="stretch", hide_index=True)
        a = rep.audience
        st.markdown("**가상 사용자 반응**")
        if a is None:
            st.caption("가상 사용자 반응 단계가 꺼져 있거나 실패했습니다.")
        else:
            st.caption("검토 모델이 만든 가상 의견입니다. 실제 사용자 조사가 아니며 참고용입니다.")
            k = st.columns(4)
            k[0].metric("끌림", a.mean_appeal); k[1].metric("신뢰", a.mean_trust); k[2].metric("클릭 의향", f"{a.click_rate:.0%}"); k[3].metric("의견 차이", a.diversity, help="가상 사용자 점수의 표준편차. 낮으면 모두 같은 말을 한 것이라 참고 가치가 낮습니다.")
            if a.sycophancy_flag:
                st.caption("가상 사용자들이 거의 같은 말을 했습니다 — 정보량이 낮아 참고용으로만 둡니다.")
            for f in a.feedbacks:
                st.markdown(f"- **{f.persona_name}** · 끌림 {f.appeal} · 신뢰 {f.trust} · {'클릭함' if f.would_click else '클릭 안 함'} — “{f.quote}” (걱정: {f.concern})")
        v = rep.verdict
        st.markdown("**검토 모델**")
        st.markdown(f"판정 {DECISION_LABEL[v.decision]} · 확신 {v.confidence:.2f} · `{v.provider}/{v.model}`"
                    + (f" · 교차 확인 {DECISION_LABEL[v.second_opinion]} ({'다름' if v.disagreement else '같음'}, `{v.second_opinion_provider}`{'' if v.second_opinion_independent else ', 같은 제공자라 독립적이지 않음'})" if v.second_opinion else ""))
        if v.rationale:
            st.caption(v.rationale)
        st.markdown("**규칙 ID (내부 식별자)**")
        rows = [{"규칙": f.rule_id, "심각도": f.severity.value, "계층": f.layer.value, "인용": f.quote or "—", "인용 확인": "예" if f.verified_quote else "아니요 (원문에 없음)", "설명": f.explanation} for f in v.findings]
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
        else:
            st.caption("검사에서 나온 항목이 없습니다.")
        if rep.rewrite_status == RewriteStatus.FAILED_RECHECK and rep.rewrite_recheck_findings:
            st.markdown("**자동 수정안이 재검사에서 걸린 항목**")
            st.dataframe([{"규칙": f.rule_id, "심각도": f.severity.value, "인용": f.quote or "—", "설명": f.explanation} for f in rep.rewrite_recheck_findings], width="stretch", hide_index=True)
        st.caption(f"판정 근거(내부): {rep.decision_reason} · 수정안: {rep.rewrite_note}")


def _variant(i: int, rep: PreflightReport, entry: dict, catalog, tracer: Tracer, router_factory, make_settings) -> None:
    run: PipelineRun = entry["run"]
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        with h1:
            st.markdown(f"### 광고안 {i}")
        with h2:
            st.markdown(f"<div style='text-align:right;padding-top:14px'>{theme.badge(rep.final_decision, big=True)}</div>", unsafe_allow_html=True)
        st.caption(DECISION_HELP[rep.final_decision])
        left, right = st.columns([1, 1])
        with left:
            st.markdown("**광고 원문** <span class='pf-muted'>— 확인이 필요한 구절은 밑줄로 표시</span>", unsafe_allow_html=True)
            render_ad(rep.ad_copy, rep.verdict.findings, catalog=catalog)
        with right:
            st.markdown("**가장 중요한 이유**")
            st.markdown(f"<div class='pf-reason'>{html.escape(plain_reason(rep, catalog))}</div>", unsafe_allow_html=True)
            fs = [f for f in rep.verdict.findings if f.verified_quote or f.layer.value != "semantic"]
            if fs:
                st.markdown("**확인이 필요한 구절**")
                for f in fs:
                    st.markdown(f"<div class='pf-reason'>{theme.severity_chip(f.severity)} {html.escape(finding_sentence(f, catalog))}</div>", unsafe_allow_html=True)
            st.markdown("**판단 근거**")
            for line in evidence_lines(rep, entry["brief"].product, catalog):
                st.markdown(f"- {line}")
            st.markdown("**다음에 할 수 있는 것**")
            st.markdown(NEXT_ACTION[rep.final_decision])
        _rewrite_block(rep, catalog)
        if rep.final_decision == Decision.HUMAN_REVIEW:
            _human_review_block(rep, entry, catalog)
        _edit_and_recheck(rep, entry, catalog, tracer, router_factory, make_settings)
        _details(rep, entry, catalog)


def _failures(entry: dict) -> None:
    run: PipelineRun = entry["run"]
    if not run.failures:
        return
    with st.expander(f"처리 중 생긴 문제 {len(run.failures)}건 — 모두 대체 경로로 처리됨"):
        st.caption("모델이 틀리거나 실패할 수 있는 지점마다 미리 정해 둔 대체 경로가 있습니다. 어떤 경우에도 '검사 실패 = 통과' 로 흐르지 않습니다.")
        st.dataframe([{"코드": f.code, "이름": BY_CODE[f.code].name if f.code in BY_CODE else "", "단계": f.stage, "무슨 일": f.message, "처리": f.fallback} for f in run.failures],
                     width="stretch", hide_index=True)


def _results(entry: dict, catalog, tracer: Tracer, router_factory, make_settings) -> None:
    st.markdown("## 검사 결과")
    stale = entry["fingerprint"] != input_fingerprint(_form())
    _state_banner(entry, stale)
    run: PipelineRun = entry["run"]
    _summary(entry)
    _intercept(entry, catalog)
    if run.halted and not run.reports:
        _failures(entry)
        return
    _plan(entry)
    for i, rep in enumerate(run.reports, start=1):
        _variant(i, rep, entry, catalog, tracer, router_factory, make_settings)
    _failures(entry)


def _empty_state() -> None:
    with st.container(border=True):
        st.markdown("**아직 결과가 없습니다.** 위에서 예시를 고르거나 직접 입력한 뒤 **광고 초안 만들기**를 누르세요.")
        st.markdown("결과로 받는 것: 광고안별로 ① 광고 원문과 확인이 필요한 구절, ② 이유, ③ 검사를 통과한 수정 제안, ④ 다음에 할 일. "
                    "자동으로 확정하지 못한 광고안은 '담당자 확인 필요' 로 표시되고 담당자에게 전달할 내용이 만들어집니다.")


def render(make_settings, tracer: Tracer, router_factory, server_status: dict) -> None:
    catalog = get_catalog()
    _init_state()
    ss = st.session_state
    qp = st.query_params
    if ss.pop("pending_load", None):
        pass  # app.py 에서 이미 폼에 넣었다
    if "_qp_preset_done" not in ss and qp.get("preset") in PRESETS:
        _load_preset(qp.get("preset"))
        ss._qp_preset_done = True
    autorun = qp.get("autorun") in ("1", "true") and not ss.get("_autorun_done")
    settings, options, mode, clicked = _inputs(make_settings, server_status)
    if autorun:
        ss._autorun_done = True
        clicked = True
    if clicked:
        _execute(settings, options, tracer, router_factory, mode)
    if not ss.runs:
        _empty_state()
        return
    _results(ss.runs[-1], catalog, tracer, router_factory, make_settings)
