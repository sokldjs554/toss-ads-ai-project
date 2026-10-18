"""품질 확인 — 평가 결과(쉬운 말·건수), 개선 이력, 처리 기록, 기술 상세."""
from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from core.config import IN_BROWSER, Settings
from evals.regression import gate
from evals.runner import REPORTS, dataset_fingerprint, load_dataset, load_report, run_eval
from guardrails.policy import get_catalog
from agents.prompts import PROMPT_VERSION
from observability.tracer import Tracer
from ui import catalog_view, redteam_view, theme, trace_view
from ui.labels import METRIC, fraction_sentence, pct

SLICE = {"clear": "명백한 위반", "evasion": "우회 표현", "injection": "숨은 지시문", "gray": "경계 사례(수정 필요)", "benign_scary": "무서운 단어가 있지만 문제없음", "semantic_only": "모델만 잡을 수 있는 사례"}
ROUNDS = [  # (파일, 회차, 바뀐 것)
    ("history/baseline_partial_run1.json", "1차", "처음 실행 (프롬프트 2026.10.1)"),
    ("history/baseline_partial_run2.json", "2차", "보이지 않는 문자 전부 제거 + 프롬프트 2026.10.2"),
    ("history/baseline_partial_run3.json", "3차", "코드가 확정한 안내·근거·형식 결과를 모델이 뒤집지 못함, 골든셋 55건"),
    ("history/baseline_partial_run4_golden_v1.1_policy_2026.10.1.json", "4차", "근거 없는 수정·게재 불가 제안은 담당자 확인으로"),
    ("baseline_partial.json", "5차", "리뷰 수정 반영: 조건 생략 규칙·수정안 의미 재검사·골든셋 v1.2(G-069 라벨 수정)·프롬프트 2026.10.3"),
]


def _benign_counts(rep: dict) -> tuple[int, int, int, int]:
    """문제없는 광고(라벨 APPROVE) 중: 올바른 통과 / 수정·게재 불가 요청(오탐) / 담당자 확인 / 전체."""
    b = [c for c in rep.get("cases", []) if c["label"] == "APPROVE"]
    ok = sum(c["predicted"] == "APPROVE" for c in b)
    ff = sum(c["predicted"] in ("REVISE", "REJECT") for c in b)
    hr = sum(c["predicted"] == "HUMAN_REVIEW" for c in b)
    return ok, ff, hr, len(b)


def _sentences(rep: dict) -> list[str]:
    s = rep["summary"]
    ua, ff, cov, hr = s["unsafe_approve"], s["false_flag"], s["coverage"], s["human_review"]
    ok, ffk, hrk, bn = _benign_counts(rep)
    out = []
    out.append("수정 또는 게재 제한이 필요한 광고: " + (f"{ua['n']}건 중 점검을 통과한 사례는 {'없었습니다' if ua['k'] == 0 else str(ua['k']) + '건이었습니다'}." if ua["n"] else "대상 없음"))
    out.append("문제없는 광고: " + (f"{bn}건 중 {ok}건을 올바르게 통과시켰고, {ffk}건에 수정 또는 게재 제한을 요청했으며, {hrk}건은 담당자 확인으로 넘겼습니다." if bn else "대상 없음"))
    out.append(f"전체 {cov['n']}건 중 {cov['k']}건은 담당자 확인 없이 판단했습니다 (통과·수정 요청·게재 불가 모두 포함). {hr['k']}건은 담당자 확인으로 넘겼습니다.")
    return out


def _report_meta(rep: dict) -> str:
    model = rep.get("model") or rep.get("reviewer", "")
    return (f"실행 방식 `{rep.get('mode')}` · 모델 {model} · 프롬프트 {rep.get('prompt_version')} · 검사 기준 {rep.get('policy_version')} · "
            f"평가 데이터 {rep.get('dataset')} v{rep.get('dataset_version') or '?'} ({rep.get('n')}건"
            + (f", 모델만 잡을 수 있는 사례 {rep['excluded_semantic_only']}건 제외" if rep.get("excluded_semantic_only") else "") + ")"
            + (f" · 자동 판정 확신 기준 {rep['threshold']}" if rep.get("threshold") is not None else "")
            + f" · {str(rep.get('generated_at', ''))[:10]} · 비용 " + _cost_text(rep))


def _cost_text(rep: dict) -> str:
    """응답을 받지 못한 요청의 비용은 알 수 없다 — 그런 요청이 있으면 합계는 '이상' 이고 0원으로 확정하지 않는다."""
    cost = rep["summary"].get("cost_usd", 0)
    if rep.get("failed_requests"):
        return f"${cost} 이상 (응답을 받지 못한 요청 {rep['failed_requests']}건의 비용은 알 수 없음)"
    return f"${cost}" + (" (추정 포함)" if rep.get("cost_estimated") else "")


def _reliability(cal: dict) -> go.Figure:
    xs = [b["confidence"] for b in cal["bins"] if b["n"]]
    ys = [b["accuracy"] for b in cal["bins"] if b["n"]]
    ns = [b["n"] for b in cal["bins"] if b["n"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(dash="dash", color="#999"), name="확신 = 정답률"))
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers+lines", marker=dict(size=[8 + n for n in ns], color="#2B5BD7"), name="관측"))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=30), xaxis_title="검토 모델이 말한 확신", yaxis_title="실제 정답 비율", xaxis_range=[0, 1], yaxis_range=[0, 1.05])
    return fig


def _coverage(curve: list[dict]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[r["tau"] for r in curve], y=[r["coverage"] for r in curve], name="담당자 확인 없이 판단한 비율", line=dict(color="#2B5BD7")))
    fig.add_trace(go.Scatter(x=[r["tau"] for r in curve], y=[r["unsafe_approve"] for r in curve], name="문제 있는 광고를 통과시킨 비율", line=dict(color="#A32116")))
    fig.add_trace(go.Scatter(x=[r["tau"] for r in curve], y=[r["false_flag"] for r in curve], name="문제없는 광고에 수정 요청한 비율", line=dict(color="#8A5A00")))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=30), xaxis_title="자동 판정에 필요한 최소 확신 (τ)", yaxis_range=[0, 1.05], legend=dict(orientation="h", y=-0.3))
    return fig


SESSION_PICK = "이 세션에서 실행한 결과"
DATASETS = {"golden": "골든셋 (개발용)", "holdout": "holdout (최종 확인용)", "contrast_g069": "G-069 대조 사례", "conditions": "숫자·조건 비교 (틀린 문구 + 정상 대조)", "adversarial": "우회 표현 회귀셋"}


def _eval_settings(make_settings, source: str) -> tuple[Settings, str]:
    """평가에 쓸 설정. 광고 작업의 설정(모델·판정 기준·직접 넣은 키)을 쓰거나 서버 기본 설정을 쓴다. (설정, 실제로 적용된 출처 설명)"""
    ss = st.session_state
    if source == "work" and ss.get("settings_overrides"):
        keys = ss.get("user_keys", ("", ""))
        return make_settings(*keys, **ss.settings_overrides), "광고 작업에서 마지막으로 쓴 설정"
    note = "서버 기본 설정" + (" (광고 작업에서 실행한 적이 없어 기본 설정을 씁니다)" if source == "work" else "")
    return make_settings(), note


def _eval_plan(settings: Settings, ds: str, use_llm: bool, limit: int) -> dict:
    cases = load_dataset(ds)
    if not use_llm or settings.mode.value == "demo":
        cases = [c for c in cases if c.difficulty != "semantic_only"]
    n = min(len(cases), limit) if limit else len(cases)
    live = use_llm and settings.mode.value != "demo"
    per_case = None
    ref = load_report("baseline_partial.json")
    if live and ref and ref.get("n"):
        per_case = ref["summary"].get("cost_usd", 0) / ref["n"]
    return {"n": n, "live": live, "est_cost": (per_case * n) if per_case is not None else None, "est_calls": int(n * 1.3) if live else 0}


def _eval_section(make_settings, router_factory, server_status: dict) -> None:
    ss = st.session_state
    st.markdown("## 평가 결과")
    st.markdown("정답(기대 판정)이 붙은 광고 사례를 같은 검사 경로로 돌려 **문제 있는 광고를 통과시켰는지**, **문제없는 광고를 괴롭혔는지**, **얼마나 담당자 확인 없이 판단했는지** 를 셉니다. "
                "라벨은 이 프로젝트가 직접 썼고 독립 검증자는 없습니다. 모든 수치는 해당 평가에서 관측된 값이며 모든 광고에 대한 보장이 아닙니다.")
    options: dict[str, str] = {}
    if (REPORTS / "baseline_partial.json").exists():
        options["저장된 실제 모델 결과 (5차, gpt-4o, 골든셋 v1.2)"] = "baseline_partial.json"
    for label, name in (("최종 확인용 holdout — 실제 모델 (5차, gpt-4o)", "holdout_partial.json"), ("G-069 대조 사례 — 실제 모델 (5차, gpt-4o)", "contrast_g069_partial.json"),
                        ("최종 확인용 holdout — 예시 실행", "holdout_demo.json"), ("최종 확인용 holdout — 코드 검사만", "holdout_deterministic_only.json"),
                        ("골든셋 — 예시 실행 (실제 AI 호출 없음)", "baseline_demo.json"), ("골든셋 — 코드 검사만 (모델 없음)", "baseline_deterministic_only.json"),
                        ("G-069 대조 사례 — 코드 검사만", "contrast_g069_deterministic_only.json")):
        if (REPORTS / name).exists():
            options[label] = name
    options[SESSION_PICK] = "__session__"
    if ss.pop("eval_just_ran", False):
        ss["eval_pick"] = SESSION_PICK  # 방금 돌린 결과를 바로 보여 준다 (저장된 첫 결과로 되돌아가지 않는다)
    pick = st.selectbox("결과 선택", list(options.keys()), key="eval_pick")
    rep = ss.get("eval_report") if options[pick] == "__session__" else load_report(options[pick])
    with st.expander("평가 다시 실행 — 설정을 확인하고 실행"):
        st.caption("저장된 결과는 위에서 고르면 바로 볼 수 있고, 보는 것만으로는 모델을 부르지 않습니다. 여기서 실행하면 아래 설정 그대로 새로 평가해 이 세션에 결과를 남깁니다. 저장된 결과는 바꾸지 않습니다.")
        c0, c1, c2, c3 = st.columns([2, 2, 2, 1])
        source = c0.radio("평가에 쓸 설정", ["work", "default"], format_func=lambda k: {"work": "광고 작업의 현재 설정", "default": "서버 기본 설정"}[k], key="eval_settings_src",
                          help="광고 작업의 설정 = 거기서 마지막으로 쓴 모델·자동 판정 확신 기준·직접 넣은 키. 서버 기본 설정 = 서버 환경변수 기준. 두 설정은 따로 쓰이며 서로를 바꾸지 않습니다.")
        ds = c1.selectbox("평가 데이터", list(DATASETS.keys()), format_func=lambda k: DATASETS[k], key="eval_ds")
        use_llm = c2.toggle("검토 모델 포함", value=True, key="eval_use_llm", help="끄면 코드 검사만 합니다 (모델 호출 없음).")
        limit = c3.number_input("사례 수 제한 (0=전부)", min_value=0, value=0, key="eval_limit")
        settings, src_note = _eval_settings(make_settings, source)
        if IN_BROWSER:
            settings = make_settings()
            src_note = "브라우저 실행판 — 항상 예시 실행"
        plan = _eval_plan(settings, ds, use_llm, int(limit))
        fp = dataset_fingerprint(ds)
        mode_text = "코드 검사만 (모델 없음)" if not use_llm else {"demo": "예시 실행 (실제 AI 호출 없음)", "partial": "실제 AI 호출 · 한 제공자", "live": "실제 AI 호출 · 두 제공자"}[settings.mode.value]
        st.markdown("**이 평가에 적용되는 설정**")
        st.dataframe([{"항목": k, "값": v} for k, v in (
            ("설정 출처", src_note), ("실행 방식", mode_text), ("OpenAI 모델", settings.openai_model if use_llm else "—"), ("Anthropic 모델", settings.anthropic_model if use_llm else "—"),
            ("자동 판정 확신 기준(τ)", f"{settings.auto_decision_confidence:.2f}"), ("검사 기준 버전", get_catalog().version), ("프롬프트 버전", PROMPT_VERSION),
            ("평가 데이터", f"{DATASETS[ds]} · v{fp['dataset_version'] or '?'} · 해시 {fp['dataset_sha256'][:8]}"), ("평가할 사례", f"{plan['n']}건"))],
            width="stretch", hide_index=True)
        using_server_key = settings.mode.value != "demo" and ss.get("user_keys", ("", "")) == ("", "") if source == "work" else settings.mode.value != "demo"
        cap = float(server_status.get("run_budget_usd") or 0.5) if using_server_key else None
        approved = True
        if plan["live"]:
            est = f"약 ${plan['est_cost']:.2f} (5차 실제 모델 실행의 사례당 평균 비용 기준 추정)" if plan["est_cost"] is not None else "알 수 없음"
            st.warning(f"실제 모델을 부릅니다 — 모델 호출 약 {plan['est_calls']}회, 예상 비용 {est}."
                       + (f" 서버 키로 도는 평가는 서버가 정한 상한 ${cap:.2f} 에서 멈추고, 멈추면 결과에 장애로 표시합니다." if cap is not None else " 직접 넣은 키로 도는 평가라 비용은 그 계정에 청구됩니다."), icon="💳")
            approved = st.checkbox("위 호출 수와 비용을 확인했고 실행합니다", key="eval_confirm")
        if st.button("평가 실행", key="eval_run", disabled=not approved):
            router = router_factory(settings) if use_llm else None
            prog = st.progress(0.0, text="평가 중…")
            r = run_eval(ds, settings, use_llm=use_llm, router=router, progress=lambda i, n, x: prog.progress(i / n, text=f"{i}/{n} {x.case.id}"), limit=int(limit) or None,
                         breaker_wait_s=0, abort_after=0, budget_cap_usd=cap, run_info={"source": src_note, "requested_dataset": ds})  # 화면에서는 기다리거나 중단하지 않는다. 장애가 섞였으면 아래에서 표시
            ss.eval_report = r
            ss.eval_just_ran = True
            prog.empty()
            st.rerun()
    if rep is not None and options[pick] == "__session__":
        rs = rep.get("run_settings") or {}
        st.success(f"방금 이 세션에서 실행한 결과입니다 — {rs.get('source', '?')} · 모델 {rs.get('openai_model') if rs.get('use_llm') else '없음(코드 검사만)'} · "
                   f"확신 기준 {rs.get('threshold')} · 검사 기준 {rep.get('policy_version')} · 프롬프트 {rep.get('prompt_version')} · 데이터 {rep.get('dataset')} v{rep.get('dataset_version') or '?'}"
                   + (f" · 사례 {rs['limit']}건 제한" if rs.get("limit") else ""), icon="🆕")
    if rep is None:
        st.info("아직 결과가 없습니다. 위에서 저장된 결과를 고르거나 평가를 실행하세요.")
        return
    pf = rep.get("provider_failures") or {}
    if pf.get("n"):
        st.warning(f"이 결과에는 검토 모델 호출이 실패해 '담당자 확인 필요' 로 떨어진 사례가 {pf['n']}건 섞여 있습니다 (오류: {pf.get('by_error')}). "
                   "장애 기록이지 모델 판단의 측정이 아니므로 기준선이나 추천 임계값에는 쓰지 않습니다.", icon="⚠️")
    st.caption(_report_meta(rep))
    for line in _sentences(rep):
        st.markdown(f"- {line}")
    s = rep["summary"]
    cols = st.columns(4)
    ua, ff, cov = s["unsafe_approve"], s["false_flag"], s["coverage"]
    ok, ffk, hrk, bn = _benign_counts(rep)
    cols[0].markdown(theme.stat(METRIC["unsafe_approve"][0], pct(ua["k"], ua["n"]), "낮을수록 좋음"), unsafe_allow_html=True)
    cols[1].markdown(theme.stat(METRIC["false_flag"][0], pct(ff["k"], ff["n"]), "낮을수록 좋음"), unsafe_allow_html=True)
    cols[2].markdown(theme.stat(METRIC["benign_correct_pass"][0], pct(ok, bn), "담당자 확인으로 넘긴 건은 제외"), unsafe_allow_html=True)
    cols[3].markdown(theme.stat(METRIC["coverage"][0], pct(cov["k"], cov["n"]), "통과·수정·게재 불가 모두 포함"), unsafe_allow_html=True)
    rows = []
    for k, v in s.get("by_difficulty", {}).items():
        rows.append({"사례 종류": SLICE.get(k, k), "건수": v["n"], METRIC["unsafe_approve"][0]: pct(v["unsafe_approve"]["k"], v["unsafe_approve"]["n"]),
                     METRIC["false_flag"][0]: pct(v["false_flag"]["k"], v["false_flag"]["n"]), "담당자 확인": pct(v["human_review"]["k"], v["human_review"]["n"])})
    if rows:
        st.markdown("**사례 종류별**")
        st.dataframe(rows, width="stretch", hide_index=True)
    with st.expander("사례별 결과"):
        from core.schemas import Decision
        from ui.labels import DECISION_LABEL

        st.dataframe([{"사례": c["id"], "종류": SLICE.get(c["difficulty"], c["difficulty"]), "기대 판정": DECISION_LABEL[Decision(c["label"])], "판정": DECISION_LABEL[Decision(c["predicted"])],
                       "확신": c["confidence"], "잡은 규칙": ", ".join(c["predicted_rules"]), "기대 규칙": ", ".join(c["label_rules"])} for c in rep["cases"]], width="stretch", hide_index=True)
    base_name = f"baseline_{rep['mode']}.json"
    if options[pick] == "__session__" and (REPORTS / base_name).exists() and rep.get("dataset") == "golden":
        g = gate(rep, load_report(base_name))
        st.markdown("**기준선과 비교** (" + ("나빠진 지표 없음" if g.passed else "나빠진 지표 있음") + ")")
        for f in g.failures:
            st.error(f)
        for w in g.warnings:
            st.caption(w)


def _history_section() -> None:
    st.markdown("## 개선 이력 (실제 모델, 골든셋)")
    st.markdown("GPT-4o 로 다섯 번 돌리며 코드 규칙을 고쳤습니다 (1~2차는 골든셋 53건, 3~4차는 사례 2건을 더한 v1.1 55건, 5차는 라벨 1건을 고친 v1.2 55건이라 회차마다 분모가 다릅니다). 프롬프트·규칙을 고친 뒤 같은 데이터로 다시 잰 값이므로 최종 확인용이 아니라 **회귀 감시용**입니다. "
                "'담당자 확인으로 넘긴 건' 은 '올바르게 통과시킨 건' 과 다르게 셉니다.")
    rows = []
    for name, label, change in ROUNDS:
        rep = load_report(name)
        if rep is None:
            continue
        s = rep["summary"]
        ok, ffk, hrk, bn = _benign_counts(rep)
        rows.append({"회차": label, "바뀐 것": change, "문제 있는 광고 통과": f"{s['unsafe_approve']['k']}/{s['unsafe_approve']['n']}",
                     "문제없는 광고 → 올바른 통과": f"{ok}/{bn}", "문제없는 광고 → 수정·게재 불가 요청": f"{ffk}/{bn}", "문제없는 광고 → 담당자 확인": f"{hrk}/{bn}",
                     "담당자 확인(전체)": f"{s['human_review']['k']}/{s['human_review']['n']}", "확신 보정 오차(ECE)": s["calibration"]["ece"], "비용": f"${s['cost_usd']}"})
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
        st.markdown("- 1차→5차에서 문제 있는 광고를 통과시킨 사례는 다섯 번 모두 0건이었습니다.\n"
                    "- 3차→4차: 문제없는 광고 14건 중 올바르게 통과시킨 수는 11건으로 같습니다. 바뀐 것은 잘못된 수정 요청 3건 중 2건이 **담당자 확인**으로 옮겨 간 것이지, 통과가 늘어난 것이 아닙니다.\n"
                    "- 4차에 남은 수정 요청 1건(G-069)은 라벨을 재검토해 v1.2 에서 '수정 필요' 로 바꿨습니다 (근거: 결제 조건 누락, 대조 사례). 그래서 5차부터 분모가 바뀝니다 — 문제 있는 광고 42건, 문제없는 광고 13건.\n"
                    "- 5차(골든셋 v1.2, 검사 기준 2026.10.2, 프롬프트 2026.10.3): 문제없는 광고 13건 중 12건 올바른 통과, 수정 요청 0건, 담당자 확인 1건(원문에 없는 인용). 처음 보는 holdout 12건에서도 통과 0/10·수정 요청 0/2. 4차와 5차는 골든셋 버전이 달라 같은 행끼리 직접 비교하면 안 됩니다.\n"
                    "- 5차의 첫 시도(run 39041334234)는 실행 도중 OpenAI 크레딧이 떨어져 절반이 모델 판단 없이 끝났습니다. 그 결과는 기준선에서 빼고 보관만 했고, 같은 일이 다시 생기면 저장되지 않도록 고쳤습니다 (FM-24).")


def _technical_section(rep_name: str = "baseline_partial.json") -> None:
    with st.expander("기술 상세 — 확신 보정, 임계값 곡선, 규칙별 정밀도·재현율, 우회 표현 검사, 실패 모드, 검사 규칙"):
        rep = load_report(rep_name)
        if rep:
            cal = rep["summary"]["calibration"]
            st.markdown(f"**확신 보정** — 검토 모델이 말한 확신(0~1)이 실제 정답 비율과 얼마나 맞는지. ECE {cal.get('ece')} (0 에 가까울수록 좋음), Brier {cal.get('brier')}. "
                        f"확신 0.8 이상인데 틀린 사례 {cal['high_confidence_error']['k']}/{cal['high_confidence_error']['n']}. 이 값은 모델이 말한 확신의 보정 정도이지 검증된 정답 확률이 아닙니다.")
            c1, c2 = st.columns(2)
            with c1:
                st.plotly_chart(_reliability(cal), width="stretch")
            with c2:
                st.plotly_chart(_coverage(rep["summary"]["coverage_curve"]), width="stretch")
            method = rep.get("curve_method") or rep["summary"].get("curve_method", "legacy_confidence_filter")
            st.caption(("임계값 곡선은 저장된 원본 심사 결과로 실제 판정 함수를 다시 불러 계산했습니다 (서비스와 같은 규칙)." if method == "replay_decide"
                        else "이 결과는 원본 심사 결과를 저장하기 전의 것이라, 곡선은 '확신 < τ 인 판정을 일괄 담당자 확인으로 간주' 하는 옛 방식으로 계산했습니다. 같은 τ 에서 실제 판정과 1건 정도 다를 수 있습니다(예: 코드가 게재 불가로 확정한 사례는 확신과 무관).")
                       + " 평가 경로는 1차 심사만 포함하고 다른 모델 계열의 교차 확인은 포함하지 않습니다.")
            st.markdown(f"추천 임계값: 문제 있는 광고 통과 0건을 만족하는 값 중 커버리지가 가장 높은 값(같으면 더 높은 τ) {rep.get('recommended_threshold_for_zero_unsafe')} · 수정 요청 0건까지 만족하는 값 중 커버리지가 가장 높은 값(같으면 더 높은 τ) {rep.get('recommended_threshold_for_zero_unsafe_and_zero_false_flag')} "
                        f"(제공자 {rep.get('provider', '?')} · 모델 {rep.get('model', '?')} · 프롬프트 {rep.get('prompt_version')} · 검사 기준 {rep.get('policy_version')} · 데이터 v{rep.get('dataset_version') or '?'} 에서만 유효)")
            with st.expander("규칙별 정밀도·재현율"):
                st.dataframe([{"규칙": k, **v} for k, v in rep["summary"]["rules"].items()], width="stretch", hide_index=True)
        st.markdown("### 우회 표현 검사")
        st.caption("위반 광고를 띄어쓰기 분해·보이지 않는 문자·한자·돌려 말하기·부정문·면책 문구 뒤 숨기기·지시문·암시 8가지로 변형해 검사를 뚫는지 봅니다. 뚫린 변형은 회귀 데이터로 모읍니다.")
        redteam_view.render_summary()
        st.markdown("### 실패 모드 목록")
        catalog_view.render_failure_modes()
        st.markdown("### 검사 규칙")
        catalog_view.render_policy()


def render(make_settings, router_factory, tracer: Tracer, server_status: dict) -> None:
    st.markdown("## 품질 확인")
    st.markdown("이 도구가 **어디서 틀리는지** 재고, 고친 뒤 **실제로 나아졌는지** 확인한 기록입니다.")
    _eval_section(make_settings, router_factory, server_status)
    _history_section()
    st.markdown("## 처리 기록")
    st.markdown("실행 한 번은 단계별 기록으로 남습니다. 어느 단계가 얼마나 걸렸고, 모델에 무엇을 보내 무엇을 받았는지, 어디서 대체 경로를 탔는지 볼 수 있습니다.")
    trace_view.render(tracer)
    _technical_section()
