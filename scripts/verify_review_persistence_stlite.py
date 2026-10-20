"""stlite 실제 브라우저 저장/새로고침 확인 (유료 API 호출 없음).

python scripts/verify_review_persistence_stlite.py --out /tmp/preflight-review-browser
개발 도구 playwright + Chromium, stlite/Pyodide CDN 연결이 필요하다.
배포와 같은 src.zip 생성 함수를 쓰고, 저장 연결만 그리는 작은 진입점을 추가한다.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_site import build_zip  # noqa: E402

APP = '''import streamlit as st
from core.schemas import AdCopy
from ui import review_queue_storage as storage

st.title("브라우저 자동 저장 확인")
if storage.ready():
    queue = st.session_state["rq_queue"]
    if st.button("검증 광고 추가", disabled=len(queue) > 0):
        queue.enqueue_ad(AdCopy(headline="자동 저장 확인 광고", body="복원 확인용 가상 문구입니다", cta="확인"), dataset_id="storage-check", category="other")
        st.rerun()
    for item in queue.items():
        st.write(item.ad_copy.headline)
    storage.save_changes()
'''


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("/tmp/preflight-review-browser"))
    ap.add_argument("--timeout-ms", type=int, default=180000)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    build_zip(out / "src.zip")
    with zipfile.ZipFile(out / "src.zip", "a") as archive:
        archive.writestr("persistence_check.py", APP)
    index = (ROOT / "site" / "app" / "index.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(index.replace('entrypoint: "app.py"', 'entrypoint: "persistence_check.py"'), encoding="utf-8")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(out)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    network_errors: list[dict] = []
    console_errors: list[str] = []
    try:
        with sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            with browser.new_context(ignore_https_errors=True) as context:
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("requestfailed", lambda request: network_errors.append({"url": request.url, "failure": request.failure}))
                page.on("response", lambda response: network_errors.append({"url": response.url, "status": response.status}) if response.status >= 400 else None)
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.goto(f"http://127.0.0.1:{server.server_port}")
                try:
                    page.get_by_role("button", name="검증 광고 추가", exact=True).click(timeout=args.timeout_ms)
                    page.get_by_text("이 브라우저에 저장됨", exact=False).wait_for(timeout=30000)
                    page.get_by_text("자동 저장 확인 광고", exact=True).wait_for()
                    page.screenshot(path=str(out / "saved.png"), full_page=True)
                    page.reload()
                    page.get_by_text("자동 저장 확인 광고", exact=True).wait_for(timeout=args.timeout_ms)
                    page.get_by_text("이 브라우저에 저장됨", exact=False).wait_for()
                    page.screenshot(path=str(out / "restored.png"), full_page=True)
                    result = {"status": "passed", "browser": browser.version, "automatic_restore": True, "page_errors": errors}
                except Exception as error:
                    page.screenshot(path=str(out / "failure.png"), full_page=True)
                    result = {"status": "failed", "error": str(error), "page_errors": errors, "network_errors": network_errors, "console_errors": console_errors,
                              "frames": [frame.url for frame in page.frames], "visible_text": page.locator("body").inner_text()[-8000:]}
                (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 0 if result["status"] == "passed" else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
