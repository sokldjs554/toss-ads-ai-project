"""운영 — 검수 대기열(사람이 확인할 건과 라벨링)과 운영 지표·모니터·효과 계산기·감사 기록을 한 화면 안의 두 칸으로 묶는다.

두 화면은 서로의 상태를 읽지 않는다. 광고 작업의 결과(`runs`)는 검수 대기열이 세션에서 직접 읽는다.
"""
from __future__ import annotations

import streamlit as st

from observability.tracer import Tracer
from ui import ops_view, review_queue_view


def render(tracer: Tracer, server_status: dict) -> None:
    st.caption("광고를 사람이 확인하는 흐름(검수 대기열·라벨링)과, 서비스를 운영할 때 보는 지표·모니터·효과 계산을 모았습니다. "
               "실제 운영 데이터는 없어서 합성 데이터와 저장된 평가 결과로 동작을 보여 줍니다.")
    q, o = st.tabs(["검수 대기열", "운영 지표·모니터"])
    with q:
        review_queue_view.render(st.session_state.get("runs"))
    with o:
        ops_view.render(tracer, server_status)
