"""운영 화면(ui/ops_view.py) 화면 흐름 테스트 — Streamlit AppTest, 네트워크 없음, 예시 실행.

- 기본값에서 예외 없음, 효과 계산기의 '실제 운영 효과 아님' 상시 표시, 모니터 시뮬레이션 표시
- 가정 0 입력 → 쉬운 오류 (예외 없음)
- 측정 파일 없음 상태 → '측정 없음' 표시, 직접 가정하면 '가정' 표지
- 감사 기록 검증: 변조 예시·올린 파일 검증
- 가로 넘침 없음: 고정 폭 HTML·표 태그가 없고 표는 모두 폭 맞춤(실제 390px 브라우저 확인은 수동으로 했다 — 보고 참조)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ops import impact
from ops.audit import AuditLog

ROOT = Path(__file__).resolve().parents[1]
JARGON = ("오탐", "미탐", "fact_id", "finding", "Finding", "Grounding", "Guardrail", "HUMAN_REVIEW", "APPROVE", "REVISE", "REJECT", "GRND-", "TA-", "가장 중요한 이유")

SCRIPT_EMPTY = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from observability.tracer import Tracer
from ui import theme, ops_view
theme.inject()
if "tracer" not in st.session_state:
    st.session_state.tracer = Tracer()
ops_view.render(st.session_state.tracer, {{"openai": False, "anthropic": False}})
'''

SCRIPT_WITH_RUNS = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from core.config import Settings
from agents.pipeline import run_pipeline
from observability.tracer import Tracer
from ui import theme, ops_view
from ui.presets import PRESETS, build_brief
theme.inject()
if "tracer" not in st.session_state:
    st.session_state.tracer = Tracer()
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
    st.session_state.runs = [{{"run": run_pipeline(build_brief(PRESETS[k]), s, tracer=st.session_state.tracer)}} for k in ("loan_ok", "commerce_landing")]
ops_view.render(st.session_state.tracer, {{"openai": False, "anthropic": False}})
'''


def _app(script: str, tmp_path: Path) -> AppTest:
    f = tmp_path / "ops_harness.py"
    f.write_text(script, encoding="utf-8")
    return AppTest.from_file(str(f), default_timeout=120)


def _texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info] + [w.value for w in at.warning] + [e.value for e in at.error] + [s.value for s in at.success]
    return "\n".join(str(p) for p in parts)


def _visible(at: AppTest) -> str:
    """접힌 expander 안을 뺀, 기본 화면에 바로 보이는 글자."""
    out: list[str] = []

    def walk(node):
        kids = getattr(node, "children", None)
        if kids is None:
            val = getattr(node, "value", None)
            if isinstance(val, str):
                out.append(val)
            return
        if getattr(node, "type", "") == "expander":
            out.append(getattr(node, "label", ""))
            return
        for k in kids.values():
            walk(k)

    walk(at.main)
    return "\n".join(out)


def _frame_text(at: AppTest) -> str:
    cells = []
    for d in at.dataframe:
        df = d.value
        cells.extend(str(c) for c in list(df.columns))
        cells.extend(str(v) for v in df.to_numpy().ravel())
    return "\n".join(cells)


def _kinds(at: AppTest) -> set[str]:
    """효과 표의 '구분' 열 값 (측정 · 가정 · 계산)."""
    return {str(v) for d in at.dataframe if "구분" in d.value.columns for v in d.value["구분"]}


@pytest.fixture
def empty_app(tmp_path):
    return _app(SCRIPT_EMPTY, tmp_path)


# --- 기본 -------------------------------------------------------------------------------------
def test_default_screen_renders_without_exceptions_and_shows_all_sections(empty_app):
    at = empty_app.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    text = _texts(at)
    for heading in ("1. 이 세션의 처리 지표", "2. 모니터 상태", "3. 효과 계산기", "4. 감사 기록 검증", "5. 처리량 측정 결과"):
        assert heading in text, heading
    assert "실제 서비스의 운영 데이터가 없습니다" in text
    assert "아직 이 세션의 처리 기록이 없습니다" in text                                   # 기록이 없는 상태도 안내로 처리


def test_effect_calculator_always_shows_the_not_real_effect_notice(empty_app):
    at = empty_app.run()
    warnings = [w.value for w in at.warning]
    assert sum(impact.NOT_REAL_EFFECT in w for w in warnings) >= 2                       # 계산기 머리말 + 결과 끝
    assert any("측정값 출처" in str(c.value) and "compare_live.json" in str(c.value) for c in at.caption)
    assert "하루 절감 시간" in _frame_text(at) and _kinds(at) == {"측정", "계산"}


def test_monitor_simulation_is_labeled_as_simulation(empty_app):
    at = empty_app.run()
    assert any("시뮬레이션입니다" in w.value and "실제 서비스의 기록이 아닙니다" in w.value for w in at.warning)
    assert "판정 분포가 평소와 달라짐" in _frame_text(at)


def test_no_jargon_in_visible_text_or_tables(empty_app):
    at = empty_app.run()
    visible = _visible(at)
    frames = _frame_text(at)
    for jargon in JARGON:
        assert jargon not in visible, jargon
        assert jargon not in frames, jargon


def test_session_keys_use_the_opsv_prefix_only(tmp_path):
    at = _app(SCRIPT_WITH_RUNS, tmp_path).run()
    assert not at.exception
    mine = {k for k in list(at.session_state) if k not in ("tracer", "runs") and not k.startswith(("$$", "_"))}
    assert mine and all(k.startswith("opsv_") for k in mine), sorted(k for k in mine if not k.startswith("opsv_"))


# --- 세션 기록이 있을 때 ------------------------------------------------------------------------
def test_metrics_section_with_session_runs(tmp_path):
    at = _app(SCRIPT_WITH_RUNS, tmp_path).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    text = _texts(at)
    assert "실행 / 광고안" in text and "예시 실행(실제 AI 호출 없음)입니다" in text and "비용 0원" in text
    frames = _frame_text(at)
    for needle in ("점검 통과", "수정 필요", "게재 불가", "담당자 확인 필요", "코드 검사", "검토 모델 검사", "예시 실행(대역)"):
        assert needle in frames, needle
    assert any(e.label.startswith("상세 기록") for e in at.expander)
    prom = [c.value for c in at.code]
    assert any("# TYPE preflight_runs_total counter" in v for v in prom)


def test_session_monitor_source_is_reported_as_insufficient_with_few_runs(tmp_path):
    at = _app(SCRIPT_WITH_RUNS, tmp_path).run()
    at.radio(key="opsv_monitor_src").set_value("session").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "판단 보류" in _texts(at) or "판단 보류" in _frame_text(at)
    assert not any("울린 점검" in e.value for e in at.error)


def test_session_monitor_source_without_runs_asks_for_more_data(empty_app):
    at = empty_app.run()
    at.radio(key="opsv_monitor_src").set_value("session").run()
    assert not at.exception and any("2건 이상" in i.value for i in at.info)


def test_monitor_scenario_selection_fires_the_expected_rule(empty_app):
    at = empty_app.run()
    at.selectbox(key="opsv_sim_scenario").set_value("provider_outage").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("모델 제공자 실패가 급증" in e.value for e in at.error)
    at.selectbox(key="opsv_sim_scenario").set_value("normal").run()
    assert not any("울린 점검" in e.value for e in at.error)
    at.slider(key="opsv_sim_point").set_value(120).run()                      # 변화 시작 전 시점에서는 표본 부족/정상
    assert not at.exception


def test_score_simulation_button_runs_and_shows_a_table(empty_app):
    at = empty_app.run()
    at.number_input(key="opsv_score_streams").set_value(5).run()
    at.button(key="opsv_score_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["opsv_score"]["n_streams"] == 5
    assert "알아챈 스트림" in _frame_text(at) and any("잘못 울린" in str(c.value) for c in at.caption)


# --- 효과 계산기 ------------------------------------------------------------------------------
def test_zero_assumption_shows_an_easy_error_not_an_exception(empty_app):
    at = empty_app.run()
    for key, label in (("opsv_daily", "하루 검수 건수"), ("opsv_min_review", "사람 1건당 검수 시간"), ("opsv_wage", "사람 시급"), ("opsv_min_flagged", "담당자 확인 건의 처리 시간")):
        at.number_input(key=key).set_value(0.0).run()
        assert not at.exception, (key, [str(e.value) for e in at.exception])
        errs = [e.value for e in at.error]
        assert any(label in e and "0보다 커야" in e for e in errs), (key, errs)
        assert all("Traceback" not in e and "ZeroDivision" not in e for e in errs)
        assert any(impact.NOT_REAL_EFFECT in str(c.value) for c in at.caption)         # 오류 상태에서도 문구는 남는다
        at.number_input(key=key).set_value({"opsv_daily": 200.0, "opsv_min_review": 5.0, "opsv_wage": 15000.0, "opsv_min_flagged": 8.0}[key]).run()
        assert not at.error


def test_effect_table_columns_put_the_worse_value_under_pessimistic(empty_app):
    at = empty_app.run()
    rows = {}
    for d in at.dataframe:
        if "구분" in d.value.columns:
            for _, r in d.value.iterrows():
                rows[r["항목"]] = r
    num = lambda x: float(str(x).replace(",", ""))                          # noqa: E731
    cost, saved = rows["한 달 AI 호출 비용"], rows["하루 절감 시간"]
    assert num(cost["비관"]) > num(cost["낙관"]) and num(saved["비관"]) < num(saved["낙관"])


def test_calculator_responds_to_assumption_changes_and_options(empty_app):
    at = empty_app.run()
    first = _frame_text(at)
    at.number_input(key="opsv_daily").set_value(1000.0).run()
    assert not at.exception and _frame_text(at) != first
    at.checkbox(key="opsv_use_share").check().run()
    assert not at.exception and any("다시 가중" in str(c.value) for c in at.caption)
    at.checkbox(key="opsv_whatif_on").check().run()
    assert not at.exception
    assert any("직접 입력한 담당자 확인율" in str(c.value) for c in at.caption) and "가정" in _frame_text(at)
    for split in ("dev", "holdout", "all"):
        at.selectbox(key="opsv_split").set_value(split).run()
        assert not at.exception, split


def test_missing_measurement_files_show_not_measured_and_invent_nothing(empty_app, tmp_path, monkeypatch):
    monkeypatch.setattr(impact, "REPORTS_DIR", tmp_path / "no_reports")
    at = empty_app.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    infos = " ".join(i.value for i in at.info)
    assert "측정 없음" in infos and "직접 가정" in infos
    assert not any("compare_live.json" in str(c.value) and "측정값 출처" in str(c.value) for c in at.caption)
    assert "하루 절감 시간" not in _frame_text(at)                                      # 측정이 없으면 효과를 계산해 보이지 않는다
    # 직접 가정하면 가정으로 표시해 계산한다
    at.checkbox(key="opsv_whatif_on").check().run()
    assert not at.exception
    frame = _frame_text(at)
    assert "하루 절감 시간" in frame and _kinds(at) == {"가정", "계산"}                  # 측정 표지가 붙은 항목은 하나도 없다
    assert any(impact.NOT_REAL_EFFECT in w.value for w in at.warning)
    assert "한 달 AI 호출 비용" not in frame                                            # 측정에서 온 비용은 없다


# --- 감사 기록 검증 ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,ok", [("none", True), ("modify", False), ("delete", False), ("reorder", False), ("truncate", False)])
def test_tamper_demo_detects_each_kind(empty_app, kind, ok):
    at = empty_app.run()
    at.selectbox(key="opsv_tamper").set_value(kind).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    if ok:
        assert any("체인이 맞습니다" in s.value for s in at.success)
        assert not any("변조 또는 손상" in e.value for e in at.error)
    else:
        assert any("변조 또는 손상이 의심됩니다" in e.value for e in at.error), kind


def test_audit_download_and_upload_verification(tmp_path):
    at = _app(SCRIPT_WITH_RUNS, tmp_path).run()
    assert at.session_state is not None
    log = AuditLog()
    for i in range(4):
        log.append("ad.judged", "u", {"i": i})
    good = log.export_jsonl()
    if not hasattr(at, "file_uploader"):
        pytest.skip("이 Streamlit 버전의 AppTest 는 file_uploader 를 지원하지 않는다")
    at.file_uploader(key="opsv_upload").set_value(("a.jsonl", good.encode(), "application/json")).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("항목 4개의 체인이 맞습니다" in s.value for s in at.success)
    lines = good.splitlines()
    d = json.loads(lines[2])
    d["actor"] = "evil"
    lines[2] = json.dumps(d)
    at.file_uploader(key="opsv_upload").set_value(("a.jsonl", ("\n".join(lines) + "\n").encode(), "application/json")).run()
    assert not at.exception
    assert any("변조 또는 손상이 의심됩니다" in e.value and "항목 2" in e.value for e in at.error)
    # 마지막 지문을 따로 입력하면 끝 잘림도 알린다
    at.text_input(key="opsv_expected_hash").set_value(log.head_hash).run()
    at.file_uploader(key="opsv_upload").set_value(("a.jsonl", ("\n".join(good.splitlines()[:3]) + "\n").encode(), "application/json")).run()
    assert any("마지막 해시가 따로 보관한 값과 다릅니다" in e.value for e in at.error)


def test_hostile_upload_is_shown_as_plain_text_without_exceptions(empty_app):
    at = empty_app.run()
    if not hasattr(at, "file_uploader"):
        pytest.skip("file_uploader 미지원")
    hostile = ('{"v":1,"alg":"sha256","index":0,"ts":"t","type":"a.b","actor":"u","payload":{},"redacted":0,"prev":"x","hash":"y",'
               '"<script>alert(1)</script>":"[click](javascript:alert(1))"}\n')
    at.file_uploader(key="opsv_upload").set_value(("x.jsonl", hostile.encode(), "application/json")).run()
    assert not at.exception
    msg = " ".join(e.value for e in at.error)
    assert "<script" not in msg and "](javascript" not in msg and "변조 또는 손상" in msg
    at.file_uploader(key="opsv_upload").set_value(("x.jsonl", b"\xff\xfe\x00bad", "application/octet-stream")).run()
    assert not at.exception and any("읽을 수 없는 파일" in e.value for e in at.error)


# --- 처리량 측정 ------------------------------------------------------------------------------
def test_loadtest_section_shows_stored_results_and_limits(empty_app):
    at = empty_app.run()
    frames = _frame_text(at)
    assert "초당 처리 건수" in frames and "예시 실행 1회" in frames and "코드 검사" in frames
    assert any("네트워크·모델 지연이 빠져 있어" in w.value for w in at.warning)
    assert any("이 측정이 말하지 않는 것" in e.label for e in at.expander)


def test_loadtest_section_without_a_stored_file(empty_app, monkeypatch, tmp_path):
    from ui import ops_view

    monkeypatch.setattr(ops_view, "LOADTEST_FILE", tmp_path / "none.json")
    at = empty_app.run()
    assert not at.exception and any("저장된 처리량 측정 결과가 없습니다" in i.value for i in at.info)


# --- 가로 넘침: 구조 점검 ------------------------------------------------------------------------
def test_layout_has_no_fixed_width_elements_that_could_overflow_on_a_phone(empty_app):
    src = (ROOT / "ui" / "ops_view.py").read_text(encoding="utf-8")
    assert "<table" not in src and "width:" not in src and "white-space" not in src and "min-width" not in src
    assert src.count("unsafe_allow_html=True") == len(re.findall(r"theme\.stat\(", src))        # HTML 은 이스케이프된 통계 카드(theme.stat)에만 쓴다
    assert len(re.findall(r"st\.dataframe\(", src)) == len(re.findall(r'width="stretch"', src))
    at = empty_app.run()
    for m in at.markdown:
        v = str(m.value)
        if v.lstrip().startswith("<style>"):          # 하네스가 넣은 공통 테마 CSS (이 화면이 그리는 것이 아니다)
            continue
        assert "<table" not in v and not re.search(r"width\s*:\s*\d{3,}px", v)


def test_view_does_not_call_models_or_the_network(empty_app, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("운영 화면이 네트워크를 썼다")

    monkeypatch.setattr(socket.socket, "connect", boom)
    at = empty_app.run()
    at.button(key="opsv_score_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
