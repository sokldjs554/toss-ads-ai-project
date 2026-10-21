"""성과 리포트 화면 (Streamlit AppTest, 네트워크 없음).

- 예시를 분석하면 이상·원인 후보·다음 액션이 보인다 / 대조 예시는 '이상 없음'
- 예시 이름에서 사건 종류가 드러나지 않는다 (정답을 미리 알려 주지 않는다)
- 잘못된 CSV 는 줄 번호가 있는 쉬운 오류 / 수식·HTML 이 든 칸은 글자 그대로
- 합성/내 데이터 표시, 한계 문장, 대역(템플릿) 표시, 가로 넘침·예외 없음, 세션 키 접두사
"""
from __future__ import annotations

import html
import re

import pytest
from streamlit.testing.v1 import AppTest

from core import performance as P
from ui import performance_view as pv

JARGON = ("오탐", "미탐", "fact_id", "finding", "Finding", "Grounding", "Guardrail", "HUMAN_REVIEW", "APPROVE", "REVISE", "REJECT", "GRND-", "undetermined", "budget_cap",
          "creative_fatigue", "tracking_break", "landing_issue", "bid_competition", "audience_saturation", "mix_shift", "seasonal_dip", "no_event", "confidence", "hypothesis")


def app(model: bool = False):
    import streamlit as st

    from core.config import Settings
    from llm.base import Task
    from llm.router import Router
    from observability.tracer import Tracer
    from ui import performance_view

    if "tracer" not in st.session_state:
        st.session_state.tracer = Tracer()

    def make_settings(a="", b="", **kw):
        if model and not kw.get("force_demo"):
            return Settings.from_env(openai_api_key="k", anthropic_api_key="k", force_demo=False)
        return Settings.from_env(openai_api_key=a, anthropic_api_key=b, **kw)

    def router_factory(settings):
        if model and not settings.force_demo:
            from core.insight_engine import template_narrative
            from tests.fakes import TaskScriptedProvider

            prov = TaskScriptedProvider("openai", {Task.INSIGHT: lambda req: template_narrative(req.context["analysis"])})
            return Router(settings, providers={"openai": prov, "anthropic": TaskScriptedProvider("anthropic")}, tracer=st.session_state.tracer)
        return Router(settings, tracer=st.session_state.tracer)

    status = {"openai": model, "anthropic": model, "run_budget_usd": 0.5}
    performance_view.render(make_settings, st.session_state.tracer, router_factory, status)


def fresh(**kw):
    return AppTest.from_function(app, kwargs=kw, default_timeout=90).run()


def text(at: AppTest) -> str:
    parts = ([m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info] + [w.value for w in at.warning] + [e.value for e in at.error]
             + [s.value for s in at.success] + [x.label for x in at.expander])
    return html.unescape("\n".join(str(p) for p in parts))


def visible(at: AppTest) -> str:
    """접힌 expander 안을 뺀, 바로 보이는 글자."""
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
    return html.unescape("\n".join(out))


def run_example(at: AppTest, label: str) -> AppTest:
    at.selectbox(key="perf_example").set_value(label).run()
    at.button(key="perf_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def label_of(prefix: str) -> str:
    return next(n for n, sid in pv.EXAMPLES if sid.startswith(prefix))


# ───────────── 기본 화면 ─────────────
def test_screen_opens_without_exceptions_and_states_its_limits():
    at = fresh()
    assert not at.exception, [str(e.value) for e in at.exception]
    t = text(at)
    assert "성과 리포트" in t and pv.LIMIT_LINE in t
    assert "예시 시나리오" in [o for o in at.radio(key="perf_source").options] and "CSV 올리기" in at.radio(key="perf_source").options
    assert "분석하기" in [b.label for b in at.button] and "키가 없으면 '예시 실행': 문장은 템플릿이 만듭니다." in t
    assert "데이터를 고르고" in t                                                   # 아직 분석 전


def test_example_names_do_not_reveal_what_happened():
    at = fresh()
    opts = list(at.selectbox(key="perf_example").options)
    assert opts == [f"예시 {chr(65 + i)}" for i in range(len(opts))] and len(opts) >= 8
    assert not any(w in " ".join(opts) for w in ("전환", "소재", "예산", "랜딩", "입찰", "이상", "대조", "추적", "요일"))
    assert len({sid for _, sid in pv.EXAMPLES}) == len(pv.EXAMPLES)
    assert [sid for _, sid in pv.EXAMPLES] != sorted(sid for _, sid in pv.EXAMPLES)       # 순서가 사건 종류별로 정렬돼 있지 않다


def test_every_example_is_a_dev_scenario_not_a_holdout_one():
    from evals.insights_eval import load_scenarios

    dev = {s.id for s in load_scenarios("dev")}
    held = {s.id for s in load_scenarios("holdout")}
    assert {sid for _, sid in pv.EXAMPLES} <= dev and not {sid for _, sid in pv.EXAMPLES} & held


# ───────────── 이상이 있는 예시 ─────────────
def test_anomalous_example_shows_what_happened_causes_actions_and_the_report():
    at = run_example(fresh(), label_of("dev-tracking_break-1"))
    t, v = text(at), visible(at)
    for needle in ("무슨 일이 있었나", "원인 후보", "다음에 할 일", "보고서 글", "지표 추이", "크게 벗어남", "전환 추적 오류", "합성 예시 데이터 — 실제 광고 성과가 아닙니다",
                   "예시 실행: 문장은 템플릿이 만듭니다", pv.LIMIT_LINE, "하지 말아야 할 것", "근거 일치 점수는 원인이 맞을 확률이 아닙니다"):
        assert needle in t, needle
    assert "전환 추적 점검" in t and "예산 상향" in t                                       # 해야 할 것과 하지 말아야 할 것(예산 상향 금지)이 함께 보인다
    assert "이상 없음" not in v
    res = at.session_state["perf_result"]
    assert res["origin"] == "synthetic" and res["report"].data_origin == "synthetic" and res["report"].narrative_source == "template"


def test_uncertain_example_says_the_cause_is_hard_to_determine():
    at = run_example(fresh(), label_of("dev-ambiguous-1"))
    t = text(at)
    assert "원인을 정하기 어렵습니다" in t and "추가 자료가 필요합니다" in t and "확인하지 못한 것" in t
    assert "데이터를 고르고" not in t


@pytest.mark.parametrize("prefix", ["dev-creative_fatigue-1", "dev-budget_cap-1", "dev-landing_issue-2", "dev-bid_competition-2", "dev-audience_saturation-1", "dev-mix_shift-1"])
def test_other_examples_render_without_errors_and_with_evidence(prefix):
    at = run_example(fresh(), label_of(prefix))
    t, v = text(at), visible(at)
    assert "원인 후보" in t and "다음에 할 일" in t and "✓" in t
    assert not any(j in v for j in JARGON)


# ───────────── 이상 없음 (대조) ─────────────
def test_control_example_says_no_anomaly_and_explains_the_weekday_pattern():
    at = run_example(fresh(), label_of("dev-seasonal_dip-2"))
    t, v = text(at), visible(at)
    assert "이상 없음" in v and "눈에 띄는 이상은 없었습니다" in t and "같은 요일 패턴" in t
    assert "원인 후보" not in v and "설정을 바꾸지 말고" in t
    assert "크게 벗어남" not in t and "벗어남" not in v.replace("지표 추이", "")


def test_plain_control_example_also_shows_no_anomaly():
    at = run_example(fresh(), label_of("dev-no_event-1"))
    assert "이상 없음" in visible(at) and "원인 후보" not in visible(at)


# ───────────── CSV ─────────────
def csv_page(at: AppTest, body: str) -> AppTest:
    at.radio(key="perf_source").set_value("CSV 올리기").run()
    at.text_area(key="perf_csv_text").set_value(body).run()
    at.button(key="perf_run").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def good_csv(spec=None, mutate=None) -> str:
    rows = P.generate(spec or P.ScenarioSpec("csv", 77, events=(P.EventSpec("landing_issue", 30, None, {"depth": 0.45}),)))
    if mutate:
        rows = mutate(rows)
    return P.rows_to_csv(rows)


def test_user_csv_is_analysed_and_marked_as_my_data():
    at = csv_page(fresh(), good_csv())
    t = text(at)
    assert "내가 올린 데이터" in t and "합성 예시 데이터" not in t and "랜딩 페이지 문제" in t
    assert at.session_state["perf_result"]["origin"] == "user" and at.session_state["perf_result"]["report"].data_origin == "user"
    assert pv.LIMIT_LINE in t


def test_csv_template_and_help_are_offered():
    at = fresh()
    at.radio(key="perf_source").set_value("CSV 올리기").run()
    t = text(at)
    assert "필요한 열: 날짜, 노출수, 클릭수, 전환수, 광고비" in t and "20,000" in t and "2MB" in t
    assert any("양식" in str(getattr(b, "label", "")) for b in at.get("download_button")) or "CSV 양식 내려받기" in str(at.main)


def test_empty_csv_gives_a_friendly_error():
    at = csv_page(fresh(), "   ")
    errs = " ".join(e.value for e in at.error)
    assert "읽을 수 없었어요" in errs and "비어 있는 파일" in text(at)
    assert at.session_state.get("perf_result") is None


def test_missing_columns_are_named():
    at = csv_page(fresh(), "날짜,노출수,클릭수\n2026-10-01,1000,30\n")
    t = text(at)
    assert "필요한 열이 없어요" in t and "전환수" in t and "광고비" in t


def test_bad_rows_are_reported_with_line_numbers_and_the_good_rows_are_still_used():
    body = good_csv()
    lines = body.splitlines()
    lines[5] = lines[5].replace(lines[5].split(",")[6], "99999999")           # 클릭 > 노출
    lines[9] = "2026-13-45,c,s,k,d,1,1,1,1"
    at = csv_page(fresh(), "\n".join(lines) + "\n")
    t = text(at)
    assert "읽지 못한 줄이 2개" in t and "6번째 줄" in t and "10번째 줄" in t and "클릭수" in t and "날짜를 읽을 수 없어요" in t
    assert "무슨 일이 있었나" in t                                                   # 나머지 줄로 분석은 이어진다
    assert not any(w in t for w in ("Traceback", "ValueError", "KeyError"))


def test_formula_and_markup_cells_are_shown_as_plain_text():
    body = good_csv().replace(",iOS,", ',"=HYPERLINK(""http://evil.example"",""x"")",').replace("세트B_30대", "<img src=x onerror=alert(1)>")
    assert '"=HYPERLINK' in body and "<img src=x" in body                                 # 내려받은 파일이 아니라 사용자가 만든 원본 꼴
    at = csv_page(fresh(), body)
    raw = "\n".join(m.value for m in at.markdown)
    t = html.unescape(raw)
    assert not re.search(r"<img\b", raw) and "&lt;img" in raw                            # 태그로 해석될 글이 그대로 나가지 않고 글자로 나간다
    assert "'=HYPERLINK" in t                                                          # 앞에 작은따옴표가 붙은 글자 그대로
    assert not re.search(r"(?<![\w'])=HYPERLINK", t)                                   # 수식으로 시작하는 칸이 그대로 나가지 않는다
    assert any("수식처럼 보이는 칸" in i.value for i in at.info)
    assert "![" not in raw and "](" not in raw.replace("&#93;&#40;", "")


def test_oversized_csv_is_refused():
    big = "날짜,노출수,클릭수,전환수,광고비\n" + "2026-10-01,1000,30,3,15000\n" * 90_000
    at = csv_page(fresh(), big)
    assert "너무 커요" in text(at)


# ───────────── 모델 문장 / 표시 ─────────────
def test_without_keys_the_model_toggle_is_absent():
    at = fresh()
    assert not [c for c in at.checkbox if c.key == "perf_use_model"]


def test_with_keys_the_report_can_use_model_sentences_and_says_so():
    at = fresh(model=True)
    assert [c for c in at.checkbox if c.key == "perf_use_model"]
    at.checkbox(key="perf_use_model").check().run()
    run_example(at, label_of("dev-tracking_break-1"))
    t = text(at)
    assert "AI가 썼고, 코드가 숫자와 근거를 하나씩 대조했습니다" in t and "예시 실행" not in t
    rep = at.session_state["perf_result"]["report"]
    assert rep.narrative_source == "model" and rep.provider == "openai"


def test_with_keys_but_toggle_off_still_uses_the_template():
    at = fresh(model=True)
    run_example(at, label_of("dev-tracking_break-1"))
    assert at.session_state["perf_result"]["report"].narrative_source == "template" and "AI 호출 없음" in text(at)


def test_changing_the_selection_hides_the_stale_result():
    at = run_example(fresh(), label_of("dev-tracking_break-1"))
    assert "원인 후보" in text(at)
    at.selectbox(key="perf_example").set_value(label_of("dev-no_event-1")).run()
    t = text(at)
    assert "선택이 바뀌었어요" in t and "원인 후보" not in t


def test_dropped_model_sentences_are_listed_in_plain_words():
    def bad_model_app():
        import streamlit as st

        from core.config import Settings
        from core.insight_engine import template_narrative
        from llm.base import Task
        from llm.router import Router
        from observability.tracer import Tracer
        from tests.fakes import TaskScriptedProvider
        from ui import performance_view

        if "tracer" not in st.session_state:
            st.session_state.tracer = Tracer()

        def narr(req):
            n = template_narrative(req.context["analysis"])
            n["findings"].append({"text": "이 변화로 약 37% 손실이 났습니다.", "evidence_ids": n["findings"][1]["evidence_ids"]})
            return n

        def make_settings(a="", b="", **kw):
            return Settings.from_env(openai_api_key="k", anthropic_api_key="k", force_demo=False) if not kw.get("force_demo") else Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)

        def router_factory(settings):
            prov = TaskScriptedProvider("openai", {Task.INSIGHT: narr})
            return Router(settings, providers={"openai": prov, "anthropic": TaskScriptedProvider("anthropic")}, tracer=st.session_state.tracer)

        performance_view.render(make_settings, st.session_state.tracer, router_factory, {"openai": True, "anthropic": True, "run_budget_usd": 0.5})

    at = AppTest.from_function(bad_model_app, default_timeout=90).run()
    at.checkbox(key="perf_use_model").check().run()
    run_example(at, label_of("dev-landing_issue-2"))
    t = text(at)
    assert "근거 검사에서 뺀 문장 1개" in t and "근거에 없는 숫자가 들어간 문장" in t and "37%" in t
    assert "37%" not in "\n".join(m.value for m in at.markdown if "pf-perf-card" in m.value and "손실" in m.value and "근거에 없는" not in m.value)


# ───────────── 모양 ─────────────
def test_no_fixed_widths_or_nowrap_that_would_overflow_a_phone():
    at = run_example(fresh(), label_of("dev-creative_fatigue-1"))
    blob = "\n".join(m.value for m in at.markdown)
    assert not re.search(r"(?<!max-)width:\s*\d{3,}px|min-width|white-space:\s*nowrap|overflow-x:\s*scroll|position:\s*absolute", blob)
    assert "overflow-wrap:anywhere" in blob                                           # 긴 이름·문장은 줄바꿈된다


def test_chart_is_built_for_small_screens():
    from ui.performance_view import _chart
    from core.insight_engine import analyze

    a = analyze(P.generate(P.ScenarioSpec("chart", 78, events=(P.EventSpec("landing_issue", 30, None, {"depth": 0.45}),))))
    fig = _chart(a, "cvr")
    assert fig.layout.height <= 320 and fig.layout.width is None and fig.layout.legend.orientation == "h"
    assert len(fig.layout.shapes) >= 2                                                # 이상 구간 칸 + 살펴본 기간 경계선
    assert fig.layout.margin.l <= 8 and fig.layout.margin.r <= 8
    assert any(tr.name == "실제" for tr in fig.data) and any(tr.name == "평소 범위" for tr in fig.data)


def test_session_keys_use_the_perf_prefix():
    at = run_example(fresh(), label_of("dev-creative_fatigue-1"))
    mine = [k for k in at.session_state.keys() if not k.startswith("_") and k != "tracer"]
    assert mine and all(k.startswith("perf_") for k in mine), mine


def test_visible_text_has_no_internal_jargon():
    for prefix in ("dev-tracking_break-1", "dev-ambiguous-1", "dev-no_event-1", "dev-low_sample-1"):
        sid = next((n for n, s in pv.EXAMPLES if s.startswith(prefix)), None)
        if sid is None:
            continue
        at = run_example(fresh(), sid)
        v = visible(at)
        for j in JARGON:
            assert j not in v, (prefix, j)
        assert not re.search(r"\b(?:GRND|TA|FIN|INJ|CAT)-[A-Z0-9-]+", v)


def test_escape_helper_neutralizes_markup_and_markdown():
    s = pv.safe('<script>alert(1)</script> [x](http://e) ![i](y) *b* `c` & "q"')
    for ch in "<>[]()*`!":
        assert ch not in s
    assert "&lt;" in s and "&amp;" in s and "&quot;" in s
    assert html.unescape(s) == '<script>alert(1)</script> [x](http://e) ![i](y) *b* `c` & "q"'
