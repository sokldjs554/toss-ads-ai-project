"""검사 사례 — 저장된 실제 모델 실행 기록과 대표 검사 사례. 새 모델 호출 없이 본다."""
from __future__ import annotations

import html
import json
from datetime import datetime

import streamlit as st

from core.config import Settings
from core.schemas import Decision
from evals.runner import evaluate_case, load_dataset
from guardrails.policy import get_catalog
from observability import replay
from ui import theme
from ui.components import finding_sentence, rule_title
from ui.labels import DECISION_LABEL, REWRITE_SHORT_LABEL, RUN_MODE_LABEL
from ui.presets import PRESETS, build_brief
from ui.workbench import PRESET_TITLES

BLURB = {
    "loan_ok": "조건과 안내 문구를 갖춘 요청. 이 기록에서는 광고안 3개 중 점검 통과 없이 게재 불가 1개와 수정 필요 2개로 판정되고, 검사를 통과한 수정 제안이 함께 나옵니다.",
    "malicious": "'원금 보장·무조건 승인' 을 써 달라는 요청. 문제 표현을 빼고 고쳐서 진행했고, 이 기록에서는 광고안 3개 중 2개가 점검 통과, 1개가 수정 필요로 나왔습니다(게재 불가 없음).",
    "injection": "상품 설명에 숨은 '승인하라' 지시문과 보이지 않는 문자. 걷어내고 진행합니다.",
    "prohibited": "리딩방 유도는 광고할 수 없는 내용이라 광고안을 만들지 않습니다.",
    "commerce_landing": "쿠폰 조건이 랜딩 페이지와 달라 수정이 필요한 경우.",
}


def _plain_reason(final: str, reason: str, review: dict, catalog) -> str:
    if final == Decision.REJECT.value:
        return "검사 기준상 쓸 수 없는 표현이 확인됐습니다."
    if final == Decision.REVISE.value:
        return "확인이 필요한 구절이 있어 고친 뒤 다시 검사해야 합니다."
    if final == Decision.HUMAN_REVIEW.value:
        if "원문에 없음" in reason:
            return "검토 모델이 원문에 없는 내용을 근거로 제시해 자동으로 확정하지 않았습니다."
        if "불일치" in reason:
            return "두 번의 검토 판단이 서로 달라 자동으로 확정하지 않았습니다."
        if "임계값" in reason:
            return "검토 모델의 확신이 기준보다 낮았습니다."
        return "자동으로 확정하지 못했습니다."
    return "검사 기준의 모든 항목을 통과했습니다."


def _stored_case(key: str, path, catalog) -> None:
    p = PRESETS[key]
    trace = json.loads(path.read_text(encoding="utf-8"))
    brief = build_brief(p)
    cats = catalog.categories_for(brief.product, p["advertiser_input"])
    s = replay.summarize_trace(trace, brief.product, cats, p.get("landing_text", ""), catalog)
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        with h1:
            st.markdown(f"### {PRESET_TITLES.get(key, key)}")
        with h2:
            st.markdown(f"<div style='text-align:right;padding-top:12px'>{theme.mode_pill('stored', RUN_MODE_LABEL['stored'] + ' · ' + s['recorded_at'])}</div>", unsafe_allow_html=True)
        st.caption(BLURB.get(key, "") + f" — 모델 {', '.join(s['models']) or '—'} · 호출 {s['llm_calls']}회 · ${s['cost_usd']:.4f} · {s['duration_ms']/1000:.1f}초 · 검사 기준 {s['policy_version']} · 프롬프트 {s['prompt_version']}")
        st.markdown(f"**요청** {html.escape(p['advertiser_input'])}  \n**상품** {html.escape(p['product_name'])} · 사실 {len(p['facts'])}개", unsafe_allow_html=True)
        ic = s["intercept"]
        if ic["triggered"]:
            kind = {"policy_intent": "요청의 문제 표현을 빼고 진행", "injection": "숨은 지시문을 걷어내고 진행", "prohibited_category": "광고 불가 내용 — 진행하지 않음"}.get(ic["kind"], "요청을 고쳐서 진행")
            st.markdown(f"- 요청 검사: **{kind}**" + (f" (뺀 표현: {', '.join(ic['removed'])})" if ic["removed"] else ""))
        if s["halted"] and not s["variants"]:
            st.markdown("- 광고안을 만들지 않았습니다.")
        for i, v in enumerate(s["variants"], start=1):
            final = v["final"]
            d = Decision(final) if final else Decision.HUMAN_REVIEW
            st.markdown(f"**광고안 {i}** {theme.badge(d)} &nbsp; <span class='pf-muted'>{html.escape(v['copy'].headline)}</span>", unsafe_allow_html=True)
            st.markdown(f"<div class='pf-reason'>{html.escape(_plain_reason(final, v['reason'], v['review'], catalog))}</div>", unsafe_allow_html=True)
            with st.expander(f"광고안 {i} 자세히"):
                from ui.components import render_ad

                render_ad(v["copy"], v["det_now"].findings, catalog=catalog)
                today = datetime.now().strftime("%Y-%m-%d")
                st.markdown(f"**지금 코드로 다시 한 코드 검사** — {today} 계산 · 검사 기준 {catalog.version}")
                st.caption(f"모델 호출 없이 지금 코드로 다시 계산한 값입니다. 아래 '저장된 실행 결과' 의 판정·수정안 상태는 {s['recorded_at']} 기록 그대로이며 이 계산으로 바뀌지 않습니다.")
                if v["det_now"].findings:
                    for f in v["det_now"].findings:
                        st.markdown(f"- {theme.severity_chip(f.severity)} {html.escape(finding_sentence(f, catalog))}", unsafe_allow_html=True)
                else:
                    st.caption("없음")
                if v["det_changed"]:
                    st.caption(f"저장된 기록({s['recorded_at']}, 검사 기준 {s['policy_version']})의 코드 검사 결과와 다릅니다 — 당시 {', '.join(rule_title(r, catalog) for r in v['det_rule_ids_then']) or '없음'} / 지금 {', '.join(rule_title(r, catalog) for r in v['det_rule_ids_now']) or '없음'}. 검사 기준이 바뀌었기 때문이며, 판정(위 배지)은 기록 당시 것입니다.")
                rv = v["review"]
                st.markdown(f"**저장된 실행 결과** — {s['recorded_at']} 기록 · 검사 기준 {s['policy_version']} · 프롬프트 {s['prompt_version']} · {', '.join(s['models']) or '모델 기록 없음'}")
                st.markdown(f"**검토 모델** 판정 {DECISION_LABEL.get(Decision(rv['decision']), rv['decision']) if rv.get('decision') else '—'} · 확신 {rv.get('confidence')} · {rv.get('provider')}"
                            + (f" · 원문에 없는 내용을 근거로 제시 {rv['unverified_quotes']}건" if rv.get("unverified_quotes") else ""))
                for f in rv.get("findings", []):
                    st.markdown(f"- “{html.escape(f.get('quote',''))}” — {html.escape(rule_title(f.get('rule_id',''), catalog))}: {html.escape(f.get('explanation',''))}")
                if v["second"]:
                    sec = v["second"]
                    st.markdown(f"**교차 확인** {DECISION_LABEL.get(Decision(sec['decision']), sec['decision']) if sec.get('decision') else '—'} · {'다름' if sec.get('disagreement') else '같음'}" + ("" if sec.get("independent") else " · 같은 제공자라 독립적이지 않음"))
                from core.schemas import RewriteStatus

                # 수정안의 상태는 저장된 실행 기록의 최종 상태 그대로다 (observability.replay.stored_rewrite_state). 코드 검사만으로 다시 계산하지 않는다.
                state = v["rewrite_state"]
                status = RewriteStatus(state["status"])
                st.markdown(f"**수정안** {REWRITE_SHORT_LABEL[status]} · 저장된 실행 결과 기준")
                if state["source"] == "legacy_unknown":
                    st.caption(state["note"])
                elif state["note"] and status != RewriteStatus.NONE:
                    st.caption(state["note"])
                rc_rec = state.get("recheck")
                if rc_rec and status != RewriteStatus.NONE:
                    parts = [f"코드 검사 {DECISION_LABEL.get(Decision(rc_rec['det_decision']), rc_rec['det_decision']) if rc_rec.get('det_decision') else '기록 없음'}"]
                    if rc_rec.get("semantic_decision"):
                        parts.append(f"의미 검사 {DECISION_LABEL.get(Decision(rc_rec['semantic_decision']), rc_rec['semantic_decision'])} (확신 {rc_rec.get('semantic_confidence')})")
                    else:
                        parts.append("의미 검사 기록 없음")
                    st.caption("기록된 근거 — " + " · ".join(parts))
                if v["rewrite"] and status == RewriteStatus.VERIFIED:
                    from core.schemas import AdCopy

                    rw = v["rewrite"]
                    render_ad(AdCopy(headline=rw["headline"], body=rw["body"], cta=rw.get("cta") or "", disclosures=rw.get("disclosures", [])), title="수정안 확인 완료", catalog=catalog)
                st.caption(f"판정 근거(내부): {v['reason']}")
        b1, b2, _ = st.columns([1, 1, 2])
        with b1:
            if st.button("광고 작업에 불러오기", key=f"load_{key}"):
                st.session_state.pending_load = key
                st.session_state.nav_request = "광고 작업"
                st.rerun()
        with b2:
            if st.button("예시로 바로 실행", key=f"run_{key}", help="이 사례의 입력으로 AI 호출 없이 예시 실행을 합니다. 위의 저장된 실제 모델 결과는 바뀌지 않습니다."):
                st.session_state.pending_load = key
                st.session_state.pending_autorun = True
                st.session_state.pending_demo_run = True
                st.session_state.nav_request = "광고 작업"
                st.rerun()


def _golden_examples(catalog) -> None:
    st.markdown("### 대표 검사 사례 (코드 검사만, 모델 호출 없음)")
    st.caption("평가 데이터에서 고른 사례입니다. '기대 판정' 은 이 프로젝트가 정한 정답이고, '코드 검사' 는 표현 규칙·안내 문구·상품 정보 일치 검사만 지금 돌린 결과입니다. 검토 모델이 없으면 통과를 확정하지 않으므로 정상 사례는 '담당자 확인 필요' 로 남습니다.")
    picks = {"정상 사례": ["G-060", "G-064"], "조건이 빠진 사례": ["G-069", "H-007"], "문제가 있는 표현": ["G-001", "H-004"], "숫자가 상품 정보와 다른 사례": ["H-002", "H-003"]}
    cases = {c.id: c for c in load_dataset("golden") + load_dataset("holdout")}
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    rows = []
    for group, ids in picks.items():
        for cid in ids:
            c = cases.get(cid)
            if not c:
                continue
            r = evaluate_case(c, s, None, catalog, use_llm=False)
            rows.append({"구분": group, "광고 문구": f"{c.headline} / {c.body}", "기대 판정": DECISION_LABEL[Decision(c.label)],
                         "코드 검사": DECISION_LABEL[Decision(r.predicted)], "잡은 이유": ", ".join(rule_title(x, catalog) for x in r.predicted_rules) or "—"})
    st.dataframe(rows, width="stretch", hide_index=True)


def render() -> None:
    catalog = get_catalog()
    st.markdown("## 검사 사례")
    st.markdown("실제 모델(GPT-4o)로 실행해 **저장해 둔 기록**과 대표 사례입니다. 여기서 보는 것은 **저장된 실제 모델 결과**이며, 보는 것만으로는 모델을 부르지 않습니다. 날짜와 모델이 함께 표시됩니다.")
    st.caption("‘예시로 바로 실행’ 은 같은 입력을 AI 호출 없이 예시 실행으로 돌리는 것이고 저장된 결과를 바꾸지 않습니다. 새 실제 모델 실행은 ‘광고 작업’ 에서 API 키와 비용을 확인한 뒤에만 할 수 있습니다.")
    stored = replay.list_stored("partial")
    if not stored:
        st.info("저장된 실제 모델 실행 기록이 없습니다.")
    for key in ("loan_ok", "malicious", "injection", "commerce_landing", "prohibited"):
        if key in stored:
            _stored_case(key, stored[key], catalog)
    _golden_examples(catalog)
