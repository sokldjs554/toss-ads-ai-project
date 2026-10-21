"""Observe current shared-review and evaluation UI in native and Pages builds.

Uses the actual build_site.py archive and app.py entrypoint. It never fabricates
an evaluation result, connects to a shared database, or calls a model provider.
Outputs must be new directories. Product and frozen evaluation files are read-only.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ESSENTIAL = (
    "ui/team_review_view.py", "ui/ops_hub.py", "ui/new_evals.py",
    "ops/team_review.py", "ops/team_review_store.py",
    "evals/reports/dialogs_regression_v2_20261021.json",
    "evals/reports/dialogs_original_after_fix_20261021.json",
)
NATIVE_LINK = "https://preflight-toss-ads.onrender.com/?view=운영"
VIEWS = (("desktop", 1280, 900), ("mobile", 390, 844))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--modes", default="native,pages", choices=("native", "pages", "native,pages"))
    ap.add_argument("--app-python", default=sys.executable, help="Existing application environment, separate from an optional QA browser environment")
    ap.add_argument("--timeout-ms", type=int, default=180000)
    ap.add_argument("--ignore-https-errors", action="store_true", help="Test proxy only; recorded in evidence, never applied to product HTML")
    args = ap.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error("Choose an empty evidence directory; prior evidence is preserved.")
    out.mkdir(parents=True, exist_ok=True)
    # Keep a virtual environment's executable path: resolving its interpreter
    # symlink would discard its site-packages.
    app_python = os.path.abspath(args.app_python)
    report = {
        "kind": "remaining-shared-review-and-evaluation-ui",
        "started_at": now(), "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "modes": args.modes.split(","), "viewports": VIEWS,
        "playwright": version("playwright"), "ignore_https_errors": args.ignore_https_errors,
        "scope": "native database unconfigured; Pages browser mode; saved evaluation records only",
        "checks": [], "observations": [], "screenshots": [],
        "source_hashes": {name: sha((ROOT / name).read_bytes()) for name in ESSENTIAL},
        "frozen_report_hashes_before": {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in (ROOT / "evals/reports/live_features").glob("*.json")},
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check(scope: str, label: str, okay: bool, detail=None):
        report["checks"].append({"scope": scope, "check": label, "ok": bool(okay), "detail": detail})
        save()
        print(f"{'PASS' if okay else 'FAIL'} {scope}: {label}", flush=True)

    def picture(page, name, anchor=None):
        if anchor:
            anchor.evaluate("e => {e.scrollIntoView({block:'start'}); document.querySelector('[data-testid=\"stMain\"]')?.scrollBy(0,-20)}")
            page.wait_for_timeout(350)
        page.screenshot(path=str(out / name), full_page=False)
        report["screenshots"].append(name)
        save()

    def inspect_page(page, scope):
        check(scope, "no Streamlit exception", page.get_by_test_id("stException").count() == 0)
        geometry = page.evaluate("({scroll:document.documentElement.scrollWidth, viewport:innerWidth})")
        check(scope, "no page horizontal overflow", geometry["scroll"] <= geometry["viewport"] + 1, geometry)

    def observe(browser, mode, url):
        for viewport, width, height in VIEWS:
            scope = mode + "-" + viewport
            observed = {"scope": scope, "started_at": now(), "page_errors": [], "console_errors": [], "network_errors": [], "blocked_model_requests": []}
            report["observations"].append(observed)
            context = browser.new_context(viewport={"width": width, "height": height}, ignore_https_errors=args.ignore_https_errors)
            page = context.new_page()
            page.on("pageerror", lambda error: observed["page_errors"].append(str(error)))
            page.on("console", lambda msg: observed["console_errors"].append(msg.text[:2000]) if msg.type == "error" else None)
            page.on("requestfailed", lambda req: observed["network_errors"].append({"url": req.url, "failure": req.failure}))

            def block_provider(route):
                observed["blocked_model_requests"].append(route.request.url)
                route.abort()

            page.route(re.compile(r"https://api\.(?:openai|anthropic)\.com/.*"), block_provider)
            try:
                print(f"OPEN {scope}", flush=True)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                observed["webassembly"] = page.evaluate("({Suspending:typeof WebAssembly.Suspending,promising:typeof WebAssembly.promising})")
                page.get_by_role("radio", name="운영", exact=True).wait_for(timeout=args.timeout_ms)
                page.get_by_role("radio", name="운영", exact=True).click()
                page.get_by_role("tab", name="공동 검수", exact=True).wait_for(timeout=60000)
                tabs = page.get_by_role("tablist").first.get_by_role("tab").all_text_contents()
                check(scope, "operations has all three tabs in order", tabs == ["검수 대기열", "운영 지표·모니터", "공동 검수"], tabs)
                page.get_by_role("tab", name="공동 검수", exact=True).click()
                expected = "공동 저장 연결이 아직 설정되지 않았습니다" if mode == "native" else "공동 검수는 서버가 연결된 운영 화면에서"
                page.get_by_text(expected, exact=False).first.wait_for(timeout=60000)
                visible = page.inner_text("body")
                check(scope, "correct shared review availability message", expected in visible)
                check(scope, "no access key input before server setup", page.get_by_label("접근키", exact=True).count() == 0)
                forbidden = re.findall(r"pfk_[A-Za-z0-9_-]{43}|postgres(?:ql)?://|PREFLIGHT_TEAM_DATABASE_URL|key_digest|request_id|acct_owner|psycopg|오탐|미탐|맞습니다", visible)
                check(scope, "no secret or unnecessary internal control names", not forbidden, forbidden)
                check(scope, "accounts do not claim independent people", "실제 참여자와 독립 검수 여부는 별도 확인" in visible)
                if mode == "pages":
                    link = page.get_by_role("link", name="서버의 운영 화면 열기", exact=True)
                    check(scope, "server link is correct", link.count() == 1 and unquote(link.get_attribute("href") or "") == NATIVE_LINK)
                    check(scope, "browser mode is identified", "브라우저 실행판 (서버 없음, 예시 실행만)" in visible)
                inspect_page(page, scope + ":shared-review")
                picture(page, scope + ("-team-database-unconfigured.png" if mode == "native" else "-team-server-link.png"), page.get_by_role("heading", name="공동 검수", exact=True))

                page.get_by_role("radio", name="품질 확인", exact=True).click()
                heading = page.get_by_role("heading", name="새 기능 평가 (합성 데이터)", exact=True)
                heading.wait_for(timeout=60000)
                page.get_by_role("heading", name="실제 모델 실행 기록", exact=True).wait_for(timeout=60000)
                page.get_by_text("실제 응답에서 확인한 모델:", exact=False).first.wait_for(timeout=60000)
                visible = page.inner_text("body")
                check(scope, "evaluation limits are displayed", "독립 검증은 없으며" in visible and "실제 광고 데이터의 정확도가 아닙니다" in visible)
                check(scope, "stored calls are not advertised as new calls", "여기서 열람하는 동안 새 호출이나 비용은 발생하지 않습니다" in visible)
                reports = []
                for path in (ROOT / "evals/reports/live_features").glob("*.json"):
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if record.get("schema_version") == 1 and isinstance(record.get("results"), dict):
                        reports.append(record)
                reports.sort(key=lambda r: (str(r.get("finished_at") or r.get("started_at") or ""), str(r.get("run_id", ""))), reverse=True)
                latest = reports[0]
                observed["latest_saved_report"] = latest["run_id"]
                detail = page.locator("details").filter(has_text=latest["run_id"]).first
                check(scope, "latest stored report is expanded", detail.get_attribute("open") is not None)
                first_rows = detail.locator("table").first.locator("tbody tr")
                actual = first_rows.filter(has_text="여러 턴 캠페인 설정").locator("td").all_text_contents()
                summary = latest["summaries"]["conversation"]
                attempted = summary["attempted_cases"]
                expected_cells = ["여러 턴 캠페인 설정", f"{attempted}/{latest['manifest']['planned_cases']['conversation']}",
                                  f"{summary['cases_with_real_calls']}/{attempted}", f"{summary['all_checks_passed']}/{attempted}"]
                check(scope, "conversation score matches frozen stored report", actual == expected_cells, {"actual": actual, "expected": expected_cells})
                observed["quality_audit_available_in_saved_report"] = bool(latest.get("quality_audit_summary"))
                observed["rewrite_supplement_available_in_saved_report"] = bool(latest.get("supplemental_summaries", {}).get("rewrite"))
                if latest.get("quality_audit_summary"):
                    check(scope, "saved quality accept/abstain summary appears", "문구 비교 · 채택과 보류" in detail.inner_text())
                if latest.get("supplemental_summaries", {}).get("rewrite"):
                    check(scope, "saved rewrite supplement appears separately", "추가 검사 · 위반 원문의 수정과 재검사" in detail.inner_text())
                inspect_page(page, scope + ":evaluations")
                picture(page, scope + "-saved-live-evaluations.png", page.get_by_role("heading", name="실제 모델 실행 기록", exact=True))
                dialog = page.locator("details").filter(has=page.locator("summary").filter(has_text="대화형 캠페인 설정")).last
                dialog.locator("summary").click()
                page.get_by_text("알려진 실패를 수정한 뒤 다시 실행한 66개 대화", exact=False).first.wait_for(timeout=30000)
                check(scope, "versioned conversation report is available", "알려진 실패를 수정한 뒤 다시 실행한 66개 대화" in dialog.inner_text() and "별도 작성한 20개 대화의 첫 결과" in dialog.inner_text())
                inspect_page(page, scope + ":conversation-evaluation")
                picture(page, scope + "-conversation-regression-and-original.png", dialog.locator("summary"))
                observed["status"] = "completed"
            except Exception as exc:
                observed["status"] = "interrupted"
                observed["error"] = {"type": type(exc).__name__, "message": str(exc)}
                check(scope, "requested screens complete", False, type(exc).__name__)
                try:
                    (out / (scope + "-interrupted-visible-text.txt")).write_text(page.inner_text("body"), encoding="utf-8")
                    picture(page, scope + "-interrupted.png")
                except Exception as capture:
                    observed["capture_error"] = str(capture)
            finally:
                check(scope, "no browser page exception", not observed["page_errors"], observed["page_errors"])
                check(scope, "no model provider request", not observed["blocked_model_requests"], observed["blocked_model_requests"])
                observed["finished_at"] = now()
                save()
                context.close()

    native = None
    static = None
    log = (out / "native-server.log").open("w", encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="preflight-remaining-site-") as temporary:
            site = Path(temporary) / "site"
            build = subprocess.run([app_python, "scripts/build_site.py", "--out", str(site)], cwd=ROOT, capture_output=True, text=True, check=True)
            (out / "pages-build.log").write_text(build.stdout, encoding="utf-8")
            archive = site / "app/src.zip"
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                report["bundle"] = {"sha256": sha(archive.read_bytes()), "files": len(names), "essential_hashes": {name: sha(zf.read(name)) for name in ESSENTIAL}, "stlite_html_sha256": sha((site / "app/index.html").read_bytes())}
                check("pages-build", "all new UI and reports bundled unchanged", report["bundle"]["essential_hashes"] == report["source_hashes"])
                leaked_names = [n for n in names if n == ".streamlit/secrets.toml" or Path(n).name.startswith(".env") or "secret" in Path(n).name.lower()]
                check("pages-build", "no secret configuration files bundled", not leaked_names, leaked_names)
            urls = {}
            if "pages" in report["modes"]:
                static = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(site)))
                threading.Thread(target=static.serve_forever, daemon=True).start()
                urls["pages"] = f"http://127.0.0.1:{static.server_port}/app/"
            if "native" in report["modes"]:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                env = {**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}
                for key in ("OPENAI_API_KEY", "OPEN_API_KEY", "ANTHROPIC_API_KEY", "PREFLIGHT_TEAM_DATABASE_URL"):
                    env.pop(key, None)
                native = subprocess.Popen([app_python, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(port), "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                urls["native"] = f"http://127.0.0.1:{port}/"
                for _ in range(60):
                    if native.poll() is not None:
                        raise RuntimeError("Native application exited before ready; see native-server.log")
                    try:
                        urllib.request.urlopen(urls["native"] + "_stcore/health", timeout=2)
                        break
                    except OSError:
                        time.sleep(1)
                else:
                    raise RuntimeError("Native application did not become ready")
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                executable = os.environ.get("PREFLIGHT_CHROMIUM")
                with pw.chromium.launch(headless=True, **({"executable_path": executable} if executable else {})) as browser:
                    report["browser"] = browser.version
                    for mode in report["modes"]:
                        observe(browser, mode, urls[mode])
    except Exception as exc:
        report["setup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        check("setup", "verification starts", False, type(exc).__name__)
    finally:
        if native is not None:
            native.terminate()
            try:
                native.wait(timeout=10)
            except subprocess.TimeoutExpired:
                native.kill()
                native.wait()
        if static is not None:
            static.shutdown()
            static.server_close()
        log.close()
        unchanged = all((ROOT / name).exists() and sha((ROOT / name).read_bytes()) == expected for name, expected in report["frozen_report_hashes_before"].items())
        check("provenance", "pre-existing live reports unchanged", unchanged)
        report["finished_at"] = now()
        report["failed_checks"] = sum(not c["ok"] for c in report["checks"])
        report["status"] = "passed" if not report["failed_checks"] else "failed"
        save()
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]), "failed": report["failed_checks"]}), flush=True)
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
