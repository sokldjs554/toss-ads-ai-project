"""저장된 검사 사례·정적 재생 페이지가 '저장된 최종 수정안 상태' 를 그대로 보여 주는지.

리뷰에서 확인된 문제: 새 실행 경로는 최종 수정안 상태를 쓰지만, 저장된 기록을 읽는 화면은 코드 검사 결과(rewrite_recheck.decision)만 보고
수정안을 '통과' 로 표시했다. 재현: traces/sample_partial_malicious.json 의 광고안 3 ('당신의 금융 파트너') — 코드 검사 APPROVE,
의미 검사 REVISE, 최종 failed_recheck 인데 '검사를 통과한 수정 제안' 으로 보였다. sample_partial_injection.json 의 광고안 3 도 같았다.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from guardrails.policy import get_catalog
from observability import replay
from ui.presets import PRESETS, build_brief

ROOT = Path(__file__).resolve().parents[1]
CAT = get_catalog()
KEYS = ("loan_ok", "malicious", "injection", "prohibited", "commerce_landing")


def _walk(n):
    yield n
    for c in n.get("children", []):
        yield from _walk(c)


def _load(key):
    return json.loads((ROOT / "traces" / f"sample_partial_{key}.json").read_text(encoding="utf-8"))


def _stored_status(trace):
    """실행 당시 판정 단계가 저장한 값 — 기준이 되는 원본."""
    return [s["attributes"].get("rewrite_status") for s in _walk(trace["root"]) if s["name"] == "adjudicate"]


def _summary(key, trace):
    p = PRESETS[key]
    brief = build_brief(p)
    cats = CAT.effective_categories(brief.product.category, p.get("description", ""))
    return replay.summarize_trace(trace, brief.product, cats, p.get("landing_text", ""), CAT)


@pytest.mark.parametrize("key", KEYS)
def test_cases_view_summary_uses_the_stored_final_status(key):
    trace = _load(key)
    stored = _stored_status(trace)
    summary = _summary(key, trace)
    assert [v["rewrite_status"] for v in summary["variants"]] == stored
    assert all(v["rewrite_state"]["source"] == "stored" for v in summary["variants"])


def test_reported_cases_are_not_shown_as_verified():
    """리뷰 재현 사례: 코드 검사는 통과했지만 의미 검사에서 걸린 수정안."""
    for key, idx in (("malicious", 2), ("injection", 2)):
        trace = _load(key)
        v = _summary(key, trace)["variants"][idx]
        assert v["rewrite_status"] == "failed_recheck", (key, v["rewrite_status"])
        rc = v["rewrite_state"]["recheck"]
        assert rc["det_decision"] == "APPROVE" and rc["semantic_decision"] == "REVISE"  # 이 근거로는 통과를 다시 계산할 수 없다
    assert _summary("malicious", _load("malicious"))["variants"][2]["rewrite"]["headline"] == "당신의 금융 파트너"


def test_records_without_a_stored_final_status_are_unknown_and_the_original_is_untouched():
    trace = _load("malicious")
    legacy = copy.deepcopy(trace)
    for s in _walk(legacy["root"]):
        if s["name"] == "adjudicate":
            s["attributes"].pop("rewrite_status", None)
            s["attributes"].pop("rewrite_note", None)
    before = json.dumps(legacy, sort_keys=True)
    variants = _summary("malicious", legacy)["variants"]
    assert variants[2]["rewrite_status"] == "unchecked" and variants[2]["rewrite_state"]["source"] == "legacy_unknown"
    assert "확인하지 못했습니다" in variants[2]["rewrite_state"]["note"]
    # 코드 검사만 통과한 과거 기록을 '검증됨' 으로 만들지 않는다. 수정안 자체가 없는 광고안은 그대로 '없음'.
    assert not any(v["rewrite_status"] == "verified" for v in variants)
    assert [v["rewrite_status"] for v in variants][:2] == ["none", "none"]
    assert json.dumps(legacy, sort_keys=True) == before  # 읽기 전용: 과거 기록을 고쳐 성공한 실행처럼 만들지 않는다
    on_disk = _load("malicious")
    assert on_disk == trace


@pytest.fixture(scope="module")
def static_page(tmp_path_factory):
    out = tmp_path_factory.mktemp("replay")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_replay.py"), "--out", str(out)], check=True, capture_output=True, cwd=ROOT)
    html = (out / "index.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1).replace("<\\/", "</"))
    return data, html


@pytest.mark.parametrize("key", KEYS)
def test_static_page_matches_the_stored_status(static_page, key):
    data, _ = static_page
    sc = next(s for s in data["scenarios"] if s["key"] == key)
    assert [v["rewrite_status"] for v in sc["variants"]] == [x or "none" for x in _stored_status(_load(key))][: len(sc["variants"])]
    assert all("rewrite_shown" not in v for v in sc["variants"])  # 코드 검사만 보고 계산하던 필드가 다시 생기지 않게


def test_static_page_and_cases_view_agree_on_every_variant(static_page):
    data, html = static_page
    for key in KEYS:
        sc = next(s for s in data["scenarios"] if s["key"] == key)
        cases = _summary(key, _load(key))["variants"]
        assert [v["rewrite_status"] for v in sc["variants"]] == [v["rewrite_status"] for v in cases], key
    assert "rewrite_shown" not in html  # 템플릿도 저장된 상태(rewrite_status)만 쓴다


def test_cases_screen_shows_confirmed_rewrites_only_for_stored_verified_status(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    at.session_state["nav_request"] = "검사 사례"
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    texts = [m.value for m in at.markdown]
    shown_verified = sum("수정안 확인 완료 · 저장된 실행 결과 기준" in t for t in texts)
    failed = sum("다시 수정 필요 · 저장된 실행 결과 기준" in t for t in texts)
    stored = [x for k in KEYS for x in _stored_status(_load(k))]
    assert shown_verified == stored.count("verified") and failed == stored.count("failed_recheck")
    # 확인 완료가 아닌 수정안은 '수정안 확인 완료' 카드로 그려지지 않는다
    cards = sum("수정안 확인 완료" in t and "pf-ad" in t for t in texts)
    assert cards <= stored.count("verified")
