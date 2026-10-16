"""공통 위젯: 판정 배지, 인용 하이라이트, 모드 배너."""
from __future__ import annotations

import html

import streamlit as st

from core.schemas import AdCopy, Decision, Finding, Severity

DECISION_STYLE = {
    Decision.APPROVE: ("#0B7A3B", "#E6F6EC", "승인 가능"),
    Decision.REVISE: ("#9A6700", "#FFF4D6", "수정 후 재심사"),
    Decision.REJECT: ("#B42318", "#FDE8E6", "반려"),
    Decision.HUMAN_REVIEW: ("#3B4B5E", "#EAEFF5", "사람 검토"),
}
SEVERITY_STYLE = {Severity.BLOCK: "#B42318", Severity.FIX: "#9A6700", Severity.WARN: "#3B4B5E"}
STATUS_COLOR = {"ok": "#2E7D32", "error": "#B42318", "intercepted": "#E8590C", "fallback": "#9A6700", "running": "#999"}
KIND_COLOR = {"stage": "#3182F6", "llm": "#7C3AED", "guardrail": "#E8590C", "tool": "#0B7A3B"}


def decision_badge(d: Decision, big: bool = False) -> str:
    fg, bg, label = DECISION_STYLE[d]
    size = "15px" if big else "12px"
    return (f'<span style="background:{bg};color:{fg};border:1px solid {fg};border-radius:999px;'
            f'padding:2px 10px;font-weight:700;font-size:{size};white-space:nowrap">{label} · {d.value}</span>')


def severity_chip(s: Severity) -> str:
    c = SEVERITY_STYLE[s]
    return f'<span style="color:{c};border:1px solid {c};border-radius:6px;padding:0 6px;font-size:11px;font-weight:700">{s.value}</span>'


def mode_banner(mode: str, openai_model: str, anthropic_model: str) -> None:
    if mode == "demo":
        st.warning("**DEMO 모드** — API 키가 없어 LLM 단계(플래너·카피·패널·심사)는 결정론적 **대역(mock)** 이 수행합니다. "
                   "가드레일·관측·평가·판정 로직은 실제 코드가 그대로 돕니다. 결과는 모델 성능이 아닙니다 (FM-22). "
                   "왼쪽 사이드바에 키를 넣으면 LIVE 로 바뀝니다.", icon="🧪")
    elif mode == "partial":
        st.info(f"**PARTIAL 모드** — 한 제공자 키만 있어 그 제공자가 모든 작업을 맡습니다 (교차 fallback 없음). OpenAI={openai_model}, Anthropic={anthropic_model}", icon="🔌")
    else:
        st.success(f"**LIVE 모드** — 구조화 작업 → OpenAI `{openai_model}` (Structured Outputs), 창의·정성 작업 → Anthropic `{anthropic_model}`. 서로가 서로의 fallback.", icon="🛫")


def _mark(text: str, spans: list[tuple[int, int, Finding]]) -> str:
    """text 안의 [start,end) 구간을 <mark> 로 감싼다 (겹치면 앞의 것 우선)."""
    spans = sorted(spans, key=lambda x: (x[0], -(x[1] - x[0])))
    out = []
    cursor = 0
    for s, e, f in spans:
        if s < cursor or s < 0 or e > len(text):
            continue
        out.append(html.escape(text[cursor:s]))
        color = SEVERITY_STYLE[f.severity]
        out.append(f'<mark style="background:{color}22;border-bottom:2px solid {color};padding:0 2px" title="{html.escape(f.rule_id)}: {html.escape(f.explanation)}">{html.escape(text[s:e])}</mark>')
        cursor = e
    out.append(html.escape(text[cursor:]))
    return "".join(out)


def render_copy(copy: AdCopy, findings: list[Finding] | None = None, title: str | None = None) -> None:
    """소재를 카드로 그린다. finding 의 인용 구간은 하이라이트."""
    findings = [f for f in (findings or []) if f.start >= 0 and f.end > f.start and f.verified_quote]
    full = copy.full_text()
    # full_text 의 필드별 오프셋
    fields = [("headline", copy.headline), ("body", copy.body), ("cta", copy.cta)] + [(f"disc{i}", d) for i, d in enumerate(copy.disclosures)]
    offsets: dict[str, int] = {}
    pos = 0
    for name, value in fields:
        offsets[name] = pos
        pos += len(value) + 1
    def spans_for(name: str, value: str):
        base = offsets[name]
        return [(f.start - base, f.end - base, f) for f in findings if base <= f.start and f.end <= base + len(value)]
    # 필드 밖에 걸친 인용은 전체에서 찾아 표시
    h = _mark(copy.headline, spans_for("headline", copy.headline))
    b = _mark(copy.body, spans_for("body", copy.body))
    c = _mark(copy.cta, spans_for("cta", copy.cta))
    disc_html = "".join(f'<div style="font-size:12px;color:#4E5968;margin-top:2px">· {_mark(d, spans_for(f"disc{i}", d))}</div>' for i, d in enumerate(copy.disclosures))
    tone = f'<div style="font-size:11px;color:#8B95A1;margin-bottom:6px">{html.escape(title or "")} {html.escape(copy.tone)}</div>' if (title or copy.tone) else ""
    st.markdown(
        f'<div style="border:1px solid #E5E8EB;border-radius:14px;padding:14px 16px;background:#fff">{tone}'
        f'<div style="font-size:17px;font-weight:700;line-height:1.35">{h}</div>'
        f'<div style="font-size:14px;margin-top:6px;line-height:1.5">{b}</div>'
        f'<div style="margin-top:10px"><span style="background:#3182F6;color:#fff;border-radius:8px;padding:4px 12px;font-size:13px;font-weight:600">{c}</span></div>'
        f'{("<div style=\"margin-top:10px;border-top:1px dashed #E5E8EB;padding-top:6px\">" + disc_html + "</div>") if disc_html else ""}</div>',
        unsafe_allow_html=True,
    )


def findings_table(findings: list[Finding], catalog) -> None:
    if not findings:
        st.caption("finding 없음")
        return
    rows = []
    for f in findings:
        rule = catalog.get(f.rule_id)
        rows.append({"규칙": f.rule_id, "심각도": f.severity.value, "계층": f.layer.value, "인용": f.quote or "—",
                     "인용 검증": "✓" if f.verified_quote else "✗ (원문에 없음)", "설명": f.explanation,
                     "출처": rule.section if rule else ""})
    st.dataframe(rows, width="stretch", hide_index=True)
