"""광고 작업 — 상품 정보 입력 → 광고안 생성 → 검사 결과 → 수정 제안 → 직접 고쳐서 다시 검사.

상태: 입력 전 / 실행 중 / 완료 / 일부 단계 실패 / 전체 실행 실패 / 결과 없음 / 입력 변경 후 재실행 필요 / 담당자 확인 필요.
판정·검사 로직은 전부 agents/·guardrails/ 의 것을 부른다. 여기서는 그리기만 한다.
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import html
import time
from datetime import datetime

import streamlit as st

from agents.pipeline import RunOptions, run_pipeline
from agents.recheck import RecheckHistory, RecheckResult, build_edited_copy, recheck, recheck_key
from core.config import IN_BROWSER, Settings
from core.schemas import Decision, PipelineRun, PreflightReport, ProductCategory, RewriteStatus, Severity
from evals.runner import recommendation_for
from guardrails.failure_modes import BY_CODE
from guardrails.failsafe import STEP_FAILURE_CODES
from guardrails.policy import get_catalog
from llm.router import Router
from observability.tracer import Tracer
from ui import theme
from ui.components import (
    checklist_rows,
    evidence_lines,
    finding_line,
    finding_sentence,
    human_review_reason,
    human_review_request_text,
    recheck_request_text,
    plain_reason,
    render_ad,
    rule_title,
    unique_findings,
    what_to_check,
)
from ui.labels import DECISION_HELP, DECISION_LABEL, LAYER_LABEL, NEXT_ACTION, REWRITE_SHORT_LABEL, REWRITE_STATUS_LABEL, RUN_MODE_HELP, RUN_MODE_LABEL, SEVERITY_LABEL, TERM
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
# 다른 화면으로 갔다 오면 Streamlit 이 그 화면에 없던 위젯의 값을 지운다. 그래서 값은 _store 에 따로 두고, 위젯을 그리기 전에 되살린다.
PERSIST_KEYS = FORM_KEYS + ("opt_n_variants", "opt_audience", "opt_second", "opt_tau", "opt_budget", "opt_drill", "opt_oa_model", "opt_an_model",
                            "user_openai_key", "user_anthropic_key")


def _restore(keys=PERSIST_KEYS) -> None:
    store = st.session_state.setdefault("_store", {})
    for k in keys:
        if k not in st.session_state and k in store:
            st.session_state[k] = store[k]


def _persist(keys=PERSIST_KEYS) -> None:
    store = st.session_state.setdefault("_store", {})
    for k in keys:
        if k in st.session_state:
            store[k] = st.session_state[k]
STEPS = [("intercept", "요청 검사"), ("planner", "캠페인 구성"), ("copywriter", "광고 초안 작성"), ("review", "광고안 검사·판정")]


# --- 상태 ---------------------------------------------------------------------
def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("runs", [])
    ss.setdefault("rechecks", {})
    ss.setdefault("rejected_history", [])
    ss.setdefault("running", False)
    _restore()
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
    _persist(FORM_KEYS)


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
    if run.halted:
        return "partial"
    # 모델 호출이 실패해 끝나지 못한 단계가 하나라도 있으면 정상 실행이 아니다: 심사·교차 확인·수정안 의미 검사가 실패한 경우를 포함한다
    stage_fail = any(f.code in STEP_FAILURE_CODES or f.stage in ("planner", "audience") for f in run.failures) or run.plan is None
    if stage_fail:
        return "partial"
    return "done"


# --- 입력 ---------------------------------------------------------------------
def _inputs(make_settings, server_status: dict) -> tuple[Settings, RunOptions, str, bool]:
    """입력 폼 + 상세 옵션. (settings, options, 실행 방식, 실행 버튼 눌림)"""
    ss = st.session_state
    st.markdown("## 광고 검사")
    st.markdown("상품 정보와 꼭 들어가야 할 조건을 적으면 광고안을 만들고, 이 프로젝트의 검사 기준으로 **확인이 필요한 구절**을 찾아 이유와 수정 제안을 보여 줍니다.")
    with st.container(border=True):
        st.markdown("### 시작하기")
        preset = st.selectbox("예시 선택", options=list(PRESETS.keys()), format_func=lambda k: PRESET_TITLES.get(k, k), key="preset_pick",
                              help="예시를 고른 뒤 바로 실행하거나, 불러와서 내용을 고쳐 실행할 수 있습니다. 처음부터 직접 입력해도 됩니다.")
        b1, b2 = st.columns(2)
        with b1:
            if st.button("예시로 바로 실행", type="primary", width="stretch", key="example_run", disabled=bool(ss.running),
                         help="선택한 예시를 불러와 AI 호출 없이 처음부터 끝까지 실행합니다 (예시 실행)."):
                _load_preset(preset)
                ss._force_autorun = True
                ss._force_demo_run = True   # 키가 있어도 이 버튼은 예시 실행이다 — 비용이 드는 실행은 아래에서 따로, 확인 뒤에만
                st.rerun()
        with b2:
            if st.button("예시 불러오기", width="stretch", key="example_load"):
                _load_preset(preset)
                st.rerun()
        st.markdown("<div class='pf-kv'><b>세 가지 결과는 서로 다릅니다.</b> "
                    "① <b>예시 실행</b> — 이 화면에서 바로, AI 호출 없이 (모델 단계는 정해진 규칙의 대역). "
                    "② <b>저장된 실제 모델 결과</b> — ‘검사 사례’ 에서 열람만 (호출 없음). "
                    "③ <b>새 실제 모델 실행</b> — API 키가 있을 때만, 아래 ‘상세 옵션’ 에서 확인한 뒤 실행 (비용 발생).</div>", unsafe_allow_html=True)
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
        ss.setdefault("opt_n_variants", 3); ss.setdefault("opt_audience", True); ss.setdefault("opt_second", True)
        ss.setdefault("opt_oa_model", "gpt-4o"); ss.setdefault("opt_an_model", "claude-sonnet-5-5")
        ss.setdefault("user_openai_key", ""); ss.setdefault("user_anthropic_key", "")
        o1, o2, o3 = st.columns(3)
        with o1:
            n_variants = st.slider("광고안 수", 1, 5, key="opt_n_variants")
        with o2:
            enable_audience = st.toggle("가상 사용자 반응 포함", key="opt_audience", help=TERM["audience"])
        with o3:
            enable_second = st.toggle("다른 모델 계열의 교차 확인", key="opt_second", help="확신이 낮거나 참고 사항이 있는 광고안만 다른 모델 계열에 한 번 더 묻습니다.")
        user_oa, user_an = "", ""
        oa_model, an_model = ss.opt_oa_model, ss.opt_an_model
        if IN_BROWSER:
            st.caption("브라우저 실행판은 서버가 없어 API 키를 쓸 수 없고 항상 예시 실행입니다. 실제 모델 결과는 '검사 사례' 에 저장돼 있습니다.")
        else:
            st.markdown("**모델과 API 키**")
            st.caption(f"서버에 설정된 키: OpenAI {'있음' if server_status['openai'] else '없음'} · Anthropic {'있음' if server_status['anthropic'] else '없음'}. "
                       "서버 키 값은 화면에 보이지 않습니다. 직접 키를 넣으면 이 세션에서만 그 키를 씁니다 (화면을 오가도 유지).")
            k1, k2 = st.columns(2)
            with k1:
                user_oa = st.text_input("내 OpenAI API 키 (선택)", type="password", key="user_openai_key", autocomplete="off")
                oa_model = st.text_input("OpenAI 모델", key="opt_oa_model")
            with k2:
                user_an = st.text_input("내 Anthropic API 키 (선택)", type="password", key="user_anthropic_key", autocomplete="off")
                an_model = st.text_input("Anthropic 모델", key="opt_an_model")
        provisional = make_settings(user_oa.strip(), user_an.strip(), openai_model=oa_model.strip() or "gpt-4o", anthropic_model=an_model.strip() or "claude-sonnet-5-5")
        rec = recommendation_for(provisional)
        st.markdown("**판정 기준**")
        default_tau = float(rec["tau"]) if rec["tau"] is not None else float(provisional.auto_decision_confidence)
        ss.setdefault("opt_tau", default_tau)
        tau = st.slider("자동 판정에 필요한 검토 모델의 최소 확신", 0.5, 0.99, step=0.01, key="opt_tau",
                        help="검토 모델의 확신이 이 값보다 낮으면 자동으로 확정하지 않고 담당자 확인으로 넘깁니다. 게재 불가 사유가 코드로 확인된 광고는 이 값과 무관하게 게재 불가입니다.")
        if rec["tau"] is not None:
            st.caption(f"기본값 {rec['tau']:.2f} 은 같은 제공자·모델·프롬프트·검사 기준·평가 데이터로 평가해 '문제 있는 광고를 통과시킨 사례 0건·문제없는 광고에 수정 요청 0건' 을 만족한 가장 낮은 값입니다.")
        else:
            st.caption("이 설정의 검증된 추천값 없음 — " + (", ".join(rec["mismatches"]) if rec["mismatches"] else rec["reason"]) + ". 기본값은 설정값(0.80)을 씁니다.")
        b1, b2 = st.columns(2)
        with b1:
            # 서버 키를 쓰는 실행은 서버가 정한 상한(PREFLIGHT_RUN_BUDGET_USD)을 넘을 수 없다. 자기 키를 넣은 경우에만 자유롭다.
            using_server_key = not (user_oa.strip() or user_an.strip())
            server_cap = float(server_status.get("run_budget_usd") or 0.5)
            ss.setdefault("opt_budget", min(server_cap, 0.5) if using_server_key else 0.5)
            if using_server_key and ss.opt_budget > server_cap:
                ss.opt_budget = server_cap
            budget = st.number_input("실행당 비용 상한 (USD)", min_value=0.01, max_value=(server_cap if using_server_key else None), step=0.05, key="opt_budget",
                                     help="실제 모델 호출 비용이 이 값을 넘으면 실행을 멈추고 남은 광고안은 담당자 확인으로 둡니다. 실패한 요청의 비용은 알 수 없어 '추정' 으로 표시합니다."
                                     + (f" 서버 키를 쓸 때는 서버가 정한 상한 ${server_cap:.2f} 까지입니다." if using_server_key else ""))
        with b2:
            ss.setdefault("opt_drill", "없음")
            drill = st.selectbox("장애 시뮬레이션 (기술 시연용 — 광고 작업에만 적용)", ["없음", "openai: timeout", "openai: schema", "anthropic: timeout", "anthropic: refusal", "mock: timeout"], key="opt_drill",
                                 help="모델 호출을 일부러 실패시켜 대체 경로와 '실패해도 통과로 흐르지 않음' 을 보여 줍니다. 품질 확인의 평가 실행에는 적용되지 않습니다.")
        _persist()
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
    confirmed = True
    with c1:
        st.markdown(theme.mode_pill("demo" if mode == "demo" else "live", RUN_MODE_LABEL[mode]), unsafe_allow_html=True)
        st.caption(RUN_MODE_HELP[mode])
        if mode != "demo":
            calls = 2 + 4 * options.n_variants
            st.warning(f"새 실제 모델 실행입니다 — 모델 호출 최대 약 {calls}회, 비용은 ${float(budget):.2f} 에서 멈춥니다 (실행당 상한).", icon="💳")
            confirmed = st.checkbox("위 호출 수와 비용 상한을 확인했고 실제 모델로 실행합니다", key="confirm_live_run")
    with c2:
        clicked = st.button("광고 초안 만들기", type="primary", width="stretch", disabled=bool(ss.running) or not confirmed, key="run_button")
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
    if run.halted and run.reports:
        st.warning(f"도중에 멈췄습니다 — {run.halt_reason}  \n검사하지 못한 광고안은 '담당자 확인 필요' 로 두었고, 코드 검사만으로 이미 사유가 확인된 광고안에는 모델 검사를 하지 못했다고 적었습니다. 비용 상한을 올리거나 광고안 수를 줄여 다시 실행할 수 있습니다.", icon="⚠️")
    if state == "failed":
        st.error(f"실행을 끝내지 못했습니다 — {run.halt_reason}  \n입력은 그대로 남아 있습니다. 요청을 고치거나 상세 옵션을 바꿔 다시 실행하세요.", icon="⛔")
    elif state == "empty":
        st.warning("광고안이 만들어지지 않았습니다. 상품 정보와 사실 목록을 조금 더 구체적으로 적고 다시 실행해 보세요.", icon="ℹ️")
    elif state == "partial":
        names = {"planner": "캠페인 구성", "audience": "가상 사용자 반응", "reviewer": "검토 모델 검사", "second_opinion": "교차 확인", "rewrite_recheck": "수정안 의미 검사"}
        parts = sorted({f.stage for f in run.failures if f.stage in names and (f.code in STEP_FAILURE_CODES or f.stage in ("planner", "audience"))})
        what = ", ".join(names.get(p, p) for p in parts) or "일부 단계"
        judged = any(p in ("reviewer", "rewrite_recheck", "second_opinion") for p in parts)
        st.info(f"{what} 단계가 실패해 그 부분 없이 진행했습니다. "
                + ("실패한 단계의 광고안은 자동으로 확정하지 않고 담당자 확인으로 두었거나, 수정안을 '확인하지 못함' 으로 처리했습니다. 같은 입력으로 다시 실행하면 다시 시도합니다." if judged
                   else "광고안 검사와 판정은 정상적으로 끝났습니다."), icon="ℹ️")


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


def _rewrite_section(rep: PreflightReport, catalog) -> None:
    """수정 내역 — 자동 수정안의 상태(수정안 확인 완료 / 다시 수정 필요 / 확인하지 못함 / 없음)와 원문 대비 바뀐 점."""
    st.markdown("**수정 내역**")
    status = rep.rewrite_status
    st.markdown(theme.chip(REWRITE_SHORT_LABEL[status]), unsafe_allow_html=True)
    if status == RewriteStatus.VERIFIED and rep.verified_rewrite is not None:
        st.caption("검토 모델이 제안한 문구를 원문과 같은 검사 기준으로 다시 검사해 통과한 것만 보여 줍니다.")
        l, r = st.columns(2)
        with l:
            render_ad(rep.ad_copy, [], title="원문", catalog=catalog)
        with r:
            render_ad(rep.verified_rewrite, [], title="수정안 확인 완료", catalog=catalog)
        _diff_explained(rep, catalog)
    elif status in (RewriteStatus.FAILED_RECHECK, RewriteStatus.UNCHECKED):
        why = ""
        if rep.rewrite_recheck_findings:
            why = " 재검사에서 걸린 것: " + "; ".join(sorted({rule_title(f.rule_id, catalog) for f in rep.rewrite_recheck_findings}))
        st.info(REWRITE_STATUS_LABEL[status] + "." + why + " 아래 ‘문구 고쳐서 다시 검사’ 에서 직접 고칠 수 있습니다.", icon="ℹ️")
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
    st.markdown("바뀐 점")
    st.markdown("\n".join(changes) if changes else "- 바뀐 문구가 없습니다.")
    if reasons:
        st.caption("고친 이유: " + " · ".join(reasons))


def _history(entry: dict, vid: str) -> RecheckHistory:
    return st.session_state.rechecks.setdefault(f"{entry['run'].run_id}:{vid}", RecheckHistory())


def _human_review_block(rep: PreflightReport, entry: dict, catalog) -> None:
    run: PipelineRun = entry["run"]
    with st.container(border=True):
        st.markdown("**담당자 확인이 필요합니다**")
        st.markdown("담당자가 확인할 것:")
        for x in what_to_check(rep, catalog):
            st.markdown(f"  - {x}")
        st.markdown("다음 단계 진행: **불가** — 담당자 확인 전에는 게재할 수 없습니다.")
        st.caption("이 화면에는 검토 대기열이나 담당자 처리 기능이 없습니다. 요청문을 만들어 복사·내려받을 수 있을 뿐이고, 전달은 직접 하셔야 합니다.")
        if _history(entry, rep.variant_id).latest() is not None:
            st.warning("아래 요청문은 **수정 전 광고 원문**에 대한 것입니다. 문구를 고쳐 다시 검사했다면 ‘문구 고쳐서 다시 검사’ 안의 최신 요청문을 쓰세요.", icon="⚠️")
        text = human_review_request_text(rep, run, catalog)
        with st.expander("확인 요청 내용 (수정 전 원문 · 복사용)"):
            st.code(text, language=None, wrap_lines=True)
            if not IN_BROWSER:
                st.download_button("확인 요청 내용 내려받기 (.txt)", data=text, file_name=f"review_request_{rep.variant_id}.txt", mime="text/plain", key=f"dl_{rep.variant_id}")


def _usage_line(u: dict) -> str:
    cost = f"${u['cost_usd']:.4f}"
    if u["cost_estimated"]:
        cost = f"≥ {cost} (응답을 받지 못한 요청 {u['failed_attempts']}건의 비용은 알 수 없음)" if u["failed_attempts"] else f"≈ {cost} (추정)"
    return f"이 문구 검사 {u['tries']}번 시도" + (f" · 실패 {u['failed_tries']}번" if u["failed_tries"] else "") + f" · 모델 호출 {u['calls']}회(요청 {u['attempts']}회) · 비용 {cost}"


def _edit_and_recheck(rep: PreflightReport, entry: dict, catalog, tracer: Tracer, router_factory, make_settings) -> None:
    ss = st.session_state
    vid = rep.variant_id
    base = rep.verified_rewrite if rep.verified_rewrite is not None else rep.ad_copy
    hist = _history(entry, vid)
    with st.expander("문구 고쳐서 다시 검사", expanded=rep.final_decision in (Decision.REVISE, Decision.REJECT)):
        st.caption("아래 칸을 고치고 '수정한 문구 다시 검사' 를 누르면 같은 검사 기준으로 다시 검사합니다. 결과는 그 시점의 문구에만 해당합니다.")
        keys = (f"edit_h_{vid}", f"edit_b_{vid}", f"edit_c_{vid}", f"edit_d_{vid}")
        _restore(keys)
        ss.setdefault(keys[0], base.headline); ss.setdefault(keys[1], base.body); ss.setdefault(keys[2], base.cta); ss.setdefault(keys[3], "\n".join(base.disclosures))
        h = st.text_input("제목", key=keys[0])
        b = st.text_area("본문", key=keys[1], height=80)
        c1, c2 = st.columns([1, 2])
        with c1:
            c = st.text_input("버튼 문구", key=keys[2])
        with c2:
            d = st.text_area("안내 문구 (한 줄에 하나)", key=keys[3], height=80)
        _persist(keys)
        edited = build_edited_copy(rep.ad_copy, h, b, c, d.splitlines())
        brief = entry["brief"]
        settings: Settings = make_settings(*ss.get("user_keys", ("", "")), **entry.get("overrides", {}))
        if entry.get("mode") == "demo":
            settings = dataclasses.replace(settings, force_demo=True)   # 예시 실행의 재검사도 예시 실행 — 키가 있어도 비용이 들지 않는다
        own = {rep.ad_copy.full_text()} | {r.report.ad_copy.full_text() for r in hist.attempts}
        history = [x for x in ss.rejected_history if x not in own]  # 고치고 있는 광고 자신은 '다른 반려 광고' 가 아니다
        # 재사용 키: 문구뿐 아니라 상품 정보·랜딩·검사 기준·프롬프트·모델·판정 기준이 같을 때만 이전 결과를 다시 쓴다
        key = recheck_key(edited, brief.product, entry.get("landing", ""), settings, catalog, history)
        last_try = hist.latest_for(key)
        label = "같은 문구로 다시 시도" if (last_try is not None and last_try.failed) else "수정한 문구 다시 검사"
        rerun_after = False
        if st.button(label, key=f"recheck_{vid}", type="primary", disabled=bool(ss.running)):
            if hist.reusable_for(key) is not None:
                st.info("같은 문구·같은 조건으로 이미 검사했습니다. 모델을 다시 부르지 않고 그 결과를 보여 드립니다.")
            else:
                ss.running = True
                try:
                    categories = catalog.effective_categories(brief.product.category, edited.full_text(), brief.product.raw_description)
                    with st.spinner("다시 검사하는 중…"):
                        res = recheck(edited, brief.product, categories, settings, router=router_factory(settings), landing_text=entry.get("landing", ""),
                                      rejected_history=history, catalog=catalog, tracer=tracer)
                    hist.add(res)
                    last_try = res
                    rerun_after = True  # 버튼 이름('같은 문구로 다시 시도') 과 시도 기록을 방금 결과에 맞추려면 한 번 더 그려야 한다
                except Exception as e:  # noqa: BLE001
                    st.error(f"다시 검사하지 못했습니다: {e}. 편집한 문구는 그대로 남아 있습니다.")
                finally:
                    ss.running = False
        if rerun_after:
            st.rerun()
        shown: RecheckResult | None = hist.latest_for(key)
        if shown is None and hist.latest() is not None:
            st.warning("문구가 바뀌었습니다. 아래 결과는 **이전에 검사한 문구**에 대한 것입니다. 다시 검사하세요.", icon="⚠️")
            shown = hist.latest()
        if shown is not None:
            _recheck_result(shown, current=(shown.key == key), hist=hist, key=key, entry=entry, rep=rep, catalog=catalog)


def _recheck_result(res: RecheckResult, *, current: bool, hist: RecheckHistory, key: str, entry: dict, rep: PreflightReport, catalog) -> None:
    r2: PreflightReport = res.report
    if res.failed:
        st.error("검토 모델 호출이 실패해 이 문구를 끝까지 확인하지 못했습니다" + (f" ({res.review_error or res.rewrite_error})" if (res.review_error or res.rewrite_error) else "")
                 + ". 통과로 보지 않습니다. 같은 문구로 다시 시도할 수 있습니다.", icon="⛔")
    st.markdown(f"재검사 결과 {theme.badge(r2.final_decision, big=True)} &nbsp; <span class='pf-muted'>{html.escape(RUN_MODE_LABEL[res.mode])} · {res.at.strftime('%H:%M:%S')}"
                f"{' · ≈ $%.4f (추정)' % res.cost_usd if res.cost_estimated else ' · $%.4f' % res.cost_usd}</span>", unsafe_allow_html=True)
    if current:
        st.caption(_usage_line(hist.usage_for(key)))
    st.markdown(f"<div class='pf-reason'>{html.escape(plain_reason(r2, catalog))}</div>", unsafe_allow_html=True)
    render_ad(r2.ad_copy, r2.verdict.findings, title="검사한 문구" if current else "이전에 검사한 문구", catalog=catalog)
    fs = [f for f in r2.verdict.findings if f.verified_quote or f.layer.value != "semantic"]
    if fs:
        st.markdown("**확인이 필요한 구절**")
        for f, n in unique_findings(fs, catalog):
            st.markdown(f"<div class='pf-reason'>{theme.severity_chip(f.severity)} {html.escape(finding_line(f, n, catalog))}</div>", unsafe_allow_html=True)
    if r2.final_decision == Decision.HUMAN_REVIEW and not res.failed:
        st.info("재검사 결과도 담당자 확인이 필요합니다: " + human_review_reason(r2), icon="ℹ️")
    if r2.final_decision == Decision.APPROVE and current and not res.failed:
        st.success("이 문구는 검사 기준을 통과했습니다. (이 프로젝트의 검사 기준 기준이며 실제 플랫폼 심사가 아닙니다.)", icon="✅")
    if r2.final_decision == Decision.HUMAN_REVIEW:
        if current:
            with st.container(border=True):
                st.markdown("**이 문구에 대한 담당자 확인 요청문**")
                st.caption("방금 검사한 문구와 그 재검사 결과·처리 기록으로 만든 내용입니다. 요청문만 만들어 줄 뿐 전달·대기열·권한 관리는 없습니다.")
                text = recheck_request_text(r2.ad_copy, res, rep.variant_id, entry["run"].run_id, catalog)
                with st.expander("확인 요청 내용 (재검사한 문구 · 복사용)"):
                    st.code(text, language=None, wrap_lines=True)
                    if not IN_BROWSER:
                        st.download_button("확인 요청 내용 내려받기 (.txt)", data=text, file_name=f"review_request_{rep.variant_id}_recheck.txt", mime="text/plain",
                                           key=f"dl_re_{rep.variant_id}_{res.trace_id}")
        else:
            st.caption("문구를 바꿨기 때문에 이전 결과로는 확인 요청문을 만들지 않습니다. 다시 검사한 뒤 만들 수 있습니다.")
    if current and hist.usage_for(key)["failed_tries"]:
        with st.expander("이 문구의 검사 시도 기록"):
            st.dataframe([{"시도": i, "결과": "실패 (모델 호출)" if a.failed else DECISION_LABEL[a.report.final_decision], "이유": a.review_error or a.rewrite_error or "—",
                           "시각": a.at.strftime("%H:%M:%S"), "모델 호출": a.calls, "실패한 요청": a.failed_attempts, "처리 기록": a.trace_id}
                          for i, a in enumerate(hist.attempts_for(key), start=1)], width="stretch", hide_index=True)


def _details(rep: PreflightReport, entry: dict, catalog) -> None:
    with st.expander("상세 기록 — 판단 근거, 검사 항목, 가상 사용자 반응, 검토 모델, 규칙 코드"):
        st.markdown("**판단 근거**")
        for line in evidence_lines(rep, entry["brief"].product, catalog):
            st.markdown(f"- {line}")
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
        st.markdown("**규칙 코드 (내부 식별자)**")
        rows = [{"규칙 코드": f.rule_id, "구분": SEVERITY_LABEL[f.severity], "검사": LAYER_LABEL[f.layer], "인용": f.quote or "—", "인용 확인": "예" if f.verified_quote else "아니요 (원문에 없음)", "설명": f.explanation} for f in v.findings]
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
        else:
            st.caption("검사에서 나온 항목이 없습니다.")
        if rep.rewrite_status == RewriteStatus.FAILED_RECHECK and rep.rewrite_recheck_findings:
            st.markdown("**자동 수정안이 재검사에서 걸린 항목**")
            st.dataframe([{"규칙 코드": f.rule_id, "구분": SEVERITY_LABEL[f.severity], "인용": f.quote or "—", "설명": f.explanation} for f in rep.rewrite_recheck_findings], width="stretch", hide_index=True)
        st.caption(f"판정 근거(내부): {rep.decision_reason} · 수정안: {rep.rewrite_note}")


def _spans_section(rep: PreflightReport, catalog) -> None:
    """수정할 문구 — 광고 원문에서 확인이 필요한 구절을 표시하고, 구절마다 이유를 한 번만 적는다."""
    fs = [f for f in rep.verdict.findings if f.verified_quote or f.layer.value != "semantic"]
    st.markdown("**수정할 문구**")
    left, right = st.columns([1, 1])
    with left:
        render_ad(rep.ad_copy, rep.verdict.findings, title="광고 원문 — 확인이 필요한 구절은 밑줄로 표시", catalog=catalog)
    with right:
        if fs:
            for f, n in unique_findings(fs, catalog):
                st.markdown(f"<div class='pf-reason'>{theme.severity_chip(f.severity)} {html.escape(finding_line(f, n, catalog))}</div>", unsafe_allow_html=True)
        else:
            st.caption("표시할 구절이 없습니다.")


def _variant(i: int, rep: PreflightReport, entry: dict, catalog, tracer: Tracer, router_factory, make_settings) -> None:
    run: PipelineRun = entry["run"]
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        with h1:
            st.markdown(f"### 광고안 {i}")
        with h2:
            st.markdown(f"<div style='text-align:right;padding-top:14px'>{theme.badge(rep.final_decision, big=True)}</div>", unsafe_allow_html=True)
        st.caption(DECISION_HELP[rep.final_decision])
        fs = [f for f in rep.verdict.findings if f.verified_quote or f.layer.value != "semantic"]
        # 이유는 한 번만: 구절별 이유가 아래 ‘수정할 문구’ 에 있으면 여기서는 개수만, 구절이 없는 판정(통과·담당자 확인)은 이유를 여기에 쓴다
        if rep.final_decision in (Decision.REVISE, Decision.REJECT) and fs:
            st.markdown(f"<div class='pf-reason'>확인이 필요한 구절 {len(unique_findings(fs, catalog))}가지 — 구절별 이유는 아래 ‘수정할 문구’ 에 있습니다.</div>", unsafe_allow_html=True)
        else:
            st.markdown(f"<div class='pf-reason'>{html.escape(plain_reason(rep, catalog))}</div>", unsafe_allow_html=True)
        if rep.final_decision == Decision.APPROVE and not fs and rep.rewrite_status == RewriteStatus.NONE:
            # 고칠 것이 없는 광고안: 빈 ‘수정할 문구’·‘수정 내역’ 을 늘어놓지 않고 원문만 보여 준다
            st.markdown("**광고 원문**")
            render_ad(rep.ad_copy, [], catalog=catalog)
        else:
            _spans_section(rep, catalog)
            _rewrite_section(rep, catalog)
        st.markdown("**다음 작업**")
        st.markdown(NEXT_ACTION[rep.final_decision])
        if rep.final_decision == Decision.HUMAN_REVIEW:
            _human_review_block(rep, entry, catalog)
        _edit_and_recheck(rep, entry, catalog, tracer, router_factory, make_settings)
        _details(rep, entry, catalog)


def _failures(entry: dict) -> None:
    run: PipelineRun = entry["run"]
    if not run.failures:
        return
    with st.expander(f"상세 기록 — 처리 중 생긴 문제 {len(run.failures)}건, 모두 대체 경로로 처리됨"):
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
    if "_qp_preset_done" not in ss:
        ss._qp_preset_done = True
        if qp.get("preset") in PRESETS and not ss.pop("_pending_load_applied", False):
            _load_preset(qp.get("preset"))  # URL 의 예시는 첫 방문 기본값일 뿐 — 사용자가 고른 사례(pending_load)가 우선
    autorun = bool(ss.pop("_force_autorun", False))
    force_demo = False
    if qp.get("autorun") in ("1", "true") and not ss.get("_autorun_done"):
        autorun = True
        force_demo = True  # 링크로 시작되는 실행은 언제나 AI 호출 없는 예시 실행 — 링크를 여는 것만으로 비용이 나가지 않게
        ss._autorun_done = True
        try:
            del st.query_params["autorun"]  # 새로고침·공유 링크로 다시 실행되지 않도록 한 번 쓰고 지운다
        except Exception:  # noqa: BLE001
            pass
    force_demo = bool(ss.pop("_force_demo_run", False)) or force_demo
    settings, options, mode, clicked = _inputs(make_settings, server_status)
    if autorun:
        clicked = True
    if force_demo:
        settings, mode = dataclasses.replace(settings, force_demo=True), "demo"   # 예시로 바로 실행 · 검사 사례의 '예시로 열기' 는 항상 AI 호출 없는 예시 실행
    if clicked:
        _execute(settings, options, tracer, router_factory, mode)
    if not ss.runs:
        _empty_state()
        return
    _results(ss.runs[-1], catalog, tracer, router_factory, make_settings)
