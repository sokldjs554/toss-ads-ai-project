"""실패 모드 카탈로그 · 정책 규칙 카탈로그."""
from __future__ import annotations

import streamlit as st

from guardrails.failure_modes import FAILURE_MODES
from guardrails.policy import get_catalog


def render_failure_modes() -> None:
    st.caption("‘이 시스템은 어떻게 망가지는가’ 를 코드로 먼저 적었습니다. 각 항목은 탐지 방법·방어선·보장하는 테스트를 가집니다 (guardrails/failure_modes.py).")
    hit: dict[str, int] = {}
    for run in st.session_state.get("runs", []):
        for f in run.failures:
            hit[f.code] = hit.get(f.code, 0) + 1
    if hit:
        st.markdown("이 세션에서 실제로 밟은 실패 모드: " + " ".join(f"`{k}`×{v}" for k, v in sorted(hit.items())))
    st.dataframe([{"코드": fm.code, "이름": fm.name, "단계": fm.stage, "증상": fm.symptom, "탐지": fm.detection, "방어": fm.defense, "테스트": fm.test,
                   "fail-closed": "✓" if fm.fail_closed else ""} for fm in FAILURE_MODES], width="stretch", hide_index=True, height=720)


def render_policy() -> None:
    cat = get_catalog()
    st.caption(f"규칙 카탈로그 v{cat.version} · {len(cat.rules)}개 규칙. 토스애즈 공개 광고주 가이드와 금소법·대부업법·표시광고법 공개 조문을 읽고 이 프로젝트가 재구성한 것이며, 실제 심사 기준이 아닙니다.")
    f1, f2 = st.columns(2)
    with f1:
        sev = st.multiselect("심각도", ["block", "fix", "warn"], default=["block", "fix", "warn"])
    with f2:
        layer = st.multiselect("계층", ["lexical", "disclosure", "grounding", "injection", "semantic", "structural"], default=["lexical", "disclosure", "grounding", "injection", "semantic", "structural"])
    rows = [r for r in cat.summary_table() if r["severity"] in sev and r["layer"] in layer]
    st.dataframe(rows, width="stretch", hide_index=True, height=600)
    with st.expander("업종별 필수 고지 체크리스트"):
        for c, spec in cat.disclosures.items():
            st.markdown(f"**{c}** → `{spec.rule_id}`")
            for it in spec.items:
                st.markdown(f"- {it.label}: `{it.canonical}`")
