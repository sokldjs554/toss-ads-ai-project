"""관측 — 트레이스 목록 · 워터폴 · 스팬 트리 · 스팬 상세 · JSON 내보내기/재생."""
from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from observability.tracer import Span, Trace, Tracer

from .components import KIND_COLOR, STATUS_COLOR

TRACES_DIR = Path(__file__).resolve().parents[1] / "traces"


def _flatten(span: Span, depth: int = 0, out=None):
    out = [] if out is None else out
    out.append((depth, span))
    for c in span.children:
        _flatten(c, depth + 1, out)
    return out


def _waterfall(trace: Trace) -> go.Figure:
    rows = _flatten(trace.root)
    t0 = trace.root.start_ms
    fig = go.Figure()
    labels, bases, widths, colors, texts = [], [], [], [], []
    for depth, s in rows:
        labels.append(("　" * depth) + s.name)
        bases.append(round(s.start_ms - t0, 1))
        widths.append(max(s.duration_ms, 0.5))
        colors.append(STATUS_COLOR.get(s.status, "#999") if s.status != "ok" else KIND_COLOR.get(s.kind, "#999"))
        extra = ""
        if s.kind == "llm":
            extra = f" · {s.attributes.get('provider','?')}/{s.attributes.get('model','?')} · ${float(s.attributes.get('cost_usd',0)):.4f} · {s.attributes.get('input_tokens',0)}→{s.attributes.get('output_tokens',0)} tok"
        texts.append(f"{s.name} [{s.kind}] {s.status} {s.duration_ms}ms{extra}")
    fig.add_trace(go.Bar(y=labels, x=widths, base=bases, orientation="h", marker_color=colors, hovertext=texts, hoverinfo="text"))
    fig.update_layout(height=max(260, 22 * len(rows) + 60), margin=dict(l=10, r=10, t=10, b=30), xaxis_title="ms", yaxis=dict(autorange="reversed"),
                      showlegend=False, plot_bgcolor="#fff")
    return fig


def render(tracer: Tracer) -> None:
    st.caption("각 실행은 트레이스 1개 = 스팬 트리. 색: 파랑=stage, 보라=llm, 주황=guardrail/intercepted, 노랑=fallback, 빨강=error.")
    sources: dict[str, Trace] = {}
    for t in reversed(tracer.traces):
        sources[f"세션 · {t.trace_id} · {t.metadata.get('product','')} · ${t.total_cost_usd:.4f}"] = t
    if TRACES_DIR.exists():
        for p in sorted(TRACES_DIR.glob("sample_*.json")):
            try:
                t = Trace.from_json(p.read_text(encoding="utf-8"))
                sources[f"재생 · {p.name}"] = t
            except Exception:  # noqa: BLE001
                continue
    up = st.file_uploader("트레이스 JSON 재생 (내보낸 파일)", type=["json"], label_visibility="collapsed")
    if up is not None:
        try:
            t = Trace.from_json(up.read().decode("utf-8"))
            sources = {f"업로드 · {t.trace_id}": t, **sources}
        except Exception as e:  # noqa: BLE001
            st.error(f"읽을 수 없는 트레이스: {e}")
    if not sources:
        st.info("아직 트레이스가 없습니다. 스튜디오에서 실행하거나 JSON 을 업로드하세요.")
        return
    key = st.selectbox("트레이스", list(sources.keys()))
    trace = sources[key]
    llm = trace.llm_spans()
    i_tok, o_tok = trace.total_tokens
    c = st.columns(6)
    c[0].metric("총 시간", f"{trace.duration_ms/1000:.2f}s")
    c[1].metric("스팬", len(trace.spans()))
    c[2].metric("LLM 호출", len(llm))
    c[3].metric("토큰 in/out", f"{i_tok}/{o_tok}")
    c[4].metric("비용", f"${trace.total_cost_usd:.4f}")
    sc = trace.status_counts()
    c[5].metric("intercept/fallback/error", f"{sc.get('intercepted',0)}/{sc.get('fallback',0)}/{sc.get('error',0)}")
    st.markdown("**메타** " + " · ".join(f"`{k}={v}`" for k, v in trace.metadata.items() if k not in ("final_decisions",)))
    st.plotly_chart(_waterfall(trace), width="stretch")

    st.markdown("#### 스팬 상세")
    rows = _flatten(trace.root)
    options = {f"{'　'*d}{s.name} · {s.kind} · {s.status} · {s.duration_ms}ms": s for d, s in rows}
    pick = st.selectbox("스팬 선택", list(options.keys()))
    s = options[pick]
    a = dict(s.attributes)
    prompt_s, prompt_u, resp = a.pop("prompt_system", None), a.pop("prompt_user", None), a.pop("response_text", None)
    col1, col2 = st.columns([1, 1])
    with col1:
        st.json(a, expanded=True)
    with col2:
        if s.events:
            st.markdown("**이벤트**")
            st.dataframe(s.events, width="stretch", hide_index=True)
        if s.kind == "llm":
            chain = s.attributes.get("fallback_chain") or s.attributes.get("chain")
            st.markdown(f"라우팅: 체인 `{s.attributes.get('chain')}` → 실제 `{chain}` · 제약 디코딩 `{s.attributes.get('constrained')}` · 수리 `{s.attributes.get('repaired')}`")
    if prompt_s or prompt_u or resp:
        with st.expander("프롬프트 / 응답 원문", expanded=False):
            if prompt_s:
                st.markdown("**system**")
                st.code(prompt_s, language="text")
            if prompt_u:
                st.markdown("**user**")
                st.code(prompt_u, language="text")
            if resp:
                st.markdown("**response**")
                st.code(resp, language="json")
    st.download_button("트레이스 JSON 내보내기", data=trace.to_json(), file_name=f"{trace.trace_id}.json", mime="application/json")
    with st.expander("LLM 호출 표"):
        st.dataframe([{"스팬": x.name, "task": x.attributes.get("task"), "provider": x.attributes.get("provider"), "model": x.attributes.get("model"),
                       "체인": " → ".join(x.attributes.get("fallback_chain") or []), "status": x.status, "ms": x.duration_ms,
                       "in": x.attributes.get("input_tokens"), "out": x.attributes.get("output_tokens"), "USD": x.attributes.get("cost_usd")} for x in llm],
                      width="stretch", hide_index=True)
