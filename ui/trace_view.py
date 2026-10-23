"""관측 — 트레이스 목록 · 워터폴 · 스팬 트리 · 스팬 상세 · JSON 내보내기/재생."""
from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from core.config import IN_BROWSER
from observability.tracer import Span, Trace, Tracer

from .components import KIND_COLOR, STATUS_COLOR
from .trace_redact import redacted_copy

TRACES_DIR = Path(__file__).resolve().parents[1] / "traces"

# 요청은 나갔는데 응답(사용량)을 받지 못한 경우를 라우터가 스팬 이벤트로 남기는 이름들. 그 요청의 비용은 기록에 없다 (성공 응답의 사용량만 스팬에 적힌다).
_FAILED_REQUEST_EVENTS = {"provider_error", "refusal", "truncated", "truncation_retry", "schema_mismatch", "repair_failed"}


def unanswered_requests(span: Span) -> int:
    """이 모델 호출 스팬에서 응답을 받지 못해 비용이 기록되지 않은 요청 수(알 수 있는 만큼).
    장애 시뮬레이션(simulated_failure)은 요청을 보내기 전에 실패시키므로 센 것에서 뺀다. 이벤트로 설명되지 않는 오류 스팬(상한에 닿은 호출, 이벤트가 없는 기록)은 한 건으로 본다."""
    names = [str(e.get("name", "")) for e in span.events if isinstance(e, dict)]
    failed = sum(n in _FAILED_REQUEST_EVENTS for n in names) - names.count("simulated_failure")
    if span.attributes.get("repaired") and "schema_mismatch" not in names:
        failed += 1   # 형식을 어겨 수리된 호출 — 수리 전 요청의 비용은 기록에 없다 (이벤트가 없는 기록 대비)
    if failed <= 0 and span.status == "error" and not any(n in ("simulated_failure", "skip_provider") for n in names) and "cost_usd" not in span.attributes:
        failed = 1
    return max(failed, 0)


def cost_text(trace: Trace) -> str:
    """기록의 총비용 표기. 응답을 못 받은 요청이 있으면 '≥' (그 요청의 비용은 알 수 없어 적힌 금액은 최소값), 사용량을 글자 수로 추정한 호출만 있으면 '≈', 깨끗하면 금액만.
    예시 실행(demo)은 모델을 부르지 않으므로 늘 금액만."""
    cost = f"${trace.total_cost_usd:.4f}"
    llm = trace.llm_spans()
    if trace.metadata.get("mode") != "demo" and sum(unanswered_requests(x) for x in llm):
        return "≥ " + cost
    if any(x.attributes.get("cost_estimated") for x in llm):
        return "≈ " + cost
    return cost


def cost_help(trace: Trace) -> str | None:
    llm = trace.llm_spans()
    n = 0 if trace.metadata.get("mode") == "demo" else sum(unanswered_requests(x) for x in llm)
    if n:
        return f"응답을 받지 못한 요청 {n}건의 비용은 기록에 없어 이 금액은 최소값입니다. 실제 비용은 더 클 수 있습니다."
    if any(x.attributes.get("cost_estimated") for x in llm):
        return "제공자가 사용량을 돌려주지 않은 호출은 글자 수로 비용을 추정했습니다."
    return None


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
            mark = "≥ " if (unanswered_requests(s) and trace.metadata.get("mode") != "demo") else ("≈ " if s.attributes.get("cost_estimated") else "")
            extra = f" · {s.attributes.get('provider','?')}/{s.attributes.get('model','?')} · {mark}${float(s.attributes.get('cost_usd',0)):.4f} · {s.attributes.get('input_tokens',0)}→{s.attributes.get('output_tokens',0)} tok"
        texts.append(f"{s.name} [{s.kind}] {s.status} {s.duration_ms}ms{extra}")
    fig.add_trace(go.Bar(y=labels, x=widths, base=bases, orientation="h", marker_color=colors, hovertext=texts, hoverinfo="text"))
    fig.update_layout(height=max(260, 22 * len(rows) + 60), margin=dict(l=10, r=10, t=10, b=30), xaxis_title="ms", yaxis=dict(autorange="reversed"),
                      showlegend=False, plot_bgcolor="#fff")
    return fig


def render(tracer: Tracer) -> None:
    st.caption("실행 하나가 기록 하나입니다. 막대 색: 파랑=처리 단계, 보라=모델 호출, 주황=검사·가로챔, 노랑=대체 경로, 빨강=오류.")
    sources: dict[str, Trace] = {}
    for t in reversed(tracer.traces):
        sources[f"이 세션 · {t.trace_id} · {t.metadata.get('product','')} · {cost_text(t)}"] = t
    if TRACES_DIR.exists():
        for p in sorted(TRACES_DIR.glob("sample_*.json")):
            try:
                t = Trace.from_json(p.read_text(encoding="utf-8"))
                sources[f"저장된 기록 · {p.name}"] = t
            except Exception:  # noqa: BLE001
                continue
    up = st.file_uploader("내보낸 처리 기록(JSON) 불러오기", type=["json"])
    if up is not None:
        try:
            t = Trace.from_json(up.read().decode("utf-8"))
            sources = {f"업로드 · {t.trace_id}": t, **sources}
        except Exception as e:  # noqa: BLE001
            st.error(f"읽을 수 없는 트레이스: {e}")
    if not sources:
        st.info("아직 처리 기록이 없습니다. 광고 작업에서 실행하거나 JSON 을 불러오세요.")
        return
    key = st.selectbox("처리 기록 선택", list(sources.keys()))
    stored = sources[key]
    # 재검사를 통과하지 못한 자동 수정안은 어느 화면에도 보여 주지 않는다 (FM-19) — 모델 입출력 원문은 사본에서 그 부분만 가려 그린다. 저장된 기록·내려받는 파일은 그대로다
    trace = redacted_copy(stored)
    llm = trace.llm_spans()
    i_tok, o_tok = trace.total_tokens
    c = st.columns(6)
    c[0].metric("총 시간", f"{trace.duration_ms/1000:.2f}s")
    c[1].metric("기록 단계", len(trace.spans()))
    c[2].metric("모델 호출", len(llm))
    c[3].metric("토큰 in/out", f"{i_tok}/{o_tok}")
    c[4].metric("비용", cost_text(trace), help=cost_help(trace))
    sc = trace.status_counts()
    c[5].metric("가로챔/대체 경로/오류", f"{sc.get('intercepted',0)}/{sc.get('fallback',0)}/{sc.get('error',0)}")
    st.markdown("**메타** " + " · ".join(f"`{k}={v}`" for k, v in trace.metadata.items() if k not in ("final_decisions",)))
    st.plotly_chart(_waterfall(trace), width="stretch")

    st.markdown("#### 단계 상세")
    rows = _flatten(trace.root)
    options = {f"{'　'*d}{s.name} · {s.kind} · {s.status} · {s.duration_ms}ms": s for d, s in rows}
    pick = st.selectbox("단계 선택", list(options.keys()))
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
    if IN_BROWSER:
        # stlite(브라우저 실행판)에서는 파일 다운로드가 동작하지 않아 JSON 을 그대로 보여 준다.
        with st.expander("처리 기록 JSON (복사용 — 브라우저 실행판은 파일 다운로드 대신 표시)"):
            st.code(json.dumps(stored.operational_record(), ensure_ascii=False, indent=2), language="json")
    else:
        st.download_button("운영 기록 JSON 내보내기 (원문 제외)", data=json.dumps(stored.operational_record(), ensure_ascii=False, indent=2),
                           file_name=f"{stored.trace_id}-metrics.json", mime="application/json")
    st.caption("공유용 운영 기록에는 프롬프트·응답·상품명·오류 원문을 넣지 않습니다. 원문 재생용 파일과 형식이 다릅니다.")
    with st.expander("모델 호출 표"):
        st.dataframe([{"스팬": x.name, "task": x.attributes.get("task"), "provider": x.attributes.get("provider"), "model": x.attributes.get("model"),
                       "체인": " → ".join(x.attributes.get("fallback_chain") or []), "status": x.status, "ms": x.duration_ms,
                       "in": x.attributes.get("input_tokens"), "out": x.attributes.get("output_tokens"), "USD": x.attributes.get("cost_usd"), "추정": "예" if x.attributes.get("cost_estimated") else ""} for x in llm],
                      width="stretch", hide_index=True)
