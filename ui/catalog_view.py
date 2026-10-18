"""실패 모드 카탈로그 · 정책 규칙 카탈로그."""
from __future__ import annotations

import streamlit as st

from core.schemas import Severity
from guardrails.failure_modes import FAILURE_MODES
from guardrails.policy import get_catalog
from ui.labels import SEVERITY_LABEL


def render_failure_modes() -> None:
    st.caption("모델이 틀리거나 실패할 수 있는 지점을 코드로 먼저 적어 두었습니다. 각 항목은 어떻게 알아채고, 어떤 대체 경로를 타고, 어떤 테스트가 보장하는지를 가집니다 (guardrails/failure_modes.py).")
    hit: dict[str, int] = {}
    for entry in st.session_state.get("runs", []):
        run = entry["run"] if isinstance(entry, dict) else entry
        for f in run.failures:
            hit[f.code] = hit.get(f.code, 0) + 1
    if hit:
        st.markdown("이 세션에서 실제로 발생한 항목: " + " ".join(f"`{k}`×{v}" for k, v in sorted(hit.items())))
    st.dataframe([{"코드": fm.code, "이름": fm.name, "단계": fm.stage, "증상": fm.symptom, "탐지": fm.detection, "방어": fm.defense, "테스트": fm.test,
                   "fail-closed": "✓" if fm.fail_closed else ""} for fm in FAILURE_MODES], width="stretch", hide_index=True, height=720)


def render_policy() -> None:
    cat = get_catalog()
    st.caption(f"검사 규칙 v{cat.version} · {len(cat.rules)}개. 공개된 광고주 가이드와 금융소비자보호법·대부업법·표시광고법 공개 조문을 읽고 이 프로젝트가 재구성한 것이며, 어느 회사의 실제 심사 기준도 아닙니다.")
    f1, f2 = st.columns(2)
    with f1:
        sev_names = {sv.value: SEVERITY_LABEL[sv] for sv in Severity}
        sev = st.multiselect("구분", list(sev_names), default=list(sev_names), format_func=sev_names.get)
    with f2:
        layer = st.multiselect("계층", ["lexical", "disclosure", "grounding", "injection", "semantic", "structural"], default=["lexical", "disclosure", "grounding", "injection", "semantic", "structural"])
    rows = [{**r, "severity": sev_names.get(r["severity"], r["severity"])} for r in cat.summary_table() if r["severity"] in sev and r["layer"] in layer]
    st.dataframe(rows, width="stretch", hide_index=True, height=600)
    with st.expander("업종별 필수 고지 체크리스트"):
        for c, spec in cat.disclosures.items():
            st.markdown(f"**{c}** → `{spec.rule_id}`")
            for it in spec.items:
                st.markdown(f"- {it.label}: `{it.canonical}`")
