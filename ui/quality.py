"""품질 확인 — 평가 결과(쉬운 말·건수), 개선 이력, 처리 기록, 기술 상세."""
from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from core.config import Settings
from evals.regression import gate
from evals.runner import REPORTS, load_report, run_eval, save_report
from observability.tracer import Tracer
from ui import catalog_view, redteam_view, theme, trace_view
from ui.labels import METRIC, fraction_sentence, pct

SLICE = {"clear": "명백한 위반", "evasion": "우회 표현", "injection": "숨은 지시문", "gray": "경계 사례(수정 필요)", "benign_scary": "무서운 단어가 있지만 문제없음", "semantic_only": "모델만 잡을 수 있는 사례"}
ROUNDS = [  # (파일, 회차, 바뀐 것)
    ("history/baseline_partial_run1.json", "1차", "처음 실행 (프롬프트 2026.10.1)"),
    ("history/baseline_partial_run2.json", "2차", "보이지 않는 문자 전부 제거 + 프롬프트 2026.10.2"),
    ("history/baseline_partial_run3.json", "3차", "코드가 확정한 안내·근거·형식 결과를 모델이 뒤집지 못함, 골든셋 55건"),
    ("baseline_partial.json", "4차", "근거 없는 수정·게재 불가 제안은 담당자 확인으로"),
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
            + f" · {str(rep.get('generated_at', ''))[:10]} · 비용 ${rep['summary'].get('cost_usd', 0)}" + (" (추정 포함)" if rep.get("cost_estimated") else ""))


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


def _eval_section(settings: Settings, router_factory) -> None:
    st.markdown("## 평가 결과")
    st.markdown("정답(기대 판정)이 붙은 광고 사례를 같은 검사 경로로 돌려 **문제 있는 광고를 통과시켰는지**, **문제없는 광고를 괴롭혔는지**, **얼마나 담당자 확인 없이 판단했는지** 를 셉니다. "
                "라벨은 이 프로젝트가 직접 썼고 독립 검증자는 없습니다. 모든 수치는 해당 평가에서 관측된 값이며 모든 광고에 대한 보장이 아닙니다.")
    options: dict[str, str] = {}
    if (REPORTS / "baseline_partial.json").exists():
        options["저장된 실제 모델 결과 (4차, gpt-4o, 골든셋)"] = "baseline_partial.json"
    for label, name in (("최종 확인용 holdout — 예시 실행", "holdout_demo.json"), ("최종 확인용 holdout — 코드 검사만", "holdout_deterministic_only.json"),
                        ("골든셋 — 예시 실행 (실제 AI 호출 없음)", "baseline_demo.json"), ("골든셋 — 코드 검사만 (모델 없음)", "baseline_deterministic_only.json"),
                        ("G-069 대조 사례 — 코드 검사만", "contrast_g069_deterministic_only.json")):
        if (REPORTS / name).exists():
            options[label] = name
    options["이 세션에서 실행한 결과"] = "__session__"
    pick = st.selectbox("결과 선택", list(options.keys()))
    rep = st.session_state.get("eval_report") if options[pick] == "__session__" else load_report(options[pick])
    with st.expander("평가 다시 실행 (현재 설정)"):
        st.caption("예시 실행이면 모델 호출 없이 바로 끝납니다. 실제 모델 키가 있으면 비용이 듭니다.")
        c1, c2, c3 = st.columns(3)
        ds = c1.selectbox("데이터", ["golden", "holdout", "contrast_g069", "adversarial"])
        use_llm = c2.toggle("검토 모델 포함", value=True)
        limit = c3.number_input("사례 수 제한 (0=전부)", min_value=0, value=0)
        if st.button("평가 실행"):
            router = router_factory(settings) if use_llm else None
            prog = st.progress(0.0, text="평가 중…")
            r = run_eval(ds, settings, use_llm=use_llm, router=router, progress=lambda i, n, x: prog.progress(i / n, text=f"{i}/{n} {x.case.id}"), limit=limit or None)
            save_report(r, "latest.json")
            st.session_state.eval_report = r
            prog.empty()
            st.rerun()
    if rep is None:
        st.info("아직 결과가 없습니다. 위에서 저장된 결과를 고르거나 평가를 실행하세요.")
        return
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
    st.markdown("같은 골든셋을 GPT-4o 로 네 번 돌리며 코드 규칙을 고쳤습니다. 프롬프트·규칙을 고친 뒤 같은 데이터로 다시 잰 값이므로 최종 확인용이 아니라 **회귀 감시용**입니다. "
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
        st.markdown("- 1차→4차에서 문제 있는 광고를 통과시킨 사례는 네 번 모두 0건이었습니다.\n"
                    "- 3차→4차: 문제없는 광고 14건 중 올바르게 통과시킨 수는 11건으로 같습니다. 바뀐 것은 잘못된 수정 요청 3건 중 2건이 **담당자 확인**으로 옮겨 간 것이지, 통과가 늘어난 것이 아닙니다.\n"
                    "- 4차에 남은 수정 요청 1건(G-069)은 라벨을 재검토해 v1.2 에서 '수정 필요' 로 바꿨습니다 (근거: 결제 조건 누락, 대조 사례). 그 뒤의 수치는 다음 실제 모델 실행에서 다시 잽니다.")


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
            st.markdown(f"추천 임계값: 문제 있는 광고 통과 0건을 만족하는 최소값 {rep.get('recommended_threshold_for_zero_unsafe')} · 수정 요청 0건까지 만족하는 최소값 {rep.get('recommended_threshold_for_zero_unsafe_and_zero_false_flag')} "
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


def render(settings: Settings, router_factory, tracer: Tracer) -> None:
    st.markdown("## 품질 확인")
    st.markdown("이 도구가 **어디서 틀리는지** 재고, 고친 뒤 **실제로 나아졌는지** 확인한 기록입니다.")
    _eval_section(settings, router_factory)
    _history_section()
    st.markdown("## 처리 기록")
    st.markdown("실행 한 번은 단계별 기록으로 남습니다. 어느 단계가 얼마나 걸렸고, 모델에 무엇을 보내 무엇을 받았는지, 어디서 대체 경로를 탔는지 볼 수 있습니다.")
    trace_view.render(tracer)
    _technical_section()
