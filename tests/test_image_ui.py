"""소재 화면(ui/creative_view.py) 흐름 시험 (Streamlit AppTest, 네트워크 없음, 통제된 가짜 제공자).

- 배너 시안 3개가 보이고 점수·이유·내려받기가 있다. 넘침 경고가 필요한 긴 카피에서 경고가 뜬다.
- 잘못된 업로드는 쉬운 말로 거부된다. 읽지 못한 이미지는 점검 통과로 보이지 않는다.
- 세션 키는 img_ 로 시작하고, 처리 기록에는 해시·크기만 남고, 디스크에 쓰지 않는다. 읽은 글은 이스케이프해 보여 준다.
"""
from __future__ import annotations

import base64
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.test_image_common import mf, vision_out

PLAIN_BAD = ("fact_id", "finding", "Finding", "HUMAN_REVIEW", "APPROVE", "REVISE", "REJECT", "GRND-", "TA-", "FIN-", "IMG-", "오탐", "미탐", "Guardrail", "UNREADABLE")

STUDIO = '''
import streamlit as st
from ui import creative_view
creative_view.render_banner_studio(st.session_state["_t_copy"], st.session_state.get("_t_facts", []), "img_banner", categories=st.session_state.get("_t_cats", []))
'''

CHECK = '''
import streamlit as st
from core.config import Settings
from llm.base import Task
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import TaskScriptedProvider
from ui import creative_view

live = st.session_state.get("_t_live", False)


def make_settings(*keys, **over):
    return Settings.from_env(openai_api_key="sk-fake-not-a-real-key" if live else "", anthropic_api_key="", force_demo=not live)


def handler(req):
    st.session_state["_t_calls"] = st.session_state.get("_t_calls", 0) + 1
    return st.session_state["_t_payload"]


def router_factory(settings):
    if not live:
        return Router(settings, tracer=Tracer())      # 예시 실행: 기본 대역(mock)만 있다 — 이미지를 읽지 못한다
    return Router(settings, providers={"openai": TaskScriptedProvider("openai", {Task.VISION: handler})}, tracer=Tracer())


creative_view.render_image_check(make_settings, router_factory, {"openai": live, "anthropic": False, "run_budget_usd": st.session_state.get("_t_cap", 0.5)},
                                 facts=[], categories=st.session_state.get("_t_cats", []), key="img_check")
'''

SHORT = {"headline": "월 3만원부터 시작", "body": "자동이체로 편하게 모아 보세요", "cta": "지금 확인"}
TYPICAL = {"headline": "월 3만원부터 시작하는 생활비 통장", "body": "자동이체로 편하게 모아 보세요. 가입비와 유지 수수료가 없습니다.", "cta": "지금 확인", "disclosures": ["금리는 가입 시점 기준이며 변동될 수 있습니다."]}


def visible(at) -> str:
    """접힌 expander 안을 뺀, 기본 화면에 보이는 글자와 단추 이름."""
    out: list[str] = []

    def walk(node):
        kids = getattr(node, "children", None)
        if kids is None:
            val = getattr(node, "value", None)
            if isinstance(val, str):
                out.append(val)
            lab = getattr(node, "label", None)
            if isinstance(lab, str) and getattr(node, "type", "") in ("button", "download_button", "checkbox", "file_uploader", "text_area"):
                out.append(lab)
            return
        if getattr(node, "type", "") == "expander":
            out.append(getattr(node, "label", ""))
            return
        for k in kids.values():
            walk(k)

    walk(at.main)
    return "\n".join(out)


def everything(at) -> str:
    return "\n".join(str(getattr(e, "value", "")) for group in (at.markdown, at.caption, at.info, at.warning, at.error, at.success) for e in group)


def studio(copy, **state) -> AppTest:
    at = AppTest.from_string(STUDIO, default_timeout=60)
    at.session_state["_t_copy"] = copy
    for k, v in state.items():
        at.session_state[f"_t_{k}"] = v
    return at.run()


def check(**state) -> AppTest:
    at = AppTest.from_string(CHECK, default_timeout=60)
    for k, v in state.items():
        at.session_state[f"_t_{k}"] = v
    return at.run()


def upload(at: AppTest, name: str, data: bytes, mime: str) -> AppTest:
    at.file_uploader[0].set_value((name, data, mime))
    return at.run()


def badges(at) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m.value).strip() for m in at.markdown if "pf-badge" in m.value]


# --------------------------------------------------------------------- 배너 시안
def test_banner_studio_shows_three_banners_with_scores_reasons_and_downloads():
    at = studio(TYPICAL)
    assert not at.exception, [str(e.value) for e in at.exception]
    imgs = [m.value for m in at.markdown if "<img" in m.value and "data:image/svg+xml;base64," in m.value]
    assert len(imgs) == 3
    for html_ in imgs:
        svg = base64.b64decode(re.search(r"base64,([A-Za-z0-9+/=]+)", html_).group(1)).decode()
        assert ET.fromstring(svg).tag.endswith("svg") and "<script" not in svg and "max-width:100%" in html_      # 미리보기는 <img> 로만 — 스크립트가 돌 수 없고 가로 넘침이 없다
    text = everything(at)
    assert text.count("읽기 쉬움 점수") == 3 and "이 프로젝트가 정한 기준" in text
    dl = at.get("download_button")
    assert len(dl) == 3 and all(d.proto.label == "이 배너 SVG 내려받기" for d in dl)
    assert not at.warning, [w.value for w in at.warning]                                                              # 보통 길이의 문구는 경고가 없다
    assert any(e.label == "점수 자세히" for e in at.expander)


def test_banner_studio_accepts_an_adcopy_object_and_a_dict():
    from core.schemas import AdCopy

    assert not studio(AdCopy(**SHORT)).exception and not studio(SHORT).exception


def test_long_copy_shows_the_overflow_warning():
    long = {"headline": "아주 긴 제목 " * 12, "body": "끝없이 이어지는 본문 문장입니다 " * 120, "cta": "자세히 알아보기 지금 바로"}
    at = studio(long)
    assert not at.exception
    warns = [w.value for w in at.warning]
    assert any("세 시안 모두 글이 칸에 들어가지 않습니다" in w for w in warns) and any("칸에 들어가지 않아 잘려 보일 수 있습니다" in w for w in warns)


def test_hidden_disclosure_warns_and_the_toggle_works():
    at = studio(TYPICAL)
    at.checkbox(key="img_banner_disc").uncheck().run()
    warns = " ".join(w.value for w in at.warning)
    assert "필수 안내 문구가 배너에서 잘려 보일 수 있습니다" in warns and "실제 규정 해석이 아닙니다" in warns          # 이 프로젝트의 규칙이라고 밝힌다
    no_disc = studio(SHORT)
    assert at.checkbox(key="img_banner_disc").disabled is False and no_disc.checkbox(key="img_banner_disc").disabled is True


def test_banner_studio_text_is_plain_korean_and_session_keys_start_with_img():
    at = studio(TYPICAL)
    shown = visible(at)
    assert "배너 시안" in shown and all(j not in shown for j in PLAIN_BAD), [j for j in PLAIN_BAD if j in shown]
    assert [k for k in at.session_state.keys() if not k.startswith(("img_", "_t_"))] == []


def test_banner_studio_rechecks_the_banner_text_with_the_same_rules():
    at = studio({"headline": "원금 보장 통장", "body": "무조건 승인", "cta": "확인"}, cats=["savings"])
    assert not at.exception
    assert "게재 불가" in " ".join(badges(at))                                                                           # 배너 속 글도 같은 기준으로 걸린다
    clean = studio(SHORT)
    assert "점검 통과" in " ".join(badges(clean))


def test_empty_copy_is_handled_without_an_error():
    at = studio({"headline": "", "body": "", "cta": ""})
    assert not at.exception and any("배너에 넣을 글이 없습니다" in i.value for i in at.info)


# --------------------------------------------------------------------- 이미지 검사: 거부
def test_no_upload_shows_guidance_only():
    at = check()
    assert not at.exception and any("이미지를 올리면" in i.value for i in at.info) and "PNG · JPEG · GIF · WEBP(5MB 까지)와 SVG(1MB 까지)" in everything(at)


@pytest.mark.parametrize("name,data,mime,needle", [
    ("photo.jpg", None, "image/jpeg", "달라 받을 수 없습니다"),                      # 내용은 PNG, 이름은 jpg
    ("a.png", "polyglot_png_html.png", "image/png", "다른 데이터"),
    ("a.png", "corrupt_png_crc.png", "image/png", "손상"),
    ("a.png", "bomb_png_8192.png", "image/png", "너무 큽니다"),
    ("a.svg", "svg_entity_bomb.svg", "image/svg+xml", "DOCTYPE"),
    ("a.png", "text_file.png", "image/png", "지원하지 않는 형식"),
])
def test_bad_uploads_are_refused_in_plain_words(name, data, mime, needle):
    raw = mf.build("ok_png.png") if data is None else mf.build(data)
    at = upload(check(), name, raw, mime)
    assert not at.exception, [str(e.value) for e in at.exception]
    errs = " ".join(e.value for e in at.error)
    assert needle in errs and "Traceback" not in errs and not badges(at)               # 거부하면 판정을 내지 않는다
    log = at.session_state["img_check_log"]
    assert log and set(log[-1]) <= {"result", "reason", "bytes"} and log[-1]["result"] == "거부"        # 거부 기록에도 이름·바이트는 없다


def test_oversize_upload_is_refused_before_it_is_read():
    at = upload(check(), "big.png", mf.build("oversize_png.png"), "image/png")
    assert any("너무 큽니다" in e.value and "5MB" in e.value for e in at.error) and not badges(at)


# --------------------------------------------------------------------- 이미지 검사: 읽지 못함은 통과가 아니다
def test_demo_raster_cannot_be_read_and_never_looks_approved():
    at = upload(check(), "banner.png", mf.build("ok_banner_1200x628.png"), "image/png")
    assert not at.exception
    text = everything(at)
    assert "예시 실행에서는 이미지 속 글을 읽지 못합니다 — 키가 있을 때만 모델이 읽습니다" in text
    assert badges(at) == ["담당자 확인 필요"] and any("읽지 못해 통과로 처리하지 않습니다" in w.value for w in at.warning)
    assert "점검 통과" not in " ".join(badges(at)) and "모델 호출이 실패" not in everything(at)               # 대역이 읽지 못한 것이지 호출이 실패한 것이 아니다
    assert at.session_state["img_check_result"]["review"].provider == "mock"
    declared = at.text_area[0].set_value("월 3만원부터 시작하는 통장").run()
    assert badges(declared) == ["담당자 확인 필요"] and "이미지는 읽지 못했습니다" in everything(declared)           # 적어 준 글이 깨끗해도 통과가 아니다
    bad = at.text_area[0].set_value("원금 보장 통장").run()
    assert badges(bad) == ["게재 불가"]                                                                                # 적어 준 글이 규칙에 걸리면 반려는 된다
    shown = visible(at)
    assert all(j not in shown for j in PLAIN_BAD), [j for j in PLAIN_BAD if j in shown]


def test_svg_is_read_by_code_and_a_clean_svg_can_pass_but_a_forbidden_phrase_cannot():
    ok = upload(check(), "banner.svg", mf.build("ok_svg.svg"), "image/svg+xml")
    assert not ok.exception and badges(ok) == ["점검 통과"] and "글자 요소를 코드가 직접 읽었습니다" in everything(ok)
    bad = upload(check(cats=["savings"]), "banner.svg", mf.build("svg_guarantee.svg"), "image/svg+xml")
    assert badges(bad) == ["게재 불가"] and "원금 보장" in everything(bad)
    hidden = upload(check(cats=["savings"]), "banner.svg", mf.build("svg_hidden_desc.svg"), "image/svg+xml")
    assert badges(hidden) == ["게재 불가"]                                                                              # 화면에 안 보이는 desc 의 글도 읽는다
    partial = upload(check(), "banner.svg", mf.build("svg_embedded_raster.svg"), "image/svg+xml")
    assert badges(partial) == ["담당자 확인 필요"]                                                                      # 그림 속 글자는 못 읽으므로 통과 아님


def test_sanitized_svg_tells_the_user_what_was_removed():
    at = upload(check(), "x.svg", mf.build("svg_script.svg"), "image/svg+xml")
    assert any("안전을 위해 파일을 고쳤습니다" in i.value and "스크립트" in i.value for i in at.info) and badges(at) == ["점검 통과"]


def test_read_text_is_escaped_not_rendered_as_html():
    evil = '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="628"><text x="40" y="100" font-size="40" fill="#000">&lt;img src=x onerror=alert(1)&gt; &lt;b&gt;굵게&lt;/b&gt;</text></svg>'
    at = upload(check(), "e.svg", evil.encode(), "image/svg+xml")
    blob = "\n".join(m.value for m in at.markdown)
    assert "&lt;img src=x onerror=alert(1)&gt;" in blob and "<img src=x" not in blob and "<b>굵게" not in blob


# --------------------------------------------------------------------- 이미지 검사: 실제 모델 경로 (통제된 가짜 제공자)
def live_upload(payload, declared=None, **state):
    at = check(live=True, payload=payload, **state)
    at.file_uploader[0].set_value(("banner.png", mf.build("ok_banner_1200x628.png"), "image/png")).run()
    if declared is not None:
        at.text_area[0].set_value(declared).run()
    return at


def test_live_run_needs_consent_and_a_click_and_never_calls_the_model_before():
    at = live_upload(vision_out("월 3만원부터 시작하는 통장", "자동이체로 편하게"), declared="월 3만원부터 시작하는 통장 자동이체로 편하게")
    assert "_t_calls" not in at.session_state.keys() and not badges(at)                           # 올리기만 해서는 모델을 부르지 않는다
    btn = at.button(key="img_check_run")
    assert btn.disabled is True and "이미지가 모델 제공자" not in " ".join(w.value for w in at.error)          # 동의 전에는 단추가 잠겨 있다
    assert any("이미지가 모델 제공자(OpenAI 또는 Anthropic)에게 전송됩니다" in w.value for w in at.warning)
    at.checkbox(key="img_check_consent").check().run()
    assert at.button(key="img_check_run").disabled is False and "_t_calls" not in at.session_state.keys()
    at.button(key="img_check_run").click().run()
    assert not at.exception and at.session_state["_t_calls"] == 1
    assert badges(at) == ["점검 통과"] and "모델이 읽은 글과 직접 적어 주신 글이 일치합니다." in everything(at) and "모델이 이미지에서 읽은 글입니다" in everything(at)
    at.run()
    assert at.session_state["_t_calls"] == 1                                                      # 다시 그려도 모델을 또 부르지 않는다 (결과를 기억한다)


def test_live_model_only_reading_is_human_review_until_the_user_writes_the_text():
    at = live_upload(vision_out("월 3만원부터 시작하는 통장"))
    at.checkbox(key="img_check_consent").check().run()
    at.button(key="img_check_run").click().run()
    assert badges(at) == ["담당자 확인 필요"] and "직접 적어 대조하지 않으면" in everything(at)


def test_live_forbidden_text_and_visual_tags_and_failures():
    bad = live_upload(vision_out("월 3만원부터", "원금 보장"), declared="월 3만원부터", cats=["savings"])
    bad.checkbox(key="img_check_consent").check().run()
    bad.button(key="img_check_run").click().run()
    assert badges(bad) == ["게재 불가"] and "직접 적어 주신 글에는 없습니다" in everything(bad)

    tags = live_upload(vision_out("월 3만원부터", tags=["money_cash", "celebrity_face"]), declared="월 3만원부터")
    tags.checkbox(key="img_check_consent").check().run()
    tags.button(key="img_check_run").click().run()
    assert badges(tags) == ["담당자 확인 필요"] and "현금·돈다발" in everything(tags) and "코드로 확인할 수 없어" in everything(tags)     # 태그만으로는 게재 불가가 아니다

    unreadable = live_upload(vision_out(readable=False), declared="월 3만원부터")
    unreadable.checkbox(key="img_check_consent").check().run()
    unreadable.button(key="img_check_run").click().run()
    assert badges(unreadable) == ["담당자 확인 필요"] and any("읽지 못해 통과로 처리하지 않습니다" in w.value for w in unreadable.warning)


def test_zero_server_budget_blocks_model_reading_with_a_plain_message():
    at = live_upload(vision_out("월 3만원부터"), cap=0)
    assert any("비용 상한을 $0" in e.value for e in at.error) and at.button(key="img_check_run").disabled is True


# --------------------------------------------------------------------- 세션·기록·디스크
def test_session_keys_and_log_hold_only_hashes_and_sizes_and_nothing_is_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = {p for p in Path(tempfile.gettempdir()).glob("*")}
    raw = mf.build("ok_banner_1200x628.png")
    at = live_upload(vision_out("월 3만원부터 시작하는 통장"), declared="월 3만원부터 시작하는 통장")
    at.checkbox(key="img_check_consent").check().run()
    at.button(key="img_check_run").click().run()
    assert [k for k in at.session_state.keys() if not k.startswith(("img_", "_t_"))] == []
    log = at.session_state["img_check_log"]
    assert log and set(log[-1]) >= {"sha256", "bytes", "decision"} and "name" not in log[-1]
    blob = repr(log) + repr({k: at.session_state[k] for k in at.session_state.keys() if k.startswith("img_") and k != "img_check_upload"})
    assert raw[:16].hex() not in blob and "banner.png" not in repr(log)
    assert list(tmp_path.iterdir()) == []                                                           # 작업 폴더에 아무것도 쓰지 않았다
    assert {p for p in Path(tempfile.gettempdir()).glob("*") if "banner" in p.name or p.suffix in (".png", ".svg")} <= {p for p in before}


def test_only_the_result_is_cached_not_the_image_bytes():
    at = upload(check(), "banner.png", mf.build("ok_banner_1200x628.png"), "image/png")
    cache = at.session_state["img_check_result"]
    assert set(cache) == {"ck", "review"} and not any(isinstance(v, (bytes, bytearray)) for v in vars(cache["review"]).values())
