"""우회 표현 검사 — 저장된 결과 요약과 (원하면) 다시 실행."""
from __future__ import annotations

import streamlit as st

from evals.runner import load_report
from ui.labels import RUN_MODE_NAME

STRATEGY = {"spacing": "띄어쓰기 분해", "zero_width": "보이지 않는 문자", "hanja": "한자 치환", "paraphrase": "같은 뜻 다른 말", "negation": "부정문 뒤에 같은 주장",
            "bury": "면책 문구 뒤에 숨기기", "injection": "심사 조작 문장", "implicature": "직접 말하지 않고 암시"}


def _table(rep: dict) -> None:
    rows = []
    for s, m in rep.get("by_strategy", {}).items():
        rows.append({"변형 방식": STRATEGY.get(s, s), "심사한 공격 수": m["n"], "뚫림(통과)": m["evaded"], "담당자 확인": m["human_review"], "심사하지 못함(장애)": m.get("errors", 0)})
    st.dataframe(rows, width="stretch", hide_index=True)


SESSION_KEY = "redteam_session_report"   # 이 세션의 화면에서 우회 표현 검사를 돌렸을 때 그 결과를 담는 세션 상태
CLI_FILE = "redteam_latest.json"         # 명령줄(python -m evals.run --redteam)이 쓰는 파일. 이 화면은 이 파일을 쓰지 않는다


def _cli_label(rep: dict) -> str:
    """서버에 저장된 마지막 명령줄 실행 결과의 이름 — 언제 만든 것인지를 적어, 이 세션의 결과로 보이지 않게 한다."""
    day = str(rep.get("generated_at") or "")[:10]
    return f"이 서버에 저장된 마지막 CLI 실행 결과 ({day or '날짜 알 수 없음'})"


def render_summary() -> None:
    """저장된 실제 모델 결과 · 예시 실행 결과 · 이 서버에 저장된 명령줄 실행 결과를 서로 다른 이름으로 따로 보여 준다.
    '이 세션에서 실행한 결과' 는 이 세션의 화면이 실제로 돌린 결과가 세션 상태에 있을 때만 나온다 (디스크의 파일을 그렇게 부르지 않는다)."""
    options: dict[str, tuple[str, str]] = {}   # 이름 → (종류, 파일 이름)
    for label, name in (("저장된 실제 모델 결과 (5차, gpt-4o)", "baseline_redteam_partial.json"), ("예시 실행 (실제 AI 호출 없음)", "baseline_redteam_demo.json")):
        if load_report(name) is not None:
            options[label] = ("file", name)
    cli = load_report(CLI_FILE)
    if cli is not None:
        options[_cli_label(cli)] = ("file", CLI_FILE)
    session_rep = st.session_state.get(SESSION_KEY)
    if session_rep is not None:
        options["이 세션에서 실행한 결과"] = ("session", "")
    if not options:
        st.caption("저장된 우회 표현 검사 결과가 없습니다.")
        return
    pick = st.selectbox("결과 선택", list(options.keys()), key="redteam_pick")
    kind, name = options[pick]
    rep = session_rep if kind == "session" else load_report(name)
    judged = [a for a in rep.get("attacks", []) if not a.get("error")]      # 제공자 장애로 심사하지 못한 변형은 세지 않는다
    errored = len(rep.get("attacks", [])) - len(judged)
    evaded = sum(1 for a in judged if a.get("evaded"))
    hr = sum(1 for a in judged if a.get("predicted") == "HUMAN_REVIEW")
    cost = f"${rep.get('cost_usd', 0)}" + (" 이상 (응답을 받지 못한 요청의 비용은 알 수 없음)" if rep.get("cost_estimated") else "")
    st.markdown(f"- 심사한 변형 {len(judged)}건 중 검사를 통과한(뚫린) 변형 {evaded}건, 담당자 확인으로 넘어간 변형 {hr}건"
                + (f" (모델 호출이 실패해 심사하지 못한 변형 {errored}건은 제외)" if errored else "") + f". 실행 방식 {RUN_MODE_NAME.get(rep.get('mode'), rep.get('mode'))} · 비용 {cost}")
    _table(rep)
    with st.expander("변형별 결과"):
        from core.schemas import Decision
        from ui.labels import DECISION_LABEL

        st.dataframe([{"시드": a.get("seed_id"), "변형 방식": STRATEGY.get(a.get("strategy"), a.get("strategy")), "제목": a.get("headline"), "판정": DECISION_LABEL.get(Decision(a["predicted"]), a["predicted"]) if a.get("predicted") in Decision.__members__ else a.get("predicted"),
                       "뚫림": "예" if a.get("evaded") else "", "잡은 규칙": ", ".join(a.get("predicted_rules", []))} for a in rep.get("attacks", [])], width="stretch", hide_index=True)


def render(settings, router_factory) -> None:
    """(기존 호환) 다시 실행 + 요약."""
    from evals.redteam import harvest, run_redteam

    if st.button("우회 표현 검사 실행 (현재 설정)"):
        with st.spinner("변형을 만들고 검사하는 중…"):
            rep = run_redteam(settings, router=router_factory(settings))
            st.session_state[SESSION_KEY] = rep   # 이 세션의 결과는 세션에만 둔다. 명령줄이 쓰는 파일(redteam_latest.json)은 건드리지 않는다
            harvest(rep)
        st.session_state["redteam_pick"] = "이 세션에서 실행한 결과"   # 방금 돌린 결과를 바로 보여 준다
        st.rerun()
    render_summary()
