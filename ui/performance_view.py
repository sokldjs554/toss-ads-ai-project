"""성과 리포트 화면 — 이상 탐지 → 원인 후보 → 다음 액션.

화면은 그리기만 한다. 탐지·진단·액션은 core/insight_engine.py, 문장 검증은 agents/insights.py 가 한다.
정직하게 보여 줄 것: 예시는 합성 데이터이고, 키가 없으면 문장은 템플릿이 만든다. 이 화면은 원인을 확정하지 않는다.

세션 상태 키는 모두 perf_ 로 시작한다.
  perf_source(데이터 출처) · perf_example(예시 이름) · perf_upload(파일) · perf_csv_text(붙여넣은 CSV) · perf_use_model(AI 문장 사용)
  perf_run(분석 버튼) · perf_metric(그래프 지표) · perf_result(마지막 분석 결과)
"""
from __future__ import annotations

import hashlib
import html
import json
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from agents.insights import InsightReport, build_report
from core.config import IN_BROWSER
from core.insight_engine import ASSERT_CONFIDENCE, CANDIDATE_CONFIDENCE, CAUSE_HELP, CATALOG, LIKELY_CONFIDENCE, METRIC_LABEL, METRICS, Analysis
from core.performance import decode_csv_bytes, parse_csv_detailed, rows_to_csv, template_csv
from observability.tracer import Tracer

LIMIT_LINE = "이 화면은 광고 성과의 원인을 확정하지 않습니다 — 근거와 가능성을 보여 줍니다."
SYNTHETIC_LINE = "합성 예시 데이터 — 실제 광고 성과가 아닙니다"
USER_LINE = "내가 올린 데이터"
# 예시 이름(예시 A…)과 시나리오 연결. 화면에는 사건 종류를 보이지 않는다 — 정답을 미리 알려 주지 않기 위해 순서를 섞어 둔다.
EXAMPLE_IDS = ("dev-tracking_break-1", "dev-seasonal_dip-2", "dev-creative_fatigue-1", "dev-no_event-1", "dev-budget_cap-1", "dev-landing_issue-2",
               "dev-bid_competition-2", "dev-audience_saturation-1", "dev-mix_shift-1", "dev-ambiguous-1")
EXAMPLES = [(f"예시 {chr(65 + i)}", sid) for i, sid in enumerate(EXAMPLE_IDS)]
SEVERITY_LABEL = {"high": "크게 벗어남", "medium": "뚜렷하게 벗어남", "low": "조금 벗어남"}
DROP_REASON = {
    "no_evidence": "근거가 없는 문장", "unknown_evidence": "없는 근거를 인용한 문장", "number_unsupported": "근거에 없는 숫자가 들어간 문장", "direction_conflict": "오르내림 방향이 근거와 반대인 문장",
    "cause_overclaim": "근거보다 강하게 원인을 단정한 문장", "all_clear_conflict": "이상이 있는데 문제없다고 쓴 문장", "unsafe_text": "링크 등 허용되지 않는 글이 든 문장", "injection": "지시문처럼 보이는 글이 든 문장",
    "empty": "빈 문장", "too_long": "너무 긴 문장", "unknown_action": "목록에 없는 액션", "precondition": "선행 조건이 맞지 않는 액션", "over_limit": "개수 제한을 넘은 문장",
}
FALLBACK_TEXT = {
    "model_not_used": "AI 문장 생성을 쓰지 않아 템플릿이 만들었습니다.", "model_call_failed": "AI 호출이 실패해 템플릿으로 바꿨습니다.",
    "all_sentences_invalid": "AI가 쓴 문장이 모두 근거 검사를 통과하지 못해 템플릿으로 바꿨습니다.",
}
_MD_SPECIAL = {"[": "&#91;", "]": "&#93;", "(": "&#40;", ")": "&#41;", "!": "&#33;", "*": "&#42;", "_": "&#95;", "`": "&#96;", "~": "&#126;", "#": "&#35;", "|": "&#124;", "\\": "&#92;"}


def safe(text: Any) -> str:
    """신뢰할 수 없는 글(CSV 칸·모델 문장)을 HTML 블록 안에 그대로(글자로) 놓기 위한 이스케이프. 마크다운 기호도 글자로 바꾼다."""
    return "".join(_MD_SPECIAL.get(ch) or html.escape(ch, quote=True) for ch in str(text if text is not None else ""))


_CSS = """
<style>
.pf-perf-card { border:1px solid var(--pf-line,#E5E8EB); border-radius:12px; padding:12px 14px; background:#fff; margin:8px 0; overflow-wrap:anywhere; }
.pf-perf-card .t { font-weight:700; margin-bottom:4px; }
.pf-perf-bar { height:8px; border-radius:6px; background:#EEF0F3; overflow:hidden; margin:6px 0; }
.pf-perf-bar > span { display:block; height:100%; background:#2B5BD7; }
.pf-perf-line { margin:4px 0; overflow-wrap:anywhere; }
.pf-perf-ok { color:#0B6B37; font-weight:700; }
.pf-perf-warn { color:#8A5A00; font-weight:700; }
.pf-perf-bad { color:#A32116; font-weight:700; }
.pf-perf-avoid { border-left:3px solid #A32116; background:#FCF2F1; padding:6px 10px; border-radius:0 8px 8px 0; margin:6px 0; overflow-wrap:anywhere; }
</style>
"""


def _html(block: str) -> None:
    st.markdown(block, unsafe_allow_html=True)


def _pct_text(x: float) -> str:
    v = abs(x) * 100
    return f"{v:,.0f}%" if v >= 1000 else f"{v:.1f}%"


def _signed_word(metric: str, pct: float) -> str:
    word = ("많음", "적음") if metric in ("impressions", "clicks", "conversions", "cost") else ("높음", "낮음")
    return f"{'▲' if pct > 0 else '▼'} {_pct_text(pct)} {word[0] if pct > 0 else word[1]}"


# ───────────────────────────── 데이터 선택 ─────────────────────────────
def _example_rows(scenario_id: str):
    from core.performance import generate
    from evals.insights_eval import load_scenarios

    sc = next(s for s in load_scenarios("dev") if s.id == scenario_id)
    return generate(sc.spec)


def _read_csv(text: str):
    res = parse_csv_detailed(text)
    return res


def _show_csv_problems(res, *, fatal: bool) -> None:
    more = res.error_count - min(len(res.errors), 12)
    head = ("파일을 읽을 수 없었어요." if fatal else f"읽지 못한 줄이 {res.error_count}개 있어요. 나머지 {len(res.rows):,}줄로 분석합니다.")
    body = "<br>".join(f"{safe(str(e))}" for e in res.errors[:12]) + (f"<br>… 외 {more}건" if more > 0 else "")
    box = (st.error if fatal else st.warning)
    box(head)
    _html(f'<div class="pf-perf-card pf-muted">{body}</div>')


# ───────────────────────────── 그래프 ─────────────────────────────
def _chart(a: Analysis, metric: str) -> go.Figure:
    import datetime as dt

    pts = a.series.get(metric) or []
    fig = go.Figure()
    if not pts:
        return fig
    x = [dt.datetime.fromisoformat(p["date"]) for p in pts]
    fig.add_trace(go.Scatter(x=x, y=[p["hi"] for p in pts], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=[p["lo"] for p in pts], mode="lines", line=dict(width=0), fill="tonexty", fillcolor="rgba(43,91,215,0.12)", name="평소 범위", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=[p["expected"] for p in pts], mode="lines", line=dict(dash="dash", color="#6B7684", width=1.5), name="기대 (같은 요일 기준)"))
    fig.add_trace(go.Scatter(x=x, y=[p["actual"] for p in pts], mode="lines+markers", line=dict(color="#2B5BD7", width=2), marker=dict(size=5), name="실제"))
    for inc in a.incidents:
        fig.add_vrect(x0=dt.datetime.combine(inc.start, dt.time()) - dt.timedelta(hours=12), x1=dt.datetime.combine(inc.end, dt.time()) + dt.timedelta(hours=12),
                      fillcolor="rgba(163,33,22,0.12)", line_width=0, layer="below")
    fig.add_vline(x=dt.datetime.combine(a.window[0], dt.time()) - dt.timedelta(hours=12), line_dash="dot", line_color="#9AA3AE", line_width=1)
    fmt = ".1%" if metric in ("ctr", "cvr") else ",.0f"
    fig.update_layout(height=300, margin=dict(l=4, r=4, t=8, b=4), legend=dict(orientation="h", y=-0.28, font=dict(size=11)), hovermode="x unified",
                      xaxis=dict(tickformat="%m/%d", fixedrange=True), yaxis=dict(tickformat=fmt, fixedrange=True, automargin=True), font=dict(size=12),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig


# ───────────────────────────── 결과 그리기 ─────────────────────────────
def _banner(rep: InsightReport) -> None:
    origin = SYNTHETIC_LINE if rep.data_origin == "synthetic" else USER_LINE
    if rep.narrative_source == "model":
        src = "문장: AI가 썼고, 코드가 숫자와 근거를 하나씩 대조했습니다"
    elif rep.mock_used:
        src = "예시 실행: 문장은 템플릿이 만듭니다 (AI 호출 없음)"
    else:
        src = "문장: 템플릿이 만들었습니다" + (f" — {FALLBACK_TEXT.get(rep.fallback_reason, '')}" if rep.fallback_reason and not rep.mock_used else "")
    _html(f'<div class="pf-perf-line"><span class="pf-mode{"" if rep.data_origin == "synthetic" else " stored"}">{safe(origin)}</span> '
          f'<span class="pf-mode live">{safe(src)}</span></div>')


def _stat_cards(a: Analysis) -> None:
    keys = [k for k in ("impressions", "ctr", "cvr", "cpa") if k in a.window_stats]
    if not keys:
        return
    cols = st.columns(len(keys))
    for col, k in zip(cols, keys):
        w = a.window_stats[k]
        if k in ("ctr", "cvr"):
            val, exp = f"{w['actual'] * 100:.2f}%", f"{w['expected'] * 100:.2f}%"
        elif k == "cpa":
            val, exp = f"{w['actual']:,.0f}원", f"{w['expected']:,.0f}원"
        else:
            val, exp = f"{w['actual']:,.0f}", f"{w['expected']:,.0f}"
        with col:
            _html(f'<div class="pf-stat"><div class="k">{safe(METRIC_LABEL[k])}</div><div class="v">{safe(val)}</div>'
                  f'<div class="s">기대 {safe(exp)} · {safe(_signed_word(k, w["pct"]))}</div></div>')


def _what_happened(rep: InsightReport) -> None:
    a = rep.analysis
    st.markdown("### 무슨 일이 있었나")
    if a.status in ("insufficient", "insufficient_history"):
        st.warning("표본이 부족해 이번에는 판단을 보류했습니다.")
        if a.status_evidence:
            st.caption(a.evidence[a.status_evidence].text)
        return
    if a.status == "partial" and a.status_evidence:
        st.info(a.evidence[a.status_evidence].text)
    if not a.incidents:
        _html('<div class="pf-perf-card"><div class="t pf-perf-ok">이상 없음</div>'
              '<div>살펴본 기간의 지표가 기준 기간의 같은 요일 수준 안에 있었습니다.</div></div>')
        for n in a.notes:
            st.caption(a.evidence[n].text + " 그래서 이상으로 보지 않았습니다.")
        return
    for dg in a.diagnoses:
        inc = dg.incident
        changes = []
        for m, s in inc.findings:
            label = METRIC_LABEL[m] if not s.segment else f"{s.segment}의 {METRIC_LABEL[m]}"
            changes.append(f"{safe(label)} {safe(_signed_word(m, s.pct))}")
        changes = list(dict.fromkeys(changes))
        tone = "pf-perf-ok" if inc.polarity == "better" else "pf-perf-bad"
        title = f"{inc.start.month}월 {inc.start.day}일 ~ {inc.end.month}월 {inc.end.day}일 ({inc.days}일간{', 아직 이어지는 중' if inc.ongoing else ''})"
        _html(f'<div class="pf-perf-card"><div class="t">{safe(title)} <span class="{tone}">{"좋은 방향의 변화" if inc.polarity == "better" else SEVERITY_LABEL[inc.severity]}</span></div>'
              + "".join(f'<div class="pf-perf-line">· {c}</div>' for c in changes[:8]) + "</div>")


def _hyp_card(h, ev: dict, rank: int) -> None:
    pct = int(round(h.confidence * 100))
    stage = "단정 가능" if h.confidence >= ASSERT_CONFIDENCE else "가능성 높음" if h.confidence >= LIKELY_CONFIDENCE else "후보" if h.confidence >= CANDIDATE_CONFIDENCE else "가능성 낮음"
    body = [f'<div class="t">{rank}. {safe(h.label)} <span class="pf-chip">{safe(stage)}</span></div>',
            f'<div class="pf-muted">{safe(CAUSE_HELP.get(h.cause, ""))}</div>',
            f'<div class="pf-perf-bar"><span style="width:{pct}%"></span></div><div class="pf-muted">확신 {pct}%</div>']
    if h.rival_note:
        body.append(f'<div class="pf-perf-line pf-perf-warn">{safe(h.rival_note)}</div>')
    elif h.cap_reason:
        body.append(f'<div class="pf-perf-line pf-perf-warn">{safe(h.cap_reason)}</div>')
    for eid in h.evidence[:4]:
        body.append(f'<div class="pf-perf-line">✓ {safe(ev[eid].text)}</div>')
    for eid in list(dict.fromkeys(h.contradicted_by))[:3]:
        body.append(f'<div class="pf-perf-line pf-perf-warn">✗ 반대 증거: {safe(ev[eid].text)}</div>')
    for m in h.data_missing[:2]:
        body.append(f'<div class="pf-perf-line pf-muted">? 확인하지 못한 것: {safe(m)}</div>')
    _html(f'<div class="pf-perf-card">{"".join(body)}</div>')


def _causes(rep: InsightReport) -> None:
    a = rep.analysis
    if not a.diagnoses:
        return
    st.markdown("### 원인 후보")
    st.caption("확신은 확률이 아닙니다. 정해 둔 확인 항목 중 맞는 것의 비율을 점수로 바꾼 값입니다. 광고 관리자에서 직접 확인하기 전에는 원인을 확정하지 마세요.")
    ev = a.evidence
    for dg in a.diagnoses:
        inc = dg.incident
        if len(a.diagnoses) > 1:
            st.markdown(f"**{inc.start.month}월 {inc.start.day}일 ~ {inc.end.month}월 {inc.end.day}일 구간**")
        if dg.cause_state == "not_applicable":
            st.info("좋은 방향의 변화라 원인 후보를 찾지 않았습니다.")
            continue
        if dg.cause_state == "undetermined":
            _html('<div class="pf-perf-card"><div class="t pf-perf-warn">원인을 정하기 어렵습니다</div>'
                  '<div>확인한 근거만으로는 어느 원인도 기준(확신 45%)에 닿지 않았습니다. 억지로 1순위를 고르지 않았습니다.</div></div>')
        shown = [h for h in dg.hypotheses if h.confidence >= CANDIDATE_CONFIDENCE][:3] or dg.hypotheses[:1]
        for i, h in enumerate(shown, start=1):
            _hyp_card(h, ev, i)


def _actions(rep: InsightReport) -> None:
    a = rep.analysis
    st.markdown("### 다음에 할 일")
    for act in rep.actions:
        body = [f'<div class="t">{safe(act.label)}</div>', f'<div class="pf-perf-line">{safe(act.why)}</div>']
        if act.watch:
            body.append(f'<div class="pf-perf-line pf-muted">볼 지표: {safe(", ".join(act.watch))}</div>')
        if act.risk and act.risk != "없음":
            body.append(f'<div class="pf-perf-line pf-muted">위험: {safe(act.risk)}</div>')
        _html(f'<div class="pf-perf-card">{"".join(body)}</div>')
    if a.avoid:
        st.markdown("**하지 말아야 할 것**")
        for av in a.avoid:
            names = ", ".join(CATALOG[x].label for x in av.action_ids if x in CATALOG)
            _html(f'<div class="pf-perf-avoid"><b>{safe(names)}</b><br>{safe(av.reason)}</div>')


def _narrative(rep: InsightReport) -> None:
    st.markdown("### 보고서 글")
    if rep.narrative_source == "model":
        st.caption("AI가 쓴 문장 중 근거 검사를 통과한 것만 실었습니다.")
    elif rep.mock_used:
        st.caption("예시 실행: 문장은 템플릿이 만듭니다. AI가 쓴 글이 아닙니다.")
    else:
        st.caption("문장은 템플릿이 만들었습니다. " + FALLBACK_TEXT.get(rep.fallback_reason, ""))
    lines = [f'<div class="pf-perf-card"><div class="t">{safe(rep.headline)}</div><div class="pf-perf-line">{safe(rep.summary)}</div>']
    lines += [f'<div class="pf-perf-line">· {safe(f.text)}</div>' for f in rep.findings]
    lines.append("</div>")
    _html("".join(lines))
    if rep.dropped_sentences:
        with st.expander(f"근거 검사에서 뺀 문장 {len(rep.dropped_sentences)}개"):
            st.caption("AI가 쓴 문장 중 아래 이유로 뺐습니다. 이 문장들은 보고서에 들어가지 않았습니다.")
            for d in rep.dropped_sentences:
                reason = DROP_REASON.get(d.reason, "금지 조건에 걸리는 액션" if d.reason.startswith("guard") else "검사를 통과하지 못한 문장")
                _html(f'<div class="pf-perf-line pf-muted">· {safe(reason)}: {safe(d.text[:140])}</div>')


def _details(rep: InsightReport) -> None:
    a = rep.analysis
    with st.expander("상세 기록 (규칙 코드·근거 목록·내려받기)"):
        st.caption(f"데이터: {'합성' if rep.data_origin == 'synthetic' else '내 데이터'} · 분석 상태 {a.status} · 행 {a.n_rows:,}개 · 살펴본 기간 {a.window[0]} ~ {a.window[1]} · 문장 출처 {rep.narrative_source}"
                   + (f" · 제공자 {rep.provider}" if rep.provider else "") + f" · 프롬프트 {rep.prompt_version}")
        st.markdown("**근거 목록** (보고서의 모든 숫자는 아래 근거 번호로 되돌릴 수 있습니다)")
        rows = [{"번호": e.id, "종류": e.kind, "내용": e.text, "원인": e.cause} for e in a.evidence.values()]
        st.dataframe(rows, width="stretch", hide_index=True)
        for dg in a.diagnoses:
            for h in dg.hypotheses:
                checks = [{"항목": c.cid, "기준": c.label, "결과": "확인 못함" if c.passed is None else ("맞음" if c.passed else "아님"), "가중치": c.weight, "근거": c.evidence_id}
                          for c in h.checks]
                st.markdown(f"**{h.label}** 확신 {h.confidence:.2f} — 계산값 {h.raw_confidence:.2f} = 맞는 비율 {h.support_ratio:.2f} × 확인한 비율 {h.coverage:.2f} × (1 − 0.6 × 반대 비율 {h.contradiction_ratio:.2f})"
                            + (f", 상한 적용: {h.cap_reason}" if h.cap_reason else ""))
                st.dataframe(checks, width="stretch", hide_index=True)
        if a.avoid:
            st.markdown("**막힌 액션의 규칙 코드**")
            st.dataframe([{"규칙": av.rule, "액션": ", ".join(av.action_ids), "이유": av.reason} for av in a.avoid], width="stretch", hide_index=True)
        st.download_button("보고서 JSON 내려받기", data=json.dumps(rep.to_dict(), ensure_ascii=False, indent=1).encode("utf-8"), file_name="preflight_성과_보고서.json",
                           mime="application/json", key="perf_dl_json")


def _show_result(res: dict) -> None:
    rep: InsightReport = res["report"]
    a = rep.analysis
    _banner(rep)
    _html(f'<div class="pf-perf-card"><div class="t">{safe(rep.headline)}</div><div class="pf-perf-line">{safe(rep.summary)}</div></div>')
    _stat_cards(a)
    st.markdown("### 지표 추이")
    options = [m for m in METRICS if a.series.get(m)]
    if options:
        top = a.diagnoses[0] if a.diagnoses else None
        default = next((m for m in ("cvr", "cpa", "ctr", "clicks") if top and any(f[0] == m for f in top.incident.findings) and m in options), options[0] if "cvr" not in options else "cvr")
        label = st.selectbox("살펴볼 지표", [METRIC_LABEL[m] for m in options], index=options.index(default), key="perf_metric")
        metric = next(m for m in options if METRIC_LABEL[m] == label)
        st.plotly_chart(_chart(a, metric), config={"displayModeBar": False})
        st.caption("파란 선이 실제, 점선이 같은 요일 기준 기대값, 연한 파란 띠가 평소 범위입니다. 붉은 칸이 이상 구간이고, 점선 세로선 오른쪽이 살펴본 기간입니다.")
    else:
        st.info("그릴 수 있는 지표가 없습니다.")
    _what_happened(rep)
    _causes(rep)
    _actions(rep)
    _narrative(rep)
    _details(rep)


# ───────────────────────────── 화면 ─────────────────────────────
def render(make_settings, tracer: Tracer, router_factory, server_status: dict) -> None:
    """성과 리포트 화면. 예시(합성) 또는 CSV 를 분석해 이상·원인 후보·다음 액션·보고서 글을 보여 준다."""
    ss = st.session_state
    _html(_CSS)
    st.markdown("## 성과 리포트")
    st.markdown("광고 성과에서 **이상을 찾고, 왜 그랬을지 후보를 보여 주고, 다음에 무엇을 할지** 알려 줍니다. 계산은 코드가 하고, 문장 속 숫자는 근거와 대조합니다.")
    st.caption(LIMIT_LINE)
    model_ok = bool(server_status.get("openai") or server_status.get("anthropic")) and not IN_BROWSER

    source = st.radio("데이터", ["예시 시나리오", "CSV 올리기"], horizontal=True, key="perf_source")
    rows, origin, csv_res, sig = None, "synthetic", None, ""
    if source == "예시 시나리오":
        names = [n for n, _ in EXAMPLES]
        pick = st.selectbox("예시", names, key="perf_example")
        st.caption("예시는 이 프로젝트가 만든 합성 데이터입니다. 어떤 일이 숨어 있는지는 알려 주지 않습니다 — 분석 결과와 비교해 보세요.")
        sig = f"ex:{pick}"
    else:
        origin = "user"
        st.download_button("CSV 양식 내려받기", data=template_csv().encode("utf-8-sig"), file_name="preflight_성과_양식.csv", mime="text/csv", key="perf_dl_template")
        st.caption("필요한 열: 날짜, 노출수, 클릭수, 전환수, 광고비. 있으면 더 정확한 열: 캠페인, 광고세트, 소재, 기기, 도달, 노출종료시각. 행은 최대 20,000개, 2MB까지입니다.")
        up = st.file_uploader("CSV 파일", type=["csv", "txt"], key="perf_upload")
        pasted = st.text_area("또는 CSV 내용을 붙여 넣기", key="perf_csv_text", height=120)
        text = decode_csv_bytes(up.getvalue()) if up is not None else pasted
        sig = "csv:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    if model_ok:
        st.checkbox("AI가 보고서 문장을 쓰게 하기 (서버 키 사용, 비용이 듭니다)", key="perf_use_model")
    else:
        st.caption("키가 없으면 '예시 실행': 문장은 템플릿이 만듭니다.")
    use_model = bool(model_ok and ss.get("perf_use_model"))
    sig += f"|model={use_model}"

    if st.button("분석하기", type="primary", key="perf_run"):
        if source == "예시 시나리오":
            sid = dict(EXAMPLES)[pick]
            rows = _example_rows(sid)
        else:
            csv_res = _read_csv(text)
            if not csv_res.rows:
                _show_csv_problems(csv_res, fatal=True)
                ss.pop("perf_result", None)
                return
            if csv_res.errors:
                _show_csv_problems(csv_res, fatal=False)
            for n in csv_res.notices:
                st.info(n)
            rows = csv_res.rows
        settings = make_settings() if use_model else make_settings(force_demo=True)
        try:
            with tracer.trace("성과 리포트", data=origin, model=use_model):
                router = router_factory(settings)
                rep = build_report(rows, router=router, settings=settings, data_origin=origin)
        except Exception:  # noqa: BLE001 — 화면에는 원인 없이 쉬운 말만 보인다 (내용은 처리 기록에 남는다)
            st.error("분석 중 문제가 생겼어요. 데이터를 확인하고 다시 눌러 주세요.")
            return
        ss["perf_result"] = {"report": rep, "sig": sig, "origin": origin, "csv": rows_to_csv(rows) if origin == "synthetic" else ""}
    res = ss.get("perf_result")
    if not res:
        st.info("데이터를 고르고 ‘분석하기’를 눌러 주세요.")
        return
    if res["sig"] != sig:
        st.info("선택이 바뀌었어요. ‘분석하기’를 눌러 다시 분석해 주세요.")
        return
    if res["origin"] == "synthetic" and res["csv"]:
        st.download_button("이 예시 데이터를 CSV로 내려받기", data=res["csv"].encode("utf-8-sig"), file_name="preflight_예시_성과.csv", mime="text/csv", key="perf_dl_example")
    _show_result(res)
