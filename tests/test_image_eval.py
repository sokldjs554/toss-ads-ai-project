"""이미지 평가(evals/image_eval.py)와 픽스처(evals/datasets/images/make_fixtures.py)의 시험.

평가 지표는 합성 사례의 값이다 — 실제 비전 모델의 정확도가 아니다. 여기서는 평가 코드가 약속대로 계산하고 게이트가 실제로 걸리는지를 본다.
"""
from __future__ import annotations

import json

import pytest

from evals import image_eval
from evals.image_eval import DATASET, FIXTURE_DIR, evaluate_case, gate, load_cases, run_eval, summarize
from tests.test_image_common import mf


def test_dataset_has_enough_cases_in_both_splits_with_unique_ids():
    rows = load_cases()
    assert len(rows) >= 40 and {r["split"] for r in rows} == {"dev", "holdout"}
    assert len({r["id"] for r in rows}) == len(rows)
    assert {r["type"] for r in rows} == {"wcag", "banner", "image_text", "svg", "load"}
    assert all(("-D" in r["id"]) == (r["split"] == "dev") and ("-H" in r["id"]) == (r["split"] == "holdout") for r in rows)         # 번호표에 개발용(D)/최종 확인용(H) 이 드러난다
    raw = DATASET.read_text(encoding="utf-8")
    assert all(json.loads(ln) for ln in raw.splitlines() if ln.strip())


def test_every_fixture_file_is_what_the_generator_makes_today():
    """저장된 픽스처 파일은 make_fixtures.py 의 결정론 출력과 같다 (바이너리를 손으로 만들지 않았다)."""
    for name in mf.FIXTURES:
        assert (FIXTURE_DIR / name).read_bytes() == mf.build(name), name
    assert set(p.name for p in FIXTURE_DIR.iterdir() if p.is_file()) - {"make_fixtures.py"} == set(mf.FIXTURES)       # 설명되지 않은 파일이 없다
    assert mf.build("ok_png.png") == mf.build("ok_png.png") and mf.build("bomb_png_8192.png") == mf.build("bomb_png_8192.png")


def test_fixture_files_are_small_and_the_big_ones_are_generated_only():
    assert sum(p.stat().st_size for p in FIXTURE_DIR.iterdir()) < 400_000
    assert all(name not in mf.FIXTURES for name in mf.GENERATED_ONLY) and len(mf.build("oversize_png.png")) > 5 * 1024 * 1024


@pytest.mark.parametrize("split", ["dev", "holdout"])
def test_gates_hold_on_both_splits(split):
    rep = run_eval(split)
    s = rep["summary"]
    assert s["errors"] == 0
    assert s["bad_pass"]["k"] == 0 and s["bad_pass"]["n"] > 0              # 문제 있는 이미지 통과 0 (분모가 비어 있지 않다)
    assert s["unreadable_pass"]["k"] == 0 and s["unreadable_pass"]["n"] > 0
    assert s["sanitize"]["k"] == s["sanitize"]["n"] > 0                     # 정화 100%
    assert gate(rep) == []


def test_dev_split_meets_every_expectation():
    rep = run_eval("dev")
    assert rep["mismatches"] == [], rep["mismatches"]
    s = rep["summary"]
    assert s["all_expectations_met"]["k"] == s["all_expectations_met"]["n"] and s["wcag"]["max_abs_error"] <= 0.01


def test_report_is_deterministic_and_states_what_it_does_not_measure():
    a, b = run_eval("all"), run_eval("all")
    assert json.dumps(a, ensure_ascii=False, default=str) == json.dumps(b, ensure_ascii=False, default=str)
    assert a["real_vision_model_measured"] is False and a["provenance"]["synthetic"] is True and "python -m evals.image_eval" in a["repro"]
    doc = image_eval.__doc__
    assert "실제 비전 모델의 정확도는 측정하지 않았다" in doc and "이 숫자가 의미하지 않는 것" in doc and "holdout" in doc
    assert set(a["by_split"]) == {"dev", "holdout"} and a["summary"]["n"] == sum(s["n"] for s in a["by_split"].values())


def test_cli_gate_exit_codes(capsys):
    assert image_eval.main(["--gate", "--split", "dev"]) == 0
    out = capsys.readouterr().out
    assert "gate: PASS" in out and "합성" in out


def test_the_gate_really_fails_when_a_problem_image_passes_or_an_unreadable_one_passes():
    rep = run_eval("dev")
    bad = json.loads(json.dumps(rep, default=str))
    bad["summary"]["bad_pass"]["k"] = 1
    assert any("문제 있는 이미지가 점검 통과" in m for m in gate(bad))
    bad = json.loads(json.dumps(rep, default=str))
    bad["by_split"]["dev"]["unreadable_pass"]["k"] = 2
    assert any("읽지 못한 이미지가 점검 통과" in m for m in gate(bad))
    bad = json.loads(json.dumps(rep, default=str))
    bad["summary"]["sanitize"]["k"] -= 1
    assert any("SVG 정화 실패" in m for m in gate(bad))


def test_summarize_counts_a_problem_image_that_passes_as_a_failure():
    """평가 코드 자체의 변이 확인: 문제 있는 사례가 APPROVE 로 예측되면 bad_pass 가 올라간다."""
    row = {"id": "X", "split": "dev", "type": "image_text", "bad": True, "unreadable": True}
    res = [{"id": "X", "split": "dev", "type": "image_text", "ok": False, "predicted": "APPROVE", "checks": {"decision": False}, "_row": row}]
    s = summarize(res)
    assert s["bad_pass"]["k"] == 1 and s["unreadable_pass"]["k"] == 1


def test_evaluator_errors_are_counted_not_hidden():
    r = evaluate_case({"id": "E", "split": "dev", "type": "load", "fixture": "no_such_fixture.png", "expect": "x"})
    assert r["ok"] is False and r["error"] and summarize([r])["errors"] == 1


def test_eval_import_is_light_and_has_no_network_or_writes_at_import():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(image_eval.__file__).read_text(encoding="utf-8"))
    mods = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in getattr(n, "names", [None])}
    assert not (mods & {"requests", "urllib", "socket", "httpx", "PIL", "openai", "anthropic"})
