"""검수 대기열 화면 (AppTest, 네트워크·모델 호출 없음, 예시 실행 결과만 사용).

- 대기열이 비었을 때 안내, 라벨러가 한 명일 때 '일치도를 잴 수 없습니다', 합성 예시 → 일치도 표시
- 인용이 문구에 없으면 거부 메시지, 올바른 라벨은 저장
- 두 라벨러가 갈리면 조정 → 평가셋 후보 내보내기
- 전문 용어·규칙 코드가 화면에 없다, 세션 키는 rq_ 로 시작한다, 가로 넘침을 부르는 마크업이 없다, 예외가 없다
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from streamlit.testing.v1 import AppTest

from agents.pipeline import RunOptions, run_pipeline
from core.config import Settings
from core.schemas import AdCopy
from observability.tracer import Tracer
from ui import review_queue_view as rqv
from ui.presets import PRESETS, build_brief

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from ui import theme, review_queue_view
theme.inject()
review_queue_view.render(st.session_state.get("runs", []))
'''
JARGON = ("오탐", "미탐", "fact_id", "finding", "Finding", "Grounding", "Guardrail", "HUMAN_REVIEW", "APPROVE", "REVISE", "REJECT", "GRND-", "TA-", "FIN-", "INJ-", "CAT-", "가장 중요한 이유")


@lru_cache(maxsize=1)
def _run():
    s = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True, auto_decision_confidence=0.99)      # 확신 기준을 높여 담당자 확인 건이 나오게 한다
    return run_pipeline(build_brief(PRESETS["loan_ok"]), s, tracer=Tracer(), options=RunOptions(n_variants=3))


def app(runs=None) -> AppTest:
    at = AppTest.from_string(SCRIPT, default_timeout=90)
    at.session_state["runs"] = runs if runs is not None else []
    return at.run()


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info] + [w.value for w in at.warning] + [e.value for e in at.error] + [s.value for s in at.success]
    return "\n".join(str(p) for p in parts)


def ok(at: AppTest) -> None:
    assert not at.exception, [str(e.value) for e in at.exception]


def nonce(at: AppTest) -> int:
    return at.session_state["rq_nonce"] if "rq_nonce" in at.session_state else 0


def queue_of(at: AppTest):
    return at.session_state["rq_queue"]


def enqueue(at: AppTest) -> AppTest:
    at.button(key="rq_enqueue").click().run()
    ok(at)
    return at


def claim_as(at: AppTest, name: str) -> AppTest:
    at.text_input(key="rq_annotator").set_value(name).run()
    at.button(key="rq_claim").click().run()
    ok(at)
    return at


def submit(at: AppTest, decision: str, *, rules=(), spans=(), free="", why="화면 테스트의 이유") -> AppTest:
    """폼 값을 채우고 제출한다. 폼 안의 값은 제출 버튼을 누르는 같은 실행에서 함께 전달된다."""
    n = nonce(at)
    at.radio(key=f"rq_decision_{n}").set_value(decision)
    at.multiselect(key=f"rq_rules_{n}").set_value(list(rules))              # 이전 시도의 선택이 남아 있을 수 있어 항상 덮어쓴다
    at.multiselect(key=f"rq_spans_{n}").set_value(list(spans))
    at.text_input(key=f"rq_span_free_{n}").set_value(free)          # 이전 시도의 값이 남아 있을 수 있어 항상 덮어쓴다
    at.text_area(key=f"rq_why_{n}").set_value(why)
    at.button(key="rq_submit").click().run()
    ok(at)
    return at


def a_valid_span(at: AppTest) -> str:
    return queue_of(at).items()[0].ad_copy.headline


def rule_option(at: AppTest, title: str) -> str:
    return next(o for o in at.multiselect(key=f"rq_rules_{nonce(at)}").options if o == title)


# ═════════════════════════ 비었을 때 · 한 명일 때 ═════════════════════════
def test_empty_queue_shows_guidance_and_no_exception():
    at = app()
    ok(at)
    t = texts(at)
    assert "대기열이 비었습니다" in t and "아직 광고 작업의 결과가 없습니다" in t
    assert "사람 라벨러가 한 명뿐입니다" in t and "독립 라벨러가 확보됐다는 뜻이 아닙니다" in t
    assert not any(b.key == "rq_claim" for b in at.button) and "라벨러 사이 일치도" not in t


def test_a_run_with_waiting_ads_offers_to_send_them_and_never_twice():
    at = app([{"run": _run(), "landing": ""}])
    btn = at.button(key="rq_enqueue")
    n_hr = sum(1 for r in _run().reports if r.final_decision.value == "HUMAN_REVIEW")
    assert str(n_hr) in btn.label and not btn.disabled
    enqueue(at)
    assert len(queue_of(at)) == n_hr and any(f"{n_hr}건을 대기열에 담았습니다" in s.value for s in at.success)
    assert at.button(key="rq_enqueue").disabled and "새로 보낼 담당자 확인 광고가 없습니다" in at.button(key="rq_enqueue").label
    assert len(at.dataframe) >= 1                                              # 대기열 목록 (우선순위·사유·마감)
    df = at.dataframe[0].value
    assert list(df.columns)[:4] == ["순서", "우선순위", "사유", "마감"] and len(df) == n_hr


def test_entries_accept_plain_runs_callables_and_ignore_unusable_sources():
    run = _run()
    assert [r for r, _ in rqv._entries([run])] == [run]
    assert [r for r, _ in rqv._entries(lambda: [{"run": run, "landing": "랜딩"}])] == [run] and rqv._entries(lambda: [{"run": run, "landing": "랜딩"}])[0][1] == "랜딩"
    assert rqv._entries(Tracer()) == [] and rqv._entries(None) == [] and rqv._entries(lambda: 1 / 0) == [] and rqv._entries([{"x": 1}, 5]) == []


def test_a_single_labeler_cannot_measure_agreement():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    submit(at, "게재 불가", rules=["무조건·무심사·누구나 승인"], spans=[a_valid_span(at)])
    t = texts(at)
    assert "라벨을 저장했습니다" in t and "라벨러가 한 명이라 일치도를 잴 수 없습니다" in t
    assert len(queue_of(at).annotations()) == 1


# ═════════════════════════ 라벨 입력 ═════════════════════════
def test_a_citation_that_is_not_in_the_copy_is_refused_with_a_message_and_nothing_is_saved():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    submit(at, "게재 불가", rules=["무조건·무심사·누구나 승인"], free="광고에 없는 구절입니다")
    msgs = [e.value for e in at.error]
    assert any("광고 문구에 그대로 없어 저장하지 않았습니다" in m and "광고에 없는 구절입니다" in m for m in msgs), msgs
    assert queue_of(at).annotations() == [] and queue_of(at).held_by("가나")          # 점유는 유지돼 고쳐서 다시 낼 수 있다
    # 같은 폼에서 인용을 고쳐 다시 내면 저장된다
    submit(at, "게재 불가", rules=["무조건·무심사·누구나 승인"], spans=[a_valid_span(at)])
    assert len(queue_of(at).annotations()) == 1


def test_inconsistent_labels_are_refused_in_plain_words():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    submit(at, "게재 불가", spans=[a_valid_span(at)])                                       # 규칙 없음
    assert any("규칙을 하나 이상 골라야 합니다" in e.value for e in at.error)
    submit(at, "점검 통과", rules=["무조건·무심사·누구나 승인"])                                 # 통과 + 게재 불가 등급 규칙
    assert any("게재 불가 등급 규칙을 골랐습니다" in e.value for e in at.error)
    submit(at, "수정 필요", rules=["무조건·무심사·누구나 승인"])                                 # 인용 없음
    assert any("구절을 광고 문구에서 하나 이상 인용해야 합니다" in e.value for e in at.error)
    assert queue_of(at).annotations() == []


def test_submitting_without_choosing_a_decision_is_refused():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    at.button(key="rq_submit").click().run()
    ok(at)
    assert any("판정을 먼저 골라 주세요" in e.value for e in at.error) and queue_of(at).annotations() == []


def test_the_automatic_suggestion_is_hidden_by_default_and_the_choice_is_recorded():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    assert "자동 검사의 제안:" not in texts(at) and "자동 검사의 제안은 숨겨져 있습니다" in texts(at)
    assert at.toggle(key="rq_hide_prelabel").value is True
    at.toggle(key="rq_hide_prelabel").set_value(False).run()
    ok(at)
    assert "자동 검사의 제안:" in texts(at) and "확정이 아닙니다" in texts(at)
    submit(at, "수정 필요", rules=["무조건·무심사·누구나 승인"], spans=[a_valid_span(at)])
    a = queue_of(at).annotations()[0]
    assert a.prelabel_mode == "shown" and a.prelabel_decision is not None


def test_a_quality_check_item_is_indistinguishable_on_screen():
    """검증용 항목을 받은 라벨러 화면에는 일반 항목과 다른 글이 없다 — '검증용'·'코드 검사만'·데이터셋 id 가 나오지 않는다."""
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    at.button(key="rq_add_gold").click().run()
    ok(at)
    q = queue_of(at)
    assert q.gold_pool_size == rqv.GOLD_COUNT
    q.gold_rate = 1.0                                                      # 이번 배정은 검증용 항목이 나오게 한다 (관리자 설정)
    claim_as(at, "가나")
    held = q.get(q.held_by("가나")[0])
    assert held.is_gold
    shown = "\n".join(m.value for m in at.markdown if "pf-ad" in m.value or "pf-stat" in m.value) + "\n" + "\n".join(c.value for c in at.caption)
    for leak in ("코드 검사만", held.source.dataset_id, "gold", "G-0"):
        assert leak not in shown, leak
    assert "자동 검사의 제안은 숨겨져 있습니다" in shown                      # 일반 항목과 같은 안내
    # 라벨을 내면 라벨러 품질 표에 검증용 정확도가 잡힌다 (화면의 표 값은 큐가 계산)
    ad = held.ad_copy
    truth = held.gold.decision.value
    label_ko = {"APPROVE": "점검 통과", "REVISE": "수정 필요", "REJECT": "게재 불가"}[truth]
    rules = [] if truth == "APPROVE" else [at.multiselect(key=f"rq_rules_{nonce(at)}").options[0]]
    submit(at, label_ko, rules=rules, spans=[] if truth == "APPROVE" else [ad.headline], why="검증용 항목에 대한 이유를 충분히 적습니다")
    assert [x for x in q.quality() if x.annotator == "가나"][0].n_gold == 1


NAV_SCRIPT = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
from ui import theme, review_queue_view
theme.inject()
if st.session_state.get("nav", "queue") == "queue":
    review_queue_view.render(st.session_state.get("runs", []))
else:
    st.markdown("다른 화면")
'''


def test_settings_survive_a_trip_to_another_view():
    """다른 화면에 다녀오면 Streamlit 이 위젯 상태를 지운다 — 라벨러 이름·자동 제안 숨김·보기 범위·필요한 라벨 수는 그대로 돌아와야 한다."""
    at = AppTest.from_string(NAV_SCRIPT, default_timeout=90)
    at.session_state["runs"] = [{"run": _run(), "landing": ""}]
    at.run()
    at.radio(key="rq_required").set_value(1).run()
    at.text_input(key="rq_annotator").set_value("가나").run()
    at.toggle(key="rq_hide_prelabel").set_value(False).run()
    at.session_state["nav"] = "other"
    at.run()
    assert "다른 화면" in texts(at) and not any(t.key == "rq_annotator" for t in at.text_input)
    at.session_state["nav"] = "queue"
    at.run()
    ok(at)
    assert at.text_input(key="rq_annotator").value == "가나" and at.toggle(key="rq_hide_prelabel").value is False and at.radio(key="rq_required").value == 1


def test_the_demo_scope_and_the_queue_survive_a_trip_to_another_view():
    at = AppTest.from_string(NAV_SCRIPT, default_timeout=90)
    at.session_state["runs"] = [{"run": _run(), "landing": ""}]
    at.run()
    enqueue(at)
    n = len(queue_of(at))
    at.radio(key="rq_scope").set_value("합성 예시").run()
    at.button(key="rq_load_demo").click().run()
    at.session_state["nav"] = "other"
    at.run()
    at.session_state["nav"] = "queue"
    at.run()
    ok(at)
    assert at.radio(key="rq_scope").value == "합성 예시" and "라벨러 사이 일치도" in texts(at)            # 불러 둔 합성 예시가 그대로 있다
    at.radio(key="rq_scope").set_value("내 대기열").run()
    assert len(queue_of(at)) == n                                                                   # 내 대기열도 그대로 있다


def test_release_gives_the_ad_back():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    assert queue_of(at).held_by("가나")
    at.button(key="rq_release").click().run()
    ok(at)
    assert queue_of(at).held_by("가나") == [] and any(b.key == "rq_claim" for b in at.button)


def test_claiming_with_nothing_left_says_so():
    at = app()
    at.text_input(key="rq_annotator").set_value("가나").run()
    at.button(key="rq_claim").click().run()
    ok(at)
    assert "지금 받을 수 있는 광고가 없습니다" in texts(at)


def test_the_displayed_ad_text_is_escaped():
    from core.schemas import AdCopy as _AdCopy

    q_at = app([{"run": _run(), "landing": ""}])
    enqueue(q_at)
    it = queue_of(q_at).items()[0]
    it.ad_copy = _AdCopy(variant_id="x", headline="<script>alert(1)</script> 원금 보장", body="<img src=x onerror=alert(1)>", cta="신청")
    claim_as(q_at, "가나")
    html_blocks = [m.value for m in q_at.markdown if "pf-ad" in m.value]
    assert html_blocks and all("<script>" not in b and "<img" not in b for b in html_blocks)
    assert "&lt;script&gt;" in "".join(html_blocks)


# ═════════════════════════ 두 라벨러 · 조정 · 내보내기 ═════════════════════════
def adjudicate_picked(at: AppTest, name: str) -> None:
    """조정 폼의 첫 항목을 게재 불가로 조정한다."""
    it = queue_of(at).get(at.selectbox(key="rq_adj_pick").value)
    at.text_input(key="rq_adj_name").set_value(name)
    at.radio(key="rq_adj_decision").set_value("게재 불가")
    at.multiselect(key="rq_adj_rules").set_value(["무조건·무심사·누구나 승인"])
    at.multiselect(key="rq_adj_spans").set_value([it.ad_copy.headline])
    at.text_area(key="rq_adj_reason").set_value("광고 문구의 단정 표현은 게재 불가 등급이다")
    at.button(key="rq_adj_submit").click().run()
    ok(at)


def test_two_labelers_who_disagree_get_an_adjudication_step_then_an_export():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    q = queue_of(at)
    n_hr = len(q)
    # 두 사람이 모든 광고를 라벨한다: 가나는 게재 불가, 나다는 점검 통과 → 모든 광고에서 갈림
    for _ in range(n_hr):
        claim_as(at, "가나")
        submit(at, "게재 불가", rules=["무조건·무심사·누구나 승인"], spans=[q.get(q.held_by("가나")[0]).ad_copy.headline])
    for _ in range(n_hr):
        claim_as(at, "나다")
        submit(at, "점검 통과")
    t = texts(at)
    assert "의견이 갈린 광고 조정" in t and "라벨러 사이 일치도" in t
    assert len(q.disagreements()) == n_hr and all(len(i.annotations) == 2 for i in q.items())
    adjudicate_picked(at, "가나")                                      # 그 광고를 라벨한 사람은 조정할 수 없다
    assert any("스스로 조정할 수 없습니다" in e.value for e in at.error) and len(q.disagreements()) == n_hr
    for _ in range(n_hr):
        adjudicate_picked(at, "제3자")
    assert q.disagreements() == [] and all(i.state.value == "adjudicated" for i in q.items())
    assert "의견이 갈린 광고 조정" not in texts(at)
    # 일치도: 두 사람이 모든 광고에서 갈렸다
    assert any("판정이 같은 비율" in m.value and "0%" in m.value for m in at.markdown)
    assert any("표본 부족" in w.value for w in at.warning)
    # 내보내기
    at.button(key="rq_export_make").click().run()
    ok(at)
    assert any(f"평가셋 후보 {n_hr}건" in s.value for s in at.success), [s.value for s in at.success]
    assert len(at.get("download_button")) >= 1


def test_export_with_nothing_confirmed_explains_why():
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    at.button(key="rq_export_make").click().run()
    ok(at)
    assert any("내보낼 수 있는 확정된 라벨이 아직 없습니다" in i.value for i in at.info)


def test_one_label_mode_requires_an_explicit_checkbox_to_export():
    at = app([{"run": _run(), "landing": ""}])
    at.radio(key="rq_required").set_value(1).run()
    ok(at)
    enqueue(at)
    assert queue_of(at).required_labels == 1
    assert at.radio(key="rq_required").disabled                                  # 대기열에 광고가 생기면 바꿀 수 없다
    claim_as(at, "가나")
    submit(at, "게재 불가", rules=["무조건·무심사·누구나 승인"], spans=[a_valid_span(at)])
    at.button(key="rq_export_make").click().run()
    assert any("내보낼 수 있는 확정된 라벨이 아직 없습니다" in i.value for i in at.info)
    at.checkbox(key="rq_export_single").check().run()
    at.button(key="rq_export_make").click().run()
    ok(at)
    assert any("라벨러가 한 명인 행이 있습니다" in w.value for w in at.warning)


# ═════════════════════════ 합성 예시 ═════════════════════════
def test_the_synthetic_example_shows_agreement_and_says_it_is_not_real():
    at = app()
    at.radio(key="rq_scope").set_value("합성 예시").run()
    ok(at)
    assert "합성 예시입니다" in texts(at) and any(b.key == "rq_load_demo" for b in at.button)
    at.button(key="rq_load_demo").click().run()
    ok(at)
    t = texts(at)
    assert "라벨러 사이 일치도" in t and "합성 예시의 값입니다" in t and "표본 부족" in t
    stats = "\n".join(m.value for m in at.markdown if "pf-stat" in m.value)
    assert "75%" in stats and "0.62" in stats and "0.36 ~ 0.84" in stats and "함께 라벨한 광고 28건" in stats
    assert any("판정 혼동 표" in m.value for m in at.markdown)
    assert at.session_state["rq_demo_queue"].stats().by_state["adjudicated"] == 7
    assert len(queue_of(at)) == 0 if "rq_queue" in at.session_state else True            # 합성 예시는 내 대기열에 섞이지 않는다


def test_the_synthetic_export_is_marked_and_warned():
    at = app()
    at.radio(key="rq_scope").set_value("합성 예시").run()
    at.button(key="rq_load_demo").click().run()
    at.button(key="rq_demo_export_make").click().run()
    ok(at)
    assert any("평가셋 후보 28건" in s.value for s in at.success)
    assert any("합성 예시가 포함돼 있습니다" in w.value and "합치지 마세요" in w.value for w in at.warning)
    res = at.session_state["rq_demo_export_result"]["res"]
    assert all(r["synthetic"] and r["source"] == "labeling_synthetic_demo" for r in res.rows)


def test_leak_warnings_are_shown_in_plain_words_when_a_split_straddles_near_duplicates():
    from ops.dataset_tools import validate_dataset

    def row(i, h, split):
        return {"id": f"R-{i}", "category": "loan", "headline": h, "body": "서류 없이 모바일로 5분이면 입금되는 대출을 지금 바로 신청하세요", "cta": "신청", "label": "REJECT", "rule_ids": ["FIN-APPROVAL-01"],
                "disclosures": [], "facts": [], "claims": [], "landing_text": "", "difficulty": "clear", "notes": "", "source": "x", "split": split}

    v = validate_dataset([row(1, "누구나 무조건 당일 승인 받는 신용대출", "dev"), row(2, "누구나 무조건 당일 승인 받는 신용대출!", "holdout")])
    [s] = rqv._leak_sentences(v)
    assert "개발용" in s and "최종 확인용" in s and "영향을 받을 수 있습니다" in s and "누수" not in s


# ═════════════════════════ 화면 계약 ═════════════════════════
def full_flow_text() -> tuple[AppTest, str]:
    at = app([{"run": _run(), "landing": ""}])
    enqueue(at)
    claim_as(at, "가나")
    t1 = texts(at)
    at.radio(key="rq_scope").set_value("합성 예시").run()
    at.button(key="rq_load_demo").click().run()
    return at, t1 + "\n" + texts(at)


def test_no_jargon_or_rule_codes_on_screen():
    at, t = full_flow_text()
    ok(at)
    for word in JARGON:
        assert word not in t, word


def test_every_session_key_starts_with_rq_and_nothing_else_is_added():
    at, _ = full_flow_text()
    own = {k for k in at.session_state.keys() if k != "runs"}
    assert own and all(k.startswith("rq_") for k in own), sorted(k for k in own if not k.startswith("rq_"))


def _walk(node):
    yield node
    kids = getattr(node, "children", None)
    if kids:
        for k in kids.values():
            yield from _walk(k)


def test_no_markup_that_would_overflow_a_phone_screen():
    """390px 폭을 가정한 정적 점검: 고정 폭(px)·nowrap·pre 가 없고, 가로 나란히 놓는 열은 3개 이하."""
    at, _ = full_flow_text()
    html = "\n".join(m.value for m in at.markdown if "<" in m.value and "<style>" not in m.value)
    assert html and not re.search(r"(?<!max-)width\s*:\s*\d{3,}px", html) and "min-width" not in html and "nowrap" not in html and "<pre" not in html and "<table" not in html
    for node in _walk(at.main):
        if getattr(node, "type", "") == "horizontal":
            assert len(node.children) <= 3, len(node.children)


def test_screen_signature_and_the_module_has_one_public_entry_point():
    import inspect

    assert str(inspect.signature(rqv.render)) == "(source: 'Any' = None, *, catalog: 'Optional[PolicyCatalog]' = None) -> 'None'"
    assert callable(rqv.render)


def test_helpers_for_candidates_and_times():
    copy = AdCopy(headline="최대 한도 5,000만원, 당일 입금", body="심사 결과에 따라 다름. 조건 확인", cta="신청", disclosures=["연 5.5%~18.0% (심사 결과에 따라 다름)"])
    c = rqv.span_candidates(copy)
    assert all(x in copy.full_text() for x in c) and "최대 한도 5,000만원" in c and "당일 입금" in c
    assert not any(x.endswith("5") or x.startswith("000") for x in c)                    # 숫자 사이의 쉼표에서 자르지 않는다
    assert rqv.span_candidates(copy, ["없는 구절"]) == c                                  # 문구에 없는 제안은 후보에 올리지 않는다
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert rqv.remaining_text(now - timedelta(minutes=1), now) == "마감 지남" and rqv.remaining_text(now + timedelta(minutes=30), now) == "30분 남음"
    assert rqv.remaining_text(now + timedelta(hours=5), now) == "5시간 남음"
    assert rqv.priority_text(0.7).startswith("높음") and rqv.priority_text(0.4).startswith("보통") and rqv.priority_text(0.1).startswith("낮음")
    assert rqv.md_escape("<b>*x*</b>") == "&lt;b&gt;\\*x\\*&lt;/b&gt;"
