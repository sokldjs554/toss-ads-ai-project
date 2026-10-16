"""레드팀 — 공격자 모델이 위반 소재를 변형해 방어선을 시험하고, 뚫린 사례를 회귀셋에 수확한다."""
from __future__ import annotations

import streamlit as st

from core.config import Settings
from evals.redteam import STRATEGIES, harvest, run_redteam
from evals.runner import load_report, save_report


def render(settings: Settings, router_factory) -> None:
    st.caption("‘우리 시스템은 어떻게 뚫리는가’ 를 기능으로 만든 페이지. 돌파율이 0 이어야 좋은 게 아니라, 돌파한 사례가 회귀셋에 쌓여야 좋은 것.")
    strategies = st.multiselect("공격 전략", STRATEGIES, default=STRATEGIES)
    seeds = st.number_input("시드 수", min_value=1, max_value=20, value=4)
    if st.button("🧨 레드팀 실행", type="primary"):
        router = router_factory()
        prog = st.progress(0.0, text="공격 생성·심사 중…")
        count = {"i": 0}

        def cb(seed_id, atk):
            count["i"] += 1
            prog.progress(min(1.0, count["i"] / (seeds * len(strategies))), text=f"{seed_id} · {atk.strategy} → {atk.predicted}")

        rep = run_redteam(settings, router=router, strategies=strategies, progress=cb, limit_seeds=int(seeds))
        save_report(rep, "redteam_latest.json")
        st.session_state.redteam_report = rep
        prog.empty()
    rep = st.session_state.get("redteam_report") or load_report("redteam_latest.json") or load_report("baseline_redteam_demo.json")
    if rep is None:
        st.info("아직 결과가 없습니다.")
        return
    c = st.columns(4)
    c[0].metric("모드", rep["mode"])
    c[1].metric("공격 수", len(rep["attacks"]))
    c[2].metric("돌파율 (APPROVE)", f"{rep['evasion_rate']:.1%}")
    c[3].metric("비용", f"${rep['cost_usd']:.4f}")
    st.dataframe([{"전략": k, "n": v["n"], "돌파": v["evaded"], "사람 검토": v["human_review"], "돌파율": f"{v['evasion_rate']:.0%}"} for k, v in rep["by_strategy"].items()],
                 width="stretch", hide_index=True)
    with st.expander("공격 목록", expanded=True):
        st.dataframe([{"시드": a["seed_id"], "전략": a["strategy"], "헤드라인": a["headline"], "본문": a["body"], "판정": a["predicted"], "신뢰도": a["confidence"],
                       "잡힌 규칙": ", ".join(a["predicted_rules"]), "돌파": "💥" if a["evaded"] else ""} for a in rep["attacks"]],
                     width="stretch", hide_index=True)
    only_evaded = st.toggle("돌파한 것만 수확", value=True)
    if st.button("회귀셋(adversarial.jsonl)에 수확"):
        n = harvest(rep, only_evaded=only_evaded)
        st.success(f"{n}건 추가 — 다음 평가부터 회귀 대상이 됩니다.")
