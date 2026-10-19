"""실패 모드 카탈로그 · 정책 규칙 카탈로그."""
from __future__ import annotations

import streamlit as st

from core.schemas import Layer, Severity
from guardrails.failure_modes import FAILURE_MODES, FEATURES, by_feature
from guardrails.policy import get_catalog
from ui.labels import LAYER_LABEL, SEVERITY_LABEL


def _code_range(modes) -> str:
    nums = sorted(int(fm.code.split("-")[1]) for fm in modes)
    return f"FM-{nums[0]:02d} ~ FM-{nums[-1]:02d}" if nums else ""


def render_failure_modes() -> None:
    st.caption("모델이 틀리거나 실패할 수 있는 지점을 코드로 먼저 적어 두었습니다. 각 항목은 어떻게 알아채고, 어떤 대체 경로를 타고, 어떤 테스트가 보장하는지를 가집니다 (guardrails/failure_modes.py).")
    hit: dict[str, int] = {}
    for entry in st.session_state.get("runs", []):
        run = entry["run"] if isinstance(entry, dict) else entry
        for f in run.failures:
            hit[f.code] = hit.get(f.code, 0) + 1
    if hit:
        st.markdown("이 세션에서 실제로 발생한 항목: " + " ".join(f"`{k}`×{v}" for k, v in sorted(hit.items())))
    groups = by_feature()
    st.markdown(f"모두 **{len(FAILURE_MODES)}개**입니다. 기능별로 묶었으니 제목을 눌러 펼쳐 보세요.")
    st.caption("‘실패 시 막힘’ 칸에 ‘한계 있음’ 이라고 적힌 항목은, 아직 고치지 못한 한계가 있어 이 방어만으로는 못 막는 경우가 있다는 뜻입니다. 어떤 한계인지는 ‘방어’ 칸에 적었습니다.")
    for key, label in FEATURES.items():
        modes = groups[key]
        if not modes:
            continue
        n_limit = sum(1 for fm in modes if not fm.fail_closed)
        title = f"{label} — {len(modes)}개 ({_code_range(modes)})" + (f" · 한계 있음 {n_limit}개" if n_limit else "")
        with st.expander(title, expanded=False):
            rows = [{"코드": fm.code, "이름": fm.name, "단계": fm.stage, "증상": fm.symptom, "탐지": fm.detection, "방어": fm.defense, "테스트": fm.test,
                     "실패 시 막힘": "✓" if fm.fail_closed else "한계 있음"} for fm in modes]
            st.dataframe(rows, width="stretch", hide_index=True, height=min(720, 38 + 35 * len(rows)))


def render_policy() -> None:
    cat = get_catalog()
    st.caption(f"검사 규칙 v{cat.version} · {len(cat.rules)}개. 공개된 광고주 가이드와 금융소비자보호법·대부업법·표시광고법 공개 조문을 읽고 이 프로젝트가 재구성한 것이며, 어느 회사의 실제 심사 기준도 아닙니다.")
    f1, f2 = st.columns(2)
    with f1:
        sev_names = {sv.value: SEVERITY_LABEL[sv] for sv in Severity}
        sev = st.multiselect("구분", list(sev_names), default=list(sev_names), format_func=sev_names.get)
    with f2:
        layer_keys = ["lexical", "disclosure", "grounding", "injection", "semantic", "structural"]
        layer_names = {k: LAYER_LABEL[Layer(k)] for k in layer_keys}
        layer_pick = st.multiselect("검사 종류", [layer_names[k] for k in layer_keys], default=[layer_names[k] for k in layer_keys])
        layer = [k for k in layer_keys if layer_names[k] in layer_pick]
    rows = [{**r, "severity": sev_names.get(r["severity"], r["severity"]), "layer": layer_names.get(r["layer"], r["layer"])} for r in cat.summary_table() if r["severity"] in sev and r["layer"] in layer]
    st.dataframe(rows, width="stretch", hide_index=True, height=600)
    with st.expander("업종별 필수 고지 체크리스트"):
        for c, spec in cat.disclosures.items():
            st.markdown(f"**{c}** → `{spec.rule_id}`")
            for it in spec.items:
                st.markdown(f"- {it.label}: `{it.canonical}`")
