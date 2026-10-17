"""재생 페이지(GitHub Pages) 가 깨지지 않는지 — 문구를 고치다 따옴표 하나로 페이지 전체가 빈 화면이 된 적이 있다.

브라우저 없이 확인할 수 있는 것: 인라인 스크립트의 문법(node --check)과 내장 데이터(JSON)가 최신 회차를 담고 있는지.
node 가 없으면 문법 검사만 건너뛴다 (GitHub Actions ubuntu 러너에는 node 가 있다).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def page(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("replay")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_replay.py"), "--out", str(out)], check=True, capture_output=True, cwd=ROOT)
    return (out / "index.html").read_text(encoding="utf-8")


def _scripts(html: str) -> list[tuple[str, str]]:
    return re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)


def test_embedded_data_has_latest_round_and_matching_labels(page):
    data_blocks = [body for attrs, body in _scripts(page) if 'id="data"' in attrs]
    assert len(data_blocks) == 1
    ev = json.loads(data_blocks[0])["evals"]
    assert [r["label"] for r in ev["rounds"]][-1] == "5차" and len(ev["rounds"]) == 5
    assert ev["red"][-1]["label"].startswith("5차") and len(ev["red"]) == 5
    assert ev["run_id"] and ev["trace_run_id"] and ev["trace_round"] and ev["eval_round"]
    # 상단 요약은 마지막 회차를 읽는다 (고정 인덱스로 4차를 읽던 문제)
    assert "D.evals.rounds[3]" not in page and "D.evals.red[3]" not in page
    assert {e["dataset"] for e in ev["extra"]} == {"holdout", "contrast_g069"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node 가 없어 스크립트 문법 검사를 건너뜀")
def test_inline_scripts_are_valid_javascript(page, tmp_path):
    checked = 0
    for i, (attrs, body) in enumerate(_scripts(page)):
        if "application/json" in attrs or not body.strip():
            continue
        f = tmp_path / f"s{i}.js"
        f.write_text(body, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"인라인 스크립트 {i} 문법 오류:\n{r.stderr[:600]}"
        checked += 1
    assert checked >= 1
