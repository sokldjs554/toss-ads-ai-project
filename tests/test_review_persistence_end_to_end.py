"""실제 Streamlit + Chromium: UI로 라벨 저장 → 새로고침 → 서버 종료/재시작 → 자동 복원."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_review_persistence_browser import browser, playwright  # noqa: F401 — 공유 브라우저 fixture

pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


def _start(script, port, log):
    process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(script), "--server.address=127.0.0.1", f"--server.port={port}",
                                "--server.headless=true", "--browser.gatherUsageStats=false"], cwd=ROOT,
                               env={**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Streamlit test server did not start")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except OSError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    raise RuntimeError("Streamlit test server timed out")


def _snapshot(page):
    return page.evaluate("""() => new Promise((resolve, reject) => {
      const request = indexedDB.open('preflight.review_queue', 1);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction('snapshots', 'readonly');
        const read = tx.objectStore('snapshots').get('preflight-studio-review-v1');
        read.onsuccess = () => resolve(read.result || null);
        tx.oncomplete = () => db.close();
      };
    })""")


def test_ui_save_reload_and_server_restart_keep_the_label(tmp_path, browser):
    script = tmp_path / "review_app.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom tests.test_labeling_ui import _run\nfrom ui.review_queue_view import render\nrender([_run()])\n", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "server.log").open("w") as log, browser.new_context() as context:
        process = _start(script, port, log)
        try:
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_role("button", name=re.compile("담당자 확인.*대기열로 보내기")).click(timeout=30000)
            page.get_by_text("이 브라우저에 저장됨", exact=False).wait_for(timeout=15000)
            page.get_by_role("textbox", name="라벨러 이름", exact=True).fill("재시작 검토자")
            page.get_by_role("textbox", name="라벨러 이름", exact=True).press("Enter")
            page.get_by_role("button", name="다음 광고 받기", exact=True).click()
            page.get_by_text("점검 통과", exact=True).click()
            page.get_by_role("textbox", name="이유", exact=True).fill("저장 후 서버를 다시 시작해도 기록이 이어지는지 확인합니다")
            page.get_by_role("button", name="라벨 저장", exact=True).click()
            page.get_by_text("라벨을 저장했습니다", exact=False).wait_for(timeout=15000)
            snapshot = _snapshot(page)
            assert '재시작 검토자' in snapshot["jsonl"] and snapshot["revision"] >= 3
            page.reload()
            page.get_by_text("이 브라우저에 저장됨", exact=False).wait_for(timeout=20000)
            page.get_by_text("라벨러가 한 명이라 일치도를 잴 수 없습니다", exact=False).wait_for()
            assert _snapshot(page)["jsonl"] == snapshot["jsonl"]
            process.terminate()
            process.wait(timeout=10)
            process = _start(script, port, log)
            page.reload()
            page.get_by_text("이 브라우저에 저장됨", exact=False).wait_for(timeout=20000)
            page.get_by_text("라벨러가 한 명이라 일치도를 잴 수 없습니다", exact=False).wait_for()
            assert _snapshot(page)["jsonl"] == snapshot["jsonl"]
        finally:
            process.terminate()
            process.wait(timeout=10)
