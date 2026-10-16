"""평가 — 커버리지·위험 승인율·오탐율·규칙별 P/R·보정(reliability diagram)·커버리지 곡선·회귀 게이트."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from core.config import Settings
from evals.regression import gate
from evals.runner import load_report, run_eval, save_report
from observability.tracer import Tracer


def _rate(m: dict) -> str:
    return "—" if m.get("rate") is None else f"{m['rate']:.1%} ({m['k']}/{m['n']})"


def _reliability(cal: dict) -> go.Figure:
    xs = [b["confidence"] for b in cal["bins"] if b["n"]]
    ys = [b["accuracy"] for b in cal["bins"] if b["n"]]
    ns = [b["n"] for b in cal["bins"] if b["n"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(dash="dash", color="#999"), name="완벽 보정"))
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers+lines", marker=dict(size=[8 + n for n in ns], color="#3182F6"), name="관측"))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=30), xaxis_title="모델이 말한 신뢰도", yaxis_title="실제 정답률", xaxis_range=[0, 1], yaxis_range=[0, 1.05])
    return fig


def _coverage(curve: list[dict]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[r["tau"] for r in curve], y=[r["coverage"] for r in curve], name="자동 판정 커버리지", line=dict(color="#3182F6")))
    fig.add_trace(go.Scatter(x=[r["tau"] for r in curve], y=[r["unsafe_approve"] for r in curve], name="위험 승인율", line=dict(color="#B42318")))
    fig.add_trace(go.Scatter(x=[r["tau"] for r in curve], y=[r["false_flag"] for r in curve], name="오탐율", line=dict(color="#9A6700")))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=30), xaxis_title="자동 판정 임계값 τ", yaxis_range=[0, 1.05])
    return fig


def render(settings: Settings, router_factory) -> None:
    st.caption("라벨은 '코드가 내야 할 판정'. 같은 저자가 규칙과 라벨을 썼다는 한계가 있어 `benign_scary`(무섭지만 괜찮음)·`evasion`·`injection` 슬라이스를 따로 봅니다.")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        source = st.radio("결과", ["세션에서 실행한 결과", "저장된 기준선 (demo)", "저장된 기준선 (deterministic_only)"], horizontal=True)
    with c2:
        use_llm = st.toggle("심사 모델 포함", value=True, help="끄면 결정론 가드레일만 (네트워크 없음)")
    with c3:
        limit = st.number_input("사례 수 제한", min_value=0, value=0, help="0 = 전부")
    if st.button("🧪 골든셋 평가 실행 (현재 모드)", type="primary"):
        router = router_factory() if use_llm else None
        prog = st.progress(0.0, text="평가 중…")

        def cb(i, n, r):
            prog.progress(i / n, text=f"{i}/{n} {r.case.id} → {r.predicted}")

        rep = run_eval("golden", settings, use_llm=use_llm, router=router, progress=cb, limit=limit or None)
        save_report(rep, "latest.json")
        st.session_state.eval_report = rep
        prog.empty()
    rep = None
    if source == "세션에서 실행한 결과":
        rep = st.session_state.get("eval_report")
    elif source == "저장된 기준선 (demo)":
        rep = load_report("baseline_demo.json")
    else:
        rep = load_report("baseline_deterministic_only.json")
    if rep is None:
        st.info("아직 결과가 없습니다. 평가를 실행하거나 저장된 기준선을 선택하세요.")
        return
    s = rep["summary"]
    st.markdown(f"**{rep['dataset']}** · n={rep['n']} · 모드 `{rep['mode']}` · 심사 `{rep['reviewer']}` · 정책 {rep['policy_version']} · 프롬프트 {rep['prompt_version']} · τ={rep['threshold']}")
    cal = s["calibration"]

    def _pct(m):
        return "—" if m.get("rate") is None else f"{m['rate']:.1%}"

    def _frac(m):
        return f"{m['k']}/{m['n']}"

    m = st.columns(6)
    m[0].metric("자동 판정 커버리지", _pct(s["coverage"]), help="사람 검토로 넘기지 않고 자동으로 판정한 비율")
    m[1].metric("위험 승인율", _pct(s["unsafe_approve"]), help="반려/수정이어야 할 소재를 승인한 비율 — 가장 중요한 안전 지표", delta_color="inverse")
    m[2].metric("오탐율", _pct(s["false_flag"]), help="괜찮은 소재를 수정/반려한 비율")
    m[3].metric("자동 판정 정확도", _pct(s["accuracy_decided"]))
    m[4].metric("사람 검토율", _pct(s["human_review"]))
    m[5].metric("고신뢰 오류율", _pct(cal["high_confidence_error"]), help="신뢰도 ≥ 0.8 로 말했는데 틀린 비율 — '자신 있게 틀림' (FM-14)")
    st.caption(f"분모 · 커버리지 {_frac(s['coverage'])} · 위험 승인 {_frac(s['unsafe_approve'])} · 오탐 {_frac(s['false_flag'])} · 정확도 {_frac(s['accuracy_decided'])} · "
               f"사람 검토 {_frac(s['human_review'])} · 고신뢰 오류 {_frac(cal['high_confidence_error'])} · Wilson 95% 는 사례별 표 참고")
    st.caption(f"ECE={cal['ece']} · Brier={cal['brier']} · 위험 승인 0% 를 만족하는 최소 τ = {rep['recommended_threshold_for_zero_unsafe']}"
               + (f" · 자신 있게 틀린 사례: {', '.join(cal['confidently_wrong'])}" if cal["confidently_wrong"] else ""))
    g1, g2 = st.columns(2)
    with g1:
        st.markdown("**Reliability diagram** (신뢰도 vs 실제 정답률)")
        st.plotly_chart(_reliability(cal), width="stretch")
    with g2:
        st.markdown("**커버리지 곡선** (τ 를 올리면 사람 검토가 늘고 위험 승인이 준다)")
        st.plotly_chart(_coverage(s["coverage_curve"]), width="stretch")
    st.markdown("**난이도별**")
    st.dataframe([{"슬라이스": k, "n": v["n"], "위험 승인율": _rate(v["unsafe_approve"]), "오탐율": _rate(v["false_flag"]), "사람 검토율": _rate(v["human_review"])} for k, v in s["by_difficulty"].items()],
                 width="stretch", hide_index=True)
    with st.expander("규칙별 precision / recall"):
        st.dataframe([{"규칙": k, **v} for k, v in s["rules"].items()], width="stretch", hide_index=True)
    with st.expander("사례별 결과"):
        st.dataframe([{k: c[k] for k in ("id", "difficulty", "category", "label", "predicted", "confidence", "predicted_rules", "label_rules", "reason")} for c in rep["cases"]],
                     width="stretch", hide_index=True)
    base_name = f"baseline_{rep['mode']}.json"
    base = load_report(base_name)
    g = gate(rep, base)
    st.markdown(f"**회귀 게이트** (기준선 `{base_name}`): " + ("🟢 PASS" if g.passed else "🔴 FAIL") + (f" · Δ {g.deltas}" if g.deltas else ""))
    for f in g.failures:
        st.error(f)
    for w in g.warnings:
        st.caption("⚠️ " + w)
