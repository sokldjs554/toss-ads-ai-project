"""운영 — 검수 대기열, 운영 지표, 서버 기반 공동 검수를 한 화면에 모은다.

광고 작업의 결과(`runs`)는 로컬 검수 대기열과 공동 검수의 추가 화면에 전달한다.
"""
from __future__ import annotations

import streamlit as st

from observability.tracer import Tracer
from ui import ops_view, review_queue_view, team_review_view


def render(tracer: Tracer, server_status: dict) -> None:
    st.caption("광고를 사람이 확인하는 흐름과, 서비스를 운영할 때 보는 지표·모니터·효과 계산을 모았습니다. "
               "검수 대기열·운영 지표의 예시와 서버에 저장하는 공동 검수를 각각 열어볼 수 있습니다.")
    q, o, t = st.tabs(["검수 대기열", "운영 지표·모니터", "공동 검수"])
    with q:
        review_queue_view.render(st.session_state.get("runs"))
    with o:
        ops_view.render(tracer, server_status)
    with t:
        team_review_view.render(st.session_state.get("runs"))
