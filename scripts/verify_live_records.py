"""저장된 실제 모델 실행 기록의 데스크톱·모바일 표시를 브라우저로 확인한다.

예:
    python scripts/verify_live_records.py --run-id features-20261020-run3 --out /tmp/live-run3
    python scripts/verify_live_records.py --report evals/reports/live_features/features-20261020-run3.json --out /tmp/live-run3

선택한 보고서가 화면에서 가장 최신인 상태를 검사한다. 로컬 Streamlit을
PREFLIGHT_FORCE_DEMO=1로 시작하고 키 환경변수를 제외한다. 보고서를 읽는
화면만 열며 유료 API를 호출하지 않는다. 공개 배포 확인과 구분한다.
requirements-dev.txt, Chromium, 한글을 표시할 수 있는 폰트가 필요하다.
PREFLIGHT_CHROMIUM으로 별도 Chromium 실행 파일을 지정할 수 있다.
실패·부분 실행도 JSON에 남기며 기존 출력 폴더를 덮어쓰지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "evals" / "reports" / "live_features"
NAMES = {
    "vision": "이미지 속 글자·검사",
    "conversation": "여러 턴 캠페인 설정",
    "insights": "성과 보고서 문장화",
    "quality": "광고 문구 비교",
    "pipeline": "캠페인 구성·문구 생성·검사",
}
STATUS_LABELS = {
    "completed": "완료",
    "incomplete_budget_or_provider_failure": "일부만 실행",
    "interrupted": "실행 중단",
    "running": "실행 중",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _reports() -> list[tuple[Path, dict]]:
    found = []
    for path in REPORTS.glob("*.json"):
        rep = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rep, dict) and rep.get("schema_version") == 1 and isinstance(rep.get("results"), dict):
            found.append((path, rep))
    return sorted(found, key=lambda item: (str(item[1].get("finished_at") or item[1].get("started_at") or ""), str(item[1].get("run_id", ""))), reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    choice = ap.add_mutually_exclusive_group(required=True)
    choice.add_argument("--run-id")
    choice.add_argument("--report", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--port", type=int, default=8622)
    ap.add_argument("--timeout-ms", type=int, default=90000)
    args = ap.parse_args()
    available = _reports()
    if not available:
        ap.error("화면에 표시할 실행 보고서가 없습니다.")
    path, latest = available[0]
    if args.report:
        selected = json.loads(args.report.read_text(encoding="utf-8"))
        if selected != latest:
            ap.error("--report는 현재 화면에서 가장 최신인 보고서와 일치해야 합니다.")
    elif args.run_id != latest.get("run_id"):
        ap.error(f"현재 최신 실행은 {latest.get('run_id')}입니다.")
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error("증거를 보존하기 위해 비어 있는 새 출력 경로를 사용하세요.")
    out.mkdir(parents=True, exist_ok=True)
    start_commit = _commit()
    source_paths = [ROOT / "ui" / "new_evals.py", Path(__file__).resolve()]
    source_hashes = {str(p.relative_to(ROOT)): _sha(p) for p in source_paths}
    report_hashes = {p.name: _sha(p) for p, _ in available}
    url = f"http://127.0.0.1:{args.port}/?view=%ED%92%88%EC%A7%88%20%ED%99%95%EC%9D%B8"
    env = {**os.environ, "PREFLIGHT_FORCE_DEMO": "1"}
    for key in ("OPENAI_API_KEY", "OPEN_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    checks: list[dict] = []
    screenshots: list[str] = []
    browser_version = ""
    failure = None
    current_page = None
    current_viewport = "startup"

    def check(name: str, ok: bool, detail=None) -> None:
        checks.append({"viewport": current_viewport, "check": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + current_viewport + ": " + name, flush=True)

    server_log = (out / "streamlit.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", str(args.port), "--browser.gatherUsageStats", "false"], cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError("검증용 Streamlit 프로세스가 준비 전에 종료됐습니다.")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{args.port}/_stcore/health", timeout=1)
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("검증용 Streamlit의 준비 완료를 확인하지 못했습니다.")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = os.environ.get("PREFLIGHT_CHROMIUM")
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"], **({"executable_path": executable} if executable else {}))
            browser_version = browser.version
            try:
                for viewport, width, height in (("desktop", 1280, 900), ("mobile", 390, 844)):
                    current_viewport = viewport
                    context = browser.new_context(viewport={"width": width, "height": height})
                    page = current_page = context.new_page()
                    errors: list[str] = []
                    provider_requests: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def block_provider(route) -> None:
                        provider_requests.append(route.request.url)
                        route.abort()

                    page.route("https://api.openai.com/**", block_provider)
                    page.route("https://api.anthropic.com/**", block_provider)
                    page.goto(url, wait_until="load", timeout=60000)
                    heading = page.get_by_role("heading", name="실제 모델 실행 기록", exact=True)
                    heading.wait_for(timeout=args.timeout_ms)
                    page.get_by_text("실제 응답에서 확인한 모델:", exact=False).first.wait_for(timeout=args.timeout_ms)
                    time.sleep(1)
                    details = page.locator("details").filter(has_text=latest["run_id"]).first
                    check("latest report is expanded", details.get_attribute("open") is not None)
                    summaries = latest.get("summaries", {})
                    planned = latest.get("manifest", {}).get("planned_cases", {})
                    unattempted = any(isinstance(total, int) and total > summaries.get(key, {}).get("attempted_cases", 0) for key, total in planned.items())
                    expected_status = "일부만 실행" if latest.get("status") == "completed" and unattempted else STATUS_LABELS.get(latest.get("status"), "상태 확인 필요")
                    check("status matches report and attempted cases", expected_status in details.locator("summary").inner_text())
                    previous = [rep for _, rep in available[1:]]
                    check("previous reports are retained and collapsed", all(page.locator("details").filter(has_text=rep["run_id"]).first.get_attribute("open") is None for rep in previous))
                    order = page.locator("summary").all_text_contents()
                    positions = [next(i for i, text in enumerate(order) if rep["run_id"] in text) for _, rep in available]
                    check("reports are ordered newest first", positions == sorted(positions))
                    body = details.inner_text()
                    follows = bool(latest.get("manifest", {}).get("follows_run_id"))
                    check("same-input regression is labelled when applicable", ("처음 보는 입력의 성능과 구분합니다" in body) == follows)
                    rows = details.locator("table tbody tr")
                    check("five task groups are visible", rows.count() == len(NAMES))
                    for key, label in NAMES.items():
                        group = summaries.get(key, {})
                        attempted = group.get("attempted_cases", 0)
                        expected = [label, f"{attempted}/{planned.get(key, '—')}", f"{group.get('cases_with_real_calls', 0)}/{attempted}", f"{group.get('all_checks_passed', 0)}/{attempted}"]
                        actual = rows.filter(has_text=label).locator("td").all_text_contents()
                        check(f"{key}: displayed denominators match stored report", actual == expected, {"expected": expected, "actual": actual})
                    ledger = latest.get("ledger", {})
                    usage = ledger.get("usage_cost_usd")
                    cost = f"${usage:.4f}" if isinstance(usage, (float, int)) else "확인되지 않음"
                    check("usage cost matches report and is qualified", cost in body and "실제 청구서 금액은 아닙니다" in body)
                    check("call count matches report", f"호출 {ledger.get('attempts', 0)}회" in body)
                    models = sorted({call["returned_model"] for call in ledger.get("calls", []) if call.get("status") == "success" and call.get("returned_model")})
                    check("actual model and source are visible", all(model in body for model in models) and latest.get("source_sha", "")[:12] in body)
                    check("no Streamlit exception", page.get_by_test_id("stException").count() == 0)
                    geometry = page.evaluate("({scroll:document.documentElement.scrollWidth, viewport:window.innerWidth})")
                    check("no page horizontal overflow", geometry["scroll"] <= geometry["viewport"] + 1, geometry)
                    check("no browser exception", not errors, errors)
                    check("viewing records makes no provider requests", not provider_requests, provider_requests)
                    heading.evaluate("e => {e.scrollIntoView({block:'start'}); document.querySelector('[data-testid=\"stMain\"]')?.scrollBy(0,-20)}")
                    time.sleep(.5)
                    name = f"live-records-{viewport}.png"
                    page.screenshot(path=str(out / name), full_page=False)
                    screenshots.append(name)
                    if viewport == "mobile":
                        details.get_by_text("응답 토큰으로 계산한 비용", exact=False).first.evaluate("e => {e.scrollIntoView({block:'start'}); document.querySelector('[data-testid=\"stMain\"]')?.scrollBy(0,-24)}")
                        time.sleep(.5)
                        name = "live-records-mobile-cost.png"
                        page.screenshot(path=str(out / name), full_page=False)
                        screenshots.append(name)
                    context.close()
                    current_page = None
            except Exception:
                if current_page is not None and not current_page.is_closed():
                    try:
                        current_page.screenshot(path=str(out / "failure.png"), full_page=True)
                    except Exception:
                        pass
                raise
            finally:
                browser.close()
    except Exception as error:
        failure = {"viewport": current_viewport, "kind": type(error).__name__, "message": str(error)}
        if current_page is not None and not current_page.is_closed():
            try:
                current_page.screenshot(path=str(out / "failure.png"), full_page=True)
            except Exception:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        server_log.close()
    failed = sum(not row["ok"] for row in checks)
    status = "failed" if failure or failed or len(checks) != 36 else "passed"
    artifact = {
        "kind": "native-local-browser-stored-api-records", "status": status,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "checkout_at_start": start_commit, "checkout_at_end": _commit(),
        "source_sha": latest.get("source_sha"), "run_id": latest["run_id"],
        "expected_report": str(path.relative_to(ROOT)), "url": url,
        "public_deployment_check": False, "paid_calls_made_by_this_verification": 0,
        "python": sys.version.split()[0], "playwright": version("playwright"),
        "streamlit": version("streamlit"), "browser": browser_version,
        "report_hashes": report_hashes, "source_hashes": source_hashes,
        "checks": len(checks), "failed": failed, "exception": failure,
        "results": checks, "screenshots": screenshots,
    }
    (out / "live-records-browser.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rc = 0 if status == "passed" else 1
    (out / "exit-code.txt").write_text(str(rc) + "\n", encoding="utf-8")
    print(json.dumps({key: artifact[key] for key in ("status", "checks", "failed", "exception", "source_sha", "run_id")}, ensure_ascii=False), flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
