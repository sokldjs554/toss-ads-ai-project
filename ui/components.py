"""공통 위젯: 광고 카드(확인이 필요한 구절 강조), 이유 문장, 근거 목록, 담당자 확인 요청문. 화면 문구는 ui/labels 를 쓴다."""
from __future__ import annotations

import html
from typing import Optional

import streamlit as st

from core.schemas import AdCopy, Decision, Finding, Layer, PipelineRun, PreflightReport, ProductFacts, RewriteStatus, Severity
from ui.labels import DECISION_LABEL, LAYER_LABEL, SEVERITY_LABEL, SEVERITY_STYLE, check_name

STATUS_COLOR = {"ok": "#2E7D32", "error": "#B42318", "intercepted": "#E8590C", "fallback": "#9A6700", "running": "#999"}
KIND_COLOR = {"stage": "#3182F6", "llm": "#7C3AED", "guardrail": "#E8590C", "tool": "#0B7A3B"}


# --- 광고 카드 -----------------------------------------------------------------
def _mark(text: str, spans: list[tuple[int, int, Finding]], catalog=None) -> str:
    spans = sorted(spans, key=lambda x: (x[0], -(x[1] - x[0])))
    out = []
    cursor = 0
    for s, e, f in spans:
        if s < cursor or s < 0 or e > len(text):
            continue
        out.append(html.escape(text[cursor:s]))
        color = SEVERITY_STYLE[f.severity]
        tip = finding_sentence(f, catalog, with_quote=False)
        out.append(f'<mark class="pf-mark" style="background:{color}1F;border-bottom:2px solid {color}" title="{html.escape(tip)}">{html.escape(text[s:e])}</mark>')
        cursor = e
    out.append(html.escape(text[cursor:]))
    return "".join(out)


def render_ad(copy: AdCopy, findings: Optional[list[Finding]] = None, title: str | None = None, catalog=None) -> None:
    """광고를 카드로 그린다. 확인이 필요한 구절은 색과 밑줄로 강조하고, 마우스를 올리면 이유가 보인다."""
    findings = [f for f in (findings or []) if f.start >= 0 and f.end > f.start and f.verified_quote]
    fields = [("headline", copy.headline), ("body", copy.body), ("cta", copy.cta)] + [(f"disc{i}", d) for i, d in enumerate(copy.disclosures)]
    offsets: dict[str, int] = {}
    pos = 0
    for name, value in fields:
        offsets[name] = pos
        pos += len(value) + 1

    def spans_for(name: str, value: str):
        base = offsets[name]
        return [(f.start - base, f.end - base, f) for f in findings if base <= f.start and f.end <= base + len(value)]

    h = _mark(copy.headline, spans_for("headline", copy.headline), catalog)
    b = _mark(copy.body, spans_for("body", copy.body), catalog)
    c = _mark(copy.cta, spans_for("cta", copy.cta), catalog)
    disc_html = "".join(f'<div>· {_mark(d, spans_for(f"disc{i}", d), catalog)}</div>' for i, d in enumerate(copy.disclosures))
    t = f'<div class="t">{html.escape(title or "")}</div>' if title else ""
    d = f'<div class="d">{disc_html}</div>' if disc_html else ""
    st.markdown(f'<div class="pf-ad">{t}<div class="h">{h}</div><div class="b">{b}</div><div><span class="c">{c}</span></div>{d}</div>',
                unsafe_allow_html=True)


# --- 이유·근거 문장 --------------------------------------------------------------
def rule_title(rule_id: str, catalog) -> str:
    r = catalog.get(rule_id) if catalog else None
    return r.title if r else rule_id


def finding_sentence(f: Finding, catalog, with_quote: bool = True) -> str:
    """내부 코드 없이 읽히는 한 문장."""
    title = rule_title(f.rule_id, catalog)
    expl = (f.explanation or "").strip()
    body = title if (not expl or expl == title) else f"{title} — {expl}"
    if with_quote and f.quote:
        body = f"“{f.quote}” · {body}"
    if not f.verified_quote:
        body += " (검토 모델이 원문에 없는 내용을 근거로 제시해 참고로만 둡니다)"
    return body


def plain_reason(rep: PreflightReport, catalog) -> str:
    """가장 중요한 이유 한 문장."""
    d = rep.final_decision
    fs = [f for f in rep.verdict.findings if f.verified_quote]
    blocks = [f for f in fs if f.severity == Severity.BLOCK]
    fixes = [f for f in fs if f.severity == Severity.FIX]
    if d == Decision.REJECT and blocks:
        f = blocks[0]
        return f"“{f.quote}” 은(는) 검사 기준상 쓸 수 없는 표현입니다 ({rule_title(f.rule_id, catalog)})." if f.quote else f"{rule_title(f.rule_id, catalog)} 사유가 확인됐습니다."
    if d == Decision.REVISE and fixes:
        f = fixes[0]
        return f"“{f.quote}” 부분을 고쳐야 합니다 ({rule_title(f.rule_id, catalog)})." if f.quote else f"{rule_title(f.rule_id, catalog)} — {f.explanation}"
    if d == Decision.HUMAN_REVIEW:
        return human_review_reason(rep)
    if d == Decision.APPROVE:
        warns = [f for f in fs if f.severity == Severity.WARN]
        if warns:
            return f"검사 기준에서 문제는 없고, 참고 사항 {len(warns)}건이 있습니다."
        return "검사 기준의 모든 항목을 통과했습니다."
    return rep.decision_reason


def human_review_reason(rep: PreflightReport) -> str:
    r = rep.decision_reason
    if "실패" in r:
        return "검토 모델 단계가 실패해 자동으로 확정하지 않았습니다."
    if "검증된 반려 근거가 없음" in r:
        return "검토 모델이 게재 불가를 제안했지만 그 근거를 원문에서 확인하지 못했습니다."
    if "원문에 없음" in r:
        return "검토 모델이 원문에 없는 내용을 근거로 제시해 그 판단을 그대로 쓰지 않았습니다."
    if "남은 근거 finding 이 없음" in r:
        return "검토 모델이 수정을 제안했지만 남은 근거가 없어 자동으로 확정하지 않았습니다."
    if "임계값" in r:
        return "검토 모델의 확신이 자동 판정 기준보다 낮았습니다."
    if "불일치" in r:
        return "두 모델 계열의 판단이 서로 달랐습니다."
    return r


def what_to_check(rep: PreflightReport, catalog) -> list[str]:
    """담당자가 확인할 것."""
    items: list[str] = []
    r = rep.decision_reason
    for f in rep.verdict.findings:
        if f.layer == Layer.SEMANTIC:
            tag = "원문에 없는 내용을 근거로 제시 — 실제로 그런 문제가 있는지" if not f.verified_quote else "실제로 문제가 되는 표현인지"
            items.append(f"“{f.quote}” — {rule_title(f.rule_id, catalog)}: {tag}")
    if "불일치" in r:
        items.append(f"검토 모델 판정 {DECISION_LABEL[rep.verdict.decision]} vs 교차 확인 {DECISION_LABEL[rep.verdict.second_opinion] if rep.verdict.second_opinion else '?'} — 어느 쪽이 맞는지")
    if "임계값" in r:
        items.append(f"검토 모델의 확신 {rep.verdict.confidence:.2f} — 모델이 든 이유가 타당한지")
    if "실패" in r:
        items.append("검토 모델 호출이 실패했습니다. 다시 실행하거나 표현 규칙·안내 문구 검사 결과만으로 판단할지")
    if rep.rewrite_status == RewriteStatus.FAILED_RECHECK:
        items.append("자동 수정안이 재검사를 통과하지 못했습니다. 직접 수정이 필요한지")
    if not items:
        items.append("검토 모델의 이유(상세 보기)가 타당한지")
    return items


def evidence_lines(rep: PreflightReport, product: ProductFacts, catalog) -> list[str]:
    """어떤 상품 정보·검사 기준을 근거로 판단했는지."""
    lines: list[str] = []
    by_id = {f.id: f for f in product.facts}
    used = sorted({c.fact_id for c in rep.ad_copy.claims if c.fact_id in by_id})
    for fid in used:
        lines.append(f"상품 정보 {fid}: {by_id[fid].text}")
    rules = []
    for f in rep.verdict.findings:
        if f.verified_quote or f.layer != Layer.SEMANTIC:
            t = f"{rule_title(f.rule_id, catalog)} ({LAYER_LABEL[f.layer]})"
            if t not in rules:
                rules.append(t)
    lines.extend(f"검사 기준: {t}" for t in rules)
    if not lines:
        lines.append("표현 규칙·필수 안내 문구·상품 정보 일치·형식 검사와 검토 모델의 의미 검사를 모두 거쳤습니다.")
    return lines


def checklist_rows(rep: PreflightReport) -> list[dict]:
    return [{"항목": check_name(c.name), "결과": "통과" if c.passed else "확인 필요", "내용": c.detail} for c in rep.checklist]


def _handoff_lines(copy: AdCopy, rep: PreflightReport, catalog, title: str, header: list[str], copy_label: str) -> str:
    lines = [title, *header, "", copy_label,
             f"  제목: {copy.headline}", f"  본문: {copy.body}", f"  버튼: {copy.cta}"]
    if copy.disclosures:
        lines.append("  안내 문구: " + " / ".join(copy.disclosures))
    lines += ["", f"확정하지 못한 이유: {human_review_reason(rep)}", "", "확인해 주실 것"]
    lines += [f"  - {x}" for x in what_to_check(rep, catalog)]
    fs = [f for f in rep.verdict.findings]
    if fs:
        lines += ["", "검사에서 나온 항목"]
        lines += [f"  - [{SEVERITY_LABEL[f.severity]}] {finding_sentence(f, catalog)} (규칙 {f.rule_id})" for f in fs]
    lines += ["", "진행 가능 여부: 담당자 확인 전에는 게재할 수 없습니다.",
              "※ 이 요청문은 이 화면이 만든 텍스트입니다. 검토 대기열·담당자 지정·권한 관리는 없으며, 전달은 사용자가 직접 합니다."]
    return "\n".join(lines)


def human_review_request_text(rep: PreflightReport, run: PipelineRun, catalog) -> str:
    """최초 검사 결과(수정 전 원본)에 대한 담당자 확인 요청문. 실제 검토 대기열은 없다 — 전달은 사용자가 한다."""
    return _handoff_lines(rep.ad_copy, rep, catalog, f"[담당자 확인 요청] 광고안 {rep.variant_id}",
                          [f"대상: 처음 검사한 광고 원문 (수정 전)", f"실행 ID: {run.run_id} · 처리 기록: {run.trace_id} · 실행 방식: {run.mode}"], "광고 원문")


def recheck_request_text(copy: AdCopy, result, original_variant_id: str, original_run_id: str, catalog) -> str:
    """고쳐서 다시 검사한 문구에 대한 담당자 확인 요청문 — 지금 검사한 문구와 그 재검사 결과·재검사의 처리 기록으로만 만든다.
    수정 전 원본의 실행 ID 는 '어디서 시작한 문구인지' 참고로만 적고, 이 요청의 근거 기록으로 쓰지 않는다."""
    info = result.info or {}
    header = [f"대상: 고쳐서 다시 검사한 문구 (원래 광고안 {original_variant_id} 의 수정본)",
              f"검사 시각: {result.at.strftime('%Y-%m-%d %H:%M:%S')} · 처리 기록(이 재검사): {result.trace_id} · 실행 방식: {result.mode}",
              f"검사 설정: 검사 기준 {info.get('policy_version', '?')} · 프롬프트 {info.get('prompt_version', '?')} · 자동 판정 확신 기준 {info.get('threshold', '?')}",
              f"참고: 수정 전 원본이 나온 실행 {original_run_id}"]
    return _handoff_lines(copy, result.report, catalog, f"[담당자 확인 요청] 광고안 {original_variant_id} (수정본 재검사)", header, "다시 검사한 문구")
