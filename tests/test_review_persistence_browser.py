"""실제 Chromium의 IndexedDB 검증. 실행: pytest tests/test_review_persistence_browser.py

선택 개발 의존성: playwright + Chromium. 앱 런타임에는 브라우저 드라이버가 필요 없다.
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from ops.review_queue import ReviewQueue

playwright_api = pytest.importorskip("playwright.sync_api")
FRONTEND = Path(__file__).resolve().parents[1] / "ui" / "review_storage_frontend"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def origin():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(FRONTEND)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(scope="module")
def playwright():
    with playwright_api.sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="module")
def browser(playwright):
    browser = playwright.chromium.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture
def page(browser, origin):
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(origin)
        yield page


def execute(page, command, namespace="test-review"):
    return page.evaluate("async ([command, namespace]) => (await import('./storage.js')).execute(command, namespace)", [command, namespace])


def save(text=None, *, expected=0, request_id="request-1", write_id="write-1"):
    return {"id": request_id, "op": "save", "expected_revision": expected, "write_id": write_id, "jsonl": text or ReviewQueue().to_jsonl()}


def test_automatic_load_recovers_after_page_reload(page):
    payload = ReviewQueue(required_labels=1).to_jsonl()
    assert execute(page, {"id": "load-1", "op": "load"}) == {"id": "load-1", "status": "loaded", "revision": 0, "jsonl": None}
    assert execute(page, save(payload))["status"] == "saved"
    page.reload()
    restored = execute(page, {"id": "load-2", "op": "load"})
    assert restored["revision"] == 1 and restored["jsonl"] == payload


def test_two_tabs_cannot_silently_overwrite_each_other(page, origin):
    other = page.context.new_page()
    other.goto(origin)
    first = save(ReviewQueue(required_labels=1).to_jsonl())
    second = save(ReviewQueue(required_labels=2).to_jsonl(), request_id="request-2", write_id="write-2")
    # 두 탭은 모두 0번째 저장본을 읽었고, 두 번째 탭은 이전 번호로 저장을 시도한다.
    execute(other, {"id": "load", "op": "load"})
    winner = execute(page, first)
    loser = execute(other, second)
    assert winner["status"] == "saved" and loser["status"] == "conflict"
    restored = execute(other, {"id": "load-after-conflict", "op": "load"})
    assert restored["revision"] == 1 and restored["jsonl"] == first["jsonl"]


def test_simultaneous_transactions_have_exactly_one_winner(page):
    commands = [save(request_id=f"request-{i}", write_id=f"write-{i}") for i in range(8)]
    results = page.evaluate("async commands => {const m = await import('./storage.js'); return Promise.all(commands.map(c => m.execute(c, 'parallel-review')))}", commands)
    assert [r["status"] for r in results].count("saved") == 1
    assert [r["status"] for r in results].count("conflict") == 7


def test_retry_after_lost_response_is_idempotent(page):
    command = save()
    assert execute(page, command)["revision"] == 1
    page.reload()
    assert execute(page, {**command, "id": "retry"}) == {"id": "retry", "status": "saved", "revision": 1}
    assert execute(page, {"id": "load", "op": "load"})["revision"] == 1


def test_different_browser_profiles_have_separate_queues(page, browser, origin):
    assert execute(page, save())["status"] == "saved"
    with browser.new_context() as private:
        other = private.new_page()
        other.goto(origin)
        assert execute(other, {"id": "load", "op": "load"})["jsonl"] is None


def test_quota_failure_does_not_replace_previous_record(page):
    first = save()
    assert execute(page, first)["status"] == "saved"
    page.evaluate("() => { IDBObjectStore.prototype.put = function () { throw new DOMException('full', 'QuotaExceededError'); }; }")
    failed = execute(page, save(ReviewQueue(required_labels=1).to_jsonl(), expected=1, request_id="next", write_id="next"))
    assert failed == {"id": "next", "status": "error", "error": "quota"}
    page.reload()
    assert execute(page, {"id": "load", "op": "load"})["jsonl"] == first["jsonl"]


def test_denied_storage_cannot_be_reported_as_saved(page):
    page.evaluate("Object.defineProperty(globalThis, 'indexedDB', {value: {open() {throw new DOMException('denied', 'SecurityError')}}})")
    assert execute(page, save()) == {"id": "request-1", "status": "error", "error": "denied"}


def test_corrupt_record_is_never_silently_overwritten(page):
    assert execute(page, save())["status"] == "saved"
    page.evaluate("""() => new Promise(resolve => {
      const req = indexedDB.open('preflight.review_queue', 1);
      req.onsuccess = () => {
        const db = req.result;
        const tx = db.transaction('snapshots', 'readwrite');
        tx.objectStore('snapshots').put({namespace: 'test-review', revision: 'broken', jsonl: 'original'});
        tx.oncomplete = () => { db.close(); resolve(); };
      };
    })""")
    assert execute(page, {"id": "load", "op": "load"})["error"] == "invalid"
    assert execute(page, save(expected=1))["error"] == "invalid"


def test_recovery_download_is_complete_and_does_not_render_raw_answers(page, tmp_path):
    text = "검증용 정답은 화면에 노출하지 않습니다\n" * 20000
    page.evaluate("backup => window.postMessage({type: 'streamlit:render', args: {command: null, backup}}, '*')",
                  {"text": text, "file_name": "review_queue_recovery.jsonl", "label": "전체 백업"})
    with page.expect_download() as download:
        page.get_by_role("link", name="전체 백업").click()
    target = tmp_path / "recovery.jsonl"
    download.value.save_as(target)
    assert target.read_text(encoding="utf-8") == text
    assert "검증용 정답" not in page.locator("body").inner_text()


def test_browser_process_restart_preserves_committed_snapshot(tmp_path, origin, playwright):
    profile = tmp_path / "browser-profile"
    with playwright.chromium.launch_persistent_context(str(profile), headless=True) as context:
        page = context.new_page()
        page.goto(origin)
        command = save(ReviewQueue(required_labels=1).to_jsonl())
        assert execute(page, command)["status"] == "saved"
    with playwright.chromium.launch_persistent_context(str(profile), headless=True) as restarted:
        page = restarted.new_page()
        page.goto(origin)
        assert execute(page, {"id": "after-restart", "op": "load"})["jsonl"] == command["jsonl"]
