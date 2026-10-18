"""Preflight — 광고가 올라가기 전에 확인이 필요한 구절을 먼저 찾는 광고 검사 도구 (개인 프로젝트).

    pip install -r requirements.txt && streamlit run app.py

화면: 광고 작업 / 검사 사례 / 품질 확인. 판정·검사 로직은 agents/·guardrails/ 에만 있고 화면은 그리기만 한다.
서버 API 키는 여기서만 읽는다 — 입력창 기본값·세션 상태·처리 기록·내려받기 어디에도 값이 들어가지 않는다.
"""
from __future__ import annotations

import html
import inspect
import os
from pathlib import Path

import streamlit as st

from core.config import IN_BROWSER, Settings
from llm.router import Router
from observability.tracer import Tracer
from ui import cases, quality, theme, workbench
from ui.labels import DISCLAIMER, PROJECT_NAME

st.set_page_config(page_title=f"{PROJECT_NAME} · 광고 검사", page_icon="✅", layout="wide", initial_sidebar_state="collapsed")
theme.inject()

# --- 서버 키: 서버에서만 읽고 쓴다. 화면에는 '설정 여부' 만 내보낸다. ------------------------------
_SERVER = Settings.from_env()
SERVER_STATUS = {"openai": bool(_SERVER.openai_api_key) and not _SERVER.force_demo, "anthropic": bool(_SERVER.anthropic_api_key) and not _SERVER.force_demo,
                 "run_budget_usd": _SERVER.run_budget_usd}  # 서버 키로 도는 실행의 비용 상한 (PREFLIGHT_RUN_BUDGET_USD)


def make_settings(user_openai_key: str = "", user_anthropic_key: str = "", **overrides) -> Settings:
    """사용자가 직접 넣은 키가 있으면 그 키를, 없으면 서버 키를 쓴다. 두 값 모두 화면으로 돌려보내지 않는다."""
    return Settings.from_env(openai_api_key=(user_openai_key or _SERVER.openai_api_key), anthropic_api_key=(user_anthropic_key or _SERVER.anthropic_api_key), **overrides)


def router_factory(settings: Settings, clean: bool = False) -> Router:
    """광고 작업용 라우터. 장애 시뮬레이션은 광고 작업에만 적용한다 (clean=True 면 무시 — 품질 확인의 평가 실행용)."""
    r = Router(settings, tracer=st.session_state.tracer)
    drill = st.session_state.get("opt_drill", "없음")
    if not clean and drill and drill != "없음":
        prov, kind = [x.strip() for x in drill.split(":")]
        r.simulate_failure(prov, kind.split("(")[0].strip())
    return r


def clean_router_factory(settings: Settings) -> Router:
    return router_factory(settings, clean=True)


if "tracer" not in st.session_state:
    st.session_state.tracer = Tracer(max_traces=30)

# --- 상단: 이름 · 개인 프로젝트 표시 · 화면 전환 --------------------------------------------
VIEWS = ["광고 작업", "검사 사례", "품질 확인"]
req = st.session_state.pop("nav_request", None)
if "_nav_reset" in st.session_state:   # 같은 탭을 다시 눌러 선택이 풀렸을 때, 위젯을 만들기 전에 마지막으로 보던 화면을 되돌려 놓는다
    st.session_state["nav"] = st.session_state.pop("_nav_reset")
elif req in VIEWS:
    st.session_state["nav"] = req
elif "nav" not in st.session_state:
    qv = st.query_params.get("view")
    st.session_state["nav"] = qv if qv in VIEWS else VIEWS[0]
pending = st.session_state.pop("pending_load", None)
if pending:
    workbench._load_preset(pending)
    st.session_state["_pending_load_applied"] = True
    if st.session_state.pop("pending_autorun", False):
        st.session_state["_force_autorun"] = True  # 세션 안에서 한 번만. URL 에는 남기지 않는다
        if st.session_state.pop("pending_demo_run", False):
            st.session_state["_force_demo_run"] = True  # 검사 사례에서 여는 실행은 AI 호출이 없는 예시 실행

# 배포된 버전 표시: Render 는 RENDER_GIT_COMMIT, 브라우저 실행판·수동 배포는 PREFLIGHT_BUILD 로 커밋을 넘긴다 (없으면 표시하지 않음)
_build = (os.environ.get("PREFLIGHT_BUILD") or os.environ.get("RENDER_GIT_COMMIT") or "").strip()
if not _build:
    try:
        _build = (Path(__file__).resolve().parent / "build_info.txt").read_text(encoding="utf-8").strip()  # 브라우저 실행판 번들
    except OSError:
        _build = ""
_build = _build[:7]
st.markdown(f'<div class="pf-topbar"><div class="pf-brand">✅ {PROJECT_NAME}<small>광고가 올라가기 전에 확인이 필요한 구절을 먼저 찾습니다</small></div>'
            f'<div class="pf-note">개인 프로젝트 · 토스·토스애즈와 무관{" · 빌드 " + html.escape(_build) if _build else ""}</div></div>', unsafe_allow_html=True)
_nav_kw = {"required": True} if "required" in inspect.signature(st.segmented_control).parameters else {}   # 선택 해제를 막는다 (지원하지 않는 버전은 아래에서 되돌린다)
nav = st.segmented_control("화면", VIEWS, key="nav", label_visibility="collapsed", width="stretch", **_nav_kw)
if nav is None:
    st.session_state["_nav_reset"] = st.session_state.get("_nav_last", VIEWS[0])
    st.rerun()
st.session_state["_nav_last"] = nav

if nav == "광고 작업":
    workbench.render(make_settings, st.session_state.tracer, router_factory, SERVER_STATUS)
elif nav == "검사 사례":
    cases.render()
else:
    quality.render(make_settings, clean_router_factory, st.session_state.tracer, SERVER_STATUS)

st.markdown(f'<hr style="border:none;border-top:1px solid #E5E8EB;margin:32px 0 10px"><div class="pf-note">{html.escape(DISCLAIMER)}'
            + (" · 브라우저 실행판 (서버 없음, 예시 실행만)" if IN_BROWSER else "") + "</div>", unsafe_allow_html=True)
