"""상품 URL 이해 평가 — 합성 페이지 묶음의 짜임, 지표 계산, 게이트가 실제로 막는지.

게이트 테스트는 '일부러 새는 읽기 함수' 를 넣어 보고, 새면 실패하는 것을 확인한다 (게이트가 늘 통과하는 장식이 아님을 보인다).
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest

from core import url_ingest as ui
from evals import url_ingest_eval as ev

PAGES = ui.PAGES_DIR


@pytest.fixture(scope="module")
def report() -> dict:
    return ev.evaluate()


def idx() -> dict:
    return json.loads((PAGES / "index.json").read_text(encoding="utf-8"))


# --- 묶음의 짜임 ---------------------------------------------------------------------------
def test_page_set_is_big_enough_and_every_file_is_listed_once():
    pages = idx()["pages"]
    ids = [p["id"] for p in pages]
    assert len(ids) == len(set(ids))
    assert sum(p["split"] == "dev" for p in pages) >= 16 and sum(p["split"] == "holdout" for p in pages) >= 8
    on_disk = {f.stem for f in PAGES.glob("*.html")}
    assert on_disk == set(ids)


def test_every_page_entry_has_the_documented_fields_and_plain_korean_label():
    for p in idx()["pages"]:
        assert p["status"] in ("ok", "ambiguous", "unreadable") and p["split"] in ("dev", "holdout")
        for key in ("expected_name", "expected_price", "expected_facts", "acceptable_facts", "forbidden", "flags", "facts_min", "label"):
            assert key in p, (p["id"], key)
        assert {"injection_suspected", "hidden_text_found", "price_conflict", "truncated", "ambiguous_product", "unreadable", "low_content"} >= set(p["flags"])
        assert re.search(r"[가-힣]", p["label"]) and not re.search(r"JSON-LD|OG\b|SPA|HTML|SSRF", p["label"]), p["label"]
        if p["status"] != "ok":
            assert not p["expected_facts"] and not p["expected_name"] and p["expected_price"] is None


def test_the_required_page_kinds_are_all_present():
    ids = " ".join(p["id"] for p in idx()["pages"])
    for kind in ("jsonld_clean", "og_only", "body_only", "euckr", "price_conflict", "hidden_display_none", "visible_injection", "comment_injection", "giant", "broken_html",
                 "spa_shell", "blog", "multi_products", "login_wall", "hanja", "coupon"):
        assert kind in ids, kind


def test_forbidden_phrases_really_are_in_the_page_source_so_a_zero_leak_is_not_trivial():
    for p in idx()["pages"]:
        raw = (PAGES / f"{p['id']}.html").read_bytes()
        text = raw.decode("utf-8") if p["id"] != "p04_euckr" and p["id"] != "h07_euckr_detail_strike" else raw.decode("cp949")
        blob = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()
        for phrase in [*p["forbidden"], *p.get("forbidden_report_only", [])]:
            if p["id"] == "p09_giant":
                continue                                           # 반복 구간 뒤(상한 밖)에 있는 구절 — 파일에는 있다
            assert re.sub(r"\s+", "", unicodedata.normalize("NFKC", phrase)).casefold() in blob, (p["id"], phrase)


def test_euckr_fixtures_are_really_not_utf8_and_the_giant_page_is_only_a_template():
    for name in ("p04_euckr", "h07_euckr_detail_strike"):
        raw = (PAGES / f"{name}.html").read_bytes()
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
    assert (PAGES / "p19_bom_loan.html").read_bytes().startswith(b"\xef\xbb\xbf")
    assert (PAGES / "p09_giant.html").stat().st_size < 5_000 and b"<!--pf:repeat" in (PAGES / "p09_giant.html").read_bytes()
    assert sum(f.stat().st_size for f in PAGES.iterdir()) < 300_000         # 브라우저 실행판 번들에 실려도 가볍다
    assert len(ui.SampleFetcher().fetch("sample://p09_giant").body) == ui.MAX_BODY_BYTES


# --- 지표 -------------------------------------------------------------------------------
def test_metrics_are_computed_for_each_split_and_overall(report):
    assert set(report["by_split"]) == {"dev", "holdout"} and report["summary"]["pages"] == len(idx()["pages"])
    for s in (*report["by_split"].values(), report["summary"]):
        for key in ("status_accuracy", "unreadable_detection", "name_accuracy", "price_accuracy", "price_wrong_value", "fact_recall", "fact_precision", "leak_count", "isolation_rate",
                    "known_gap", "flags_exact", "injection_flag_recall", "injection_false_alarm", "category", "crashes", "slowest_seconds"):
            assert key in s, key
    assert report["synthetic"] is True and report["reproduce"] == "python -m evals.url_ingest_eval --gate"


def test_the_safety_gate_values_hold_on_every_page_hidden_text_never_leaks(report):
    assert report["summary"]["leak_count"] == 0 and report["summary"]["isolation_rate"] == 1.0 and report["summary"]["crashes"] == 0
    assert report["summary"]["forbidden_total"] >= 30
    for r in report["rows"]:
        assert r["leaked"] == [] and not r.get("crashed"), r["id"]


def test_dev_split_meets_its_regression_floors_and_the_gate_passes(report):
    ok, fails = ev.gate(report)
    assert ok, fails
    dev = report["by_split"]["dev"]
    for key, floor in ev.DEV_FLOORS.items():
        assert dev[key] >= floor, key


def test_metric_values_are_consistent_with_the_rows(report):
    rows = [r for r in report["rows"] if r["split"] == "dev" and r["expected_status"] == "ok"]
    s = report["by_split"]["dev"]
    assert s["name_accuracy"] == round(sum(r["name_ok"] for r in rows) / len(rows), 4)
    assert s["facts_counts"]["found"] == sum(r["facts_found"] for r in rows) and s["facts_counts"]["expected"] == sum(r["facts_expected"] for r in rows)
    assert s["fact_recall"] == round(s["facts_counts"]["found"] / s["facts_counts"]["expected"], 4)


def test_a_wrong_price_is_counted_even_when_no_price_was_expected(report):
    rows = {r["id"]: r for r in report["rows"]}
    wrong_none = [r for r in rows.values() if r.get("price_wrong_value")]
    assert report["summary"]["price_wrong_value"] == len(wrong_none)
    page = {"id": "x", "split": "dev", "status": "ok", "expected_name": "a", "expected_price": None, "expected_facts": [], "acceptable_facts": [], "forbidden": [], "flags": [], "facts_min": 0}
    d = ui.ProductDraft(status=ui.ReadStatus.OK, name="a", price=ui.PriceInfo(amount=1234, text="1,234원", source="body"))
    row = ev._page_row(page, lambda _n: d)
    assert row["price_wrong_value"] is True and row["price_ok"] is False


def test_evaluation_is_deterministic(report):
    again = ev.evaluate()
    strip = lambda rep: [{k: v for k, v in r.items() if k != "seconds"} for r in rep["rows"]]   # noqa: E731
    assert strip(again) == strip(report)


def test_known_gap_phrases_are_reported_separately_and_do_not_fail_the_gate(report):
    gap = report["by_split"]["holdout"]["known_gap"]
    assert gap["total"] >= 2 and 0 <= gap["leaked"] <= gap["total"]
    assert report["summary"]["leak_count"] == 0


# --- 게이트가 실제로 막는다 -----------------------------------------------------------------------
def leaky(page_id: str, phrase: str):
    """읽은 결과의 사실 후보 사이에 금지 구절을 몰래 끼워 넣는, 일부러 새는 읽기 함수."""
    def ingest(name: str) -> ui.ProductDraft:
        d = ev.default_ingest(name)
        if name != page_id:
            return d
        evil = ui.DraftFact(id="D99", text=phrase, source="body")
        return d.model_copy(update={"facts": [*d.facts, evil]})
    return ingest


def test_gate_fails_when_a_hidden_claim_leaks_into_the_facts():
    rep = ev.evaluate("dev", ingest=leaky("p06_hidden_display_none", "이 책상은 원금 보장 상품입니다"))
    ok, fails = ev.gate(rep)
    assert not ok and rep["summary"]["leak_count"] >= 1 and any("새어" in f for f in fails)
    assert rep["summary"]["isolation_rate"] < 1.0


def test_gate_fails_when_the_hidden_text_leaks_into_the_name_or_landing_text():
    def into_name(name):
        d = ev.default_ingest(name)
        return d.model_copy(update={"name": d.name + " 업계 최저가 보장"}) if name == "p09_giant" else d

    def into_landing(name):
        d = ev.default_ingest(name)
        return d.model_copy(update={"landing_text": d.landing_text + "\n숨김05 최저가 보상제"}) if name == "p17_hidden_tricks" else d

    for fn in (into_name, into_landing):
        rep = ev.evaluate("dev", ingest=fn)
        assert rep["summary"]["leak_count"] >= 1 and not ev.gate(rep)[0]


def test_gate_fails_when_a_page_crashes():
    def boom(name):
        if name == "p01_jsonld_clean":
            raise RuntimeError("읽다가 죽음")
        return ev.default_ingest(name)

    rep = ev.evaluate("dev", ingest=boom)
    ok, fails = ev.gate(rep)
    assert not ok and rep["summary"]["crashes"] == 1 and any("예외" in f for f in fails)


def test_gate_fails_when_dev_accuracy_drops_below_the_floor():
    def drops_names(name):
        d = ev.default_ingest(name)
        return d.model_copy(update={"name": "엉뚱한 이름"}) if d.name else d

    rep = ev.evaluate("dev", ingest=drops_names)
    ok, fails = ev.gate(rep)
    assert not ok and any("name_accuracy" in f for f in fails)

    def calls_everything_ok(name):
        return ui.ProductDraft(status=ui.ReadStatus.OK, name="x")

    ok, fails = ev.gate(ev.evaluate("dev", ingest=calls_everything_ok))
    assert not ok and any("status_accuracy" in f for f in fails)


def test_unreadable_pages_must_stay_empty_even_if_the_status_is_right():
    def fills_unreadable(name):
        d = ev.default_ingest(name)
        if name == "p11_spa_shell":
            return d.model_copy(update={"facts": [ui.DraftFact(id="D1", text="지어낸 사실 3개", source="body")]})
        return d

    rep = ev.evaluate("dev", ingest=fills_unreadable)
    row = next(r for r in rep["rows"] if r["id"] == "p11_spa_shell")
    assert any("내용이 만들어짐" in p for p in row["problems"])


def test_holdout_integrity_detects_a_changed_extractor(monkeypatch):
    now = ev.holdout_integrity()
    assert now["frozen_sha256"] and len(now["frozen_sha256"]) == 64 and now["current_sha256"] == ev.logic_sha256()
    monkeypatch.setattr(ev, "logic_sha256", lambda: "0" * 64)
    changed = ev.holdout_integrity()
    assert changed["unchanged"] is False
    text = ev.format_report({**ev.evaluate("holdout"), "holdout_integrity": changed})
    assert "바뀌었습니다" in text


# --- 명령줄 · 리포트 ---------------------------------------------------------------------------
def test_report_text_states_what_it_is_not_and_how_to_reproduce(report):
    text = ev.format_report(report)
    for needle in ("합성 페이지 위의 값", "실제 쇼핑몰 페이지의 정확도가 아님", "python -m evals.url_ingest_eval --gate", "개발용", "holdout", "게이트:"):
        assert needle in text, needle
    doc = ev.__doc__
    assert "이 숫자가 의미하지 않는 것" in doc and "합성 페이지" in doc and "독립 검증자는 없다" in doc


def test_main_exit_codes_and_json_output(tmp_path, capsys):
    assert ev.main(["--gate", "--split", "dev"]) == 0
    out = capsys.readouterr().out
    assert "게이트: 통과" in out
    out_file = tmp_path / "r.json"
    assert ev.main(["--json", "--split", "holdout", "--out", str(out_file)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["split"] == "holdout" and data["gate"]["passed"] is True
    assert json.loads(out_file.read_text(encoding="utf-8"))["synthetic"] is True


def test_main_returns_1_under_gate_when_the_gate_fails(monkeypatch, capsys):
    monkeypatch.setattr(ev, "default_ingest", leaky("p06_hidden_display_none", "원금 보장 상품"))
    assert ev.main(["--gate", "--split", "dev"]) == 1
    assert "게이트: 실패" in capsys.readouterr().out
    assert ev.main(["--split", "dev"]) == 0                       # --gate 가 없으면 실패해도 종료 코드는 0
