"""스튜디오 — 요청 입력 → 파이프라인 실행 → 단계별 결과(Planner 생각 → 가상 유저 → 가드레일·심사 → 판정)."""
from __future__ import annotations

import difflib
import html

import streamlit as st

from agents.pipeline import RunOptions, run_pipeline
from core.config import Settings
from core.schemas import Decision, PipelineRun, PreflightReport, ProductCategory
from guardrails.failure_modes import BY_CODE
from guardrails.policy import get_catalog
from llm.router import Router
from observability.tracer import Tracer

from .components import decision_badge, findings_table, render_copy, severity_chip
from .presets import PRESETS, build_brief

CATEGORY_LABELS = {c.value: n for c, n in [(ProductCategory.LOAN, "대출"), (ProductCategory.CARD, "카드"), (ProductCategory.INSURANCE, "보험"),
                                            (ProductCategory.INVESTMENT, "투자·증권"), (ProductCategory.SAVINGS, "예·적금"), (ProductCategory.CRYPTO, "가상자산"),
                                            (ProductCategory.ECOMMERCE, "커머스"), (ProductCategory.APP, "앱·서비스"), (ProductCategory.EDUCATION, "교육"),
                                            (ProductCategory.HEALTH, "건강·의료"), (ProductCategory.REALESTATE, "부동산"), (ProductCategory.OTHER, "기타")]}


def _load_preset(key: str) -> None:
    p = PRESETS[key]
    st.session_state.form_input = p["advertiser_input"]
    st.session_state.form_name = p["product_name"]
    st.session_state.form_category = p["category"]
    st.session_state.form_facts = "\n".join(p["facts"])
    st.session_state.form_desc = p.get("description", "")
    st.session_state.form_landing = p.get("landing_text", "")
    st.session_state.form_budget = int(p.get("budget") or 0)


def render(settings: Settings, tracer: Tracer, router_factory) -> None:
    catalog = get_catalog()
    qp = st.query_params
    if "form_input" not in st.session_state:
        _load_preset(qp.get("preset") if qp.get("preset") in PRESETS else "loan_ok")
    autorun = qp.get("autorun") in ("1", "true") and not st.session_state.get("_autorun_done")

    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            preset = st.selectbox("예시 불러오기", options=list(PRESETS.keys()), format_func=lambda k: PRESETS[k]["label"], key="preset_pick")
        with c2:
            st.write("")
            st.write("")
            if st.button("불러오기", width="stretch"):
                _load_preset(preset)
                st.rerun()
        st.text_area("광고주 요청 (자연어)", key="form_input", height=80,
                     help="목표·예산·대상을 말로 적으세요. 정책 위반을 지시하거나 지시문을 섞으면 Safety Intercept 가 가로챕니다.")
        a, b, c = st.columns([2, 1, 1])
        with a:
            st.text_input("상품명", key="form_name")
        with b:
            st.selectbox("업종 (광고주 선언)", options=list(CATEGORY_LABELS.keys()), format_func=lambda k: f"{CATEGORY_LABELS[k]} ({k})", key="form_category")
        with c:
            st.number_input("월 예산 (원)", key="form_budget", min_value=0, step=100_000)
        d, e = st.columns(2)
        with d:
            st.text_area("사실 목록 (한 줄에 하나, `F1: ...`) — 광고의 모든 주장은 여기 있어야 합니다", key="form_facts", height=120)
        with e:
            st.text_area("상품 설명 (신뢰할 수 없는 자유 텍스트 — 인젝션 유입 경로)", key="form_desc", height=55)
            st.text_area("랜딩 페이지 텍스트 (소재↔랜딩 일치 검사)", key="form_landing", height=55)
        run_clicked = st.button("🛫 Preflight 실행", type="primary", width="stretch")

    if autorun:
        st.session_state._autorun_done = True
        run_clicked = True
    if run_clicked:
        try:
            brief = build_brief({
                "advertiser_input": st.session_state.form_input, "product_name": st.session_state.form_name or "상품",
                "category": st.session_state.form_category, "facts": st.session_state.form_facts, "budget": st.session_state.form_budget or None,
                "description": st.session_state.form_desc,
            })
        except Exception as e:  # noqa: BLE001
            st.error(f"입력을 해석할 수 없습니다: {e}")
            return
        router: Router = router_factory()
        opts = RunOptions(n_variants=st.session_state.get("opt_n_variants", 3), landing_text=st.session_state.form_landing,
                          rejected_history=list(st.session_state.get("rejected_history", [])),
                          enable_audience=st.session_state.get("opt_audience", True), enable_second_opinion=st.session_state.get("opt_second", True))
        with st.status("파이프라인 실행 중…", expanded=True) as status:
            st.write("0️⃣ Safety Intercept → 1️⃣ Planner → 2️⃣ Copywriter → 3️⃣ Output Guard → 4️⃣ 가상 유저 패널 → 5️⃣ Reviewer(+교차 검증) → 6️⃣ 판정")
            run = run_pipeline(brief, settings, router=router, options=opts, tracer=tracer)
            status.update(label=f"완료 — {len(run.reports)}개 소재 · ${run.total_cost_usd:.4f} · {run.total_latency_ms/1000:.1f}s", state="complete", expanded=False)
        st.session_state.runs.append(run)
        for r in run.reports:
            if r.final_decision == Decision.REJECT:
                st.session_state.rejected_history.append(r.ad_copy.full_text())
    if not st.session_state.runs:
        st.info("예시를 불러오거나 요청을 적고 **Preflight 실행**을 누르세요. 키가 없어도 DEMO 모드로 전체 흐름을 볼 수 있습니다.")
        return
    _render_run(st.session_state.runs[-1], catalog)


def _render_run(run: PipelineRun, catalog) -> None:
    st.divider()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("모드", run.mode.upper())
    m2.metric("소재", len(run.reports))
    m3.metric("비용 (USD)", f"{run.total_cost_usd:.4f}")
    m4.metric("지연", f"{run.total_latency_ms/1000:.1f}s")
    decisions = [r.final_decision for r in run.reports]
    m5.metric("승인/수정/반려/검토", f"{decisions.count(Decision.APPROVE)}/{decisions.count(Decision.REVISE)}/{decisions.count(Decision.REJECT)}/{decisions.count(Decision.HUMAN_REVIEW)}")

    # --- Safety Intercept ---
    n = run.intercept
    if n.triggered:
        kind = {"policy_intent": "정책 위반 의도", "injection": "프롬프트 인젝션", "prohibited_category": "금지 업종·표현"}.get(n.kind.value if n.kind else "", "")
        box = st.error if n.hard_block else st.warning
        with box(f"**Safety Intercept — {kind}**  \n{n.message}", icon="🛡️"):
            pass
        with st.expander("가로챈 내용 · 준수 제안 (원문 → 제안)", expanded=not n.hard_block):
            if n.removed_phrases:
                st.markdown("제거한 구절: " + " ".join(f"`{html.escape(p)}`" for p in n.removed_phrases))
            if n.suggested_input:
                diff = difflib.ndiff([run.brief.advertiser_input], [n.suggested_input])
                st.code("\n".join(diff), language="diff")
                st.caption("제안된 요청으로 파이프라인을 계속 진행했습니다. 제안문은 다시 가드레일을 통과한 것만 씁니다 (FM-19).")
            findings_table(n.findings, catalog)
    if run.halted:
        st.error(f"⛔ 실행 중단: {run.halt_reason}")
        if run.failures:
            st.caption("실패 모드: " + ", ".join(f"{f.code} {BY_CODE[f.code].name if f.code in BY_CODE else ''}" for f in run.failures))
        return

    # --- Planner ---
    if run.plan is not None:
        p = run.plan
        with st.expander(f"🧠 Planner 생각 과정 — 목표 `{p.objective.value}` · KPI {p.kpi} · 총 예산 {p.total_budget_krw:,}원" + (" · ⚙️ 코드가 수리함" if p.repaired else ""), expanded=True):
            st.markdown(p.planner_reasoning)
            cols = st.columns(2)
            with cols[0]:
                st.markdown("**가정**")
                for a in p.assumptions:
                    st.markdown(f"- {a}")
            with cols[1]:
                st.markdown("**리스크 (심사에서 걸릴 지점)**")
                for r in p.risks:
                    st.markdown(f"- {r}")
            st.dataframe([{"광고 세트": a.name, "타겟": a.audience, "지면": a.placement, "예산(원)": f"{a.budget_krw:,}", "입찰": a.bid_strategy, "근거": a.rationale} for a in p.ad_sets],
                         width="stretch", hide_index=True)
    else:
        st.warning("플래너 단계가 실패해 계획 없이 소재만 생성했습니다 (fail-safe).")

    # --- Variants ---
    st.subheader("소재별 사전점검 결과")
    for i, rep in enumerate(run.reports, start=1):
        _render_report(i, rep, catalog)

    if run.failures:
        with st.expander(f"⚠️ 이번 실행에서 밟은 실패 모드 {len(run.failures)}건 (전부 처리됨)"):
            st.dataframe([{"코드": f.code, "이름": BY_CODE[f.code].name if f.code in BY_CODE else "", "단계": f.stage, "내용": f.message, "대체 경로": f.fallback} for f in run.failures],
                         width="stretch", hide_index=True)


def _render_report(i: int, rep: PreflightReport, catalog) -> None:
    v = rep.verdict
    with st.container(border=True):
        head, badge = st.columns([5, 2])
        with head:
            st.markdown(f"**변형 {i}** · `{rep.variant_id}`")
        with badge:
            st.markdown(decision_badge(rep.final_decision, big=True), unsafe_allow_html=True)
        left, right = st.columns([1, 1])
        with left:
            render_copy(rep.ad_copy, v.findings)
            st.caption(f"판정 근거: {rep.decision_reason}")
        with right:
            tabs = st.tabs(["✅ 체크리스트", "👥 가상 유저 피드백", "🛡️ 가드레일·반려 사유", "✏️ 수정안"])
            with tabs[0]:
                for c in rep.checklist:
                    st.markdown(f"{'🟢' if c.passed else '🔴'} **{c.name}** — {c.detail}  \n<span style='color:#8B95A1;font-size:11px'>{c.layer.value}</span>", unsafe_allow_html=True)
            with tabs[1]:
                a = rep.audience
                if a is None:
                    st.caption("패널 단계가 꺼져 있거나 실패했습니다.")
                else:
                    k1, k2, k3, k4 = st.columns(4)
                    k1.metric("끌림", a.mean_appeal)
                    k2.metric("신뢰", a.mean_trust)
                    k3.metric("클릭 의향", f"{a.click_rate:.0%}")
                    k4.metric("관점 분산", a.diversity, help="페르소나 점수의 표준편차. 낮으면 모두 같은 말 → 아첨 의심(FM-15)")
                    if a.sycophancy_flag:
                        st.warning("패널이 거의 같은 말을 했습니다 — 정보량이 낮아 참고용으로만 표시합니다 (FM-15).")
                    for f in a.feedbacks:
                        st.markdown(f"**{f.persona_name}** · 끌림 {f.appeal} · 신뢰 {f.trust} · 이해 {f.clarity} · {'클릭 O' if f.would_click else '클릭 X'}  \n"
                                    f"> {html.escape(f.quote)}  \n<span style='color:#B42318;font-size:12px'>걱정: {html.escape(f.concern)}</span>", unsafe_allow_html=True)
                    st.caption(a.summary)
            with tabs[2]:
                st.markdown(f"심사 모델 판정 **{v.decision.value}** · 신뢰도 **{v.confidence:.2f}** · `{v.provider}/{v.model}`"
                            + (f" · 2차 의견 **{v.second_opinion.value}** {'⚠️ 불일치' if v.disagreement else '✓ 일치'}" if v.second_opinion else ""))
                if v.rationale:
                    st.caption(v.rationale)
                by_layer: dict[str, list] = {}
                for f in v.findings:
                    by_layer.setdefault(f.layer.value, []).append(f)
                if not v.findings:
                    st.success("위반 없음")
                for layer, fs in by_layer.items():
                    st.markdown(f"**{layer}** " + " ".join(severity_chip(f.severity) for f in fs), unsafe_allow_html=True)
                    for f in fs:
                        rule = catalog.get(f.rule_id)
                        st.markdown(f"- `{f.rule_id}` {rule.title if rule else ''} — {html.escape(f.explanation)}"
                                    + (f"  \n  인용: “{html.escape(f.quote)}”" if f.quote else "")
                                    + ("" if f.verified_quote else "  \n  ⚠️ 원문에 없는 인용 (환각) → 반려 근거로 쓰지 않음")
                                    + (f"  \n  <span style='color:#8B95A1;font-size:11px'>출처: {html.escape(rule.section)}</span>" if rule else ""), unsafe_allow_html=True)
            with tabs[3]:
                if v.compliant_rewrite is not None:
                    st.caption("전체 가드레일을 다시 통과한 수정안만 보여 줍니다.")
                    render_copy(v.compliant_rewrite, title="수정안")
                    diff = difflib.unified_diff(rep.ad_copy.full_text().splitlines(), v.compliant_rewrite.full_text().splitlines(), lineterm="", n=0)
                    st.code("\n".join(list(diff)[2:]) or "(변경 없음)", language="diff")
                elif rep.final_decision in (Decision.REVISE, Decision.REJECT):
                    hints = sorted({catalog.get(f.rule_id).fix_hint for f in v.findings if catalog.get(f.rule_id) and catalog.get(f.rule_id).fix_hint})
                    st.markdown("자동 수정안이 없거나 재검사를 통과하지 못해 폐기했습니다. 수정 힌트:")
                    for h in hints:
                        st.markdown(f"- {h}")
                else:
                    st.caption("수정이 필요하지 않습니다.")
