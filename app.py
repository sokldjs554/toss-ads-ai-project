"""Preflight — 광고가 뜨기 전에, LLM 이 틀릴 곳을 먼저 보는 가드레일 스튜디오.

    pip install -r requirements.txt && streamlit run app.py
"""
from __future__ import annotations

import os

import streamlit as st

from core.config import IN_BROWSER, Settings
from llm.router import Router
from observability.tracer import Tracer
from ui import catalog_view, evals_view, redteam_view, studio, trace_view
from ui.components import mode_banner

st.set_page_config(page_title="Preflight · 광고 사전점검", page_icon="🛫", layout="wide", initial_sidebar_state="expanded")

if "tracer" not in st.session_state:
    st.session_state.tracer = Tracer(max_traces=30)
if "runs" not in st.session_state:
    st.session_state.runs = []
if "rejected_history" not in st.session_state:
    st.session_state.rejected_history = []

with st.sidebar:
    st.markdown("## 🛫 Preflight")
    st.caption("광고주 요청 → 캠페인 계획 → 소재 → 가상 유저 → 가드레일 심사 → 판정. 모든 단계가 트레이스로 남습니다.")
    if IN_BROWSER:
        oa_key, an_key = "", ""
        oa_model, an_model = os.environ.get("OPENAI_MODEL", "gpt-4o"), os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        st.caption("🧪 브라우저 실행판 — 서버가 없어 API 키를 쓸 수 없고 항상 DEMO 모드입니다. 실제 모델 결과는 재생 페이지(../replay/)에 있습니다.")
    else:
        with st.expander("🔑 API 키 (세션에만 저장)", expanded=False):
            oa_key = st.text_input("OPENAI_API_KEY", type="password", value=os.environ.get("OPENAI_API_KEY", ""))
            an_key = st.text_input("ANTHROPIC_API_KEY", type="password", value=os.environ.get("ANTHROPIC_API_KEY", ""))
            oa_model = st.text_input("OpenAI 모델 (구조화·심사)", value=os.environ.get("OPENAI_MODEL", "gpt-4o"))
            an_model = st.text_input("Anthropic 모델 (창의·패널)", value=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
            st.caption("둘 다 비우면 DEMO 모드. gpt-4o 는 2027-01-14 API 종료 예정 — 그 뒤엔 gpt-5 계열로 바꾸세요.")
    st.markdown("### 판정·실행 옵션")
    _provisional = Settings.from_env(openai_api_key=oa_key.strip(), anthropic_api_key=an_key.strip())
    _rec = None
    try:
        from evals.runner import load_report

        _base = load_report(f"baseline_{_provisional.mode.value}.json")
        _rec = (_base or {}).get("recommended_threshold_for_zero_unsafe_and_zero_false_flag")
    except Exception:  # noqa: BLE001
        _rec = None
    _default_tau = float(_rec) if _rec else float(os.environ.get("PREFLIGHT_AUTO_DECISION_CONFIDENCE", "0.80"))
    threshold = st.slider("자동 판정 최소 신뢰도 τ", 0.5, 0.99, _default_tau, 0.01,
                          help="심사 신뢰도가 이 값보다 낮으면 사람 검토. 평가 탭의 커버리지 곡선으로 정합니다.")
    if _rec:
        st.caption(f"기본값 {_rec} 은 `{_provisional.mode.value}` 모드 기준선에서 '위험 승인 0%·오탐 0%' 를 만족하는 최소 τ 입니다.")
    st.session_state.opt_n_variants = st.slider("소재 변형 수", 1, 5, 3)
    st.session_state.opt_audience = st.toggle("가상 유저 패널", value=True)
    st.session_state.opt_second = st.toggle("경계 사례 교차 검증 (다른 모델 계열)", value=True)
    budget = st.number_input("실행당 비용 상한 (USD)", min_value=0.01, value=0.50, step=0.05, help="초과하면 FM-20 으로 중단하고 남은 소재는 사람 검토")
    st.markdown("### 🧯 장애 훈련 (outage drill)")
    drill = st.selectbox("제공자 장애 시뮬레이션", ["없음", "openai: timeout", "openai: schema(JSON 깨짐)", "anthropic: timeout", "anthropic: refusal", "mock: timeout"],
                         help="해당 제공자의 호출을 강제로 실패시켜 fallback·fail-closed 경로를 보여 줍니다.")
    if st.session_state.runs and st.button("세션 초기화"):
        st.session_state.runs = []
        st.session_state.tracer = Tracer(max_traces=30)
        st.session_state.rejected_history = []
        st.rerun()

settings = Settings.from_env(openai_api_key=oa_key.strip(), anthropic_api_key=an_key.strip(), openai_model=oa_model.strip() or "gpt-4o",
                             anthropic_model=an_model.strip() or "claude-sonnet-5-5", auto_decision_confidence=threshold, run_budget_usd=float(budget))


def router_factory() -> Router:
    r = Router(settings, tracer=st.session_state.tracer)
    if drill != "없음":
        prov, kind = [x.strip() for x in drill.split(":")]
        r.simulate_failure(prov, kind.split("(")[0].strip())
    return r


st.markdown("# 🛫 Preflight — 광고 사전점검 스튜디오")
st.markdown("광고주가 말로 요청하면 캠페인·소재가 만들어지고, **올라가기 전에** 가드레일이 먼저 틀릴 곳을 잡습니다. "
            "모델은 제안만 하고, 판정은 코드가 하며, 모든 결정은 트레이스로 증명합니다.")
mode_banner(settings.mode.value, settings.openai_model, settings.anthropic_model)

tabs = st.tabs(["🛫 스튜디오", "🔍 관측 (Trace)", "📊 평가", "🧨 레드팀", "⚠️ 실패 모드", "📜 정책 규칙"])
with tabs[0]:
    studio.render(settings, st.session_state.tracer, router_factory)
with tabs[1]:
    trace_view.render(st.session_state.tracer)
with tabs[2]:
    evals_view.render(settings, router_factory)
with tabs[3]:
    redteam_view.render(settings, router_factory)
with tabs[4]:
    catalog_view.render_failure_modes()
with tabs[5]:
    catalog_view.render_policy()
